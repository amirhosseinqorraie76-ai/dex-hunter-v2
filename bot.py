
import os
import json
import time
import requests
from datetime import datetime, timezone

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

CHAT_IDS = [
    "6665909136",
    "6765810776",
    "98529959"
]

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1/tokens"
STATE_FILE = "seen_tokens.json"

# فیلترهای نسخه V6
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
session.headers.update({"User-Agent": "DEX-Hunter-V6"})


def fa(value, decimals=0):
    try:
        return f"{float(value):,.{decimals}f}".translate(
            str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
        )
    except (ValueError, TypeError):
        return str(value)


def number(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0.0


def get_json(url, timeout=20):
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("API error:", url, e)
        return None


def telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    for chat_id in CHAT_IDS:
        try:
            r = session.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_web_page_preview": True
                },
                timeout=20
            )
            r.raise_for_status()

            if not r.json().get("ok"):
                print("Telegram rejected:", chat_id, r.text)

        except Exception as e:
            print("Telegram error:", chat_id, e)


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


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
            if item.get("chainId") != "solana":
                continue

            address = item.get("tokenAddress")

            if address:
                found[address] = item

    print("Discovered tokens:", len(found))
    return list(found.items())[:MAX_CANDIDATES]


def analyze(pair):
    liquidity = number((pair.get("liquidity") or {}).get("usd"))

    volume = pair.get("volume") or {}
    v5 = number(volume.get("m5"))
    v1 = number(volume.get("h1"))

    txns = pair.get("txns") or {}
    t5 = txns.get("m5") or {}
    t1 = txns.get("h1") or {}

    buys5 = number(t5.get("buys"))
    sells5 = number(t5.get("sells"))
    buys1 = number(t1.get("buys"))
    sells1 = number(t1.get("sells"))

    total1 = buys1 + sells1
    ratio = buys1 / max(sells1, 1)

    change = pair.get("priceChange") or {}
    c5 = number(change.get("m5"))
    c1 = number(change.get("h1"))

    fdv = number(pair.get("fdv"))
    created = pair.get("pairCreatedAt")

    age = None
    if created:
        age = (
            datetime.now(timezone.utc).timestamp()
            - number(created) / 1000
        ) / 3600

    # امتیاز بازار؛ مستقل از امتیاز امنیت
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
        if 0 <= age <= 6:
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
        "price": pair.get("priceUsd", "نامشخص"),
        "fdv": fdv
    }


def security_check(address):
    data = get_json(f"{RUG}/{address}/report", timeout=15)

    if not isinstance(data, dict):
        return {
            "status": "نامشخص؛ سرویس پاسخ نداد",
            "warnings": []
        }

    token = data.get("token") or {}
    warnings = []

    mint = data.get("mintAuthority") or token.get("mintAuthority")
    freeze = data.get("freezeAuthority") or token.get("freezeAuthority")

    if mint:
        warnings.append("⚠️ اختیار ضرب توکن فعال است")

    if freeze:
        warnings.append("⚠️ اختیار فریز توکن فعال است")

    holders = data.get("topHolders") or []
    percentages = [
        number(h.get("pct"))
        for h in holders[:10]
        if isinstance(h, dict)
    ]

    concentration = sum(percentages)

    if percentages:
        if concentration >= 80:
            warnings.append("🚨 تمرکز بسیار زیاد در ۱۰ هولدر اول")
        elif concentration >= 60:
            warnings.append("⚠️ تمرکز بالا در ۱۰ هولدر اول")

    risks = data.get("risks") or []

    for risk in risks:
        if not isinstance(risk, dict):
            continue

        level = str(risk.get("level", "")).lower()
        name = risk.get("name", "ریسک نامشخص")

        if level in ("danger", "critical", "high"):
            warnings.append("🚨 " + str(name))

    if not warnings:
        warnings.append("ریسک مهمی در گزارش دریافتی ثبت نشده")

    return {
        "status": "گزارش دریافت شد",
        "warnings": warnings[:5]
    }


def classify(data):
    score = data["score"]
    liquidity = data["liquidity"]
    volume = data["volume"]
    txns = data["buys"] + data["sells"]
    change = data["change"]

    if (
        liquidity >= MIN_LIQ_EARLY
        and volume >= 300
        and txns >= 3
        and score >= MIN_SCORE_EARLY
        and data["age"] is not None
        and data["age"] <= 6
    ):
        return "🟡 شکار زودهنگام", "early"

    if (
        liquidity >= MIN_LIQ_PREPUMP
        and volume >= 1000
        and txns >= 5
        and score >= MIN_SCORE_PREPUMP
        and change <= 60
    ):
        return "🟢 احتمال رشد اولیه", "prepump"

    if (
        liquidity >= MIN_LIQ_MOMENTUM
        and volume >= 2000
        and txns >= 8
        and score >= MIN_SCORE_MOMENTUM
        and 0 < change <= 100
    ):
        return "🔥 مومنتوم فعال", "momentum"

    return None, None


def make_message(name, symbol, address, pair, data, category, security):
    pair_address = pair.get("pairAddress", "")
    age = (
        f"{fa(data['age'], 1)} ساعت"
        if data["age"] is not None
        else "نامشخص"
    )

    warnings = security["warnings"]
    warning_text = "\n".join(warnings)

    return (
        "🎯 DEX HUNTER V6\n"
        "━━━━━━━━━━━━━━\n"
        f"🪙 {name} ({symbol})\n"
        f"📌 نوع شکار: {category}\n"
        f"⭐ امتیاز بازار: {fa(data['score'])}/۱۰۰\n\n"
        "📊 تحلیل بازار\n"
        f"💧 نقدینگی: ${fa(data['liquidity'])}\n"
        f"📈 حجم یک‌ساعته: ${fa(data['volume'])}\n"
        f"⚡ حجم ۵ دقیقه: ${fa(data['volume5'])}\n"
        f"🟢 خرید یک‌ساعته: {fa(data['buys'])}\n"
        f"🔴 فروش یک‌ساعته: {fa(data['sells'])}\n"
        f"⚖️ نسبت خرید/فروش: {fa(data['ratio'], 2)}\n"
        f"📈 تغییر قیمت ۵ دقیقه: {fa(data['change5'], 2)}٪\n"
        f"📈 تغییر قیمت یک‌ساعته: {fa(data['change'], 2)}٪\n"
        f"⏳ عمر استخر: {age}\n"
        f"💵 قیمت: ${data['price']}\n\n"
        "🛡️ بررسی امنیت\n"
        f"وضعیت: {security['status']}\n"
        f"{warning_text}\n\n"
        "📍 قرارداد:\n"
        f"{address}\n\n"
        f"🔗 نمودار:\nhttps://dexscreener.com/solana/{pair_address}\n\n"
        "⚠️ امتیاز بازار، تضمین سود یا امنیت نیست.\n"
        "⚠️ تعداد خرید و فروش، تعداد کیف پول‌های منحصربه‌فرد نیست."
    )


def scan():
    state = load_state()
    candidates = discover_tokens()

    sent = 0
    security_checks = 0

    for address, source in candidates:
        old = state.get(address, {})

        pairs = get_json(
            f"{DEX}/token-pairs/v1/solana/{address}"
        )

        if not isinstance(pairs, list) or not pairs:
            continue

        pairs.sort(
            key=lambda p: number(
                (p.get("liquidity") or {}).get("usd")
            ),
            reverse=True
        )

        pair = pairs[0]
        base = pair.get("baseToken") or {}

        data = analyze(pair)
        category, mode = classify(data)

        if not category:
            continue

        previous_score = number(old.get("score"))
        previous_volume = number(old.get("volume"))

        is_new = not old.get("sent")
        score_upgrade = data["score"] >= previous_score + 10
        volume_jump = (
            previous_volume > 0
            and data["volume"] >= previous_volume * 2
        )

        should_alert = (
            is_new
            or (old.get("sent") and (score_upgrade or volume_jump))
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
            security = {
                "status": "در این نوبت بررسی نشد",
                "warnings": ["⚠️ وضعیت امنیت نامشخص است"]
            }

        name = base.get("name", "Unknown")
        symbol = base.get("symbol", "N/A")

        message = make_message(
            name, symbol, address, pair,
            data, category, security
        )

        telegram(message)

        state[address] = {
            "sent": True,
            "score": data["score"],
            "volume": data["volume"],
            "last_alert": datetime.now(timezone.utc).isoformat()
        }

        save_state(state)

        sent += 1

        if sent >= MAX_ALERTS:
            break

        time.sleep(0.2)

    save_state(state)

    print(
        f"DEX Hunter V6 finished. "
        f"Candidates: {len(candidates)} | "
        f"Alerts: {sent} | "
        f"Security checks: {security_checks}"
    )


if __name__ == "__main__":
    scan()
