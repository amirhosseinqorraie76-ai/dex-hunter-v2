
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V14

SOURCES:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun

FILTER 1: ATH DROP
Market cap >= $50,000
Price >= 30% below observed ATH

FILTER 2: VOLUME
Token age > 1 day
24h volume >= $500,000

FILTER 3: ATH BREAKOUT
Previous ATH identified
Price drops >= 75% below ATH
Later candle high breaks same ATH
Close above ATH NOT required

NEW FILTER 4: ATH RECLAIM BUY
Only tokens previously alerted by ATH DROP.
Persistent tracking in seen.json.
Price >= 99.5% of stored ATH.
Independent DexScreener monitoring.
One BUY alert per token per Telegram chat.

Alerts only. No automatic trading.
"""

import json
import logging
import os
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests


# ==================================================
# CONFIG
# ==================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

CHAT_IDS = [
    "6665909136",
    "6765810776",
    "98529959",
    "1507608801",
    "1012061523",
]

# FILTER 1

ATH_MIN_MARKET_CAP_USD = 50_000
ATH_MIN_DROP_PERCENT = 30.0

# FILTER 2

VOLUME_MIN_AGE_DAYS = 1
VOLUME_MIN_24H_USD = 500_000

# FILTER 3

BREAKOUT_DROP_PERCENT = 75.0
BREAKOUT_HISTORY_LIMIT = 1000
BREAKOUT_MAX_AGE_DAYS = 990

# FILTER 4 - NEW BUY ALERT

BUY_ATH_THRESHOLD_PERCENT = 99.5
BUY_CHECKS_PER_RUN = 30
BUY_ALERT_ENABLED = True

STATE_FILE = Path("seen.json")

MAX_SOURCE_ROWS = 100
MAX_ATH_CHECKS_PER_RUN = 5
MAX_BREAKOUT_CHECKS_PER_RUN = 5

HTTP_TIMEOUT = 20

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
PUMP = "https://frontend-api-v3.pump.fun"
STONK = "https://www.stonkfun.xyz/api/public/v1"

HEADERS = {
    "User-Agent": "DEX-HUNTER-V14/1.0",
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

def get_json(url, params=None):

    try:

        response = session.get(
            url,
            params=params,
            timeout=HTTP_TIMEOUT,
        )

        if response.status_code == 429:
            logging.warning("Rate limited: %s", url)
            return None

        response.raise_for_status()

        return response.json()

    except Exception as exc:

        logging.warning(
            "Request failed %s: %s",
            url,
            exc,
        )

        return None


def as_float(value):

    try:

        if value is None or value == "":
            return None

        result = float(value)

        if result != result:
            return None

        if result in (
            float("inf"),
            float("-inf"),
        ):
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
                parsed = parsed.replace(
                    tzinfo=timezone.utc
                )

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

        return [
            x for x in payload
            if isinstance(x, dict)
        ]

    if not isinstance(payload, dict):
        return []

    for key in (
        "tokens",
        "coins",
        "items",
        "results",
        "pools",
        "data",
    ):

        value = payload.get(key)

        if isinstance(value, list):

            return [
                x for x in value
                if isinstance(x, dict)
            ]

        if isinstance(value, dict):

            for nested in (
                "tokens",
                "coins",
                "items",
                "results",
                "pools",
            ):

                inner = value.get(nested)

                if isinstance(inner, list):

                    return [
                        x for x in inner
                        if isinstance(x, dict)
                    ]

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
    }


def load_state():

    if not STATE_FILE.exists():
        return empty_state()

    try:

        state = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        default = empty_state()

        state.setdefault("sent", {})

        for key in default["sent"]:
            state["sent"].setdefault(key, [])

        state.setdefault("ath_watch", {})

        state.setdefault("ath_cursor", 0)
        state.setdefault("breakout_cursor", 0)
        state.setdefault("buy_cursor", 0)

        return state

    except Exception:

        logging.exception("State loading failed")

        return empty_state()


def save_state(state):

    temp = STATE_FILE.with_suffix(".tmp")

    temp.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(STATE_FILE)


# ==================================================
# TELEGRAM
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

        logging.warning(
            "Telegram failed %s: %s",
            chat_id,
            exc,
        )

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

            logging.warning(
                "Telegram retry required: %s",
                key,
            )

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
            if isinstance(base_id, str)
            and "_" in base_id
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

    volume_data = first(
        attrs.get("volume_usd"),
        row.get("volume"),
        row.get("volume_usd"),
        {},
    )

    if not isinstance(volume_data, dict):
        volume_data = {}

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

        "volume_24h": as_float(first(
            row.get("volume24h"),
            row.get("volume_24h"),
            row.get("volume24hUsd"),
            row.get("volume_24h_usd"),
            row.get("volumeUsd24h"),
            volume_data.get("h24"),
            volume_data.get("24h"),
        )),

        "created_ts": parse_timestamp(created),

        "pair_created_ts": parse_timestamp(first(
            row.get("pairCreatedAt"),
            row.get("pair_created_at"),
            attrs.get("pair_created_at"),
            attrs.get("pool_created_at"),
        )),

        "dex_url": first(
            row.get("url"),
            attrs.get("url"),
        ),

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

    new_volume = candidate.get("volume_24h")
    old_volume = old.get("volume_24h")

    if new_volume is not None:

        if old_volume is None or new_volume > old_volume:
            old["volume_24h"] = new_volume


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
            payload
            if isinstance(payload, list)
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

    for endpoint in (
        "new_pools",
        "trending_pools",
    ):

        payload = get_json(
            f"{GECKO}/networks/solana/{endpoint}",
            {"page": 1},
        )

        for row in rows_from(payload)[:MAX_SOURCE_ROWS]:

            rels = row.get("relationships") or {}

            base_data = (
                (rels.get("base_token") or {}).get("data")
                or {}
            )

            base_id = base_data.get("id")

            mint = (
                base_id.split("_", 1)[1]
                if isinstance(base_id, str)
                and "_" in base_id
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
                        "volume": attrs.get("volume_usd"),
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

        add_candidate(
            store,
            normalize(row, "Pump.fun"),
        )


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

        add_candidate(
            store,
            normalize(row, "Stonk.fun"),
        )


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

            pair_volume = as_float(
                (pair.get("volume") or {}).get("h24")
            )

            if pair_volume is not None:

                old_volume = candidate.get("volume_24h")

                if old_volume is None or pair_volume > old_volume:
                    candidate["volume_24h"] = pair_volume

            pair_created = parse_timestamp(
                pair.get("pairCreatedAt")
            )

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

        liquidity = as_float(
            attrs.get("reserve_in_usd")
        ) or 0.0

        if pool_address and liquidity > best_liquidity:

            best_pool = pool_address
            best_side = side
            best_liquidity = liquidity

    return best_pool, best_side


# ==================================================
# DAILY CANDLES
# ==================================================

def get_daily_candles(mint, limit=1000):

    pool, side = gecko_best_pool(mint)

    if not pool or not side:
        return None, None, "استخر مناسب پیدا نشد"

    payload = get_json(
        f"{GECKO}/networks/solana/pools/{pool}/ohlcv/day",
        {
            "aggregate": 1,
            "limit": limit,
            "currency": "usd",
            "token": side,
        },
    )

    try:
        candles = payload["data"]["attributes"]["ohlcv_list"]
    except Exception:
        return None, None, "کندل روزانه در دسترس نیست"

    valid = []

    for candle in candles:

        if not isinstance(candle, list) or len(candle) < 5:
            continue

        ts = as_float(candle[0])
        high = as_float(candle[2])
        low = as_float(candle[3])

        if (
            ts is None
            or high is None
            or low is None
            or high <= 0
            or low <= 0
        ):
            continue

        valid.append({
            "time": ts,
            "high": high,
            "low": low,
        })

    if not valid:
        return None, None, "کندل معتبر پیدا نشد"

    valid.sort(key=lambda x: x["time"])

    return valid, pool, "GeckoTerminal daily candles"


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
# TOKEN AGE
# ==================================================

def token_age_info(candidate):

    created = candidate.get("created_ts")

    if created:

        age = max(
            0.0,
            (time.time() - created) / 86400,
        )

        return age, "زمان ایجاد توکن"

    pair_created = candidate.get("pair_created_ts")

    if pair_created:

        age = max(
            0.0,
            (time.time() - pair_created) / 86400,
        )

        return age, "عمر تخمینی بر اساس زمان ایجاد استخر"

    return None, None


# ==================================================
# FILTER 2: VOLUME
# ==================================================

def volume_filter(store, state):

    matches = 0

    for candidate in store.values():

        age, age_note = token_age_info(candidate)

        volume = candidate.get("volume_24h")

        if age is None or age <= VOLUME_MIN_AGE_DAYS:
            continue

        if volume is None or volume < VOLUME_MIN_24H_USD:
            continue

        mint = candidate["mint"]

        if all(
            f"{mint}|{chat}" in state["sent"]["volume"]
            for chat in CHAT_IDS
        ):
            continue

        market_cap = candidate.get("market_cap")

        market_cap_text = (
            f"${market_cap:,.0f}"
            if market_cap is not None
            else "نامشخص"
        )

        url = candidate.get("dex_url") or dex_url(mint)

        message = (
            "🔥 شکار حجم معاملات | DEX HUNTER V14\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 توکن: {candidate['name']} ({candidate['symbol']})\n"
            f"⏳ عمر: {age:.1f} روز\n"
            f"🕒 مبنای عمر: {age_note}\n"
            f"📦 حجم ۲۴ ساعته: ${volume:,.0f}\n"
            f"📊 مارکت‌کپ: {market_cap_text}\n"
            f"🔎 منابع: {', '.join(sorted(candidate['sources']))}\n"
            f"🧾 قرارداد: {mint}\n"
            f"📈 نمودار: {url}\n"
            "✅ شرط: عمر بالای یک روز و حجم حداقل ۵۰۰ هزار دلار\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید."
        )

        deliver(state, "volume", mint, message)

        matches += 1

    return matches


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

        # NEW: Persistent ATH tracking.
        # Keep the highest recorded ATH for this token.

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

        # Do not repeat the original ATH drop alert.

        if all(
            f"{mint}|{chat}" in state["sent"]["ath"]
            for chat in CHAT_IDS
        ):
            continue

        message = (
            "🎯 شکار ریزش از سقف تاریخی | DEX HUNTER V14\n"
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
# FILTER 3: ATH BREAKOUT
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
                and low <= peak * (
                    1 - BREAKOUT_DROP_PERCENT / 100
                )
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
            (reference_ath - drawdown_low)
            / reference_ath
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
        key=lambda x: (
            x.get("liquidity") or 0,
            x.get("volume_24h") or 0,
        ),
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
            "🚀 شکست سقف تاریخی پس از ریزش ۷۵٪ | DEX HUNTER V14\n"
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
# NEW: DEXSCREENER LIVE PRICE
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

    # Highest-liquidity pair is selected.

    valid_pairs.sort(
        key=lambda x: x["liquidity"],
        reverse=True,
    )

    return valid_pairs[0]


# ==================================================
# NEW FILTER 4: ATH RECLAIM BUY ALERT
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

        # Check whether all Telegram users
        # already received this BUY alert.

        if all(
            f"{mint}|{chat}" in state["sent"]["ath_reclaim"]
            for chat in CHAT_IDS
        ):
            continue

        live = get_live_token_data(mint)

        if not live:
            continue

        price = live["price"]

        threshold = ath * (
            BUY_ATH_THRESHOLD_PERCENT / 100
        )

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

        deliver(
            state,
            "ath_reclaim",
            mint,
            message,
        )

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
# MIGRATION: OLD V13 ATH ALERTS
# ==================================================

def migrate_old_ath_alerts(store, state):

    """
    Old V13 sent.ath records did not store ATH.
    Recover ATH for previously alerted tokens
    when they are still present in current sources.
    """

    known = state["ath_watch"]

    old_mints = set()

    for item in state["sent"]["ath"]:

        if "|" in item:
            old_mints.add(item.split("|", 1)[0])

    missing = [
        mint for mint in old_mints
        if mint not in known
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

        known[mint] = {
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


# ==================================================
# MAIN
# ==================================================

def main():

    if not BOT_TOKEN:

        raise SystemExit(
            "Missing GitHub Secret: TELEGRAM_BOT_TOKEN"
        )

    state = load_state()

    candidates = {}

    logging.info("DEX HUNTER V14 scan started")

    # FOUR ORIGINAL SOURCES

    source_dexscreener(candidates)
    source_geckoterminal(candidates)
    source_pump_fun(candidates)
    source_stonk_fun(candidates)

    logging.info(
        "Discovered %d unique tokens",
        len(candidates),
    )

    enrich_from_dexscreener(candidates)

    # RESTORE OLD V13 TRACKING WHEN POSSIBLE

    migrate_old_ath_alerts(candidates, state)

    with_age = sum(
        1 for c in candidates.values()
        if token_age_info(c)[0] is not None
    )

    with_volume = sum(
        1 for c in candidates.values()
        if c.get("volume_24h") is not None
    )

    with_market_cap = sum(
        1 for c in candidates.values()
        if c.get("market_cap") is not None
    )

    # FILTER 2

    volume_count = volume_filter(
        candidates,
        state,
    )

    # FILTER 1

    ath_count = ath_filter(
        candidates,
        state,
    )

    # FILTER 3

    breakout_count = ath_breakout_filter(
        candidates,
        state,
    )

    # FILTER 4 - NEW

    buy_count = ath_reclaim_filter(
        state,
    )

    save_state(state)

    logging.info(
        "V14 COMPLETE | "
        "tokens=%d | "
        "age=%d | "
        "volume_data=%d | "
        "market_cap=%d | "
        "volume_alerts=%d | "
        "ath_alerts=%d | "
        "breakout_alerts=%d | "
        "buy_alerts=%d | "
        "tracked_ath_tokens=%d",
        len(candidates),
        with_age,
        with_volume,
        with_market_cap,
        volume_count,
        ath_count,
        breakout_count,
        buy_count,
        len(state["ath_watch"]),
    )


if __name__ == "__main__":
    main()
