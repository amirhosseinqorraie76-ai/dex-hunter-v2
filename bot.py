
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V17 - SMART RECOVERY

Sources:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun
5. VoidScreener

Preserved:
- ATH DROP
- ATH BREAKOUT after 75% drawdown
- ATH RECLAIM BUY
- Telegram forwarded-post charts (4H + 1D)
- Multi-source candle fallback
- No volume filter
- No automatic trading

New:
- Smart Recovery Engine
- FALLING / BOTTOM FORMING / RECOVERY / CONFIRMED REVERSAL
- Transparent technical score
- WATCH / RECOVERY / STRONG BUY alerts
- Persistent recovery.json
- Persistent performance.json
- 1h / 6h / 24h / 7d signal tracking

Alerts only. No automatic trading.
"""

import json
import logging
import os
import re
import time

from datetime import datetime, timezone
from pathlib import Path

import requests
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle


# =========================================================
# CONFIG
# =========================================================

VERSION = "V17 SMART RECOVERY"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
COINGECKO_DEMO_API_KEY = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY", "").strip()

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

COINGECKO_ONCHAIN = "https://api.coingecko.com/api/v3/onchain"
MORALIS_SOLANA = "https://solana-gateway.moralis.io"

STATE_FILE = Path("seen.json")
RECOVERY_FILE = Path("recovery.json")
PERFORMANCE_FILE = Path("performance.json")

CHART_OUTPUT_DIR = Path("charts")

HTTP_TIMEOUT = 20
TELEGRAM_UPDATES_LIMIT = 30

MAX_SOURCE_ROWS = 100

# Original ATH filters
ATH_MIN_MARKET_CAP_USD = 50_000
ATH_MIN_DROP_PERCENT = 30.0

BREAKOUT_DROP_PERCENT = 75.0
BREAKOUT_HISTORY_LIMIT = 1000
BREAKOUT_MAX_AGE_DAYS = 990

BUY_ATH_THRESHOLD_PERCENT = 99.5
BUY_CHECKS_PER_RUN = 30
BUY_ALERT_ENABLED = True

MAX_ATH_CHECKS_PER_RUN = 5
MAX_BREAKOUT_CHECKS_PER_RUN = 5

# Smart Recovery
RECOVERY_ENABLED = True

RECOVERY_MIN_MARKET_CAP_USD = 50_000
RECOVERY_MIN_LIQUIDITY_USD = 10_000

RECOVERY_CHECKS_PER_RUN = 12
RECOVERY_CANDLE_LIMIT = 80

WATCH_SCORE = 50
RECOVERY_SCORE = 65
STRONG_BUY_SCORE = 80

# Minimum number of candles required for technical analysis
RECOVERY_MIN_CANDLES = 18

# Avoid repeated alerts for the same signal stage
RECOVERY_ALERT_COOLDOWN_HOURS = 24

# Performance evaluation
PERFORMANCE_WINDOWS = {
    "1h": 3600,
    "6h": 21600,
    "24h": 86400,
    "7d": 604800,
}

CHART_4H_LIMIT = 100
CHART_1D_LIMIT = 100

SOLANA_ADDRESS_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)

HEADERS = {
    "User-Agent": "DEX-HUNTER-V17/1.0",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update(HEADERS)


# =========================================================
# HTTP AND GENERAL HELPERS
# =========================================================

def get_json(url, params=None, headers=None):

    try:
        response = session.get(
            url,
            params=params,
            headers=headers,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 429:
            logging.warning("Rate limited: %s", url)
            return None

        response.raise_for_status()
        return response.json()

    except Exception as exc:
        logging.warning("Request failed %s: %s", url, exc)
        return None


def as_float(value):

    try:
        if value is None or value == "":
            return None

        result = float(value)

        if result != result:
            return None

        if result in (float("inf"), float("-inf")):
            return None

        return result

    except (TypeError, ValueError):
        return None


def first(*values):

    for value in values:
        if value is not None and value != "":
            return value

    return None


def parse_timestamp(value):

    if value is None or value == "":
        return None

    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed.timestamp()

        except ValueError:
            pass

    result = as_float(value)

    if result is None or result <= 0:
        return None

    if result > 10_000_000_000:
        result /= 1000

    return result


def rows_from(payload):

    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    for key in ("tokens", "coins", "items", "results", "pools", "data"):

        value = payload.get(key)

        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            for nested in ("tokens", "coins", "items", "results", "pools"):
                inner = value.get(nested)

                if isinstance(inner, list):
                    return [x for x in inner if isinstance(x, dict)]

    return []


def mint_looks_valid(mint):

    return (
        isinstance(mint, str)
        and 32 <= len(mint) <= 44
    )


def dex_url(mint):
    return f"https://dexscreener.com/solana/{mint}"


def safe_float(value, default=0.0):
    result = as_float(value)
    return result if result is not None else default


# =========================================================
# DATABASE HELPERS
# =========================================================

def empty_state():

    return {
        "sent": {
            "ath": [],
            "volume": [],
            "ath_breakout": [],
            "ath_reclaim": [],
        },
        "ath_watch": {},
        "ath_cursor": 0,
        "breakout_cursor": 0,
        "buy_cursor": 0,
        "telegram_update_offset": 0,
    }


def load_json_file(path, default):

    if not path.exists():
        return default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(data, dict):
            return data

        return default

    except Exception:
        logging.exception("Could not load %s", path)
        return default


def save_json_file(path, data):

    temp = path.with_suffix(".tmp")

    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp.replace(path)


def load_state():

    state = load_json_file(STATE_FILE, empty_state())
    default = empty_state()

    state.setdefault("sent", {})

    for key in default["sent"]:
        state["sent"].setdefault(key, [])

    state.setdefault("ath_watch", {})
    state.setdefault("ath_cursor", 0)
    state.setdefault("breakout_cursor", 0)
    state.setdefault("buy_cursor", 0)
    state.setdefault("telegram_update_offset", 0)

    return state


def save_state(state):
    save_json_file(STATE_FILE, state)


def load_recovery():

    data = load_json_file(
        RECOVERY_FILE,
        {"tokens": {}, "cursor": 0},
    )

    data.setdefault("tokens", {})
    data.setdefault("cursor", 0)

    return data


def save_recovery(data):
    save_json_file(RECOVERY_FILE, data)


def load_performance():

    data = load_json_file(
        PERFORMANCE_FILE,
        {"signals": []},
    )

    data.setdefault("signals", [])

    return data


def save_performance(data):
    save_json_file(PERFORMANCE_FILE, data)


# =========================================================
# TELEGRAM OUTGOING
# =========================================================

def send_telegram(chat_id, message):

    if not BOT_TOKEN:
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:
        response = session.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()
        return bool(response.json().get("ok"))

    except Exception as exc:
        logging.warning("Telegram failed %s: %s", chat_id, exc)
        return False


def send_telegram_photo(chat_id, photo_path):

    if not BOT_TOKEN:
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendPhoto"
    )

    try:
        with open(photo_path, "rb") as photo:
            response = session.post(
                url,
                data={"chat_id": chat_id},
                files={"photo": photo},
                timeout=HTTP_TIMEOUT,
            )

        response.raise_for_status()
        return bool(response.json().get("ok"))

    except Exception as exc:
        logging.warning("Telegram photo failed %s: %s", chat_id, exc)
        return False


def deliver(state, scanner, mint, message):

    state["sent"].setdefault(scanner, [])

    sent = set(state["sent"][scanner])

    for chat_id in CHAT_IDS:

        key = f"{mint}|{chat_id}"

        if key in sent:
            continue

        if send_telegram(chat_id, message):
            sent.add(key)
            state["sent"][scanner] = sorted(sent)
            save_state(state)

        else:
            logging.warning("Telegram retry required: %s", key)

        time.sleep(0.12)


# =========================================================
# TOKEN NORMALIZATION
# =========================================================

def normalize(row, source):

    attrs = (
        row.get("attributes")
        if isinstance(row.get("attributes"), dict)
        else {}
    )

    rels = row.get("relationships") or {}
    base_rel = rels.get("base_token") or {}
    base_data = base_rel.get("data") or {}
    base_id = base_data.get("id")
    base = row.get("baseToken") or {}

    mint = first(
        row.get("mint"),
        row.get("address"),
        row.get("tokenAddress"),
        row.get("token_address"),
        row.get("contractAddress"),
        row.get("baseTokenAddress"),
        attrs.get("address"),
        attrs.get("mint"),
        (
            base_id.split("_", 1)[1]
            if isinstance(base_id, str) and "_" in base_id
            else None
        ),
    )

    if not mint_looks_valid(mint):
        return None

    created = first(
        row.get("created_timestamp"),
        row.get("createdAt"),
        row.get("created_at"),
        row.get("createdAtUnix"),
        row.get("creation_timestamp"),
        row.get("launch_timestamp"),
        row.get("timestamp"),
        attrs.get("created_timestamp"),
        attrs.get("created_at"),
        attrs.get("pool_created_at"),
        attrs.get("creation_timestamp"),
    )

    return {
        "mint": mint,
        "name": str(first(
            row.get("name"),
            row.get("tokenName"),
            base.get("name"),
            attrs.get("name"),
            "Unknown",
        )),
        "symbol": str(first(
            row.get("symbol"),
            row.get("ticker"),
            base.get("symbol"),
            attrs.get("symbol"),
            "???",
        )),
        "sources": {source},
        "price": as_float(first(
            row.get("priceUsd"),
            row.get("price_usd"),
            attrs.get("base_token_price_usd"),
        )),
        "market_cap": as_float(first(
            row.get("marketCap"),
            row.get("market_cap"),
            row.get("usd_market_cap"),
            row.get("marketCapUsd"),
            attrs.get("market_cap_usd"),
        )),
        "created_ts": parse_timestamp(created),
        "pair_created_ts": parse_timestamp(first(
            row.get("pairCreatedAt"),
            row.get("pair_created_at"),
            attrs.get("pair_created_at"),
            attrs.get("pool_created_at"),
        )),
        "dex_url": first(row.get("url"), attrs.get("url")),
        "liquidity": 0.0,
    }


def add_candidate(store, candidate):

    if not candidate:
        return

    mint = candidate["mint"]

    if mint not in store:
        store[mint] = candidate
        return

    old = store[mint]
    old["sources"].update(candidate["sources"])

    for field in (
        "name",
        "symbol",
        "price",
        "market_cap",
        "created_ts",
        "pair_created_ts",
        "dex_url",
    ):

        if (
            old.get(field) is None
            or old.get(field) in ("Unknown", "???")
        ) and candidate.get(field) is not None:

            old[field] = candidate[field]


# =========================================================
# SOURCE 1: DEXSCREENER
# =========================================================

def source_dexscreener(store):

    endpoints = [
        f"{DEX}/token-profiles/latest/v1",
        f"{DEX}/token-boosts/latest/v1",
        f"{DEX}/token-boosts/top/v1",
    ]

    for url in endpoints:

        payload = get_json(url)

        source_rows = (
            payload if isinstance(payload, list)
            else rows_from(payload)
        )

        for row in source_rows[:MAX_SOURCE_ROWS]:

            if row.get("chainId") not in (None, "solana"):
                continue

            mint = row.get("tokenAddress")

            if not mint_looks_valid(mint):
                continue

            add_candidate(
                store,
                normalize(
                    {
                        "mint": mint,
                        "name": row.get("description") or "Unknown",
                        "symbol": "???",
                        "url": row.get("url"),
                    },
                    "DexScreener",
                ),
            )


# =========================================================
# SOURCE 2: GECKOTERMINAL
# =========================================================

def source_geckoterminal(store):

    for endpoint in ("new_pools", "trending_pools"):

        payload = get_json(
            f"{GECKO}/networks/solana/{endpoint}",
            {"page": 1},
        )

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:

            rels = row.get("relationships") or {}
            base_data = (
                (rels.get("base_token") or {}).get("data") or {}
            )

            base_id = base_data.get("id")

            mint = (
                base_id.split("_", 1)[1]
                if isinstance(base_id, str) and "_" in base_id
                else None
            )

            if not mint_looks_valid(mint):
                continue

            attrs = row.get("attributes") or {}

            add_candidate(
                store,
                normalize(
                    {
                        "mint": mint,
                        "name": attrs.get("name"),
                        "price_usd": attrs.get("base_token_price_usd"),
                        "created_at": attrs.get("pool_created_at"),
                        "url": attrs.get("url"),
                    },
                    "GeckoTerminal",
                ),
            )


# =========================================================
# SOURCE 3: PUMP.FUN
# =========================================================

def source_pump_fun(store):

    payload = get_json(
        f"{PUMP}/coins/latest",
        {
            "offset": 0,
            "limit": MAX_SOURCE_ROWS,
            "includeNsfw": "false",
        },
    )

    for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
        add_candidate(store, normalize(row, "Pump.fun"))


# =========================================================
# SOURCE 4: STONK.FUN
# =========================================================

def source_stonk_fun(store):

    payload = get_json(
        f"{STONK}/tokens",
        {
            "sort": "newest",
            "page": 1,
            "pageSize": MAX_SOURCE_ROWS,
        },
    )

    for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
        add_candidate(store, normalize(row, "Stonk.fun"))


# =========================================================
# SOURCE 5: VOIDScreener
# =========================================================

def source_voidscreener(store):

    endpoints = (
        (f"{VOID}/api-trending", {"chain": "solana"}),
        (f"{VOID}/api-tokens", {
            "chain": "solana",
            "limit": MAX_SOURCE_ROWS,
        }),
    )

    for url, params in endpoints:

        payload = get_json(url, params)
        rows = rows_from(payload)

        if isinstance(payload, list):
            rows = [x for x in payload if isinstance(x, dict)]

        for row in rows[:MAX_SOURCE_ROWS]:

            candidate = normalize(row, "VoidScreener")

            if candidate:
                add_candidate(store, candidate)


# =========================================================
# DEXSCREENER ENRICHMENT
# =========================================================

def enrich_from_dexscreener(store):

    mints = list(store.keys())

    for start in range(0, len(mints), 30):

        batch = mints[start:start + 30]

        payload = get_json(
            f"{DEX}/tokens/v1/solana/{','.join(batch)}"
        )

        if not isinstance(payload, list):
            continue

        for pair in payload:

            if (
                not isinstance(pair, dict)
                or pair.get("chainId") != "solana"
            ):
                continue

            base = pair.get("baseToken") or {}
            mint = base.get("address")

            if mint not in store:
                continue

            candidate = store[mint]

            candidate["name"] = base.get("name") or candidate["name"]
            candidate["symbol"] = base.get("symbol") or candidate["symbol"]

            liquidity = as_float(
                (pair.get("liquidity") or {}).get("usd")
            ) or 0.0

            pair_created = parse_timestamp(pair.get("pairCreatedAt"))

            if pair_created:

                old_created = candidate.get("pair_created_ts")

                if old_created is None or pair_created < old_created:
                    candidate["pair_created_ts"] = pair_created

            if liquidity >= candidate.get("liquidity", 0):

                candidate["liquidity"] = liquidity

                candidate["price"] = (
                    as_float(pair.get("priceUsd"))
                    or candidate.get("price")
                )

                candidate["market_cap"] = (
                    as_float(pair.get("marketCap"))
                    or as_float(pair.get("fdv"))
                    or candidate.get("market_cap")
                )

                candidate["dex_url"] = (
                    pair.get("url")
                    or candidate.get("dex_url")
                )


# =========================================================
# CANDLE NORMALIZATION
# =========================================================

def _extract_ohlcv_rows(payload):

    if not isinstance(payload, dict):
        return []

    try:
        rows = payload["data"]["attributes"]["ohlcv_list"]

        if isinstance(rows, list):
            return rows

    except Exception:
        pass

    for key in ("result", "data", "ohlcv", "candles", "items"):

        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):

            for inner in ("result", "data", "ohlcv", "candles", "items"):

                if isinstance(value.get(inner), list):
                    return value[inner]

    return []


def _normalize_candles(raw, include_ohlc=False):

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

        ts = parse_timestamp(ts)

        op, hi, lo, cl = map(
            as_float,
            (op, hi, lo, cl),
        )

        if ts is None or any(
            v is None or v <= 0
            for v in (op, hi, lo, cl)
        ):
            continue

        row = {
            "time": ts,
            "high": hi,
            "low": lo,
        }

        if include_ohlc:

            row.update({
                "open": op,
                "close": cl,
                "datetime": datetime.fromtimestamp(
                    ts,
                    timezone.utc,
                ),
            })

        output.append(row)

    output.sort(key=lambda x: x["time"])

    # Remove duplicate timestamps
    unique = {}

    for candle in output:
        unique[candle["time"]] = candle

    return sorted(
        unique.values(),
        key=lambda x: x["time"],
    )


# =========================================================
# GECKO POOL SELECTION
# =========================================================

def gecko_best_pool(mint):

    payload = get_json(
        f"{GECKO}/networks/solana/tokens/{mint}/pools",
        {"page": 1},
    )

    best_pool = None
    best_liquidity = -1.0
    best_side = None

    for row in rows_from(payload):

        attrs = row.get("attributes") or {}
        rels = row.get("relationships") or {}
        pool_id = row.get("id")

        pool_address = (
            pool_id.split("_", 1)[1]
            if isinstance(pool_id, str) and "_" in pool_id
            else attrs.get("address")
        )

        base_id = (
            (rels.get("base_token") or {}).get("data") or {}
        ).get("id")

        quote_id = (
            (rels.get("quote_token") or {}).get("data") or {}
        ).get("id")

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

        liquidity = as_float(
            attrs.get("reserve_in_usd")
        ) or 0.0

        if pool_address and liquidity > best_liquidity:

            best_pool = pool_address
            best_side = side
            best_liquidity = liquidity

    return best_pool, best_side


# =========================================================
# CANDLE PROVIDERS
# =========================================================

def _coingecko_pool_ohlcv(
    pool,
    timeframe,
    aggregate,
    limit,
    side="base",
):

    headers = {"accept": "application/json"}

    if COINGECKO_DEMO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_DEMO_API_KEY

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

    return _extract_ohlcv_rows(payload)


def _moralis_pair_addresses(mint):

    if not MORALIS_API_KEY:
        return []

    headers = {
        "accept": "application/json",
        "X-API-Key": MORALIS_API_KEY,
    }

    payload = get_json(
        f"{MORALIS_SOLANA}/token/mainnet/{mint}/pairs",
        headers=headers,
    )

    rows = rows_from(payload)

    if not rows and isinstance(payload, dict):

        for key in ("pairs", "result", "data"):

            if isinstance(payload.get(key), list):
                rows = payload[key]
                break

    pairs = []

    for row in rows:

        if not isinstance(row, dict):
            continue

        address = first(
            row.get("pairAddress"),
            row.get("pair_address"),
            row.get("address"),
        )

        if address:

            liquidity = as_float(
                first(
                    row.get("liquidityUsd"),
                    row.get("liquidity_usd"),
                    row.get("liquidity"),
                )
            ) or 0

            pairs.append((address, liquidity))

    pairs.sort(key=lambda x: x[1], reverse=True)

    return [x[0] for x in pairs[:3]]


def _moralis_ohlcv(mint, timeframe, limit):

    if not MORALIS_API_KEY:
        return []

    headers = {
        "accept": "application/json",
        "X-API-Key": MORALIS_API_KEY,
    }

    tf = "4h" if timeframe == "hour" else "1d"

    for pair in _moralis_pair_addresses(mint):

        payload = get_json(
            f"{MORALIS_SOLANA}/token/mainnet/pairs/{pair}/ohlcv",
            {
                "timeframe": tf,
                "currency": "usd",
                "limit": limit,
            },
            headers=headers,
        )

        rows = _extract_ohlcv_rows(payload)

        if rows:
            return rows

    return []


def get_candles(mint, timeframe, limit=100):

    if timeframe == "4h":
        endpoint = "hour"
        aggregate = 4

    elif timeframe == "1d":
        endpoint = "day"
        aggregate = 1

    else:
        return None, "Unsupported timeframe"

    pool, side = gecko_best_pool(mint)

    providers = []

    if pool and side:

        providers.append((
            "GeckoTerminal",
            lambda: get_json(
                f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{endpoint}",
                {
                    "aggregate": aggregate,
                    "limit": limit,
                    "currency": "usd",
                    "token": side,
                },
            ),
        ))

        providers.append((
            "CoinGecko Onchain",
            lambda: {
                "data": {
                    "attributes": {
                        "ohlcv_list": _coingecko_pool_ohlcv(
                            pool,
                            endpoint,
                            aggregate,
                            limit,
                            side,
                        )
                    }
                }
            },
        ))

    providers.append((
        "Moralis",
        lambda: {
            "result": _moralis_ohlcv(
                mint,
                endpoint,
                limit,
            )
        },
    ))

    for provider, fetch in providers:

        try:

            payload = fetch()

            candles = _normalize_candles(
                _extract_ohlcv_rows(payload),
                include_ohlc=True,
            )

            if len(candles) >= 2:

                logging.info(
                    "%s candles for %s from %s: %d",
                    timeframe,
                    mint,
                    provider,
                    len(candles),
                )

                return candles, provider

        except Exception:

            logging.exception(
                "Candle provider failed: %s",
                provider,
            )

    return None, "No candle provider returned usable data"


def get_daily_candles(mint, limit=1000):

    candles, provider = get_candles(
        mint,
        "1d",
        limit,
    )

    if not candles:
        return None, None, provider

    pool, side = gecko_best_pool(mint)

    return (
        [
            {
                "time": c["time"],
                "high": c["high"],
                "low": c["low"],
            }
            for c in candles
        ],
        pool,
        provider,
    )


# =========================================================
# OBSERVED ATH
# =========================================================

def observed_ath(mint, created_ts):

    if not created_ts:
        return None, "Token creation time unknown"

    age_days = (time.time() - created_ts) / 86400

    if age_days > 175:
        return None, "Full ATH history unavailable"

    candles, pool, provider = get_daily_candles(
        mint,
        1000,
    )

    if not candles:
        return None, provider

    if candles[0]["time"] > created_ts + 36 * 3600:
        return None, "History does not start near token creation"

    ath = max(c["high"] for c in candles)

    return ath, f"Observed daily ATH via {provider}"


# =========================================================
# FILTER 1: ATH DROP
# =========================================================

def ath_filter(store, state):

    candidates = [
        c for c in store.values()
        if (
            (c.get("market_cap") or 0) >= ATH_MIN_MARKET_CAP_USD
            and (c.get("price") or 0) > 0
            and c.get("created_ts")
        )
    ]

    candidates.sort(
        key=lambda x: x.get("market_cap") or 0,
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = int(state.get("ath_cursor", 0)) % len(candidates)

    ordered = candidates[cursor:] + candidates[:cursor]
    batch = ordered[:MAX_ATH_CHECKS_PER_RUN]

    state["ath_cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    save_state(state)

    matches = 0

    for candidate in batch:

        mint = candidate["mint"]

        ath, note = observed_ath(
            mint,
            candidate.get("created_ts"),
        )

        price = candidate.get("price") or 0

        if not ath or price <= 0 or price > ath:
            continue

        drop = ((ath - price) / ath) * 100

        if drop < ATH_MIN_DROP_PERCENT:
            continue

        watch = state["ath_watch"].get(mint, {})

        previous_ath = as_float(watch.get("ath")) or 0
        stored_ath = max(ath, previous_ath)

        state["ath_watch"][mint] = {
            "ath": stored_ath,
            "name": candidate["name"],
            "symbol": candidate["symbol"],
            "sources": sorted(candidate["sources"]),
            "url": candidate.get("dex_url") or dex_url(mint),
            "created_ts": candidate.get("created_ts"),
            "registered_at": watch.get("registered_at") or time.time(),
        }

        save_state(state)

        message = (
            f"🎯 شکار ریزش از سقف تاریخی | DEX HUNTER {VERSION}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 توکن: {candidate['name']} ({candidate['symbol']})\n"
            f"💰 قیمت فعلی: ${price:.10g}\n"
            f"🏔 سقف مشاهده‌شده: ${stored_ath:.10g}\n"
            f"📉 افت از ATH: {drop:.1f}%\n"
            f"📊 مارکت‌کپ: ${(candidate.get('market_cap') or 0):,.0f}\n"
            f"🔎 منابع: {', '.join(sorted(candidate['sources']))}\n"
            f"🧾 قرارداد: {mint}\n"
            f"📈 نمودار: {candidate.get('dex_url') or dex_url(mint)}\n"
            f"ℹ️ {note}\n"
            "👁 این توکن برای بررسی بازگشت ذخیره شد.\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید."
        )

        deliver(state, "ath", mint, message)

        matches += 1
        time.sleep(0.5)

    return matches


# =========================================================
# FILTER 2: ATH BREAKOUT
# =========================================================

def find_ath_breakout(candles, created_ts):

    if not candles:
        return None

    if created_ts and candles[0]["time"] > created_ts + 36 * 3600:
        return None

    peak = 0.0
    peak_time = None
    armed = False

    reference_ath = None
    drawdown_time = None
    drawdown_low = None

    for candle in candles:

        ts = candle["time"]
        high = candle["high"]
        low = candle["low"]

        if not armed:

            if (
                peak > 0
                and peak_time is not None
                and ts > peak_time
                and low <= peak * (1 - BREAKOUT_DROP_PERCENT / 100)
            ):

                armed = True
                reference_ath = peak
                drawdown_time = ts
                drawdown_low = low
                continue

            if high > peak:
                peak = high
                peak_time = ts

        else:
            drawdown_low = min(drawdown_low, low)

    if not armed:
        return None

    latest = candles[-1]

    if (
        latest["time"] <= drawdown_time
        or latest["high"] <= reference_ath
    ):
        return None

    return {
        "previous_ath": reference_ath,
        "drawdown_low": drawdown_low,
        "drop_percent": (
            (reference_ath - drawdown_low) / reference_ath
        ) * 100,
        "breakout_high": latest["high"],
        "breakout_time": latest["time"],
    }


def ath_breakout_filter(store, state):

    candidates = [
        c for c in store.values()
        if c.get("created_ts") and (c.get("price") or 0) > 0
    ]

    candidates.sort(
        key=lambda x: x.get("liquidity") or 0,
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = int(state.get("breakout_cursor", 0)) % len(candidates)

    ordered = candidates[cursor:] + candidates[:cursor]
    batch = ordered[:MAX_BREAKOUT_CHECKS_PER_RUN]

    state["breakout_cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    save_state(state)

    matches = 0

    for candidate in batch:

        mint = candidate["mint"]

        if all(
            f"{mint}|{chat}" in state["sent"]["ath_breakout"]
            for chat in CHAT_IDS
        ):
            continue

        created_ts = candidate.get("created_ts")
        age_days = (time.time() - created_ts) / 86400

        if age_days <= 0 or age_days > BREAKOUT_MAX_AGE_DAYS:
            continue

        candles, pool, note = get_daily_candles(
            mint,
            BREAKOUT_HISTORY_LIMIT,
        )

        if not candles:
            continue

        result = find_ath_breakout(
            candles,
            created_ts,
        )

        if not result:
            continue

        price = candidate.get("price") or 0

        if price <= result["previous_ath"]:
            continue

        breakout_time = datetime.fromtimestamp(
            result["breakout_time"],
            timezone.utc,
        ).strftime("%Y-%m-%d %H:%M UTC")

        message = (
            f"🚀 شکست سقف تاریخی پس از ریزش ۷۵٪ | DEX HUNTER {VERSION}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 توکن: {candidate['name']} ({candidate['symbol']})\n"
            f"🏔 ATH قبلی: ${result['previous_ath']:.10g}\n"
            f"📉 کف ریزش: ${result['drawdown_low']:.10g}\n"
            f"📊 میزان ریزش: {result['drop_percent']:.1f}%\n"
            f"💥 سقف کندل شکست: ${result['breakout_high']:.10g}\n"
            f"💰 قیمت فعلی: ${price:.10g}\n"
            f"🕒 زمان شکست: {breakout_time}\n"
            f"🧾 قرارداد: {mint}\n"
            f"📈 نمودار: {candidate.get('dex_url') or dex_url(mint)}\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید."
        )

        deliver(state, "ath_breakout", mint, message)

        matches += 1
        time.sleep(0.5)

    return matches


# =========================================================
# DEXSCREENER LIVE PRICE
# =========================================================

def get_live_token_data(mint):

    payload = get_json(
        f"{DEX}/tokens/v1/solana/{mint}"
    )

    if not isinstance(payload, list) or not payload:
        return None

    valid_pairs = []

    for pair in payload:

        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != "solana":
            continue

        base = pair.get("baseToken") or {}

        if base.get("address") != mint:
            continue

        price = as_float(pair.get("priceUsd"))

        if price is None or price <= 0:
            continue

        liquidity = as_float(
            (pair.get("liquidity") or {}).get("usd")
        ) or 0

        valid_pairs.append({
            "price": price,
            "liquidity": liquidity,
            "market_cap": (
                as_float(pair.get("marketCap"))
                or as_float(pair.get("fdv"))
            ),
            "name": base.get("name"),
            "symbol": base.get("symbol"),
            "url": pair.get("url"),
        })

    if not valid_pairs:
        return None

    valid_pairs.sort(
        key=lambda x: x["liquidity"],
        reverse=True,
    )

    return valid_pairs[0]


# =========================================================
# FILTER 3: ATH RECLAIM BUY
# =========================================================

def ath_reclaim_filter(state):

    if not BUY_ALERT_ENABLED:
        return 0

    watch = state.get("ath_watch", {})
    mints = list(watch.keys())

    if not mints:
        return 0

    cursor = int(state.get("buy_cursor", 0)) % len(mints)

    ordered = mints[cursor:] + mints[:cursor]
    batch = ordered[:BUY_CHECKS_PER_RUN]

    state["buy_cursor"] = (
        cursor + len(batch)
    ) % len(mints)

    save_state(state)

    matches = 0

    for mint in batch:

        info = watch.get(mint)

        if not isinstance(info, dict):
            continue

        ath = as_float(info.get("ath"))

        if not ath or ath <= 0:
            continue

        if all(
            f"{mint}|{chat}" in state["sent"]["ath_reclaim"]
            for chat in CHAT_IDS
        ):
            continue

        live = get_live_token_data(mint)

        if not live:
            continue

        price = live["price"]
        threshold = ath * (BUY_ATH_THRESHOLD_PERCENT / 100)

        if price < threshold:
            continue

        distance = ((price / ath) - 1) * 100

        name = live.get("name") or info.get("name") or "Unknown"
        symbol = live.get("symbol") or info.get("symbol") or "???"
        chart = live.get("url") or dex_url(mint)

        message = (
            "🟢🚨🚨 ATH RECLAIM BUY SIGNAL 🚨🚨🟢\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🟢 هشدار جدی بازگشت به سقف تاریخی\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 نام توکن: {name}\n"
            f"🔖 نماد: {symbol}\n"
            f"🏔 سقف تاریخی ذخیره‌شده: ${ath:.12g}\n"
            f"💰 قیمت لحظه‌ای: ${price:.12g}\n"
            f"📊 فاصله از ATH: {distance:+.2f}%\n"
            f"📈 حد هشدار: {BUY_ATH_THRESHOLD_PERCENT}% ATH\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🟢 وضعیت: ATH RECLAIM DETECTED\n"
            "🟢 سیگنال: BUY ALERT\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🧾 آدرس قرارداد:\n{mint}\n"
            f"🔗 نمودار و بررسی:\n{chart}\n"
            "⚠️ این هشدار تضمین شکست پایدار یا سود نیست."
        )

        deliver(state, "ath_reclaim", mint, message)

        matches += 1
        time.sleep(0.3)

    return matches


# =========================================================
# RECOVERY ENGINE: TECHNICAL ANALYSIS
# =========================================================

def calculate_recovery_score(candles, market_cap, liquidity):

    """
    Transparent 100-point technical score.

    ATH distance:             15 points
    Bottom formation:         20 points
    Trend structure:          20 points
    Resistance breakout:      20 points
    Liquidity:                15 points
    Market cap:               10 points

    Contract security and holder data are NOT fabricated.
    They are not included in this score.
    """

    if not candles or len(candles) < RECOVERY_MIN_CANDLES:
        return None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    current = closes[-1]

    if current <= 0:
        return None

    recent = candles[-12:]
    previous = candles[-24:-12]

    if len(previous) < 6:
        return None

    recent_low = min(c["low"] for c in recent)
    previous_low = min(c["low"] for c in previous)

    recent_high = max(c["high"] for c in recent)
    previous_high = max(c["high"] for c in previous)

    all_time_window_high = max(highs)
    distance_from_window_high = (
        (all_time_window_high - current)
        / all_time_window_high
    ) * 100 if all_time_window_high > 0 else 0

    score = 0
    details = {}

    # 1. Distance from recent high: 15 points
    if 20 <= distance_from_window_high <= 85:
        points = 15
    elif 10 <= distance_from_window_high < 20:
        points = 8
    elif distance_from_window_high > 85:
        points = 5
    else:
        points = 0

    score += points
    details["ath_distance"] = points

    # 2. Bottom formation: 20 points
    # Recent lows should stop making lower lows.
    if recent_low >= previous_low * 0.98:
        bottom_points = 15

        if current > recent_low * 1.05:
            bottom_points += 5

    elif recent_low > previous_low:
        bottom_points = 20

    else:
        bottom_points = 0

    score += bottom_points
    details["bottom"] = bottom_points

    # 3. Trend structure: 20 points
    structure_points = 0

    if current > closes[-4]:
        structure_points += 5

    if current > closes[-7]:
        structure_points += 5

    if recent_high > previous_high:
        structure_points += 5

    if recent_low >= previous_low * 0.98:
        structure_points += 5

    score += structure_points
    details["structure"] = structure_points

    # 4. Resistance breakout: 20 points
    resistance = max(
        c["high"] for c in candles[-7:-1]
    )

    breakout_points = 0

    if current > resistance:
        breakout_points = 20

    elif current >= resistance * 0.98:
        breakout_points = 10

    score += breakout_points
    details["breakout"] = breakout_points

    # 5. Liquidity: 15 points
    if liquidity >= 100_000:
        liquidity_points = 15
    elif liquidity >= 50_000:
        liquidity_points = 12
    elif liquidity >= 25_000:
        liquidity_points = 9
    elif liquidity >= RECOVERY_MIN_LIQUIDITY_USD:
        liquidity_points = 6
    else:
        liquidity_points = 0

    score += liquidity_points
    details["liquidity"] = liquidity_points

    # 6. Market cap: 10 points
    if market_cap >= 250_000:
        mc_points = 10
    elif market_cap >= 100_000:
        mc_points = 8
    elif market_cap >= RECOVERY_MIN_MARKET_CAP_USD:
        mc_points = 5
    else:
        mc_points = 0

    score += mc_points
    details["market_cap"] = mc_points

    # Stage determination
    if current < closes[-7] and recent_low < previous_low:
        stage = "FALLING"

    elif (
        recent_low >= previous_low * 0.98
        and current > recent_low * 1.03
    ):
        stage = "BOTTOM FORMING"

    elif (
        current > closes[-4]
        and recent_high > previous_high
    ):
        stage = "RECOVERY"

    else:
        stage = "FALLING"

    # Confirmed reversal requires two consecutive candle closes
    # above the previous resistance.
    confirmed = False

    if len(candles) >= 8:

        resistance_before = max(
            c["high"] for c in candles[-8:-2]
        )

        last_two = candles[-2:]

        if all(
            c["close"] > resistance_before
            for c in last_two
        ):
            confirmed = True

    if confirmed:
        stage = "CONFIRMED REVERSAL"

    return {
        "score": min(100, int(score)),
        "stage": stage,
        "details": details,
        "price": current,
        "recent_low": recent_low,
        "resistance": resistance,
        "distance_from_window_high": distance_from_window_high,
        "confirmed": confirmed,
    }


# =========================================================
# RECOVERY SIGNAL CLASSIFICATION
# =========================================================

def classify_recovery_signal(analysis, liquidity):

    if not analysis:
        return None

    score = analysis["score"]

    # A minimum liquidity condition is required for stronger alerts.
    if score >= STRONG_BUY_SCORE:

        if (
            liquidity >= RECOVERY_MIN_LIQUIDITY_USD
            and analysis["confirmed"]
        ):
            return "STRONG BUY"

    if score >= RECOVERY_SCORE:
        return "RECOVERY"

    if score >= WATCH_SCORE:
        return "WATCH"

    return None


def recovery_signal_message(candidate, analysis, signal):

    mint = candidate["mint"]

    details = analysis["details"]

    risk_status = (
        "نامشخص؛ بررسی امنیت قرارداد از منبع مستقل لازم است"
    )

    return (
        f"🧠 DEX HUNTER V17 | {signal}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🪙 توکن: {candidate.get('name', 'Unknown')} "
        f"({candidate.get('symbol', '???')})\n"
        f"💰 قیمت: ${analysis['price']:.12g}\n"
        f"📊 امتیاز بازگشت: {analysis['score']}/100\n"
        f"📍 مرحله: {analysis['stage']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔹 فاصله از سقف اخیر: "
        f"{analysis['distance_from_window_high']:.1f}%\n"
        f"🔹 کف اخیر: ${analysis['recent_low']:.12g}\n"
        f"🔹 مقاومت: ${analysis['resistance']:.12g}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📋 جزئیات امتیاز:\n"
        f"🏔 فاصله از سقف: {details['ath_distance']}/15\n"
        f"🟡 تشکیل کف: {details['bottom']}/20\n"
        f"📈 ساختار روند: {details['structure']}/20\n"
        f"💥 شکست مقاومت: {details['breakout']}/20\n"
        f"💧 نقدینگی: {details['liquidity']}/15\n"
        f"📊 مارکت‌کپ: {details['market_cap']}/10\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💧 نقدینگی: ${candidate.get('liquidity', 0):,.0f}\n"
        f"📊 مارکت‌کپ: ${(candidate.get('market_cap') or 0):,.0f}\n"
        f"🛡 امنیت قرارداد: {risk_status}\n"
        f"🧾 قرارداد:\n{mint}\n"
        f"🔗 بررسی نمودار:\n{candidate.get('dex_url') or dex_url(mint)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ امتیاز، تحلیل تکنیکال است و تضمین سود نیست.\n"
        "⚠️ قبل از خرید، امنیت قرارداد، نقدینگی و اسلیپیج را بررسی کنید."
    )


# =========================================================
# RECOVERY DATABASE AND SIGNAL ENGINE
# =========================================================

def recovery_filter(store, recovery, performance):

    if not RECOVERY_ENABLED:
        return 0

    candidates = [
        c for c in store.values()
        if (
            (c.get("market_cap") or 0) >= RECOVERY_MIN_MARKET_CAP_USD
            and (c.get("price") or 0) > 0
            and (c.get("liquidity") or 0) > 0
        )
    ]

    candidates.sort(
        key=lambda x: x.get("liquidity") or 0,
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = int(recovery.get("cursor", 0)) % len(candidates)

    ordered = candidates[cursor:] + candidates[:cursor]

    batch = ordered[:RECOVERY_CHECKS_PER_RUN]

    recovery["cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    save_recovery(recovery)

    alerts = 0

    for candidate in batch:

        mint = candidate["mint"]

        candles, provider = get_candles(
            mint,
            "4h",
            RECOVERY_CANDLE_LIMIT,
        )

        if not candles or len(candles) < RECOVERY_MIN_CANDLES:
            continue

        analysis = calculate_recovery_score(
            candles,
            candidate.get("market_cap") or 0,
            candidate.get("liquidity") or 0,
        )

        if not analysis:
            continue

        signal = classify_recovery_signal(
            analysis,
            candidate.get("liquidity") or 0,
        )

        record = recovery["tokens"].setdefault(
            mint,
            {
                "first_seen": time.time(),
                "last_stage": None,
                "last_score": 0,
                "last_signal": None,
                "last_signal_at": 0,
                "history": [],
            },
        )

        record["last_stage"] = analysis["stage"]
        record["last_score"] = analysis["score"]
        record["last_price"] = analysis["price"]
        record["last_checked"] = time.time()
        record["candle_provider"] = provider

        record["history"].append({
            "time": time.time(),
            "price": analysis["price"],
            "score": analysis["score"],
            "stage": analysis["stage"],
        })

        # Keep the last 100 observations per token.
        record["history"] = record["history"][-100:]

        if not signal:
            continue

        now = time.time()

        previous_signal = record.get("last_signal")
        previous_time = safe_float(
            record.get("last_signal_at"),
            0,
        )

        # Send a new signal if its level improves.
        # Same-level signals require cooldown.
        improved = (
            previous_signal is None
            or (
                previous_signal == "WATCH"
                and signal in ("RECOVERY", "STRONG BUY")
            )
            or (
                previous_signal == "RECOVERY"
                and signal == "STRONG BUY"
            )
        )

        cooldown_passed = (
            now - previous_time
            >= RECOVERY_ALERT_COOLDOWN_HOURS * 3600
        )

        if not improved and not cooldown_passed:
            continue

        message = recovery_signal_message(
            candidate,
            analysis,
            signal,
        )

        for chat_id in CHAT_IDS:
            send_telegram(chat_id, message)
            time.sleep(0.12)

        record["last_signal"] = signal
        record["last_signal_at"] = now

        # Register a performance record only for newly issued signals.
        performance["signals"].append({
            "mint": mint,
            "name": candidate.get("name", "Unknown"),
            "symbol": candidate.get("symbol", "???"),
            "signal": signal,
            "score": analysis["score"],
            "entry_price": analysis["price"],
            "entry_time": now,
            "samples": {},
        })

        save_performance(performance)
        save_recovery(recovery)

        alerts += 1

        logging.info(
            "RECOVERY ALERT %s %s score=%s price=%s",
            signal,
            mint,
            analysis["score"],
            analysis["price"],
        )

    save_recovery(recovery)

    return alerts


# =========================================================
# PERFORMANCE TRACKING
# =========================================================

def update_performance(performance):

    now = time.time()

    signals = performance.get("signals", [])

    # Process recent signals. Older completed records remain in database.
    for record in signals[-500:]:

        mint = record.get("mint")

        if not mint:
            continue

        entry_price = as_float(record.get("entry_price"))

        if not entry_price or entry_price <= 0:
            continue

        entry_time = safe_float(
            record.get("entry_time"),
            0,
        )

        if not entry_time:
            continue

        elapsed = now - entry_time

        pending = [
            name
            for name, seconds in PERFORMANCE_WINDOWS.items()
            if (
                elapsed >= seconds
                and name not in record.get("samples", {})
            )
        ]

        if not pending:
            continue

        live = get_live_token_data(mint)

        if not live:
            continue

        current_price = live["price"]

        if current_price <= 0:
            continue

        samples = record.setdefault("samples", {})

        for name in pending:

            change = (
                (current_price / entry_price) - 1
            ) * 100

            samples[name] = {
                "time": now,
                "price": current_price,
                "change_percent": round(change, 4),
            }

            logging.info(
                "PERFORMANCE %s %s %s: %.2f%%",
                mint,
                record.get("signal"),
                name,
                change,
            )

    save_performance(performance)


# =========================================================
# MIGRATION: OLD ATH RECORDS
# =========================================================

def migrate_old_ath_alerts(store, state):

    old_mints = set()

    for item in state["sent"]["ath"]:

        if "|" in item:
            old_mints.add(item.split("|", 1)[0])

    missing = [
        mint for mint in old_mints
        if mint not in state["ath_watch"]
    ]

    recovered = 0

    for mint in missing[:MAX_ATH_CHECKS_PER_RUN]:

        candidate = store.get(mint)

        if not candidate:
            continue

        created_ts = candidate.get("created_ts")

        if not created_ts:
            continue

        ath, note = observed_ath(
            mint,
            created_ts,
        )

        if not ath:
            continue

        price = candidate.get("price") or 0

        if price <= 0:
            continue

        drop = ((ath - price) / ath) * 100

        if drop < ATH_MIN_DROP_PERCENT:
            continue

        state["ath_watch"][mint] = {
            "ath": ath,
            "name": candidate.get("name", "Unknown"),
            "symbol": candidate.get("symbol", "???"),
            "sources": sorted(candidate.get("sources", [])),
            "url": candidate.get("dex_url") or dex_url(mint),
            "created_ts": created_ts,
            "registered_at": time.time(),
        }

        recovered += 1

    if recovered:
        save_state(state)

    logging.info(
        "Legacy ATH records recovered: %d",
        recovered,
    )

    return recovered


# =========================================================
# TELEGRAM INCOMING: FORWARDED POSTS
# =========================================================

def get_telegram_updates(offset):

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

        response.raise_for_status()

        payload = response.json()

        if not payload.get("ok"):
            logging.warning(
                "Telegram getUpdates returned not-ok: %s",
                payload,
            )
            return []

        return payload.get("result", [])

    except Exception as exc:

        logging.warning(
            "Telegram getUpdates failed: %s",
            exc,
        )

        return []


def is_forwarded_message(message):

    return bool(
        message.get("forward_origin")
        or message.get("forward_from")
        or message.get("forward_from_chat")
        or message.get("forward_sender_name")
    )


def extract_mint_from_message(message):

    text_content = (
        message.get("text")
        or message.get("caption")
        or ""
    )

    matches = SOLANA_ADDRESS_PATTERN.findall(
        text_content
    )

    for mint in matches:

        if not mint_looks_valid(mint):
            continue

        token_data = get_live_token_data(mint)

        if token_data:
            return mint

    return None


# =========================================================
# CHART GENERATION
# =========================================================

def get_chart_candles(mint, timeframe, limit):

    candles, provider = get_candles(
        mint,
        timeframe,
        limit,
    )

    if not candles:
        return None

    return [
        {
            "time": candle["datetime"],
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
        }
        for candle in candles
    ]


def create_candlestick_chart(mint, timeframe, symbol):

    limit = (
        CHART_4H_LIMIT
        if timeframe == "4h"
        else CHART_1D_LIMIT
    )

    candles = get_chart_candles(
        mint,
        timeframe,
        limit,
    )

    if not candles:
        return None

    CHART_OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    fig, ax = plt.subplots(
        figsize=(13, 7),
        dpi=150,
    )

    dates = [
        mdates.date2num(candle["time"])
        for candle in candles
    ]

    if timeframe == "4h":

        candle_width = 0.11
        timeframe_label = "4H"
        date_format = "%m-%d %H:%M"

    else:

        candle_width = 0.65
        timeframe_label = "1D"
        date_format = "%Y-%m-%d"

    for x, candle in zip(dates, candles):

        open_price = candle["open"]
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]

        candle_color = (
            "#16a34a"
            if close >= open_price
            else "#dc2626"
        )

        ax.plot(
            [x, x],
            [low, high],
            color=candle_color,
            linewidth=1.1,
        )

        body_bottom = min(
            open_price,
            close,
        )

        body_height = abs(
            close - open_price
        )

        if body_height == 0:
            body_height = max(
                open_price * 0.00001,
                1e-12,
            )

        ax.add_patch(
            Rectangle(
                (
                    x - candle_width / 2,
                    body_bottom,
                ),
                candle_width,
                body_height,
                facecolor=candle_color,
                edgecolor=candle_color,
                linewidth=0.7,
            )
        )

    ax.set_title(
        f"{symbol} / {timeframe_label}",
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

    ax.grid(True, alpha=0.18)
    ax.tick_params(axis="x", rotation=25)
    ax.margins(x=0.02, y=0.08)

    fig.tight_layout()

    safe_mint = re.sub(
        r"[^A-Za-z0-9_-]",
        "_",
        mint,
    )

    filename = (
        CHART_OUTPUT_DIR
        / f"{safe_mint}_{timeframe}.png"
    )

    try:

        fig.savefig(
            filename,
            bbox_inches="tight",
        )

    except Exception:

        logging.exception(
            "Chart image save failed"
        )

        plt.close(fig)
        return None

    plt.close(fig)

    return filename


# =========================================================
# PROCESS FORWARDED TELEGRAM POSTS
# =========================================================

def process_forwarded_messages(state):

    offset = int(
        state.get("telegram_update_offset", 0)
    )

    updates = get_telegram_updates(offset)

    processed = 0
    allowed_chats = set(CHAT_IDS)

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
            or chat_id not in allowed_chats
        ):

            state["telegram_update_offset"] = update_id + 1
            save_state(state)
            continue

        if not is_forwarded_message(message):

            state["telegram_update_offset"] = update_id + 1
            save_state(state)
            continue

        mint = extract_mint_from_message(message)

        if not mint:

            send_telegram(
                chat_id,
                "❌ آدرس قرارداد معتبر سولانا در پست فورواردشده پیدا نشد.",
            )

            state["telegram_update_offset"] = update_id + 1
            save_state(state)

            processed += 1
            continue

        live = get_live_token_data(mint)

        if not live:

            send_telegram(
                chat_id,
                "❌ اطلاعات این توکن در DexScreener پیدا نشد.",
            )

            state["telegram_update_offset"] = update_id + 1
            save_state(state)

            processed += 1
            continue

        symbol = live.get("symbol") or "TOKEN"

        chart_4h = create_candlestick_chart(
            mint,
            "4h",
            symbol,
        )

        chart_1d = create_candlestick_chart(
            mint,
            "1d",
            symbol,
        )

        if not chart_4h or not chart_1d:

            send_telegram(
                chat_id,
                "❌ دریافت کندل‌های 4H یا 1D از منابع موجود ناموفق بود.",
            )

        else:

            send_telegram_photo(
                chat_id,
                chart_4h,
            )

            send_telegram_photo(
                chat_id,
                chart_1d,
            )

        state["telegram_update_offset"] = update_id + 1
        save_state(state)

        processed += 1

        time.sleep(0.3)

    logging.info(
        "Forwarded Telegram posts processed: %d",
        processed,
    )

    return processed


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise SystemExit(
            "Missing GitHub Secret: TELEGRAM_BOT_TOKEN"
        )

    state = load_state()
    recovery = load_recovery()
    performance = load_performance()

    logging.info(
        "%s scan started",
        VERSION,
    )

    # 1. Process forwarded Telegram posts
    process_forwarded_messages(state)

    # 2. Discover tokens
    candidates = {}

    source_dexscreener(candidates)
    source_geckoterminal(candidates)
    source_pump_fun(candidates)
    source_stonk_fun(candidates)
    source_voidscreener(candidates)

    logging.info(
        "Discovered %d unique tokens",
        len(candidates),
    )

    # 3. Enrich market data
    enrich_from_dexscreener(candidates)

    with_market_cap = sum(
        1
        for candidate in candidates.values()
        if candidate.get("market_cap") is not None
    )

    # 4. Restore legacy ATH records
    migrate_old_ath_alerts(
        candidates,
        state,
    )

    # 5. Original ATH Drop
    ath_count = ath_filter(
        candidates,
        state,
    )

    # 6. Original ATH Breakout
    breakout_count = ath_breakout_filter(
        candidates,
        state,
    )

    # 7. Original ATH Reclaim
    buy_count = ath_reclaim_filter(
        state,
    )

    # 8. New Smart Recovery Engine
    recovery_count = recovery_filter(
        candidates,
        recovery,
        performance,
    )

    # 9. Update signal performance
    update_performance(performance)

    # 10. Save all databases
    save_state(state)
    save_recovery(recovery)
    save_performance(performance)

    logging.info(
        "%s COMPLETE | "
        "tokens=%d | "
        "market_cap_data=%d | "
        "ath_alerts=%d | "
        "breakout_alerts=%d | "
        "ath_reclaim_alerts=%d | "
        "recovery_alerts=%d | "
        "ath_watch=%d | "
        "recovery_tracked=%d | "
        "performance_records=%d",
        VERSION,
        len(candidates),
        with_market_cap,
        ath_count,
        breakout_count,
        buy_count,
        recovery_count,
        len(state["ath_watch"]),
        len(recovery["tokens"]),
        len(performance["signals"]),
    )


if __name__ == "__main__":
    main()
