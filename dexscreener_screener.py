"""DexScreener screener discovery.

DexScreener's website uses an internal WebSocket screener feed:
``wss://io.dexscreener.com/dex/screener/v7/pairs/{timeframe}/{page}``.
The feed accepts the same filter parameters as the website's "Customize
Filters" dialog and returns protobuf-encoded pair snapshots.

This module intentionally decodes only the fields needed by the bot.  The
endpoint is undocumented, so callers should treat failures as recoverable and
fall back to another discovery source when possible.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
import time
from typing import Any, Iterator
from urllib.parse import urlencode

import websockets
from websockets.exceptions import WebSocketException

logger = logging.getLogger(__name__)

SCREENER_WS_HOST = "wss://io.dexscreener.com"
SCREENER_WS_PATH = "/dex/screener/v7/pairs"
SCREENER_ORIGIN = "https://dexscreener.com"
SCREENER_USER_AGENT = "CryptoScannerBot/1.0 (+https://render.com)"

SCREENER_PAGE_SIZE = 100


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


SCREENER_MAX_PAGES = max(1, min(20, _env_int("SCREENER_MAX_PAGES", 3)))
SCREENER_TIMEOUT_SECONDS = max(
    5.0, _env_float("SCREENER_TIMEOUT_SECONDS", 15.0)
)

TIMEFRAME_TO_DEX = {
    "5M": "m5",
    "1H": "h1",
    "6H": "h6",
    "24H": "h24",
}

SUPPORTED_CHAINS = (
    "solana",
    "ethereum",
    "bsc",
    "base",
    "arbitrum",
    "polygon",
    "starknet",
    "robinhood",
)


class ScreenerUnavailable(RuntimeError):
    """Raised when the internal DexScreener screener cannot be reached."""


def _normalize_timeframe(filter_data: dict[str, Any]) -> str:
    raw = str(filter_data.get("timeframe") or "24H").strip().upper()
    return TIMEFRAME_TO_DEX.get(raw, "h24")


def _number(value: Any) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.12f}".rstrip("0").rstrip(".")


def _chain_filter_values(filter_data: dict[str, Any]) -> list[str]:
    chains = [
        str(chain).strip().lower()
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip().lower() in SUPPORTED_CHAINS
    ]
    chains = list(dict.fromkeys(chains))

    # The screener currently returns zero results when exactly one chain is
    # supplied.  Supplying the same chain twice satisfies its parser while
    # keeping the query scoped to the requested chain.
    if len(chains) == 1:
        chains.append(chains[0])
    return chains


def build_screener_url(
    filter_data: dict[str, Any],
    page: int = 1,
) -> str:
    """Build the screener WebSocket URL from the bot's filter data."""

    timeframe = _normalize_timeframe(filter_data)
    params: list[tuple[str, str]] = [
        ("rankBy[key]", "trendingScoreH6"),
        ("rankBy[order]", "desc"),
    ]

    for index, chain in enumerate(_chain_filter_values(filter_data)):
        params.append((f"filters[chainIds][{index}]", chain))

    min_liquidity = filter_data.get("min_liquidity")
    if min_liquidity is not None:
        params.append(("filters[liquidity][min]", _number(min_liquidity)))

    max_mcap = filter_data.get("max_mcap")
    if max_mcap is not None:
        params.append(("filters[marketCap][max]", _number(max_mcap)))

    max_age_days = filter_data.get("max_age_days")
    if max_age_days is not None:
        # DexScreener's pairAge filter is expressed in hours.
        params.append(
            ("filters[pairAge][max]", _number(float(max_age_days) * 24.0))
        )

    min_volume = filter_data.get("min_volume")
    if min_volume is not None:
        params.append(
            (
                f"filters[volume][{timeframe}][min]",
                _number(min_volume),
            )
        )

    max_volume = filter_data.get("max_volume")
    if max_volume is not None:
        params.append(
            (
                f"filters[volume][{timeframe}][max]",
                _number(max_volume),
            )
        )

    min_buys = filter_data.get("min_buys")
    if min_buys is not None:
        params.append(
            (f"filters[buys][{timeframe}][min]", _number(min_buys))
        )

    max_buys = filter_data.get("max_buys")
    if max_buys is not None:
        params.append(
            (f"filters[buys][{timeframe}][max]", _number(max_buys))
        )

    min_sells = filter_data.get("min_sells")
    if min_sells is not None:
        params.append(
            (f"filters[sells][{timeframe}][min]", _number(min_sells))
        )

    max_sells = filter_data.get("max_sells")
    if max_sells is not None:
        params.append(
            (f"filters[sells][{timeframe}][max]", _number(max_sells))
        )

    min_change = filter_data.get("min_change")
    if min_change is not None:
        params.append(
            (
                f"filters[priceChange][{timeframe}][min]",
                _number(min_change),
            )
        )

    max_change = filter_data.get("max_change")
    if max_change is not None:
        params.append(
            (
                f"filters[priceChange][{timeframe}][max]",
                _number(max_change),
            )
        )

    labels = [
        str(label).strip()
        for label in (filter_data.get("labels") or [])
        if str(label).strip()
    ]
    for index, label in enumerate(labels):
        params.append((f"filters[labels][{index}]", label))

    address_suffix = str(filter_data.get("address_suffix") or "").strip()
    if address_suffix:
        params.append(("filters[baseTokenSuffixes][0]", address_suffix))

    # Match DexScreener's Profile / Boosted / Ads / Launchpad toggles.
    if filter_data.get("req_profile"):
        params.append(("filters[enhancedTokenInfo]", "true"))
    if filter_data.get("req_boosted"):
        params.append(("filters[activeBoosts][min]", "1"))
    if filter_data.get("req_ads"):
        params.append(("filters[recentPurchasedImpressions][min]", "1"))
    if filter_data.get("req_launchpad"):
        params.append(("filters[launchpadProgress][min]", "1"))

    query = urlencode(params)
    return (
        f"{SCREENER_WS_HOST}{SCREENER_WS_PATH}/"
        f"{timeframe}/{max(1, int(page))}?{query}"
    )


# ---------------------------------------------------------------------------
# Minimal protobuf wire decoder for the screener response.
# ---------------------------------------------------------------------------


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(data):
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
        if shift > 63:
            raise ValueError("Invalid protobuf varint")
    raise ValueError("Truncated protobuf varint")


def _iter_fields(data: bytes) -> Iterator[tuple[int, int, Any]]:
    position = 0
    while position < len(data):
        key, position = _read_varint(data, position)
        field_number = key >> 3
        wire_type = key & 0x07

        if wire_type == 0:
            value, position = _read_varint(data, position)
        elif wire_type == 1:
            if position + 8 > len(data):
                raise ValueError("Truncated fixed64 field")
            value = data[position : position + 8]
            position += 8
        elif wire_type == 2:
            length, position = _read_varint(data, position)
            if position + length > len(data):
                raise ValueError("Truncated length-delimited field")
            value = data[position : position + length]
            position += length
        elif wire_type == 5:
            if position + 4 > len(data):
                raise ValueError("Truncated fixed32 field")
            value = data[position : position + 4]
            position += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")

        yield field_number, wire_type, value


def _text(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _double(value: bytes) -> float:
    if len(value) != 8:
        raise ValueError("Invalid fixed64 double")
    return struct.unpack("<d", value)[0]


def _wrapper_string(data: bytes) -> str | None:
    for field, wire, value in _iter_fields(data):
        if field == 1 and wire == 2:
            return _text(value)
    return None


def _wrapper_double(data: bytes) -> float | None:
    for field, wire, value in _iter_fields(data):
        if field == 1 and wire == 1:
            return _double(value)
    return None


def _parse_token(data: bytes) -> dict[str, str]:
    token = {"address": "", "name": "", "symbol": ""}
    for field, wire, value in _iter_fields(data):
        if wire != 2:
            continue
        if field == 1:
            token["address"] = _text(value)
        elif field == 2:
            token["name"] = _text(value)
        elif field == 3:
            token["symbol"] = _text(value)
    return token


_TIMEFRAME_FIELDS = {1: "m5", 2: "h1", 3: "h6", 4: "h24"}


def _parse_txns(data: bytes) -> dict[str, dict[str, int]]:
    txns: dict[str, dict[str, int]] = {}
    for field, wire, value in _iter_fields(data):
        key = _TIMEFRAME_FIELDS.get(field)
        if not key or wire != 2:
            continue
        buys = 0
        sells = 0
        for inner_field, inner_wire, inner_value in _iter_fields(value):
            if inner_wire != 0:
                continue
            if inner_field == 1:
                buys = int(inner_value)
            elif inner_field == 2:
                sells = int(inner_value)
        txns[key] = {"buys": buys, "sells": sells}
    return txns


def _parse_double_map(data: bytes) -> dict[str, float]:
    values: dict[str, float] = {}
    for field, wire, value in _iter_fields(data):
        key = _TIMEFRAME_FIELDS.get(field)
        if not key or wire != 2:
            continue
        parsed = _wrapper_double(value)
        if parsed is not None:
            values[key] = parsed
    return values


def _parse_liquidity(data: bytes) -> dict[str, float]:
    liquidity: dict[str, float] = {}
    names = {1: "usd", 2: "base", 3: "quote"}
    for field, wire, value in _iter_fields(data):
        name = names.get(field)
        if name and wire == 1:
            liquidity[name] = _double(value)
    return liquidity


def _parse_pair(data: bytes) -> dict[str, Any]:
    pair: dict[str, Any] = {
        "chainId": "",
        "dexId": "",
        "labels": [],
        "pairAddress": "",
        "baseToken": {"address": "", "name": "", "symbol": ""},
        "quoteToken": {"address": "", "name": "", "symbol": ""},
        "price": "",
        "priceUsd": None,
        "txns": {},
        "volume": {},
        "priceChange": {},
        "liquidity": {},
        "marketCap": None,
        "fdv": None,
        "pairCreatedAt": None,
        "boosts": None,
    }

    for field, wire, value in _iter_fields(data):
        if field == 6 and wire == 2:
            pair["chainId"] = _text(value)
        elif field == 7 and wire == 2:
            pair["dexId"] = _text(value)
        elif field == 8 and wire == 2:
            pair["labels"].append(_text(value))
        elif field == 9 and wire == 2:
            pair["pairAddress"] = _text(value)
        elif field == 10 and wire == 2:
            pair["baseToken"] = _parse_token(value)
        elif field == 11 and wire == 2:
            pair["quoteToken"] = _parse_token(value)
        elif field == 12 and wire == 2:
            pair["price"] = _text(value)
        elif field == 13 and wire == 2:
            pair["priceUsd"] = _wrapper_string(value)
        elif field == 14 and wire == 2:
            pair["txns"] = _parse_txns(value)
        elif field == 18 and wire == 2:
            pair["volume"] = _parse_double_map(value)
        elif field == 21 and wire == 2:
            pair["priceChange"] = _parse_double_map(value)
        elif field == 22 and wire == 2:
            pair["liquidity"] = _parse_liquidity(value)
        elif field == 23 and wire == 2:
            pair["marketCap"] = _wrapper_double(value)
        elif field == 24 and wire == 2:
            pair["fdv"] = _wrapper_double(value)
        elif field == 25 and wire == 2:
            seconds = 0
            nanos = 0
            for inner_field, inner_wire, inner_value in _iter_fields(value):
                if inner_wire != 0:
                    continue
                if inner_field == 1:
                    seconds = int(inner_value)
                elif inner_field == 2:
                    nanos = int(inner_value)
            if seconds:
                pair["pairCreatedAt"] = (
                    seconds * 1000 + nanos // 1_000_000
                )
        elif field == 29 and wire == 2:
            active = 0
            for inner_field, inner_wire, inner_value in _iter_fields(value):
                if inner_field == 1 and inner_wire == 0:
                    active = int(inner_value)
            pair["boosts"] = {"active": active}

    chain_id = str(pair.get("chainId") or "")
    pair_address = str(pair.get("pairAddress") or "")
    if chain_id and pair_address:
        pair["url"] = f"https://dexscreener.com/{chain_id}/{pair_address}"
    return pair


def _parse_pairs_frame(data: bytes) -> tuple[bool, list[dict[str, Any]], int]:
    pairs_payload: bytes | None = None
    for field, wire, value in _iter_fields(data):
        if field == 1 and wire == 2:
            pairs_payload = value
            break

    if pairs_payload is None:
        return False, [], 0

    pairs: list[dict[str, Any]] = []
    total = 0
    for field, wire, value in _iter_fields(pairs_payload):
        if field == 2 and wire == 2:
            pairs.append(_parse_pair(value))
        elif field == 3 and wire == 0:
            total = int(value)
    return True, pairs, total


def _parse_channel_frame(
    data: bytes,
) -> tuple[bool, list[dict[str, Any]], int]:
    try:
        return _parse_pairs_frame(data)
    except (ValueError, struct.error) as exc:
        raise ScreenerUnavailable(
            f"Could not decode DexScreener screener response: {exc}"
        ) from exc


async def _fetch_page(
    filter_data: dict[str, Any],
    page: int,
) -> tuple[list[dict[str, Any]], int]:
    url = build_screener_url(filter_data, page)
    deadline = time.monotonic() + SCREENER_TIMEOUT_SECONDS

    try:
        async with websockets.connect(
            url,
            origin=SCREENER_ORIGIN,
            additional_headers={"User-Agent": SCREENER_USER_AGENT},
            open_timeout=SCREENER_TIMEOUT_SECONDS,
            close_timeout=5,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                message = await asyncio.wait_for(
                    websocket.recv(), timeout=remaining
                )
                if not isinstance(message, bytes):
                    continue
                is_pairs, pairs, total = _parse_channel_frame(message)
                if is_pairs:
                    return pairs, total
    except asyncio.TimeoutError as exc:
        raise ScreenerUnavailable(
            "DexScreener screener timed out"
        ) from exc
    except (OSError, WebSocketException, ValueError) as exc:
        raise ScreenerUnavailable(
            f"DexScreener screener unavailable: {type(exc).__name__}: {exc}"
        ) from exc


async def fetch_screener_pairs(
    filter_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return pairs matching the filter from DexScreener's screener feed.

    Raises ``ScreenerUnavailable`` when the internal feed cannot be reached.
    A successful query with no matches returns an empty list.
    """

    if not isinstance(filter_data, dict) or not filter_data:
        return []

    selected_chains = [
        str(chain).strip().lower()
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip()
    ]
    if not selected_chains:
        return []

    first_pairs, total = await _fetch_page(filter_data, 1)
    pairs: list[dict[str, Any]] = list(first_pairs)

    total_pages = max(
        1, math.ceil(total / SCREENER_PAGE_SIZE)
    ) if total > 0 else 1
    pages_to_fetch = min(total_pages, SCREENER_MAX_PAGES)
    if total_pages > pages_to_fetch:
        logger.info(
            "DexScreener screener has %s matching pairs; fetching %s of %s pages",
            total,
            pages_to_fetch,
            total_pages,
        )

    for page in range(2, pages_to_fetch + 1):
        try:
            page_pairs, _page_total = await _fetch_page(filter_data, page)
        except ScreenerUnavailable:
            logger.warning(
                "Screener page %s failed; using %s already-fetched pairs",
                page,
                len(pairs),
                exc_info=True,
            )
            break
        if not page_pairs:
            break
        pairs.extend(page_pairs)

    allowed_chains = {
        str(chain).strip().lower()
        for chain in (filter_data.get("chains") or [])
        if str(chain).strip()
    }
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        chain_id = str(pair.get("chainId") or "").lower()
        pair_address = str(pair.get("pairAddress") or "").lower()
        if allowed_chains and chain_id not in allowed_chains:
            continue
        key = (chain_id, pair_address)
        if not pair_address or key in seen:
            continue
        seen.add(key)
        deduplicated.append(pair)

    logger.info(
        "DexScreener screener returned %s pairs (%s total matches)",
        len(deduplicated),
        total,
    )
    return deduplicated
