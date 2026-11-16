
import os
import json
import time
import requests
from datetime import datetime, timezone

# =========================
# DEX HUNTER V6.3
# Persian + English
# =========================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

CHAT_IDS = [
    "6665909136",
    "6765810776",
    "98529959",
    "1507608801"
]

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1/tokens"
STATE_FILE = "seen_tokens.json"

MAX_ALERTS = 20
MAX_CANDIDATES = 65
MAX_SECURITY_CHECKS = 8

MIN_LIQ_EARLY = 2500
MIN_LIQ_PREPUMP = 4000
MIN_LIQ_MOMENTUM = 5000

MIN_SCORE_EARLY = 26
MIN_SCORE_PREPUMP = 34
MIN_SCORE_MOMENTUM = 40

session = requests.Session()
session.headers.update({"User-Agent": "DEX-Hunter-V6.3"})


def num(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def fa(value, decimals=0):
    try:
        result = f"{float(value):,.{decimals}f}"
        return result.translate(str.maketrans(
            "0123456789", "۰۱۲۳۴۵۶۷۸۹"
        ))
    except (ValueError, TypeError):
        return str(value)


def get_json(url, timeout=18):
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as error:
        print("API ERROR:", url, str(error))
        return None


def send_telegram(message):
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing")
        return False

    success_count = 0
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

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
            result = response.json()

            if result.get("ok"):
                success_count += 1
                print("Telegram sent:", chat_id)
            else:
                print("Telegram rejected:", chat_id, result)

        except Exception as error:
            print("Telegram error:", chat_id, str(error))

    print(f"Telegram delivery: {success_count}/{len(CHAT_IDS)}")
    return success_count == len(CHAT_IDS)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)

        if not isinstance(raw, dict):
            return {}

        clean = {}

        for address, value in raw.items():
            if isinstance(value, dict):
                clean[address] = value
            else:
                clean[address] = {
                    "sent": True,
                    "score": 0,
                    "volume": 0
                }

        return clean

    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False)
    except Exception as error:
        print("STATE SAVE ERROR:", str(error))


def discover_tokens():
    endpoints = [
        "/token-profiles/latest/v1",
        "/token-boosts/latest/v1",
        "/token-boosts/top/v1",
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

            if item.get("chainId") != "solana":
                continue

            address = item.get("tokenAddress")

            if address:
                found[address] = item

    print("Discovered:", len(found))
    return list(found.items())[:MAX_CANDIDATES]


def analyze(pair):
    liquidity = num((pair.get("liquidity") or {}).get("usd"))

    volume = pair.get("volume") or {}
    v5 = num(volume.get("m5"))
    v1 = num(volume.get("h1"))

    txns = pair.get("txns") or {}
    t5 = txns.get("m5") or {}
    t1 = txns.get("h1") or {}

    buys5 = num(t5.get("buys"))
    sells5 = num(t5.get("sells"))
    buys1 = num(t1.get("buys"))
    sells1 = num(t1.get("sells"))

    total1 = buys1 + sells1
    ratio = buys1 / max(sells1, 1)

    changes = pair.get("priceChange") or {}
    c5 = num(changes.get("m5"))
    c1 = num(changes.get("h1"))

    fdv = num(pair.get("fdv"))
    created = pair.get("pairCreatedAt")

    age = None

    if created:
        age = max(
            0,
            (datetime.now(timezone.utc).timestamp()
             - num(created) / 1000) / 3600
        )

    score = 0

    if liquidity >= 50000:
        score += 15
    elif liquidity >= 15000:
        score += 12
    elif liquidity >= 5000:
        score += 9
    elif liquidity >= 2500:
        score += 5

    if v1 >= 50000:
        score += 15
    elif v1 >= 10000:
        score += 12
    elif v1 >= 1000:
        score += 8
    elif v1 >= 300:
        score += 4

    if total1 >= 200:
        score += 15
    elif total1 >= 50:
        score += 12
    elif total1 >= 10:
        score += 8
    elif total1 >= 3:
        score += 4

    if ratio >= 2:
        score += 15
    elif ratio >= 1.5:
        score += 12
    elif ratio >= 1:
        score += 8
    else:
        score -= 8

    if 0 < c5 <= 10:
        score += 8
    elif c5 > 10:
        score += 5
    elif c5 < -5:
        score -= 5

    if 3 <= c1 <= 30:
        score += 10
    elif 30 < c1 <= 60:
        score += 6
    elif c1 > 100:
        score -= 15
    elif c1 < -10:
        score -= 8

    if age is not None:
        if age <= 6:
            score += 10
        elif age <= 24:
            score += 8
        elif age <= 72:
            score += 4

    if fdv and liquidity and fdv / liquidity > 40:
        score -= 8

    if liquidity and v1 / liquidity > 15:
        score -= 5

    score = max(0, min(100, score))

    return {
        "score": score,
        "liquidity": liquidity,
        "volume": v1,
        "volume5": v5,
        "buys": buys1,
        "sells": sells1,
        "buys5": buys5,
        "sells5": sells5,
        "ratio": ratio,
        "change": c1,
        "change5": c5,
        "age": age,
        "price": pair.get("priceUsd", "Unknown")
    }


def security_check(address):
    data = get_json(f"{RUG}/{address}/report", timeout=15)

    if not isinstance(data, dict):
        return (
            "گزارش امنیت دریافت نشد | Security report unavailable",
            [
                "⚠️ امنیت تأیید نشده است | Security not verified"
            ]
        )

    warnings = []

    token = data.get("token") or {}
    mint = data.get("mintAuthority") or token.get("mintAuthority")
    freeze = data.get("freezeAuthority") or token.get("freezeAuthority")

    if mint:
        warnings.append(
            "⚠️ اختیار ضرب توکن فعال است | Mint authority is active"
        )

    if freeze:
        warnings.append(
            "⚠️ اختیار فریز توکن فعال است | Freeze authority is active"
        )

    holders = data.get("topHolders") or []

    percentages = [
        num(holder.get("pct"))
        for holder in holders[:10]
        if isinstance(holder, dict)
    ]

    concentration = sum(percentages)

    if percentages and concentration >= 80:
        warnings.append(
            "🚨 تمرکز بسیار زیاد در ۱۰ هولدر اول | "
            "Very high concentration among top 10 holders"
        )
    elif percentages and concentration >= 60:
        warnings.append(
            "⚠️ تمرکز بالا در ۱۰ هولدر اول | "
            "High concentration among top 10 holders"
        )

    for risk in data.get("risks") or []:
        if not isinstance(risk, dict):
            continue

        level = str(risk.get("level", "")).lower()
        name = risk.get("name", "ریسک نامشخص")

        if level in ("danger", "critical", "high"):
            warnings.append(
                f"🚨 ریسک: {name} | Risk: {name}"
            )

    if not warnings:
        warnings.append(
            "ریسک مهمی ثبت نشده | No major risk reported"
        )

    return (
        "گزارش دریافت شد | Report received",
        warnings[:5]
    )


def classify(data):
    score = data["score"]
    liquidity = data["liquidity"]
    volume = data["volume"]
    txns = data["buys"] + data["sells"]
    change = data["change"]

    if (
        data["age"] is not None
        and data["age"] <= 6
        and liquidity >= MIN_LIQ_EARLY
        and volume >= 300
        and txns >= 3
        and score >= MIN_SCORE_EARLY
    ):
        return "🟡 شکار زودهنگام | Early Discovery"

    if (
        liquidity >= MIN_LIQ_PREPUMP
        and volume >= 1000
        and txns >= 5
        and score >= MIN_SCORE_PREPUMP
        and change <= 60
    ):
        return "🟢 احتمال رشد اولیه | Early Growth Potential"

    if (
        liquidity >= MIN_LIQ_MOMENTUM
        and volume >= 2000
        and txns >= 8
        and score >= MIN_SCORE_MOMENTUM
        and 0 < change <= 100
    ):
        return "🔥 مومنتوم فعال | Active Momentum"

    return None


def make_message(name, symbol, address, pair, data, category, security):
    age = (
        f"{fa(data['age'], 1)} ساعت | {data['age']:.1f} hours"
        if data["age"] is not None
        else "نامشخص | Unknown"
    )

    warning_text = "\n".join(security[1])
    pair_address = pair.get("pairAddress", "")

    score = data["score"]
    ratio = data["ratio"]

    if score >= 75:
        analysis_fa = "قدرت بازار بالا است؛ اما ریسک همچنان وجود دارد."
        analysis_en = "Market strength is high, but risk remains."
    elif score >= 55:
        analysis_fa = "نشانه‌های مثبتی دیده می‌شود؛ برای تأیید بیشتر بررسی کن."
        analysis_en = "Positive signals detected; further confirmation is needed."
    elif score >= 35:
        analysis_fa = "سیگنال اولیه است و هنوز قدرت کافی تأیید نشده."
        analysis_en = "Early signal; strength is not yet fully confirmed."
    else:
        analysis_fa = "قدرت سیگنال پایین است؛ احتیاط کن."
        analysis_en = "Signal strength is low; proceed with caution."

    if ratio > 1.5:
        flow_fa = "تعداد خریدها از فروش‌ها بیشتر است."
        flow_en = "Buy transactions are higher than sell transactions."
    elif ratio >= 1:
        flow_fa = "خرید و فروش تقریباً متعادل هستند."
        flow_en = "Buying and selling activity is relatively balanced."
    else:
        flow_fa = "فشار فروش بیشتر از خرید است."
        flow_en = "Selling pressure is stronger than buying pressure."

    return (
        "🎯 DEX HUNTER V6.3\n"
        "━━━━━━━━━━━━━━\n"
        f"🪙 {name} ({symbol})\n"
        f"📌 نوع شکار | Signal: {category}\n"
        f"⭐ امتیاز بازار | Market Score: {fa(score)}/۱۰۰ | {score}/100\n\n"

        "🇮🇷 تحلیل فارسی\n"
        f"• {analysis_fa}\n"
        f"• {flow_fa}\n\n"

        "🇬🇧 English Analysis\n"
        f"• {analysis_en}\n"
        f"• {flow_en}\n\n"

        "📊 اطلاعات بازار | Market Data\n"
        f"💧 نقدینگی | Liquidity: ${fa(data['liquidity'])}\n"
        f"📈 حجم یک‌ساعته | 1H Volume: ${fa(data['volume'])}\n"
        f"⚡ حجم ۵ دقیقه | 5M Volume: ${fa(data['volume5'])}\n"
        f"🟢 خرید یک‌ساعته | 1H Buys: {fa(data['buys'])}\n"
        f"🔴 فروش یک‌ساعته | 1H Sells: {fa(data['sells'])}\n"
        f"⚖️ نسبت خرید/فروش | Buy/Sell Ratio: {fa(ratio, 2)}\n"
        f"📈 تغییر ۵ دقیقه | 5M Change: {fa(data['change5'], 2)}٪ | {data['change5']:.2f}%\n"
        f"📈 تغییر یک‌ساعته | 1H Change: {fa(data['change'], 2)}٪ | {data['change']:.2f}%\n"
        f"⏳ عمر استخر | Pool Age: {age}\n"
        f"💵 قیمت | Price: ${data['price']}\n\n"

        "🛡️ بررسی امنیت | Security Check\n"
        f"وضعیت | Status: {security[0]}\n"
        f"{warning_text}\n\n"

        "📍 آدرس قرارداد | Contract Address:\n"
        f"{address}\n\n"

        "🔗 نمودار | Chart:\n"
        f"https://dexscreener.com/solana/{pair_address}\n\n"

        "⚠️ امتیاز بازار تضمین سود یا امنیت نیست.\n"
        "⚠️ Market score does not guarantee profit or safety.\n"
        "⚠️ تعداد خرید و فروش، تعداد کیف پول‌های منحصربه‌فرد نیست.\n"
        "⚠️ Transaction counts do not represent unique wallets."
    )


def scan():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    state = load_state()
    candidates = discover_tokens()

    sent = 0
    security_checks = 0

    for address, source in candidates:
        try:
            old = state.get(address, {})

            if not isinstance(old, dict):
                old = {}

            pairs = get_json(
                f"{DEX}/token-pairs/v1/solana/{address}"
            )

            if not isinstance(pairs, list) or not pairs:
                continue

            pairs.sort(
                key=lambda p: num(
                    (p.get("liquidity") or {}).get("usd")
                ),
                reverse=True
            )

            pair = pairs[0]
            base = pair.get("baseToken") or {}

            data = analyze(pair)
            category = classify(data)

            if not category:
                continue

            previous_score = num(old.get("score"))
            previous_volume = num(old.get("volume"))

            is_new = not old.get("sent")

            score_upgrade = (
                data["score"] >= previous_score + 10
            )

            volume_jump = (
                previous_volume > 0
                and data["volume"] >= previous_volume * 2
            )

            should_alert = (
                is_new or (
                    old.get("sent")
                    and (score_upgrade or volume_jump)
                )
            )

            if not should_alert:
                old["score"] = data["score"]
                old["volume"] = data["volume"]
                state[address] = old
                continue

            if security_checks < MAX_SECURITY_CHECKS:
                security = security_check(address)
                security_checks += 1
                time.sleep(0.3)
            else:
                security = (
                    "در این نوبت بررسی نشد | Not checked this scan",
                    [
                        "⚠️ وضعیت امنیت نامشخص است | "
                        "Security status is unknown"
                    ]
                )

            message = make_message(
                base.get("name", "Unknown"),
                base.get("symbol", "N/A"),
                address,
                pair,
                data,
                category,
                security
            )

            delivered = send_telegram(message)

            if delivered:
                state[address] = {
                    "sent": True,
                    "score": data["score"],
                    "volume": data["volume"],
                    "last_alert": datetime.now(
                        timezone.utc
                    ).isoformat()
                }

                sent += 1
                save_state(state)

            if sent >= MAX_ALERTS:
                break

            time.sleep(0.2)

        except Exception as error:
            print("TOKEN ERROR:", address, str(error))
            continue

    save_state(state)

    print(
        "DEX Hunter V6.3 finished | "
        f"Candidates: {len(candidates)} | "
        f"Alerts: {sent} | "
        f"Security checks: {security_checks}"
    )


if __name__ == "__main__":
    scan()
