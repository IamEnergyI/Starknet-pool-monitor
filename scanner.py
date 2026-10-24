"""DexScreener discovery and GeckoTerminal verification.

Discovery primarily uses DexScreener's own screener feed with the same filter
parameters as its website.  If that internal feed is unavailable, the bot
falls back to the latest token profiles and boosts.  GeckoTerminal is used for
the fallback path; screener matches already contain DexScreener's own
transaction counts and do not need a second verification source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any
from urllib.parse import quote

import httpx

from database import is_token_recently_sent, mark_token_as_sent
from dexscreener_screener import ScreenerUnavailable, fetch_screener_pairs

logger = logging.getLogger(__name__)

DEXSCREENER_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
DEXSCREENER_PAIR_URL = "https://api.dexscreener.com/latest/dex/pairs"
GECKO_POOL_URL = (
    "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}"
)

MAX_DISCOVERY_ADDRESSES = 30

REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
USER_AGENT = "CryptoScannerBot/1.0 (+https://render.com)"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


# Price-integrity policy.  A fill is only executed on a price that two
# independent providers agree on, and a single-provider price may not move more
# than the jump ratio away from the position's last known price.
PAIR_PRICE_MAX_DEVIATION = min(
    0.9, max(0.01, _env_float("PRICE_MAX_DEVIATION", 0.20))
)
PAIR_PRICE_JUMP_RATIO = max(1.5, _env_float("PRICE_MAX_JUMP_RATIO", 3.0))
PAIR_PRICE_CACHE_SECONDS = max(
    0.0, _env_float("PRICE_CACHE_SECONDS", 20.0)
)
MAX_CONCURRENT_PRICE_REQUESTS = 4

# (chain, pair) -> (monotonic timestamp, price, status)
_price_cache: dict[tuple[str, str], tuple[float, float | None, str]] = {}

GECKO_FAILURE_WARNING = (
    "⚠️ GeckoTerminal API failed. Buys/Sells unverified."
)

# GeckoTerminal uses its own network identifiers.  Unknown values are passed
# through unchanged so newer chains keep working if their ids happen to match.
GECKO_NETWORKS = {
    "ethereum": "eth",
    "eth": "eth",
    "bsc": "bsc",
    "solana": "solana",
    "polygon": "polygon_pos",
    "polygon_pos": "polygon_pos",
    "arbitrum": "arbitrum",
    "base": "base",
    "avalanche": "avax",
    "avax": "avax",
    "optimism": "optimism",
    "pulsechain": "pulsechain",
    "blast": "blast",
    "linea": "linea",
    "scroll": "scroll",
    "zksync": "zksync",
    "mantle": "mantle",
    "sui": "sui",
    "ton": "ton",
    "cronos": "cronos",
    "celo": "celo",
    "moonbeam": "moonbeam",
    "fantom": "ftm",
    "ftm": "ftm",
    "starknet": "starknet-alpha",
    "robinhood": "robinhood",
}

TIMEFRAME_TO_GECKO = {
    "5M": "m5",
    "15M": "m15",
    "30M": "m30",
    "1H": "h1",
    "4H": "h4",
    "6H": "h6",
    "12H": "h12",
    "24H": "h24",
}

TIMEFRAME_TO_DEXSCREENER = {
    "5M": "m5",
    "1H": "h1",
    "6H": "h6",
    "24H": "h24",
}


def _as_float(value: Any) -> float | None:
    """Convert an API/Supabase value to a finite float when possible."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _timeframe_key(filter_data: dict[str, Any]) -> str:
    raw = str(filter_data.get("timeframe") or "24H").strip().upper()
    return raw if raw in TIMEFRAME_TO_GECKO else "24H"


def _gecko_timeframe(filter_data: dict[str, Any]) -> str:
    return TIMEFRAME_TO_GECKO[_timeframe_key(filter_data)]


def _dex_timeframe(filter_data: dict[str, Any]) -> str:
    return TIMEFRAME_TO_DEXSCREENER.get(_timeframe_key(filter_data), "h24")


def _token_address(pair: dict[str, Any]) -> str | None:
    base_token = pair.get("baseToken")
    if isinstance(base_token, dict):
        address = base_token.get("address")
        if address:
            return str(address)
    pair_address = pair.get("pairAddress")
    return str(pair_address) if pair_address else None


def _pair_age_days(pair: dict[str, Any]) -> float | None:
    created_at = _as_float(pair.get("pairCreatedAt"))
    if created_at is None or created_at <= 0:
        return None

    # DexScreener returns milliseconds.  Tolerate a seconds-based test/mock.
    if created_at < 10_000_000_000:
        created_at *= 1000

    age_ms = (time.time() * 1000.0) - created_at
    return max(0.0, age_ms / 86_400_000.0)


def _passes_core_filters(
    pair: dict[str, Any], filter_data: dict[str, Any]
) -> bool:
    """Apply cheap local filters before consuming a GeckoTerminal request."""

    chains = {
        str(chain).strip().lower()
        for chain in _as_list(filter_data.get("chains"))
        if str(chain).strip()
    }
    chain_id = str(pair.get("chainId") or "").strip().lower()
    if not chains or chain_id not in chains:
        return False

    min_liquidity = _as_float(filter_data.get("min_liquidity"))
    if min_liquidity is not None:
        liquidity_data = pair.get("liquidity")
        liquidity = (
            _as_float(liquidity_data.get("usd"))
            if isinstance(liquidity_data, dict)
            else None
        )
        if liquidity is None or liquidity < min_liquidity:
            return False

    max_mcap = _as_float(filter_data.get("max_mcap"))
    if max_mcap is not None:
        market_cap = _as_float(pair.get("marketCap"))
        if market_cap is None:
            market_cap = _as_float(pair.get("fdv"))
        if market_cap is None or market_cap > max_mcap:
            return False

    max_age_days = _as_float(filter_data.get("max_age_days"))
    if max_age_days is not None:
        age_days = _pair_age_days(pair)
        if age_days is None or age_days > max_age_days:
            return False

    address_suffix = str(filter_data.get("address_suffix") or "").strip()
    if address_suffix:
        token_address = _token_address(pair)
        if not token_address or not token_address.lower().endswith(
            address_suffix.lower()
        ):
            return False

    # Volume and price-change filters are evaluated against the active
    # timeframe when DexScreener provides that granularity.
    dex_timeframe = _dex_timeframe(filter_data)
    volume_data = pair.get("volume")
    volume = (
        _as_float(volume_data.get(dex_timeframe))
        if isinstance(volume_data, dict)
        else None
    )

    min_volume = _as_float(filter_data.get("min_volume"))
    if min_volume is not None and (volume is None or volume < min_volume):
        return False

    max_volume = _as_float(filter_data.get("max_volume"))
    if max_volume is not None and (volume is None or volume > max_volume):
        return False

    price_change_data = pair.get("priceChange")
    price_change = (
        _as_float(price_change_data.get(dex_timeframe))
        if isinstance(price_change_data, dict)
        else None
    )

    min_change = _as_float(filter_data.get("min_change"))
    if min_change is not None and (
        price_change is None or price_change < min_change
    ):
        return False

    max_change = _as_float(filter_data.get("max_change"))
    if max_change is not None and (
        price_change is None or price_change > max_change
    ):
        return False

    # Buys/sells come from the same DexScreener pair snapshot.  The screener
    # applies these server-side, while the promoted-token fallback needs the
    # local check before it reaches GeckoTerminal.
    txns_data = pair.get("txns")
    timeframe_txns = (
        txns_data.get(dex_timeframe) if isinstance(txns_data, dict) else None
    )
    buys = (
        _as_float(timeframe_txns.get("buys"))
        if isinstance(timeframe_txns, dict)
        else None
    )
    sells = (
        _as_float(timeframe_txns.get("sells"))
        if isinstance(timeframe_txns, dict)
        else None
    )

    min_buys = _as_float(filter_data.get("min_buys"))
    if min_buys is not None and (buys is None or buys < min_buys):
        return False

    max_buys = _as_float(filter_data.get("max_buys"))
    if max_buys is not None and (buys is None or buys > max_buys):
        return False

    min_sells = _as_float(filter_data.get("min_sells"))
    if min_sells is not None and (sells is None or sells < min_sells):
        return False

    max_sells = _as_float(filter_data.get("max_sells"))
    if max_sells is not None and (sells is None or sells > max_sells):
        return False

    return True


async def _fetch_latest_token_addresses(
    client: httpx.AsyncClient,
) -> list[str]:
    """Fetch promoted token addresses from profiles and boosts.

    Both endpoints return a JSON list whose objects include ``chainId`` and
    ``tokenAddress``.  If either request fails, discovery is aborted for this
    scan and an empty list is returned.
    """

    addresses: list[str] = []
    fetch_failed = False

    sources = (
        (DEXSCREENER_PROFILES_URL, "token profiles"),
        (DEXSCREENER_BOOSTS_URL, "token boosts"),
    )

    for url, label in sources:
        try:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, list):
                raise ValueError(
                    f"Expected a list, got {type(payload).__name__}"
                )

            for item in payload:
                if not isinstance(item, dict):
                    continue
                token_address = str(item.get("tokenAddress") or "").strip()
                if token_address:
                    addresses.append(token_address)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning(
                "Failed to fetch DexScreener %s (%s): %s",
                label,
                type(exc).__name__,
                exc,
            )
            fetch_failed = True

    if fetch_failed:
        return []

    # dict.fromkeys removes duplicates while preserving discovery order.
    unique_addresses = list(dict.fromkeys(addresses))
    return unique_addresses[:MAX_DISCOVERY_ADDRESSES]


async def _fetch_pairs_by_addresses(
    client: httpx.AsyncClient,
    token_addresses: list[str],
) -> list[dict[str, Any]]:
    """Fetch pairs for up to 30 comma-separated token addresses."""

    if not token_addresses:
        return []

    comma_separated_addresses = ",".join(token_addresses)
    response = await client.get(
        f"{DEXSCREENER_TOKENS_URL}/{comma_separated_addresses}"
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        logger.warning("Unexpected DexScreener response type: %s", type(payload))
        return []

    pairs = payload.get("pairs")
    if not isinstance(pairs, list):
        return []
    return [pair for pair in pairs if isinstance(pair, dict)]


def _price_key(chain_id: Any, pair_address: Any) -> tuple[str, str]:
    return (
        str(chain_id or "").strip().lower(),
        str(pair_address or "").strip().lower(),
    )


def _cached_price(
    key: tuple[str, str],
) -> tuple[float | None, str] | None:
    entry = _price_cache.get(key)
    if entry is None:
        return None
    stored_at, price, status = entry
    if (
        PAIR_PRICE_CACHE_SECONDS > 0
        and time.monotonic() - stored_at < PAIR_PRICE_CACHE_SECONDS
    ):
        return price, status
    _price_cache.pop(key, None)
    return None


def _store_price(
    key: tuple[str, str], price: float | None, status: str
) -> None:
    if PAIR_PRICE_CACHE_SECONDS <= 0:
        return
    _price_cache[key] = (time.monotonic(), price, status)


async def _fetch_dex_pair_price(
    client: httpx.AsyncClient,
    chain_id: str,
    pair_address: str,
) -> float | None:
    """Price one exact pool through DexScreener's per-pair endpoint.

    The token endpoint (``/latest/dex/tokens``) is deliberately not used here:
    it is an aggregate whose price can lag or disagree with the pool it names.
    """

    if not chain_id or not pair_address:
        return None

    url = (
        f"{DEXSCREENER_PAIR_URL}/{quote(chain_id, safe='')}"
        f"/{quote(pair_address, safe='')}"
    )
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()

    pair: Any = None
    if isinstance(payload, dict):
        pair = payload.get("pair")
        if not isinstance(pair, dict):
            pairs = payload.get("pairs")
            if isinstance(pairs, list) and pairs:
                pair = pairs[0]
    if not isinstance(pair, dict):
        return None

    # Never accept a response that describes a different pool.
    returned_pair = str(pair.get("pairAddress") or "").strip().lower()
    if returned_pair and returned_pair != pair_address.strip().lower():
        logger.warning(
            "DexScreener pair lookup for %s returned %s; ignoring",
            pair_address,
            returned_pair,
        )
        return None

    return _as_float(pair.get("priceUsd"))


async def _fetch_gecko_price(
    client: httpx.AsyncClient,
    chain_id: str,
    pair_address: str,
) -> float | None:
    """Price the same pool independently through GeckoTerminal."""

    network = GECKO_NETWORKS.get(chain_id, chain_id)
    if not network or not pair_address:
        return None

    url = GECKO_POOL_URL.format(
        network=quote(network, safe=""),
        pool=quote(pair_address, safe=""),
    )
    response = await client.get(url, headers={"Accept": "application/json"})
    if response.status_code == 404:
        return None
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    attributes = data.get("attributes") if isinstance(data, dict) else None
    if not isinstance(attributes, dict):
        return None

    return _as_float(attributes.get("base_token_price_usd"))


async def _source_prices(
    client: httpx.AsyncClient,
    chain_id: str,
    pair_address: str,
) -> tuple[float | None, float | None]:
    """Return ``(dexscreener_price, geckoterminal_price)`` independently."""

    results = await asyncio.gather(
        _fetch_dex_pair_price(client, chain_id, pair_address),
        _fetch_gecko_price(client, chain_id, pair_address),
        return_exceptions=True,
    )

    values: list[float | None] = []
    for name, result in zip(("DexScreener", "GeckoTerminal"), results):
        if isinstance(result, BaseException):
            logger.warning(
                "%s price lookup failed for %s/%s: %s: %s",
                name,
                chain_id,
                pair_address,
                type(result).__name__,
                result,
            )
            values.append(None)
        else:
            values.append(result)
    return values[0], values[1]


def _combine_price_sources(
    dex_price: float | None,
    gecko_price: float | None,
) -> tuple[float | None, str]:
    """Combine both providers into one trusted price and a status."""

    if dex_price is not None and gecko_price is not None:
        high = max(dex_price, gecko_price)
        low = min(dex_price, gecko_price)
        if high <= 0:
            return None, "no_price"
        if (high - low) / high > PAIR_PRICE_MAX_DEVIATION:
            logger.warning(
                "Price sources disagree (%.10g vs %.10g); refusing the price",
                dex_price,
                gecko_price,
            )
            return None, "source_disagreement"
        return dex_price, "confirmed"

    if dex_price is not None:
        return dex_price, "single_source"
    if gecko_price is not None:
        return gecko_price, "single_source"
    return None, "no_price"


def _is_unconfirmed_move(
    price: float | None,
    status: str,
    reference: Any,
) -> bool:
    """Reject a single-source price that jumps far from the last known price."""

    if price is None or status == "confirmed":
        return False

    reference_price = _as_float(reference)
    if reference_price is None or reference_price <= 0:
        return False

    ratio = price / reference_price
    return ratio > PAIR_PRICE_JUMP_RATIO or ratio < 1 / PAIR_PRICE_JUMP_RATIO


async def resolve_prices(
    requests: list[tuple[str, str, str]],
) -> dict[tuple[str, str], tuple[float | None, str]]:
    """Resolve prices for ``(token, chain, pair)`` requests.

    Each distinct pool is queried once through DexScreener and once through
    GeckoTerminal.  Results are cached briefly so a portfolio refresh and the
    auto-sell pass in the same scan do not duplicate requests.
    """

    if not requests:
        return {}

    resolved: dict[tuple[str, str], tuple[float | None, str]] = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PRICE_REQUESTS)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:

        async def resolve_one(
            chain_id: str, pair_address: str
        ) -> tuple[tuple[str, str], tuple[float | None, str]]:
            key = _price_key(chain_id, pair_address)
            cached = _cached_price(key)
            if cached is not None:
                return key, cached

            async with semaphore:
                dex_price, gecko_price = await _source_prices(
                    client, chain_id, pair_address
                )
            price, status = _combine_price_sources(dex_price, gecko_price)
            _store_price(key, price, status)
            return key, (price, status)

        results = await asyncio.gather(
            *(
                resolve_one(chain_id, pair_address)
                for _token, chain_id, pair_address in requests
            )
        )

    for key, outcome in results:
        resolved[key] = outcome
    return resolved


async def fetch_current_prices(
    requests: list[tuple[str, str, str]],
    references: dict[tuple[str, str], float | None] | None = None,
) -> dict[tuple[str, str], float]:
    """Return trusted USD prices keyed by ``(chain_id, pair_address)``.

    Only the exact pool supplied is priced.  Valuing a position through another
    pool for the same token is unsafe: fragmented tokens can trade at wildly
    different prices in different pools.

    ``references`` maps a pool key to its last known price; a single-provider
    price that moves more than ``PAIR_PRICE_JUMP_RATIO`` away from it is
    discarded rather than used for a fill.
    """

    if not requests:
        return {}

    resolved = await resolve_prices(requests)
    references = references or {}

    prices: dict[tuple[str, str], float] = {}
    for _token, chain_id, pair_address in requests:
        key = _price_key(chain_id, pair_address)
        price, status = resolved.get(key, (None, "no_price"))
        if price is None:
            continue
        if _is_unconfirmed_move(price, status, references.get(key)):
            logger.warning(
                "Discarding unconfirmed price for %s/%s: %.10g vs reference %.10g",
                chain_id,
                pair_address,
                price,
                _as_float(references.get(key)) or 0.0,
            )
            continue
        prices[key] = price

    return prices


async def fetch_checked_pair_price(
    token_address: str,
    chain_id: str,
    pair_address: str,
    *,
    reference: float | None = None,
) -> tuple[float | None, str]:
    """Return ``(price, status)`` for one pool.

    ``status`` is one of ``confirmed``, ``single_source``,
    ``source_disagreement``, ``unconfirmed_move``, or ``no_price`` so callers
    can explain a refusal to the user.
    """

    resolved = await resolve_prices(
        [(token_address, chain_id, pair_address)]
    )
    key = _price_key(chain_id, pair_address)
    price, status = resolved.get(key, (None, "no_price"))

    if _is_unconfirmed_move(price, status, reference):
        logger.warning(
            "Discarding unconfirmed price for %s/%s: %.10g vs reference %.10g",
            chain_id,
            pair_address,
            price or 0.0,
            _as_float(reference) or 0.0,
        )
        return None, "unconfirmed_move"

    return price, status


async def fetch_pair_price(
    token_address: str,
    chain_id: str,
    pair_address: str,
) -> float | None:
    """Return the current USD price for one token pair."""

    price, _status = await fetch_checked_pair_price(
        token_address, chain_id, pair_address
    )
    return price


async def _verify_with_geckoterminal(
    client: httpx.AsyncClient,
    pair: dict[str, Any],
    filter_data: dict[str, Any],
) -> tuple[bool, str | None]:
    """Return ``(passed, warning)`` for one GeckoTerminal lookup.

    An API/parsing failure returns ``(True, GECKO_FAILURE_WARNING)`` so the
    alert still fires.  A successful response with buys/sells outside the
    configured range returns ``(False, None)``.
    """

    chain_id = str(pair.get("chainId") or "").strip().lower()
    pair_address = str(pair.get("pairAddress") or "").strip()
    network = GECKO_NETWORKS.get(chain_id, chain_id)
    timeframe = _gecko_timeframe(filter_data)

    if not network or not pair_address:
        logger.warning("Cannot verify pair without chain/pair address: %s", pair)
        return True, GECKO_FAILURE_WARNING

    url = GECKO_POOL_URL.format(
        network=quote(network, safe=""),
        pool=quote(pair_address, safe=""),
    )

    try:
        response = await client.get(
            url,
            headers={"Accept": "application/json"},
        )
        if response.status_code == 429:
            logger.warning("GeckoTerminal rate limited for %s", pair_address)
        response.raise_for_status()
        payload = response.json()

        data = payload.get("data") if isinstance(payload, dict) else None
        attributes = data.get("attributes") if isinstance(data, dict) else None
        transactions = (
            attributes.get("transactions") if isinstance(attributes, dict) else None
        )
        timeframe_data = (
            transactions.get(timeframe) if isinstance(transactions, dict) else None
        )
        if not isinstance(timeframe_data, dict):
            raise ValueError(
                f"Missing GeckoTerminal transactions.{timeframe} for {pair_address}"
            )

        buys = _as_float(timeframe_data.get("buys"))
        sells = _as_float(timeframe_data.get("sells"))
        if buys is None or sells is None:
            raise ValueError(
                f"Missing buys/sells in GeckoTerminal response for {pair_address}"
            )
    except Exception as exc:  # noqa: BLE001 - fail open is intentional here
        logger.warning(
            "GeckoTerminal verification failed for %s (%s): %s",
            pair_address,
            type(exc).__name__,
            exc,
        )
        return True, GECKO_FAILURE_WARNING

    min_buys = _as_float(filter_data.get("min_buys"))
    max_buys = _as_float(filter_data.get("max_buys"))
    min_sells = _as_float(filter_data.get("min_sells"))
    max_sells = _as_float(filter_data.get("max_sells"))

    if min_buys is not None and buys < min_buys:
        return False, None
    if max_buys is not None and buys > max_buys:
        return False, None
    if min_sells is not None and sells < min_sells:
        return False, None
    if max_sells is not None and sells > max_sells:
        return False, None

    return True, None


async def _process_pairs(
    client: httpx.AsyncClient | None,
    pairs: list[dict[str, Any]],
    filter_data: dict[str, Any],
    telegram_id: int,
    *,
    verify_with_gecko: bool,
    fallback_warning: str | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []

    for pair in pairs:
        if not _passes_core_filters(pair, filter_data):
            continue

        token_address = _token_address(pair)
        if not token_address:
            continue

        try:
            already_sent = await is_token_recently_sent(
                telegram_id, token_address
            )
        except Exception:  # noqa: BLE001 - a DB hiccup should not stop the scan
            logger.exception(
                "Duplicate check failed for %s; continuing", token_address
            )
            already_sent = False

        if already_sent:
            continue

        warning = fallback_warning
        if verify_with_gecko:
            if client is None:
                continue
            passed, gecko_warning = await _verify_with_geckoterminal(
                client, pair, filter_data
            )
            if not passed:
                continue
            if gecko_warning:
                warning = (
                    f"{warning}\n{gecko_warning}"
                    if warning
                    else gecko_warning
                )

        chain_id = str(pair.get("chainId") or "")
        try:
            await mark_token_as_sent(telegram_id, token_address, chain_id)
        except Exception:  # noqa: BLE001 - still alert if marking fails
            logger.exception(
                "Failed to mark %s as sent; alert will still be emitted",
                token_address,
            )

        matches.append({"pair": pair, "warning": warning})
        base_token = pair.get("baseToken")
        symbol = (
            base_token.get("symbol", "?")
            if isinstance(base_token, dict)
            else "?"
        )
        logger.info("Match: %s (%s:%s)", symbol, chain_id, token_address)

    return matches


async def scan_dexscreener(
    filter_data: dict[str, Any] | None,
    telegram_id: int,
) -> list[dict[str, Any]]:
    """Scan DexScreener and return matches in the required format.

    Each match is ``{"pair": <raw DexScreener pair>, "warning": <str|None>}``.
    Duplicate suppression is scoped to the supplied Telegram user.

    The primary path mirrors DexScreener's screener filters.  If the internal
    screener feed is unavailable, the scanner falls back to the latest token
    profiles and boosts and retains the GeckoTerminal verification step.
    """

    if not isinstance(filter_data, dict) or not filter_data:
        return []

    telegram_id = int(telegram_id)
    fallback_warning = (
        "⚠️ DexScreener screener unavailable; using promoted-token fallback."
    )

    try:
        try:
            screener_pairs = await fetch_screener_pairs(filter_data)
        except ScreenerUnavailable as exc:
            logger.warning(
                "DexScreener screener failed; falling back to profiles/boosts: %s",
                exc,
            )
        except Exception:  # noqa: BLE001 - fallback must remain available
            logger.exception(
                "Unexpected DexScreener screener failure; using fallback"
            )
        else:
            logger.info(
                "DexScreener screener returned %s candidate pairs",
                len(screener_pairs),
            )
            return await _process_pairs(
                None,
                screener_pairs,
                filter_data,
                telegram_id,
                verify_with_gecko=False,
                fallback_warning=None,
            )

        # Fallback: latest profiles + boosts through the public API.
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            token_addresses = await _fetch_latest_token_addresses(client)
            pairs = await _fetch_pairs_by_addresses(client, token_addresses)
            logger.info(
                "Fallback discovery returned %s pairs for %s promoted tokens",
                len(pairs),
                len(token_addresses),
            )
            return await _process_pairs(
                client,
                pairs,
                filter_data,
                telegram_id,
                verify_with_gecko=True,
                fallback_warning=fallback_warning,
            )

    except httpx.TimeoutException:
        logger.warning("DexScreener fallback request timed out")
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "DexScreener fallback returned HTTP %s", exc.response.status_code
        )
    except httpx.HTTPError as exc:
        logger.warning("DexScreener fallback request failed: %s", exc)
    except Exception:  # noqa: BLE001 - never let one scan crash the scheduler
        logger.exception("Unexpected scanner failure")

    return []
