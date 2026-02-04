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


# ============== AI JSON helpers =============
def extract_json_from_raw(raw_text):
    """
    Try to parse JSON from raw_text, or find the first JSON object inside it.
    """
    if not isinstance(raw_text, str):
        raw_text = str(raw_text)

    # direct attempt
    try:
        return json.loads(raw_text)
    except Exception:
        pass

    start = raw_text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")

    # try progressively shorter substrings from the end
    for end in range(len(raw_text) - 1, start, -1):
        candidate = raw_text[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            continue

    raise ValueError("no parseable JSON found")


def analyze_with_ai(text):
    """
    Sends prompt to OpenAI and returns parsed JSON dict.
    JSON schema:
    {
      "should_save": true|false,
      "date": "YYYY-MM-DD",
      "process": "شراء"|"بيع"|"فاتورة"|"راتب"|"أخرى",
      "type": "علف"|"منتجات"|"عمال"|"علاج"|"كهرباء"|"ماء"|"اخرى",
      "amount": number (positive),
      "note": string
    }
    """
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

    system_instructions = (
        "أنت مساعد مالي لمزرعة وغنم. أعد فقط JSON صالح بدون أي تعليق.\n"
        "استخدم هذا السكيم:\n"
        "{\n"
        '  \"should_save\": true|false,\n'
        '  \"date\": \"YYYY-MM-DD\",\n'
        '  \"process\": \"شراء\"|\"بيع\"|\"فاتورة\"|\"راتب\"|\"أخرى\",\n'
        '  \"type\": \"علف\"|\"منتجات\"|\"عمال\"|\"علاج\"|\"كهرباء\"|\"ماء\"|\"اخرى\",\n'
        '  \"amount\": رقم موجب فقط (بدون إشارة + أو -),\n'
        '  \"note\": \"نص\"\n'
        "}\n\n"
        "تفسير التاريخ:\n"
        f"- إذا قال أمس/امس → استخدم {yesterday}\n"
        f"- إذا لم يذكر تاريخ أو قال اليوم → استخدم {today}\n"
        "- لو ذكر تاريخ صريح، حوّله إلى YYYY-MM-DD.\n\n"
        "process:\n"
        "- شراء: عند شراء أي شيء (علف، معدات، أغراض، حيوانات...)\n"
        "- بيع: عند بيع أي شيء (غنم، علف، بيض، منتجات...)\n"
        "- فاتورة: كهرباء، ماء، صيانة، فواتير رسمية.\n"
        "- راتب: رواتب العمال.\n"
        "- أخرى: أي شيء غير ذلك.\n\n"
        "type:\n"
        "- علف: علف، شعير، برسيم، تبن، مركزات.\n"
        "- منتجات: بيض، حليب، لحم، صوف، سمن، أي منتج يتم بيعه من المزرعة.\n"
        "- عمال: رواتب أو مصاريف تتعلق بالعمال.\n"
        "- علاج: دواء، علاج، بيطري.\n"
        "- كهرباء: كهرب، مولد، ديزل للمولد لو مخصص للكهرباء.\n"
        "- ماء: ماء، مويه، وايت ماء.\n"
        "- اخرى: أي شيء لا يناسب ما سبق.\n\n"
        "amount:\n"
        "- دائماً رقم موجب (مثلاً 100 ، 250.5). لا تضف سالب.\n"
        "- لا تضف عملة في القيمة.\n\n"
        "إذا لم تكن الرسالة عن عملية مالية، اجعل should_save = false."
    )

    user_block = json.dumps({"message": text}, ensure_ascii=False)
    prompt = system_instructions + "\n\nUserMessage:\n" + user_block

    try:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
            max_output_tokens=400,
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI API call failed: {e}")

    raw = None
    # Preferred: aggregated text if available
    try:
        raw = getattr(resp, "output_text", None)
    except Exception:
        raw = None

    if not raw:
        # Fallback: dig into structured fields
        try:
            out = getattr(resp, "output", None)
            if out and len(out) > 0:
                first = out[0]
                content = getattr(first, "content", None)
                if isinstance(first, dict):
                    content = first.get("content", content)

                if isinstance(content, list) and len(content) > 0:
                    c0 = content[0]
                    text_field = getattr(c0, "text", None)
                    if isinstance(c0, dict):
                        text_field = c0.get("text", text_field) or c0.get("content", text_field) or c0

                    if hasattr(text_field, "value"):
                        raw = text_field.value
                    elif isinstance(text_field, str):
                        raw = text_field
                    else:
                        raw = str(text_field)
                else:
                    raw = str(first)
        except Exception as e:
            print("DEBUG: structured extraction failed:", repr(e))
            raw = None

    if not raw:
        raw = str(resp)

    print("RAW_OPENAI_RESPONSE:", raw)

    try:
        data = extract_json_from_raw(raw)
    except Exception as e:
        raise RuntimeError(f"failed to parse JSON from OpenAI response: {e}\nRAW: {raw[:500]}")

    if not isinstance(data, dict):
        raise RuntimeError(f"AI returned non-dict JSON: {type(data)}")

    return data


# ============== Balance helper =============
def compute_previous_balance(sheet):
    """
    Recompute balance from all previous rows based on process & amount.
    بيع  -> +amount
    غير ذلك (شراء/فاتورة/راتب/أخرى) -> -amount
    """
    try:
        rows = sheet.get_all_values()
    except Exception:
        return 0.0

    if len(rows) <= 1:
        return 0.0

    balance = 0.0
    for row in rows[1:]:
        if len(row) < 4:
            continue
        proc = row[1].strip() if len(row) > 1 and row[1] else ""
        amount_str = row[3].strip()
        if not amount_str:
            continue
        try:
            amt = float(str(amount_str).replace(",", ""))
        except Exception:
            continue

        if proc == "بيع":
            balance += amt
        else:
            balance -= amt

    return round(balance, 2)


# ============== Handlers ==============
def help_command(update, context):
    update.message.reply_text(
        "✍️ مثال للشراء:\n"
        "امس اشتريت علف 20 كيس ب 500\n\n"
        "✍️ مثال للبيع (بيض / غنم / أي منتج):\n"
        "اليوم بعت 100 بيضة ب 100 درهم\n\n"
        "الرصيد يحسب هكذا:\n"
        "شراء / فاتورة / راتب = سالب من الرصيد\n"
        "بيع = زيادة على الرصيد\n"
    )


def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    text = update.message.text

    # 1) AI analysis
    try:
        ai_data = analyze_with_ai(text)
    except Exception as e:
        print("ERROR in analyze_with_ai:", repr(e))
        update.message.reply_text(f"❌ OpenAI error:\n{e}")
        return

    if not ai_data.get("should_save", False):
        update.message.reply_text("ℹ️ ليست عملية مالية — لم يتم حفظ شيء.")
        return

    # 2) Extract fields with fallbacks
    date_str = ai_data.get("date") or datetime.now().date().isoformat()
    process = ai_data.get("process") or "أخرى"
    type_ = ai_data.get("type") or "اخرى"
    amount = ai_data.get("amount")
    note = ai_data.get("note") or text

    # Ensure amount is numeric
    if amount is None:
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            update.message.reply_text("❌ لم أقدر أستخرج مبلغ. اذكر المبلغ كرقم واضح.")
            return
        amount = float(m.group(1).replace(",", "."))

    try:
        amount = float(amount)
        if amount < 0:
            amount = abs(amount)
    except Exception:
        update.message.reply_text("❌ المبلغ غير واضح، ارسله كرقم فقط.")
        return

    person_name = USER_NAMES.get(user_id, update.message.from_user.first_name or "مستخدم")

    # 3) Compute previous balance and new balance
    try:
        sheet = get_sheet()
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في الوصول إلى Google Sheets: {e}")
        return

    prev_balance = compute_previous_balance(sheet)

    # بيع = +amount, غيره = -amount
    signed_amount = amount if process == "بيع" else -amount
    new_balance = round(prev_balance + signed_amount, 2)

    # 4) Append row: date, process, type, amount, note, person, balance
    try:
        sheet.append_row(
            [date_str, process, type_, amount, note, person_name, new_balance],
            value_input_option="USER_ENTERED",
        )
        sign_str = "+" if signed_amount >= 0 else "-"
        update.message.reply_text(
            "✅ تم الحفظ\n"
            f"{date_str} | {process} | {type_} | {amount}\n"
            f"التأثير على الرصيد: {sign_str}{abs(signed_amount)}\n"
            f"الرصيد الآن: {new_balance}"
        )
    except Exception as e:
        print("ERROR saving to sheet:", repr(e))
        update.message.reply_text(f"❌ خطأ في الحفظ داخل Google Sheets:\n{e}")


# ============== Reports (ما تغيرت) ==============
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
    update.message.reply_text(f"📊 مجموع المبالغ (بدون إشارات) لآخر 7 أيام: {total}")


def month_report(update, context):
    expenses = load_expenses()
    today = datetime.now().date()
    start = datetime(today.year, today.month, 1).date()
    total = sum(e["amount"] for e in expenses if start <= e["date"] <= today)
    update.message.reply_text(f"📊 مجموع المبالغ (بدون إشارات) لهذا الشهر: {total}")


def status_report(update, context):
    expenses = load_expenses()
    today = datetime.now().date()
    week_start = today - timedelta(days=6)
    month_start = datetime(today.year, today.month, 1).date()
    total_today = sum(e["amount"] for e in expenses if e["date"] == today)
    total_week = sum(e["amount"] for e in expenses if week_start <= e["date"] <= today)
    total_month = sum(e["amount"] for e in expenses if month_start <= e["date"] <= today)
    update.message.reply_text(
        f"اليوم (مجموع المبالغ): {total_today}\n"
        f"آخر 7 أيام (مجموع المبالغ): {total_week}\n"
        f"هذا الشهر (مجموع المبالغ): {total_month}\n\n"
        "ملاحظة: هذه الأرقام بدون اعتبار إشارات الرصيد (فقط مجموع المبالغ)."
    )


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
