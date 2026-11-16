
import os
import json
import time
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://api.dexscreener.com"
SEEN_FILE = "seen_tokens.json"

# V4 - Flexible filters
MIN_LIQUIDITY = 8000
MIN_VOLUME = 2000
MIN_TXNS = 10
MIN_SCORE = 45
MAX_ALERTS = 10

session = requests.Session()
session.headers.update({"User-Agent": "DEX-Hunter-V4"})


def telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = session.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=20
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(data):
    with open(SEEN_FILE, "w") as f:
        json.dump(data, f)


def analyze(pair):
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume = (pair.get("volume") or {}).get("h1", 0)
    tx = (pair.get("txns") or {}).get("h1") or {}

    buys = tx.get("buys", 0)
    sells = tx.get("sells", 0)
    total = buys + sells

    change = (pair.get("priceChange") or {}).get("h1", 0)
    fdv = pair.get("fdv") or 0

    created = pair.get("pairCreatedAt")
    age = None

    if created:
        age = (
            datetime.now(timezone.utc).timestamp()
            - created / 1000
        ) / 3600

    score = 0

    if liquidity >= 50000:
        score += 20
    elif liquidity >= 20000:
        score += 15
    elif liquidity >= MIN_LIQUIDITY:
        score += 10

    if volume >= 50000:
        score += 20
    elif volume >= 10000:
        score += 15
    elif volume >= MIN_VOLUME:
        score += 10

    if total >= 200:
        score += 20
    elif total >= 50:
        score += 15
    elif total >= MIN_TXNS:
        score += 10

    ratio = buys / max(sells, 1)

    if ratio >= 2:
        score += 15
    elif ratio >= 1.3:
        score += 10
    elif ratio >= 1:
        score += 5
    else:
        score -= 10

    if 3 <= change <= 30:
        score += 15
    elif 30 < change <= 60:
        score += 8
    elif change > 100:
        score -= 20
    elif change > 60:
        score -= 10
    elif change < -10:
        score -= 10

    if age is not None:
        if 0 <= age <= 24:
            score += 10
        elif age <= 72:
            score += 5
        elif age < 0:
            score -= 10

    if sells > buys:
        score -= 10

    if fdv and liquidity and fdv / liquidity > 30:
        score -= 10

    if liquidity and volume / liquidity > 10:
        score -= 10

    score = max(0, min(100, score))

    if change > 100:
        category = "PUMP - HIGH RISK"
    elif change > 60:
        category = "OVERHEATED"
    elif score >= 65:
        category = "STRONG EARLY"
    else:
        category = "EARLY WATCH"

    return {
        "score": score,
        "category": category,
        "liquidity": liquidity,
        "volume": volume,
        "buys": buys,
        "sells": sells,
        "change": change,
        "price": pair.get("priceUsd", "N/A"),
        "age": age,
        "ratio": ratio
    }


def scan():
    seen = load_seen()
    sent = 0

    response = session.get(
        f"{BASE}/token-profiles/latest/v1",
        timeout=30
    )
    response.raise_for_status()

    tokens = response.json()

    if not isinstance(tokens, list):
        raise RuntimeError("Unexpected DexScreener response")

    for token in tokens:
        if token.get("chainId") != "solana":
            continue

        address = token.get("tokenAddress")

        if not address or address in seen:
            continue

        try:
            r = session.get(
                f"{BASE}/token-pairs/v1/solana/{address}",
                timeout=20
            )
            r.raise_for_status()
            pairs = r.json()

            if not isinstance(pairs, list) or not pairs:
                continue

            pairs.sort(
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
                reverse=True
            )

            for pair in pairs:
                data = analyze(pair)

                if data["liquidity"] < MIN_LIQUIDITY:
                    continue
                if data["volume"] < MIN_VOLUME:
                    continue
                if data["buys"] + data["sells"] < MIN_TXNS:
                    continue
                if data["score"] < MIN_SCORE:
                    continue

                if data["age"] is not None and data["age"] < 0:
                    continue

                base = pair.get("baseToken") or {}
                name = base.get("name", "Unknown")
                symbol = base.get("symbol", "N/A")
                pair_address = pair.get("pairAddress", "")

                age_text = (
                    f"{data['age']:.1f} hours"
                    if data["age"] is not None
                    else "Unknown"
                )

                message = (
                    "🎯 DEX HUNTER V4\n"
                    "━━━━━━━━━━━━━━\n"
                    f"🪙 {name} ({symbol})\n"
                    f"📌 {data['category']}\n"
                    f"⭐ SCORE: {data['score']}/100\n\n"
                    f"💧 Liquidity: ${data['liquidity']:,.0f}\n"
                    f"📊 Volume 1H: ${data['volume']:,.0f}\n"
                    f"🟢 Buys: {data['buys']}\n"
                    f"🔴 Sells: {data['sells']}\n"
                    f"⚖️ Buy/Sell: {data['ratio']:.2f}\n"
                    f"📈 Change 1H: {data['change']}%\n"
                    f"⏳ Pool Age: {age_text}\n"
                    f"💵 Price: ${data['price']}\n\n"
                    f"📍 Token:\n{address}\n\n"
                    f"🔗 Chart:\n"
                    f"https://dexscreener.com/solana/{pair_address}\n\n"
                    "⚠️ Score is not a guarantee of safety or profit."
                )

                telegram(message)

                seen[address] = datetime.now(
                    timezone.utc
                ).isoformat()

                save_seen(seen)
                sent += 1
                break

            if sent >= MAX_ALERTS:
                break

        except Exception as error:
            print(f"Token error {address}: {error}")

        time.sleep(0.15)

    print(f"DEX HUNTER V4 finished. Alerts: {sent}")


if __name__ == "__main__":
    scan()
