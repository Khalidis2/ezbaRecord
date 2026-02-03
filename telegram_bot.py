# file: telegram_bot.py
import re
import sqlite3
import os
from datetime import datetime, timedelta

from telegram.ext import Updater, MessageHandler, Filters, CommandHandler

import json
import gspread
from google.oauth2.service_account import Credentials

TOKEN = os.environ.get("BOT_TOKEN")
DB_NAME = "azba_expenses.db"  # لن نستخدمه فعليًا الآن، بس نخليه لو حبيت ترجّع SQLite

ALLOWED_USERS = [
    47329648,   # انت
    222222222,  # ولدك
    333333333,  # عامل 1
    444444444   # عامل 2
]

SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

def get_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    # أول ورقة (Sheet1)
    return sh.sheet1

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

def save_expense_to_sheet(exp, user_id):
    sheet = get_sheet()
    # ترتيب الأعمدة: date | type | amount | note | raw_text | user_id
    row = [
        exp["date"],
        exp["type"],
        exp["amount"],
        exp["note"],
        exp["raw_text"],
        str(user_id),
    ]
    sheet.append_row(row, value_input_option="USER_ENTERED")

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
        "📊 التقارير (من Google Sheets):\n"
        "المجموع والتفاصيل شوفها مباشرة في Google Sheet.\n\n"
        "/help - المساعدة"
    )

<<<<<<< HEAD
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

=======
>>>>>>> 178b8a597837063ee5922365adb2d4d52be8ee9d
def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    text = update.message.text
    exp = parse_expense(text)
    if not exp:
        update.message.reply_text("❌ اكتب المبلغ بشكل واضح وفيه رقم، مثال: اشتريت علف 350 اليوم")
        return

    try:
        save_expense_to_sheet(exp, user_id)
        update.message.reply_text(
            f"✅ تم التسجيل في Google Sheet\n"
            f"النوع: {exp['type']}\n"
            f"المبلغ: {exp['amount']}\n"
            f"التاريخ: {exp['date']}"
        )
    except Exception as e:
        update.message.reply_text(f"❌ صار خطأ أثناء الحفظ في Google Sheets")
        print("ERROR saving to sheet:", e)

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    print("DEBUG: starting bot")
    print("DEBUG: BOT_TOKEN present?", bool(TOKEN))
    print("DEBUG: SHEET_ID =", SHEET_ID)
    print("DEBUG: GOOGLE_SERVICE_ACCOUNT_JSON present?", bool(GOOGLE_SERVICE_ACCOUNT_JSON))
    print("DEBUG: ALLOWED_USERS =", ALLOWED_USERS)

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

<<<<<<< HEAD
    # تشغيل عادي (polling) لو شغلت السكربت محليًا
    if os.environ.get("RUN_MODE") == "local":
        updater.start_polling()
        updater.idle()
        return

    # Webhook لو شغَّلته على Render Web Service
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
=======
    updater.start_polling()
    print("DEBUG: start_polling called")
>>>>>>> 178b8a597837063ee5922365adb2d4d52be8ee9d
    updater.idle()

if __name__ == "__main__":
    main()
