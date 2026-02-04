# file: telegram_bot.py
import os
import re
import json
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from openai import OpenAI

# ============== ENV =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.environ.get("SHEET_ID")

if not all([BOT_TOKEN, OPENAI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID]):
    raise RuntimeError("Missing environment variables: BOT_TOKEN / OPENAI_API_KEY / GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID")

# ============== Clients ============
openai_client = OpenAI(api_key=OPENAI_API_KEY)


def get_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client_gs = gspread.authorize(creds)
    return client_gs.open_by_key(SHEET_ID).sheet1


# ============== Config =============
ALLOWED_USERS = {47329648}
USER_NAMES = {47329648: "أنت"}
ALLOWED_PROCESSES = {"شراء", "بيع", "فاتورة", "راتب", "أخرى"}
ALLOWED_TYPES = {"علف", "عمال", "علاج", "كهرباء", "ماء", "اخرى"}


def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS


# ============== AI helper (robust extractor) ============
def extract_json_from_raw(raw_text):
    """
    Tries to find a valid JSON object inside raw_text.
    Returns Python object if found, else raises ValueError.
    """
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)

    # try direct load if it is clean JSON
    try:
        return json.loads(raw_text)
    except Exception:
        pass

    # find the first JSON object (greedy-ish) between braces
    # This finds the first '{' and the last '}' after it.
    start = raw_text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")
    end = raw_text.rfind("}")
    if end == -1 or end <= start:
        raise ValueError("no JSON object end found in response")
    json_candidate = raw_text[start:end+1]

    # try iterative tightening if there are nested braces mismatches
    # attempt to parse; if fails, try to find next closing brace
    for i in range(end, start, -1):
        candidate = raw_text[start:i+1]
        try:
            return json.loads(candidate)
        except Exception:
            continue

    # final attempt
    return json.loads(json_candidate)  # may raise


def analyze_with_ai(text):
    """
    Sends prompt to OpenAI and returns parsed JSON dict.
    This function is defensive: it handles multiple SDK shapes and string responses.
    """
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

    system_instructions = (
        "أنت مساعد مالي لمزرعة وغنم. أعد فقط JSON صالح بدون أي تعليق.\n"
        "الشكل:\n"
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

    user_block = json.dumps({"message": text}, ensure_ascii=False)

    prompt = system_instructions + "\n\nUserMessage: " + user_block

    try:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            max_output_tokens=400,
        )
    except Exception as e:
        # Bubble up the concrete OpenAI error for logs / telegram
        raise RuntimeError(f"OpenAI API call failed: {e}")

    # attempt safe extraction in multiple ways
    raw = None
    try:
        # 1) preferred convenient aggregated field if present
        raw = getattr(resp, "output_text", None)
    except Exception:
        raw = None

    if not raw:
        # 2) try the structured path (SDK returns objects / lists)
        try:
            out = getattr(resp, "output", None)
            if out and len(out) > 0:
                first = out[0]
                # content may be list/dict/object
                content = None
                if isinstance(first, dict):
                    content = first.get("content")
                else:
                    content = getattr(first, "content", None)

                if isinstance(content, list) and len(content) > 0:
                    c0 = content[0]
                    # try multiple shapes for text field
                    text_field = None
                    if isinstance(c0, dict):
                        # dictionary shape
                        text_field = c0.get("text") or c0.get("content") or c0
                    else:
                        text_field = getattr(c0, "text", None) or getattr(c0, "content", None) or c0

                    # if text_field is a dict with 'value'
                    if isinstance(text_field, dict):
                        raw = text_field.get("value") or text_field.get("text") or json.dumps(text_field, ensure_ascii=False)
                    elif isinstance(text_field, str):
                        raw = text_field
                    else:
                        raw = str(text_field)
                else:
                    # fallback: stringify first
                    raw = str(first)
        except Exception as e:
            # log and continue to fallback
            print("DEBUG: structured extraction failed:", repr(e))
            raw = None

    if not raw:
        # 3) last fallback: use str(resp)
        raw = str(resp)

    # Log raw response for debugging (check Railway logs)
    print("RAW_OPENAI_RESPONSE:", raw)

    # Try to extract/parse JSON from raw text
    try:
        data = extract_json_from_raw(raw)
    except Exception as e:
        # include raw snippet in raised error for easier debugging in logs
        raise RuntimeError(f"failed to parse JSON from OpenAI response: {e}\nRAW: {raw[:1000]}")

    # Basic validation of expected keys
    if not isinstance(data, dict):
        raise RuntimeError(f"AI returned non-dict JSON: {type(data)}")

    return data


# ============== Handlers ==============
def help_command(update, context):
    update.message.reply_text(
        "✍️ اكتب العملية بشكل طبيعي:\nمثال: امس شريت 20 كيلو علف الغنم ب 100\nأو استخدم /help"
    )


def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    text = update.message.text
    try:
        ai_data = analyze_with_ai(text)
    except Exception as e:
        # Show exact error to help debugging (safe because only allowed users talk to bot)
        print("ERROR in analyze_with_ai:", repr(e))
        update.message.reply_text(f"❌ OpenAI parse error:\n{e}")
        return

    if not ai_data.get("should_save", False):
        update.message.reply_text("ℹ️ ليست عملية مالية — لم يتم حفظ شيء.")
        return

    # fallback safety for fields
    date_str = ai_data.get("date") or datetime.now().date().isoformat()
    process = ai_data.get("process") or "أخرى"
    type_ = ai_data.get("type") or "اخرى"
    amount = ai_data.get("amount")
    note = ai_data.get("note") or text

    # try fallback numeric extraction
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
        sheet.append_row([date_str, process, type_, amount, note, update.message.from_user.first_name], value_input_option="USER_ENTERED")
        update.message.reply_text(f"✅ تم الحفظ — {process} | {type_} | {amount} | {date_str}")
    except Exception as e:
        print("ERROR saving to sheet:", repr(e))
        update.message.reply_text(f"❌ خطأ في الحفظ: {e}")


# ============== Reports (simple) ==============
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


# ============== Main ==============
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
