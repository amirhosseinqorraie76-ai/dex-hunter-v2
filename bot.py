
import os
import json
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://api.dexscreener.com"

MIN_LIQUIDITY = 10000
MIN_VOLUME_H1 = 3000
MIN_TXNS_H1 = 15
MIN_SCORE = 60
MAX_ALERTS = 10

SEEN_FILE = "seen_tokens.json"

session = requests.Session()
session.headers.update({"User-Agent": "DEX-Hunter/2.0"})


def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f)


def send_telegram(message):
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
        raise RuntimeError("Telegram rejected the message")


def calculate_score(pair):
    liquidity = (pair.get("liquidity") or {}).get("usd", 0)
    volume = (pair.get("volume") or {}).get("h1", 0)

    txns = (pair.get("txns") or {}).get("h1") or {}
    buys = txns.get("buys", 0)
    sells = txns.get("sells", 0)
    total = buys + sells

    change = (pair.get("priceChange") or {}).get("h1", 0)
    fdv = pair.get("fdv") or 0

    score = 0

    if liquidity >= 50000:
        score += 20
    elif liquidity >= 20000:
        score += 15
    elif liquidity >= MIN_LIQUIDITY:
        score += 10

    if volume >= 50000:
        score += 30
    elif volume >= 15000:
        score += 25
    elif volume >= MIN_VOLUME_H1:
        score += 20

    if total >= 100:
        score += 20
    elif total >= 50:
        score += 15
    elif total >= MIN_TXNS_H1:
        score += 10

    ratio = buys / max(sells, 1)

    if ratio >= 1.5:
        score += 15
    elif ratio >= 1.2:
        score += 10
    elif ratio >= 1:
        score += 5

    if change > 10:
        score += 10
    elif change > 0:
        score += 5

    if sells > buys:
        score -= 10

    if fdv and liquidity and fdv / liquidity > 30:
        score -= 10

    return max(0, min(100, score))


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
            pair_url = f"{BASE}/token-pairs/v1/solana/{address}"
            result = session.get(pair_url, timeout=20)
            result.raise_for_status()

            pairs = result.json()
            if not isinstance(pairs, list):
                continue

            pairs.sort(
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
                reverse=True
            )

            for pair in pairs:
                liquidity = (pair.get("liquidity") or {}).get("usd", 0)
                volume = (pair.get("volume") or {}).get("h1", 0)

                txns = (pair.get("txns") or {}).get("h1") or {}
                total = txns.get("buys", 0) + txns.get("sells", 0)

                if liquidity < MIN_LIQUIDITY:
                    continue
                if volume < MIN_VOLUME_H1:
                    continue
                if total < MIN_TXNS_H1:
                    continue

                score = calculate_score(pair)

                if score < MIN_SCORE:
                    continue

                base_token = pair.get("baseToken") or {}
                name = base_token.get("name", "Unknown")
                symbol = base_token.get("symbol", "N/A")

                buys = txns.get("buys", 0)
                sells = txns.get("sells", 0)
                price = pair.get("priceUsd", "N/A")
                change = (pair.get("priceChange") or {}).get("h1", 0)
                pair_address = pair.get("pairAddress", "")

                message = (
                    "🎯 DEX HUNTER V2\n"
                    "━━━━━━━━━━━━━━\n"
                    f"🪙 {name} ({symbol})\n"
                    f"⭐ SCORE: {score}/100\n\n"
                    f"💧 Liquidity: ${liquidity:,.0f}\n"
                    f"📊 Volume 1H: ${volume:,.0f}\n"
                    f"🟢 Buys: {buys}\n"
                    f"🔴 Sells: {sells}\n"
                    f"📈 Price Change 1H: {change}%\n"
                    f"💵 Price: ${price}\n\n"
                    f"📍 Token:\n{address}\n\n"
                    f"🔗 Chart:\n"
                    f"https://dexscreener.com/solana/{pair_address}\n\n"
                    "⚠️ امتیاز بالا تضمین رشد یا امنیت نیست."
                )

                send_telegram(message)

                seen[address] = datetime.now(timezone.utc).isoformat()
                save_seen(seen)

                sent += 1
                break

            if sent >= MAX_ALERTS:
                break

        except Exception as error:
            print(f"Token error {address}: {error}")

    if len(seen) > 5000:
        seen = dict(list(seen.items())[-4000:])
        save_seen(seen)

    print(f"Scan complete. New alerts: {sent}")


if __name__ == "__main__":
    scan()
