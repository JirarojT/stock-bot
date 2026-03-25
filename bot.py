"""
Thai Stock AI Bot — Final Version
===================================
รันได้ 3 โหมด:
  python bot.py morning   → รายงานเช้า + หุ้นแนะนำ 3 ตัว (ราคาจริง)
  python bot.py evening   → รายงานเย็น + สรุปตลาด
  python bot.py monitor   → สแกนข่าวใหม่ real-time (รันทุก 5 นาที)

ตั้งค่า Secrets ใน GitHub:
  ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import sys
import json
import re
import os
import time
import hashlib
import requests
import feedparser
from datetime import datetime, timezone, timedelta
import anthropic

# ============================================================
#  SETTINGS — แก้ตรงนี้เท่านั้น
# ============================================================
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "sk-ant-xxxxxxxx")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "xxxxxxxx:xxxxxxxx")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "-100xxxxxxxxxx")

# สไตล์การเทรด: "swing" (2-7 วัน) หรือ "day" (จบในวัน)
TRADE_STYLE = "swing"

# งบต่อ trade (บาท) — ใช้คำนวณ lot แนะนำ
BUDGET_PER_TRADE = 10000

# หุ้นที่ต้องการให้ติดตามและแนะนำ
WATCHLIST = [
    "AOT", "CPALL", "GULF", "PTT", "BANPU",
    "KTB", "SCB", "ADVANC", "DELTA", "BBL",
    "PTTGC", "TOP", "BTS", "CENTEL", "CPN",
]

# คีย์เวิร์ดข่าวด่วน (จะ alert ทันที ไม่รอรอบถัดไป)
URGENT_KEYWORDS = [
    "ฉุกเฉิน", "หยุดพักการซื้อขาย", "halt",
    "Fed", "เฟด", "ดอกเบี้ย", "น้ำมัน",
    "สงคราม", "ตะวันออกกลาง", "เลือกตั้ง",
    "นายกรัฐมนตรี", "รัฐบาล", "circuit breaker",
]

# แหล่งข่าว RSS
RSS_FEEDS = [
    "https://www.set.or.th/th/news/rss/news.xml",
    "https://feeds.manager.co.th/manager-stock.xml",
    "https://rss.thansettakij.com/feed/stock",
    "https://www.bangkokbiznews.com/rss/data/finance.xml",
]

# ไฟล์เก็บ ID ข่าวที่ส่งไปแล้ว (ป้องกันส่งซ้ำ)
SEEN_FILE = "seen_news.json"
# ============================================================

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
TH_TZ  = timezone(timedelta(hours=7))


# ──────────────────────────────────────────────
#  Helpers พื้นฐาน
# ──────────────────────────────────────────────
def now_th() -> datetime:
    return datetime.now(TH_TZ)


def send_telegram(message: str, silent: bool = False):
    """ส่งข้อความ Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"[{now_th():%H:%M:%S}] Telegram OK ({len(message)} chars)")
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")


def ask_claude(system_prompt: str, user_msg: str,
               max_tokens: int = 800) -> str:
    """เรียก Claude API"""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        return resp.content[0].text
    except Exception as e:
        return f"[Claude Error] {e}"


# ──────────────────────────────────────────────
#  ดึงราคาหุ้น (Settrade → Yahoo Finance fallback)
# ──────────────────────────────────────────────
def fetch_stock_price(symbol: str) -> float | None:
    """ดึงราคาจาก Settrade ก่อน ถ้าไม่ได้ใช้ Yahoo Finance"""

    # ลอง Settrade ก่อน
    try:
        url = (
            f"https://api.settrade.com/api/market/SET"
            f"/quote-symbol/{symbol}/info"
        )
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6
        )
        price = r.json().get("last")
        if price:
            return float(price)
    except Exception:
        pass

    # Fallback: Yahoo Finance (.BK = Bangkok Stock Exchange)
    try:
        url = (
            f"https://query1.finance.yahoo.com"
            f"/v8/finance/chart/{symbol}.BK"
        )
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        price = (r.json()["chart"]["result"][0]
                          ["meta"]["regularMarketPrice"])
        return float(price)
    except Exception as e:
        print(f"[WARN] Price {symbol}: {e}")
        return None


def fetch_all_prices() -> dict:
    """ดึงราคาหุ้นทุกตัวใน WATCHLIST พร้อมกัน"""
    prices = {}
    for sym in WATCHLIST:
        p = fetch_stock_price(sym)
        if p:
            prices[sym] = p
    return prices


def prices_to_text(prices: dict) -> str:
    """แปลง dict ราคาเป็น text ส่งให้ Claude"""
    if not prices:
        return "ไม่มีข้อมูลราคา"
    return "\n".join(f"{sym}: {price:.2f} บาท"
                     for sym, price in prices.items())


# ──────────────────────────────────────────────
#  ดึงข่าว RSS
# ──────────────────────────────────────────────
def fetch_rss_news(max_items: int = 12) -> list[dict]:
    items = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:4]:
                item_id = hashlib.md5(
                    entry.get("link",
                    entry.get("title", "")).encode()
                ).hexdigest()[:12]
                items.append({
                    "id":      item_id,
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", "")[:300],
                    "source":  source,
                })
        except Exception as e:
            print(f"[WARN] RSS {url}: {e}")
    return items[:max_items]


def news_to_text(items: list[dict]) -> str:
    return "\n".join(
        f"[{i['source']}] {i['title']} — {i['summary']}"
        for i in items
    ) or "ไม่มีข่าวใหม่"


def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-200:], f)


# ──────────────────────────────────────────────
#  หุ้นแนะนำประจำวัน (ใช้ราคาจริง)
# ──────────────────────────────────────────────
def get_daily_picks(news_text: str, prices: dict) -> str:
    style_label = (
        "Swing Trade (2-7 วัน)"
        if TRADE_STYLE == "swing"
        else "Day Trade (จบในวัน)"
    )
    price_context = prices_to_text(prices)

    raw = ask_claude(
        system_prompt=(
            "คุณเป็นนักวิเคราะห์หุ้นไทยมืออาชีพ "
            "ตอบเป็น JSON array เท่านั้น ห้ามมี markdown หรือข้อความอื่น "
            "ต้องใช้ราคาจริงที่ให้มาเท่านั้น ห้ามใช้ราคาที่ไม่มีในข้อมูล"
        ),
        user_msg=(
            f"ราคาหุ้นจริงตอนนี้:\n{price_context}\n\n"
            f"ข่าววันนี้:\n{news_text}\n\n"
            f"สไตล์: {style_label}\n"
            f"งบต่อ trade: {BUDGET_PER_TRADE:,} บาท\n\n"
            "แนะนำหุ้น 5 ตัว เลือกจากรายการที่มีราคาให้เท่านั้น "
            "คำนวณ entry/TP/SL จากราคาจริงด้านบน "
            "ตอบเป็น JSON array:\n"
            '[{"symbol":"","name":"","sector":"",'
            '"entry_low":0,"entry_high":0,'
            '"tp1":0,"tp2":0,"sl":0,'
            '"hold_days":"","rr_ratio":"","catalyst":"","signal":"BUY"}]'
        ),
        max_tokens=1000,
    )

    # Parse JSON
    try:
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        picks = json.loads(clean)
    except Exception:
        return f"หุ้นแนะนำวันนี้\n\n{raw}"

    signal_icon = {"STRONG_BUY": "🔥", "BUY": "✅", "WATCH": "👀"}
    lines = [
        f"🎯 <b>หุ้นแนะนำวันนี้ ({style_label})</b>",
        f"<i>งบต่อ trade: {BUDGET_PER_TRADE:,} บาท</i>\n",
    ]

    for i, p in enumerate(picks, 1):
        sym    = p.get("symbol", "?")
        name   = p.get("name", "")
        sector = p.get("sector", "")
        e_low  = float(p.get("entry_low", 0))
        e_high = float(p.get("entry_high", 0))
        tp1    = float(p.get("tp1", 0))
        tp2    = float(p.get("tp2", 0))
        sl     = float(p.get("sl", 0))
        hold   = p.get("hold_days", "-")
        rr     = p.get("rr_ratio", "-")
        cat    = p.get("catalyst", "")
        sig    = p.get("signal", "BUY")
        icon   = signal_icon.get(sig, "✅")

        mid    = (e_low + e_high) / 2 if e_low and e_high else max(e_low, 0.01)
        lot    = int(BUDGET_PER_TRADE / (mid * 100))
        tp1pct = (tp1 - mid) / mid * 100 if mid else 0
        slpct  = (sl  - mid) / mid * 100 if mid else 0

        # ราคาปัจจุบันจริง
        cur_price = prices.get(sym)
        cur_str   = f"ราคาตอนนี้: {cur_price:.2f} บาท" if cur_price else ""

        lines.append(
            f"{icon} <b>#{i} {sym}</b> — {name} | {sector}\n"
            f"   {cur_str}\n"
            f"   ซื้อ: <b>{e_low:.2f}–{e_high:.2f} บาท</b>  |  ถือ: {hold}\n"
            f"   TP1: <b>{tp1:.2f} บาท</b> ({tp1pct:+.1f}%)  "
            f"TP2: <b>{tp2:.2f} บาท</b>\n"
            f"   SL:  <b>{sl:.2f} บาท</b> ({slpct:+.1f}%)  RR: {rr}\n"
            f"   แนะนำ ~{lot} lot ({lot*100:,} หุ้น)\n"
            f"   📌 {cat}"
        )

    lines.append(
        "\n<i>ข้อมูลเพื่อประกอบการตัดสินใจเท่านั้น "
        "ไม่ใช่คำแนะนำการลงทุน</i>"
    )
    return "\n\n".join(lines)


# ──────────────────────────────────────────────
#  MODE 1: รายงานเช้า (08:45)
# ──────────────────────────────────────────────
def morning_report():
    print(f"[{now_th():%H:%M}] รายงานเช้า...")

    # ดึงข้อมูลพร้อมกัน
    items     = fetch_rss_news(max_items=10)
    news_text = news_to_text(items)
    prices    = fetch_all_prices()
    today     = now_th().strftime("%d %B %Y")

    # ข้อความที่ 1 — ภาพรวมตลาด
    analysis = ask_claude(
        system_prompt=(
            "คุณเป็นนักวิเคราะห์หุ้นไทยมืออาชีพ "
            "สรุปกระชับ อ่านง่าย ภาษาไทย ไม่เกิน 200 คำ "
            "ห้ามใช้ ** ## _ ` หรือ Markdown ใดๆ ทั้งสิ้น "
            "ใช้เฉพาะข้อความธรรมดาและตัวเลขเท่านั้น"
        ),
        user_msg=(
            f"วันที่ {today}\n\n"
            f"ราคาหุ้นตอนนี้:\n{prices_to_text(prices)}\n\n"
            f"ข่าว:\n{news_text}\n\n"
            "สรุป 3 หัวข้อ:\n"
            "1. ภาพรวมตลาดวันนี้คาดว่าเป็นอย่างไร\n"
            "2. ข่าวสำคัญที่น่าจับตาวันนี้\n"
            "3. กลุ่มหุ้นที่น่าสนใจ"
        ),
    )

    send_telegram(
        f"🌅 <b>รายงานเช้า — {now_th():%d/%m/%Y %H:%M}</b>\n"
        f"{'─'*28}\n\n"
        f"{analysis}\n\n"
        f"{'─'*28}\n"
        f"<i>Claude AI</i>"
    )

    # ข้อความที่ 2 — หุ้นแนะนำ (ราคาจริง)
    time.sleep(3)
    send_telegram(get_daily_picks(news_text, prices))


# ──────────────────────────────────────────────
#  MODE 2: รายงานเย็น (16:30)
# ──────────────────────────────────────────────
def evening_report():
    print(f"[{now_th():%H:%M}] รายงานเย็น...")

    prices    = fetch_all_prices()
    news_text = news_to_text(fetch_rss_news(max_items=8))

    analysis = ask_claude(
        system_prompt=(
            "คุณเป็นนักวิเคราะห์หุ้นไทย "
            "สรุปตลาดปิดและมุมมองพรุ่งนี้ ภาษาไทย ไม่เกิน 200 คำ "
            "ห้ามใช้ ** ## _ ` หรือ Markdown ใดๆ ทั้งสิ้น "
            "ใช้เฉพาะข้อความธรรมดาและตัวเลขเท่านั้น"
        ),
        user_msg=(
            f"ราคาหุ้นปิดวันนี้:\n{prices_to_text(prices)}\n\n"
            f"ข่าวช่วงบ่าย:\n{news_text}\n\n"
            "สรุป 3 หัวข้อ:\n"
            "1. ภาพรวมตลาดวันนี้เป็นอย่างไร\n"
            "2. ปัจจัยที่ต้องติดตามพรุ่งนี้\n"
            "3. ข่าวต่างประเทศคืนนี้ที่อาจกระทบ"
        ),
    )

    # สรุปราคาปิด
    price_lines = "\n".join(
        f"{sym}: {price:.2f} บาท"
        for sym, price in prices.items()
    ) or "ไม่มีข้อมูล"

    send_telegram(
        f"🌆 <b>รายงานเย็น — {now_th():%d/%m/%Y %H:%M}</b>\n"
        f"{'─'*28}\n\n"
        f"<b>ราคาปิดวันนี้</b>\n{price_lines}\n\n"
        f"{'─'*28}\n\n"
        f"{analysis}\n\n"
        f"{'─'*28}\n"
        f"<i>Claude AI | ไม่ใช่คำแนะนำการลงทุน</i>"
    )


# ──────────────────────────────────────────────
#  MODE 3: Real-time monitor (รันทุก 5 นาที)
# ──────────────────────────────────────────────
def is_urgent(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in URGENT_KEYWORDS)


def realtime_monitor():
    print(f"[{now_th():%H:%M:%S}] Monitor...")

    seen       = load_seen()
    items      = fetch_rss_news(max_items=15)
    new_items  = [it for it in items if it["id"] not in seen]
    urgent     = [it for it in new_items if is_urgent(it["title"], it["summary"])]

    print(f"  ข่าวทั้งหมด {len(items)} | ใหม่ {len(new_items)} | ด่วน {len(urgent)}")

    if not new_items:
        return

    seen.update(it["id"] for it in new_items)
    save_seen(seen)

    if urgent:
        # ดึงราคาปัจจุบันประกอบการวิเคราะห์
        prices    = fetch_all_prices()
        news_text = news_to_text(urgent)

        analysis = ask_claude(
            system_prompt=(
                "คุณเป็นโค้ชหุ้นส่วนตัว วิเคราะห์ข่าวใหม่กระชับตรงประเด็น "
                "บอกผลกระทบและสิ่งที่ควรทำ ภาษาไทย ไม่เกิน 150 คำ "
                "ห้ามใช้ ** ## _ ` หรือ Markdown ใดๆ ทั้งสิ้น"
            ),
            user_msg=(
                f"ข่าวด่วน ({now_th():%H:%M}):\n{news_text}\n\n"
                f"ราคาหุ้นตอนนี้:\n{prices_to_text(prices)}\n\n"
                "วิเคราะห์:\n"
                "1. ข่าวนี้กระทบตลาดอย่างไร (บวก/ลบ/กลาง)\n"
                "2. หุ้นกลุ่มไหนได้ประโยชน์หรือเสียประโยชน์\n"
                "3. ควรทำอะไร: ถือ / เพิ่ม / ลด / รอดู\n"
                "4. ระดับความเร่งด่วน: ต้องตัดสินใจตอนนี้ หรือรอได้"
            ),
            max_tokens=400,
        )

        titles = "\n".join(f"• {it['title']}" for it in urgent)
        send_telegram(
            f"🚨 <b>ข่าวด่วน — {now_th():%H:%M}</b>\n"
            f"{'─'*28}\n\n"
            f"<b>ข่าวที่พบ:</b>\n{titles}\n\n"
            f"{'─'*28}\n\n"
            f"<b>วิเคราะห์:</b>\n{analysis}\n\n"
            f"{'─'*28}\n"
            f"<i>Claude AI Coach | ไม่ใช่คำแนะนำการลงทุน</i>"
        )

    elif len(new_items) >= 3:
        # ข่าวใหม่ทั่วไป — ส่งเงียบๆ ไม่มีเสียง
        summary = ask_claude(
            system_prompt=(
                "สรุปข่าวหุ้นไทยสั้นๆ ภาษาไทย ไม่เกิน 80 คำ "
                "ห้ามใช้ ** ## _ ` หรือ Markdown ใดๆ ทั้งสิ้น"
            ),
            user_msg=f"ข่าวใหม่:\n{news_to_text(new_items[:5])}",
            max_tokens=200,
        )
        titles = "\n".join(f"• {it['title']}" for it in new_items[:5])
        send_telegram(
            f"📰 <b>ข่าวใหม่ — {now_th():%H:%M}</b>\n\n"
            f"{titles}\n\n"
            f"<b>สรุป:</b> {summary}",
            silent=True,
        )


# ──────────────────────────────────────────────
#  Entry Point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"

    if mode == "morning":
        morning_report()
    elif mode == "evening":
        evening_report()
    elif mode == "monitor":
        realtime_monitor()
    else:
        print("Usage: python bot.py [morning|evening|monitor]")
        sys.exit(1)
