# file: telegram_bot.py
import os
import json
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from openai import OpenAI


# ================== ENV ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.environ.get("SHEET_ID")

if not all([BOT_TOKEN, OPENAI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID]):
    raise RuntimeError("Missing environment variables")


# ================== CLIENTS ==================
openai_client = OpenAI(api_key=OPENAI_API_KEY)


def get_sheet():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SERVICE_ACCOUNT_JSON),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds).open_by_key(SHEET_ID).sheet1


# ================== AUTH ==================
ALLOWED_USERS = {47329648}


def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS


# ================== AI ==================
def analyze_with_ai(text):
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

    prompt = f"""
You are a financial assistant.
Return ONLY valid JSON.

Schema:
{{
  "should_save": true | false,
  "date": "YYYY-MM-DD",
  "process": "شراء" | "بيع" | "فاتورة" | "راتب" | "أخرى",
  "type": "علف" | "عمال" | "علاج" | "كهرباء" | "ماء" | "اخرى",
  "amount": number,
  "note": string
}}

Rules:
- If message is not a financial transaction → should_save=false
- "امس" or "أمس" → {yesterday}
- Otherwise → {today}

Message:
{text}
"""

    response = openai_client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        max_output_tokens=300,
    )

    raw = response.output[0].content[0].text.value
    return json.loads(raw)


# ================== HANDLERS ==================
def help_command(update, context):
    update.message.reply_text(
        "✍️ اكتب العملية بشكل طبيعي:\n"
        "مثال:\n"
        "امس شريت 20 كيلو علف الغنم ب 100\n"
    )


def handle_message(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    try:
        data = analyze_with_ai(update.message.text)
    except Exception as e:
        update.message.reply_text(f"❌ OpenAI error:\n{e}")
        return

    if not data.get("should_save"):
        update.message.reply_text("ℹ️ ليست عملية مالية")
        return

    try:
        sheet = get_sheet()
        sheet.append_row(
            [
                data["date"],
                data["process"],
                data["type"],
                data["amount"],
                data["note"],
                update.message.from_user.first_name,
            ],
            value_input_option="USER_ENTERED",
        )

        update.message.reply_text(
            "✅ تم الحفظ\n"
            f"العملية: {data['process']}\n"
            f"النوع: {data['type']}\n"
            f"المبلغ: {data['amount']}\n"
            f"التاريخ: {data['date']}"
        )
    except Exception as e:
        update.message.reply_text(f"❌ Google Sheets error:\n{e}")


# ================== MAIN ==================
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
