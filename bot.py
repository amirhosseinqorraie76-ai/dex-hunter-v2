
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DEX HUNTER V11

Four sources:
1. DexScreener
2. GeckoTerminal
3. Pump.fun
4. Stonk.fun

Independent filters:

A) ATH:
   Market cap >= $150,000
   Price >= 50% below observed ATH

B) Volume:
   Token age > 3 days
   24h volume >= $300,000

Alerts only. No automatic trading.
"""

import json
import logging
import os
import time
from datetime import datetime
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

# V11 FILTER SETTINGS

ATH_MIN_MARKET_CAP_USD = 150_000
ATH_MIN_DROP_PERCENT = 50.0

VOLUME_MIN_AGE_DAYS = 3
VOLUME_MIN_24H_USD = 300_000

STATE_FILE = Path("seen.json")

MAX_SOURCE_ROWS = 100
MAX_ATH_CHECKS_PER_RUN = 2
HTTP_TIMEOUT = 20

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
PUMP = "https://frontend-api-v3.pump.fun"
STONK = "https://www.stonkfun.xyz/api/public/v1"

HEADERS = {
    "User-Agent": "DEX-HUNTER-V11/1.0",
    "Accept": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update(HEADERS)


# ==================================================
# GENERAL HELPERS
# ==================================================

def get_json(url: str, params: Optional[dict] = None) -> Any:
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


def as_float(value: Any) -> Optional[float]:
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


def rows_from(payload: Any) -> list:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

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
            return [x for x in value if isinstance(x, dict)]

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


def mint_looks_valid(mint: Any) -> bool:
    return (
        isinstance(mint, str)
        and 32 <= len(mint) <= 44
    )


# ==================================================
# STATE / DUPLICATE CONTROL
# ==================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "sent": {
                "ath": [],
                "volume": [],
            },
            "ath_cursor": 0,
        }

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        state.setdefault("sent", {})
        state["sent"].setdefault("ath", [])
        state["sent"].setdefault("volume", [])
        state.setdefault("ath_cursor", 0)

        return state

    except Exception:
        logging.exception("Could not read seen.json")

        return {
            "sent": {
                "ath": [],
                "volume": [],
            },
            "ath_cursor": 0,
        }


def save_state(state: dict) -> None:
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
# TOKEN NORMALIZATION
# ==================================================

def normalize(row: dict, source: str) -> Optional[dict]:

    attrs = (
        row.get("attributes")
        if isinstance(row.get("attributes"), dict)
        else {}
    )

    rels = (
        row.get("relationships")
        if isinstance(row.get("relationships"), dict)
        else {}
    )

    base_rel = rels.get("base_token", {})

    base_data = (
        base_rel.get("data", {})
        if isinstance(base_rel, dict)
        else {}
    )

    base_id = (
        base_data.get("id")
        if isinstance(base_data, dict)
        else None
    )

    base = (
        row.get("baseToken")
        if isinstance(row.get("baseToken"), dict)
        else {}
    )

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
        attrs.get("created_timestamp"),
        attrs.get("created_at"),
    )

    created_ts = None

    if isinstance(created, str):
        try:
            created_ts = datetime.fromisoformat(
                created.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            created_ts = None
    else:
        created_ts = as_float(created)

        if created_ts and created_ts > 10_000_000_000:
            created_ts /= 1000.0

    volume_data = (
        attrs.get("volume_usd")
        if isinstance(attrs.get("volume_usd"), dict)
        else {}
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

        "volume_24h": as_float(first(
            row.get("volume24h"),
            row.get("volume_24h"),
            row.get("volume24hUsd"),
            row.get("volume_24h_usd"),
            volume_data.get("h24"),
        )),

        "created_ts": created_ts,
        "pair_created_ts": None,
        "dex_url": first(
            row.get("url"),
            attrs.get("url"),
        ),
        "liquidity": 0.0,
    }


def add_candidate(store: dict, candidate: Optional[dict]) -> None:
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
        "volume_24h",
        "created_ts",
        "dex_url",
    ):
        if (
            old.get(field) is None
            or old.get(field) in ("Unknown", "???")
        ) and candidate.get(field) is not None:
            old[field] = candidate[field]


# ==================================================
# SOURCE 1: DEXSCREENER
# ==================================================

def source_dexscreener(store: dict) -> None:

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

            if mint_looks_valid(mint):
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

def source_geckoterminal(store: dict) -> None:

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
                if isinstance(base_id, str) and "_" in base_id
                else None
            )

            if not mint_looks_valid(mint):
                continue

            attrs = row.get("attributes") or {}

            candidate = normalize(
                {
                    "mint": mint,
                    "name": attrs.get("name"),
                    "price_usd": attrs.get("base_token_price_usd"),
                },
                "GeckoTerminal",
            )

            add_candidate(store, candidate)


# ==================================================
# SOURCE 3: PUMP.FUN
# ==================================================

def source_pump_fun(store: dict) -> None:

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

def source_stonk_fun(store: dict) -> None:

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
# DEXSCREENER METRICS ENRICHMENT
# ==================================================

def enrich_from_dexscreener(store: dict) -> None:

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

            candidate["name"] = (
                base.get("name")
                or candidate["name"]
            )

            candidate["symbol"] = (
                base.get("symbol")
                or candidate["symbol"]
            )

            liquidity = (
                as_float(
                    (pair.get("liquidity") or {}).get("usd")
                ) or 0.0
            )

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

                candidate["volume_24h"] = (
                    as_float(
                        (pair.get("volume") or {}).get("h24")
                    )
                    or candidate.get("volume_24h")
                )

                pair_created = as_float(
                    pair.get("pairCreatedAt")
                )

                if pair_created and pair_created > 10_000_000_000:
                    pair_created /= 1000.0

                candidate["pair_created_ts"] = pair_created

                candidate["dex_url"] = (
                    pair.get("url")
                    or candidate.get("dex_url")
                )


# ==================================================
# ATH CHECK USING GECKOTERMINAL
# ==================================================

def gecko_best_pool(mint: str):

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
        pool_address = None

        if isinstance(pool_id, str) and "_" in pool_id:
            pool_address = pool_id.split("_", 1)[1]

        pool_address = pool_address or attrs.get("address")

        base_id = (
            ((rels.get("base_token") or {}).get("data") or {}).get("id")
        )

        quote_id = (
            ((rels.get("quote_token") or {}).get("data") or {}).get("id")
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

        liquidity = (
            as_float(attrs.get("reserve_in_usd"))
            or 0.0
        )

        if pool_address and liquidity > best_liquidity:
            best_pool = pool_address
            best_side = side
            best_liquidity = liquidity

    return best_pool, best_side


def observed_ath(
    mint: str,
    created_ts: Optional[float],
):

    if not created_ts:
        return None, "زمان ایجاد توکن مشخص نیست"

    age_days = (
        time.time() - created_ts
    ) / 86400.0

    if age_days > 175:
        return None, "تاریخچه کافی برای تأیید ATH در دسترس نیست"

    pool, side = gecko_best_pool(mint)

    if not pool or not side:
        return None, "استخر مناسب برای بررسی ATH پیدا نشد"

    payload = get_json(
        f"{GECKO}/networks/solana/pools/{pool}/ohlcv/day",
        {
            "aggregate": 1,
            "limit": 1000,
            "currency": "usd",
            "token": side,
        },
    )

    try:
        candles = payload["data"]["attributes"]["ohlcv_list"]
    except Exception:
        return None, "داده کندل روزانه در دسترس نیست"

    highs = []
    candle_times = []

    for candle in candles:

        if isinstance(candle, list) and len(candle) >= 3:

            ts = as_float(candle[0])
            high = as_float(candle[2])

            if ts and high and high > 0:
                candle_times.append(ts)
                highs.append(high)

    if not highs or not candle_times:
        return None, "کندل معتبر برای محاسبه ATH وجود ندارد"

    if min(candle_times) > created_ts + 36 * 3600:
        return None, "تاریخچه از ابتدای عمر توکن شروع نمی‌شود"

    return (
        max(highs),
        "ATH مشاهده‌شده از کندل‌های روزانه GeckoTerminal",
    )


# ==================================================
# TELEGRAM
# ==================================================

def send_telegram(chat_id: str, message: str) -> bool:

    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is missing")
        return False

    url = (
        f"https://api.telegram.org/"
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
            "Telegram delivery failed for %s: %s",
            chat_id,
            exc,
        )

        return False


def deliver(
    state: dict,
    scanner: str,
    mint: str,
    message: str,
) -> None:

    sent = set(state["sent"][scanner])

    for chat_id in CHAT_IDS:

        delivery_key = f"{mint}|{chat_id}"

        if delivery_key in sent:
            continue

        if send_telegram(chat_id, message):

            sent.add(delivery_key)

            state["sent"][scanner] = sorted(sent)

            save_state(state)

        else:

            logging.warning(
                "Will retry delivery: %s",
                delivery_key,
            )

        time.sleep(0.12)


# ==================================================
# FILTER 2: VOLUME SCANNER
# ==================================================

def token_age_days(candidate: dict) -> Optional[float]:

    created = candidate.get("created_ts")

    if not created:
        return None

    return max(
        0.0,
        (time.time() - created) / 86400.0,
    )


def volume_filter(store: dict, state: dict) -> int:

    matches = 0

    for candidate in store.values():

        age = token_age_days(candidate)
        volume = candidate.get("volume_24h")

        # Independent filter:
        # No ATH condition.
        # No market-cap condition.

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

        url = (
            candidate.get("dex_url")
            or f"https://dexscreener.com/solana/{mint}"
        )

        message = (
            "🔥 شکار حجم معاملات | DEX HUNTER V11\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 توکن: {candidate['name']} ({candidate['symbol']})\n"
            f"⏳ عمر توکن: {age:.1f} روز\n"
            f"📦 حجم معاملات ۲۴ ساعته: ${volume:,.0f}\n"
            f"📊 مارکت‌کپ: {market_cap_text}\n"
            f"🔎 منابع: {', '.join(sorted(candidate['sources']))}\n"
            f"🧾 قرارداد: {mint}\n"
            f"📈 نمودار: {url}\n"
            "✅ شرط فیلتر دوم: عمر بالای ۳ روز + حجم ۲۴ ساعته حداقل ۳۰۰ هزار دلار\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید. نقدینگی و ریسک قرارداد را بررسی کن."
        )

        deliver(
            state,
            "volume",
            mint,
            message,
        )

        matches += 1

    return matches


# ==================================================
# FILTER 1: ATH DROP SCANNER
# ==================================================

def ath_filter(store: dict, state: dict) -> int:

    candidates = [
        candidate
        for candidate in store.values()
        if (candidate.get("market_cap") or 0)
        >= ATH_MIN_MARKET_CAP_USD
        and (candidate.get("price") or 0) > 0
        and candidate.get("created_ts")
    ]

    candidates.sort(
        key=lambda x: x.get("market_cap") or 0,
        reverse=True,
    )

    if not candidates:
        return 0

    cursor = (
        int(state.get("ath_cursor", 0))
        % len(candidates)
    )

    ordered = (
        candidates[cursor:]
        + candidates[:cursor]
    )

    batch = ordered[:MAX_ATH_CHECKS_PER_RUN]

    state["ath_cursor"] = (
        cursor + len(batch)
    ) % len(candidates)

    save_state(state)

    matches = 0

    for candidate in batch:

        mint = candidate["mint"]

        if all(
            f"{mint}|{chat}" in state["sent"]["ath"]
            for chat in CHAT_IDS
        ):
            continue

        ath, note = observed_ath(
            mint,
            candidate.get("created_ts"),
        )

        price = candidate.get("price") or 0

        if not ath or price <= 0 or ath < price:
            continue

        drop = (
            (ath - price) / ath
        ) * 100.0

        # Independent filter:
        # Market cap >= $150,000
        # Price >=50% below observed ATH.

        if drop < ATH_MIN_DROP_PERCENT:
            continue

        url = (
            candidate.get("dex_url")
            or f"https://dexscreener.com/solana/{mint}"
        )

        message = (
            "🎯 شکار ریزش از سقف تاریخی | DEX HUNTER V11\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🪙 توکن: {candidate['name']} ({candidate['symbol']})\n"
            f"💰 قیمت فعلی: ${price:.10g}\n"
            f"🏔 سقف مشاهده‌شده: ${ath:.10g}\n"
            f"📉 افت از ATH: {drop:.1f}%\n"
            f"📊 مارکت‌کپ: ${(candidate.get('market_cap') or 0):,.0f}\n"
            f"🔎 منابع: {', '.join(sorted(candidate['sources']))}\n"
            f"🧾 قرارداد: {mint}\n"
            f"📈 نمودار: {url}\n"
            f"ℹ️ {note}\n"
            "✅ شرط فیلتر اول: مارکت‌کپ حداقل ۱۵۰ هزار دلار + افت حداقل ۵۰٪ از ATH تأییدشده\n"
            "⚠️ هشدار تحلیلی است، نه توصیه خرید. نقدینگی و ریسک قرارداد را بررسی کن."
        )

        deliver(
            state,
            "ath",
            mint,
            message,
        )

        matches += 1

        time.sleep(1)

    return matches


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

    logging.info("DEX HUNTER V11 scan started")

    # Four sources are scanned independently.
    source_dexscreener(candidates)
    source_geckoterminal(candidates)
    source_pump_fun(candidates)
    source_stonk_fun(candidates)

    logging.info(
        "Discovered %d unique tokens",
        len(candidates),
    )

    enrich_from_dexscreener(candidates)

    # The two filters are independent.
    volume_count = volume_filter(
        candidates,
        state,
    )

    ath_count = ath_filter(
        candidates,
        state,
    )

    save_state(state)

    logging.info(
        "Scan complete | tokens=%d | volume=%d | ath=%d",
        len(candidates),
        volume_count,
        ath_count,
    )


if __name__ == "__main__":
    main()
