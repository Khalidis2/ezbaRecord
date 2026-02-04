# file: telegram_bot.py
import os
import re
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
    raise RuntimeError(
        "Missing environment variables: BOT_TOKEN / OPENAI_API_KEY / "
        "GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID"
    )

# ================== CLIENTS ==============
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# حط IDs اللي تسمح لهم يستخدمون البوت هنا
ALLOWED_USERS = {47329648, 6894180427}
USER_NAMES = {
    47329648: "Khaled",
    6894180427: "Hamad" 
}

# نخزن آخر رسالة تنتظر تأكيد لكل مستخدم
PENDING_MESSAGES = {}


def get_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client_gs = gspread.authorize(creds)
    return client_gs.open_by_key(SHEET_ID).sheet1


def authorized(update):
    return update.message.from_user.id in ALLOWED_USERS


# ================== AI HELPERS ==================
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

    for end in range(len(raw_text) - 1, start, -1):
        candidate = raw_text[start : end + 1]
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
        "استخدم السكيم التالي:\n"
        "{\n"
        '  \"should_save\": true|false,\n'
        '  \"date\": \"YYYY-MM-DD\",\n'
        '  \"process\": \"شراء\"|\"بيع\"|\"فاتورة\"|\"راتب\"|\"أخرى\",\n'
        '  \"type\": \"علف\"|\"منتجات\"|\"عمال\"|\"علاج\"|\"كهرباء\"|\"ماء\"|\"اخرى\",\n'
        '  \"item\": \"وصف قصير للشيء (بيض، حليب، علف، ...)\",\n'
        '  \"amount\": رقم موجب فقط,\n'
        '  \"note\": \"نص\"\n'
        "}\n\n"
        "التاريخ:\n"
        f"- إذا قال أمس/امس → استخدم {yesterday}\n"
        f"- إذا لم يذكر تاريخ أو قال اليوم → استخدم {today}\n"
        "- إذا ذكر تاريخ صريح فحوّله إلى YYYY-MM-DD.\n\n"
        "process:\n"
        "- شراء: عند شراء أي شيء.\n"
        "- بيع: عند بيع أي شيء.\n"
        "- فاتورة: كهرباء، ماء، صيانة، فواتير.\n"
        "- راتب: رواتب العمال.\n"
        "- أخرى: أي شيء غير ذلك.\n\n"
        "type:\n"
        "- علف: علف، شعير، برسيم، تبن، مركزات.\n"
        "- منتجات: بيض، حليب، لحم، صوف، سمن، أي منتج من المزرعة.\n"
        "- عمال: رواتب أو مصاريف العمال.\n"
        "- علاج: دواء، علاج، بيطري.\n"
        "- كهرباء: كهرب، مولد.\n"
        "- ماء: ماء، مويه.\n"
        "- اخرى: غير ذلك.\n\n"
        "amount:\n"
        "- دائماً رقم موجب (بدون سالب).\n"
        "إذا لم تكن الرسالة عملية مالية، اجعل should_save = false."
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
    try:
        raw = getattr(resp, "output_text", None)
    except Exception:
        raw = None

    if not raw:
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
                        text_field = (
                            c0.get("text", text_field)
                            or c0.get("content", text_field)
                            or c0
                        )
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

    data = extract_json_from_raw(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"AI returned non-dict JSON: {type(data)}")
    return data


# ================== BALANCE HELPERS ==================
def compute_previous_balance(sheet):
    try:
        rows = sheet.get_all_values()
    except Exception:
        return 0.0

    if len(rows) <= 1:
        return 0.0

    balance = 0.0
    for row in rows[1:]:
        if len(row) < 5:
            continue
        proc = row[1].strip() if len(row) > 1 and row[1] else ""
        amount_str = row[4].strip()
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


# ================== COMMANDS ==================
def start_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك باستخدام هذا البوت.")
        return
    update.message.reply_text(
        "👋 أهلاً، هذا بوت المحاسبة للمزرعة.\n"
        "اكتب أي عملية شراء أو بيع بالعربي بشكل طبيعي في هذه المحادثة أو في القروب.\n"
        "البوت راح يرسل لك رسالة تأكيد، وبعدها تستخدم /confirm للحفظ.\n"
        "استخدم /help لرؤية كل الأوامر."
    )


def help_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك باستخدام هذا البوت.")
        return

    text = (
        "📋 أوامر البوت:\n\n"
        "🆘 /help\n"
        "عرض قائمة الأوامر هذه.\n\n"
        "💰 /balance\n"
        "عرض الرصيد الحالي الحقيقي (الدخل − المصاريف) منذ بداية الدفتر.\n\n"
        "↩️ /undo\n"
        "حذف آخر عملية محفوظة (التراجع خطوة واحدة).\n\n"
        "📅 /week\n"
        "ملخص آخر 7 أيام:\n"
        "الدخل (+) ، المصاريف (−) ، والصافي (الدخل − المصاريف).\n\n"
        "📆 /month\n"
        "ملخص هذا الشهر:\n"
        "الدخل (+) ، المصاريف (−) ، والصافي (الدخل − المصاريف).\n\n"
        "📊 /status\n"
        "ملخص اليوم + آخر 7 أيام + هذا الشهر:\n"
        "لكل فترة يعرض:\n"
        "الدخل (+) ، المصاريف (−) ، والصافي.\n\n"
        "✅ /confirm\n"
        "تأكيد وحفظ آخر رسالة كتبتها في Google Sheets بعد تحليلها بالذكاء الاصطناعي.\n\n"
        "❌ /cancel\n"
        "إلغاء آخر رسالة قيد التأكيد وعدم حفظها.\n\n"
        "✍️ طريقة الاستخدام:\n"
        "1️⃣ اكتب رسالة طبيعية عن عملية بيع أو شراء.\n"
        "   مثال: بعت 50 بيضة ب 100 درهم.\n"
        "2️⃣ البوت يرسل لك رسالة تأكيد.\n"
        "3️⃣ إذا موافق، أرسل /confirm ليتم التحليل والحفظ.\n"
        "4️⃣ إذا ما تبي تحفظها، أرسل /cancel.\n"
        "5️⃣ إذا حفظت شيء بالغلط، استخدم /undo لحذف آخر عملية محفوظة.\n"
    )
    update.message.reply_text(text)


def cancel_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    if user_id in PENDING_MESSAGES:
        del PENDING_MESSAGES[user_id]
        update.message.reply_text("❌ تم إلغاء العملية، لن يتم حفظ شيء.")
    else:
        update.message.reply_text("ℹ️ لا توجد عملية قيد التأكيد حالياً.")


def confirm_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    pending = PENDING_MESSAGES.get(user_id)
    if not pending:
        update.message.reply_text("ℹ️ لا توجد رسالة قيد التأكيد. أرسل رسالة جديدة أولاً.")
        return

    text = pending["text"]
    del PENDING_MESSAGES[user_id]

    try:
        ai_data = analyze_with_ai(text)
    except Exception as e:
        print("ERROR in analyze_with_ai:", repr(e))
        update.message.reply_text(f"❌ OpenAI error:\n{e}")
        return

    if not ai_data.get("should_save", False):
        update.message.reply_text(
            "ℹ️ بعد التحليل تبيّن أنها ليست عملية مالية — لم يتم حفظ شيء."
        )
        return

    date_str = ai_data.get("date") or datetime.now().date().isoformat()
    process = ai_data.get("process") or "أخرى"
    type_ = ai_data.get("type") or "اخرى"
    item = ai_data.get("item") or ""
    amount = ai_data.get("amount")
    note = ai_data.get("note") or text

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

    person_name = USER_NAMES.get(
        user_id, update.message.from_user.first_name or "مستخدم"
    )

    try:
        sheet = get_sheet()
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في الوصول إلى Google Sheets: {e}")
        return

    prev_balance = compute_previous_balance(sheet)
    signed_amount = amount if process == "بيع" else -amount
    new_balance = round(prev_balance + signed_amount, 2)

    try:
        sheet.append_row(
            [date_str, process, type_, item, amount, note, person_name, new_balance],
            value_input_option="USER_ENTERED",
        )
        sign_str = "+" if signed_amount >= 0 else "-"
        update.message.reply_text(
            "✅ تم الحفظ في Google Sheets\n"
            f"{date_str} | {process} | {type_} | {item or '-'} | {amount}\n"
            f"التأثير على الرصيد: {sign_str}{abs(signed_amount)}\n"
            f"الرصيد الآن: {new_balance}"
        )
    except Exception as e:
        print("ERROR saving to sheet:", repr(e))
        update.message.reply_text(f"❌ خطأ في الحفظ داخل Google Sheets:\n{e}")


def balance_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    try:
        sheet = get_sheet()
        balance = compute_previous_balance(sheet)
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في قراءة الرصيد من Google Sheets:\n{e}")
        return

    update.message.reply_text(f"💰 الرصيد الحالي في الدفتر: {balance}")


def undo_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في الوصول إلى Google Sheets:\n{e}")
        return

    if len(rows) <= 1:
        update.message.reply_text("ℹ️ لا توجد أي عملية لحذفها (الجدول فارغ).")
        return

    last_row_index = len(rows)
    last_row = rows[-1]

    date_str = last_row[0] if len(last_row) > 0 else ""
    process = last_row[1] if len(last_row) > 1 else ""
    type_ = last_row[2] if len(last_row) > 2 else ""
    item = last_row[3] if len(last_row) > 3 else ""
    amount = last_row[4] if len(last_row) > 4 else ""
    balance_value = last_row[7] if len(last_row) > 7 else ""

    try:
        sheet.delete_rows(last_row_index)
        update.message.reply_text(
            "↩️ تم التراجع عن آخر عملية وحذفها من Google Sheets:\n"
            f"{date_str} | {process} | {type_} | {item or '-'} | {amount}\n"
            f"الرصيد في الصف المحذوف كان: {balance_value}\n"
            "إذا كان هذا الحذف بالخطأ، تحتاج تعيد إدخال العملية مرة أخرى."
        )
    except Exception as e:
        print("ERROR deleting last row:", repr(e))
        update.message.reply_text(f"❌ تعذر حذف آخر عملية:\n{e}")


# ================== MESSAGE HANDLER ==================
def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    text = update.message.text
    PENDING_MESSAGES[user_id] = {"text": text}

    update.message.reply_text(
        "📨 تأكيد العملية\n"
        f"رسالتك:\n\"{text}\"\n\n"
        "هل أنت متأكد أنك تريد حفظ هذه العملية في Google Sheets؟\n"
        "إذا نعم، أرسل الأمر: /confirm\n"
        "إذا لا، أرسل: /cancel"
    )


# ================== REPORT HELPERS ==================
def load_expenses():
    sheet = get_sheet()
    rows = sheet.get_all_values()
    expenses = []
    for row in rows[1:]:
        if len(row) < 5:
            continue
        date_str = row[0].strip()
        process = row[1].strip() if len(row) > 1 and row[1] else ""
        amount_str = row[4].strip()
        if not date_str or not amount_str:
            continue
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            amount = float(str(amount_str).replace(",", ""))
        except Exception:
            continue
        expenses.append({"date": d, "amount": amount, "process": process})
    return expenses


def summarize_period(expenses, start_date, end_date):
    income = 0.0
    expense = 0.0
    net = 0.0

    for e in expenses:
        if not (start_date <= e["date"] <= end_date):
            continue
        amt = e["amount"]
        if e["process"] == "بيع":
            income += amt
            net += amt
        else:
            expense += amt
            net -= amt

    return round(income, 2), round(expense, 2), round(net, 2)


# ================== REPORT COMMANDS ==================
def week_report(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    expenses = load_expenses()
    today = datetime.now().date()
    start = today - timedelta(days=6)

    income, expense, net = summarize_period(expenses, start, today)

    update.message.reply_text(
        f"📅 ملخص آخر 7 أيام (من {start} إلى {today}):\n"
        f"الدخل: +{income}\n"
        f"المصاريف: -{expense}\n"
        f"الصافي: {net:+}"
    )


def month_report(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    expenses = load_expenses()
    today = datetime.now().date()
    start = datetime(today.year, today.month, 1).date()

    income, expense, net = summarize_period(expenses, start, today)

    update.message.reply_text(
        f"📆 ملخص هذا الشهر ({today.year}-{today.month:02d}):\n"
        f"الدخل: +{income}\n"
        f"المصاريف: -{expense}\n"
        f"الصافي: {net:+}"
    )


def status_report(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    expenses = load_expenses()
    today = datetime.now().date()
    week_start = today - timedelta(days=6)
    month_start = datetime(today.year, today.month, 1).date()

    inc_today, exp_today, net_today = summarize_period(expenses, today, today)
    inc_week, exp_week, net_week = summarize_period(expenses, week_start, today)
    inc_month, exp_month, net_month = summarize_period(expenses, month_start, today)

    update.message.reply_text(
        "📊 ملخص الدخل والمصاريف:\n\n"
        f"📌 اليوم ({today}):\n"
        f"الدخل: +{inc_today}\n"
        f"المصاريف: -{exp_today}\n"
        f"الصافي: {net_today:+}\n\n"
        f"📌 آخر 7 أيام (من {week_start} إلى {today}):\n"
        f"الدخل: +{inc_week}\n"
        f"المصاريف: -{exp_week}\n"
        f"الصافي: {net_week:+}\n\n"
        f"📌 هذا الشهر ({today.year}-{today.month:02d}):\n"
        f"الدخل: +{inc_month}\n"
        f"المصاريف: -{exp_month}\n"
        f"الصافي: {net_month:+}"
    )


# ================== MAIN ==================
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("cancel", cancel_command))
    dp.add_handler(CommandHandler("confirm", confirm_command))
    dp.add_handler(CommandHandler("balance", balance_command))
    dp.add_handler(CommandHandler("undo", undo_command))
    dp.add_handler(CommandHandler("week", week_report))
    dp.add_handler(CommandHandler("month", month_report))
    dp.add_handler(CommandHandler("status", status_report))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
