# file: telegram_bot.py
import os
import re
import json
from datetime import datetime, timedelta
import threading
import http.server
import socketserver

import gspread
from google.oauth2.service_account import Credentials
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from openai import OpenAI

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.environ.get("SHEET_ID")

if not all([BOT_TOKEN, OPENAI_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID]):
    raise RuntimeError(
        "Missing environment variables: BOT_TOKEN / OPENAI_API_KEY / "
        "GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID"
    )

openai_client = OpenAI(api_key=OPENAI_API_KEY)

ALLOWED_USERS = {47329648}
USER_NAMES = {
    47329648: "أنت",
}

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


def get_livestock_sheet():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client_gs = gspread.authorize(creds)
    sh = client_gs.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("المواشي")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="المواشي", rows=1000, cols=6)
        ws.append_row(
            ["التاريخ", "نوع الحيوان", "السلالة", "العدد", "نوع الحركة", "ملاحظة"],
            value_input_option="USER_ENTERED",
        )
    return ws


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
        "استخدم السكيم التالي للأعمال المالية:\n"
        "{\n"
        '  "should_save": true|false,\n'
        '  "mode": "transaction"|"query"|"other",\n'
        '  "date": "YYYY-MM-DD",\n'
        '  "process": "شراء"|"بيع"|"فاتورة"|"راتب"|"أخرى",\n'
        '  "type": "علف"|"منتجات"|"عمال"|"علاج"|"كهرباء"|"ماء"|"اخرى",\n'
        '  "item": "وصف قصير للشيء (بيض، حليب، علف، ...)",\n'
        '  "amount": رقم موجب فقط أو null إذا غير معروف,\n'
        '  "note": "نص",\n'
        '  "query_mode": true|false,\n'
        '  "query_process": "شراء"|"بيع"|"فاتورة"|"راتب"|"أخرى"|null,\n'
        '  "query_type": "علف"|"منتجات"|"عمال"|"علاج"|"كهرباء"|"ماء"|"اخرى"|null,\n'
        '  "query_item": نص أو null,\n'
        '  "query_period": "today"|"yesterday"|"this_week"|"last_7_days"|"this_month"|"all_time"\n'
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
        "- دائماً رقم موجب (بدون سالب).\n\n"
        "وضعيات الرسالة:\n"
        '- إذا كانت الرسالة تصف عملية شراء/بيع/مصروف/دخل حالية → mode = "transaction" و should_save = true.\n'
        '- إذا كانت الرسالة سؤال عن مبلغ سابق مثل: "كم صرفت/شريت/بعت علف هالشهر؟" أو "كم دخلنا من بيع البيض الأسبوع اللي فات؟" →\n'
        '  حينها mode = "query" و query_mode = true و should_save = false.\n'
        "  حدّد في query_process نوع العملية المناسبة،\n"
        "  و query_type أو query_item حسب الكلام،\n"
        "  و query_period حسب الكلام.\n"
        '- إذا لم تكن الرسالة متعلقة بالمال إطلاقاً → mode = "other" و should_save = false و query_mode = false.\n'
        "إذا لم تكن الرسالة عملية مالية يمكن حفظها، اجعل should_save = false دائماً."
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


def analyze_livestock(text):
    system_instructions = (
        "أنت مساعد لإدارة المواشي في المزرعة. أعد فقط JSON صالح بدون أي تعليق.\n"
        "المطلوب: تحويل النص إلى قائمة سجلات مواشي.\n"
        "استخدم السكيم التالي:\n"
        "{\n"
        '  "date": "YYYY-MM-DD",\n'
        '  "note": "نص قصير يصف هذه العملية أو الصورة العامة",\n'
        '  "entries": [\n'
        "    {\n"
        '      "animal_type": "غنم"|"أبقار"|"ثور"|"جمال"|"ماعز"|"اخرى",\n'
        '      "breed": "حري"|"صلالي"|"صومالي"|"سوري"|"اضاحي"|"اخرى",\n'
        '      "count": عدد صحيح موجب,\n'
        '      "movement": "إجمالي"|"إضافة"|"نقص"|"بيع"|"نفوق"|"مواليد"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "إذا كان النص مثل: \"سجل العدد الكلي للمواشي كالتالي: عدد (60) حري ...\" فهذا يعني صورة إجمالية للحظيرة،\n"
        "اجعل movement = \"إجمالي\" لكل بند، واملأ animal_type المناسب.\n"
        "إذا لم يذكر تاريخ صريح استخدم تاريخ اليوم بالتنسيق YYYY-MM-DD.\n"
        "count يجب أن يكون عدداً صحيحاً موجباً دائماً."
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
        raise RuntimeError(f"OpenAI API call failed (livestock): {e}")

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
            print("DEBUG: structured extraction failed livestock:", repr(e))
            raw = None

    if not raw:
        raw = str(resp)

    print("RAW_OPENAI_LIVESTOCK_RESPONSE:", raw)

    data = extract_json_from_raw(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"AI returned non-dict JSON (livestock): {type(data)}")
    return data


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


def start_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك باستخدام هذا البوت.")
        return
    update.message.reply_text(
        "👋 أهلاً، هذا بوت المحاسبة للمزرعة.\n"
        "اكتب أي عملية شراء أو بيع بالعربي بشكل طبيعي، أو اسأل عن المصاريف والدخل.\n"
        "تقدر بعد تسجل عدد المواشي برسالة مثل:\n"
        "سجل العدد الكلي للمواشي كالتالي: عدد (60) حري ...\n"
        "البوت يرسل لك رسالة تأكيد، وبعدها تستخدم /confirm للحفظ.\n"
        "استخدم /help لرؤية كل الأوامر."
    )


def help_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك باستخدام هذا البوت.")
        return

    text = (
        "📋 أوامر البوت:\n\n"
        "🆘 /help\n"
        "عرض قائمة الأوامر.\n\n"
        "💰 /balance\n"
        "عرض الرصيد الحالي (الدخل − المصاريف).\n\n"
        "↩️ /undo\n"
        "حذف آخر عملية محفوظة.\n\n"
        "📅 /week\n"
        "ملخص آخر 7 أيام.\n\n"
        "📆 /month\n"
        "ملخص هذا الشهر.\n\n"
        "📊 /status\n"
        "ملخص اليوم + آخر 7 أيام + هذا الشهر.\n\n"
        "✅ /confirm\n"
        "تأكيد وحفظ آخر رسالة بعد تحليلها.\n\n"
        "❌ /cancel\n"
        "إلغاء آخر رسالة قيد التأكيد.\n\n"
        "✍️ أمثلة للعمليات:\n"
        "• شريت علف ب 500 اليوم\n"
        "• بعت 50 بيضة ب 120\n\n"
        "❓ أمثلة للأسئلة (استعلام):\n"
        "• كم صرفت على العلف هذا الشهر؟\n"
        "• كم دخلنا من بيع البيض هذا الأسبوع؟\n\n"
        "🐑 أمثلة لتسجيل عدد المواشي:\n"
        "سجل العدد الكلي للمواشي كالتالي:\n"
        "عدد (60) حري\n"
        "عدد (8) صلالي\n"
        "عدد (7) أبقار\n"
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
    kind = pending.get("kind", "expense")

    if kind == "livestock":
        ai_data = pending.get("ai")
        del PENDING_MESSAGES[user_id]

        if not ai_data:
            update.message.reply_text("❌ لا توجد بيانات مواشي صالحة للحفظ.")
            return

        entries = ai_data.get("entries") or []
        if not isinstance(entries, list) or not entries:
            update.message.reply_text("❌ لم أجد أي سجلات مواشي في الرسالة.")
            return

        date_str = ai_data.get("date") or datetime.now().date().isoformat()
        note = ai_data.get("note") or text

        try:
            sheet = get_livestock_sheet()
        except Exception as e:
            update.message.reply_text(f"❌ خطأ في الوصول إلى شيت المواشي:\n{e}")
            return

        saved = 0
        for e in entries:
            animal_type = e.get("animal_type") or ""
            breed = e.get("breed") or ""
            movement = e.get("movement") or "إجمالي"
            count = e.get("count")
            if count is None:
                continue
            try:
                count_val = int(float(count))
                if count_val <= 0:
                    continue
            except Exception:
                continue

            try:
                sheet.append_row(
                    [date_str, animal_type, breed, count_val, movement, note],
                    value_input_option="USER_ENTERED",
                )
                saved += 1
            except Exception as ex:
                print("ERROR saving livestock row:", repr(ex))

        if saved == 0:
            update.message.reply_text("❌ لم يتم حفظ أي سجل مواشي، تحقق من الرسالة.")
        else:
            update.message.reply_text(
                f"✅ تم حفظ سجلات المواشي ({saved} صفوف) في تبويب المواشي.\n"
                f"التاريخ: {date_str}"
            )
        return

    ai_data = pending.get("ai")
    if not ai_data:
        try:
            ai_data = analyze_with_ai(text)
        except Exception as e:
            print("ERROR in analyze_with_ai:", repr(e))
            update.message.reply_text(f"❌ OpenAI error:\n{e}")
            del PENDING_MESSAGES[user_id]
            return

    del PENDING_MESSAGES[user_id]

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


def load_expenses():
    sheet = get_sheet()
    rows = sheet.get_all_values()
    expenses = []
    for row in rows[1:]:
        if len(row) < 5:
            continue
        date_str = row[0].strip()
        process = row[1].strip() if len(row) > 1 and row[1] else ""
        type_ = row[2].strip() if len(row) > 2 and row[2] else ""
        item = row[3].strip() if len(row) > 3 and row[3] else ""
        amount_str = row[4].strip()
        if not date_str or not amount_str:
            continue
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            amount = float(str(amount_str).replace(",", ""))
        except Exception:
            continue
        expenses.append(
            {"date": d, "amount": amount, "process": process, "type": type_, "item": item}
        )
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


def answer_query_from_ai(update, ai_data, original_text):
    try:
        expenses = load_expenses()
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في قراءة البيانات من Google Sheets:\n{e}")
        return

    today = datetime.now().date()
    period = ai_data.get("query_period") or "all_time"

    if period == "today":
        start = end = today
        period_label = "اليوم"
    elif period == "yesterday":
        d = today - timedelta(days=1)
        start = end = d
        period_label = "أمس"
    elif period in ("this_week", "last_7_days"):
        start = today - timedelta(days=6)
        end = today
        period_label = "آخر 7 أيام"
    elif period == "this_month":
        start = datetime(today.year, today.month, 1).date()
        end = today
        period_label = "هذا الشهر"
    else:
        start = datetime(1970, 1, 1).date()
        end = today
        period_label = "كل الفترة"

    q_process = ai_data.get("query_process") or None
    q_type = ai_data.get("query_type") or None
    q_item = ai_data.get("query_item") or None

    total = 0.0
    count = 0

    for e in expenses:
        if not (start <= e["date"] <= end):
            continue
        if q_process and e["process"] != q_process:
            continue
        if q_type and e.get("type") != q_type:
            continue
        if q_item and q_item not in (e.get("item") or ""):
            continue
        total += e["amount"]
        count += 1

    if q_process == "شراء":
        proc_txt = "المشتريات"
    elif q_process == "بيع":
        proc_txt = "المبيعات"
    elif q_process:
        proc_txt = f"عمليات {q_process}"
    else:
        proc_txt = "العمليات"

    detail_txt = ""
    if q_item:
        detail_txt = f" لـ {q_item}"
    elif q_type and q_type != "اخرى":
        detail_txt = f" ({q_type})"

    update.message.reply_text(
        "📊 نتيجة سؤالك:\n"
        f"إجمالي {proc_txt}{detail_txt} في {period_label}: {total}\n"
        f"عدد العمليات المحسوبة: {count}"
    )


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


def handle_message(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    text = update.message.text

    if "المواشي" in text and "سجل" in text and "عدد" in text:
        try:
            ai_livestock = analyze_livestock(text)
        except Exception as e:
            update.message.reply_text(f"❌ خطأ في تحليل نص المواشي:\n{e}")
            return

        entries = ai_livestock.get("entries") or []
        if not isinstance(entries, list) or not entries:
            update.message.reply_text("❌ لم أستطع فهم أعداد المواشي من الرسالة.")
            return

        lines = []
        for e in entries:
            animal_type = e.get("animal_type") or "-"
            breed = e.get("breed") or "-"
            count = e.get("count")
            try:
                count_val = int(float(count)) if count is not None else None
            except Exception:
                count_val = None
            if count_val is None:
                continue
            lines.append(f"{animal_type} | {breed} | {count_val}")

        if not lines:
            update.message.reply_text("❌ البيانات غير واضحة، لم أستطع استخراج الأعداد.")
            return

        PENDING_MESSAGES[user_id] = {"text": text, "ai": ai_livestock, "kind": "livestock"}

        update.message.reply_text(
            "📨 تأكيد تسجيل المواشي\n"
            f"رسالتك:\n\"{text}\"\n\n"
            "سيتم حفظ السجلات التالية في تبويب المواشي:\n"
            + "\n".join(lines)
            + "\n\nإذا موافق، أرسل /confirm\n"
            "إذا لا، أرسل /cancel"
        )
        return

    try:
        ai_data = analyze_with_ai(text)
    except Exception as e:
        print("ERROR in analyze_with_ai (handle_message):", repr(e))
        PENDING_MESSAGES[user_id] = {"text": text, "kind": "expense"}
        update.message.reply_text(
            "📨 تأكيد العملية\n"
            f"رسالتك:\n\"{text}\"\n\n"
            "هل أنت متأكد أنك تريد حفظ هذه العملية في Google Sheets؟\n"
            "إذا نعم، أرسل الأمر: /confirm\n"
            "إذا لا، أرسل: /cancel"
        )
        return

    if ai_data.get("query_mode"):
        answer_query_from_ai(update, ai_data, text)
        return

    if ai_data.get("should_save", False):
        PENDING_MESSAGES[user_id] = {"text": text, "ai": ai_data, "kind": "expense"}
        update.message.reply_text(
            "📨 تأكيد العملية\n"
            f"رسالتك:\n\"{text}\"\n\n"
            "هل أنت متأكد أنك تريد حفظ هذه العملية في Google Sheets؟\n"
            "إذا نعم، أرسل الأمر: /confirm\n"
            "إذا لا، أرسل: /cancel"
        )
        return

    update.message.reply_text(
        "ℹ️ هذه الرسالة ليست عملية مالية ولا سؤال عن مبلغ ولا تسجيل مواشي.\n"
        "اكتب عملية مثل: شريت علف بـ 100\n"
        "أو اسأل عن مبلغ مثل: كم صرفت على العلف هذا الشهر؟\n"
        "أو سجل المواشي مثل: سجل العدد الكلي للمواشي كالتالي: عدد (60) حري ..."
    )


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            return

    with socketserver.TCPServer(("", port), Handler) as httpd:
        print(f"Health server running on port {port}")
        httpd.serve_forever()


def main():
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()

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
