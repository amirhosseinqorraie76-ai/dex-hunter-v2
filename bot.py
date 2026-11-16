
import os
import json
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://api.dexscreener.com"
SEEN_FILE = "seen_tokens.json"

MIN_LIQUIDITY = 15000
MIN_VOLUME = 5000
MIN_TXNS = 20
MIN_SCORE = 60
MAX_ALERTS = 10

session = requests.Session()
session.headers.update({"User-Agent": "DEX-Hunter-V3"})


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(data):
    with open(SEEN_FILE, "w") as f:
        json.dump(data, f)


def send_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = session.post(url, data={
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }, timeout=20)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram send failed")


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
    age_hours = None

    if created:
        age_hours = (
            datetime.now(timezone.utc).timestamp()
            - created / 1000
        ) / 3600

    score = 0

    # Liquidity
    if liquidity >= 50000:
        score += 20
    elif liquidity >= 25000:
        score += 15
    elif liquidity >= MIN_LIQUIDITY:
        score += 10

    # Trading volume
    if volume >= 50000:
        score += 25
    elif volume >= 15000:
        score += 20
    elif volume >= MIN_VOLUME:
        score += 15

    # Transaction activity
    if total >= 200:
        score += 20
    elif total >= 80:
        score += 15
    elif total >= MIN_TXNS:
        score += 10

    # Buy pressure
    ratio = buys / max(sells, 1)

    if ratio >= 1.5:
        score += 15
    elif ratio >= 1.2:
        score += 10
    elif ratio >= 1:
        score += 5

    # Early momentum
    if 5 <= change <= 30:
        score += 15
    elif 30 < change <= 60:
        score += 8
    elif change > 100:
        score -= 20
    elif change > 60:
        score -= 10
    elif change < -10:
        score -= 10

    # Pool age
    if age_hours is not None:
        if 0 <= age_hours <= 24:
            score += 10
        elif age_hours <= 72:
            score += 5
        elif age_hours < 0:
            score -= 10

    # Risk penalties
    if sells > buys:
        score -= 10

    if fdv and liquidity and fdv / liquidity > 30:
        score -= 10

    if liquidity and volume / liquidity > 10:
        score -= 10

    score = max(0, min(100, score))

    if change > 100:
        category = "PUMP"
    elif change > 60:
        category = "OVERHEATED"
    else:
        category = "EARLY"

    return {
        "score": score,
        "category": category,
        "liquidity": liquidity,
        "volume": volume,
        "buys": buys,
        "sells": sells,
        "change": change,
        "price": pair.get("priceUsd", "N/A"),
        "age": age_hours,
        "ratio": ratio
    }


def scan():
    seen = load_seen()

    url = f"{BASE}/token-profiles/latest/v1"
    response = session.get(url, timeout=30)
    response.raise_for_status()

    tokens = response.json()
    sent = 0

    for token in tokens:
        if token.get("chainId") != "solana":
            continue

        address = token.get("tokenAddress")

        if not address or address in seen:
            continue

        try:
            url = f"{BASE}/token-pairs/v1/solana/{address}"
            r = session.get(url, timeout=20)
            r.raise_for_status()

            pairs = r.json()

            if not isinstance(pairs, list):
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

                base = pair.get("baseToken") or {}
                name = base.get("name", "Unknown")
                symbol = base.get("symbol", "N/A")
                pair_address = pair.get("pairAddress", "")

                labels = {
                    "EARLY": "🟢 فرصت اولیه",
                    "PUMP": "🔴 پامپ پرریسک",
                    "OVERHEATED": "🟠 احتمال ورود دیرهنگام"
                }

                age = (
                    f"{data['age']:.1f} ساعت"
                    if data["age"] is not None
                    else "نامشخص"
                )

                message = (
                    "🎯 DEX HUNTER V3\n"
                    "━━━━━━━━━━━━━━\n"
                    f"🪙 {name} ({symbol})\n"
                    f"{labels[data['category']]}\n"
                    f"⭐ SCORE: {data['score']}/100\n\n"
                    f"💧 Liquidity: ${data['liquidity']:,.0f}\n"
                    f"📊 Volume 1H: ${data['volume']:,.0f}\n"
                    f"🟢 Buys: {data['buys']}\n"
                    f"🔴 Sells: {data['sells']}\n"
                    f"⚖️ Buy/Sell: {data['ratio']:.2f}\n"
                    f"📈 Change 1H: {data['change']}%\n"
                    f"⏳ Pool Age: {age}\n"
                    f"💵 Price: ${data['price']}\n\n"
                    f"📍 Token:\n{address}\n\n"
                    f"🔗 Chart:\n"
                    f"https://dexscreener.com/solana/{pair_address}\n\n"
                    "⚠️ امتیاز، تضمین امنیت یا سود نیست."
                )

                send_message(message)

                seen[address] = datetime.now(
                    timezone.utc
                ).isoformat()

                save_seen(seen)
                sent += 1
                break

            if sent >= MAX_ALERTS:
                break

        except Exception as error:
            print(f"Error {address}: {error}")

    print(f"V3 scan finished. Alerts: {sent}")


if __name__ == "__main__":
    scan()
