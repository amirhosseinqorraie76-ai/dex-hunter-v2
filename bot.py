
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V17 - DAILY SMART RECOVERY

FEATURES:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun
5. VoidScreener

SCANNERS:
- ATH DROP
- SMART RECOVERY
- BOTTOM FORMING
- RECOVERY
- CONFIRMED REVERSAL
- ATH BREAKOUT
- ATH RECLAIM
- TELEGRAM FORWARDED CHARTS

DAILY RESET:
- Daily operational memory resets at 00:00 UTC.
- ATH history is preserved.
- Performance history is preserved.
- Tokens can be introduced again on a new UTC day.

IMPORTANT:
- NO volume filter.
- NO minimum score for introducing a token.
- Score is informational only.
- NO automatic trading.

Run interval: every 5 minutes through GitHub Actions.
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

VERSION = "V17 DAILY SMART RECOVERY"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
COINGECKO_KEY = os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
MORALIS_KEY = os.getenv("MORALIS_API_KEY", "").strip()

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
MORALIS = "https://solana-gateway.moralis.io"

HTTP_TIMEOUT = 20
MAX_SOURCE_ROWS = 100

# ATH DROP
MIN_MARKET_CAP = 50_000
MIN_ATH_DROP = 30.0
ATH_CHECKS_PER_RUN = 5

# SMART RECOVERY
RECOVERY_CHECKS_PER_RUN = 20

# ATH BREAKOUT
BREAKOUT_CHECKS_PER_RUN = 5
BREAKOUT_DROP_PERCENT = 75.0
BREAKOUT_MAX_AGE_DAYS = 990

# Candles
CANDLE_4H_LIMIT = 100
CANDLE_1D_LIMIT = 1000

# Telegram
TELEGRAM_UPDATES_LIMIT = 30

# Alert cooldowns
WATCH_COOLDOWN = 12 * 3600
RECOVERY_COOLDOWN = 12 * 3600
STRONG_COOLDOWN = 24 * 3600

# Files
SEEN_FILE = Path("seen.json")
RECOVERY_FILE = Path("recovery.json")
PERFORMANCE_FILE = Path("performance.json")
CHART_DIR = Path("charts")

ADDRESS_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)

HEADERS = {
    "User-Agent": "DEX-HUNTER-V17/1.0",
    "Accept": "application/json",
}

session = requests.Session()
session.headers.update(HEADERS)

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
    return (
        isinstance(mint, str)
        and 32 <= len(mint) <= 44
    )


def dex_url(mint):
    return f"https://dexscreener.com/solana/{mint}"


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
        logging.warning("API error %s: %s", url, exc)
        return None


def rows_from(payload):
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if not isinstance(payload, dict):
        return []

    for key in (
        "tokens", "coins", "items", "results",
        "pools", "data", "pairs"
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):
            for nested in (
                "tokens", "coins", "items",
                "results", "pools", "pairs"
            ):
                inner = value.get(nested)

                if isinstance(inner, list):
                    return [
                        x for x in inner
                        if isinstance(x, dict)
                    ]

    return []


# =========================================================
# DATABASES
# =========================================================

def read_json(path, default):
    if not path.exists():
        return default

    try:
        data = json.loads(path.read_text(encoding="utf-8"))

        if isinstance(data, dict):
            return data

    except Exception:
        logging.exception("Could not read %s", path)

    return default


def write_json(path, data):
    temp = path.with_suffix(".tmp")

    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
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
    }


def load_seen():
    state = read_json(SEEN_FILE, empty_seen())

    state.setdefault("sent", {})
    state.setdefault("ath_watch", {})
    state.setdefault("telegram_update_offset", 0)
    state.setdefault("ath_cursor", 0)
    state.setdefault("breakout_cursor", 0)
    state.setdefault("daily_reset_date", None)

    for key in (
        "ath", "breakout", "watch",
        "recovery", "strong", "ath_reclaim"
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
    """
    Reset operational memory once per UTC day.

    Preserve:
    - ATH history
    - Telegram update offset
    - Performance history
    - Historical price information

    Reset:
    - Daily alert memory
    - Daily recovery tracking
    - Daily scan cursors
    """

    today = utc_day()
    previous = state.get("daily_reset_date")

    if previous == today:
        return False

    logging.info(
        "DAILY RESET: %s -> %s",
        previous,
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

    # Preserve ATH history in state["ath_watch"].
    # Reset only daily recovery memory.
    recovery["tokens"] = {}
    recovery["cursor"] = 0

    state["daily_reset_date"] = today

    write_json(SEEN_FILE, state)
    write_json(RECOVERY_FILE, recovery)

    logging.info("Daily memory successfully reset.")

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

        response.raise_for_status()
        result = response.json()

        if result.get("ok"):
            return result.get("result")

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

        response.raise_for_status()
        return bool(response.json().get("ok"))

    except Exception as exc:
        logging.warning("Photo send failed: %s", exc)
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
    attrs = row.get("attributes") or {}
    rels = row.get("relationships") or {}

    base = row.get("baseToken") or {}

    base_id = (
        (rels.get("base_token") or {}).get("data") or {}
    ).get("id")

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

    if not valid_mint(mint):
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
        "sources": [source],
        "price": number(first(
            row.get("priceUsd"),
            row.get("price_usd"),
            attrs.get("base_token_price_usd"),
        )),
        "market_cap": number(first(
            row.get("marketCap"),
            row.get("market_cap"),
            row.get("usd_market_cap"),
            row.get("marketCapUsd"),
            attrs.get("market_cap_usd"),
        )),
        "created_ts": timestamp(created),
        "pair_created_ts": timestamp(first(
            row.get("pairCreatedAt"),
            row.get("pair_created_at"),
            attrs.get("pair_created_at"),
            attrs.get("pool_created_at"),
        )),
        "liquidity": 0.0,
        "url": first(row.get("url"), attrs.get("url")),
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
        "name", "symbol", "price",
        "market_cap", "created_ts",
        "pair_created_ts", "url",
    ):
        if old.get(key) is None or old.get(key) in (
            "Unknown", "???"
        ):
            if candidate.get(key) is not None:
                old[key] = candidate[key]


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
    for endpoint in ("new_pools", "trending_pools"):
        payload = get_json(
            f"{GECKO}/networks/solana/{endpoint}",
            {"page": 1},
        )

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
            rels = row.get("relationships") or {}

            base_id = (
                (rels.get("base_token") or {}).get("data") or {}
            ).get("id")

            mint = (
                base_id.split("_", 1)[1]
                if isinstance(base_id, str) and "_" in base_id
                else None
            )

            if not valid_mint(mint):
                continue

            attrs = row.get("attributes") or {}

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
        add_candidate(store, normalize(row, "Pump.fun"))


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
        add_candidate(store, normalize(row, "Stonk.fun"))


def source_void(store):
    endpoints = [
        (f"{VOID}/api-trending", {"chain": "solana"}),
        (
            f"{VOID}/api-tokens",
            {"chain": "solana", "limit": MAX_SOURCE_ROWS},
        ),
    ]

    for url, params in endpoints:
        payload = get_json(url, params)

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:
            add_candidate(store, normalize(row, "VoidScreener"))


# =========================================================
# DEXSCREENER ENRICHMENT
# =========================================================

def enrich(store):
    mints = list(store.keys())

    for start in range(0, len(mints), 30):
        batch = mints[start:start + 30]

        payload = get_json(
            f"{DEX}/tokens/v1/solana/{','.join(batch)}"
        )

        if not isinstance(payload, list):
            continue

        for pair in payload:
            if not isinstance(pair, dict):
                continue

            if pair.get("chainId") != "solana":
                continue

            base = pair.get("baseToken") or {}
            mint = base.get("address")

            if mint not in store:
                continue

            item = store[mint]

            liquidity = number(
                (pair.get("liquidity") or {}).get("usd")
            ) or 0.0

            if liquidity >= item.get("liquidity", 0):
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

                item["name"] = base.get("name") or item["name"]
                item["symbol"] = base.get("symbol") or item["symbol"]
                item["url"] = pair.get("url") or item.get("url")

            pair_created = timestamp(pair.get("pairCreatedAt"))

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

    for key in ("result", "data", "ohlcv", "candles", "items"):
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for inner in (
                "result", "data", "ohlcv",
                "candles", "items"
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
        op, hi, lo, cl = map(number, (op, hi, lo, cl))

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
    )

    best = None
    best_liquidity = -1
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

        liquidity = number(attrs.get("reserve_in_usd")) or 0

        if pool_address and liquidity > best_liquidity:
            best = pool_address
            best_side = side
            best_liquidity = liquidity

    return best, best_side


def coingecko_ohlcv(pool, timeframe, aggregate, limit, side):
    headers = {"accept": "application/json"}

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

    pairs.sort(key=lambda x: x[1], reverse=True)

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
            ),
        ))

        attempts.append((
            "CoinGecko",
            lambda: {
                "result": coingecko_ohlcv(
                    pool, timeframe, aggregate, limit, side
                )
            },
        ))

    attempts.append((
        "Moralis",
        lambda: {
            "result": moralis_ohlcv(mint, timeframe, limit)
        },
    ))

    for provider, fetch in attempts:
        try:
            candles = normalize_candles(
                extract_ohlcv(fetch())
            )

            if len(candles) >= 2:
                logging.info(
                    "Candles %s %s via %s: %d",
                    mint, timeframe, provider, len(candles)
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

def observed_ath(mint, created_ts):
    if not created_ts:
        return None

    age = (now() - created_ts) / 86400

    if age > 175:
        return None

    candles = get_1d(mint)

    if not candles:
        return None

    if candles[0]["time"] > created_ts + 36 * 3600:
        return None

    return max(x["high"] for x in candles)


def ath_drop_filter(store, state):
    candidates = [
        item for item in store.values()
        if (
            (item.get("market_cap") or 0) >= MIN_MARKET_CAP
            and (item.get("price") or 0) > 0
            and item.get("created_ts")
        )
    ]

    candidates.sort(
        key=lambda x: x.get("market_cap") or 0,
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = int(state.get("ath_cursor", 0)) % len(candidates)

    batch = (
        candidates[cursor:] + candidates[:cursor]
    )[:ATH_CHECKS_PER_RUN]

    state["ath_cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    matches = 0

    for item in batch:
        mint = item["mint"]

        ath = observed_ath(
            mint,
            item.get("created_ts"),
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
            "created_ts": item["created_ts"],
            "registered_at": previous.get("registered_at") or now(),
            "url": item.get("url") or dex_url(mint),
        }

        message = (
            "🎯 ATH DROP DETECTED | DEX HUNTER V17\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {item['name']} ({item['symbol']})\n"
            f"💰 Price: ${price:.10g}\n"
            f"🏔 ATH: ${stored_ath:.10g}\n"
            f"📉 Drop: {drop:.2f}%\n"
            f"📊 Market Cap: ${(item.get('market_cap') or 0):,.0f}\n"
            f"💧 Liquidity: ${(item.get('liquidity') or 0):,.0f}\n"
            f"🧾 Contract:\n{mint}\n"
            f"🔗 {item.get('url') or dex_url(mint)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "👁 Token registered for recovery monitoring.\n"
            "⚠️ Analytical alert only."
        )

        if not all(
            f"{mint}|{chat}" in state["sent"]["ath"]
            for chat in CHAT_IDS
        ):
            if deliver(state, "ath", mint, message):
                matches += 1

        time.sleep(0.4)

    write_json(SEEN_FILE, state)

    return matches


# =========================================================
# LIVE TOKEN DATA
# =========================================================

def live_token(mint):
    payload = get_json(
        f"{DEX}/tokens/v1/solana/{mint}"
    )

    if not isinstance(payload, list):
        return None

    valid = []

    for pair in payload:
        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != "solana":
            continue

        base = pair.get("baseToken") or {}

        if base.get("address") != mint:
            continue

        price = number(pair.get("priceUsd"))

        if not price or price <= 0:
            continue

        liquidity = number(
            (pair.get("liquidity") or {}).get("usd")
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
    """
    Maximum technical score: 80.

    ATH distance: 15
    Bottom formation: 20
    Structure change: 20
    Resistance breakout: 15
    Liquidity: 10

    IMPORTANT:
    Score is informational only.
    There is NO minimum score for introducing a token.
    """

    if len(candles) < 12:
        return None

    price = item.get("price") or candles[-1]["close"]

    if not price or price <= 0:
        return None

    ath = number(item.get("ath"))

    if not ath or ath <= 0:
        ath = max(x["high"] for x in candles)

    drop = max(0, (ath - price) / ath * 100)

    score = 0
    details = {}

    # 1. ATH DISTANCE: 15 POINTS

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

    # 2. BOTTOM FORMATION: 20 POINTS

    recent = candles[-6:]
    previous = candles[-12:-6]

    recent_low = min(x["low"] for x in recent)
    previous_low = min(x["low"] for x in previous)

    no_new_low = recent_low >= previous_low * 0.985

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

    # 3. STRUCTURE CHANGE: 20 POINTS

    last = candles[-1]
    old = candles[-7:-1]

    old_high = max(x["high"] for x in old)
    old_low = min(x["low"] for x in old)

    structure_score = 0

    if last["close"] > old_high:
        structure_score += 12

    if last["low"] > old_low:
        structure_score += 4

    if last["close"] > last["open"]:
        structure_score += 4

    score += structure_score
    details["structure"] = structure_score

    # 4. RESISTANCE BREAKOUT: 15 POINTS

    resistance = max(
        x["high"] for x in candles[-13:-1]
    )

    breakout_score = 0

    if last["close"] > resistance:
        breakout_score = 15
    elif last["high"] > resistance:
        breakout_score = 7

    score += breakout_score
    details["breakout"] = breakout_score

    # 5. LIQUIDITY: 10 POINTS

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

    # PHASE CLASSIFICATION
    # This is based on price structure, not minimum score.

    if last["close"] > resistance:
        phase = "CONFIRMED REVERSAL"

    elif structure_score >= 12:
        phase = "RECOVERY"

    elif bottom_score >= 10:
        phase = "BOTTOM FORMING"

    else:
        phase = "FALLING / NO CONFIRMATION"

    # ALERT CLASSIFICATION
    # No score threshold blocks introduction.

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
        urgency = "قیمت مقاومت اخیر را با کندل بسته‌شده شکسته است."

    elif result["stage"] == "RECOVERY":
        heading = "🟢 RECOVERY SIGNAL"
        urgency = "نشانه‌های تغییر ساختار و شروع بازگشت مشاهده شده است."

    else:
        heading = "👁 WATCH ALERT"
        urgency = "ارز شناسایی شده؛ هنوز بازگشت آن تأیید نشده است."

    liquidity_text = (
        f"${result['liquidity']:,.0f}"
        if result["liquidity"] is not None
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
        f"🏔 ATH: ${result['ath']:.10g}\n"
        f"📉 Drop from ATH: {result['drop']:.2f}%\n"
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
        f"🧾 Contract:\n{item['mint']}\n"
        f"🔗 Chart: {item.get('url') or dex_url(item['mint'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ هیچ حداقل امتیازی برای معرفی ارز وجود ندارد.\n"
        "⚠️ امتیاز فقط برای ارزیابی کیفیت فرصت است.\n"
        "⚠️ امنیت قرارداد و تعداد هولدرها در این نسخه بررسی نشده‌اند.\n"
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
            if elapsed >= hours
            and label not in signal.get("snapshots", {})
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
                "change_percent": (price / entry - 1) * 100,
                "recorded_at": now(),
            }


# =========================================================
# RECOVERY SCANNER
# =========================================================

def recovery_scanner(store, state, recovery, performance):
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
            }

            live = live_token(mint)

            if live:
                item.update(live)

        else:
            item["ath"] = info.get("ath")

        if item.get("price"):
            candidates.append(item)
            existing.add(mint)

    # New candidates are also examined independently.
    # No score filter and no volume filter.

    for item in store.values():
        if (
            item["mint"] not in existing
            and (item.get("market_cap") or 0) >= MIN_MARKET_CAP
            and (item.get("price") or 0) > 0
        ):
            candidates.append(item)
            existing.add(item["mint"])

    if not candidates:
        return 0

    cursor = int(recovery.get("cursor", 0)) % len(candidates)

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

        result = analyze_recovery(item, candles)

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

        previous = entry["last_alert"].get(category, 0)

        if now() - previous < cooldown:
            entry["last_stage"] = stage
            continue

        rank = {
            "WATCH": 1,
            "RECOVERY": 2,
            "STRONG BUY": 3,
        }

        previous_stage = entry.get("highest_stage")

        # A lower alert is not sent after a higher stage
        # has already been reached during the same day.

        if (
            previous_stage
            and rank.get(stage, 0) < rank.get(previous_stage, 0)
        ):
            entry["last_stage"] = stage
            continue

        message = recovery_message(item, result)

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

        distance = (price / ath - 1) * 100

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

        if deliver(state, "ath_reclaim", mint, message):
            alerts += 1

    return alerts


# =========================================================
# ATH BREAKOUT AFTER 75% DROP
# =========================================================

def find_breakout(candles, created_ts):
    if len(candles) < 10:
        return None

    if (
        created_ts
        and candles[0]["time"] > created_ts + 36 * 3600
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
        x for x in store.values()
        if x.get("created_ts") and x.get("price")
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

        age = (now() - item["created_ts"]) / 86400

        if age <= 0 or age > BREAKOUT_MAX_AGE_DAYS:
            continue

        candles = get_1d(mint)

        result = find_breakout(
            candles,
            item["created_ts"],
        )

        if not result:
            continue

        if item["price"] <= result["ath"]:
            continue

        message = (
            "🚀 ATH BREAKOUT AFTER 75% DROP\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {item['name']} ({item['symbol']})\n"
            f"🏔 Previous ATH: ${result['ath']:.10g}\n"
            f"📉 Drawdown low: ${result['low']:.10g}\n"
            f"💥 Breakout close: ${result['breakout']:.10g}\n"
            f"💰 Current price: ${item['price']:.10g}\n"
            f"🧾 Contract:\n{mint}\n"
            f"🔗 {item.get('url') or dex_url(mint)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Confirmation requires a daily candle close above ATH."
        )

        if deliver(state, "breakout", mint, message):
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

        response.raise_for_status()
        payload = response.json()

        if payload.get("ok"):
            return payload.get("result", [])

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
    candles = chart_candles(mint, timeframe)

    if not candles:
        return None

    CHART_DIR.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(
        figsize=(13, 7),
        dpi=150,
    )

    dates = [
        mdates.date2num(
            datetime.fromtimestamp(
                x["time"],
                timezone.utc,
            )
        )
        for x in candles
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
            height = max(op * 0.00001, 1e-12)

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

    ax.grid(True, alpha=0.18)
    ax.tick_params(axis="x", rotation=25)
    ax.margins(x=0.02, y=0.08)

    fig.tight_layout()

    safe = re.sub(
        r"[^A-Za-z0-9_-]",
        "_",
        mint,
    )

    filename = CHART_DIR / f"{safe}_{timeframe}.png"

    try:
        fig.savefig(
            filename,
            bbox_inches="tight",
        )

    except Exception:
        logging.exception("Chart creation failed")
        plt.close(fig)
        return None

    plt.close(fig)

    return filename


def process_forwarded(state):
    offset = int(state.get("telegram_update_offset", 0))
    updates = get_updates(offset)

    allowed = set(CHAT_IDS)
    processed = 0

    for update in updates:
        update_id = update.get("update_id")

        if update_id is None:
            continue

        message = update.get("message") or {}
        chat = message.get("chat") or {}

        chat_id = str(chat.get("id", ""))

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

    # DAILY RESET
    daily_reset(state, recovery)

    logging.info(
        "%s scan started | UTC day=%s",
        VERSION,
        utc_day(),
    )

    # 1. Telegram forwarded charts
    process_forwarded(state)

    # 2. Discover tokens from all sources
    store = {}

    source_dexscreener(store)
    source_gecko(store)
    source_pump(store)
    source_stonk(store)
    source_void(store)

    logging.info(
        "Discovered unique tokens: %d",
        len(store),
    )

    # 3. Enrich candidate information
    enrich(store)

    # 4. Filter one: ATH DROP
    ath_count = ath_drop_filter(
        store,
        state,
    )

    # 5. Filter two: SMART RECOVERY
    # No minimum score blocks a token.
    recovery_count = recovery_scanner(
        store,
        state,
        recovery,
        performance,
    )

    # 6. ATH BREAKOUT
    breakout_count = breakout_scanner(
        store,
        state,
    )

    # 7. ATH RECLAIM
    reclaim_count = ath_reclaim_scanner(state)

    # 8. Performance tracking
    update_performance(performance)

    # 9. Save databases
    write_json(SEEN_FILE, state)
    write_json(RECOVERY_FILE, recovery)
    write_json(PERFORMANCE_FILE, performance)

    logging.info(
        "%s COMPLETE | tokens=%d | ATH=%d | "
        "RECOVERY=%d | BREAKOUT=%d | RECLAIM=%d | "
        "tracked=%d | performance=%d",
        VERSION,
        len(store),
        ath_count,
        recovery_count,
        breakout_count,
        reclaim_count,
        len(recovery["tokens"]),
        len(performance["signals"]),
    )


if __name__ == "__main__":
    main()
