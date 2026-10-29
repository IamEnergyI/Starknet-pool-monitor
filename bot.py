"""Telegram bot: filters, inline menus, alerts, and scheduled scans."""

from __future__ import annotations

import logging
import math
import os
import re
import time
from datetime import datetime, timezone
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
from scanner import (
    fetch_checked_pair_price,
    fetch_current_prices,
    fetch_pair_price,
    lookup_token,
    scan_dexscreener,
)

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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


PAPER_MAX_AMOUNT = 1_000_000.0
PAPER_STARTING_BALANCE = min(
    PAPER_MAX_AMOUNT, max(1.0, _env_float("PAPER_STARTING_BALANCE", 1000.0))
)
PAPER_FEE_RATE = min(0.5, max(0.0, _env_float("PAPER_FEE_RATE", 0.005)))
PAPER_SLIPPAGE_BPS = min(
    5000, max(0, _env_int("PAPER_SLIPPAGE_BPS", 200))
)
PAPER_BUY_PRESETS = (25.0, 50.0, 100.0)

# user_id -> {"field": "<filters column>"}
WAITING_FOR_VALUE: dict[int, dict[str, Any]] = {}

# user_id -> {"action": "paper_buy"|"paper_sell"|"addfunds"|"setbalance", ...}
WAITING_FOR_PAPER_VALUE: dict[int, dict[str, Any]] = {}

# user_id -> requested reset balance awaiting confirmation
PENDING_PAPER_RESET: dict[int, float] = {}

# user_id -> monotonic deadline for a pending "sell all" confirmation
PENDING_SELL_ALL: dict[int, float] = {}
SELL_ALL_CONFIRM_SECONDS = 300.0

TIMEFRAMES = ("5M", "1H", "6H", "24H")

REQUIREMENT_LABELS = {
    "req_profile": "Profile",
    "req_boosted": "Boosted",
    "req_ads": "Ads",
    "req_launchpad": "Launchpad",
}

CHAIN_LABELS = {
    "solana": "Solana",
    "ethereum": "Ethereum",
    "bsc": "BNB Chain",
    "base": "Base",
    "arbitrum": "Arbitrum",
    "polygon": "Polygon",
    "starknet": "Starknet",
    "robinhood": "Robinhood",
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
    application.add_handler(CommandHandler("portfolio", portfolio_command))
    application.add_handler(CommandHandler("paper", portfolio_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("addfunds", add_funds_command))
    application.add_handler(CommandHandler("setbalance", set_balance_command))
    application.add_handler(CommandHandler("resetpaper", reset_paper_command))
    application.add_handler(CommandHandler("autobuy", auto_buy_command))
    application.add_handler(CommandHandler("autosell", auto_sell_command))
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
                BotCommand("portfolio", "Paper portfolio and PnL"),
                BotCommand("paper", "Alias for /portfolio"),
                BotCommand("history", "Paper trade history"),
                BotCommand("addfunds", "Add fake paper funds"),
                BotCommand("setbalance", "Set starting paper balance"),
                BotCommand("resetpaper", "Reset paper account"),
                BotCommand("autobuy", "Auto-buy new alerts"),
                BotCommand("autosell", "Manage take-profit rules"),
                BotCommand("cancel", "Cancel a pending action"),
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


def _format_money(value: Any, *, compact: bool = False) -> str:
    """Format a USD amount with thousands separators.

    Account and trade amounts are always shown in full (``$1,000.00``).
    ``compact`` abbreviates large values (``$1.0K``) for dense market-data
    lines such as liquidity, market cap, and volume in alerts.
    """

    number = _coerce_float(value)
    if number is None:
        return "N/A"

    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if compact:
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
        return _format_money(value, compact=True)
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


def _format_price(value: Any) -> str:
    number = _coerce_float(value)
    if number is None:
        return "N/A"
    if number >= 1:
        return f"${number:,.4f}"
    if number >= 0.01:
        return f"${number:.6f}"
    return f"${number:.10f}".rstrip("0").rstrip(".")


def _format_quantity(value: Any) -> str:
    number = _coerce_float(value)
    if number is None:
        return "N/A"
    if abs(number) >= 1_000_000:
        return f"{number:,.0f}"
    if abs(number) >= 1_000:
        return f"{number:,.2f}"
    return f"{number:,.6f}".rstrip("0").rstrip(".")


def _format_pct(value: Any) -> str:
    number = _coerce_float(value)
    if number is None:
        return "N/A"
    return f"{number:+.2f}%"


def _format_when(value: Any) -> str:
    if not value:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def _parse_usd_amount(
    raw: str,
    *,
    minimum: float = 1.0,
    maximum: float = PAPER_MAX_AMOUNT,
) -> tuple[float | None, str | None]:
    cleaned = (
        raw.replace(",", "").replace("$", "").lower().replace("usd", "").strip()
    )
    if not cleaned:
        return None, "Enter a dollar amount."
    try:
        amount = round(float(cleaned), 2)
    except ValueError:
        return None, "That is not a valid dollar amount."
    if not math.isfinite(amount):
        return None, "The amount must be finite."
    if amount < minimum:
        return None, f"The minimum amount is ${minimum:,.2f}."
    if amount > maximum:
        return None, f"The maximum amount is ${maximum:,.2f}."
    return amount, None


def _price_key(chain_id: Any, pair_address: Any) -> tuple[str, str]:
    return (
        str(chain_id or "").strip().lower(),
        str(pair_address or "").strip().lower(),
    )


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
    active_requirements = [
        label
        for field, label in REQUIREMENT_LABELS.items()
        if filter_data.get(field)
    ]
    requirement_text = (
        ", ".join(active_requirements) if active_requirements else "None"
    )

    return (
        "🎛 Filter Menu\n\n"
        f"Chains: {chain_text}\n"
        f"Timeframe: {timeframe}\n"
        f"Requirements: {requirement_text}\n\n"
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
            [
                InlineKeyboardButton(
                    "🧩 Requirements", callback_data="menu:req"
                )
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


def _requirements_menu_text(filter_data: dict[str, Any]) -> str:
    return (
        "🧩 Requirements\n\n"
        "These mirror DexScreener's Profile, Boosted, Ads, and Launchpad "
        "global toggles.\n\n"
        "Only enable the same toggles you use on DexScreener; enabling all "
        "four can be very restrictive."
    )


def _requirements_menu_keyboard(
    filter_data: dict[str, Any],
) -> InlineKeyboardMarkup:
    rows = []
    for field, label in REQUIREMENT_LABELS.items():
        marker = "✅ " if filter_data.get(field) else "⬜ "
        rows.append(
            [
                InlineKeyboardButton(
                    f"{marker}{label}",
                    callback_data=f"req:{field}",
                )
            ]
        )
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
# Paper-trading helpers
# ---------------------------------------------------------------------------


async def _ensure_paper_account(telegram_id: int) -> dict[str, Any]:
    await db.ensure_user(telegram_id)
    return await db.get_or_create_paper_account(
        telegram_id, PAPER_STARTING_BALANCE
    )


def _account_float(account: dict[str, Any], key: str) -> float:
    return _coerce_float(account.get(key)) or 0.0


async def _build_portfolio(telegram_id: int) -> dict[str, Any]:
    account = await _ensure_paper_account(telegram_id)
    all_positions = await db.get_paper_positions(telegram_id, status=None)
    open_positions = [
        position
        for position in all_positions
        if position.get("status") == "open"
        and (_coerce_float(position.get("quantity")) or 0.0) > 0
    ]

    requests = [
        (
            str(position.get("token_address") or ""),
            str(position.get("chain_id") or ""),
            str(position.get("pair_address") or ""),
        )
        for position in open_positions
    ]
    references = {
        _price_key(position.get("chain_id"), position.get("pair_address")): (
            _coerce_float(position.get("last_price_usd"))
        )
        for position in open_positions
    }
    prices = (
        await fetch_current_prices(requests, references) if requests else {}
    )

    priced_positions: list[dict[str, Any]] = []
    positions_value = 0.0
    unrealized_total = 0.0
    any_stale = False

    for position in open_positions:
        quantity = _coerce_float(position.get("quantity")) or 0.0
        cost_basis = _coerce_float(position.get("cost_basis_usd")) or 0.0
        entry_price = _coerce_float(position.get("avg_entry_price")) or 0.0

        key = _price_key(
            position.get("chain_id"), position.get("pair_address")
        )
        live_price = prices.get(key)
        last_price = _coerce_float(position.get("last_price_usd"))
        current_price = live_price if live_price is not None else last_price
        stale = live_price is None
        any_stale = any_stale or stale

        market_value: float | None = None
        unrealized: float | None = None
        unrealized_pct: float | None = None
        if current_price is not None:
            market_value = quantity * current_price
            unrealized = market_value - cost_basis
            if cost_basis > 0:
                unrealized_pct = (unrealized / cost_basis) * 100.0
            positions_value += market_value
            unrealized_total += unrealized

        priced_positions.append(
            {
                **position,
                "quantity": quantity,
                "cost_basis_usd": cost_basis,
                "avg_entry_price": entry_price,
                "current_price": current_price,
                "market_value": market_value,
                "unrealized_pnl_usd": unrealized,
                "unrealized_pct": unrealized_pct,
                "price_is_stale": stale,
            }
        )

    cash = _account_float(account, "cash_balance")
    total_deposited = _account_float(account, "total_deposited")
    realized = sum(
        _coerce_float(position.get("realized_pnl_usd")) or 0.0
        for position in all_positions
    )
    closed_positions = [
        position
        for position in all_positions
        if position.get("status") == "closed"
    ]
    wins = sum(
        1
        for position in closed_positions
        if (_coerce_float(position.get("realized_pnl_usd")) or 0.0) > 0
    )
    losses = sum(
        1
        for position in closed_positions
        if (_coerce_float(position.get("realized_pnl_usd")) or 0.0) < 0
    )
    breakeven = len(closed_positions) - wins - losses
    win_rate = (
        (wins / len(closed_positions) * 100.0)
        if closed_positions
        else None
    )
    equity = cash + positions_value
    net_pnl = equity - total_deposited
    net_pct = (net_pnl / total_deposited * 100.0) if total_deposited > 0 else 0.0

    return {
        "account": account,
        "all_positions": all_positions,
        "open_positions": priced_positions,
        "cash": cash,
        "positions_value": positions_value,
        "equity": equity,
        "total_deposited": total_deposited,
        "realized": realized,
        "unrealized": unrealized_total,
        "net_pnl": net_pnl,
        "net_pct": net_pct,
        "closed_count": len(closed_positions),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": win_rate,
        "stale_prices": any_stale,
    }


PORTFOLIO_PAGE_SIZE = 8


def _portfolio_page(
    snapshot: dict[str, Any], page: int
) -> tuple[int, int, list[dict[str, Any]]]:
    total = len(snapshot["open_positions"])
    pages = max(1, (total + PORTFOLIO_PAGE_SIZE - 1) // PORTFOLIO_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * PORTFOLIO_PAGE_SIZE
    return page, pages, snapshot["open_positions"][start : start + PORTFOLIO_PAGE_SIZE]


def _portfolio_text(snapshot: dict[str, Any], page: int = 0) -> str:
    lines = [
        "💼 Paper Portfolio",
        "",
        f"Cash: {_format_money(snapshot['cash'])}",
        f"Positions: {_format_money(snapshot['positions_value'])}",
        f"Equity: {_format_money(snapshot['equity'])}",
        f"Deposited: {_format_money(snapshot['total_deposited'])}",
        (
            f"Net P&L: {_format_money(snapshot['net_pnl'])} "
            f"({_format_pct(snapshot['net_pct'])})"
        ),
        f"Realized: {_format_money(snapshot['realized'])}",
        f"Unrealized: {_format_money(snapshot['unrealized'])}",
        (
            "Auto-buy: "
            + (
                f"ON · {_format_money(snapshot['account'].get('auto_buy_amount'))}"
                if snapshot["account"].get("auto_buy_enabled")
                else "OFF"
            )
        ),
        (
            f"Closed trades: {snapshot['closed_count']} · "
            f"Win rate: "
            + (
                f"{snapshot['win_rate']:.1f}% "
                f"({snapshot['wins']}W / {snapshot['losses']}L"
                + (
                    f" / {snapshot['breakeven']}BE"
                    if snapshot["breakeven"]
                    else ""
                )
                + ")"
                if snapshot["win_rate"] is not None
                else "N/A"
            )
        ),
    ]

    if snapshot["stale_prices"]:
        lines.append("⚠️ Some prices could not be refreshed; last known price used.")

    footer = (
        "Updated "
        + datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    )

    if not snapshot["open_positions"]:
        lines.extend(["", "No open paper positions.", "", footer])
        return "\n".join(lines)

    page, pages, positions = _portfolio_page(snapshot, page)
    if pages > 1:
        lines.append(f"Page {page + 1}/{pages}")

    lines.extend(["", "Open positions"])
    for position in positions:
        symbol = _clean_text(
            position.get("symbol")
            or position.get("name")
            or position.get("token_address"),
            20,
        )
        chain = _chain_display(position.get("chain_id"))
        current_price = _format_price(position.get("current_price"))
        pnl = _format_money(position.get("unrealized_pnl_usd"))
        pct = _format_pct(position.get("unrealized_pct"))
        lines.extend(
            [
                f"• {symbol} · {chain}",
                (
                    f"  {_format_quantity(position.get('quantity'))} @ "
                    f"{_format_price(position.get('avg_entry_price'))}"
                ),
                f"  Now {current_price} · {pnl} ({pct})",
            ]
        )

    lines.extend(
        ["", "Tap a position below to sell part or all of it.", "", footer]
    )
    return "\n".join(lines)


def _portfolio_keyboard(
    snapshot: dict[str, Any], page: int = 0
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    page, pages, positions = _portfolio_page(snapshot, page)

    for position in positions:
        symbol = _clean_text(
            position.get("symbol")
            or position.get("name")
            or position.get("token_address"),
            14,
        )
        pct = _format_pct(position.get("unrealized_pct"))
        rows.append(
            [
                InlineKeyboardButton(
                    f"{symbol}  {pct}",
                    callback_data=f"paper:pos:{position['id']}",
                )
            ]
        )

    if pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(
                InlineKeyboardButton(
                    "⬅️ Previous", callback_data=f"paper:page:{page - 1}"
                )
            )
        if page + 1 < pages:
            navigation.append(
                InlineKeyboardButton(
                    "Next ➡️", callback_data=f"paper:page:{page + 1}"
                )
            )
        if navigation:
            rows.append(navigation)

    if snapshot["open_positions"]:
        rows.append(
            [
                InlineKeyboardButton(
                    f"💸 Sell all ({len(snapshot['open_positions'])})",
                    callback_data="paper:sellall",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 Refresh", callback_data=f"pf:refresh:{page}"
            ),
            InlineKeyboardButton("➕ Add funds", callback_data="paper:addfunds"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🕘 History", callback_data="paper:history"),
            InlineKeyboardButton("♻️ Reset", callback_data="paper:reset"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _position_money_stats(
    position: dict[str, Any], fills: list[dict[str, Any]]
) -> dict[str, float] | None:
    """Summarise how much cash went in, came out, and is still invested.

    Buy fills store the gross amount spent (fee included) in
    ``cash_amount_usd``; sell fills store net proceeds.  A position that was
    fully sold and later re-bought keeps its fills, so the cycle counter below
    resets at each full close to describe the position that is open now.
    """

    if not fills:
        return None

    invested = 0.0
    proceeds = 0.0
    sold_quantity = 0.0
    cycle_bought = 0.0
    cycle_quantity = 0.0

    for fill in fills:
        side = str(fill.get("side") or "").lower()
        quantity = _coerce_float(fill.get("quantity")) or 0.0
        cash = _coerce_float(fill.get("cash_amount_usd")) or 0.0

        if side == "buy":
            invested += cash
            cycle_bought += quantity
            cycle_quantity += quantity
        elif side == "sell":
            proceeds += cash
            sold_quantity += quantity
            cycle_quantity -= quantity
            if cycle_quantity <= 1e-12:
                cycle_quantity = 0.0
                cycle_bought = 0.0

    remaining = _coerce_float(position.get("quantity")) or 0.0
    left_pct = (
        remaining / cycle_bought * 100.0 if cycle_bought > 0 else None
    )

    return {
        "invested": invested,
        "proceeds": proceeds,
        "sold_quantity": sold_quantity,
        "left_pct": left_pct,
    }


def _position_text(
    position: dict[str, Any],
    current_price: float | None,
    fills: list[dict[str, Any]] | None = None,
) -> str:
    quantity = _coerce_float(position.get("quantity")) or 0.0
    cost_basis = _coerce_float(position.get("cost_basis_usd")) or 0.0
    entry = _coerce_float(position.get("avg_entry_price")) or 0.0
    current = current_price
    if current is None:
        current = _coerce_float(position.get("last_price_usd"))

    market_value = quantity * current if current is not None else None
    unrealized = (
        market_value - cost_basis if market_value is not None else None
    )
    unrealized_pct = (
        (unrealized / cost_basis * 100.0)
        if unrealized is not None and cost_basis > 0
        else None
    )
    realized = _coerce_float(position.get("realized_pnl_usd")) or 0.0

    symbol = _clean_text(
        position.get("symbol") or position.get("name") or "Token", 30
    )

    stats = _position_money_stats(position, fills or [])
    left_suffix = ""
    if stats and stats["left_pct"] is not None:
        left_suffix = f" · {stats['left_pct']:.0f}% of position left"

    lines = [
        f"📊 {symbol}",
        "",
        f"Chain: {_chain_display(position.get('chain_id'))}",
        f"Opened: {_format_when(position.get('opened_at'))}",
        "",
        f"Quantity: {_format_quantity(quantity)}{left_suffix}",
        f"Average entry: {_format_price(entry)}",
        f"Current price: {_format_price(current)}",
        f"Value left: {_format_money(market_value)}",
        f"Cost basis left: {_format_money(cost_basis)}",
    ]

    if stats:
        lines.extend(
            [
                "",
                f"Invested: {_format_money(stats['invested'])}",
                (
                    f"Sold: {_format_quantity(stats['sold_quantity'])} "
                    f"for {_format_money(stats['proceeds'])}"
                ),
            ]
        )

    lines.extend(
        [
            f"Realized P&L: {_format_money(realized)}",
            (
                f"Unrealized P&L: {_format_money(unrealized)} "
                f"({_format_pct(unrealized_pct)})"
            ),
        ]
    )

    if stats and stats["invested"] > 0:
        total_pnl = realized + (unrealized or 0.0)
        total_pct = total_pnl / stats["invested"] * 100.0
        lines.append(
            f"Total P&L: {_format_money(total_pnl)} "
            f"({_format_pct(total_pct)} on invested)"
        )

    lines.extend(
        ["", "Sell 25%, 50%, 100%, or a custom percentage."]
    )
    if current_price is None:
        lines.append("⚠️ Live price unavailable; last known price used.")
    return "\n".join(lines)


def _position_keyboard(position: dict[str, Any]) -> InlineKeyboardMarkup:
    position_id = str(position.get("id"))
    rows = [
        [
            InlineKeyboardButton(
                "Sell 25%", callback_data=f"ps:{position_id}:25"
            ),
            InlineKeyboardButton(
                "Sell 50%", callback_data=f"ps:{position_id}:50"
            ),
            InlineKeyboardButton(
                "Sell 100%", callback_data=f"ps:{position_id}:100"
            ),
        ],
        [
            InlineKeyboardButton(
                "Custom %", callback_data=f"ps:{position_id}:custom"
            )
        ],
    ]

    pair_address = str(position.get("pair_address") or "").strip()
    chain_id = str(position.get("chain_id") or "").strip()
    if pair_address and chain_id:
        rows.append(
            [
                InlineKeyboardButton(
                    "📈 DexScreener",
                    url=f"https://dexscreener.com/{chain_id}/{pair_address}",
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton("« Portfolio", callback_data="paper:portfolio")]
    )
    return InlineKeyboardMarkup(rows)


async def _history_text(telegram_id: int) -> str:
    fills = await db.get_paper_fills(telegram_id, limit=20)
    if not fills:
        return "🧾 Paper Trade History\n\nNo paper trades yet."

    lines = ["🧾 Paper Trade History", ""]
    for fill in fills:
        side = str(fill.get("side") or "").upper()
        symbol = _clean_text(fill.get("symbol") or "Token", 18)
        quantity = _format_quantity(fill.get("quantity"))
        price = _format_price(fill.get("price_usd"))
        amount = _format_money(fill.get("cash_amount_usd"))
        when = _format_when(fill.get("created_at"))

        line = f"• {when} · {side} {symbol} · {quantity} @ {price} · {amount}"
        if side == "SELL":
            realized = _coerce_float(fill.get("realized_pnl_usd")) or 0.0
            line += f" · P&L {_format_money(realized)}"
        lines.append(line)

    lines.extend(["", "Paper trades only — no real funds were used."])
    return "\n".join(lines)


async def _show_portfolio_message(
    message: Any, telegram_id: int, page: int = 0
) -> None:
    snapshot = await _build_portfolio(telegram_id)
    await message.reply_text(
        _portfolio_text(snapshot, page),
        reply_markup=_portfolio_keyboard(snapshot, page),
    )


async def _edit_portfolio(
    query: Any, telegram_id: int, page: int = 0
) -> None:
    snapshot = await _build_portfolio(telegram_id)
    await _safe_edit(
        query,
        _portfolio_text(snapshot, page),
        _portfolio_keyboard(snapshot, page),
    )


def _price_refusal_text(status: str) -> str:
    """Explain why a paper trade was refused because of a bad price."""

    if status == "source_disagreement":
        return (
            "⚠️ Paper trade skipped: DexScreener and GeckoTerminal report "
            "very different prices for this pool, so the price cannot be "
            "trusted. Try again in a minute."
        )
    if status == "unconfirmed_move":
        return (
            "⚠️ Paper trade skipped: the pool price jumped far beyond the "
            "last known price and only one source confirms it. Try again in "
            "a minute."
        )
    return (
        "⚠️ I could not get a valid price for this token. Try again shortly."
    )


def _paper_error_text(exc: Exception) -> str:
    if isinstance(exc, db.PaperTradingError):
        return exc.message

    message = str(exc).lower()
    logger.error("Paper-trading error: %s", exc)

    setup_markers = (
        "could not find the function",
        "schema cache",
        "pgrst202",
        "undefined function",
        "undefined table",
        "does not exist",
        "relation \"paper_",
    )
    if any(marker in message for marker in setup_markers):
        return (
            "⚠️ Paper trading is not set up in Supabase yet.\n\n"
            "Run the updated supabase_schema.sql in the Supabase SQL Editor, "
            "then wait a few seconds and try again. The file also reloads the "
            "PostgREST schema cache."
        )

    access_markers = (
        "permission denied",
        "row-level security",
        "violates row-level security",
        "invalid api key",
        "jwt",
        "unauthorized",
        "no api key",
    )
    if any(marker in message for marker in access_markers):
        return (
            "⚠️ Supabase rejected the paper-trading request.\n\n"
            "Check that SUPABASE_KEY is the project's service-role key and "
            "that the Supabase URL matches the project where you ran the schema."
        )

    if "timeout" in message or "connection" in message:
        return "⚠️ The database connection timed out. Please try again."

    return "⚠️ The paper trade could not be completed. Please try again."


async def _execute_paper_buy(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    alert_id: int,
    amount: float,
    *,
    auto: bool = False,
) -> bool:
    alert = await db.get_paper_alert(alert_id, telegram_id)
    if alert is None:
        await context.bot.send_message(
            chat_id=telegram_id,
            text="⚠️ That alert is no longer available.",
        )
        return False

    alert_price = _coerce_float(alert.get("price_usd"))
    price, price_status = await fetch_checked_pair_price(
        str(alert.get("token_address") or ""),
        str(alert.get("chain_id") or ""),
        str(alert.get("pair_address") or ""),
        reference=alert_price,
    )
    if price is None and price_status in {
        "source_disagreement",
        "unconfirmed_move",
    }:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=_price_refusal_text(price_status),
        )
        return False

    stale = price is None
    if price is None:
        price = alert_price
    if price is None or price <= 0:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=_price_refusal_text(price_status),
        )
        return False

    try:
        await _ensure_paper_account(telegram_id)
        fill = await db.execute_paper_buy(
            telegram_id,
            alert_id,
            amount,
            price,
            PAPER_FEE_RATE,
            PAPER_SLIPPAGE_BPS,
        )
    except Exception as exc:  # noqa: BLE001 - user-facing failure
        await context.bot.send_message(
            chat_id=telegram_id,
            text=_paper_error_text(exc),
        )
        return False

    symbol = _clean_text(fill.get("symbol") or alert.get("symbol") or "Token", 30)
    heading = "🤖 Auto-buy filled" if auto else "✅ Paper buy filled"
    text = (
        f"{heading}: {symbol}\n\n"
        f"Spent: {_format_money(fill.get('cash_amount_usd'))}\n"
        f"Fee: {_format_money(fill.get('fee_usd'))}\n"
        f"Average fill price: {_format_price(fill.get('price_usd'))}\n"
        f"Quantity: {_format_quantity(fill.get('quantity'))}\n"
        f"Cash left: {_format_money(fill.get('cash_balance'))}"
    )
    if stale:
        text += "\n\n⚠️ Live price was unavailable; the alert price was used."

    try:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "💼 View portfolio", callback_data="paper:portfolio"
                        )
                    ]
                ]
            ),
        )
    except TelegramError:
        logger.exception(
            "Paper buy executed for %s but confirmation failed", telegram_id
        )
    return True


async def _execute_paper_sell(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    position_id: str,
    fraction: float,
) -> None:
    position = await db.get_paper_position(position_id, telegram_id)
    if (
        position is None
        or position.get("status") != "open"
        or (_coerce_float(position.get("quantity")) or 0.0) <= 0
    ):
        await context.bot.send_message(
            chat_id=telegram_id,
            text="⚠️ That paper position is no longer open.",
        )
        return

    last_price = _coerce_float(position.get("last_price_usd"))
    price, price_status = await fetch_checked_pair_price(
        str(position.get("token_address") or ""),
        str(position.get("chain_id") or ""),
        str(position.get("pair_address") or ""),
        reference=last_price,
    )
    if price is None and price_status in {
        "source_disagreement",
        "unconfirmed_move",
    }:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=_price_refusal_text(price_status),
        )
        return

    stale = price is None
    if price is None:
        price = last_price
    if price is None or price <= 0:
        await context.bot.send_message(
            chat_id=telegram_id,
            text=_price_refusal_text(price_status),
        )
        return

    try:
        fill = await db.execute_paper_sell(
            telegram_id,
            position_id,
            fraction,
            price,
            PAPER_FEE_RATE,
            PAPER_SLIPPAGE_BPS,
        )
    except Exception as exc:  # noqa: BLE001 - user-facing failure
        await context.bot.send_message(
            chat_id=telegram_id,
            text=_paper_error_text(exc),
        )
        return

    symbol = _clean_text(fill.get("symbol") or position.get("symbol") or "Token", 30)
    status = str(fill.get("position_status") or "open")
    text = (
        f"✅ Paper sell filled: {symbol}\n\n"
        f"Sold: {_format_quantity(fill.get('quantity'))}\n"
        f"Average fill price: {_format_price(fill.get('price_usd'))}\n"
        f"Proceeds: {_format_money(fill.get('cash_amount_usd'))}\n"
        f"Fee: {_format_money(fill.get('fee_usd'))}\n"
        f"Realized P&L: {_format_money(fill.get('realized_pnl_usd'))}\n"
        f"Position: {'Closed' if status == 'closed' else 'Still open'}\n"
        f"Cash: {_format_money(fill.get('cash_balance'))}"
    )
    if stale:
        text += "\n\n⚠️ Live price was unavailable; the last known price was used."

    await context.bot.send_message(
        chat_id=telegram_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💼 View portfolio", callback_data="paper:portfolio"
                    )
                ]
            ]
        ),
    )


async def _prompt_sell_all(query: Any, telegram_id: int) -> None:
    """Ask for confirmation before selling every open position."""

    snapshot = await _build_portfolio(telegram_id)
    positions = snapshot["open_positions"]
    if not positions:
        await _safe_edit(query, "📭 There are no open positions to sell.")
        return

    PENDING_SELL_ALL[telegram_id] = time.monotonic() + SELL_ALL_CONFIRM_SECONDS
    await _safe_edit(
        query,
        "💸 Sell all positions?\n\n"
        f"Positions: {len(positions)}\n"
        f"Estimated value: {_format_money(snapshot['positions_value'])}\n\n"
        "This sells 100% of every open position at the current market price. "
        "Positions whose price cannot be verified are skipped.",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Sell everything",
                        callback_data="paper:sellall:yes",
                    ),
                    InlineKeyboardButton(
                        "✖ Cancel", callback_data="paper:sellall:no"
                    ),
                ]
            ]
        ),
    )


async def _execute_sell_all(query: Any, telegram_id: int) -> None:
    """Sell 100% of every open position and report a single summary."""

    portfolio_button = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« Portfolio", callback_data="paper:portfolio")]]
    )

    deadline = PENDING_SELL_ALL.pop(telegram_id, 0.0)
    if not deadline or time.monotonic() > deadline:
        await _safe_edit(
            query,
            "⌛ That confirmation expired. Tap “Sell all” again.",
            portfolio_button,
        )
        return

    positions = [
        position
        for position in await db.get_paper_positions(
            telegram_id, status="open"
        )
        if (_coerce_float(position.get("quantity")) or 0.0) > 0
    ]
    if not positions:
        await _safe_edit(
            query, "📭 There are no open positions to sell.", portfolio_button
        )
        return

    requests = [
        (
            str(position.get("token_address") or ""),
            str(position.get("chain_id") or ""),
            str(position.get("pair_address") or ""),
        )
        for position in positions
    ]
    references = {
        _price_key(position.get("chain_id"), position.get("pair_address")): (
            _coerce_float(position.get("last_price_usd"))
        )
        for position in positions
    }
    await _safe_edit(query, f"⏳ Selling {len(positions)} position(s)…")

    prices = await fetch_current_prices(requests, references)

    sold = 0
    skipped: list[str] = []
    proceeds = 0.0
    realized = 0.0

    for position in positions:
        symbol = _clean_text(
            position.get("symbol") or position.get("name") or "Token", 20
        )
        key = _price_key(position.get("chain_id"), position.get("pair_address"))
        price = prices.get(key)
        if price is None or price <= 0:
            skipped.append(f"{symbol} — price unverified")
            continue

        try:
            fill = await db.execute_paper_sell(
                telegram_id,
                str(position["id"]),
                1.0,
                price,
                PAPER_FEE_RATE,
                PAPER_SLIPPAGE_BPS,
            )
        except db.PaperTradingError as exc:
            skipped.append(f"{symbol} — {_clean_text(exc.message, 40)}")
            continue
        except Exception:  # noqa: BLE001 - one bad position must not stop the rest
            logger.exception(
                "Sell-all failed for position %s of user %s",
                position.get("id"),
                telegram_id,
            )
            skipped.append(f"{symbol} — temporary error")
            continue

        sold += 1
        proceeds += _coerce_float(fill.get("cash_amount_usd")) or 0.0
        realized += _coerce_float(fill.get("realized_pnl_usd")) or 0.0

    if sold == 0:
        lines = ["⚠️ Nothing was sold.", ""]
    else:
        lines = [
            "✅ Sell-all complete",
            "",
            f"Closed: {sold} position(s)",
            f"Proceeds: {_format_money(proceeds)}",
            f"Realized P&L: {_format_money(realized)}",
        ]

    if skipped:
        lines.extend(["", f"Still open ({len(skipped)}):"])
        lines.extend(f"• {entry}" for entry in skipped[:10])
        if len(skipped) > 10:
            lines.append(f"…and {len(skipped) - 10} more")
        if sold:
            lines.extend(
                ["", "Open a position below to sell it individually."]
            )

    await _safe_edit(query, "\n".join(lines), portfolio_button)


async def _prompt_paper_input(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    text: str,
    state: dict[str, Any],
) -> None:
    WAITING_FOR_VALUE.pop(telegram_id, None)
    PENDING_PAPER_RESET.pop(telegram_id, None)
    WAITING_FOR_PAPER_VALUE[telegram_id] = state
    await context.bot.send_message(
        chat_id=telegram_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✖ Cancel", callback_data="paper:cancel")]]
        ),
    )


async def _trigger_reset_confirmation(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    balance: float,
) -> None:
    await db.ensure_user(telegram_id)
    WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
    WAITING_FOR_VALUE.pop(telegram_id, None)
    PENDING_PAPER_RESET[telegram_id] = balance
    await context.bot.send_message(
        chat_id=telegram_id,
        text=(
            "♻️ Reset paper account?\n\n"
            f"New starting balance: {_format_money(balance)}\n"
            "This deletes all paper positions, fills, alerts, and deposits. "
            "Your filters, sent-token history, auto-buy setting, and auto-sell "
            "rules stay intact."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Reset", callback_data="preset:confirm"
                    ),
                    InlineKeyboardButton(
                        "✖ Cancel", callback_data="preset:cancel"
                    ),
                ]
            ]
        ),
    )


async def _handle_paper_callback(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    data: str,
) -> None:
    try:
        WAITING_FOR_VALUE.pop(telegram_id, None)
        WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
        if data == "paper:portfolio":
            await _edit_portfolio(query, telegram_id)
            return

        if data == "paper:sellall":
            await _prompt_sell_all(query, telegram_id)
            return

        if data == "paper:sellall:yes":
            await _execute_sell_all(query, telegram_id)
            return

        if data == "paper:sellall:no":
            PENDING_SELL_ALL.pop(telegram_id, None)
            await _safe_edit(query, "✖ Sell-all cancelled.")
            return

        if data.startswith("pf:refresh"):
            parts = data.split(":")
            page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            await _edit_portfolio(query, telegram_id, page)
            return

        if data.startswith("paper:page:"):
            page_token = data.split(":", 2)[2]
            page = int(page_token) if page_token.isdigit() else 0
            await _edit_portfolio(query, telegram_id, page)
            return

        if data == "paper:history":
            await _safe_edit(
                query,
                await _history_text(telegram_id),
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "« Portfolio", callback_data="paper:portfolio"
                            )
                        ]
                    ]
                ),
            )
            return

        if data == "paper:addfunds":
            await _prompt_paper_input(
                context,
                telegram_id,
                (
                    "➕ Add paper funds\n\n"
                    "Send the amount in USD, for example: 250\n"
                    "This adds cash and is tracked as a deposit, not profit."
                ),
                {"action": "addfunds"},
            )
            return

        if data == "paper:reset":
            account = await db.get_paper_account(telegram_id)
            balance = (
                _coerce_float(account.get("starting_balance"))
                if account
                else None
            ) or PAPER_STARTING_BALANCE
            await _trigger_reset_confirmation(
                context, telegram_id, balance
            )
            return

        if data == "paper:cancel":
            WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
            PENDING_PAPER_RESET.pop(telegram_id, None)
            await context.bot.send_message(
                chat_id=telegram_id,
                text="✖ Paper action cancelled.",
            )
            return

        if data == "preset:confirm":
            balance = PENDING_PAPER_RESET.pop(telegram_id, None)
            if balance is None:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text="⚠️ No reset is pending. Use /resetpaper first.",
                )
                return
            try:
                account = await db.reset_paper_account(telegram_id, balance)
            except Exception as exc:  # noqa: BLE001
                await context.bot.send_message(
                    chat_id=telegram_id, text=_paper_error_text(exc)
                )
                return
            await context.bot.send_message(
                chat_id=telegram_id,
                text=(
                    "♻️ Paper account reset.\n\n"
                    f"Cash: {_format_money(account.get('cash_balance'))}"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "💼 Portfolio", callback_data="paper:portfolio"
                            )
                        ]
                    ]
                ),
            )
            return

        if data == "preset:cancel":
            PENDING_PAPER_RESET.pop(telegram_id, None)
            await context.bot.send_message(
                chat_id=telegram_id, text="✖ Reset cancelled."
            )
            return

        if data.startswith("paper:pos:"):
            position_id = data.split(":", 2)[2]
            position = await db.get_paper_position(position_id, telegram_id)
            if (
                position is None
                or position.get("status") != "open"
                or (_coerce_float(position.get("quantity")) or 0.0) <= 0
            ):
                await _safe_edit(
                    query, "⚠️ That paper position is no longer open."
                )
                return
            price = await fetch_pair_price(
                str(position.get("token_address") or ""),
                str(position.get("chain_id") or ""),
                str(position.get("pair_address") or ""),
            )
            try:
                fills = await db.get_position_fills(telegram_id, position_id)
            except Exception:  # noqa: BLE001 - the screen still works without it
                logger.exception(
                    "Could not load fills for position %s", position_id
                )
                fills = []
            await _safe_edit(
                query,
                _position_text(position, price, fills),
                _position_keyboard(position),
            )
            return

        if data.startswith("pb:"):
            parts = data.split(":")
            if len(parts) != 3:
                await _safe_edit(query, "⚠️ Invalid buy button.")
                return
            alert_id = int(parts[1])
            amount_token = parts[2]
            if amount_token == "custom":
                await _prompt_paper_input(
                    context,
                    telegram_id,
                    (
                        "📄 Custom paper buy\n\n"
                        "Send the USD amount to spend, for example: 75"
                    ),
                    {"action": "paper_buy", "alert_id": alert_id},
                )
                return
            amount = _coerce_float(amount_token)
            if amount is None or amount <= 0:
                await _safe_edit(query, "⚠️ Invalid buy amount.")
                return
            await _execute_paper_buy(context, telegram_id, alert_id, amount)
            return

        if data.startswith("ps:"):
            parts = data.split(":")
            if len(parts) != 3:
                await _safe_edit(query, "⚠️ Invalid sell button.")
                return
            position_id = parts[1]
            fraction_token = parts[2]
            if fraction_token == "custom":
                await _prompt_paper_input(
                    context,
                    telegram_id,
                    (
                        "📉 Custom paper sell\n\n"
                        "Send the percentage of the position to sell (1–100), "
                        "for example: 40"
                    ),
                    {"action": "paper_sell", "position_id": position_id},
                )
                return
            percent = _coerce_float(fraction_token)
            if percent is None or percent <= 0 or percent > 100:
                await _safe_edit(query, "⚠️ Invalid sell percentage.")
                return
            await _execute_paper_sell(
                context, telegram_id, position_id, percent / 100.0
            )
            return

        await _safe_edit(query, "⚠️ Unknown paper-trading action.")
    except db.PaperTradingError as exc:
        await context.bot.send_message(chat_id=telegram_id, text=exc.message)
    except Exception:
        logger.exception("Paper callback failed for user %s", telegram_id)
        await _safe_edit(
            query, "⚠️ Something went wrong with the paper trade."
        )


async def _handle_paper_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict[str, Any],
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.text is None:
        return

    telegram_id = int(user.id)
    action = state.get("action")
    text = message.text.strip()

    if action == "paper_buy":
        amount, error = _parse_usd_amount(text)
        if error:
            await message.reply_text(f"⚠️ {error}\n\nTry again, or send /cancel.")
            return
        WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
        await _execute_paper_buy(
            context, telegram_id, int(state["alert_id"]), amount
        )
        return

    if action == "paper_sell":
        raw_percent = text.replace("%", "").replace(",", "").strip()
        try:
            percent = float(raw_percent)
        except ValueError:
            await message.reply_text(
                "⚠️ Enter a percentage between 1 and 100."
            )
            return
        if not math.isfinite(percent) or percent <= 0 or percent > 100:
            await message.reply_text(
                "⚠️ Enter a percentage between 1 and 100."
            )
            return
        WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
        await _execute_paper_sell(
            context,
            telegram_id,
            str(state["position_id"]),
            percent / 100.0,
        )
        return

    if action == "addfunds":
        amount, error = _parse_usd_amount(text)
        if error:
            await message.reply_text(f"⚠️ {error}\n\nTry again, or send /cancel.")
            return
        WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
        try:
            await _ensure_paper_account(telegram_id)
            account = await db.add_paper_funds(telegram_id, amount)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(_paper_error_text(exc))
            return
        await message.reply_text(
            f"✅ Added {_format_money(amount)} in paper funds.\n"
            f"Cash: {_format_money(account.get('cash_balance'))}\n"
            f"Total deposited: {_format_money(account.get('total_deposited'))}"
        )
        return

    if action == "setbalance":
        amount, error = _parse_usd_amount(text)
        if error:
            await message.reply_text(f"⚠️ {error}\n\nTry again, or send /cancel.")
            return
        WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
        try:
            await _ensure_paper_account(telegram_id)
            account = await db.set_paper_balance(telegram_id, amount)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(_paper_error_text(exc))
            return
        await message.reply_text(
            f"✅ Starting paper balance set to "
            f"{_format_money(account.get('starting_balance'))}."
        )
        return

    WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
    await message.reply_text("⚠️ Unknown paper-trading action.")


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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

    try:
        await db.ensure_user(user.id)
        await _ensure_paper_account(user.id)
    except Exception as exc:
        logger.exception("Could not create user %s", user.id)
        await message.reply_text(_paper_error_text(exc))
        return

    await message.reply_text(
        "👋 Welcome to the Crypto Scanner Bot.\n\n"
        "Use /filter to configure your token filters.\n"
        "Paste any token contract address to see its details and paper-buy it.\n"
        f"Your paper balance starts at {_format_money(PAPER_STARTING_BALANCE)}.\n"
        "Use /setbalance before your first trade to choose another amount.\n"
        "Use /portfolio to see PnL and /addfunds to top up fake cash anytime.\n"
        "Use /autobuy 25 to buy every new alert automatically.\n"
        "Use /autosell add 2 50 to sell 50% at 2x."
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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)
    await message.reply_text("✖ Action cancelled.")


async def portfolio_command(
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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

    try:
        await _show_portfolio_message(message, user.id)
    except Exception as exc:
        logger.exception("Could not load portfolio for %s", user.id)
        await message.reply_text(_paper_error_text(exc))


async def history_command(
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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

    try:
        text = await _history_text(user.id)
    except Exception as exc:
        logger.exception("Could not load paper history for %s", user.id)
        await message.reply_text(_paper_error_text(exc))
        return

    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "« Portfolio", callback_data="paper:portfolio"
                    )
                ]
            ]
        ),
    )


async def add_funds_command(
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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

    if context.args:
        amount, error = _parse_usd_amount(" ".join(context.args))
        if error:
            await message.reply_text(f"⚠️ {error}")
            return
        try:
            await _ensure_paper_account(user.id)
            account = await db.add_paper_funds(user.id, amount)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(_paper_error_text(exc))
            return
        await message.reply_text(
            f"✅ Added {_format_money(amount)} in paper funds.\n"
            f"Cash: {_format_money(account.get('cash_balance'))}\n"
            f"Total deposited: {_format_money(account.get('total_deposited'))}"
        )
        return

    await _prompt_paper_input(
        context,
        user.id,
        (
            "➕ Add paper funds\n\n"
            "Send the amount in USD, for example: 250\n"
            "This adds cash and is tracked as a deposit, not profit."
        ),
        {"action": "addfunds"},
    )


async def set_balance_command(
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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

    if context.args:
        amount, error = _parse_usd_amount(" ".join(context.args))
        if error:
            await message.reply_text(f"⚠️ {error}")
            return
        try:
            await _ensure_paper_account(user.id)
            account = await db.set_paper_balance(user.id, amount)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(_paper_error_text(exc))
            return
        await message.reply_text(
            f"✅ Starting paper balance set to "
            f"{_format_money(account.get('starting_balance'))}."
        )
        return

    await _prompt_paper_input(
        context,
        user.id,
        (
            "💵 Set starting paper balance\n\n"
            "Send the starting amount in USD, for example: 1000\n"
            "This is only allowed before your first paper trade."
        ),
        {"action": "setbalance"},
    )


async def reset_paper_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return
    if not _is_allowed(user.id):
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    balance = PAPER_STARTING_BALANCE
    try:
        account = await db.get_paper_account(user.id)
    except Exception:
        logger.warning("Could not load account for reset", exc_info=True)
        account = None
    if account:
        balance = _coerce_float(account.get("starting_balance")) or balance

    if context.args:
        parsed_balance, error = _parse_usd_amount(" ".join(context.args))
        if error:
            await message.reply_text(f"⚠️ {error}")
            return
        balance = parsed_balance

    await _trigger_reset_confirmation(context, user.id, balance)


def _auto_sell_rules_text(rules: list[dict[str, Any]]) -> str:
    lines = ["🎯 Auto-Sell Rules", ""]
    if not rules:
        lines.append("No automatic take-profit rules configured.")
    else:
        for rule in rules:
            rule_id = int(rule["id"])
            multiple = _coerce_float(rule.get("target_multiple")) or 0.0
            percent = _coerce_float(rule.get("sell_percent")) or 0.0
            lines.append(
                f"#{rule_id} · At {multiple:g}x, sell {percent:g}% of "
                "the remaining position"
            )
    lines.extend(
        [
            "",
            "Rules apply to all open and future paper positions.",
            "Percentages are based on the quantity remaining at trigger time.",
            "",
            "Add: /autosell add 2 50",
            "Remove: /autosell remove <rule id>",
            "Clear all: /autosell clear",
        ]
    )
    return "\n".join(lines)


async def _reply_auto_sell_rules(message: Any, telegram_id: int) -> None:
    rules = await db.get_auto_sell_rules(telegram_id, enabled_only=False)
    await message.reply_text(_auto_sell_rules_text(rules))


async def auto_buy_command(
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
    WAITING_FOR_PAPER_VALUE.pop(user.id, None)
    PENDING_PAPER_RESET.pop(user.id, None)

    try:
        account = await _ensure_paper_account(user.id)
    except Exception as exc:  # noqa: BLE001
        await message.reply_text(_paper_error_text(exc))
        return

    if not context.args:
        enabled = bool(account.get("auto_buy_enabled"))
        amount = _format_money(account.get("auto_buy_amount"))
        await message.reply_text(
            "🤖 Auto-Buy\n\n"
            f"Status: {'ON' if enabled else 'OFF'}\n"
            f"Amount per new alert: {amount}\n\n"
            "Enable: /autobuy 25\n"
            "Disable: /autobuy off"
        )
        return

    first = context.args[0].strip().lower()
    if first in {"off", "disable", "stop"}:
        try:
            account = await db.set_auto_buy(user.id, False)
        except Exception as exc:  # noqa: BLE001
            await message.reply_text(_paper_error_text(exc))
            return
        await message.reply_text(
            "🤖 Auto-buy disabled.\n"
            f"Saved amount: {_format_money(account.get('auto_buy_amount'))}"
        )
        return

    raw_amount = " ".join(
        context.args[1:] if first == "on" else context.args
    )
    amount, error = _parse_usd_amount(raw_amount)
    if error:
        await message.reply_text(f"⚠️ {error}")
        return

    try:
        account = await db.set_auto_buy(user.id, True, amount)
    except Exception as exc:  # noqa: BLE001
        await message.reply_text(_paper_error_text(exc))
        return

    await message.reply_text(
        "🤖 Auto-buy enabled.\n"
        f"Amount per new alert: {_format_money(account.get('auto_buy_amount'))}"
    )


async def auto_sell_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return
    if not _is_allowed(user.id):
        await message.reply_text("⛔ You are not authorized to use this bot.")
        return

    args = [arg.strip() for arg in context.args if arg.strip()]
    subcommand = args[0].lower() if args else "list"

    try:
        await _ensure_paper_account(user.id)

        if subcommand in {"list", "show"}:
            await _reply_auto_sell_rules(message, user.id)
            return

        if subcommand == "clear":
            await db.clear_auto_sell_rules(user.id)
            await message.reply_text("🧹 All auto-sell rules removed.")
            return

        if subcommand == "remove":
            if len(args) < 2:
                await message.reply_text("Usage: /autosell remove <rule id>")
                return
            try:
                rule_id = int(args[1].lstrip("#"))
            except ValueError:
                await message.reply_text("⚠️ The rule id must be a number.")
                return
            deleted = await db.delete_auto_sell_rule(user.id, rule_id)
            if deleted:
                await message.reply_text(f"🗑 Removed auto-sell rule #{rule_id}.")
            else:
                await message.reply_text(
                    f"⚠️ No auto-sell rule #{rule_id} found."
                )
            return

        if subcommand == "add":
            if len(args) < 3:
                await message.reply_text(
                    "Usage: /autosell add <multiple> <percent>\n"
                    "Example: /autosell add 2 50"
                )
                return
            try:
                multiple = float(args[1].lower().rstrip("x"))
                percent = float(args[2].rstrip("%"))
            except ValueError:
                await message.reply_text(
                    "⚠️ Multiple and percent must be numbers. "
                    "Example: /autosell add 2 50"
                )
                return
            if not math.isfinite(multiple) or multiple <= 1:
                await message.reply_text(
                    "⚠️ The target multiple must be greater than 1."
                )
                return
            if not math.isfinite(percent) or percent <= 0 or percent > 100:
                await message.reply_text(
                    "⚠️ The sell percentage must be between 0 and 100."
                )
                return

            try:
                rule = await db.add_auto_sell_rule(
                    user.id, multiple, percent
                )
            except db.PaperTradingError as exc:
                await message.reply_text(f"⚠️ {exc.message}")
                return

            await message.reply_text(
                f"✅ Auto-sell rule added: at {multiple:g}x, sell "
                f"{percent:g}% of the remaining position.\n"
                f"Rule id: #{rule['id']}"
            )
            return

        await message.reply_text(
            "Usage:\n"
            "/autosell — list rules\n"
            "/autosell add 2 50 — at 2x, sell 50%\n"
            "/autosell remove <id>\n"
            "/autosell clear"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-sell command failed for user %s", user.id)
        await message.reply_text(_paper_error_text(exc))


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

    if data.startswith(("pb:", "ps:", "pf:", "paper:", "preset:")):
        await _handle_paper_callback(query, context, telegram_id, data)
        return

    try:
        WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
        PENDING_PAPER_RESET.pop(telegram_id, None)
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

            WAITING_FOR_PAPER_VALUE.pop(telegram_id, None)
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

        if data == "menu:req":
            await _safe_edit(
                query,
                _requirements_menu_text(filter_data),
                _requirements_menu_keyboard(filter_data),
            )
            return

        if data.startswith("req:"):
            field = data.split(":", 1)[1]
            if field not in REQUIREMENT_LABELS:
                await _safe_edit(query, "⚠️ Unknown requirement.")
                return
            updated = await db.update_filter(
                str(filter_data["id"]),
                telegram_id,
                {field: not bool(filter_data.get(field))},
            )
            if updated is None:
                raise RuntimeError("Filter update returned no row")
            await _safe_edit(
                query,
                _requirements_menu_text(updated),
                _requirements_menu_keyboard(updated),
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
        WAITING_FOR_PAPER_VALUE.pop(user.id, None)
        return

    paper_state = WAITING_FOR_PAPER_VALUE.get(user.id)
    if paper_state is not None:
        await _handle_paper_text(update, context, paper_state)
        return

    state = WAITING_FOR_VALUE.get(user.id)
    if state is None:
        # No pending edit: a bare contract address triggers a token lookup.
        address = _extract_token_address(message.text)
        if address is not None:
            await _handle_token_lookup(update, context, address)
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


EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _extract_token_address(text: str) -> str | None:
    """Return a contract address when the whole message is one."""

    candidate = (text or "").strip()
    if not candidate or len(candidate) > 64:
        return None
    if EVM_ADDRESS_RE.fullmatch(candidate):
        return candidate
    if SOLANA_ADDRESS_RE.fullmatch(candidate):
        return candidate
    return None


def _lookup_text(
    pair: dict[str, Any],
    status: str,
    warning: str | None,
    can_buy: bool,
) -> str:
    """Format a pasted-address lookup as Telegram Markdown."""

    base_token = pair.get("baseToken")
    if not isinstance(base_token, dict):
        base_token = {}

    name = _clean_text(
        base_token.get("name") or base_token.get("symbol") or "Unknown Token"
    )
    symbol = _clean_text(base_token.get("symbol"))
    token_display = name
    if symbol and symbol.lower() != name.lower():
        token_display = f"{name} ({symbol})"

    chain_id = str(pair.get("chainId") or "")
    dex_id = str(pair.get("dexId") or "")
    pair_address = str(pair.get("pairAddress") or "")

    liquidity_data = pair.get("liquidity")
    liquidity = (
        _coerce_float(liquidity_data.get("usd"))
        if isinstance(liquidity_data, dict)
        else None
    )
    mcap = _coerce_float(pair.get("marketCap"))
    if mcap is None:
        mcap = _coerce_float(pair.get("fdv"))

    volume_data = pair.get("volume")
    volume = (
        _coerce_float(volume_data.get("h24"))
        if isinstance(volume_data, dict)
        else None
    )
    txns_data = pair.get("txns")
    day_txns = txns_data.get("h24") if isinstance(txns_data, dict) else None
    buys = (
        _coerce_float(day_txns.get("buys"))
        if isinstance(day_txns, dict)
        else None
    )
    sells = (
        _coerce_float(day_txns.get("sells"))
        if isinstance(day_txns, dict)
        else None
    )
    buys_text = f"{int(buys):,}" if buys is not None else "N/A"
    sells_text = f"{int(sells):,}" if sells is not None else "N/A"

    url = str(pair.get("url") or "").strip()
    if not url:
        url = f"https://dexscreener.com/{chain_id}/{pair_address}"

    chain_text = _chain_display(chain_id)
    if dex_id:
        chain_text = f"{chain_text} · {dex_id.title()}"

    lines = [
        "🔍 *Token Lookup*",
        "",
        f"*Token:* {_escape_markdown(token_display)}",
        f"*Chain:* {_escape_markdown(chain_text)}",
        f"*Price:* {_escape_markdown(_format_price(pair.get('priceUsd')))}",
        (
            "*Liquidity:* "
            f"{_escape_markdown(_format_money(liquidity, compact=True))}"
        ),
        f"*MCAP:* {_escape_markdown(_format_money(mcap, compact=True))}",
        (
            "*Volume (24H):* "
            f"{_escape_markdown(_format_money(volume, compact=True))}"
        ),
        f"*Buys/Sells (24H):* {_escape_markdown(buys_text)} / "
        f"{_escape_markdown(sells_text)}",
        f"*Age:* {_escape_markdown(_format_age(pair))}",
    ]

    pair_count = int(_coerce_float(pair.get("lookup_pair_count")) or 0)
    if pair_count > 1:
        lines.append(
            f"*Pools found:* {pair_count} (showing the most liquid)"
        )

    lines.extend(["", f"[Open on DexScreener]({url})"])

    if warning:
        lines.extend(["", _escape_markdown(warning)])
    elif can_buy:
        lines.extend(["", "_Paper buy buttons expire in 1 hour._"])

    return "\n".join(lines)


def _lookup_keyboard(alert_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🧪 ${amount:g}",
                    callback_data=f"pb:{alert_id}:{amount:g}",
                )
                for amount in PAPER_BUY_PRESETS
            ],
            [
                InlineKeyboardButton(
                    "Custom amount", callback_data=f"pb:{alert_id}:custom"
                )
            ],
        ]
    )


async def _handle_token_lookup(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token_address: str,
) -> None:
    """Look up a pasted contract address and offer paper buys."""

    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    telegram_id = int(user.id)
    try:
        pair, status = await lookup_token(token_address)
    except Exception:  # noqa: BLE001 - a lookup must never crash the handler
        logger.exception("Token lookup crashed for %s", token_address)
        await message.reply_text(
            "⚠️ I could not look that token up. Please try again shortly."
        )
        return

    if pair is None:
        await message.reply_text(
            "🔍 No DexScreener pair found for that address.\n\n"
            "Make sure it is a token contract address — not a wallet, a pair "
            "address, or a transaction signature."
        )
        return

    warning = None
    buyable = status in {"confirmed", "single_source"}
    if status == "source_disagreement":
        warning = (
            "⚠️ Price sources disagree for this pool, so paper buying is "
            "disabled until they agree."
        )
    elif status == "no_price":
        warning = (
            "⚠️ No verified live price for this pool, so paper buying is "
            "disabled."
        )

    alert_id: int | None = None
    if buyable:
        try:
            alert = await db.create_paper_alert(
                telegram_id, None, pair, warning
            )
            if alert is not None:
                alert_id = int(alert["id"])
        except Exception:  # noqa: BLE001 - details are still worth showing
            logger.exception(
                "Could not create lookup alert for user %s", telegram_id
            )

    text = _lookup_text(pair, status, warning, alert_id is not None)
    markup = _lookup_keyboard(alert_id) if alert_id is not None else None

    try:
        await message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except TelegramError:
        logger.exception("Could not send token lookup for %s", token_address)
        await message.reply_text(text, disable_web_page_preview=True)


async def send_alert(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    match: dict[str, Any],
    alert_id: int | None = None,
    filter_name: str | None = None,
    filter_data: dict[str, Any] | None = None,
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

    price = _coerce_float(pair.get("priceUsd"))
    chain = str(pair.get("chainId") or "unknown")
    pair_address = str(pair.get("pairAddress") or "")

    active_filter = filter_data or {}
    timeframe_label = str(active_filter.get("timeframe") or "24H").upper()
    timeframe_key = {
        "5M": "m5",
        "1H": "h1",
        "6H": "h6",
        "24H": "h24",
    }.get(timeframe_label, "h24")

    volume_data = pair.get("volume")
    volume = (
        _coerce_float(volume_data.get(timeframe_key))
        if isinstance(volume_data, dict)
        else None
    )
    txns_data = pair.get("txns")
    timeframe_txns = (
        txns_data.get(timeframe_key)
        if isinstance(txns_data, dict)
        else None
    )
    buys = (
        _coerce_float(timeframe_txns.get("buys"))
        if isinstance(timeframe_txns, dict)
        else None
    )
    sells = (
        _coerce_float(timeframe_txns.get("sells"))
        if isinstance(timeframe_txns, dict)
        else None
    )

    url = str(pair.get("url") or "").strip()
    if not url:
        url = f"https://dexscreener.com/{chain}/{pair_address}"

    buys_text = f"{int(buys):,}" if buys is not None else "N/A"
    sells_text = f"{int(sells):,}" if sells is not None else "N/A"
    active_requirements = [
        label
        for field, label in REQUIREMENT_LABELS.items()
        if active_filter.get(field)
    ]
    requirements_text = (
        ", ".join(active_requirements) if active_requirements else "None"
    )

    text = (
        "🚀 *New Token Match*\n\n"
        f"*Filter:* {_escape_markdown(filter_name or 'Default')}\n"
        f"*Requirements:* {_escape_markdown(requirements_text)}\n"
        f"*Token:* {_escape_markdown(token_display)}\n"
        f"*Chain:* {_escape_markdown(chain)}\n"
        f"*Price:* {_escape_markdown(_format_price(price))}\n"
        f"*Liquidity:* {_escape_markdown(_format_money(liquidity, compact=True))}\n"
        f"*MCAP:* {_escape_markdown(_format_money(mcap, compact=True))}\n"
        f"*Volume ({_escape_markdown(timeframe_label)}):* "
        f"{_escape_markdown(_format_money(volume, compact=True))}\n"
        f"*Buys/Sells ({_escape_markdown(timeframe_label)}):* "
        f"{_escape_markdown(buys_text)} / {_escape_markdown(sells_text)}\n"
        f"*Age:* {_escape_markdown(_format_age(pair))}\n\n"
        f"[Open on DexScreener]({url})"
    )

    warning = match.get("warning")
    if warning:
        text += f"\n\n{_escape_markdown(warning)}"

    reply_markup = None
    if alert_id is not None:
        text += "\n\n_Paper trade buttons expire in 1 hour._"
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🧪 ${amount:g}",
                        callback_data=f"pb:{alert_id}:{amount:g}",
                    )
                    for amount in PAPER_BUY_PRESETS
                ],
                [
                    InlineKeyboardButton(
                        "Custom amount",
                        callback_data=f"pb:{alert_id}:custom",
                    )
                ],
            ]
        )

    await context.bot.send_message(
        chat_id=int(chat_id),
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )


async def _process_auto_sells(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict[str, int]:
    stats = {
        "auto_sells": 0,
        "auto_sell_errors": 0,
        "price_unavailable": 0,
    }

    positions = await db.get_all_open_paper_positions()
    if not positions:
        return stats

    rules = await db.get_all_auto_sell_rules()
    if not rules:
        return stats

    events = await db.get_all_auto_sell_events()
    triggered = {
        (int(event["rule_id"]), str(event["position_id"]))
        for event in events
    }

    rules_by_user: dict[int, list[dict[str, Any]]] = {}
    for rule in rules:
        rules_by_user.setdefault(int(rule["telegram_id"]), []).append(rule)
    for user_rules in rules_by_user.values():
        user_rules.sort(
            key=lambda rule: _coerce_float(rule.get("target_multiple")) or 0.0
        )

    requests = [
        (
            str(position.get("token_address") or ""),
            str(position.get("chain_id") or ""),
            str(position.get("pair_address") or ""),
        )
        for position in positions
    ]
    references = {
        _price_key(position.get("chain_id"), position.get("pair_address")): (
            _coerce_float(position.get("last_price_usd"))
        )
        for position in positions
    }
    prices = await fetch_current_prices(requests, references)

    for position in positions:
        telegram_id = int(position["telegram_id"])
        position_id = str(position["id"])
        user_rules = rules_by_user.get(telegram_id)
        if not user_rules:
            continue

        current_price = prices.get(
            _price_key(position.get("chain_id"), position.get("pair_address"))
        )
        if current_price is None or current_price <= 0:
            stats["price_unavailable"] += 1
            continue

        avg_entry = _coerce_float(position.get("avg_entry_price")) or 0.0
        if avg_entry <= 0:
            continue

        for rule in user_rules:
            rule_id = int(rule["id"])
            trigger_key = (rule_id, position_id)
            if trigger_key in triggered:
                continue

            multiple = _coerce_float(rule.get("target_multiple")) or 0.0
            percent = _coerce_float(rule.get("sell_percent")) or 0.0
            if multiple <= 1 or percent <= 0:
                continue
            if current_price < avg_entry * multiple:
                continue

            try:
                fill = await db.execute_paper_auto_sell(
                    telegram_id,
                    rule_id,
                    position_id,
                    current_price,
                    PAPER_FEE_RATE,
                    PAPER_SLIPPAGE_BPS,
                )
            except db.PaperTradingError as exc:
                if exc.code == "rule_already_triggered":
                    triggered.add(trigger_key)
                    continue
                if exc.code == "position_not_found":
                    break
                logger.warning(
                    "Auto-sell rule %s failed for position %s: %s",
                    rule_id,
                    position_id,
                    exc.message,
                )
                stats["auto_sell_errors"] += 1
                continue
            except Exception:  # noqa: BLE001 - isolate one bad rule
                logger.exception(
                    "Unexpected auto-sell failure for rule %s position %s",
                    rule_id,
                    position_id,
                )
                stats["auto_sell_errors"] += 1
                continue

            triggered.add(trigger_key)
            stats["auto_sells"] += 1

            symbol = _clean_text(
                fill.get("symbol") or position.get("symbol") or "Token", 30
            )
            status = str(fill.get("position_status") or "open")
            text = (
                f"🎯 Auto-sell triggered: {symbol}\n\n"
                f"Rule: {multiple:g}x · sell {percent:g}%\n"
                f"Trigger price: {_format_price(current_price)}\n"
                f"Sold: {_format_quantity(fill.get('quantity'))}\n"
                f"Proceeds: {_format_money(fill.get('cash_amount_usd'))}\n"
                f"Fee: {_format_money(fill.get('fee_usd'))}\n"
                f"Realized P&L: {_format_money(fill.get('realized_pnl_usd'))}\n"
                f"Position: {'Closed' if status == 'closed' else 'Still open'}"
            )
            try:
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "💼 View portfolio",
                                    callback_data="paper:portfolio",
                                )
                            ]
                        ]
                    ),
                )
            except TelegramError:
                logger.exception(
                    "Could not send auto-sell notification to %s", telegram_id
                )

            if status == "closed":
                break

    return stats


async def trigger_scan(context: ContextTypes.DEFAULT_TYPE) -> dict[str, int]:
    """Scan every active user's filter and send all matching alerts."""

    stats = {
        "active_users": 0,
        "matches": 0,
        "alerts_sent": 0,
        "auto_buys": 0,
        "auto_sells": 0,
        "auto_sell_errors": 0,
        "price_unavailable": 0,
        "errors": 0,
    }

    active_users = await db.get_all_active_users()
    stats["active_users"] = len(active_users)
    logger.info("Starting scan for %s active users", len(active_users))

    for entry in active_users:
        telegram_id = entry.get("telegram_id")
        filter_data = entry.get("filter")
        filter_id = entry.get("active_filter_id") or (
            filter_data.get("id") if isinstance(filter_data, dict) else None
        )

        if telegram_id is None or not isinstance(filter_data, dict):
            stats["errors"] += 1
            continue

        paper_account: dict[str, Any] | None = None
        try:
            paper_account = await db.get_paper_account(int(telegram_id))
        except Exception:  # noqa: BLE001 - auto-buy is optional
            logger.exception("Could not load paper account for %s", telegram_id)

        auto_buy_enabled = bool(
            paper_account and paper_account.get("auto_buy_enabled")
        )
        auto_buy_amount = (
            _coerce_float(paper_account.get("auto_buy_amount"))
            if paper_account
            else None
        )

        try:
            matches = await scan_dexscreener(filter_data, int(telegram_id))
        except Exception:  # noqa: BLE001 - isolate failures per user
            logger.exception("Scan failed for user %s", telegram_id)
            stats["errors"] += 1
            continue

        stats["matches"] += len(matches)

        for match in matches:
            pair = match.get("pair")
            alert_id: int | None = None

            if isinstance(pair, dict):
                try:
                    alert = await db.create_paper_alert(
                        int(telegram_id),
                        str(filter_id) if filter_id else None,
                        pair,
                        match.get("warning"),
                    )
                    if alert is not None:
                        alert_id = int(alert["id"])
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Could not persist paper alert for user %s",
                        telegram_id,
                    )

            sent = False
            try:
                await send_alert(
                    context,
                    int(telegram_id),
                    match,
                    alert_id=alert_id,
                    filter_name=str(filter_data.get("name") or "Default"),
                    filter_data=filter_data,
                )
                stats["alerts_sent"] += 1
                sent = True
            except TelegramError:
                logger.exception("Could not send alert to user %s", telegram_id)
                stats["errors"] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Unexpected alert error for user %s", telegram_id
                )
                stats["errors"] += 1

            if (
                sent
                and alert_id is not None
                and auto_buy_enabled
                and auto_buy_amount is not None
                and auto_buy_amount > 0
            ):
                try:
                    bought = await _execute_paper_buy(
                        context,
                        int(telegram_id),
                        alert_id,
                        auto_buy_amount,
                        auto=True,
                    )
                    if bought:
                        stats["auto_buys"] += 1
                except Exception:  # noqa: BLE001 - keep scanning
                    logger.exception(
                        "Auto-buy failed for user %s", telegram_id
                    )

    try:
        auto_stats = await _process_auto_sells(context)
        stats["auto_sells"] = auto_stats["auto_sells"]
        stats["auto_sell_errors"] = auto_stats["auto_sell_errors"]
        stats["price_unavailable"] = auto_stats["price_unavailable"]
    except Exception:  # noqa: BLE001 - do not fail the whole scan
        logger.exception("Auto-sell processing failed")
        stats["auto_sell_errors"] += 1

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
    "PAPER_STARTING_BALANCE",
    "WAITING_FOR_PAPER_VALUE",
    "WAITING_FOR_VALUE",
    "get_telegram_app",
    "initialize",
    "send_alert",
    "shutdown",
    "trigger_scan",
    "validate_environment",
]
