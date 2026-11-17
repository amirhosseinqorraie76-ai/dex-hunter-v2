
import os
import json
import time
import requests
from datetime import datetime, timezone

# ==========================================
# DEX HUNTER V8 - SOLANA POOL ATH SCANNER
# ==========================================

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

CHAIN = "solana"
GMGN_CHAIN = "sol"

STATE_FILE = "seen_tokens.json"

# New filters
DROP_THRESHOLD = 70
MIN_MARKET_CAP = 500000

# Scanner limits
MAX_CANDIDATES = 35
MAX_ALERTS = 20
MAX_HISTORY_PAGES = 24

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 DEX-HUNTER-V8",
    "Accept": "application/json",
    "Origin": "https://gmgn.ai",
    "Referer": "https://gmgn.ai/"
})


# ==========================================
# HELPERS
# ==========================================

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


def get_json(url, params=None, timeout=25):
    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=timeout
            )

            if response.status_code == 429:
                wait = number(
                    response.headers.get("Retry-After")
                ) or (5 * (attempt + 1))

                print("RATE LIMIT:", wait)
                time.sleep(min(wait, 30))
                continue

            response.raise_for_status()
            return response.json()

        except Exception as error:
            print("API ERROR:", url, str(error))
            time.sleep(attempt + 1)

    return None


def post_one_chat(chat_id, message):
    if not BOT_TOKEN:
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

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
        return response.json().get("ok", False)

    except Exception as error:
        print("TELEGRAM ERROR:", chat_id, error)
        return False


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        return data if isinstance(data, dict) else {}

    except Exception:
        return {}


def save_state(state):
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(temp_file, STATE_FILE)


# ==========================================
# DISCOVERY - DEXSCREENER
# ==========================================

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

    print("DEX TOKENS:", len(found))
    return found


# ==========================================
# DISCOVERY - GMGN
# ==========================================

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
        print("GMGN UNAVAILABLE")
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

    print("GMGN TOKENS:", len(found))
    return found


def discover_tokens():
    dex = discover_dex()
    gmgn = discover_gmgn()

    combined = {}

    for address, item in dex.items():
        combined[address] = item

    for address, item in gmgn.items():
        if address in combined:
            combined[address]["source"] = (
                "GMGN + DexScreener"
            )
        else:
            combined[address] = item

    result = list(combined.values())

    print("UNIQUE TOKENS:", len(result))

    return result[:MAX_CANDIDATES]


# ==========================================
# TOKEN PAIRS AND MARKET CAP
# ==========================================

def get_best_pair(address):
    url = f"{DEX}/token-pairs/v1/{CHAIN}/{address}"

    pairs = get_json(url)

    if not isinstance(pairs, list) or not pairs:
        return None

    valid = []

    for pair in pairs:
        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != CHAIN:
            continue

        base = pair.get("baseToken") or {}

        if base.get("address") != address:
            continue

        if not pair.get("pairAddress"):
            continue

        price = number(pair.get("priceUsd"))
        market_cap = number(pair.get("marketCap"))
        liquidity = number(
            (pair.get("liquidity") or {}).get("usd")
        )

        # Do not substitute FDV for market cap.
        if price <= 0:
            continue

        if market_cap < MIN_MARKET_CAP:
            continue

        valid.append(pair)

    if not valid:
        return None

    valid.sort(
        key=lambda p: number(
            (p.get("liquidity") or {}).get("usd")
        ),
        reverse=True
    )

    return valid[0]


# ==========================================
# HISTORICAL POOL ATH - GECKOTERMINAL
# ==========================================

def get_ath(pair_address, pair_created_at):
    url = (
        f"{GECKO}/networks/{CHAIN}/"
        f"pools/{pair_address}/ohlcv/hour"
    )

    created_seconds = number(pair_created_at) / 1000

    if created_seconds <= 0:
        return None

    before = None
    highest = 0
    oldest_seen = None
    pages = 0
    candles_found = 0

    while pages < MAX_HISTORY_PAGES:
        params = {
            "aggregate": 1,
            "limit": 1000,
            "currency": "usd",
            "token": "base",
            "include_empty_intervals": "false"
        }

        if before is not None:
            params["before_timestamp"] = before

        data = get_json(
            url,
            params=params,
            timeout=30
        )

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
            if not isinstance(candle, list):
                continue

            if len(candle) < 3:
                continue

            timestamp = number(candle[0])
            high = number(candle[2])

            if timestamp <= 0:
                continue

            if high > highest:
                highest = high

            timestamps.append(timestamp)

        if not timestamps:
            break

        oldest = min(timestamps)
        oldest_seen = oldest

        # Reached the pool creation period.
        if oldest <= created_seconds:
            break

        next_before = int(oldest) - 1

        if before == next_before:
            break

        before = next_before

        time.sleep(0.3)

    complete = (
        oldest_seen is not None
        and oldest_seen <= created_seconds
        and highest > 0
    )

    return {
        "ath": highest,
        "candles": candles_found,
        "pages": pages,
        "complete": complete,
        "oldest": oldest_seen
    }


# ==========================================
# ANALYSIS
# ==========================================

def analyze_token(address):
    pair = get_best_pair(address)

    if not pair:
        return None

    current_price = number(pair.get("priceUsd"))
    market_cap = number(pair.get("marketCap"))

    if current_price <= 0:
        return None

    if market_cap < MIN_MARKET_CAP:
        return None

    created = pair.get("pairCreatedAt")

    if not created:
        return None

    pair_address = pair.get("pairAddress")

    history = get_ath(
        pair_address,
        created
    )

    if not history:
        return None

    if not history["complete"]:
        print("INCOMPLETE HISTORY:", address)
        return None

    ath = history["ath"]

    if ath <= 0:
        return None

    drop = ((ath - current_price) / ath) * 100

    if drop < DROP_THRESHOLD:
        return None

    base = pair.get("baseToken") or {}

    return {
        "name": base.get("name", "Unknown"),
        "symbol": base.get("symbol", "N/A"),
        "address": address,
        "price": current_price,
        "ath": ath,
        "drop": drop,
        "market_cap": market_cap,
        "liquidity": number(
            (pair.get("liquidity") or {}).get("usd")
        ),
        "volume": number(
            (pair.get("volume") or {}).get("h24")
        ),
        "created": created,
        "candles": history["candles"],
        "pages": history["pages"],
        "pair_address": pair_address
    }


# ==========================================
# TELEGRAM MESSAGE
# ==========================================

def make_message(item, source):
    age_hours = max(
        0,
        (time.time() - number(item["created"]) / 1000) / 3600
    )

    return (
        "🎯 DEX HUNTER V8\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🚨 SOLANA POOL ATH ALERT\n"
        "🚨 هشدار فاصله از سقف استخر سولانا\n\n"

        f"🪙 {item['name']} ({item['symbol']})\n"
        f"📍 Discovery: {source}\n\n"

        "🇮🇷 تحلیل فارسی\n"
        f"🔻 فاصله از سقف استخر: {fa(item['drop'])}٪\n"
        f"🏔 سقف تاریخی استخر: ${item['ath']:.12g}\n"
        f"💵 قیمت فعلی: ${item['price']:.12g}\n"
        f"💰 مارکت‌کپ: ${fa(item['market_cap'])}\n"
        f"💧 نقدینگی: ${fa(item['liquidity'])}\n"
        f"📊 حجم ۲۴ ساعته: ${fa(item['volume'])}\n"
        f"⏳ عمر استخر: {fa(age_hours, 1)} ساعت\n"
        "✅ مارکت‌کپ حداقل ۵۰۰ هزار دلار است.\n"
        "✅ فاصله از سقف حداقل ۷۰ درصد است.\n\n"

        "🇬🇧 ENGLISH ANALYSIS\n"
        f"🔻 Drawdown from pool ATH: {item['drop']:.2f}%\n"
        f"🏔 Pool historical high: ${item['ath']:.12g}\n"
        f"💵 Current price: ${item['price']:.12g}\n"
        f"💰 Market cap: ${item['market_cap']:,.2f}\n"
        f"💧 Liquidity: ${item['liquidity']:,.2f}\n"
        f"📊 24H Volume: ${item['volume']:,.2f}\n"
        f"⏳ Pool age: {age_hours:.1f} hours\n\n"

        "📚 HISTORY VERIFICATION\n"
        f"Candles checked: {item['candles']}\n"
        f"History pages: {item['pages']}\n"
        "History status: COMPLETE TO POOL CREATION\n\n"

        "📍 Contract:\n"
        f"{item['address']}\n\n"

        "🔗 Chart:\n"
        f"https://dexscreener.com/solana/{item['pair_address']}\n\n"

        "⚠️ سقف محاسبه‌شده مربوط به همین استخر است، نه لزوماً تمام استخرهای توکن.\n"
        "⚠️ ATH shown is for this pool, not necessarily every market for this token.\n"
        "⚠️ افت قیمت به‌تنهایی به معنی فرصت خرید نیست.\n"
        "⚠️ A large drawdown alone is not a buy signal."
    )


# ==========================================
# MAIN SCANNER
# ==========================================

def scan():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    state = load_state()
    candidates = discover_tokens()

    alerts = 0

    for candidate in candidates:
        address = candidate["address"]
        source = candidate["source"]

        old = state.get(address, {})

        # Old V7 alerts remain ignored.
        if old.get("alerted"):
            continue

        try:
            result = analyze_token(address)

            if not result:
                continue

            message = make_message(result, source)

            sent_to = old.get("sent_to", [])

            if not isinstance(sent_to, list):
                sent_to = []

            # Retry only recipients who did not receive it.
            for chat_id in CHAT_IDS:
                if chat_id in sent_to:
                    continue

                if post_one_chat(chat_id, message):
                    sent_to.append(chat_id)

                    state[address] = {
                        "alerted": False,
                        "sent_to": sent_to,
                        "ath": result["ath"],
                        "price": result["price"],
                        "drop": result["drop"],
                        "market_cap": result["market_cap"],
                        "date": now()
                    }

                    save_state(state)

                time.sleep(0.3)

            if all(chat_id in sent_to for chat_id in CHAT_IDS):
                state[address]["alerted"] = True
                state[address]["date"] = now()
                save_state(state)

                alerts += 1
                print("ALERT COMPLETED:", address)

            if alerts >= MAX_ALERTS:
                break

            time.sleep(0.5)

        except Exception as error:
            print(
                "TOKEN ERROR:",
                address,
                str(error)
            )

    save_state(state)

    print(
        "DEX HUNTER V8 FINISHED | "
        f"Candidates: {len(candidates)} | "
        f"Alerts: {alerts}"
    )


if __name__ == "__main__":
    scan()
