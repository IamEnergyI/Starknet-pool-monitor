"""Telegram bot: filters, inline menus, alerts, and scheduled scans."""

from __future__ import annotations

import logging
import math
import os
import re
import time
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
from scanner import scan_dexscreener

logger = logging.getLogger(__name__)


def _parse_allowed_users(raw: str) -> set[int]:
    allowed: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowed.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid Telegram ID in ALLOWED_USERS: %r", part)
    return allowed


ALLOWED_USERS = _parse_allowed_users(os.environ.get("ALLOWED_USERS", ""))

# user_id -> {"field": "<filters column>"}
WAITING_FOR_VALUE: dict[int, dict[str, Any]] = {}

TIMEFRAMES = ("5M", "1H", "6H", "24H")

CHAIN_LABELS = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "bsc": "BNB Chain",
    "base": "Base",
    "arbitrum": "Arbitrum",
    "polygon": "Polygon",
}

NUMERIC_FIELDS: dict[str, dict[str, Any]] = {
    "min_liquidity": {
        "label": "Min Liquidity",
        "kind": "usd",
        "minimum": 0.0,
    },
    "max_mcap": {"label": "Max MCAP", "kind": "usd", "minimum": 0.0},
    "max_age_days": {
        "label": "Max Age",
        "kind": "days",
        "minimum": 0.0,
    },
    "min_volume": {
        "label": "Min Volume",
        "kind": "usd",
        "minimum": 0.0,
    },
    "max_volume": {
        "label": "Max Volume",
        "kind": "usd",
        "minimum": 0.0,
    },
    "min_change": {"label": "Min Change", "kind": "percent", "minimum": None},
    "max_change": {"label": "Max Change", "kind": "percent", "minimum": None},
    "min_buys": {"label": "Min Buys", "kind": "count", "minimum": 0.0},
    "max_buys": {"label": "Max Buys", "kind": "count", "minimum": 0.0},
    "min_sells": {"label": "Min Sells", "kind": "count", "minimum": 0.0},
    "max_sells": {"label": "Max Sells", "kind": "count", "minimum": 0.0},
}

TEXT_FIELDS: dict[str, dict[str, Any]] = {
    "address_suffix": {"label": "Address Suffix", "kind": "text"},
}

EDITABLE_FIELDS: dict[str, dict[str, Any]] = {**NUMERIC_FIELDS, **TEXT_FIELDS}

CORE_FIELDS = (
    "min_liquidity",
    "max_mcap",
    "max_age_days",
    "min_volume",
    "max_volume",
    "min_change",
    "max_change",
)

BUY_SELL_FIELDS = ("min_buys", "max_buys", "min_sells", "max_sells")

_telegram_app: Application | None = None
_initialized = False

REQUIRED_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "WEBHOOK_URL",
    "ALLOWED_USERS",
)


# ---------------------------------------------------------------------------
# Environment / application lifecycle
# ---------------------------------------------------------------------------


def validate_environment() -> None:
    missing = [
        name
        for name in REQUIRED_ENV_VARS
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    webhook_base = (os.environ.get("WEBHOOK_URL") or "").strip()
    if not webhook_base.startswith(("https://", "http://")):
        raise RuntimeError("WEBHOOK_URL must start with https:// or http://")

    if not _parse_allowed_users(os.environ.get("ALLOWED_USERS", "")):
        raise RuntimeError("ALLOWED_USERS must contain at least one Telegram ID")


def _webhook_target() -> str:
    base = (os.environ.get("WEBHOOK_URL") or "").strip().rstrip("/")
    return base if base.endswith("/webhook") else f"{base}/webhook"


def _build_application() -> Application:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    # The updater is intentionally disabled: updates arrive through the
    # FastAPI webhook endpoint, never through long polling.
    application = (
        Application.builder().token(token).updater(None).build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("filter", filter_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_waiting_value)
    )
    application.add_error_handler(error_handler)
    return application


def get_telegram_app() -> Application:
    global _telegram_app

    if _telegram_app is None:
        _telegram_app = _build_application()
    return _telegram_app


async def initialize() -> Application:
    """Initialize Telegram, start the application, and register the webhook."""

    global _initialized

    validate_environment()
    application = get_telegram_app()
    if _initialized:
        return application

    await application.initialize()
    await application.start()

    try:
        await application.bot.set_webhook(
            url=_webhook_target(),
            allowed_updates=Update.ALL_TYPES,
        )
    except Exception:
        await application.stop()
        await application.shutdown()
        raise

    try:
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Start the bot"),
                BotCommand("filter", "Configure token filters"),
                BotCommand("cancel", "Cancel a pending edit"),
            ]
        )
    except TelegramError:
        logger.warning("Could not set Telegram command menu", exc_info=True)

    _initialized = True
    logger.info("Telegram webhook registered at %s", _webhook_target())
    return application


async def shutdown() -> None:
    """Gracefully stop the Telegram application."""

    global _telegram_app, _initialized

    application = _telegram_app
    if application is None:
        return

    try:
        await application.stop()
    except Exception:  # noqa: BLE001 - shutdown must be best-effort
        logger.warning("Error while stopping Telegram application", exc_info=True)

    try:
        await application.shutdown()
    except Exception:  # noqa: BLE001
        logger.warning("Error while shutting down Telegram application", exc_info=True)

    _telegram_app = None
    _initialized = False
    logger.info("Telegram application stopped")


# ---------------------------------------------------------------------------
# Authorization and formatting helpers
# ---------------------------------------------------------------------------


def _is_allowed(user_id: int | None) -> bool:
    return user_id is not None and int(user_id) in ALLOWED_USERS


def _escape_markdown(value: Any) -> str:
    """Escape user-controlled text for Telegram's legacy Markdown parser."""

    return re.sub(r"([_*\[\]`])", r"\\\1", str(value))


def _clean_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _format_money(value: Any) -> str:
    number = _coerce_float(value)
    if number is None:
        return "N/A"

    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"{sign}${absolute / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{sign}${absolute / 1_000:.1f}K"
    return f"{sign}${absolute:,.2f}"


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_field_value(field: str, value: Any) -> str:
    if value is None or value == "":
        return "Any"

    spec = EDITABLE_FIELDS.get(field, {})
    kind = spec.get("kind")

    if kind == "text":
        return _clean_text(value, 40)
    if kind == "usd":
        return _format_money(value)
    if kind == "days":
        number = _coerce_float(value)
        return f"{number:g} d" if number is not None else "Any"
    if kind == "percent":
        number = _coerce_float(value)
        return f"{number:g}%" if number is not None else "Any"
    if kind == "count":
        number = _coerce_float(value)
        return f"{number:g}" if number is not None else "Any"

    return _clean_text(value, 40)


def _format_age(pair: dict[str, Any]) -> str:
    created_at = _coerce_float(pair.get("pairCreatedAt"))
    if created_at is None or created_at <= 0:
        return "Unknown"

    if created_at < 10_000_000_000:
        created_at *= 1000

    age_days = max(
        0.0, ((time.time() * 1000.0) - created_at) / 86_400_000.0
    )
    if age_days < 1:
        return f"{age_days * 24:.1f} hours"
    return f"{age_days:.1f} days"


def _chain_display(chain: Any) -> str:
    raw = str(chain)
    return CHAIN_LABELS.get(raw.lower(), raw)


# ---------------------------------------------------------------------------
# Menu rendering
# ---------------------------------------------------------------------------


def _main_menu_text(filter_data: dict[str, Any]) -> str:
    chains = [
        _chain_display(chain)
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip()
    ]
    chain_text = ", ".join(chains) if chains else "None selected"
    timeframe = filter_data.get("timeframe") or "24H"

    return (
        "🎛 Filter Menu\n\n"
        f"Chains: {chain_text}\n"
        f"Timeframe: {timeframe}\n\n"
        "Choose a category to edit."
    )


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔗 Chains", callback_data="menu:chains"),
                InlineKeyboardButton(
                    "📊 Core Metrics", callback_data="menu:core"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📈 Buys / Sells", callback_data="menu:bs"
                ),
                InlineKeyboardButton("👁 View Active", callback_data="menu:view"),
            ],
        ]
    )


def _core_menu_text(filter_data: dict[str, Any]) -> str:
    timeframe = filter_data.get("timeframe") or "24H"
    return (
        "📊 Core Metrics\n\n"
        f"Timeframe: {timeframe}\n"
        "Tap a metric to edit it, or ✖ to clear it. "
        "Volume and price change use the active timeframe."
    )


def _core_menu_keyboard(
    filter_data: dict[str, Any],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for field in CORE_FIELDS:
        label = EDITABLE_FIELDS[field]["label"]
        value = _format_field_value(field, filter_data.get(field))
        rows.append(
            [
                InlineKeyboardButton(
                    f"{label}: {value}",
                    callback_data=f"edit:{field}",
                ),
                InlineKeyboardButton("✖", callback_data=f"clear:{field}"),
            ]
        )

    timeframe_buttons = [
        InlineKeyboardButton(
            f"{'✅ ' if (filter_data.get('timeframe') or '24H') == timeframe else ''}"
            f"{timeframe}",
            callback_data=f"tf:{timeframe}",
        )
        for timeframe in TIMEFRAMES
    ]
    rows.append(timeframe_buttons)

    suffix = _format_field_value(
        "address_suffix", filter_data.get("address_suffix")
    )
    rows.append(
        [
            InlineKeyboardButton(
                f"Address Suffix: {suffix}",
                callback_data="edit:address_suffix",
            ),
            InlineKeyboardButton(
                "✖", callback_data="clear:address_suffix"
            ),
        ]
    )
    rows.append(
        [InlineKeyboardButton("« Back", callback_data="menu:main")]
    )
    return InlineKeyboardMarkup(rows)


def _buy_sell_menu_text(filter_data: dict[str, Any]) -> str:
    timeframe = filter_data.get("timeframe") or "24H"
    return (
        "📈 Buys / Sells\n\n"
        f"Verified against GeckoTerminal ({timeframe}).\n"
        "Tap a metric to edit it, or ✖ to clear it."
    )


def _buy_sell_menu_keyboard(
    filter_data: dict[str, Any],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for field in BUY_SELL_FIELDS:
        label = EDITABLE_FIELDS[field]["label"]
        value = _format_field_value(field, filter_data.get(field))
        rows.append(
            [
                InlineKeyboardButton(
                    f"{label}: {value}",
                    callback_data=f"edit:{field}",
                ),
                InlineKeyboardButton("✖", callback_data=f"clear:{field}"),
            ]
        )
    rows.append(
        [InlineKeyboardButton("« Back", callback_data="menu:main")]
    )
    return InlineKeyboardMarkup(rows)


def _chains_menu_text(filter_data: dict[str, Any]) -> str:
    chains = [
        _chain_display(chain)
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip()
    ]
    selected = ", ".join(chains) if chains else "None"
    return (
        "🔗 Chains\n\n"
        f"Selected: {selected}\n\n"
        "Tap a chain to toggle it on or off. "
        "At least one chain must be selected to receive alerts."
    )


def _chains_menu_keyboard(
    filter_data: dict[str, Any],
) -> InlineKeyboardMarkup:
    selected = {
        str(chain).strip().lower()
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip()
    }

    chain_buttons: list[InlineKeyboardButton] = []
    for chain_id, label in CHAIN_LABELS.items():
        marker = "✅ " if chain_id in selected else ""
        chain_buttons.append(
            InlineKeyboardButton(
                f"{marker}{label}",
                callback_data=f"chain:{chain_id}",
            )
        )

    rows = [
        chain_buttons[index : index + 2]
        for index in range(0, len(chain_buttons), 2)
    ]
    rows.append(
        [InlineKeyboardButton("« Back", callback_data="menu:main")]
    )
    return InlineKeyboardMarkup(rows)


def _view_menu_text(filter_data: dict[str, Any]) -> str:
    chains = [
        _chain_display(chain)
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip()
    ]

    lines = [
        "👁 Active Filter",
        "",
        f"Name: {filter_data.get('name') or 'Default'}",
        f"Chains: {', '.join(chains) if chains else 'None'}",
        f"Timeframe: {filter_data.get('timeframe') or '24H'}",
        "",
        "Core Metrics",
    ]
    for field in CORE_FIELDS:
        label = EDITABLE_FIELDS[field]["label"]
        lines.append(
            f"• {label}: {_format_field_value(field, filter_data.get(field))}"
        )

    lines.extend(["", "Buys / Sells"])
    for field in BUY_SELL_FIELDS:
        label = EDITABLE_FIELDS[field]["label"]
        lines.append(
            f"• {label}: {_format_field_value(field, filter_data.get(field))}"
        )

    lines.extend(
        [
            "",
            "Advanced",
            "• Address Suffix: "
            + _format_field_value(
                "address_suffix", filter_data.get("address_suffix")
            ),
            "• Labels: "
            + (
                ", ".join(str(label) for label in filter_data.get("labels") or [])
                or "Any"
            ),
            "",
            "Requirements",
            "• Profile: " + ("Yes" if filter_data.get("req_profile") else "No"),
            "• Boosted: " + ("Yes" if filter_data.get("req_boosted") else "No"),
            "• Ads: " + ("Yes" if filter_data.get("req_ads") else "No"),
            "• Launchpad: "
            + ("Yes" if filter_data.get("req_launchpad") else "No"),
        ]
    )
    return "\n".join(lines)


def _view_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Back", callback_data="menu:main")]]
    )


async def _safe_edit(
    query: Any, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        logger.warning("Could not edit Telegram message: %s", exc)
        message = getattr(query, "message", None)
        if message is not None:
            await message.reply_text(text=text, reply_markup=reply_markup)


async def _reply_main_menu(message: Any, filter_data: dict[str, Any]) -> None:
    await message.reply_text(
        _main_menu_text(filter_data),
        reply_markup=_main_menu_keyboard(),
    )


async def _get_or_create_filter(telegram_id: int) -> dict[str, Any]:
    await db.ensure_user(telegram_id)
    return await db.get_or_create_active_filter(telegram_id)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return
    if not _is_allowed(user.id):
        await message.reply_text("⛔ You are not authorized to use this bot.")
        logger.warning("Unauthorized /start from Telegram ID %s", user.id)
        return

    WAITING_FOR_VALUE.pop(user.id, None)

    try:
        await db.ensure_user(user.id)
    except Exception:
        logger.exception("Could not create user %s", user.id)
        await message.reply_text(
            "⚠️ I could not reach the database. Please try again shortly."
        )
        return

    await message.reply_text(
        "👋 Welcome to the Crypto Scanner Bot.\n\n"
        "Use /filter to configure your token filters. "
        "The scanner runs automatically every minute."
    )


async def filter_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return
    if not _is_allowed(user.id):
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    WAITING_FOR_VALUE.pop(user.id, None)

    try:
        filter_data = await _get_or_create_filter(user.id)
    except Exception:
        logger.exception("Could not load filter for %s", user.id)
        await message.reply_text(
            "⚠️ I could not load your filter. Please try again shortly."
        )
        return

    await _reply_main_menu(message, filter_data)


async def cancel_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return
    if not _is_allowed(user.id):
        return

    WAITING_FOR_VALUE.pop(user.id, None)
    await message.reply_text("✖ Edit cancelled.")


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------


async def callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user

    if query is None:
        return
    if user is None or not _is_allowed(user.id):
        await query.answer("⛔ Unauthorized", show_alert=True)
        return

    try:
        await query.answer()
    except TelegramError:
        logger.debug("Could not answer callback query", exc_info=True)

    telegram_id = int(user.id)
    data = query.data or "menu:main"

    try:
        if data == "cancel":
            WAITING_FOR_VALUE.pop(telegram_id, None)

        filter_data = await _get_or_create_filter(telegram_id)
        WAITING_FOR_VALUE.pop(telegram_id, None)

        if data == "cancel":
            await _safe_edit(
                query,
                _main_menu_text(filter_data),
                _main_menu_keyboard(),
            )
            return

        if data.startswith("edit:"):
            field = data.split(":", 1)[1]
            if field not in EDITABLE_FIELDS:
                await _safe_edit(query, "⚠️ Unknown filter field.")
                return

            WAITING_FOR_VALUE[telegram_id] = {"field": field}
            label = EDITABLE_FIELDS[field]["label"]
            if field in TEXT_FIELDS:
                prompt = (
                    f"✏️ Send the new {label}.\n\n"
                    "Send 'any' to clear this filter, or /cancel to abort."
                )
            else:
                prompt = (
                    f"✏️ Send a new numeric value for {label}.\n\n"
                    "Send 'any' to clear this filter, or /cancel to abort."
                )
            await _safe_edit(
                query,
                prompt,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✖ Cancel", callback_data="cancel"
                            )
                        ]
                    ]
                ),
            )
            return

        if data == "menu:chains":
            await _safe_edit(
                query,
                _chains_menu_text(filter_data),
                _chains_menu_keyboard(filter_data),
            )
            return

        if data == "menu:core":
            await _safe_edit(
                query,
                _core_menu_text(filter_data),
                _core_menu_keyboard(filter_data),
            )
            return

        if data == "menu:bs":
            await _safe_edit(
                query,
                _buy_sell_menu_text(filter_data),
                _buy_sell_menu_keyboard(filter_data),
            )
            return

        if data == "menu:view":
            await _safe_edit(
                query,
                _view_menu_text(filter_data),
                _view_menu_keyboard(),
            )
            return

        if data.startswith("chain:"):
            chain = data.split(":", 1)[1].strip().lower()
            if chain not in CHAIN_LABELS:
                await _safe_edit(query, "⚠️ Unsupported chain.")
                return

            chains = [
                str(item).strip().lower()
                for item in (filter_data.get("chains") or [])
                if str(item).strip()
            ]
            if chain in chains:
                chains = [item for item in chains if item != chain]
            else:
                chains.append(chain)

            updated = await db.update_filter(
                str(filter_data["id"]), telegram_id, {"chains": chains}
            )
            if updated is None:
                raise RuntimeError("Filter update returned no row")
            await _safe_edit(
                query,
                _chains_menu_text(updated),
                _chains_menu_keyboard(updated),
            )
            return

        if data.startswith("tf:"):
            timeframe = data.split(":", 1)[1].strip().upper()
            if timeframe not in TIMEFRAMES:
                await _safe_edit(query, "⚠️ Unsupported timeframe.")
                return

            updated = await db.update_filter(
                str(filter_data["id"]),
                telegram_id,
                {"timeframe": timeframe},
            )
            if updated is None:
                raise RuntimeError("Filter update returned no row")
            await _safe_edit(
                query,
                _core_menu_text(updated),
                _core_menu_keyboard(updated),
            )
            return

        if data.startswith("clear:"):
            field = data.split(":", 1)[1]
            if field not in EDITABLE_FIELDS:
                await _safe_edit(query, "⚠️ Unknown filter field.")
                return

            updated = await db.update_filter(
                str(filter_data["id"]), telegram_id, {field: None}
            )
            if updated is None:
                raise RuntimeError("Filter update returned no row")

            if field in CORE_FIELDS or field == "address_suffix":
                await _safe_edit(
                    query,
                    _core_menu_text(updated),
                    _core_menu_keyboard(updated),
                )
            else:
                await _safe_edit(
                    query,
                    _buy_sell_menu_text(updated),
                    _buy_sell_menu_keyboard(updated),
                )
            return

        # Unknown/stale callback: fall back to the main menu.
        await _safe_edit(
            query,
            _main_menu_text(filter_data),
            _main_menu_keyboard(),
        )
    except Exception:
        logger.exception("Callback handling failed for user %s", telegram_id)
        await _safe_edit(
            query,
            "⚠️ Something went wrong. Please send /filter and try again.",
        )


# ---------------------------------------------------------------------------
# Text input handler (numeric/text field editing)
# ---------------------------------------------------------------------------


def _parse_field_value(
    field: str, raw_text: str
) -> tuple[bool, Any, str | None]:
    spec = EDITABLE_FIELDS[field]
    text = raw_text.strip()

    if text.lower() in {"any", "none", "clear", "reset", "-"}:
        return True, None, None

    if spec["kind"] == "text":
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", text):
            return (
                False,
                None,
                "Use 1–32 letters or digits, or send 'any' to clear.",
            )
        return True, text, None

    cleaned = (
        text.replace(",", "")
        .replace("$", "")
        .replace("%", "")
        .strip()
    )
    try:
        value = float(cleaned)
    except ValueError:
        return False, None, "That is not a valid number."

    if not math.isfinite(value):
        return False, None, "The number must be finite."

    minimum = spec.get("minimum")
    if minimum is not None and value < minimum:
        return False, None, f"The value must be at least {minimum:g}."

    return True, value, None


async def handle_waiting_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None or message.text is None:
        return
    if not _is_allowed(user.id):
        WAITING_FOR_VALUE.pop(user.id, None)
        return

    state = WAITING_FOR_VALUE.get(user.id)
    if state is None:
        return

    field = state.get("field")
    if field not in EDITABLE_FIELDS:
        WAITING_FOR_VALUE.pop(user.id, None)
        return

    valid, value, error = _parse_field_value(field, message.text)
    if not valid:
        await message.reply_text(
            f"⚠️ {error}\n\nPlease try again, or send /cancel."
        )
        return

    try:
        filter_data = await _get_or_create_filter(user.id)
        updated = await db.update_filter(
            str(filter_data["id"]), user.id, {field: value}
        )
        if updated is None:
            raise RuntimeError("Filter update returned no row")
    except Exception:
        logger.exception("Could not update filter for %s", user.id)
        await message.reply_text(
            "⚠️ I could not save that value. Please try again."
        )
        return

    WAITING_FOR_VALUE.pop(user.id, None)
    label = EDITABLE_FIELDS[field]["label"]
    await message.reply_text(
        f"✅ {label} set to "
        f"{_format_field_value(field, updated.get(field))}."
    )
    await _reply_main_menu(message, updated)


# ---------------------------------------------------------------------------
# Alerts and scan orchestration
# ---------------------------------------------------------------------------


async def send_alert(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    match: dict[str, Any],
) -> None:
    """Format and send one token alert as Telegram Markdown."""

    pair = match.get("pair")
    if not isinstance(pair, dict):
        return

    base_token = pair.get("baseToken")
    if not isinstance(base_token, dict):
        base_token = {}

    name = _clean_text(base_token.get("name") or base_token.get("symbol"))
    symbol = _clean_text(base_token.get("symbol"))
    if not name:
        name = "Unknown Token"

    token_display = name
    if symbol and symbol.lower() != name.lower():
        token_display = f"{name} ({symbol})"

    liquidity_data = pair.get("liquidity")
    liquidity = (
        _coerce_float(liquidity_data.get("usd"))
        if isinstance(liquidity_data, dict)
        else None
    )

    mcap = _coerce_float(pair.get("marketCap"))
    if mcap is None:
        mcap = _coerce_float(pair.get("fdv"))

    chain = str(pair.get("chainId") or "unknown")
    pair_address = str(pair.get("pairAddress") or "")

    url = str(pair.get("url") or "").strip()
    if not url:
        url = f"https://dexscreener.com/{chain}/{pair_address}"

    text = (
        "🚀 *New Token Match*\n\n"
        f"*Token:* {_escape_markdown(token_display)}\n"
        f"*Chain:* {_escape_markdown(chain)}\n"
        f"*Liquidity:* {_escape_markdown(_format_money(liquidity))}\n"
        f"*MCAP:* {_escape_markdown(_format_money(mcap))}\n"
        f"*Age:* {_escape_markdown(_format_age(pair))}\n\n"
        f"[Open on DexScreener]({url})"
    )

    warning = match.get("warning")
    if warning:
        text += f"\n\n{_escape_markdown(warning)}"

    await context.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def trigger_scan(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    """Scan every active user's filter and send all matching alerts."""

    stats = {
        "active_users": 0,
        "matches": 0,
        "alerts_sent": 0,
        "errors": 0,
    }

    active_users = await db.get_all_active_users()
    stats["active_users"] = len(active_users)
    logger.info("Starting scan for %s active users", len(active_users))

    for entry in active_users:
        telegram_id = entry.get("telegram_id")
        filter_data = entry.get("filter")

        if telegram_id is None or not isinstance(filter_data, dict):
            stats["errors"] += 1
            continue

        try:
            matches = await scan_dexscreener(filter_data)
        except Exception:  # noqa: BLE001 - isolate failures per user
            logger.exception("Scan failed for user %s", telegram_id)
            stats["errors"] += 1
            continue

        stats["matches"] += len(matches)

        for match in matches:
            try:
                await send_alert(context, int(telegram_id), match)
                stats["alerts_sent"] += 1
            except TelegramError:
                logger.exception("Could not send alert to user %s", telegram_id)
                stats["errors"] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unexpected alert error for user %s", telegram_id
                )
                stats["errors"] += 1

    logger.info("Scan complete: %s", stats)
    return stats


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.error(
        "Unhandled Telegram handler error for update %s",
        update,
        exc_info=context.error,
    )


# Re-exported for main.py's generic context construction.
__all__ = [
    "ALLOWED_USERS",
    "WAITING_FOR_VALUE",
    "get_telegram_app",
    "initialize",
    "send_alert",
    "shutdown",
    "trigger_scan",
    "validate_environment",
]
