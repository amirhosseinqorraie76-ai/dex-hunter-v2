
import os
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_URL = "https://api.dexscreener.com"

MIN_LIQUIDITY = 10000
MIN_VOLUME = 5000
MAX_RESULTS = 10

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }, timeout=20)

def scan():
    url = f"{BASE_URL}/token-profiles/latest/v1"
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    tokens = response.json()
    sent = 0
    checked = set()

    for token in tokens:
        if token.get("chainId") != "solana":
            continue

        address = token.get("tokenAddress")
        if not address or address in checked:
            continue

        checked.add(address)

        try:
            pair_url = f"{BASE_URL}/token-pairs/v1/solana/{address}"
            result = requests.get(pair_url, timeout=20)
            result.raise_for_status()
            pairs = result.json()

            if not isinstance(pairs, list):
                continue

            for pair in sorted(
                pairs,
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0),
                reverse=True
            ):
                liquidity = (pair.get("liquidity") or {}).get("usd", 0)
                volume = (pair.get("volume") or {}).get("h24", 0)
                price = pair.get("priceUsd", "N/A")
                name = pair.get("baseToken", {}).get("name", "Unknown")
                symbol = pair.get("baseToken", {}).get("symbol", "N/A")
                pair_address = pair.get("pairAddress", "")

                if liquidity < MIN_LIQUIDITY:
                    continue
                if volume < MIN_VOLUME:
                    continue

                message = (
                    "🎯 DEX HUNTER | SOLANA\n\n"
                    f"🪙 {name} ({symbol})\n"
                    f"💵 Price: ${price}\n"
                    f"💧 Liquidity: ${liquidity:,.0f}\n"
                    f"📊 Volume 24h: ${volume:,.0f}\n\n"
                    f"📍 Token:\n{address}\n\n"
                    f"🔗 Chart:\nhttps://dexscreener.com/solana/{pair_address}\n\n"
                    "⚠️ این هشدار توصیه خرید نیست."
                )

                send_telegram(message)
                sent += 1
                break

            if sent >= MAX_RESULTS:
                break

        except Exception as error:
            print(f"Error: {error}")

    print(f"Scan completed. Alerts: {sent}")

if __name__ == "__main__":
    scan()
