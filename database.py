"""Supabase persistence layer for the crypto trading bot.

The Supabase Python SDK is synchronous.  Every network call in this module is
therefore executed in a worker thread with ``asyncio.to_thread`` so that the
FastAPI event loop is never blocked by database I/O.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from supabase import Client, ClientOptions, create_client

logger = logging.getLogger(__name__)

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.environ.get("SUPABASE_KEY") or "").strip()

_PAGE_SIZE = 1000
_FILTER_ID_BATCH_SIZE = 100
_DUPLICATE_WINDOW = timedelta(hours=72)

_client: Client | None = None

DEFAULT_FILTER: dict[str, Any] = {
    "name": "Default",
    "chains": ["solana"],
    "req_profile": False,
    "req_boosted": False,
    "req_ads": False,
    "req_launchpad": False,
    "min_liquidity": 5_000.0,
    "max_mcap": 1_000_000.0,
    "max_age_days": 7.0,
    "timeframe": "24H",
    "min_buys": None,
    "max_buys": None,
    "min_sells": None,
    "max_sells": None,
    "min_volume": None,
    "max_volume": None,
    "min_change": None,
    "max_change": None,
    "labels": [],
    "address_suffix": None,
}


def get_supabase() -> Client:
    """Return the lazily-created Supabase client."""

    global _client

    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set in the environment."
            )
        _client = create_client(
            SUPABASE_URL,
            SUPABASE_KEY,
            options=ClientOptions(postgrest_client_timeout=15.0),
        )
        logger.info("Supabase client initialized")
    return _client


async def _execute(query: Any) -> Any:
    """Execute a synchronous PostgREST query without blocking the event loop."""

    return await asyncio.to_thread(query.execute)


def _response_rows(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    if not data:
        return []
    return [row for row in data if isinstance(row, dict)]


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


async def ensure_user(telegram_id: int) -> None:
    """Create the user row if it does not exist.

    The upsert only supplies ``telegram_id``.  On conflict PostgREST updates
    that same column, so an existing ``active_filter_id`` is not overwritten.
    """

    telegram_id = int(telegram_id)
    await _execute(
        get_supabase()
        .table("users")
        .upsert({"telegram_id": telegram_id}, on_conflict="telegram_id")
    )
    logger.debug("Ensured user %s exists", telegram_id)


async def get_user_active_filter(telegram_id: int) -> dict[str, Any] | None:
    """Return the user's active filter row, or ``None`` when unset/missing."""

    telegram_id = int(telegram_id)

    user_response = await _execute(
        get_supabase()
        .table("users")
        .select("active_filter_id")
        .eq("telegram_id", telegram_id)
        .limit(1)
    )
    user_rows = _response_rows(user_response)
    if not user_rows:
        return None

    active_filter_id = user_rows[0].get("active_filter_id")
    if not active_filter_id:
        return None

    filter_response = await _execute(
        get_supabase()
        .table("filters")
        .select("*")
        .eq("id", str(active_filter_id))
        .eq("telegram_id", telegram_id)
        .limit(1)
    )
    filter_rows = _response_rows(filter_response)
    return filter_rows[0] if filter_rows else None


async def get_filter(
    filter_id: str, telegram_id: int | None = None
) -> dict[str, Any] | None:
    """Fetch a filter by id, optionally enforcing ownership."""

    query = (
        get_supabase()
        .table("filters")
        .select("*")
        .eq("id", str(filter_id))
        .limit(1)
    )
    if telegram_id is not None:
        query = query.eq("telegram_id", int(telegram_id))

    rows = _response_rows(await _execute(query))
    return rows[0] if rows else None


async def update_filter(
    filter_id: str,
    telegram_id: int,
    updates: dict[str, Any],
) -> dict[str, Any] | None:
    """Update owned filter fields and return the refreshed row."""

    allowed_fields = {
        "name",
        "chains",
        "req_profile",
        "req_boosted",
        "req_ads",
        "req_launchpad",
        "min_liquidity",
        "max_mcap",
        "max_age_days",
        "timeframe",
        "min_buys",
        "max_buys",
        "min_sells",
        "max_sells",
        "min_volume",
        "max_volume",
        "min_change",
        "max_change",
        "labels",
        "address_suffix",
    }
    invalid_fields = set(updates) - allowed_fields
    if invalid_fields:
        raise ValueError(f"Unsupported filter fields: {sorted(invalid_fields)}")
    if not updates:
        return await get_filter(filter_id, telegram_id)

    payload = dict(updates)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    response = await _execute(
        get_supabase()
        .table("filters")
        .update(payload)
        .eq("id", str(filter_id))
        .eq("telegram_id", int(telegram_id))
    )
    rows = _response_rows(response)
    if rows:
        return rows[0]

    # Some PostgREST configurations do not return the updated representation.
    # Verify that the row exists and is owned by the caller before returning it.
    return await get_filter(filter_id, telegram_id)


async def set_active_filter(telegram_id: int, filter_id: str) -> None:
    """Point a user at one of their filters."""

    await _execute(
        get_supabase()
        .table("users")
        .update({"active_filter_id": str(filter_id)})
        .eq("telegram_id", int(telegram_id))
    )


async def create_default_filter(telegram_id: int) -> dict[str, Any]:
    """Create and activate a sensible default filter for a new user."""

    telegram_id = int(telegram_id)
    await ensure_user(telegram_id)

    payload = {**DEFAULT_FILTER, "telegram_id": telegram_id}
    response = await _execute(get_supabase().table("filters").insert(payload))
    rows = _response_rows(response)
    if not rows:
        raise RuntimeError("Supabase did not return the newly created filter")

    filter_row = rows[0]
    await set_active_filter(telegram_id, str(filter_row["id"]))
    logger.info("Created default filter %s for user %s", filter_row["id"], telegram_id)
    return filter_row


async def get_or_create_active_filter(telegram_id: int) -> dict[str, Any]:
    """Return the active filter, creating one on first use."""

    telegram_id = int(telegram_id)

    current = await get_user_active_filter(telegram_id)
    if current is not None:
        return current

    await ensure_user(telegram_id)
    return await create_default_filter(telegram_id)


async def get_all_active_users() -> list[dict[str, Any]]:
    """Return active users joined with their active filter data.

    Result shape::

        [{"telegram_id": 123, "active_filter_id": "...", "filter": {...}}, ...]

    Users without an active filter are ignored.
    """

    users: list[dict[str, Any]] = []
    offset = 0

    while True:
        response = await _execute(
            get_supabase()
            .table("users")
            .select("telegram_id, active_filter_id")
            .order("telegram_id")
            .range(offset, offset + _PAGE_SIZE - 1)
        )
        batch = _response_rows(response)
        users.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    active_users = [
        user for user in users if user.get("telegram_id") and user.get("active_filter_id")
    ]
    if not active_users:
        return []

    filter_ids = list(
        dict.fromkeys(str(user["active_filter_id"]) for user in active_users)
    )

    filters_by_id: dict[str, dict[str, Any]] = {}
    for batch_ids in _chunks(filter_ids, _FILTER_ID_BATCH_SIZE):
        response = await _execute(
            get_supabase()
            .table("filters")
            .select("*")
            .in_("id", batch_ids)
        )
        for row in _response_rows(response):
            filters_by_id[str(row["id"])] = row

    joined: list[dict[str, Any]] = []
    for user in active_users:
        telegram_id = int(user["telegram_id"])
        active_filter_id = str(user["active_filter_id"])
        filter_row = filters_by_id.get(active_filter_id)

        # Never allow a stale FK to send another user's filter data.
        if filter_row is None or str(filter_row.get("telegram_id")) != str(
            telegram_id
        ):
            logger.warning(
                "Skipping user %s: active filter %s is missing or not owned",
                telegram_id,
                active_filter_id,
            )
            continue

        joined.append(
            {
                "telegram_id": telegram_id,
                "active_filter_id": active_filter_id,
                "filter": filter_row,
            }
        )

    return joined


class PaperTradingError(Exception):
    """A user-facing paper-trading failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_RPC_ERROR_MESSAGES = {
    "invalid_amount": "Enter an amount between $1 and $1,000,000.",
    "invalid_price": "A valid current token price is required.",
    "invalid_fee_rate": "The paper fee configuration is invalid.",
    "invalid_slippage": "The paper slippage configuration is invalid.",
    "invalid_fraction": "Choose a sell amount between 1% and 100%.",
    "alert_not_found": "That alert is no longer available.",
    "alert_expired": "That alert has expired. Buy buttons are valid for 1 hour.",
    "account_not_found": "Paper account not found. Open /portfolio first.",
    "insufficient_funds": "Not enough paper cash for that buy.",
    "position_not_found": "That paper position is no longer open.",
    "trading_started": (
        "Trading has already started. Use /resetpaper to start over with "
        "a new balance."
    ),
}


def _translate_rpc_error(exc: Exception) -> None:
    text = str(exc).lower()
    for code, message in _RPC_ERROR_MESSAGES.items():
        if code in text:
            raise PaperTradingError(code, message) from exc
    raise exc


async def _rpc(function_name: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = await _execute(get_supabase().rpc(function_name, params))
    except Exception as exc:  # noqa: BLE001 - translated into domain errors
        _translate_rpc_error(exc)
        raise

    data = getattr(response, "data", None)
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, dict):
        return data
    if data is None:
        return {}
    return {"value": data}


async def is_token_recently_sent(
    telegram_id: int, token_address: str
) -> bool:
    """Return ``True`` if this user was sent the token in the last 72 hours."""

    token_address = (token_address or "").strip()
    if not token_address:
        return False

    cutoff = (datetime.now(timezone.utc) - _DUPLICATE_WINDOW).isoformat()
    response = await _execute(
        get_supabase()
        .table("sent_tokens")
        .select("token_address")
        .eq("telegram_id", int(telegram_id))
        .eq("token_address", token_address)
        .gte("sent_at", cutoff)
        .limit(1)
    )
    return bool(_response_rows(response))


async def mark_token_as_sent(
    telegram_id: int,
    token_address: str,
    chain_id: str,
) -> dict[str, Any] | None:
    """Insert or refresh this user's send timestamp for a token."""

    token_address = (token_address or "").strip()
    chain_id = (chain_id or "").strip()
    if not token_address:
        raise ValueError("token_address cannot be empty")

    payload = {
        "telegram_id": int(telegram_id),
        "token_address": token_address,
        "chain_id": chain_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    response = await _execute(
        get_supabase()
        .table("sent_tokens")
        .upsert(
            payload,
            on_conflict="telegram_id,token_address",
        )
    )
    rows = _response_rows(response)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Paper-trading persistence
# ---------------------------------------------------------------------------


async def get_or_create_paper_account(
    telegram_id: int,
    starting_balance: float = 1000.0,
) -> dict[str, Any]:
    """Create the user's fake account on first use and return it."""

    return await _rpc(
        "paper_get_or_create_account",
        {
            "p_telegram_id": int(telegram_id),
            "p_starting_balance": float(starting_balance),
        },
    )


async def get_paper_account(telegram_id: int) -> dict[str, Any] | None:
    response = await _execute(
        get_supabase()
        .table("paper_accounts")
        .select("*")
        .eq("telegram_id", int(telegram_id))
        .limit(1)
    )
    rows = _response_rows(response)
    return rows[0] if rows else None


async def set_paper_balance(
    telegram_id: int,
    balance: float,
) -> dict[str, Any]:
    return await _rpc(
        "paper_set_balance",
        {
            "p_telegram_id": int(telegram_id),
            "p_balance": float(balance),
        },
    )


async def add_paper_funds(
    telegram_id: int,
    amount: float,
) -> dict[str, Any]:
    return await _rpc(
        "paper_add_funds",
        {
            "p_telegram_id": int(telegram_id),
            "p_amount": float(amount),
        },
    )


async def reset_paper_account(
    telegram_id: int,
    balance: float = 1000.0,
) -> dict[str, Any]:
    return await _rpc(
        "paper_reset",
        {
            "p_telegram_id": int(telegram_id),
            "p_balance": float(balance),
        },
    )


async def create_paper_alert(
    telegram_id: int,
    filter_id: str | None,
    pair: dict[str, Any],
    warning: str | None,
) -> dict[str, Any] | None:
    """Persist an alert so its buy buttons can reference a short id."""

    base_token = pair.get("baseToken")
    if not isinstance(base_token, dict):
        base_token = {}

    try:
        price_usd = float(pair.get("priceUsd"))
    except (TypeError, ValueError):
        price_usd = None
    if price_usd is not None and not math.isfinite(price_usd):
        price_usd = None

    payload = {
        "telegram_id": int(telegram_id),
        "filter_id": str(filter_id) if filter_id else None,
        "token_address": str(
            base_token.get("address") or pair.get("pairAddress") or ""
        ).strip(),
        "chain_id": str(pair.get("chainId") or "").strip(),
        "pair_address": str(pair.get("pairAddress") or "").strip(),
        "symbol": (base_token.get("symbol") or None),
        "name": (base_token.get("name") or None),
        "price_usd": price_usd,
        "warning": warning,
    }
    if not payload["token_address"] or not payload["pair_address"]:
        logger.warning("Not creating paper alert without token/pair address")
        return None

    response = await _execute(
        get_supabase().table("paper_alerts").insert(payload)
    )
    rows = _response_rows(response)
    return rows[0] if rows else None


async def get_paper_alert(
    alert_id: int,
    telegram_id: int,
) -> dict[str, Any] | None:
    response = await _execute(
        get_supabase()
        .table("paper_alerts")
        .select("*")
        .eq("id", int(alert_id))
        .eq("telegram_id", int(telegram_id))
        .limit(1)
    )
    rows = _response_rows(response)
    return rows[0] if rows else None


async def get_paper_positions(
    telegram_id: int,
    status: str | None = "open",
) -> list[dict[str, Any]]:
    query = (
        get_supabase()
        .table("paper_positions")
        .select("*")
        .eq("telegram_id", int(telegram_id))
        .order("opened_at", desc=True)
    )
    if status:
        query = query.eq("status", status)

    return _response_rows(await _execute(query))


async def get_paper_position(
    position_id: str,
    telegram_id: int,
) -> dict[str, Any] | None:
    response = await _execute(
        get_supabase()
        .table("paper_positions")
        .select("*")
        .eq("id", str(position_id))
        .eq("telegram_id", int(telegram_id))
        .limit(1)
    )
    rows = _response_rows(response)
    return rows[0] if rows else None


async def get_paper_fills(
    telegram_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    response = await _execute(
        get_supabase()
        .table("paper_fills")
        .select("*")
        .eq("telegram_id", int(telegram_id))
        .order("created_at", desc=True)
        .limit(max(1, min(int(limit), 100)))
    )
    return _response_rows(response)


async def execute_paper_buy(
    telegram_id: int,
    alert_id: int,
    amount_usd: float,
    market_price: float,
    fee_rate: float,
    slippage_bps: int,
) -> dict[str, Any]:
    return await _rpc(
        "paper_buy",
        {
            "p_telegram_id": int(telegram_id),
            "p_alert_id": int(alert_id),
            "p_amount_usd": float(amount_usd),
            "p_market_price": float(market_price),
            "p_fee_rate": float(fee_rate),
            "p_slippage_bps": int(slippage_bps),
        },
    )


async def execute_paper_sell(
    telegram_id: int,
    position_id: str,
    fraction: float,
    market_price: float,
    fee_rate: float,
    slippage_bps: int,
) -> dict[str, Any]:
    return await _rpc(
        "paper_sell",
        {
            "p_telegram_id": int(telegram_id),
            "p_position_id": str(position_id),
            "p_fraction": float(fraction),
            "p_market_price": float(market_price),
            "p_fee_rate": float(fee_rate),
            "p_slippage_bps": int(slippage_bps),
        },
    )
