"""
Thai Stock AI Bot — Full Version
=================================
รันได้ 3 โหมด:
  python bot.py morning   → รายงานเช้า + หุ้นแนะนำ
  python bot.py evening   → รายงานเย็น + สรุป P&L
  python bot.py monitor   → สแกนข่าวใหม่ real-time (GitHub Actions รัน ทุก 5 นาที)

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
from bs4 import BeautifulSoup
import anthropic

# ============================================================
#  SETTINGS
# ============================================================
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "sk-ant-xxxxxxxx")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "xxxxxxxx:xxxxxxxx")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "-100xxxxxxxxxx")

MY_PORTFOLIO = {
    "GULF": {"shares": 200, "avg_cost": 62.42},
    "PTT":  {"shares": 500, "avg_cost": 37.35},
}

TRADE_STYLE       = "swing"   # "swing" หรือ "day"
BUDGET_PER_TRADE  = 10000     # บาทต่อ trade

# แหล่งข่าว RSS (ดึงข่าวหุ้นไทย)
RSS_FEEDS = [
    "https://www.set.or.th/th/news/rss/news.xml",
    "https://feeds.manager.co.th/manager-stock.xml",
    "https://rss.thansettakij.com/feed/stock",
    "https://www.bangkokbiznews.com/rss/data/finance.xml",
]

# คีย์เวิร์ดที่ถือว่าเป็นข่าวสำคัญ (จะ alert ทันที)
URGENT_KEYWORDS = [
    "ฉุกเฉิน", "หยุดพักการซื้อขาย", "halt", "circuit breaker",
    "Fed", "เฟด", "ดอกเบี้ย", "น้ำมัน", "สงคราม", "ตะวันออกกลาง",
    "GULF", "PTT", "ปตท", "กัลฟ์",
    "เลือกตั้ง", "นายกรัฐมนตรี", "รัฐบาล",
]

# ไฟล์เก็บ ID ข่าวที่เคยเห็นแล้ว (ป้องกันส่งซ้ำ)
SEEN_FILE = "seen_news.json"
# ============================================================

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
TH_TZ  = timezone(timedelta(hours=7))


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────
def now_th() -> datetime:
    return datetime.now(TH_TZ)


def send_telegram(message: str, silent: bool = False):
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
        print(f"[{now_th():%H:%M:%S}] ✓ Telegram sent ({len(message)} chars)")
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")


def ask_claude(system_prompt: str, user_msg: str, max_tokens: int = 800) -> str:
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


def fetch_rss_news(max_items: int = 12) -> list[dict]:
    """คืน list of {id, title, summary, source}"""
    items = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
            for entry in feed.entries[:4]:
                item_id = hashlib.md5(
                    entry.get("link", entry.get("title", "")).encode()
                ).hexdigest()[:12]
                items.append({
                    "id":      item_id,
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", "")[:300],
                    "source":  source,
                    "link":    entry.get("link", ""),
                })
        except Exception as e:
            print(f"[WARN] RSS {url}: {e}")
    return items[:max_items]


def news_to_text(items: list[dict]) -> str:
    return "\n".join(
        f"[{i['source']}] {i['title']} — {i['summary']}"
        for i in items
    )


def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set):
    # เก็บแค่ 200 IDs ล่าสุดเพื่อไม่ให้ไฟล์โป่ง
    recent = list(seen)[-200:]
    with open(SEEN_FILE, "w") as f:
        json.dump(recent, f)


def fetch_stock_price(symbol: str) -> float | None:
    try:
        url = f"https://www.settrade.com/th/equities/quote/{symbol}/overview"
        r   = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        el = soup.find("div", class_="last-price") or soup.find("span", class_="price")
        return float(el.text.strip().replace(",", "")) if el else None
    except Exception:
        return None


def build_portfolio_summary(prices: dict) -> str:
    lines = ["<b>📊 พอร์ตของคุณ</b>"]
    total_cost = total_value = 0.0
    for sym, info in MY_PORTFOLIO.items():
        cost  = info["avg_cost"] * info["shares"]
        total_cost += cost
        price = prices.get(sym)
        if price:
            value = price * info["shares"]
            total_value += value
            pnl  = value - cost
            pct  = pnl / cost * 100
            icon = "🔴" if pnl < 0 else "🟢"
            lines.append(f"{icon} {sym}: {price:.2f}฿  ({pnl:+.0f}฿ / {pct:+.1f}%)")
        else:
            lines.append(f"⚪ {sym}: ไม่มีราคา")
    if total_value:
        total_pnl = total_value - total_cost
        total_pct = total_pnl / total_cost * 100
        lines.append(f"\n<b>รวม: {total_pnl:+.0f}฿  ({total_pct:+.1f}%)</b>")
    return "\n".join(lines)


# ──────────────────────────────────────────────
#  Daily Picks
# ──────────────────────────────────────────────
def get_daily_picks(news_text: str) -> str:
    style_label = "Swing Trade (2–7 วัน)" if TRADE_STYLE == "swing" else "Day Trade (จบในวัน)"
    raw = ask_claude(
        system_prompt=(
            "คุณเป็นนักวิเคราะห์หุ้นไทยมืออาชีพ ตอบเป็น JSON array เท่านั้น "
            "ห้ามมี markdown หรือข้อความอื่น"
        ),
        user_msg=(
            f"วันที่: {now_th():%d/%m/%Y}\n"
            f"สไตล์: {style_label}\n"
            f"งบต่อ trade: {BUDGET_PER_TRADE:,} บาท\n\n"
            f"ข่าววันนี้:\n{news_text}\n\n"
            "แนะนำหุ้น SET 3 ตัว คืนเป็น JSON array:\n"
            '[{"symbol":"","name":"","sector":"","entry_low":0,"entry_high":0,'
            '"tp1":0,"tp2":0,"sl":0,"hold_days":"","rr_ratio":"","catalyst":"","signal":"BUY"}]'
        ),
        max_tokens=1000,
    )
    try:
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        picks = json.loads(clean)
    except Exception:
        return f"<b>🎯 หุ้นแนะนำวันนี้</b>\n\n{raw}"

    signal_icon = {"STRONG_BUY": "🔥", "BUY": "✅", "WATCH": "👀"}
    lines = [f"<b>🎯 หุ้นแนะนำวันนี้ ({style_label})</b>",
             f"<i>งบต่อ trade: {BUDGET_PER_TRADE:,} บาท</i>\n"]

    for i, p in enumerate(picks, 1):
        sym    = p.get("symbol","?")
        name   = p.get("name","")
        sector = p.get("sector","")
        e_low  = float(p.get("entry_low", 0))
        e_high = float(p.get("entry_high", 0))
        tp1    = float(p.get("tp1", 0))
        tp2    = float(p.get("tp2", 0))
        sl     = float(p.get("sl", 0))
        hold   = p.get("hold_days","–")
        rr     = p.get("rr_ratio","–")
        cat    = p.get("catalyst","")
        sig    = p.get("signal","BUY")
        icon   = signal_icon.get(sig, "✅")

        mid    = (e_low + e_high) / 2 if e_low and e_high else e_low or 1
        lot    = int(BUDGET_PER_TRADE / (mid * 100))
        tp1pct = (tp1 - mid) / mid * 100
        slpct  = (sl  - mid) / mid * 100

        lines.append(
            f"{icon} <b>#{i} {sym}</b> — {name} | {sector}\n"
            f"   ซื้อ: <b>{e_low:.2f}–{e_high:.2f} ฿</b>  |  ถือ: {hold}\n"
            f"   TP1: <b>{tp1:.2f} ฿</b> ({tp1pct:+.1f}%)  "
            f"TP2: <b>{tp2:.2f} ฿</b>\n"
            f"   SL:  <b>{sl:.2f} ฿</b> ({slpct:+.1f}%)  RR: {rr}\n"
            f"   แนะนำ ~{lot} lot ({lot*100:,} หุ้น)\n"
            f"   📌 {cat}"
        )

    lines.append("\n<i>⚠️ เพื่อประกอบการตัดสินใจเท่านั้น ไม่ใช่คำแนะนำการลงทุน</i>")
    return "\n\n".join(lines)


# ──────────────────────────────────────────────
#  MODE 1: รายงานเช้า
# ──────────────────────────────────────────────
def morning_report():
    print(f"[{now_th():%H:%M}] === รายงานเช้า ===")
    items     = fetch_rss_news(max_items=10)
    news_text = news_to_text(items)
    today     = now_th().strftime("%A %d %B %Y")

    analysis = ask_claude(
        system_prompt=(
            "คุณเป็นนักวิเคราะห์หุ้นไทยมืออาชีพ สรุปกระชับ อ่านง่าย "
            "ใช้ภาษาไทย ไม่เกิน 250 คำ"
        ),
        user_msg=(
            f"วันนี้ {today}\n\nข่าว:\n{news_text}\n\n"
            f"พอร์ต: GULF (ต้นทุน 62.42฿), PTT (ต้นทุน 37.35฿)\n\n"
            "สรุป: 1) ภาพรวมตลาดวันนี้  2) ข่าวกระทบพอร์ต  "
            "3) แนวรับ/แนวต้าน GULF และ PTT  4) คำแนะนำ"
        ),
    )

    send_telegram(
        f"🌅 <b>รายงานเช้า — {now_th():%d/%m/%Y %H:%M}</b>\n"
        f"{'─'*28}\n\n{analysis}\n\n"
        f"{'─'*28}\n<i>Claude AI</i>"
    )

    time.sleep(3)
    send_telegram(get_daily_picks(news_text))


# ──────────────────────────────────────────────
#  MODE 2: รายงานเย็น
# ──────────────────────────────────────────────
def evening_report():
    print(f"[{now_th():%H:%M}] === รายงานเย็น ===")
    prices = {sym: fetch_stock_price(sym) for sym in MY_PORTFOLIO}
    prices = {k: v for k, v in prices.items() if v}

    portfolio_text = build_portfolio_summary(prices)
    news_text      = news_to_text(fetch_rss_news(max_items=8))

    analysis = ask_claude(
        system_prompt="คุณเป็นนักวิเคราะห์หุ้นไทย สรุปตลาดปิดและมุมมองพรุ่งนี้ ภาษาไทย ไม่เกิน 220 คำ",
        user_msg=(
            f"ตลาดปิดแล้ว\nราคาปิด: {prices}\n\n"
            f"ข่าวช่วงบ่าย:\n{news_text}\n\n"
            "สรุป: 1) ภาพรวมตลาดวันนี้  2) ปัจจัยติดตามพรุ่งนี้  "
            "3) ข่าวต่างประเทศคืนนี้"
        ),
    )

    send_telegram(
        f"🌆 <b>รายงานเย็น — {now_th():%d/%m/%Y %H:%M}</b>\n"
        f"{'─'*28}\n\n"
        f"{portfolio_text}\n\n"
        f"{'─'*28}\n\n"
        f"{analysis}\n\n"
        f"{'─'*28}\n<i>Claude AI | ไม่ใช่คำแนะนำการลงทุน</i>"
    )


# ──────────────────────────────────────────────
#  MODE 3: Real-time monitor (รันทุก 5 นาที)
# ──────────────────────────────────────────────
def is_urgent(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in URGENT_KEYWORDS)


def realtime_monitor():
    """
    GitHub Actions รันสคริปต์นี้ทุก 5 นาทีช่วงตลาดเปิด
    ตรวจสอบข่าวใหม่ → ถ้ามีข่าวสำคัญหรือข่าวที่กระทบพอร์ต
    → ให้ Claude วิเคราะห์แล้วส่ง Telegram ทันที
    """
    print(f"[{now_th():%H:%M:%S}] === Realtime Monitor ===")

    seen  = load_seen()
    items = fetch_rss_news(max_items=15)

    new_items   = [it for it in items if it["id"] not in seen]
    urgent_news = [it for it in new_items if is_urgent(it["title"], it["summary"])]

    print(f"  ข่าวทั้งหมด: {len(items)} | ใหม่: {len(new_items)} | สำคัญ: {len(urgent_news)}")

    if not new_items:
        print("  ไม่มีข่าวใหม่")
        return

    # อัปเดต seen
    seen.update(it["id"] for it in new_items)
    save_seen(seen)

    # ถ้ามีข่าวสำคัญ → วิเคราะห์ทันที
    if urgent_news:
        news_text = news_to_text(urgent_news)
        prices    = {sym: fetch_stock_price(sym) for sym in MY_PORTFOLIO}
        prices    = {k: v for k, v in prices.items() if v}

        # สร้าง context พอร์ตปัจจุบัน
        port_ctx = ", ".join(
            f"{sym} (ต้นทุน {info['avg_cost']}฿, ราคาปัจจุบัน {prices.get(sym,'?')}฿)"
            for sym, info in MY_PORTFOLIO.items()
        )

        analysis = ask_claude(
            system_prompt=(
                "คุณเป็นโค้ชหุ้นส่วนตัว วิเคราะห์ข่าวใหม่กระชับ ตรงประเด็น "
                "บอกผลกระทบต่อพอร์ตและสิ่งที่ควรทำ ใช้ภาษาไทย ไม่เกิน 200 คำ"
            ),
            user_msg=(
                f"ข่าวด่วนที่เพิ่งออก ({now_th():%H:%M}):\n{news_text}\n\n"
                f"พอร์ตปัจจุบัน: {port_ctx}\n\n"
                "วิเคราะห์:\n"
                "1. ข่าวนี้กระทบหุ้นในพอร์ตอย่างไร (บวก/ลบ/กลาง)\n"
                "2. ควรทำอะไร: ถือต่อ / เพิ่ม / ลด / รอดู\n"
                "3. ระดับความเร่งด่วน: ต้องตัดสินใจตอนนี้ หรือรอได้"
            ),
            max_tokens=500,
        )

        urgent_titles = "\n".join(f"• {it['title']}" for it in urgent_news)
        send_telegram(
            f"🚨 <b>ข่าวด่วน — {now_th():%H:%M}</b>\n"
            f"{'─'*28}\n\n"
            f"<b>ข่าวที่พบ:</b>\n{urgent_titles}\n\n"
            f"{'─'*28}\n\n"
            f"<b>วิเคราะห์:</b>\n{analysis}\n\n"
            f"{'─'*28}\n"
            f"<i>Claude AI Coach | ไม่ใช่คำแนะนำการลงทุน</i>"
        )

    # ถ้ามีข่าวใหม่ทั่วไป (ไม่ urgent) → ส่งสรุปเงียบๆ ไม่มีการแจ้งเตือน
    elif len(new_items) >= 3:
        headlines = "\n".join(f"• {it['title']}" for it in new_items[:5])
        summary = ask_claude(
            system_prompt="สรุปข่าวหุ้นไทยสั้นๆ ภาษาไทย ไม่เกิน 100 คำ",
            user_msg=f"ข่าวใหม่:\n{news_to_text(new_items[:5])}",
            max_tokens=300,
        )
        send_telegram(
            f"📰 <b>ข่าวใหม่ — {now_th():%H:%M}</b>\n\n"
            f"{headlines}\n\n"
            f"<b>สรุป:</b> {summary}",
            silent=True,   # ไม่มีเสียง ping
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
        print(f"Usage: python bot.py [morning|evening|monitor]")
        sys.exit(1)
