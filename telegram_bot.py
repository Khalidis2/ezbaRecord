# file: telegram_bot.py
import re
import os
import json
from datetime import datetime, timedelta

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
    444444444
]

def get_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
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
        "date": detect_date(text),
        "type": detect_type(text),
        "amount": float(m.group(1).replace(",", ".")),
        "note": text,
        "raw_text": text
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
        "اكتب مثل:\n"
        "اشتريت علف 350 اليوم\n\n"
        "/help - المساعدة"
    )

def handle_message(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    exp = parse_expense(update.message.text)
    if not exp:
        update.message.reply_text("❌ اكتب مبلغ واضح")
        return

    sheet = get_sheet()
    sheet.append_row([
        exp["date"],
        exp["type"],
        exp["amount"],
        exp["note"],
        exp["raw_text"],
        str(update.message.from_user.id)
    ], value_input_option="USER_ENTERED")

    update.message.reply_text("✅ تم التسجيل في Google Sheets")

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
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
