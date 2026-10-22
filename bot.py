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
from scanner import fetch_current_prices, fetch_pair_price, scan_dexscreener

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

TIMEFRAMES = ("5M", "1H", "6H", "24H")

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
    prices = await fetch_current_prices(requests) if requests else {}

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
    ]

    if snapshot["stale_prices"]:
        lines.append("⚠️ Some prices could not be refreshed; last known price used.")

    if not snapshot["open_positions"]:
        lines.extend(["", "No open paper positions."])
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

    lines.extend(["", "Tap a position below to sell part or all of it."])
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


def _position_text(position: dict[str, Any], current_price: float | None) -> str:
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

    symbol = _clean_text(
        position.get("symbol") or position.get("name") or "Token", 30
    )
    lines = [
        f"📊 {symbol}",
        "",
        f"Chain: {_chain_display(position.get('chain_id'))}",
        f"Quantity: {_format_quantity(quantity)}",
        f"Average entry: {_format_price(entry)}",
        f"Current price: {_format_price(current)}",
        f"Cost basis: {_format_money(cost_basis)}",
        f"Market value: {_format_money(market_value)}",
        (
            f"Unrealized P&L: {_format_money(unrealized)} "
            f"({_format_pct(unrealized_pct)})"
        ),
        f"Realized P&L: {_format_money(position.get('realized_pnl_usd'))}",
        f"Opened: {_format_when(position.get('opened_at'))}",
        "",
        "Sell 25%, 50%, 100%, or a custom percentage.",
    ]
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


def _paper_error_text(exc: Exception) -> str:
    if isinstance(exc, db.PaperTradingError):
        return exc.message
    logger.exception("Unexpected paper-trading error")
    return "⚠️ The paper trade could not be completed. Please try again."


async def _execute_paper_buy(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_id: int,
    alert_id: int,
    amount: float,
) -> None:
    alert = await db.get_paper_alert(alert_id, telegram_id)
    if alert is None:
        await context.bot.send_message(
            chat_id=telegram_id,
            text="⚠️ That alert is no longer available.",
        )
        return

    price = await fetch_pair_price(
        str(alert.get("token_address") or ""),
        str(alert.get("chain_id") or ""),
        str(alert.get("pair_address") or ""),
    )
    stale = price is None
    if price is None:
        price = _coerce_float(alert.get("price_usd"))
    if price is None or price <= 0:
        await context.bot.send_message(
            chat_id=telegram_id,
            text="⚠️ I could not get a valid price for this token. Try again shortly.",
        )
        return

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
        return

    symbol = _clean_text(fill.get("symbol") or alert.get("symbol") or "Token", 30)
    text = (
        f"✅ Paper buy filled: {symbol}\n\n"
        f"Spent: {_format_money(fill.get('cash_amount_usd'))}\n"
        f"Fee: {_format_money(fill.get('fee_usd'))}\n"
        f"Average fill price: {_format_price(fill.get('price_usd'))}\n"
        f"Quantity: {_format_quantity(fill.get('quantity'))}\n"
        f"Cash left: {_format_money(fill.get('cash_balance'))}"
    )
    if stale:
        text += "\n\n⚠️ Live price was unavailable; the alert price was used."

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

    price = await fetch_pair_price(
        str(position.get("token_address") or ""),
        str(position.get("chain_id") or ""),
        str(position.get("pair_address") or ""),
    )
    stale = price is None
    if price is None:
        price = _coerce_float(position.get("last_price_usd"))
    if price is None or price <= 0:
        await context.bot.send_message(
            chat_id=telegram_id,
            text="⚠️ I could not get a valid price for this token. Try again shortly.",
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
            "Your filters and sent-token history stay intact."
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
            await _safe_edit(
                query,
                _position_text(position, price),
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
    except Exception:
        logger.exception("Could not create user %s", user.id)
        await message.reply_text(
            "⚠️ I could not reach the database. Please try again shortly."
        )
        return

    await message.reply_text(
        "👋 Welcome to the Crypto Scanner Bot.\n\n"
        "Use /filter to configure your token filters.\n"
        f"Your paper balance starts at {_format_money(PAPER_STARTING_BALANCE)}.\n"
        "Use /setbalance before your first trade to choose another amount.\n"
        "Use /portfolio to see PnL and /addfunds to top up fake cash anytime."
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
    except Exception:
        logger.exception("Could not load portfolio for %s", user.id)
        await message.reply_text(
            "⚠️ I could not load your paper portfolio. Please try again."
        )


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
    except Exception:
        logger.exception("Could not load paper history for %s", user.id)
        await message.reply_text(
            "⚠️ I could not load your paper trade history."
        )
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
    alert_id: int | None = None,
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

    url = str(pair.get("url") or "").strip()
    if not url:
        url = f"https://dexscreener.com/{chain}/{pair_address}"

    text = (
        "🚀 *New Token Match*\n\n"
        f"*Token:* {_escape_markdown(token_display)}\n"
        f"*Chain:* {_escape_markdown(chain)}\n"
        f"*Price:* {_escape_markdown(_format_price(price))}\n"
        f"*Liquidity:* {_escape_markdown(_format_money(liquidity))}\n"
        f"*MCAP:* {_escape_markdown(_format_money(mcap))}\n"
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
                        f"🧪 Paper ${amount:g}",
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
        filter_id = entry.get("active_filter_id") or (
            filter_data.get("id") if isinstance(filter_data, dict) else None
        )

        if telegram_id is None or not isinstance(filter_data, dict):
            stats["errors"] += 1
            continue

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

            try:
                await send_alert(
                    context,
                    int(telegram_id),
                    match,
                    alert_id=alert_id,
                )
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
