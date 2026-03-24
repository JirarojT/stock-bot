# Thai Stock AI Bot 🤖📈

ส่งรายงานหุ้นไทยอัตโนมัติผ่าน Telegram ด้วย Claude AI  
ไม่ต้องเปิดคอม — รันบน GitHub Actions ฟรี

---

## สิ่งที่ Bot ทำ

| เวลา | สิ่งที่ส่ง |
|------|-----------|
| **08:45** ทุกวันทำการ | ภาพรวมตลาด + พอร์ต + 🎯 หุ้นแนะนำ 3 ตัว (ราคาเข้า/TP/SL) |
| **ทุก 5 นาที** 09:00–16:35 | สแกนข่าวใหม่ — ถ้าเจอข่าวด่วนส่ง 🚨 alert + วิเคราะห์ทันที |
| **16:30** ทุกวันทำการ | สรุปตลาด + P&L พอร์ต + มุมมองพรุ่งนี้ |

---

## วิธีติดตั้ง (5 ขั้นตอน)

### 1. Fork / Upload โปรเจกต์นี้ขึ้น GitHub

ไปที่ github.com → New repository → ชื่อ `stock-bot`  
แล้ว upload ไฟล์ทั้งหมดในโฟลเดอร์นี้ขึ้นไป

### 2. สร้าง Telegram Bot

1. เปิด Telegram → ค้นหา **@BotFather**
2. พิมพ์ `/newbot` → ตั้งชื่อ → ได้ **Bot Token**
3. สร้างกลุ่ม Telegram → เพิ่ม Bot เข้ากลุ่ม
4. เปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` → หา **chat_id**

### 3. ขอ Claude API Key

1. ไปที่ **console.anthropic.com**
2. สมัครบัญชี → API Keys → Create Key
3. คัดลอก key ไว้

### 4. ตั้งค่า GitHub Secrets

ใน GitHub repository → Settings → Secrets and variables → Actions → New secret

| Secret Name | ค่าที่ใส่ |
|-------------|---------|
| `ANTHROPIC_API_KEY` | Claude API Key (sk-ant-...) |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | Chat ID (เช่น -100123456789) |

### 5. เปิดใช้งาน Actions

ไปที่ tab **Actions** ใน repository → กด Enable  
จากนั้น Bot จะทำงานอัตโนมัติตามเวลาเลยครับ

---

## ปรับแต่ง

แก้ใน `bot.py` ส่วน SETTINGS:

```python
MY_PORTFOLIO = {
    "GULF": {"shares": 200, "avg_cost": 62.42},
    "PTT":  {"shares": 500, "avg_cost": 37.35},
    # เพิ่มหุ้นได้เลย
}

TRADE_STYLE      = "swing"   # "swing" หรือ "day"
BUDGET_PER_TRADE = 10000     # บาทต่อ trade

URGENT_KEYWORDS = [
    "GULF", "PTT", "Fed", "น้ำมัน",
    # เพิ่ม keyword ที่อยากให้ alert ได้เลย
]
```

---

## ค่าใช้จ่าย (โดยประมาณ/เดือน)

| บริการ | ค่าใช้จ่าย |
|--------|-----------|
| GitHub Actions | **ฟรี** (2,000 นาที/เดือน ใช้ไม่ถึง) |
| Claude API | ~**100–300 บาท** (ขึ้นกับจำนวน token) |
| Telegram Bot | **ฟรี** |

---

*ข้อมูลเพื่อประกอบการตัดสินใจเท่านั้น ไม่ใช่คำแนะนำการลงทุน*
