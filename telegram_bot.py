# file: telegram_bot.py
import re
import os
import json
from datetime import datetime, timedelta, date

import gspread
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler

TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

ALLOWED_USERS = [
    47329648,
    222222222,
    333333333,
    444444444,
]

USER_NAMES = {
    47329648: "Khalid",
    222222222: "الولد",
    333333333: "عامل 1",
    444444444: "عامل 2",
}

PROCESS_KEYWORDS = {
    "شراء": ["اشتريت", "شراء"],
    "بيع": ["بعت", "بيع"],
    "فاتورة": ["فاتورة", "كهرب", "ماء", "صيانة"],
    "راتب": ["راتب", "عامل", "عمال"],
}

USER_PROCESS_OVERRIDE = {}

def get_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).sheet1

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
    if any(k in text for k in ["كهرب", "مولد", "كهرباء"]):
        return "كهرباء"
    if any(k in text for k in ["مويه", "ماء", "ماي"]):
        return "ماء"
    return "اخرى"

def detect_process(text, user_id):
    if user_id in USER_PROCESS_OVERRIDE:
        return USER_PROCESS_OVERRIDE[user_id]
    for process, keywords in PROCESS_KEYWORDS.items():
        if any(k in text for k in keywords):
            return process
    return "أخرى"

def parse_expense(text, user_id):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    return {
        "date": detect_date(text),
        "process": detect_process(text, user_id),
        "type": detect_type(text),
        "amount": float(m.group(1).replace(",", ".")),
        "note": text,
    }

def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS

def help_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    update.message.reply_text(
        "📋 أوامر البوت:\n\n"
        "✍️ تسجيل مصروف:\n"
        "– اكتب جملة فيها رقم\n"
        "  مثال: اشتريت علف 200 اليوم\n\n"
        "⚙️ نوع العملية:\n"
        "– /process شراء\n"
        "– /process بيع\n"
        "– /process فاتورة\n"
        "– /process راتب\n\n"
        "📊 التقارير:\n"
        "– /week   مجموع مصاريف آخر 7 أيام\n"
        "– /month  مجموع مصاريف هذا الشهر\n"
        "– /status ملخص اليوم + الأسبوع + الشهر\n\n"
        "ℹ️ أخرى:\n"
        "– /help   عرض هذه القائمة"
    )

def process_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    if not context.args:
        update.message.reply_text("❌ استخدم: /process شراء | بيع | فاتورة | راتب")
        return
    proc = context.args[0]
    allowed = {"شراء", "بيع", "فاتورة", "راتب", "أخرى"}
    if proc not in allowed:
        update.message.reply_text("❌ نوع عملية غير معروف، استخدم: شراء / بيع / فاتورة / راتب")
        return
    USER_PROCESS_OVERRIDE[user_id] = proc
    update.message.reply_text(f"✅ تم تعيين نوع العملية لهذا المستخدم: {proc}")

def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    text = update.message.text
    exp = parse_expense(text, user_id)
    if not exp:
        update.message.reply_text("❌ اكتب مبلغ واضح، مثال: اشتريت علف 200 اليوم")
        return
    person_name = USER_NAMES.get(user_id, str(user_id))
    try:
        sheet = get_sheet()
        sheet.append_row(
            [
                exp["date"],
                exp["process"],
                exp["type"],
                exp["amount"],
                exp["note"],
                person_name,
            ],
            value_input_option="USER_ENTERED",
        )
        update.message.reply_text(
            f"✅ تم تسجيل المصروف\n"
            f"العملية: {exp['process']}\n"
            f"النوع: {exp['type']}\n"
            f"المبلغ: {exp['amount']}\n"
            f"التاريخ: {exp['date']}\n"
            f"الشخص: {person_name}"
        )
    except Exception as e:
        print("ERROR saving to sheet:", e)
        update.message.reply_text("❌ صار خطأ أثناء الحفظ في Google Sheets")

def load_expenses():
    sheet = get_sheet()
    rows = sheet.get_all_values()
    expenses = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        date_str = row[0].strip()
        amount_str = row[3].strip()
        if not date_str or not amount_str:
            continue
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            amount = float(str(amount_str).replace(",", ""))
        except Exception:
            continue
        expenses.append({"date": d, "amount": amount})
    return expenses

def week_report(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    today = datetime.now().date()
    start = today - timedelta(days=6)
    expenses = load_expenses()
    total = sum(e["amount"] for e in expenses if start <= e["date"] <= today)
    update.message.reply_text(
        f"📊 تقرير الأسبوع (من {start} إلى {today}):\n"
        f"إجمالي المصاريف: {total}"
    )

def month_report(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    today = datetime.now().date()
    start = date(today.year, today.month, 1)
    expenses = load_expenses()
    total = sum(e["amount"] for e in expenses if start <= e["date"] <= today)
    update.message.reply_text(
        f"📊 تقرير الشهر ({today.year}-{today.month:02d}):\n"
        f"إجمالي المصاريف: {total}"
    )

def status_report(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    today = datetime.now().date()
    week_start = today - timedelta(days=6)
    month_start = date(today.year, today.month, 1)
    expenses = load_expenses()
    total_today = sum(e["amount"] for e in expenses if e["date"] == today)
    total_week = sum(e["amount"] for e in expenses if week_start <= e["date"] <= today)
    total_month = sum(e["amount"] for e in expenses if month_start <= e["date"] <= today)
    update.message.reply_text(
        "📈 ملخص المصاريف:\n"
        f"اليوم ({today}): {total_today}\n"
        f"آخر 7 أيام: {total_week}\n"
        f"هذا الشهر: {total_month}\n\n"
        "هذه الأرقام للمصاريف فقط."
    )

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID is not set")
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("process", process_command))
    dp.add_handler(CommandHandler("week", week_report))
    dp.add_handler(CommandHandler("month", month_report))
    dp.add_handler(CommandHandler("status", status_report))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
