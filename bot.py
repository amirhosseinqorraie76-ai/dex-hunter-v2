#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V16 + MULTI-SOURCE CANDLES + TELEGRAM FORWARDED-POST CHARTS

SOURCES:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun
5. VoidScreener (optional; skipped automatically if unavailable)

FILTER 1: ATH DROP
Market cap >= $50,000
Price >= 30% below observed ATH

FILTER 2: ATH BREAKOUT
Previous ATH identified
Price drops >= 75% below ATH
Later daily candle high breaks same ATH

FILTER 3: ATH RECLAIM BUY
Only tokens previously registered by ATH DROP.
Persistent tracking in seen.json.
Price >= 99.5% of stored ATH.
Independent DexScreener monitoring.
One BUY alert per token per Telegram chat.

VOLUME FILTER: REMOVED COMPLETELY.

CANDLE PROVIDERS (fallback order): GeckoTerminal, CoinGecko Onchain, Moralis.

NEW:
Forward a Telegram channel post to this bot in a private chat.
If a Solana contract address is found, send only:
- 4-hour candlestick chart
- Daily candlestick chart

Charts contain candles only: no indicators, RSI, MACD, or volume.
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


# ==================================================
# CONFIG
# ==================================================

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

# FILTER 1: ATH DROP
ATH_MIN_MARKET_CAP_USD = 50_000
ATH_MIN_DROP_PERCENT = 30.0

# FILTER 2: ATH BREAKOUT
BREAKOUT_DROP_PERCENT = 75.0
BREAKOUT_HISTORY_LIMIT = 1000
BREAKOUT_MAX_AGE_DAYS = 990

# FILTER 3: ATH RECLAIM BUY
BUY_ATH_THRESHOLD_PERCENT = 99.5
BUY_CHECKS_PER_RUN = 30
BUY_ALERT_ENABLED = True

STATE_FILE = Path("seen.json")

MAX_SOURCE_ROWS = 100
MAX_ATH_CHECKS_PER_RUN = 5
MAX_BREAKOUT_CHECKS_PER_RUN = 5

HTTP_TIMEOUT = 20

COINGECKO_ONCHAIN = "https://api.coingecko.com/api/v3/onchain"
MORALIS_SOLANA = "https://solana-gateway.moralis.io"
VOID = "https://api.voidscreener.com"

# Telegram incoming-message settings
TELEGRAM_UPDATES_LIMIT = 30

# Chart settings
CHART_4H_LIMIT = 100
CHART_1D_LIMIT = 100
CHART_OUTPUT_DIR = Path("charts")

# Solana base58 address. Address is verified against DexScreener afterwards.
SOLANA_ADDRESS_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
PUMP = "https://frontend-api-v3.pump.fun"
STONK = "https://www.stonkfun.xyz/api/public/v1"

HEADERS = {
    "User-Agent": "DEX-HUNTER-V16/1.0",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update(HEADERS)


# ==================================================
# HELPERS
# ==================================================

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


# ==================================================
# STATE
# ==================================================

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


def load_state():

    if not STATE_FILE.exists():
        return empty_state()

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

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

    except Exception:
        logging.exception("State loading failed")
        return empty_state()


def save_state(state):

    temp = STATE_FILE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp.replace(STATE_FILE)


# ==================================================
# TELEGRAM OUTGOING
# ==================================================

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


# ==================================================
# NORMALIZATION
# ==================================================

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
        "name", "symbol", "price", "market_cap",
        "created_ts", "pair_created_ts", "dex_url",
    ):
        if (
            old.get(field) is None
            or old.get(field) in ("Unknown", "???")
        ) and candidate.get(field) is not None:
            old[field] = candidate[field]


# ==================================================
# SOURCE 1: DEXSCREENER
# ==================================================

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


# ==================================================
# SOURCE 2: GECKOTERMINAL
# ==================================================

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


# ==================================================
# SOURCE 3: PUMP.FUN
# ==================================================

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


# ==================================================
# SOURCE 4: STONK.FUN
# ==================================================

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


# ==================================================
# SOURCE 5: VOIDScreener (OPTIONAL)
# ==================================================

def source_voidscreener(store):
    """Best-effort discovery. VoidScreener outages never stop the scan."""
    endpoints = (
        (f"{VOID}/api-trending", {"chain": "solana"}),
        (f"{VOID}/api-tokens", {"chain": "solana", "limit": MAX_SOURCE_ROWS}),
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



# ==================================================
# DEXSCREENER ENRICHMENT
# ==================================================

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


# ==================================================
# GECKO POOL SELECTION
# ==================================================

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

        liquidity = as_float(attrs.get("reserve_in_usd")) or 0.0

        if pool_address and liquidity > best_liquidity:
            best_pool = pool_address
            best_side = side
            best_liquidity = liquidity

    return best_pool, best_side


# ==================================================
# DAILY CANDLES (ORIGINAL FILTER LOGIC)
# ==================================================

def _extract_ohlcv_rows(payload):
    """Normalize common GeckoTerminal/CoinGecko/Moralis OHLCV payloads."""
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
            ts = first(item.get("timestamp"), item.get("time"), item.get("t"), item.get("from"))
            op = first(item.get("open"), item.get("o"))
            hi = first(item.get("high"), item.get("h"))
            lo = first(item.get("low"), item.get("l"))
            cl = first(item.get("close"), item.get("c"))
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            ts, op, hi, lo, cl = item[:5]
        else:
            continue
        ts = parse_timestamp(ts)
        op, hi, lo, cl = map(as_float, (op, hi, lo, cl))
        if ts is None or any(v is None or v <= 0 for v in (op, hi, lo, cl)):
            continue
        row = {"time": ts, "high": hi, "low": lo}
        if include_ohlc:
            row.update({
                "open": op, "close": cl,
                "datetime": datetime.fromtimestamp(ts, timezone.utc),
            })
        output.append(row)
    output.sort(key=lambda x: x["time"])
    return output


def _coingecko_pool_ohlcv(pool, timeframe, aggregate, limit, side="base"):
    headers = {"accept": "application/json"}
    if COINGECKO_DEMO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_DEMO_API_KEY
    payload = get_json(
        f"{COINGECKO_ONCHAIN}/networks/solana/pools/{pool}/ohlcv/{timeframe}",
        {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": side},
        headers=headers,
    )
    return _extract_ohlcv_rows(payload)


def _moralis_pair_addresses(mint):
    if not MORALIS_API_KEY:
        return []
    headers = {"accept": "application/json", "X-API-Key": MORALIS_API_KEY}
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
        address = first(row.get("pairAddress"), row.get("pair_address"), row.get("address"))
        if address:
            pairs.append((address, as_float(first(row.get("liquidityUsd"), row.get("liquidity_usd"), row.get("liquidity"))) or 0))
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in pairs[:3]]


def _moralis_ohlcv(mint, timeframe, limit):
    if not MORALIS_API_KEY:
        return []
    headers = {"accept": "application/json", "X-API-Key": MORALIS_API_KEY}
    tf = "4h" if timeframe == "hour" else "1d"
    for pair in _moralis_pair_addresses(mint):
        payload = get_json(
            f"{MORALIS_SOLANA}/token/mainnet/pairs/{pair}/ohlcv",
            {"timeframe": tf, "currency": "usd", "limit": limit},
            headers=headers,
        )
        rows = _extract_ohlcv_rows(payload)
        if rows:
            return rows
    return []


def get_daily_candles(mint, limit=1000):
    pool, side = gecko_best_pool(mint)
    providers = []
    if pool and side:
        providers.append(("GeckoTerminal", lambda: get_json(
            f"{GECKO}/networks/solana/pools/{pool}/ohlcv/day",
            {"aggregate": 1, "limit": limit, "currency": "usd", "token": side},
        )))
        providers.append(("CoinGecko Onchain", lambda: {"data": {"attributes": {"ohlcv_list": _coingecko_pool_ohlcv(pool, "day", 1, limit, side)}}}))
    providers.append(("Moralis", lambda: {"result": _moralis_ohlcv(mint, "day", limit)}))
    for provider, fetch in providers:
        candles = _normalize_candles(_extract_ohlcv_rows(fetch()))
        if candles:
            return candles, pool, f"{provider} daily candles"
    return None, pool, "کندل روزانه از هیچ‌یک از منابع در دسترس نبود"


# ==================================================
# OBSERVED ATH
# ==================================================

def observed_ath(mint, created_ts):

    if not created_ts:
        return None, "زمان ایجاد توکن مشخص نیست"

    age_days = (time.time() - created_ts) / 86400

    if age_days > 175:
        return None, "تاریخچه کامل ATH در دسترس نیست"

    candles, pool, note = get_daily_candles(mint, 1000)

    if not candles:
        return None, note

    if candles[0]["time"] > created_ts + 36 * 3600:
        return None, "تاریخچه از ابتدای عمر توکن شروع نمی‌شود"

    ath = max(c["high"] for c in candles)

    return ath, "ATH مشاهده‌شده از کندل‌های روزانه GeckoTerminal"


# ==================================================
# FILTER 1: ATH DROP
# ==================================================

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

    state["ath_cursor"] = (cursor + len(batch)) % len(candidates)
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

        if all(
            f"{mint}|{chat}" in state["sent"]["ath"]
            for chat in CHAT_IDS
        ):
            continue

        message = (
            "🎯 شکار ریزش از سقف تاریخی | DEX HUNTER V15\n"
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
            "✅ شرط: مارکت‌کپ حداقل ۵۰ هزار دلار + افت حداقل ۳۰٪\n"
            "👁 این توکن برای هشدار بازگشت به ATH ذخیره شد.\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید."
        )

        deliver(state, "ath", mint, message)
        matches += 1
        time.sleep(1)

    return matches


# ==================================================
# FILTER 2: ATH BREAKOUT
# ==================================================

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
        key=lambda x: (x.get("liquidity") or 0),
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = int(state.get("breakout_cursor", 0)) % len(candidates)
    ordered = candidates[cursor:] + candidates[:cursor]
    batch = ordered[:MAX_BREAKOUT_CHECKS_PER_RUN]

    state["breakout_cursor"] = (cursor + len(batch)) % len(candidates)
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

        result = find_ath_breakout(candles, created_ts)

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
            "🚀 شکست سقف تاریخی پس از ریزش ۷۵٪ | DEX HUNTER V15\n"
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
            "✅ شرط: ریزش ۷۵٪ و شکست مجدد ATH قبلی\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید."
        )

        deliver(state, "ath_breakout", mint, message)
        matches += 1
        time.sleep(1)

    return matches


# ==================================================
# DEXSCREENER LIVE PRICE
# ==================================================

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


# ==================================================
# FILTER 3: ATH RECLAIM BUY ALERT
# ==================================================

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

    state["buy_cursor"] = (cursor + len(batch)) % len(mints)
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
            f"🔗 نمودار و بررسی خرید:\n{chart}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ این هشدار صرفاً بازگشت قیمت به محدوده ATH را نشان می‌دهد؛ "
            "تضمین شکست پایدار یا سود نیست.\n"
            "⚠️ نقدینگی، اسلیپیج و امنیت قرارداد را بررسی کنید."
        )

        deliver(state, "ath_reclaim", mint, message)
        matches += 1

        logging.info(
            "ATH RECLAIM BUY ALERT: %s | price=%s | ATH=%s",
            mint,
            price,
            ath,
        )

        time.sleep(0.5)

    return matches


# ==================================================
# MIGRATION: OLD V13/V14 ATH ALERTS
# ==================================================

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

        ath, note = observed_ath(mint, created_ts)

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

    logging.info("Legacy ATH records recovered: %d", recovered)

    return recovered


# ==================================================
# TELEGRAM INCOMING: FORWARDED POSTS
# ==================================================

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
            logging.warning("Telegram getUpdates returned not-ok: %s", payload)
            return []

        return payload.get("result", [])

    except Exception as exc:
        logging.warning("Telegram getUpdates failed: %s", exc)
        return []


def is_forwarded_message(message):

    # Telegram Bot API newer and older forward metadata fields.
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

    matches = SOLANA_ADDRESS_PATTERN.findall(text_content)

    for mint in matches:

        if not mint_looks_valid(mint):
            continue

        # Confirm that this address is actually a Solana token on DexScreener.
        token_data = get_live_token_data(mint)

        if token_data:
            return mint

    return None


def get_chart_candles(mint, timeframe, limit):
    if timeframe == "4h":
        endpoint, aggregate = "hour", 4
    elif timeframe == "1d":
        endpoint, aggregate = "day", 1
    else:
        return None

    pool, side = gecko_best_pool(mint)
    attempts = []
    if pool and side:
        attempts.append(("GeckoTerminal", lambda: get_json(
            f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{endpoint}",
            {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": side},
        )))
        # CoinGecko Demo pool endpoint uses the same pool address and OHLCV schema.
        attempts.append(("CoinGecko Onchain", lambda: {"data": {"attributes": {"ohlcv_list": _coingecko_pool_ohlcv(pool, endpoint, aggregate, limit, side)}}}))
    attempts.append(("Moralis", lambda: {"result": _moralis_ohlcv(mint, endpoint, limit)}))

    for provider, fetch in attempts:
        try:
            raw = _extract_ohlcv_rows(fetch())
            parsed = _normalize_candles(raw, include_ohlc=True)
            if len(parsed) >= 2:
                logging.info("Chart candles for %s %s: %s (%d)", mint, timeframe, provider, len(parsed))
                return [{
                    "time": candle["datetime"],
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                } for candle in parsed]
        except Exception:
            logging.exception("Candle provider failed: %s", provider)

    logging.warning("All candle providers failed for %s (%s)", mint, timeframe)
    return None


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

    CHART_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(13, 7), dpi=150)

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

        body_bottom = min(open_price, close)
        body_height = abs(close - open_price)

        if body_height == 0:
            body_height = max(open_price * 0.00001, 1e-12)

        ax.add_patch(
            Rectangle(
                (x - candle_width / 2, body_bottom),
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
        mdates.DateFormatter(date_format, tz=timezone.utc)
    )

    ax.grid(True, alpha=0.18)
    ax.tick_params(axis="x", rotation=25)
    ax.margins(x=0.02, y=0.08)

    fig.tight_layout()

    safe_mint = re.sub(r"[^A-Za-z0-9_-]", "_", mint)
    filename = CHART_OUTPUT_DIR / f"{safe_mint}_{timeframe}.png"

    try:
        fig.savefig(filename, bbox_inches="tight")
    except Exception:
        logging.exception("Chart image save failed")
        plt.close(fig)
        return None

    plt.close(fig)
    return filename


def process_forwarded_messages(state):

    offset = int(state.get("telegram_update_offset", 0))
    updates = get_telegram_updates(offset)

    processed = 0
    allowed_chats = set(CHAT_IDS)

    for update in updates:

        update_id = update.get("update_id")

        if update_id is None:
            continue

        # Advance the offset only after handling this update.
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))

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
                "❌ دریافت کندل‌های 4H یا 1D از منابع موجود ناموفق بود. "
                "ممکن است تاریخچه کافی نباشد یا APIهای جایگزین تنظیم/در دسترس نباشند.",
            )
        else:
            # Send only the two requested chart images.
            send_telegram_photo(chat_id, chart_4h)
            send_telegram_photo(chat_id, chart_1d)

        state["telegram_update_offset"] = update_id + 1
        save_state(state)
        processed += 1

        time.sleep(0.3)

    logging.info("Forwarded Telegram posts processed: %d", processed)
    return processed


# ==================================================
# MAIN
# ==================================================

def main():

    if not BOT_TOKEN:
        raise SystemExit(
            "Missing GitHub Secret: TELEGRAM_BOT_TOKEN"
        )

    state = load_state()

    logging.info("DEX HUNTER V16 scan started")

    # First, process posts forwarded to the bot.
    # This uses the same Bot Token and the persistent update offset.
    process_forwarded_messages(state)

    candidates = {}

    # FOUR ORIGINAL SOURCES
    source_dexscreener(candidates)
    source_geckoterminal(candidates)
    source_pump_fun(candidates)
    source_stonk_fun(candidates)
    source_voidscreener(candidates)

    logging.info(
        "Discovered %d unique tokens",
        len(candidates),
    )

    enrich_from_dexscreener(candidates)

    # RESTORE OLD ATH TRACKING WHEN POSSIBLE
    migrate_old_ath_alerts(candidates, state)

    with_market_cap = sum(
        1 for candidate in candidates.values()
        if candidate.get("market_cap") is not None
    )

    # FILTER 1: ATH DROP
    ath_count = ath_filter(candidates, state)

    # FILTER 2: ATH BREAKOUT
    breakout_count = ath_breakout_filter(candidates, state)

    # FILTER 3: ATH RECLAIM BUY
    buy_count = ath_reclaim_filter(state)

    save_state(state)

    logging.info(
        "V16 COMPLETE | "
        "tokens=%d | "
        "market_cap_data=%d | "
        "ath_alerts=%d | "
        "breakout_alerts=%d | "
        "buy_alerts=%d | "
        "tracked_ath_tokens=%d",
        len(candidates),
        with_market_cap,
        ath_count,
        breakout_count,
        buy_count,
        len(state["ath_watch"]),
    )


if __name__ == "__main__":
    main()