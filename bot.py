
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V15

SOURCES:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun

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

TELEGRAM CHART REQUEST:
Forward a Telegram channel post to the bot.
Extract Solana contract address.
Send exactly two candlestick charts:
4H and 1D.
No API_ID, API_HASH or Telethon.

VOLUME FILTER: REMOVED COMPLETELY.
Alerts only. No automatic trading.
"""

import io
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
from matplotlib.patches import Rectangle


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

ATH_MIN_MARKET_CAP_USD = 50_000
ATH_MIN_DROP_PERCENT = 30.0

BREAKOUT_DROP_PERCENT = 75.0
BREAKOUT_HISTORY_LIMIT = 1000
BREAKOUT_MAX_AGE_DAYS = 990

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
    "User-Agent": "DEX-HUNTER-V15/1.0",
    "Accept": "application/json",
}

BASE58_PATTERN = re.compile(
    r"(?<![1-9A-HJ-NP-Za-km-z])"
    r"[1-9A-HJ-NP-Za-km-z]{32,44}"
    r"(?![1-9A-HJ-NP-Za-km-z])"
)

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

    for key in (
        "tokens", "coins", "items",
        "results", "pools", "data",
    ):

        value = payload.get(key)

        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

        if isinstance(value, dict):

            for nested in (
                "tokens", "coins", "items",
                "results", "pools",
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
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(STATE_FILE)


# ==================================================
# TELEGRAM MESSAGE
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


def send_telegram_photo(chat_id, image_buffer):

    if not BOT_TOKEN:
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendPhoto"
    )

    try:
        image_buffer.seek(0)

        response = session.post(
            url,
            data={"chat_id": chat_id},
            files={
                "photo": (
                    "chart.png",
                    image_buffer,
                    "image/png",
                )
            },
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()
        return bool(response.json().get("ok"))

    except Exception as exc:
        logging.warning("Telegram photo failed: %s", exc)
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
# DAILY CANDLES FOR ATH FILTERS
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
            ts is None or high is None or low is None
            or high <= 0 or low <= 0
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
# CHART CANDLES: 4H AND 1D
# ==================================================

def get_chart_candles(mint, timeframe):

    pool, side = gecko_best_pool(mint)

    if not pool or not side:
        logging.warning("No Gecko pool for chart: %s", mint)
        return None

    if timeframe == "4H":
        endpoint = "hour"
        aggregate = 4
        limit = 120

    elif timeframe == "1D":
        endpoint = "day"
        aggregate = 1
        limit = 120

    else:
        return None

    payload = get_json(
        f"{GECKO}/networks/solana/pools/{pool}/ohlcv/{endpoint}",
        {
            "aggregate": aggregate,
            "limit": limit,
            "currency": "usd",
            "token": side,
        },
    )

    try:
        rows = payload["data"]["attributes"]["ohlcv_list"]
    except Exception:
        logging.warning(
            "%s candles unavailable for %s",
            timeframe,
            mint,
        )
        return None

    candles = []

    for row in rows:

        if not isinstance(row, list) or len(row) < 5:
            continue

        ts = as_float(row[0])
        open_price = as_float(row[1])
        high = as_float(row[2])
        low = as_float(row[3])
        close = as_float(row[4])

        if any(
            value is None
            for value in (
                ts, open_price, high, low, close
            )
        ):
            continue

        if min(open_price, high, low, close) <= 0:
            continue

        candles.append({
            "time": ts,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
        })

    candles.sort(key=lambda x: x["time"])

    return candles if len(candles) >= 5 else None


def create_candlestick_image(mint, timeframe, token_name):

    candles = get_chart_candles(mint, timeframe)

    if not candles:
        return None

    fig, ax = plt.subplots(figsize=(12, 6), dpi=130)

    width = 0.65

    for index, candle in enumerate(candles):

        open_price = candle["open"]
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]

        rising = close >= open_price
        color = "#16a34a" if rising else "#dc2626"

        ax.plot(
            [index, index],
            [low, high],
            color=color,
            linewidth=1.1,
        )

        body_bottom = min(open_price, close)
        body_height = abs(close - open_price)

        if body_height == 0:
            body_height = max(open_price * 0.0001, 1e-12)

        ax.add_patch(
            Rectangle(
                (index - width / 2, body_bottom),
                width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.7,
            )
        )

    ax.set_title(
        f"{token_name} | {timeframe}",
        fontsize=15,
        fontweight="bold",
    )

    ax.set_ylabel("Price (USD)")
    ax.set_xlim(-1, len(candles))

    step = max(1, len(candles) // 8)
    tick_positions = list(range(0, len(candles), step))

    ax.set_xticks(tick_positions)

    labels = []

    for index in tick_positions:

        dt = datetime.fromtimestamp(
            candles[index]["time"],
            timezone.utc,
        )

        if timeframe == "4H":
            labels.append(dt.strftime("%m-%d %H:%M"))
        else:
            labels.append(dt.strftime("%Y-%m-%d"))

    ax.set_xticklabels(labels, rotation=25, ha="right")

    ax.grid(False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()

    image_buffer = io.BytesIO()

    fig.savefig(
        image_buffer,
        format="png",
        bbox_inches="tight",
    )

    plt.close(fig)

    image_buffer.seek(0)

    return image_buffer


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

    logging.info(
        "Legacy ATH records recovered: %d",
        recovered,
    )

    return recovered


# ==================================================
# TELEGRAM FORWARDED POST PROCESSING
# ==================================================

def extract_token_addresses(text):

    if not text:
        return []

    found = BASE58_PATTERN.findall(text)

    return list(dict.fromkeys(found))


def process_forwarded_post(message):

    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))

    if chat.get("type") != "private":
        return

    if chat_id not in CHAT_IDS:
        logging.warning(
            "Unauthorized chart request from %s",
            chat_id,
        )
        return

    text_content = (
        message.get("text")
        or message.get("caption")
        or ""
    )

    addresses = extract_token_addresses(text_content)

    if not addresses:
        send_telegram(
            chat_id,
            "❌ آدرس قرارداد سولانا در متن یا کپشن پست پیدا نشد.\n"
            "لطفاً پستی را فوروارد کن که آدرس قرارداد داخل آن باشد."
        )
        return

    token = None
    mint = None

    for address in addresses:

        live = get_live_token_data(address)

        if live:
            token = live
            mint = address
            break

    if not token or not mint:
        send_telegram(
            chat_id,
            "❌ توکن معتبری از آدرس‌های پست پیدا نشد."
        )
        return

    token_name = (
        token.get("symbol")
        or token.get("name")
        or "TOKEN"
    )

    # Generate only two requested charts.
    chart_4h = create_candlestick_image(
        mint,
        "4H",
        token_name,
    )

    chart_1d = create_candlestick_image(
        mint,
        "1D",
        token_name,
    )

    sent_any = False

    if chart_4h:
        if send_telegram_photo(chat_id, chart_4h):
            sent_any = True

    if chart_1d:
        if send_telegram_photo(chat_id, chart_1d):
            sent_any = True

    if not sent_any:
        send_telegram(
            chat_id,
            "❌ اطلاعات کندلی این توکن در GeckoTerminal در دسترس نیست."
        )

    logging.info(
        "Chart request processed: %s",
        mint,
    )


# ==================================================
# TELEGRAM BOT API POLLING
# NO API ID / NO API HASH
# ==================================================

def poll_telegram_messages(state):

    if not BOT_TOKEN:
        return

    offset = int(state.get("telegram_update_offset", 0))

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/getUpdates"
    )

    try:

        response = session.get(
            url,
            params={
                "offset": offset,
                "timeout": 0,
                "allowed_updates": json.dumps(["message"]),
            },
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            logging.warning("Telegram getUpdates returned an error")
            return

        for update in payload.get("result", []):

            update_id = update.get("update_id")

            if update_id is None:
                continue

            # Save update offset to avoid processing it again.
            state["telegram_update_offset"] = update_id + 1
            save_state(state)

            message = update.get("message")

            if not isinstance(message, dict):
                continue

            # Accept forwarded posts only.
            is_forwarded = any(
                message.get(key)
                for key in (
                    "forward_origin",
                    "forward_from",
                    "forward_from_chat",
                    "forward_sender_name",
                )
            )

            if not is_forwarded:
                continue

            process_forwarded_post(message)

    except Exception as exc:
        logging.warning(
            "Telegram polling failed: %s",
            exc,
        )


# ==================================================
# MAIN
# ==================================================

def main():

    if not BOT_TOKEN:
        raise SystemExit(
            "Missing GitHub Secret: TELEGRAM_BOT_TOKEN"
        )

    state = load_state()

    logging.info("DEX HUNTER V15 scan started")

    # Receive forwarded Telegram posts.
    poll_telegram_messages(state)

    candidates = {}

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

    # RESTORE OLD ATH TRACKING WHEN POSSIBLE
    migrate_old_ath_alerts(candidates, state)

    with_market_cap = sum(
        1 for c in candidates.values()
        if c.get("market_cap") is not None
    )

    # FILTER 1: ATH DROP
    ath_count = ath_filter(candidates, state)

    # FILTER 2: ATH BREAKOUT
    breakout_count = ath_breakout_filter(candidates, state)

    # FILTER 3: ATH RECLAIM BUY
    buy_count = ath_reclaim_filter(state)

    save_state(state)

    logging.info(
        "V15 COMPLETE | "
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
