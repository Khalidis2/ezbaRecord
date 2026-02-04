# file: telegram_bot.py
import re
import os
import json
from datetime import datetime, timedelta, date

import gspread
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from openai import OpenAI

TOKEN = os.environ.get("BOT_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

ALLOWED_USERS = [
    47329648,
    222222222,
    333333333,
    444444444,
]

USER_NAMES = {
    47329648: "أنت",
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

ALLOWED_PROCESSES = {"شراء", "بيع", "فاتورة", "راتب", "أخرى"}
ALLOWED_TYPES = {"علف", "عمال", "علاج", "كهرباء", "ماء", "اخرى"}

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def get_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client_gs = gspread.authorize(creds)
    return client_gs.open_by_key(SHEET_ID).sheet1


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


def detect_process(text, user_id):
    if user_id in USER_PROCESS_OVERRIDE:
        return USER_PROCESS_OVERRIDE[user_id]
    for process, keywords in PROCESS_KEYWORDS.items():
        if any(k in text for k in keywords):
            return process
    return "أخرى"


def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS


def help_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    update.message.reply_text(
        "📋 أوامر البوت:\n\n"
        "✍️ تسجيل مصروف أو بيع/شراء:\n"
        "– اكتب الجملة بشكل طبيعي، مثال:\n"
        "  اشتريت علف 200 اليوم\n"
        "  بعت 20 خروف ب 8000 أمس\n\n"
        "⚙️ نوع العملية اليدوي (اختياري):\n"
        "– /process شراء\n"
        "– /process بيع\n"
        "– /process فاتورة\n"
        "– /process راتب\n\n"
        "📊 التقارير:\n"
        "– /week   مجموع مصاريف آخر 7 أيام\n"
        "– /month  مجموع مصاريف هذا الشهر\n"
        "– /status ملخص اليوم + الأسبوع + الشهر\n\n"
        "ℹ️ كل رسالة عادية يحللها الذكاء الاصطناعي ويحفظها في Google Sheets إذا كانت عملية مالية."
    )


def process_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    if not context.args:
        update.message.reply_text("❌ استخدم: /process شراء | بيع | فاتورة | راتب | أخرى")
        return
    proc = context.args[0]
    if proc not in ALLOWED_PROCESSES:
        update.message.reply_text("❌ نوع عملية غير معروف، استخدم: شراء / بيع / فاتورة / راتب / أخرى")
        return
    USER_PROCESS_OVERRIDE[user_id] = proc
    update.message.reply_text(f"✅ تم تعيين نوع العملية لهذا المستخدم: {proc}")


def ai_analyze_message(text, user_id):
    if not client:
        return None

    person_name = USER_NAMES.get(user_id, "مستخدم")
    process_override = USER_PROCESS_OVERRIDE.get(user_id)
    today = datetime.now().date().isoformat()

    system_instructions = (
        "أنت مساعد مالي لمزرعة وغنم. مهمتك تحويل أي رسالة إلى JSON واحد فقط بدون أي نص إضافي.\n"
        "الهدف هو تسجيل العمليات في Google Sheets.\n\n"
        "أخرج دائمًا JSON بهذا الشكل:\n"
        "{\n"
        '  "should_save": true أو false,\n'
        '  "date": "YYYY-MM-DD",\n'
        '  "process": "شراء" أو "بيع" أو "فاتورة" أو "راتب" أو "أخرى",\n'
        '  "type": "علف" أو "عمال" أو "علاج" أو "كهرباء" أو "ماء" أو "اخرى",\n'
        '  "amount": رقم عشري (بدون نص),\n'
        '  "note": "النص الأصلي أو وصف مختصر"\n'
        "}\n\n"
        "إذا لم تكن الرسالة عن عملية مالية (شراء/بيع/فاتورة/راتب/مصروف)، اجعل should_save = false.\n\n"
        "تفسير التاريخ:\n"
        f"- إذا قال اليوم أو ما ذكر تاريخ، استخدم تاريخ اليوم: {today}\n"
        "- إذا قال أمس أو امس، اجعل التاريخ تاريخ أمس.\n"
        "- إذا ذكر تاريخ صريح، استخدمه بصيغة YYYY-MM-DD.\n\n"
        "نوع العملية process:\n"
        "- شراء: عند شراء شيء للمزرعة أو العلف أو أغراض.\n"
        "- بيع: عند بيع غنم أو علف أو أي شيء.\n"
        "- فاتورة: كهرباء، ماء، صيانة، فواتير.\n"
        "- راتب: رواتب العمال.\n"
        "- أخرى: أي شيء آخر.\n\n"
        "type:\n"
        "- علف: علف، شعير، برسيم، تبن.\n"
        "- عمال: رواتب العمال أو مصاريف متعلقة بهم.\n"
        "- علاج: دواء، علاج، بيطري.\n"
        "- كهرباء: كهرب، مولد.\n"
        "- ماء: ماء، مويه.\n"
        "- اخرى: غير ذلك.\n\n"
        "يجب أن يكون الإخراج JSON صالح تماماً بدون أي تعليق أو نص آخر."
    )

    user_content = {
        "person_name": person_name,
        "process_override": process_override,
        "message": text,
    }

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_instructions},
                {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            max_tokens=400,
        )
        raw = resp.choices[0].message.content
        data = json.loads(raw)
        return data
    except Exception as e:
        print("ERROR calling OpenAI or parsing JSON:", e)
        return None


def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    text = update.message.text

    ai_data = ai_analyze_message(text, user_id)
    if not ai_data:
        update.message.reply_text("❌ صار خطأ في تحليل الرسالة بالذكاء الاصطناعي، حاول مرة ثانية.")
        return

    should_save = ai_data.get("should_save", True)
    if not should_save:
        update.message.reply_text("ℹ️ ما اعتبرت هذه الرسالة عملية مالية، ما تم حفظ أي شيء.")
        return

    raw_date = ai_data.get("date") or detect_date(text)
    try:
        parsed_date = datetime.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
        date_str = parsed_date.isoformat()
    except Exception:
        date_str = detect_date(text)

    process = ai_data.get("process") or detect_process(text, user_id)
    if process not in ALLOWED_PROCESSES:
        process = detect_process(text, user_id)

    type_ = ai_data.get("type") or detect_type(text)
    if type_ not in ALLOWED_TYPES:
        type_ = detect_type(text)

    amount = ai_data.get("amount")
    if amount is None:
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            update.message.reply_text("❌ ما قدرت أستخرج مبلغ من الرسالة، حاول تذكر المبلغ بشكل أوضح.")
            return
        amount = float(m.group(1).replace(",", "."))

    try:
        amount = float(amount)
    except Exception:
        update.message.reply_text("❌ المبلغ اللي رجعه الذكاء الاصطناعي غير واضح، حاول مرة ثانية.")
        return

    note = ai_data.get("note") or text
    person_name = USER_NAMES.get(user_id, str(user_id))

    try:
        sheet = get_sheet()
        sheet.append_row(
            [
                date_str,
                process,
                type_,
                amount,
                note,
                person_name,
            ],
            value_input_option="USER_ENTERED",
        )
        update.message.reply_text(
            "✅ تم تسجيل العملية عن طريق الذكاء الاصطناعي\n"
            f"العملية: {process}\n"
            f"النوع: {type_}\n"
            f"المبلغ: {amount}\n"
            f"التاريخ: {date_str}\n"
            f"الشخص: {person_name}\n"
            f"الوصف: {note}"
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
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

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
