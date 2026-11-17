
import os
import json
import time
import requests
from datetime import datetime, timezone

# =====================================
# DEX HUNTER V7 - ATH DROP SCANNER
# =====================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

CHAT_IDS = [
    "6665909136",
    "6765810776",
    "98529959",
    "1507608801"
]

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
GMGN = "https://gmgn.ai"

STATE_FILE = "seen_tokens.json"

DROP_THRESHOLD = 75
MAX_CANDIDATES = 35
MAX_ALERTS = 20
MAX_HISTORY_PAGES = 12

# Only Solana
CHAIN = "solana"
GMGN_CHAIN = "sol"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 DEX-HUNTER-V7",
    "Accept": "application/json",
    "Origin": "https://gmgn.ai",
    "Referer": "https://gmgn.ai/"
})


def now():
    return datetime.now(timezone.utc).isoformat()


def number(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def fa(value, decimals=2):
    try:
        result = f"{float(value):,.{decimals}f}"
        return result.translate(str.maketrans(
            "0123456789", "۰۱۲۳۴۵۶۷۸۹"
        ))
    except Exception:
        return str(value)


def get_json(url, params=None, timeout=20):
    try:
        response = session.get(
            url,
            params=params,
            timeout=timeout
        )
        response.raise_for_status()
        return response.json()
    except Exception as error:
        print("API ERROR:", url, str(error))
        return None


def post_telegram(message):
    if not BOT_TOKEN:
        print("BOT TOKEN MISSING")
        return False

    success = 0

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    for chat_id in CHAT_IDS:
        try:
            response = session.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=20
            )

            response.raise_for_status()

            if response.json().get("ok"):
                success += 1
                print("SENT:", chat_id)

        except Exception as error:
            print("SEND ERROR:", chat_id, error)

    print(f"DELIVERY: {success}/{len(CHAT_IDS)}")

    return success == len(CHAT_IDS)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =====================================
# DISCOVERY: DEXSCREENER
# =====================================

def discover_dex():
    endpoints = [
        "/token-profiles/latest/v1",
        "/token-boosts/latest/v1",
        "/community-takeovers/latest/v1"
    ]

    found = {}

    for endpoint in endpoints:
        data = get_json(DEX + endpoint)

        if not isinstance(data, list):
            continue

        for item in data:
            if not isinstance(item, dict):
                continue

            if item.get("chainId") != CHAIN:
                continue

            address = item.get("tokenAddress")

            if address:
                found[address] = {
                    "address": address,
                    "source": "DexScreener"
                }

    print("DEX tokens:", len(found))
    return found


# =====================================
# DISCOVERY: GMGN
# =====================================

def discover_gmgn():
    found = {}

    url = (
        f"{GMGN}/defi/quotation/v1/"
        f"rank/{GMGN_CHAIN}/swaps/1h"
    )

    params = {
        "orderby": "open_timestamp",
        "direction": "desc",
        "limit": 100
    }

    data = get_json(url, params=params)

    if not isinstance(data, dict):
        print("GMGN unavailable")
        return found

    inner = data.get("data") or {}
    rows = inner.get("rank") or inner.get("list") or []

    if isinstance(rows, list):
        for item in rows:
            if not isinstance(item, dict):
                continue

            address = (
                item.get("address")
                or item.get("token_address")
                or item.get("tokenAddress")
            )

            if address:
                found[address] = {
                    "address": address,
                    "source": "GMGN"
                }

    print("GMGN tokens:", len(found))
    return found


def discover_tokens():
    dex = discover_dex()
    gmgn = discover_gmgn()

    combined = {}

    for address, item in dex.items():
        combined[address] = item

    for address, item in gmgn.items():
        if address in combined:
            combined[address]["source"] = "GMGN + DexScreener"
        else:
            combined[address] = item

    result = list(combined.values())

    print("TOTAL UNIQUE TOKENS:", len(result))

    return result[:MAX_CANDIDATES]


# =====================================
# TOKEN PAIRS
# =====================================

def get_best_pair(address):
    url = f"{DEX}/token-pairs/v1/{CHAIN}/{address}"

    pairs = get_json(url)

    if not isinstance(pairs, list) or not pairs:
        return None

    valid = [
        p for p in pairs
        if p.get("chainId") == CHAIN
        and p.get("baseToken", {}).get("address") == address
        and p.get("pairAddress")
    ]

    if not valid:
        return None

    valid.sort(
        key=lambda p: number(
            (p.get("liquidity") or {}).get("usd")
        ),
        reverse=True
    )

    return valid[0]


# =====================================
# HISTORICAL ATH
# GeckoTerminal OHLCV
# =====================================

def get_ath(pair_address, pair_created_at):
    url = (
        f"{GECKO}/networks/{CHAIN}/"
        f"pools/{pair_address}/ohlcv/hour"
    )

    before = None
    highest = 0
    oldest_seen = None
    pages = 0
    candles_found = 0

    created_seconds = number(pair_created_at) / 1000

    while pages < MAX_HISTORY_PAGES:
        params = {
            "aggregate": 1,
            "limit": 1000,
            "currency": "usd",
            "token": "base",
            "include_empty_intervals": "false"
        }

        if before:
            params["before_timestamp"] = before

        data = get_json(url, params=params, timeout=30)

        if not isinstance(data, dict):
            break

        attributes = (
            data.get("data", {})
            .get("attributes", {})
        )

        candles = attributes.get("ohlcv_list") or []

        if not candles:
            break

        pages += 1
        candles_found += len(candles)

        timestamps = []

        for candle in candles:
            if not isinstance(candle, list) or len(candle) < 3:
                continue

            timestamp = number(candle[0])
            high = number(candle[2])

            if high > highest:
                highest = high

            timestamps.append(timestamp)

        if not timestamps:
            break

        oldest = min(timestamps)
        oldest_seen = oldest

        if created_seconds and oldest <= created_seconds:
            break

        next_before = int(oldest) - 1

        if before == next_before:
            break

        before = next_before

        time.sleep(0.25)

    complete = (
        bool(created_seconds)
        and bool(oldest_seen)
        and oldest_seen <= created_seconds
    )

    return {
        "ath": highest,
        "candles": candles_found,
        "pages": pages,
        "complete": complete,
        "oldest": oldest_seen
    }


# =====================================
# ANALYSIS
# =====================================

def analyze_token(address):
    pair = get_best_pair(address)

    if not pair:
        return None

    current_price = number(pair.get("priceUsd"))

    if current_price <= 0:
        return None

    created = pair.get("pairCreatedAt")

    if not created:
        return None

    history = get_ath(
        pair.get("pairAddress"),
        created
    )

    ath = history["ath"]

    if ath <= 0:
        return None

    drop = ((ath - current_price) / ath) * 100

    return {
        "pair": pair,
        "name": pair.get("baseToken", {}).get("name", "Unknown"),
        "symbol": pair.get("baseToken", {}).get("symbol", "N/A"),
        "address": address,
        "price": current_price,
        "ath": ath,
        "drop": drop,
        "liquidity": number(
            (pair.get("liquidity") or {}).get("usd")
        ),
        "volume": number(
            (pair.get("volume") or {}).get("h24")
        ),
        "created": created,
        "history_complete": history["complete"],
        "candles": history["candles"],
        "pages": history["pages"],
        "pair_address": pair.get("pairAddress"),
        "dex_url": pair.get("url")
    }


# =====================================
# MESSAGE
# =====================================

def make_message(item, source):
    age_hours = max(
        0,
        (time.time() - number(item["created"]) / 1000) / 3600
    )

    drop = item["drop"]

    message = (
        "🎯 DEX HUNTER V7\n"
        "━━━━━━━━━━━━━━━━\n"
        "🚨 ATH DROP ALERT | هشدار ریزش از سقف\n\n"

        f"🪙 {item['name']} ({item['symbol']})\n"
        f"📍 منبع کشف | Discovery: {source}\n\n"

        "🇮🇷 تحلیل فارسی\n"
        f"🔻 ریزش از سقف تاریخی: {fa(drop)}٪\n"
        f"🏔 بالاترین قیمت ثبت‌شده: ${item['ath']}\n"
        f"💵 قیمت فعلی: ${item['price']}\n"
        f"💧 نقدینگی: ${fa(item['liquidity'])}\n"
        f"📊 حجم ۲۴ ساعته: ${fa(item['volume'])}\n"
        f"⏳ عمر استخر: {fa(age_hours, 1)} ساعت\n"
        "✅ شرط ریزش ۷۵٪ یا بیشتر برقرار است.\n\n"

        "🇬🇧 English Analysis\n"
        f"🔻 Drawdown from ATH: {drop:.2f}%\n"
        f"🏔 Historical high: ${item['ath']}\n"
        f"💵 Current price: ${item['price']}\n"
        f"💧 Liquidity: ${item['liquidity']:,.2f}\n"
        f"📊 24H Volume: ${item['volume']:,.2f}\n"
        f"⏳ Pool age: {age_hours:.1f} hours\n"
        "✅ The 75% or greater drawdown condition is met.\n\n"

        "📚 بررسی تاریخچه | History Check\n"
        f"کندل‌های بررسی‌شده | Candles: {item['candles']}\n"
        f"صفحات تاریخچه | Pages: {item['pages']}\n"
        f"تاریخچه کامل | Full history: "
        f"{'بله / Yes' if item['history_complete'] else 'خیر / No'}\n\n"

        "📍 قرارداد | Contract:\n"
        f"{item['address']}\n\n"

        "🔗 نمودار | Chart:\n"
        f"https://dexscreener.com/solana/{item['pair_address']}\n\n"

        "⚠️ ریزش زیاد به معنی فرصت خرید نیست.\n"
        "⚠️ A large drawdown does not mean a buying opportunity.\n"
        "⚠️ اگر تاریخچه کامل نباشد، سقف محاسبه‌شده ممکن است سقف واقعی نباشد.\n"
        "⚠️ If history is incomplete, the calculated high may not be the true ATH."
    )

    return message


# =====================================
# MAIN SCANNER
# =====================================

def scan():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    state = load_state()
    candidates = discover_tokens()

    alerts = 0

    for candidate in candidates:
        address = candidate["address"]
        source = candidate["source"]

        try:
            result = analyze_token(address)

            if not result:
                continue

            if result["drop"] < DROP_THRESHOLD:
                continue

            # Never claim a true lifetime ATH
            # if historical candles are incomplete.
            if not result["history_complete"]:
                print("INCOMPLETE HISTORY:", address)
                continue

            old = state.get(address, {})

            # Send once per token
            if old.get("alerted"):
                continue

            message = make_message(result, source)

            delivered = post_telegram(message)

            if delivered:
                state[address] = {
                    "alerted": True,
                    "ath": result["ath"],
                    "price": result["price"],
                    "drop": result["drop"],
                    "date": now()
                }

                save_state(state)
                alerts += 1

            if alerts >= MAX_ALERTS:
                break

            time.sleep(0.5)

        except Exception as error:
            print("TOKEN ERROR:", address, str(error))

    save_state(state)

    print(
        "DEX HUNTER V7 FINISHED | "
        f"Candidates: {len(candidates)} | "
        f"Alerts: {alerts}"
    )


if __name__ == "__main__":
    scan()
