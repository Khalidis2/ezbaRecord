# file: telegram_bot.py
import re
import sqlite3
import os
from datetime import datetime, timedelta
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler

TOKEN = os.environ.get("BOT_TOKEN")
DB_NAME = "azba_expenses.db"

ALLOWED_USERS = [
    47329648,  # انت
    222222222,  # ولدك
    333333333,  # عامل 1
    444444444   # عامل 2
]

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            amount REAL,
            date TEXT,
            note TEXT,
            raw_text TEXT
        )
    """)
    conn.commit()
    conn.close()

def detect_date(text):
    today = datetime.now().date()
    if "أمس" in text or "امس" in text:
        return (today - timedelta(days=1)).isoformat()
    return today.isoformat()

def detect_type(text):
    if any(k in text for k in ["علف", "شعير", "برسيم", "تبن"]):
        return "علف"
    if any(k in text for k in ["عامل", "راتب", "عمال"]):
        return "عمال"
    if any(k in text for k in ["دواء", "علاج", "بيطري"]):
        return "علاج"
    if any(k in text for k in ["كهرب", "مولد"]):
        return "كهرباء"
    if any(k in text for k in ["مويه", "ماء"]):
        return "ماء"
    return "اخرى"

def parse_expense(text):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    return {
        "type": detect_type(text),
        "amount": float(m.group(1).replace(",", ".")),
        "date": detect_date(text),
        "note": text,
        "raw_text": text
    }

def save_expense(exp):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO expenses (type, amount, date, note, raw_text) VALUES (?, ?, ?, ?, ?)",
        (exp["type"], exp["amount"], exp["date"], exp["note"], exp["raw_text"])
    )
    conn.commit()
    conn.close()

def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS

def help_command(update, context):
    if not authorized(update): 
        update.message.reply_text("❌ غير مصرح لك")
        return
    update.message.reply_text(
        "📋 أوامر البوت:\n\n"
        "✍️ تسجيل مصروف:\n"
        "اكتب مثل:\n"
        "اشتريت علف 350 اليوم\n\n"
        "📊 التقارير:\n"
        "/today - مجموع اليوم\n"
        "/week - تقرير الأسبوع\n"
        "/help - المساعدة"
    )

def today_report(update, context):
    if not authorized(update): 
        update.message.reply_text("❌ غير مصرح لك")
        return
    today = datetime.now().date().isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT SUM(amount) FROM expenses WHERE date = ?", (today,))
    total = cur.fetchone()[0] or 0
    conn.close()
    update.message.reply_text(f"📅 مجموع اليوم: {total}")

def week_report(update, context):
    if not authorized(update): 
        update.message.reply_text("❌ غير مصرح لك")
        return
    today = datetime.now().date()
    start = (today - timedelta(days=6)).isoformat()
    end = today.isoformat()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT type, SUM(amount) FROM expenses "
        "WHERE date BETWEEN ? AND ? GROUP BY type",
        (start, end)
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        update.message.reply_text("لا يوجد مصاريف هذا الأسبوع")
        return
    msg = "📊 تقرير الأسبوع:\n"
    for t, s in rows:
        msg += f"- {t}: {s}\n"
    update.message.reply_text(msg)

def handle_message(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    exp = parse_expense(update.message.text)
    if not exp:
        update.message.reply_text("❌ اكتب المبلغ بشكل واضح")
        return
    save_expense(exp)
    update.message.reply_text(
        f"✅ تم التسجيل\nالنوع: {exp['type']}\nالمبلغ: {exp['amount']}\nالتاريخ: {exp['date']}"
    )

def main():
    init_db()

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("today", today_report))
    dp.add_handler(CommandHandler("week", week_report))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    port = int(os.environ.get("PORT", "8443"))
    base_url = os.environ.get("BASE_URL")
    if not base_url:
        raise RuntimeError("BASE_URL is not set")

    webhook_url = f"{base_url}/{TOKEN}"

    updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TOKEN,
    )
    updater.bot.set_webhook(webhook_url)

    updater.idle()

if __name__ == "__main__":
    main()
