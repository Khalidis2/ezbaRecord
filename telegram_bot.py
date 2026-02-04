# file: telegram_bot.py
import os
import re
import json
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from openai import OpenAI

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.environ.get("SHEET_ID")

if not all([BOT_TOKEN, OPENAI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID]):
    raise RuntimeError("Missing environment variables: BOT_TOKEN / OPENAI_API_KEY / GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

ALLOWED_USERS = {47329648}
USER_NAMES = {47329648: "أنت"}

def get_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client_gs = gspread.authorize(creds)
    return client_gs.open_by_key(SHEET_ID).sheet1

def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS

def extract_json_from_raw(raw_text):
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)
    try:
        return json.loads(raw_text)
    except Exception:
        pass
    start = raw_text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")
    for end in range(len(raw_text)-1, start, -1):
        candidate = raw_text[start:end+1]
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise ValueError("no parseable JSON found")

def analyze_with_ai(text):
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    system_instructions = (
        "أنت مساعد مالي لمزرعة وغنم. أعد فقط JSON صالح بدون أي تعليق.\n"
        "{\n"
        '  "should_save": true|false,\n'
        '  "date": "YYYY-MM-DD",\n'
        '  "process": "شراء"|"بيع"|"فاتورة"|"راتب"|"أخرى",\n'
        '  "type": "علف"|"عمال"|"علاج"|"كهرباء"|"ماء"|"اخرى",\n'
        '  "amount": رقم,\n'
        '  "note": "نص"\n'
        "}\n"
        f"- إذا قال أمْس/امس → استخدم {yesterday}\n"
        f"- خلاف ذلك → استخدم {today}\n"
        "إذا ليست عملية مالية، اجعل should_save = false."
    )
    prompt = system_instructions + "\n\nUserMessage: " + json.dumps({"message": text}, ensure_ascii=False)
    try:
        resp = openai_client.responses.create(model="gpt-4.1-mini", input=prompt, max_output_tokens=400)
    except Exception as e:
        raise RuntimeError(f"OpenAI API call failed: {e}")
    raw = None
    try:
        raw = getattr(resp, "output_text", None)
    except Exception:
        raw = None
    if not raw:
        try:
            out = getattr(resp, "output", None)
            if out and len(out) > 0:
                first = out[0]
                content = None
                if isinstance(first, dict):
                    content = first.get("content")
                else:
                    content = getattr(first, "content", None)
                if isinstance(content, list) and len(content) > 0:
                    c0 = content[0]
                    text_field = None
                    if isinstance(c0, dict):
                        text_field = c0.get("text") or c0.get("content") or c0
                    else:
                        text_field = getattr(c0, "text", None) or getattr(c0, "content", None) or c0
                    if isinstance(text_field, dict):
                        raw = text_field.get("value") or text_field.get("text") or json.dumps(text_field, ensure_ascii=False)
                    elif isinstance(text_field, str):
                        raw = text_field
                    else:
                        raw = str(text_field)
                else:
                    raw = str(first)
        except Exception:
            raw = None
    if not raw:
        raw = str(resp)
    print("RAW_OPENAI_RESPONSE:", raw)
    try:
        data = extract_json_from_raw(raw)
    except Exception as e:
        raise RuntimeError(f"failed to parse JSON from OpenAI response: {e}\nRAW: {raw[:1000]}")
    if not isinstance(data, dict):
        raise RuntimeError(f"AI returned non-dict JSON: {type(data)}")
    return data

def compute_previous_total(sheet):
    try:
        rows = sheet.get_all_values()
    except Exception:
        return 0.0
    if len(rows) <= 1:
        return 0.0
    total = 0.0
    for row in rows[1:]:
        if len(row) >= 4:
            amt_s = row[3].strip()
            if amt_s:
                try:
                    total += float(amt_s.replace(",", ""))
                except Exception:
                    continue
    return total

def help_command(update, context):
    update.message.reply_text("✍️ اكتب العملية بشكل طبيعي:\nمثال: امس شريت 20 كيلو علف الغنم ب 100\nأو استخدم /help")

def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return
    text = update.message.text
    try:
        ai_data = analyze_with_ai(text)
    except Exception as e:
        print("ERROR in analyze_with_ai:", repr(e))
        update.message.reply_text(f"❌ OpenAI parse error:\n{e}")
        return
    if not ai_data.get("should_save", False):
        update.message.reply_text("ℹ️ ليست عملية مالية — لم يتم حفظ شيء.")
        return
    date_str = ai_data.get("date") or datetime.now().date().isoformat()
    process = ai_data.get("process") or "أخرى"
    type_ = ai_data.get("type") or "اخرى"
    amount = ai_data.get("amount")
    note = ai_data.get("note") or text
    if amount is None:
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            update.message.reply_text("❌ لم أقدر أستخرج مبلغ. اذكر المبلغ صريحًا.")
            return
        amount = float(m.group(1).replace(",", "."))
    try:
        amount = float(amount)
    except Exception:
        update.message.reply_text("❌ المبلغ غير واضح، ارسله كرقم.")
        return
    try:
        sheet = get_sheet()
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في الوصول للـ Google Sheets: {e}")
        return
    try:
        prev_total = compute_previous_total(sheet)
        new_total = round(prev_total + amount, 2)
        sheet.append_row([date_str, process, type_, amount, note, update.message.from_user.first_name, new_total], value_input_option="USER_ENTERED")
        update.message.reply_text(f"✅ تم الحفظ — {process} | {type_} | {amount} | {date_str}\nالمجموع الكلي الآن: {new_total}")
    except Exception as e:
        print("ERROR saving to sheet:", repr(e))
        update.message.reply_text(f"❌ خطأ في الحفظ: {e}")

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
    expenses = load_expenses()
    today = datetime.now().date()
    start = today - timedelta(days=6)
    total = sum(e["amount"] for e in expenses if start <= e["date"] <= today)
    update.message.reply_text(f"📊 تقرير الأسبوع: {total}")

def month_report(update, context):
    expenses = load_expenses()
    today = datetime.now().date()
    start = datetime(today.year, today.month, 1).date()
    total = sum(e["amount"] for e in expenses if start <= e["date"] <= today)
    update.message.reply_text(f"📊 تقرير الشهر: {total}")

def status_report(update, context):
    expenses = load_expenses()
    today = datetime.now().date()
    week_start = today - timedelta(days=6)
    month_start = datetime(today.year, today.month, 1).date()
    total_today = sum(e["amount"] for e in expenses if e["date"] == today)
    total_week = sum(e["amount"] for e in expenses if week_start <= e["date"] <= today)
    total_month = sum(e["amount"] for e in expenses if month_start <= e["date"] <= today)
    update.message.reply_text(f"اليوم: {total_today}\nآخر 7 أيام: {total_week}\nهذا الشهر: {total_month}")

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("week", week_report))
    dp.add_handler(CommandHandler("month", month_report))
    dp.add_handler(CommandHandler("status", status_report))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
