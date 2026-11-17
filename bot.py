
import os
import json
import time
import requests
from datetime import datetime, timezone

# ==================================================
# DEX HUNTER V10 - TWO INDEPENDENT SOLANA SCANNERS
# ==================================================

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

# FILTER 1: ATH DROP
DROP_THRESHOLD = 50
MIN_MARKET_CAP = 100000
MAX_ATH_CANDIDATES = 120
MAX_ATH_ALERTS = 30

# FILTER 2: OLD POOL + HIGH VOLUME
MIN_POOL_AGE_DAYS = 3
MIN_VOLUME_24H = 300000
MAX_VOLUME_CANDIDATES = 250
MAX_VOLUME_ALERTS = 50

# ATH HISTORY
MAX_HISTORY_PAGES = 24

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 DEX-HUNTER-V10",
    "Accept": "application/json",
    "Origin": "https://gmgn.ai",
    "Referer": "https://gmgn.ai/"
})

PAIR_CACHE = {}


# ==================================================
# HELPERS
# ==================================================

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


def get_filter_record(state, filter_name, address):
    key = f"{filter_name}:{address}"

    if key in state:
        return key, state[key]

    # Preserve old ATH alerts from previous versions.
    if filter_name == "ath" and address in state:
        old = state[address]

        if isinstance(old, dict):
            return key, old

    return key, {}


# ==================================================
# DISCOVERY - DEXSCREENER
# ==================================================

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


# ==================================================
# DISCOVERY - GMGN
# ==================================================

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
        combined[address] = item.copy()

    for address, item in gmgn.items():
        if address in combined:
            combined[address]["source"] = (
                "GMGN + DexScreener"
            )
        else:
            combined[address] = item.copy()

    result = list(combined.values())

    print("UNIQUE TOKENS:", len(result))
    return result


# ==================================================
# TOKEN PAIRS
# ==================================================

def get_token_pairs(address):
    if address in PAIR_CACHE:
        return PAIR_CACHE[address]

    url = f"{DEX}/token-pairs/v1/{CHAIN}/{address}"
    pairs = get_json(url)

    if not isinstance(pairs, list):
        pairs = []

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

        if number(pair.get("priceUsd")) <= 0:
            continue

        valid.append(pair)

    PAIR_CACHE[address] = valid
    return valid


# ==================================================
# FILTER 1: BEST PAIR FOR ATH
# ==================================================

def get_best_pair_for_ath(address):
    pairs = get_token_pairs(address)
    valid = []

    for pair in pairs:
        market_cap = number(pair.get("marketCap"))

        # Market cap only. Never substitute FDV.
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


# ==================================================
# FILTER 2: OLD POOL + HIGH VOLUME
# ==================================================

def get_volume_candidate(address):
    pairs = get_token_pairs(address)

    current_time = time.time()
    eligible = []

    for pair in pairs:
        created = number(pair.get("pairCreatedAt"))

        if created <= 0:
            continue

        created_seconds = created / 1000
        age_days = (
            current_time - created_seconds
        ) / 86400

        if age_days <= MIN_POOL_AGE_DAYS:
            continue

        volume = number(
            (pair.get("volume") or {}).get("h24")
        )

        if volume <= 0:
            continue

        eligible.append({
            "pair": pair,
            "age_days": age_days,
            "volume": volume
        })

    if not eligible:
        return None

    total_volume = sum(
        item["volume"] for item in eligible
    )

    if total_volume < MIN_VOLUME_24H:
        return None

    eligible.sort(
        key=lambda item: number(
            (item["pair"].get("liquidity") or {}).get("usd")
        ),
        reverse=True
    )

    main = eligible[0]
    pair = main["pair"]
    base = pair.get("baseToken") or {}

    oldest_age = max(
        item["age_days"] for item in eligible
    )

    return {
        "name": base.get("name", "Unknown"),
        "symbol": base.get("symbol", "N/A"),
        "address": address,
        "price": number(pair.get("priceUsd")),
        "market_cap": number(pair.get("marketCap")),
        "liquidity": number(
            (pair.get("liquidity") or {}).get("usd")
        ),
        "volume": total_volume,
        "pool_age_days": oldest_age,
        "pair_address": pair.get("pairAddress"),
        "eligible_pools": len(eligible)
    }


# ==================================================
# GECKOTERMINAL: HISTORICAL POOL ATH
# ==================================================

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

            highest = max(highest, high)
            timestamps.append(timestamp)

        if not timestamps:
            break

        oldest = min(timestamps)
        oldest_seen = oldest

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


# ==================================================
# FILTER 1: ATH ANALYSIS
# ==================================================

def analyze_ath_token(address):
    pair = get_best_pair_for_ath(address)

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

    history = get_ath(pair_address, created)

    if not history or not history["complete"]:
        print("INCOMPLETE ATH HISTORY:", address)
        return None

    ath = history["ath"]

    if ath <= 0:
        return None

    drop = ((ath - current_price) / ath) * 100

    if drop < DROP_THRESHOLD:
        return None

    base = pair.get("baseToken") or {}

    age_hours = max(
        0,
        (time.time() - number(created) / 1000) / 3600
    )

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
        "age_hours": age_hours,
        "candles": history["candles"],
        "pages": history["pages"],
        "pair_address": pair_address
    }


# ==================================================
# MESSAGE 1: ATH ALERT
# ==================================================

def make_ath_message(item, source):
    return (
        "🎯 DEX HUNTER V10\n"
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
        f"⏳ عمر استخر: {fa(item['age_hours'], 1)} ساعت\n"
        "✅ مارکت‌کپ حداقل ۱۰۰ هزار دلار است.\n"
        "✅ افت از ATH حداقل ۵۰ درصد است.\n\n"

        "🇬🇧 ENGLISH ANALYSIS\n"
        f"🔻 Pool ATH drawdown: {item['drop']:.2f}%\n"
        f"🏔 Pool historical high: ${item['ath']:.12g}\n"
        f"💵 Current price: ${item['price']:.12g}\n"
        f"💰 Market cap: ${item['market_cap']:,.2f}\n"
        f"💧 Liquidity: ${item['liquidity']:,.2f}\n"
        f"📊 24H Volume: ${item['volume']:,.2f}\n"
        f"⏳ Pool age: {item['age_hours']:.1f} hours\n\n"

        "📚 HISTORY VERIFICATION\n"
        f"Candles checked: {item['candles']}\n"
        f"History pages: {item['pages']}\n"
        "History status: COMPLETE TO POOL CREATION\n\n"

        "📍 Contract:\n"
        f"{item['address']}\n\n"

        "🔗 Chart:\n"
        f"https://dexscreener.com/solana/{item['pair_address']}\n\n"

        "⚠️ سقف محاسبه‌شده مربوط به همین استخر است، نه تمام بازارهای توکن.\n"
        "⚠️ ATH applies to this pool, not necessarily all token markets.\n"
        "⚠️ افت قیمت به‌تنهایی سیگنال خرید نیست.\n"
        "⚠️ A large drawdown alone is not a buy signal."
    )


# ==================================================
# MESSAGE 2: HIGH VOLUME ALERT
# ==================================================

def make_volume_message(item, source):
    return (
        "🎯 DEX HUNTER V10\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🟣 SOLANA HIGH VOLUME ALERT\n"
        "🟣 هشدار توکن‌های قدیمی با حجم بالا\n\n"

        f"🪙 {item['name']} ({item['symbol']})\n"
        f"📍 Discovery: {source}\n\n"

        "🇮🇷 تحلیل فارسی\n"
        f"📊 حجم معاملات ۲۴ ساعته: ${fa(item['volume'])}\n"
        f"⏳ سن استخر معاملاتی: {fa(item['pool_age_days'], 1)} روز\n"
        f"💵 قیمت فعلی: ${item['price']:.12g}\n"
        f"💧 نقدینگی: ${fa(item['liquidity'])}\n"
        f"💰 مارکت‌کپ: ${fa(item['market_cap'])}\n"
        f"🔗 تعداد استخرهای واجد شرایط: {fa(item['eligible_pools'], 0)}\n\n"

        "✅ سن استخر بیشتر از ۳ روز است.\n"
        "✅ حجم ۲۴ ساعته حداقل ۳۰۰ هزار دلار است.\n"
        "ℹ️ مارکت‌کپ و افت از سقف، شرط این فیلتر نیستند.\n\n"

        "🇬🇧 ENGLISH ANALYSIS\n"
        f"📊 24H Trading Volume: ${item['volume']:,.2f}\n"
        f"⏳ Trading pool age: {item['pool_age_days']:.1f} days\n"
        f"💵 Current price: ${item['price']:.12g}\n"
        f"💧 Liquidity: ${item['liquidity']:,.2f}\n"
        f"💰 Market cap: ${item['market_cap']:,.2f}\n"
        f"🔗 Eligible pools: {item['eligible_pools']}\n\n"

        "📍 Contract:\n"
        f"{item['address']}\n\n"

        "🔗 Chart:\n"
        f"https://dexscreener.com/solana/{item['pair_address']}\n\n"

        "⚠️ سن اعلام‌شده، سن استخر معاملاتی است؛ نه زمان دقیق ساخت توکن.\n"
        "⚠️ Pool age is not necessarily the token creation date.\n"
        "⚠️ حجم بالا به‌تنهایی سیگنال خرید نیست.\n"
        "⚠️ High volume alone is not a buy signal."
    )


# ==================================================
# INDEPENDENT ALERT DELIVERY
# ==================================================

def deliver_alert(state, filter_name, address, message, details):
    key, old = get_filter_record(
        state,
        filter_name,
        address
    )

    if old.get("alerted"):
        return False

    sent_to = old.get("sent_to", [])

    if not isinstance(sent_to, list):
        sent_to = []

    for chat_id in CHAT_IDS:
        if chat_id in sent_to:
            continue

        if post_one_chat(chat_id, message):
            sent_to.append(chat_id)

            state[key] = {
                "alerted": False,
                "sent_to": sent_to,
                "details": details,
                "date": now()
            }

            save_state(state)

        time.sleep(0.3)

    if all(chat_id in sent_to for chat_id in CHAT_IDS):
        state[key] = {
            "alerted": True,
            "sent_to": sent_to,
            "details": details,
            "date": now()
        }

        save_state(state)

        print(
            "ALERT COMPLETED:",
            filter_name,
            address
        )

        return True

    state[key] = {
        "alerted": False,
        "sent_to": sent_to,
        "details": details,
        "date": now()
    }

    save_state(state)
    return False


# ==================================================
# FILTER 1 SCANNER
# ==================================================

def scan_ath(candidates, state):
    alerts = 0
    checked = 0

    print("\n========== ATH SCANNER ==========")

    for candidate in candidates[:MAX_ATH_CANDIDATES]:
        address = candidate["address"]
        source = candidate["source"]

        key, old = get_filter_record(
            state, "ath", address
        )

        if old.get("alerted"):
            continue

        try:
            checked += 1

            result = analyze_ath_token(address)

            if not result:
                continue

            message = make_ath_message(result, source)

            if deliver_alert(
                state,
                "ath",
                address,
                message,
                {
                    "ath": result["ath"],
                    "drop": result["drop"],
                    "market_cap": result["market_cap"]
                }
            ):
                alerts += 1

            if alerts >= MAX_ATH_ALERTS:
                break

            time.sleep(0.5)

        except Exception as error:
            print("ATH TOKEN ERROR:", address, error)

    print(
        "ATH FINISHED | "
        f"Checked: {checked} | Alerts: {alerts}"
    )


# ==================================================
# FILTER 2 SCANNER
# ==================================================

def scan_volume(candidates, state):
    alerts = 0
    checked = 0

    print("\n========== HIGH VOLUME SCANNER ==========")

    for candidate in candidates[:MAX_VOLUME_CANDIDATES]:
        address = candidate["address"]
        source = candidate["source"]

        key, old = get_filter_record(
            state, "volume", address
        )

        if old.get("alerted"):
            continue

        try:
            checked += 1

            result = get_volume_candidate(address)

            if not result:
                continue

            message = make_volume_message(result, source)

            if deliver_alert(
                state,
                "volume",
                address,
                message,
                {
                    "volume_24h": result["volume"],
                    "pool_age_days": result["pool_age_days"]
                }
            ):
                alerts += 1

            if alerts >= MAX_VOLUME_ALERTS:
                break

            time.sleep(0.3)

        except Exception as error:
            print("VOLUME TOKEN ERROR:", address, error)

    print(
        "VOLUME FINISHED | "
        f"Checked: {checked} | Alerts: {alerts}"
    )


# ==================================================
# MAIN
# ==================================================

def scan():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing"
        )

    global PAIR_CACHE
    PAIR_CACHE = {}

    state = load_state()

    candidates = discover_tokens()

    print("\nTOTAL CANDIDATES:", len(candidates))

    scan_ath(candidates, state)
    scan_volume(candidates, state)

    save_state(state)

    print("\n================================")
    print("DEX HUNTER V10 FINISHED")
    print("ATH and HIGH VOLUME SCANNERS DONE")
    print("================================")


if __name__ == "__main__":
    scan()
