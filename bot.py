
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V19 - SMART DISCOVERY

SOURCES:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun
5. VoidScreener
6. DEXTools via API.SH

SCANNERS:
- ATH DROP
- SMART RECOVERY
- BOTTOM FORMING
- CONFIRMED REVERSAL
- ATH BREAKOUT
- ATH RECLAIM
- TELEGRAM FORWARDED CHARTS

V19 IMPROVEMENTS:
- Robust DEXTools response parsing.
- Strict Solana mint validation.
- DEXTools rejection diagnostics.
- No mandatory market-cap filter.
- Unknown market cap allowed.
- No volume filter.
- No minimum technical score.
- Better API error reporting.
- In-run GET cache.
- Better Telegram error diagnostics.
- Historical ATH up to 990 days.
- Pair creation fallback for history start.
- No automatic trading.
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

import requests
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle


# =========================================================
# CONFIG
# =========================================================

VERSION = "V19 SMART DISCOVERY"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
COINGECKO_KEY = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
MORALIS_KEY = os.getenv("MORALIS_API_KEY", "").strip()
APISH_KEY = os.getenv("APISH_KEY", "").strip()

CHAT_IDS = [
    "6665909136",
    "6765810776",
    "98529959",
    "1507608801",
    "1012061523",
]

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
PUMP = "https://frontend-api-v3.pump.fun"
STONK = "https://www.stonkfun.xyz/api/public/v1"
VOID = "https://api.voidscreener.com"

APISH_DEXTOOLS = "https://api.api.sh/dextools/v1/hot-pairs"

COINGECKO_ONCHAIN = "https://api.coingecko.com/api/v3/onchain"
MORALIS = "https://solana-gateway.moralis.io"

HTTP_TIMEOUT = 20
MAX_SOURCE_ROWS = 100

LOW_CAP_REFERENCE = 5000

MIN_ATH_DROP = 30.0
ATH_CHECKS_PER_RUN = 5
ATH_MAX_AGE_DAYS = 990
ATH_HISTORY_GAP_DAYS = 7

RECOVERY_CHECKS_PER_RUN = 20

BREAKOUT_CHECKS_PER_RUN = 5
BREAKOUT_DROP_PERCENT = 75.0
BREAKOUT_MAX_AGE_DAYS = 990

APISH_FETCH_INTERVAL = 3600

CANDLE_4H_LIMIT = 100
CANDLE_1D_LIMIT = 1000

TELEGRAM_UPDATES_LIMIT = 30

WATCH_COOLDOWN = 12 * 3600
RECOVERY_COOLDOWN = 12 * 3600
STRONG_COOLDOWN = 24 * 3600

SEEN_FILE = Path("seen.json")
RECOVERY_FILE = Path("recovery.json")
PERFORMANCE_FILE = Path("performance.json")
CHART_DIR = Path("charts")

SOLANA_MINT_PATTERN = re.compile(
    r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
)

ADDRESS_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)

HEADERS = {
    "User-Agent": "DEX-HUNTER-V19/1.0",
    "Accept": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)

JSON_CACHE = {}
JSON_CACHE_SECONDS = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# =========================================================
# BASIC HELPERS
# =========================================================

def now():
    return time.time()


def utc_day():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def number(value):
    try:
        if value is None or value == "":
            return None

        result = float(value)

        if result != result:
            return None

        if result in (float("inf"), float("-inf")):
            return None

        return result

    except Exception:
        return None


def first(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def timestamp(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            return dt.timestamp()

        except Exception:
            pass

    result = number(value)

    if result is None or result <= 0:
        return None

    if result > 10_000_000_000:
        result /= 1000

    return result


def valid_mint(mint):
    if not isinstance(mint, str):
        return False

    mint = mint.strip()

    return bool(SOLANA_MINT_PATTERN.fullmatch(mint))


def dex_url(mint):
    return f"https://dexscreener.com/solana/{mint}"


def history_start_ts(item):
    return first(
        item.get("created_ts"),
        item.get("pair_created_ts"),
    )


def get_json(url, params=None, headers=None, cache_seconds=0):
    cache_key = (
        url,
        json.dumps(params or {}, sort_keys=True),
    )

    if cache_seconds:
        cached = JSON_CACHE.get(cache_key)

        if cached and now() - cached["time"] < cache_seconds:
            return cached["data"]

    try:
        response = session.get(
            url,
            params=params,
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            logging.warning(
                "Rate limited: %s | retry_after=%s",
                url,
                retry_after,
            )
            return None

        response.raise_for_status()

        try:
            payload = response.json()
        except ValueError:
            logging.warning(
                "Invalid JSON response: %s",
                url,
            )
            return None

        if cache_seconds:
            JSON_CACHE[cache_key] = {
                "time": now(),
                "data": payload,
            }

        return payload

    except requests.RequestException as exc:
        logging.warning(
            "API error %s: %s",
            url,
            exc,
        )
        return None


def rows_from(payload):
    """
    Find arrays of row dictionaries in common API wrappers.
    Supports API.SH pairs, nested data and raw structures.
    """

    if isinstance(payload, list):
        return [
            row for row in payload
            if isinstance(row, dict)
        ]

    if not isinstance(payload, dict):
        return []

    preferred = (
        "pairs",
        "hot_pairs",
        "tokens",
        "coins",
        "items",
        "results",
        "pools",
    )

    for key in preferred:
        value = payload.get(key)

        if isinstance(value, list):
            return [
                row for row in value
                if isinstance(row, dict)
            ]

    for key in ("data", "result", "raw"):
        value = payload.get(key)

        if isinstance(value, list):
            return [
                row for row in value
                if isinstance(row, dict)
            ]

        if isinstance(value, dict):
            found = rows_from(value)

            if found:
                return found

    return []


def nested_dict(value):
    return value if isinstance(value, dict) else {}


def deep_value(data, keys, max_depth=4):
    """
    Search only for named fields.
    Does not treat pair/pool addresses as token addresses.
    """

    if not isinstance(data, dict):
        return None

    wanted = set(keys)

    def walk(obj, depth):
        if not isinstance(obj, dict) or depth > max_depth:
            return None

        for key in keys:
            value = obj.get(key)

            if value is not None and value != "":
                return value

        for value in obj.values():
            if isinstance(value, dict):
                result = walk(value, depth + 1)

                if result is not None:
                    return result

        return None

    return walk(data, 0)


# =========================================================
# DATABASES
# =========================================================

def read_json(path, default):
    if not path.exists():
        return default

    try:
        data = json.loads(
            path.read_text(encoding="utf-8")
        )

        if isinstance(data, dict):
            return data

    except Exception:
        logging.exception(
            "Could not read %s",
            path,
        )

    return default


def write_json(path, data):
    temp = path.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(path)


def empty_seen():
    return {
        "sent": {
            "ath": [],
            "breakout": [],
            "watch": [],
            "recovery": [],
            "strong": [],
            "ath_reclaim": [],
        },
        "ath_watch": {},
        "telegram_update_offset": 0,
        "ath_cursor": 0,
        "breakout_cursor": 0,
        "daily_reset_date": None,
        "last_apish_fetch_ts": 0,
    }


def load_seen():
    state = read_json(
        SEEN_FILE,
        empty_seen(),
    )

    state.setdefault("sent", {})
    state.setdefault("ath_watch", {})
    state.setdefault("telegram_update_offset", 0)
    state.setdefault("ath_cursor", 0)
    state.setdefault("breakout_cursor", 0)
    state.setdefault("daily_reset_date", None)
    state.setdefault("last_apish_fetch_ts", 0)

    for key in (
        "ath",
        "breakout",
        "watch",
        "recovery",
        "strong",
        "ath_reclaim",
    ):
        state["sent"].setdefault(key, [])

    return state


def load_recovery():
    data = read_json(
        RECOVERY_FILE,
        {"tokens": {}, "cursor": 0},
    )

    data.setdefault("tokens", {})
    data.setdefault("cursor", 0)

    return data


def load_performance():
    data = read_json(
        PERFORMANCE_FILE,
        {"signals": []},
    )

    data.setdefault("signals", [])

    return data


# =========================================================
# DAILY RESET
# =========================================================

def daily_reset(state, recovery):
    today = utc_day()

    if state.get("daily_reset_date") == today:
        return False

    logging.info(
        "DAILY RESET: %s -> %s",
        state.get("daily_reset_date"),
        today,
    )

    state["sent"] = {
        "ath": [],
        "breakout": [],
        "watch": [],
        "recovery": [],
        "strong": [],
        "ath_reclaim": [],
    }

    state["ath_cursor"] = 0
    state["breakout_cursor"] = 0

    recovery["tokens"] = {}
    recovery["cursor"] = 0

    # Preserve ATH history, Telegram offset,
    # performance and API.SH fetch timestamp.

    state["daily_reset_date"] = today

    write_json(SEEN_FILE, state)
    write_json(RECOVERY_FILE, recovery)

    return True


# =========================================================
# TELEGRAM
# =========================================================

def telegram_request(method, **kwargs):
    if not BOT_TOKEN:
        return None

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    try:
        response = session.post(
            url,
            data=kwargs,
            timeout=HTTP_TIMEOUT,
        )

        if not response.ok:
            logging.warning(
                "Telegram HTTP error | method=%s | status=%s | body=%s",
                method,
                response.status_code,
                response.text[:500],
            )
            return None

        result = response.json()

        if result.get("ok"):
            return result.get("result")

        logging.warning(
            "Telegram rejected request | method=%s | description=%s",
            method,
            result.get("description"),
        )

    except Exception as exc:
        logging.warning(
            "Telegram %s failed: %s",
            method,
            exc,
        )

    return None


def send_message(chat_id, message):
    result = telegram_request(
        "sendMessage",
        chat_id=chat_id,
        text=message,
        disable_web_page_preview="true",
    )

    return result is not None


def send_photo(chat_id, filename):
    if not BOT_TOKEN:
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendPhoto"
    )

    try:
        with open(filename, "rb") as photo:
            response = session.post(
                url,
                data={"chat_id": chat_id},
                files={"photo": photo},
                timeout=HTTP_TIMEOUT,
            )

        if not response.ok:
            logging.warning(
                "Telegram photo HTTP error | status=%s | body=%s",
                response.status_code,
                response.text[:500],
            )
            return False

        payload = response.json()

        if not payload.get("ok"):
            logging.warning(
                "Telegram photo rejected: %s",
                payload.get("description"),
            )

        return bool(payload.get("ok"))

    except Exception as exc:
        logging.warning(
            "Photo send failed: %s",
            exc,
        )
        return False


def deliver(state, category, mint, message):
    sent = set(state["sent"][category])
    successful = False

    for chat_id in CHAT_IDS:
        key = f"{mint}|{chat_id}"

        if key in sent:
            continue

        if send_message(chat_id, message):
            sent.add(key)
            successful = True

            state["sent"][category] = sorted(sent)
            write_json(SEEN_FILE, state)

        time.sleep(0.12)

    return successful


# =========================================================
# TOKEN NORMALIZATION
# =========================================================

def normalize(row, source):
    if not isinstance(row, dict):
        return None

    attrs = nested_dict(row.get("attributes"))
    rels = nested_dict(row.get("relationships"))

    token = nested_dict(row.get("token"))
    base = nested_dict(row.get("baseToken"))

    base_id = (
        nested_dict(rels.get("base_token"))
        .get("data") or {}
    )

    base_id = (
        base_id.get("id")
        if isinstance(base_id, dict)
        else None
    )

    mint = first(
        row.get("mint"),
        row.get("token_address"),
        row.get("tokenAddress"),
        row.get("contractAddress"),
        row.get("baseTokenAddress"),
        row.get("address"),
        token.get("address"),
        base.get("address"),
        attrs.get("address"),
        attrs.get("mint"),
        (
            base_id.split("_", 1)[1]
            if isinstance(base_id, str) and "_" in base_id
            else None
        ),
    )

    if not valid_mint(mint):
        return None

    created = first(
        row.get("created_timestamp"),
        row.get("token_created_at"),
        row.get("tokenCreatedAt"),
        row.get("createdAt"),
        row.get("created_at"),
        row.get("createdAtUnix"),
        row.get("creation_timestamp"),
        row.get("launch_timestamp"),
        row.get("timestamp"),
        attrs.get("created_timestamp"),
        attrs.get("created_at"),
    )

    pair_created = first(
        row.get("pairCreatedAt"),
        row.get("pair_created_at"),
        row.get("poolCreatedAt"),
        row.get("created_at"),
        attrs.get("pair_created_at"),
        attrs.get("pool_created_at"),
    )

    return {
        "mint": mint,
        "name": str(first(
            row.get("name"),
            row.get("tokenName"),
            token.get("name"),
            base.get("name"),
            attrs.get("name"),
            "Unknown",
        )),
        "symbol": str(first(
            row.get("symbol"),
            row.get("ticker"),
            token.get("symbol"),
            base.get("symbol"),
            attrs.get("symbol"),
            "???",
        )),
        "sources": [source],
        "price": number(first(
            row.get("priceUsd"),
            row.get("price_usd"),
            row.get("price"),
            attrs.get("base_token_price_usd"),
        )),
        "market_cap": number(first(
            row.get("marketCap"),
            row.get("market_cap"),
            row.get("usd_market_cap"),
            row.get("marketCapUsd"),
            row.get("market_cap_usd"),
            attrs.get("market_cap_usd"),
        )),
        "created_ts": timestamp(created),
        "pair_created_ts": timestamp(pair_created),
        "liquidity": number(first(
            row.get("liquidityUsd"),
            row.get("liquidity_usd"),
            row.get("liquidity"),
            attrs.get("reserve_in_usd"),
        )) or 0.0,
        "url": first(
            row.get("url"),
            row.get("pairUrl"),
            row.get("pair_url"),
            attrs.get("url"),
        ),
    }


def add_candidate(store, candidate):
    if not candidate:
        return

    mint = candidate["mint"]

    if mint not in store:
        store[mint] = candidate
        return

    old = store[mint]

    old["sources"] = sorted(
        set(old.get("sources", []))
        | set(candidate.get("sources", []))
    )

    for key in (
        "name",
        "symbol",
        "price",
        "market_cap",
        "created_ts",
        "pair_created_ts",
        "url",
    ):
        if old.get(key) is None or old.get(key) in (
            "Unknown",
            "???",
        ):
            if candidate.get(key) is not None:
                old[key] = candidate[key]

    old["liquidity"] = max(
        number(old.get("liquidity")) or 0,
        number(candidate.get("liquidity")) or 0,
    )


# =========================================================
# DISCOVERY SOURCES
# =========================================================

def source_dexscreener(store):
    endpoints = [
        f"{DEX}/token-profiles/latest/v1",
        f"{DEX}/token-boosts/latest/v1",
        f"{DEX}/token-boosts/top/v1",
    ]

    for url in endpoints:
        payload = get_json(url)

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
            if row.get("chainId") not in (None, "solana"):
                continue

            mint = row.get("tokenAddress")

            if not valid_mint(mint):
                continue

            candidate = normalize(
                {
                    "mint": mint,
                    "name": row.get("description") or "Unknown",
                    "url": row.get("url"),
                },
                "DexScreener",
            )

            add_candidate(store, candidate)


def source_gecko(store):
    for endpoint in (
        "new_pools",
        "trending_pools",
    ):
        payload = get_json(
            f"{GECKO}/networks/solana/{endpoint}",
            {"page": 1},
            cache_seconds=10,
        )

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
            rels = nested_dict(row.get("relationships"))

            base_data = (
                nested_dict(rels.get("base_token"))
                .get("data") or {}
            )

            base_id = (
                base_data.get("id")
                if isinstance(base_data, dict)
                else None
            )

            mint = (
                base_id.split("_", 1)[1]
                if isinstance(base_id, str) and "_" in base_id
                else None
            )

            if not valid_mint(mint):
                continue

            attrs = nested_dict(row.get("attributes"))

            candidate = normalize(
                {
                    "mint": mint,
                    "name": attrs.get("name"),
                    "price_usd": attrs.get("base_token_price_usd"),
                    "created_at": attrs.get("pool_created_at"),
                    "url": attrs.get("url"),
                },
                "GeckoTerminal",
            )

            add_candidate(store, candidate)


def source_pump(store):
    payload = get_json(
        f"{PUMP}/coins/latest",
        {
            "offset": 0,
            "limit": MAX_SOURCE_ROWS,
            "includeNsfw": "false",
        },
    )

    for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
        add_candidate(
            store,
            normalize(row, "Pump.fun"),
        )


def source_stonk(store):
    payload = get_json(
        f"{STONK}/tokens",
        {
            "sort": "newest",
            "page": 1,
            "pageSize": MAX_SOURCE_ROWS,
        },
    )

    for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
        add_candidate(
            store,
            normalize(row, "Stonk.fun"),
        )


def source_void(store):
    endpoints = [
        (
            f"{VOID}/api-trending",
            {"chain": "solana"},
        ),
        (
            f"{VOID}/api-tokens",
            {
                "chain": "solana",
                "limit": MAX_SOURCE_ROWS,
            },
        ),
    ]

    for url, params in endpoints:
        payload = get_json(url, params)

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
            add_candidate(
                store,
                normalize(row, "VoidScreener"),
            )


# =========================================================
# DEXTOOLS VIA API.SH - V19
# =========================================================

def dextools_token_address(row):
    """
    Extract only token contract address.
    Never use pair/pool address as token address.
    """

    if not isinstance(row, dict):
        return None

    token = nested_dict(row.get("token"))
    base = nested_dict(row.get("baseToken"))
    attrs = nested_dict(row.get("attributes"))
    raw = nested_dict(row.get("raw"))

    possible = [
        row.get("token_address"),
        row.get("tokenAddress"),
        row.get("baseTokenAddress"),
        row.get("base_token_address"),
        row.get("contractAddress"),
        row.get("mint"),
        token.get("address"),
        token.get("token_address"),
        base.get("address"),
        base.get("token_address"),
        attrs.get("address"),
        attrs.get("mint"),
        raw.get("token_address"),
        raw.get("tokenAddress"),
    ]

    for value in possible:
        if valid_mint(value):
            return value.strip()

    # Some wrappers put token data under data.
    for key in ("data", "result"):
        nested = row.get(key)

        if isinstance(nested, dict):
            result = dextools_token_address(nested)

            if result:
                return result

    return None


def dextools_row_candidate(row):
    if not isinstance(row, dict):
        return None, "not_dict"

    chain = str(
        first(
            row.get("chain"),
            row.get("chainId"),
            "",
        )
    ).lower()

    if chain and chain not in (
        "solana",
        "sol",
    ):
        return None, "wrong_chain"

    mint = dextools_token_address(row)

    if not mint:
        return None, "missing_or_invalid_token_address"

    token = nested_dict(row.get("token"))
    base = nested_dict(row.get("baseToken"))
    raw = nested_dict(row.get("raw"))

    name = first(
        row.get("name"),
        token.get("name"),
        base.get("name"),
        raw.get("name"),
        "Unknown",
    )

    symbol = first(
        row.get("symbol"),
        token.get("symbol"),
        base.get("symbol"),
        raw.get("symbol"),
        "???",
    )

    # DEXTools created_at is pair creation time.
    pair_created = first(
        row.get("created_at"),
        row.get("createdAt"),
        row.get("pair_created_at"),
        row.get("pairCreatedAt"),
        raw.get("created_at"),
    )

    data = {
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "price_usd": first(
            row.get("price_usd"),
            row.get("priceUsd"),
            raw.get("price_usd"),
        ),
        "market_cap_usd": first(
            row.get("market_cap_usd"),
            row.get("marketCap"),
            row.get("fdv"),
            raw.get("market_cap_usd"),
        ),
        "pair_created_at": pair_created,
        "liquidity_usd": first(
            row.get("liquidity_usd"),
            row.get("liquidityUsd"),
            raw.get("liquidity_usd"),
        ),
        "url": first(
            row.get("url"),
            row.get("pair_url"),
            raw.get("url"),
        ),
    }

    candidate = normalize(
        data,
        "DEXTools",
    )

    if not candidate:
        return None, "normalization_failed"

    return candidate, None


def source_dextools(store, state):
    if not APISH_KEY:
        logging.warning(
            "DEXTools skipped: APISH_KEY is missing."
        )
        return 0

    last_fetch = number(
        state.get("last_apish_fetch_ts")
    ) or 0

    elapsed = now() - last_fetch

    if elapsed < APISH_FETCH_INTERVAL:
        logging.info(
            "DEXTools skipped: next fetch in %.0f seconds.",
            APISH_FETCH_INTERVAL - elapsed,
        )
        return 0

    logging.info(
        "DEXTools request started | chain=solana"
    )

    payload = get_json(
        APISH_DEXTOOLS,
        params={"chain": "solana"},
        headers={
            "x-api-key": APISH_KEY,
            "accept": "application/json",
        },
    )

    if payload is None:
        logging.warning(
            "DEXTools request failed. Timestamp unchanged."
        )
        return 0

    if (
        isinstance(payload, dict)
        and payload.get("success") is False
    ):
        logging.warning(
            "DEXTools API returned unsuccessful response: %s",
            payload.get("error"),
        )
        return 0

    rows = rows_from(payload)

    logging.info(
        "DEXTools response parsed | rows=%d | top_keys=%s",
        len(rows),
        list(payload.keys())[:20]
        if isinstance(payload, dict)
        else type(payload).__name__,
    )

    added = 0
    rejected = {}

    for row in rows[:MAX_SOURCE_ROWS]:
        candidate, reason = dextools_row_candidate(row)

        if not candidate:
            rejected[reason or "unknown"] = (
                rejected.get(reason or "unknown", 0) + 1
            )
            continue

        mint = candidate["mint"]

        existed = mint in store

        add_candidate(store, candidate)

        if not existed:
            added += 1

    # Successful API response, even if no rows are usable.
    state["last_apish_fetch_ts"] = now()
    write_json(SEEN_FILE, state)

    logging.info(
        "DEXTools result | rows=%d | new_tokens=%d | rejected=%s",
        len(rows),
        added,
        rejected,
    )

    if rows and added == 0:
        sample = rows[0]

        logging.warning(
            "DEXTools zero accepted diagnostic | "
            "sample_keys=%s | sample_chain=%s | "
            "sample_token_address=%s",
            list(sample.keys())[:30],
            sample.get("chain"),
            str(sample.get("token_address"))[:60],
        )

    return added


# =========================================================
# DEXSCREENER ENRICHMENT
# =========================================================

def enrich(store):
    mints = list(store.keys())

    for start in range(0, len(mints), 30):
        batch = mints[start:start + 30]

        payload = get_json(
            f"{DEX}/tokens/v1/solana/{','.join(batch)}",
            cache_seconds=10,
        )

        if not isinstance(payload, list):
            continue

        for pair in payload:
            if not isinstance(pair, dict):
                continue

            if pair.get("chainId") != "solana":
                continue

            base = nested_dict(pair.get("baseToken"))
            mint = base.get("address")

            if mint not in store:
                continue

            item = store[mint]

            liquidity = number(
                nested_dict(pair.get("liquidity")).get("usd")
            ) or 0.0

            if liquidity >= (
                number(item.get("liquidity")) or 0
            ):
                item["liquidity"] = liquidity

                item["price"] = (
                    number(pair.get("priceUsd"))
                    or item.get("price")
                )

                item["market_cap"] = (
                    number(pair.get("marketCap"))
                    or number(pair.get("fdv"))
                    or item.get("market_cap")
                )

                item["name"] = (
                    base.get("name") or item["name"]
                )

                item["symbol"] = (
                    base.get("symbol") or item["symbol"]
                )

                item["url"] = (
                    pair.get("url") or item.get("url")
                )

            pair_created = timestamp(
                pair.get("pairCreatedAt")
            )

            if pair_created:
                old = item.get("pair_created_ts")

                if old is None or pair_created < old:
                    item["pair_created_ts"] = pair_created


# =========================================================
# CANDLE DATA
# =========================================================

def extract_ohlcv(payload):
    if not isinstance(payload, dict):
        return []

    try:
        rows = payload["data"]["attributes"]["ohlcv_list"]

        if isinstance(rows, list):
            return rows

    except Exception:
        pass

    for key in (
        "result",
        "data",
        "ohlcv",
        "candles",
        "items",
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for inner in (
                "result",
                "data",
                "ohlcv",
                "candles",
                "items",
            ):
                if isinstance(value.get(inner), list):
                    return value[inner]

    return []


def normalize_candles(raw):
    output = []

    for item in raw:
        if isinstance(item, dict):
            ts = first(
                item.get("timestamp"),
                item.get("time"),
                item.get("t"),
                item.get("from"),
            )

            op = first(item.get("open"), item.get("o"))
            hi = first(item.get("high"), item.get("h"))
            lo = first(item.get("low"), item.get("l"))
            cl = first(item.get("close"), item.get("c"))

        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            ts, op, hi, lo, cl = item[:5]

        else:
            continue

        ts = timestamp(ts)

        op, hi, lo, cl = map(
            number,
            (op, hi, lo, cl),
        )

        if ts is None or any(
            x is None or x <= 0
            for x in (op, hi, lo, cl)
        ):
            continue

        output.append({
            "time": ts,
            "open": op,
            "high": hi,
            "low": lo,
            "close": cl,
        })

    output.sort(key=lambda x: x["time"])

    unique = {}

    for candle in output:
        unique[candle["time"]] = candle

    return list(unique.values())


def gecko_best_pool(mint):
    payload = get_json(
        f"{GECKO}/networks/solana/tokens/{mint}/pools",
        {"page": 1},
        cache_seconds=20,
    )

    best = None
    best_liquidity = -1
    best_side = None

    for row in rows_from(payload):
        attrs = nested_dict(row.get("attributes"))
        rels = nested_dict(row.get("relationships"))

        pool_id = row.get("id")

        pool_address = (
            pool_id.split("_", 1)[1]
            if isinstance(pool_id, str) and "_" in pool_id
            else attrs.get("address")
        )

        base_data = (
            nested_dict(rels.get("base_token"))
            .get("data") or {}
        )

        quote_data = (
            nested_dict(rels.get("quote_token"))
            .get("data") or {}
        )

        base_id = (
            base_data.get("id")
            if isinstance(base_data, dict)
            else None
        )

        quote_id = (
            quote_data.get("id")
            if isinstance(quote_data, dict)
            else None
        )

        base_mint = (
            base_id.split("_", 1)[1]
            if isinstance(base_id, str) and "_" in base_id
            else None
        )

        quote_mint = (
            quote_id.split("_", 1)[1]
            if isinstance(quote_id, str) and "_" in quote_id
            else None
        )

        if mint == base_mint:
            side = "base"
        elif mint == quote_mint:
            side = "quote"
        else:
            continue

        liquidity = number(
            attrs.get("reserve_in_usd")
        ) or 0

        if pool_address and liquidity > best_liquidity:
            best = pool_address
            best_side = side
            best_liquidity = liquidity

    return best, best_side


def coingecko_ohlcv(pool, timeframe, aggregate, limit, side):
    headers = {
        "accept": "application/json",
    }

    if COINGECKO_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_KEY

    payload = get_json(
        f"{COINGECKO_ONCHAIN}/networks/solana/pools/{pool}/ohlcv/{timeframe}",
        {
            "aggregate": aggregate,
            "limit": limit,
            "currency": "usd",
            "token": side,
        },
        headers=headers,
    )

    return extract_ohlcv(payload)


def moralis_pairs(mint):
    if not MORALIS_KEY:
        return []

    payload = get_json(
        f"{MORALIS}/token/mainnet/{mint}/pairs",
        headers={
            "accept": "application/json",
            "X-API-Key": MORALIS_KEY,
        },
    )

    pairs = []

    for row in rows_from(payload):
        address = first(
            row.get("pairAddress"),
            row.get("pair_address"),
            row.get("address"),
        )

        if address:
            liquidity = number(first(
                row.get("liquidityUsd"),
                row.get("liquidity_usd"),
                row.get("liquidity"),
            )) or 0

            pairs.append((address, liquidity))

    pairs.sort(
        key=lambda x: x[1],
        reverse=True,
    )

    return [x[0] for x in pairs[:3]]


def moralis_ohlcv(mint, timeframe, limit):
    if not MORALIS_KEY:
        return []

    tf = "4h" if timeframe == "hour" else "1d"

    headers = {
        "accept": "application/json",
        "X-API-Key": MORALIS_KEY,
    }

    for pair in moralis_pairs(mint):
        payload = get_json(
            f"{MORALIS}/token/mainnet/pairs/{pair}/ohlcv",
            {
                "timeframe": tf,
                "currency": "usd",
                "limit": limit,
            },
            headers=headers,
        )

        rows = extract_ohlcv(payload)

        if rows:
            return rows

    return []


def get_candles(mint, timeframe="hour", aggregate=4, limit=100):
    pool, side = gecko_best_pool(mint)

    attempts = []

    if pool and side:
        attempts.append((
            "GeckoTerminal",
            lambda: get_json(
                f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{timeframe}",
                {
                    "aggregate": aggregate,
                    "limit": limit,
                    "currency": "usd",
                    "token": side,
                },
                cache_seconds=15,
            ),
        ))

        if COINGECKO_KEY:
            attempts.append((
                "CoinGecko",
                lambda: {
                    "result": coingecko_ohlcv(
                        pool,
                        timeframe,
                        aggregate,
                        limit,
                        side,
                    )
                },
            ))

    if MORALIS_KEY:
        attempts.append((
            "Moralis",
            lambda: {
                "result": moralis_ohlcv(
                    mint,
                    timeframe,
                    limit,
                )
            },
        ))

    for provider, fetch in attempts:
        try:
            payload = fetch()

            candles = normalize_candles(
                extract_ohlcv(payload)
            )

            if len(candles) >= 2:
                logging.info(
                    "Candles %s %s via %s: %d",
                    mint,
                    timeframe,
                    provider,
                    len(candles),
                )

                return candles

        except Exception:
            logging.exception(
                "Candle provider failed: %s",
                provider,
            )

    return []


def get_4h(mint):
    return get_candles(
        mint,
        timeframe="hour",
        aggregate=4,
        limit=CANDLE_4H_LIMIT,
    )


def get_1d(mint):
    return get_candles(
        mint,
        timeframe="day",
        aggregate=1,
        limit=CANDLE_1D_LIMIT,
    )


# =========================================================
# ATH ENGINE
# =========================================================

def observed_ath(mint, history_start):
    if not history_start:
        return None

    age = (now() - history_start) / 86400

    if age > ATH_MAX_AGE_DAYS or age < 0:
        return None

    candles = get_1d(mint)

    if not candles:
        return None

    if candles[0]["time"] > (
        history_start + ATH_HISTORY_GAP_DAYS * 86400
    ):
        return None

    return max(
        candle["high"]
        for candle in candles
    )


def ath_drop_filter(store, state):
    candidates = [
        item for item in store.values()
        if (
            (item.get("price") or 0) > 0
            and history_start_ts(item)
        )
    ]

    candidates.sort(
        key=lambda x: (
            number(x.get("market_cap"))
            if number(x.get("market_cap")) is not None
            else -1
        ),
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = (
        int(state.get("ath_cursor", 0))
        % len(candidates)
    )

    batch = (
        candidates[cursor:] + candidates[:cursor]
    )[:ATH_CHECKS_PER_RUN]

    state["ath_cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    matches = 0

    for item in batch:
        mint = item["mint"]
        history_start = history_start_ts(item)

        ath = observed_ath(
            mint,
            history_start,
        )

        price = item.get("price") or 0

        if not ath or price <= 0 or price > ath:
            continue

        drop = (ath - price) / ath * 100

        if drop < MIN_ATH_DROP:
            continue

        previous = state["ath_watch"].get(mint, {})

        old_ath = number(previous.get("ath")) or 0
        stored_ath = max(ath, old_ath)

        state["ath_watch"][mint] = {
            "ath": stored_ath,
            "name": item["name"],
            "symbol": item["symbol"],
            "created_ts": item.get("created_ts"),
            "pair_created_ts": item.get("pair_created_ts"),
            "registered_at": previous.get("registered_at") or now(),
            "url": item.get("url") or dex_url(mint),
        }

        market_cap = number(item.get("market_cap"))

        market_cap_text = (
            f"${market_cap:,.0f}"
            if market_cap is not None
            else "Unknown"
        )

        message = (
            "🎯 ATH DROP DETECTED | DEX HUNTER V19\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {item['name']} ({item['symbol']})\n"
            f"💰 Price: ${price:.10g}\n"
            f"🏔 Observed ATH: ${stored_ath:.10g}\n"
            f"📉 Drop: {drop:.2f}%\n"
            f"📊 Market Cap: {market_cap_text}\n"
            f"💧 Liquidity: ${(item.get('liquidity') or 0):,.0f}\n"
            f"🔎 Sources: {', '.join(item.get('sources', []))}\n"
            f"🧾 Contract:\n{mint}\n"
            f"🔗 {item.get('url') or dex_url(mint)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "👁 Token registered for recovery monitoring.\n"
            "ℹ️ ATH is based on available candle history, not guaranteed lifetime ATH.\n"
            "⚠️ Analytical alert only."
        )

        if not all(
            f"{mint}|{chat}" in state["sent"]["ath"]
            for chat in CHAT_IDS
        ):
            if deliver(
                state,
                "ath",
                mint,
                message,
            ):
                matches += 1

        time.sleep(0.4)

    write_json(SEEN_FILE, state)

    return matches

# =========================================================
# LIVE TOKEN DATA
# =========================================================

def live_token(mint):
    payload = get_json(
        f"{DEX}/tokens/v1/solana/{mint}",
        cache_seconds=10,
    )

    if not isinstance(payload, list):
        return None

    valid = []

    for pair in payload:
        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != "solana":
            continue

        base = nested_dict(pair.get("baseToken"))

        if base.get("address") != mint:
            continue

        price = number(pair.get("priceUsd"))

        if not price or price <= 0:
            continue

        liquidity = number(
            nested_dict(pair.get("liquidity")).get("usd")
        ) or 0

        valid.append({
            "price": price,
            "liquidity": liquidity,
            "name": base.get("name") or "Unknown",
            "symbol": base.get("symbol") or "???",
            "market_cap": number(pair.get("marketCap")),
            "url": pair.get("url") or dex_url(mint),
        })

    if not valid:
        return None

    valid.sort(
        key=lambda x: x["liquidity"],
        reverse=True,
    )

    return valid[0]


# =========================================================
# SMART RECOVERY ANALYSIS
# =========================================================

def analyze_recovery(item, candles):
    if len(candles) < 12:
        return None

    price = item.get("price") or candles[-1]["close"]

    if not price or price <= 0:
        return None

    ath = number(item.get("ath"))

    if not ath or ath <= 0:
        ath = max(
            x["high"]
            for x in candles
        )

    drop = max(
        0,
        (ath - price) / ath * 100,
    )

    score = 0
    details = {}

    if 30 <= drop <= 90:
        distance_score = 15
    elif 20 <= drop < 30:
        distance_score = 8
    elif 90 < drop <= 95:
        distance_score = 8
    else:
        distance_score = 0

    score += distance_score
    details["ath_distance"] = distance_score

    recent = candles[-6:]
    previous = candles[-12:-6]

    recent_low = min(
        x["low"] for x in recent
    )

    previous_low = min(
        x["low"] for x in previous
    )

    no_new_low = (
        recent_low >= previous_low * 0.985
    )

    rising_lows = (
        candles[-1]["low"] > candles[-4]["low"]
    )

    recent_mid = (
        max(x["high"] for x in recent)
        + min(x["low"] for x in recent)
    ) / 2

    close_above_recent_mid = (
        candles[-1]["close"] >= recent_mid
    )

    bottom_score = 0

    if no_new_low:
        bottom_score += 10

    if rising_lows:
        bottom_score += 5

    if close_above_recent_mid:
        bottom_score += 5

    score += bottom_score
    details["bottom"] = bottom_score

    last = candles[-1]
    old = candles[-7:-1]

    old_high = max(
        x["high"] for x in old
    )

    old_low = min(
        x["low"] for x in old
    )

    structure_score = 0

    if last["close"] > old_high:
        structure_score += 12

    if last["low"] > old_low:
        structure_score += 4

    if last["close"] > last["open"]:
        structure_score += 4

    score += structure_score
    details["structure"] = structure_score

    resistance = max(
        x["high"]
        for x in candles[-13:-1]
    )

    breakout_score = 0

    if last["close"] > resistance:
        breakout_score = 15
    elif last["high"] > resistance:
        breakout_score = 7

    score += breakout_score
    details["breakout"] = breakout_score

    liquidity = number(item.get("liquidity"))

    liquidity_score = 0

    if liquidity is not None:
        if liquidity >= 100_000:
            liquidity_score = 10
        elif liquidity >= 50_000:
            liquidity_score = 8
        elif liquidity >= 20_000:
            liquidity_score = 6
        elif liquidity >= 5_000:
            liquidity_score = 3

    score += liquidity_score
    details["liquidity"] = liquidity_score

    if last["close"] > resistance:
        phase = "CONFIRMED REVERSAL"
    elif structure_score >= 12:
        phase = "RECOVERY"
    elif bottom_score >= 10:
        phase = "BOTTOM FORMING"
    else:
        phase = "FALLING / NO CONFIRMATION"

    if phase == "CONFIRMED REVERSAL":
        stage = "STRONG BUY"
    elif phase == "RECOVERY":
        stage = "RECOVERY"
    else:
        stage = "WATCH"

    return {
        "score": score,
        "max_score": 80,
        "stage": stage,
        "phase": phase,
        "drop": drop,
        "price": price,
        "ath": ath,
        "resistance": resistance,
        "details": details,
        "liquidity": liquidity,
        "coverage": "5/5 technical categories",
    }


# =========================================================
# RECOVERY MESSAGE
# =========================================================

def recovery_message(item, result):
    details = result["details"]

    if result["stage"] == "STRONG BUY":
        heading = "🟢🚨 STRONG BUY SIGNAL 🚨🟢"
        urgency = (
            "قیمت مقاومت اخیر را با کندل بسته‌شده شکسته است."
        )

    elif result["stage"] == "RECOVERY":
        heading = "🟢 RECOVERY SIGNAL"
        urgency = (
            "نشانه‌های تغییر ساختار و شروع بازگشت مشاهده شده است."
        )

    else:
        heading = "👁 WATCH ALERT"
        urgency = (
            "ارز شناسایی شده؛ هنوز بازگشت آن تأیید نشده است."
        )

    liquidity_text = (
        f"${result['liquidity']:,.0f}"
        if result["liquidity"] is not None
        else "نامشخص"
    )

    market_cap = number(item.get("market_cap"))

    market_cap_text = (
        f"${market_cap:,.0f}"
        if market_cap is not None
        else "نامشخص"
    )

    return (
        f"{heading}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 {item.get('name', 'Unknown')} "
        f"({item.get('symbol', '???')})\n"
        f"📍 Phase: {result['phase']}\n"
        f"📊 Score: {result['score']}/80\n"
        f"💰 Price: ${result['price']:.10g}\n"
        f"🏔 Observed ATH: ${result['ath']:.10g}\n"
        f"📉 Drop from ATH: {result['drop']:.2f}%\n"
        f"📊 Market Cap: {market_cap_text}\n"
        f"💧 Liquidity: {liquidity_text}\n"
        f"🧱 Resistance: ${result['resistance']:.10g}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 TECHNICAL SCORE\n"
        f"ATH distance: {details['ath_distance']}/15\n"
        f"Bottom formation: {details['bottom']}/20\n"
        f"Structure: {details['structure']}/20\n"
        f"Breakout: {details['breakout']}/15\n"
        f"Liquidity: {details['liquidity']}/10\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 وضعیت: {urgency}\n"
        f"🔎 Sources: {', '.join(item.get('sources', []))}\n"
        f"🧾 Contract:\n{item['mint']}\n"
        f"🔗 Chart: {item.get('url') or dex_url(item['mint'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ هیچ حداقل امتیازی برای معرفی ارز وجود ندارد.\n"
        "⚠️ Market Cap نامشخص مانع بررسی نیست.\n"
        "⚠️ امنیت قرارداد و تعداد هولدرها بررسی نشده‌اند.\n"
        "⚠️ این هشدار توصیه قطعی خرید نیست."
    )


# =========================================================
# SIGNAL PERFORMANCE TRACKING
# =========================================================

def record_signal(performance, mint, item, result):
    signals = performance["signals"]

    for signal in signals:
        if (
            signal.get("mint") == mint
            and signal.get("stage") == result["stage"]
            and signal.get("day") == utc_day()
        ):
            return

    signals.append({
        "mint": mint,
        "name": item.get("name"),
        "symbol": item.get("symbol"),
        "stage": result["stage"],
        "score": result["score"],
        "entry_price": result["price"],
        "entry_time": now(),
        "day": utc_day(),
        "max_price": result["price"],
        "min_price": result["price"],
        "snapshots": {},
    })

    performance["signals"] = signals[-2000:]


def update_performance(performance):
    for signal in performance["signals"]:
        mint = signal.get("mint")

        if not mint:
            continue

        entry = number(signal.get("entry_price"))

        if not entry or entry <= 0:
            continue

        elapsed = (
            now() - signal.get("entry_time", now())
        ) / 3600

        due = [
            (label, hours)
            for label, hours in (
                ("1h", 1),
                ("6h", 6),
                ("24h", 24),
                ("7d", 168),
            )
            if (
                elapsed >= hours
                and label not in signal.get("snapshots", {})
            )
        ]

        if not due:
            continue

        live = live_token(mint)

        if not live:
            continue

        price = live["price"]

        signal["max_price"] = max(
            number(signal.get("max_price")) or price,
            price,
        )

        signal["min_price"] = min(
            number(signal.get("min_price")) or price,
            price,
        )

        for label, hours in due:
            signal.setdefault("snapshots", {})

            signal["snapshots"][label] = {
                "price": price,
                "change_percent": (
                    (price / entry - 1) * 100
                ),
                "recorded_at": now(),
            }


# =========================================================
# RECOVERY SCANNER
# =========================================================

def recovery_scanner(
    store,
    state,
    recovery,
    performance,
):
    candidates = []
    existing = set()

    # Previously identified ATH DROP tokens.

    for mint, info in state["ath_watch"].items():
        item = store.get(mint)

        if not item:
            item = {
                "mint": mint,
                "name": info.get("name", "Unknown"),
                "symbol": info.get("symbol", "???"),
                "price": None,
                "market_cap": None,
                "liquidity": None,
                "url": info.get("url") or dex_url(mint),
                "ath": info.get("ath"),
                "sources": [],
                "created_ts": info.get("created_ts"),
                "pair_created_ts": info.get("pair_created_ts"),
            }

            live = live_token(mint)

            if live:
                item.update(live)

        else:
            item["ath"] = info.get("ath")

        if item.get("price"):
            candidates.append(item)
            existing.add(mint)

    # New candidates: no market-cap or volume gate.

    for item in store.values():
        if (
            item["mint"] not in existing
            and (item.get("price") or 0) > 0
        ):
            candidates.append(item)
            existing.add(item["mint"])

    if not candidates:
        return 0

    cursor = (
        int(recovery.get("cursor", 0))
        % len(candidates)
    )

    batch = (
        candidates[cursor:] + candidates[:cursor]
    )[:RECOVERY_CHECKS_PER_RUN]

    recovery["cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    alerts = 0

    for item in batch:
        mint = item["mint"]

        candles = get_4h(mint)

        if len(candles) < 12:
            continue

        result = analyze_recovery(
            item,
            candles,
        )

        if not result:
            continue

        entry = recovery["tokens"].setdefault(
            mint,
            {
                "first_seen": now(),
                "last_stage": "FALLING",
                "last_alert": {},
                "highest_stage": None,
            },
        )

        entry["last_score"] = result["score"]
        entry["last_phase"] = result["phase"]
        entry["last_price"] = result["price"]
        entry["last_checked"] = now()

        stage = result["stage"]

        if stage == "WATCH":
            category = "watch"
            cooldown = WATCH_COOLDOWN

        elif stage == "RECOVERY":
            category = "recovery"
            cooldown = RECOVERY_COOLDOWN

        else:
            category = "strong"
            cooldown = STRONG_COOLDOWN

        previous = entry["last_alert"].get(
            category,
            0,
        )

        if now() - previous < cooldown:
            entry["last_stage"] = stage
            continue

        rank = {
            "WATCH": 1,
            "RECOVERY": 2,
            "STRONG BUY": 3,
        }

        previous_stage = entry.get("highest_stage")

        if (
            previous_stage
            and rank.get(stage, 0)
            < rank.get(previous_stage, 0)
        ):
            entry["last_stage"] = stage
            continue

        message = recovery_message(
            item,
            result,
        )

        delivered = deliver(
            state,
            category,
            mint,
            message,
        )

        if delivered:
            entry["last_alert"][category] = now()
            entry["highest_stage"] = stage

            record_signal(
                performance,
                mint,
                item,
                result,
            )

            alerts += 1

            logging.info(
                "RECOVERY ALERT %s | %s | score=%s",
                mint,
                stage,
                result["score"],
            )

        entry["last_stage"] = stage

        write_json(RECOVERY_FILE, recovery)
        write_json(PERFORMANCE_FILE, performance)
        write_json(SEEN_FILE, state)

        time.sleep(0.3)

    return alerts


# =========================================================
# ATH RECLAIM ALERT
# =========================================================

def ath_reclaim_scanner(state):
    alerts = 0

    for mint, info in list(state["ath_watch"].items()):
        ath = number(info.get("ath"))

        if not ath or ath <= 0:
            continue

        if all(
            f"{mint}|{chat}" in state["sent"]["ath_reclaim"]
            for chat in CHAT_IDS
        ):
            continue

        live = live_token(mint)

        if not live:
            continue

        price = live["price"]

        if price < ath * 0.995:
            continue

        distance = (
            (price / ath - 1) * 100
        )

        message = (
            "🟢🚨 ATH RECLAIM ALERT 🚨🟢\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {live['name']} ({live['symbol']})\n"
            f"🏔 Stored ATH: ${ath:.10g}\n"
            f"💰 Current price: ${price:.10g}\n"
            f"📊 Distance from ATH: {distance:+.2f}%\n"
            "🟢 Price returned to the ATH zone.\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🧾 Contract:\n{mint}\n"
            f"🔗 {live['url']}\n"
            "⚠️ This does not guarantee a sustained breakout."
        )

        if deliver(
            state,
            "ath_reclaim",
            mint,
            message,
        ):
            alerts += 1

    return alerts


# =========================================================
# ATH BREAKOUT AFTER 75% DROP
# =========================================================

def find_breakout(candles, history_start):
    if len(candles) < 10:
        return None

    if (
        history_start
        and candles[0]["time"] > (
            history_start + ATH_HISTORY_GAP_DAYS * 86400
        )
    ):
        return None

    peak = 0
    peak_time = None

    armed = False
    reference_ath = None
    drawdown_low = None
    drawdown_time = None

    for candle in candles:
        if not armed:
            if (
                peak > 0
                and peak_time is not None
                and candle["time"] > peak_time
                and candle["low"] <= peak * (
                    1 - BREAKOUT_DROP_PERCENT / 100
                )
            ):
                armed = True
                reference_ath = peak
                drawdown_low = candle["low"]
                drawdown_time = candle["time"]
                continue

            if candle["high"] > peak:
                peak = candle["high"]
                peak_time = candle["time"]

        else:
            drawdown_low = min(
                drawdown_low,
                candle["low"],
            )

    if not armed:
        return None

    latest = candles[-1]

    if (
        latest["time"] <= drawdown_time
        or latest["close"] <= reference_ath
    ):
        return None

    return {
        "ath": reference_ath,
        "low": drawdown_low,
        "breakout": latest["close"],
        "time": latest["time"],
    }


def breakout_scanner(store, state):
    candidates = [
        item for item in store.values()
        if (
            history_start_ts(item)
            and item.get("price")
        )
    ]

    if not candidates:
        return 0

    cursor = (
        int(state.get("breakout_cursor", 0))
        % len(candidates)
    )

    batch = (
        candidates[cursor:] + candidates[:cursor]
    )[:BREAKOUT_CHECKS_PER_RUN]

    state["breakout_cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    alerts = 0

    for item in batch:
        mint = item["mint"]

        if all(
            f"{mint}|{chat}" in state["sent"]["breakout"]
            for chat in CHAT_IDS
        ):
            continue

        history_start = history_start_ts(item)

        age = (
            now() - history_start
        ) / 86400

        if age <= 0 or age > BREAKOUT_MAX_AGE_DAYS:
            continue

        candles = get_1d(mint)

        result = find_breakout(
            candles,
            history_start,
        )

        if not result:
            continue

        if item["price"] <= result["ath"]:
            continue

        message = (
            "🚀 ATH BREAKOUT AFTER 75% DROP\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {item['name']} ({item['symbol']})\n"
            f"🏔 Previous observed ATH: ${result['ath']:.10g}\n"
            f"📉 Drawdown low: ${result['low']:.10g}\n"
            f"💥 Breakout close: ${result['breakout']:.10g}\n"
            f"💰 Current price: ${item['price']:.10g}\n"
            f"🧾 Contract:\n{mint}\n"
            f"🔗 {item.get('url') or dex_url(mint)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Confirmation requires a daily candle close above ATH.\n"
            "ATH is based on available candle history."
        )

        if deliver(
            state,
            "breakout",
            mint,
            message,
        ):
            alerts += 1

        time.sleep(0.3)

    return alerts


# =========================================================
# TELEGRAM FORWARDED POSTS
# =========================================================

def get_updates(offset):
    if not BOT_TOKEN:
        return []

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/getUpdates"
    )

    try:
        response = session.get(
            url,
            params={
                "offset": offset,
                "limit": TELEGRAM_UPDATES_LIMIT,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=HTTP_TIMEOUT,
        )

        if not response.ok:
            logging.warning(
                "Telegram getUpdates HTTP error | status=%s | body=%s",
                response.status_code,
                response.text[:500],
            )
            return []

        payload = response.json()

        if payload.get("ok"):
            return payload.get("result", [])

        logging.warning(
            "Telegram getUpdates rejected: %s",
            payload.get("description"),
        )

    except Exception as exc:
        logging.warning(
            "Telegram updates failed: %s",
            exc,
        )

    return []


def is_forwarded(message):
    return bool(
        message.get("forward_origin")
        or message.get("forward_from")
        or message.get("forward_from_chat")
        or message.get("forward_sender_name")
    )


def extract_mint(message):
    content = (
        message.get("text")
        or message.get("caption")
        or ""
    )

    for mint in ADDRESS_PATTERN.findall(content):
        if not valid_mint(mint):
            continue

        if live_token(mint):
            return mint

    return None


def chart_candles(mint, timeframe):
    if timeframe == "4h":
        candles = get_4h(mint)
    else:
        candles = get_1d(mint)

    if len(candles) < 2:
        return []

    return candles


def create_chart(mint, timeframe, symbol):
    candles = chart_candles(
        mint,
        timeframe,
    )

    if not candles:
        return None

    CHART_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    fig, ax = plt.subplots(
        figsize=(13, 7),
        dpi=150,
    )

    dates = [
        mdates.date2num(
            datetime.fromtimestamp(
                candle["time"],
                timezone.utc,
            )
        )
        for candle in candles
    ]

    if timeframe == "4h":
        width = 0.11
        label = "4H"
        date_format = "%m-%d %H:%M"
    else:
        width = 0.65
        label = "1D"
        date_format = "%Y-%m-%d"

    for x, candle in zip(dates, candles):
        op = candle["open"]
        hi = candle["high"]
        lo = candle["low"]
        cl = candle["close"]

        color = (
            "#16a34a"
            if cl >= op
            else "#dc2626"
        )

        ax.plot(
            [x, x],
            [lo, hi],
            color=color,
            linewidth=1.1,
        )

        bottom = min(op, cl)
        height = abs(cl - op)

        if height == 0:
            height = max(
                op * 0.00001,
                1e-12,
            )

        ax.add_patch(
            Rectangle(
                (x - width / 2, bottom),
                width,
                height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.7,
            )
        )

    ax.set_title(
        f"{symbol} / {label}",
        fontsize=16,
        fontweight="bold",
    )

    ax.set_ylabel("Price (USD)")
    ax.xaxis_date()

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter(
            date_format,
            tz=timezone.utc,
        )
    )

    ax.grid(
        True,
        alpha=0.18,
    )

    ax.tick_params(
        axis="x",
        rotation=25,
    )

    ax.margins(
        x=0.02,
        y=0.08,
    )

    fig.tight_layout()

    safe = re.sub(
        r"[^A-Za-z0-9_-]",
        "_",
        mint,
    )

    filename = (
        CHART_DIR
        / f"{safe}_{timeframe}.png"
    )

    try:
        fig.savefig(
            filename,
            bbox_inches="tight",
        )

    except Exception:
        logging.exception(
            "Chart creation failed"
        )
        plt.close(fig)
        return None

    plt.close(fig)

    return filename


def process_forwarded(state):
    offset = int(
        state.get("telegram_update_offset", 0)
    )

    updates = get_updates(offset)

    allowed = set(CHAT_IDS)
    processed = 0

    for update in updates:
        update_id = update.get("update_id")

        if update_id is None:
            continue

        message = update.get("message") or {}
        chat = message.get("chat") or {}

        chat_id = str(
            chat.get("id", "")
        )

        if (
            chat.get("type") != "private"
            or chat_id not in allowed
        ):
            state["telegram_update_offset"] = update_id + 1
            write_json(SEEN_FILE, state)
            continue

        if not is_forwarded(message):
            state["telegram_update_offset"] = update_id + 1
            write_json(SEEN_FILE, state)
            continue

        mint = extract_mint(message)

        if not mint:
            send_message(
                chat_id,
                "❌ آدرس معتبر توکن سولانا پیدا نشد.",
            )

        else:
            live = live_token(mint)

            if not live:
                send_message(
                    chat_id,
                    "❌ اطلاعات توکن در DexScreener پیدا نشد.",
                )

            else:
                symbol = live.get("symbol") or "TOKEN"

                chart4 = create_chart(
                    mint,
                    "4h",
                    symbol,
                )

                chart1 = create_chart(
                    mint,
                    "1d",
                    symbol,
                )

                if not chart4 or not chart1:
                    send_message(
                        chat_id,
                        "❌ دریافت کندل‌های 4H یا 1D ناموفق بود.",
                    )

                else:
                    send_photo(chat_id, chart4)
                    send_photo(chat_id, chart1)

        state["telegram_update_offset"] = update_id + 1
        write_json(SEEN_FILE, state)

        processed += 1
        time.sleep(0.3)

    logging.info(
        "Forwarded messages processed: %d",
        processed,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN GitHub Secret"
        )

    state = load_seen()
    recovery = load_recovery()
    performance = load_performance()

    daily_reset(
        state,
        recovery,
    )

    logging.info(
        "%s scan started | UTC day=%s",
        VERSION,
        utc_day(),
    )

    # 1. Telegram forwarded charts
    process_forwarded(state)

    # 2. Discovery sources
    store = {}

    source_dexscreener(store)
    source_gecko(store)
    source_pump(store)
    source_stonk(store)
    source_void(store)

    # 3. DEXTools hourly discovery
    dextools_count = source_dextools(
        store,
        state,
    )

    logging.info(
        "Discovered unique tokens: %d | DEXTools new tokens: %d",
        len(store),
        dextools_count,
    )

    # 4. Enrich candidates
    enrich(store)

    # 5. ATH DROP
    ath_count = ath_drop_filter(
        store,
        state,
    )

    # 6. SMART RECOVERY
    recovery_count = recovery_scanner(
        store,
        state,
        recovery,
        performance,
    )

    # 7. ATH BREAKOUT
    breakout_count = breakout_scanner(
        store,
        state,
    )

    # 8. ATH RECLAIM
    reclaim_count = ath_reclaim_scanner(state)

    # 9. Performance
    update_performance(performance)

    # 10. Save databases
    write_json(
        SEEN_FILE,
        state,
    )

    write_json(
        RECOVERY_FILE,
        recovery,
    )

    write_json(
        PERFORMANCE_FILE,
        performance,
    )

    logging.info(
        "%s COMPLETE | tokens=%d | DEXTools=%d | "
        "ATH=%d | RECOVERY=%d | BREAKOUT=%d | "
        "RECLAIM=%d | tracked=%d | performance=%d",
        VERSION,
        len(store),
        dextools_count,
        ath_count,
        recovery_count,
        breakout_count,
        reclaim_count,
        len(recovery["tokens"]),
        len(performance["signals"]),
    )


if __name__ == "__main__":
    main()
