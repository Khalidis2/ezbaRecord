# file: telegram_bot.py
import os
import re
import json
import threading
import http.server
import socketserver
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

# المستخدمين المصرح لهم
ALLOWED_USERS = {47329648, 6894180427}
USER_NAMES = {
    47329648: "خالد",
    6894180427: "حمد",
}

# نخزن آخر رسالة تنتظر تأكيد لكل مستخدم
# { user_id: {"text": str, "ai": dict} }
PENDING_MESSAGES = {}


# ================== SHEETS HELPERS ==================
def _get_gspread_client():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client_gs = gspread.authorize(creds)
    return client_gs


def get_expense_sheet():
    client_gs = _get_gspread_client()
    return client_gs.open_by_key(SHEET_ID).sheet1


def get_livestock_summary_sheet():
    client_gs = _get_gspread_client()
    sh = client_gs.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("المواشي - إجمالي")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="المواشي - إجمالي", rows=1000, cols=3)
        ws.append_row(
            ["نوع الحيوان", "السلالة", "العدد الحالي"],
            value_input_option="USER_ENTERED",
        )
    return ws


def get_meta_sheet():
    """ورقة داخلية لتخزين ميتا المواشي لكل صف في Azba Expenses."""
    client_gs = _get_gspread_client()
    sh = client_gs.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Azba Meta")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Azba Meta", rows=1000, cols=4)
        ws.append_row(
            ["Row", "AnimalType", "Breed", "Delta"],
            value_input_option="USER_ENTERED",
        )
    return ws


def log_livestock_meta(row_index: int, animal_type: str, breed: str, delta: int):
    """نسجل ارتباط صف Azba Expenses مع تعديل المواشي في ورقة Azba Meta."""
    try:
        meta_sheet = get_meta_sheet()
        meta_sheet.append_row(
            [row_index, animal_type or "", breed or "", delta],
            value_input_option="USER_ENTERED",
        )
    except Exception as e:
        print("ERROR logging livestock meta:", repr(e))


def fetch_livestock_meta_for_row(row_index: int):
    """نرجع (meta_row_index_in_meta_sheet, meta_dict) لصف معيّن أو (None, None)."""
    try:
        meta_sheet = get_meta_sheet()
        rows = meta_sheet.get_all_values()
    except Exception as e:
        print("ERROR reading Azba Meta:", repr(e))
        return None, None

    for idx, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        row_id_str = (row[0] or "").strip()
        try:
            rid = int(row_id_str)
        except Exception:
            continue
        if rid == row_index:
            meta = {
                "animal_type": row[1] if len(row) > 1 else "",
                "breed": row[2] if len(row) > 2 else "",
                "delta": int(float(row[3])) if len(row) > 3 and row[3] else 0,
            }
            return idx, meta

    return None, None


def delete_meta_row(meta_row_index: int):
    try:
        meta_sheet = get_meta_sheet()
        meta_sheet.delete_rows(meta_row_index)
    except Exception as e:
        print("ERROR deleting meta row:", repr(e))


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
    """تحليل موحّد لكل شيء: عمليات مالية + استعلامات + مواشي."""
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

    system_instructions = (
        "أنت مساعد محاسبي ومساعد لإدارة المواشي في مزرعة.\n"
        "اقرأ رسالة المستخدم وحدد نيته بدقة، ثم أعد فقط JSON صالح بدون أي تعليق.\n\n"
        "السكيم:\n"
        "{\n"
        '  "intent": "expense_create" | "financial_query" | '
        '            "livestock_baseline" | "livestock_change" | '
        '            "livestock_status" | "other",\n'
        '\n'
        '  "date": "YYYY-MM-DD" أو null,\n'
        '\n'
        '  "process": "شراء"|"بيع"|"فاتورة"|"راتب"|"أخرى"|null,\n'
        '  "type": "علف"|"منتجات"|"عمال"|"علاج"|"كهرباء"|"ماء"|"اخرى"|null,\n'
        '  "item": نص قصير أو null,\n'
        '  "amount": رقم موجب أو null,\n'
        '  "note": نص أو null,\n'
        '\n'
        '  "query_period": "today"|"yesterday"|"this_week"|"last_7_days"|"this_month"|"all_time"|null,\n'
        '  "query_process": مثل process أو null,\n'
        '  "query_type": مثل type أو null,\n'
        '  "query_item": نص أو null,\n'
        '\n'
        '  "livestock_entries": [\n'
        "     {\n"
        '       "animal_type": "غنم"|"أبقار"|"ثور"|"جمال"|"ماعز"|"اخرى",\n'
        '       "breed": "حري"|"صلالي"|"صومالي"|"سوري"|"اضاحي"|"اخرى",\n'
        '       "count": عدد صحيح موجب,\n'
        '       "movement": "إجمالي"|"إضافة"|"نقص"|"بيع"|"نفوق"|"مواليد"\n'
        "     }\n"
        "  ] أو [],\n"
        '\n'
        '  "livestock_status_target": true|false\n'
        "}\n\n"
        "اختر intent حسب معنى الرسالة:\n"
        "- إذا كانت عملية مالية للحفظ في الدفتر (شراء، بيع، فاتورة، راتب...) → intent = \"expense_create\".\n"
        "- إذا كان سؤال عن مبالغ (كم صرفت، كم ربحت، كم دخلت من بيع شيء...) → intent = \"financial_query\".\n"
        "- إذا كانت رسالة حصر مثل: \"سجل العدد الكلي للمواشي\" → intent = \"livestock_baseline\" "
        "وملّئ livestock_entries مع movement = \"إجمالي\".\n"
        "- إذا كانت بيع/شراء/نفوق/مواليد لعدد محدد من المواشي بدون التركيز على المبلغ "
        "أو مع مبلغ لكن التركيز على تعديل الأعداد → اجعل intent = \"expense_create\" إذا كان هناك مبلغ واضح، "
        "مع تعبئة الحقول المالية، واملأ livestock_entries لتعديل الأعداد.\n"
        "- إذا طلب المستخدم كشف أو حالة المواشي (مثل: اعطني كشف المواشي، كم عندي مواشي) "
        "→ intent = \"livestock_status\".\n"
        "- إذا كانت الرسالة تحتوي على تغيير في أعداد المواشي فقط بدون أي مبلغ واضح (مثل: نفق 2 حري) "
        "→ intent = \"livestock_change\" واملأ livestock_entries بما يناسب.\n"
        "- إذا كانت الرسالة لا تنطبق على ما سبق → intent = \"other\".\n\n"
        f"لو قال اليوم أو لم يذكر تاريخ استخدم {today}, لو قال امس استخدم {yesterday}.\n"
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

    raw = getattr(resp, "output_text", None)
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


def has_explicit_date(text: str) -> bool:
    if not isinstance(text, str):
        return False
    t = (
        text.replace("إ", "ا")
        .replace("أ", "ا")
        .replace("آ", "ا")
        .replace("ى", "ي")
    )
    if re.search(r"\d{1,4}\s*[/-]\s*\d{1,2}(\s*[/-]\s*\d{1,4})?", t):
        return True
    keywords = ["امس", "قبل امس", "اليوم"]
    return any(k in t for k in keywords)


def choose_date_from_ai(ai_date, original_text: str) -> str:
    today = datetime.now().date()
    if has_explicit_date(original_text):
        if isinstance(ai_date, str):
            m = re.match(r"\d{4}-\d{2}-\d{2}", ai_date.strip())
            if m:
                return m.group(0)
        return today.isoformat()
    return today.isoformat()


# ================== BALANCE & EXPENSE HELPERS ==================
def compute_balance_from_rows(rows):
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


def compute_previous_balance(sheet):
    try:
        rows = sheet.get_all_values()
    except Exception:
        return 0.0
    return compute_balance_from_rows(rows)


# ================== LIVESTOCK SUMMARY ==================
def _norm_arabic(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.strip()
    s = (
        s.replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
    )
    s = re.sub(r"[^\u0621-\u063A\u0641-\u064A0-9]+", "", s)
    return s


def update_livestock_summary(animal_type: str, breed: str, count: int, movement: str):
    """تحديث تبويب المواشي - إجمالي حسب حركة واحدة."""
    animal_type_raw = animal_type or ""
    breed_raw = breed or ""
    animal_type_n = _norm_arabic(animal_type_raw)
    breed_n = _norm_arabic(breed_raw)
    movement = (movement or "").strip()

    try:
        sheet = get_livestock_summary_sheet()
        rows = sheet.get_all_values()
    except Exception as e:
        print("ERROR accessing livestock summary sheet:", repr(e))
        return

    current_row_index = None
    current_value = 0
    current_breed_display = breed_raw
    same_type_rows = []

    for idx, row in enumerate(rows[1:], start=2):
        a_raw = row[0] or ""
        b_raw = row[1] or ""
        a_n = _norm_arabic(a_raw)
        b_n = _norm_arabic(b_raw)
        if a_n == animal_type_n:
            same_type_rows.append((idx, a_raw, b_raw, row))
        if a_n == animal_type_n and breed_n and b_n == breed_n:
            current_row_index = idx
            current_breed_display = b_raw
            try:
                current_value = int(float((row[2] or "0").strip()))
            except Exception:
                current_value = 0
            break

    if current_row_index is None and movement != "إجمالي" and same_type_rows:
        idx, a_raw, b_raw, row = same_type_rows[0]
        current_row_index = idx
        current_breed_display = b_raw
        try:
            current_value = int(float((row[2] or "0").strip()))
        except Exception:
            current_value = 0

    if movement == "إجمالي":
        new_value = count
        if current_row_index is None and same_type_rows:
            idx, a_raw, b_raw, row = same_type_rows[0]
            current_row_index = idx
            current_breed_display = b_raw
    else:
        minus_moves = {"بيع", "نقص", "نفوق"}
        sign = -1 if movement in minus_moves else 1
        new_value = current_value + sign * count
        if new_value < 0:
            new_value = 0

    if current_row_index is None:
        display_animal = animal_type_raw or (same_type_rows[0][1] if same_type_rows else "")
        display_breed = (
            breed_raw
            or current_breed_display
            or (same_type_rows[0][2] if same_type_rows else "اخرى")
        )
        try:
            sheet.append_row(
                [display_animal, display_breed, new_value],
                value_input_option="USER_ENTERED",
            )
        except Exception as e:
            print("ERROR appending summary row:", repr(e))
    else:
        try:
            sheet.update_cell(current_row_index, 3, new_value)
        except Exception as e:
            print("ERROR updating summary row:", repr(e))


def get_livestock_totals():
    sheet = get_livestock_summary_sheet()
    rows = sheet.get_all_values()
    totals = {}
    for row in rows[1:]:
        if len(row) < 3:
            continue
        animal = (row[0] or "").strip()
        breed = (row[1] or "").strip()
        count_str = (row[2] or "").strip()
        if not count_str:
            continue
        try:
            cnt = int(float(count_str))
        except Exception:
            continue
        totals[(animal or "-", breed or "-")] = cnt
    return totals


def reply_livestock_status(update):
    try:
        totals = get_livestock_totals()
    except Exception as e:
        update.message.reply_text(f"❌ خطأ في قراءة سجلات المواشي من Google Sheets:\n{e}")
        return

    if not totals:
        update.message.reply_text("ℹ️ لا توجد أي سجلات مواشي حالياً في تبويب \"المواشي - إجمالي\".")
        return

    lines = []
    overall = 0
    for (animal, breed), cnt in sorted(totals.items()):
        overall += cnt
        lines.append(f"{animal} | {breed}: {cnt}")

    msg = (
        "🐑 الأعداد الحالية للمواشي في العزبة (من تبويب \"المواشي - إجمالي\"):\n"
        + "\n".join(lines)
        + f"\n\nالمجموع الكلي لجميع الأنواع: {overall}"
    )
    update.message.reply_text(msg)


# ================== REPORT HELPERS ==================
def load_expenses():
    sheet = get_expense_sheet()
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


# ================== PREVIEW MESSAGE ==================
def send_preview_message(update, user_id, text, ai_data):
    intent = ai_data.get("intent") or "other"

    date_str = choose_date_from_ai(ai_data.get("date"), text)
    process = ai_data.get("process") or "أخرى"
    type_ = ai_data.get("type") or "اخرى"
    item = ai_data.get("item") or ""
    amount = ai_data.get("amount")

    if amount is None:
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if m:
            amount = float(m.group(1).replace(",", "."))
    try:
        if amount is not None:
            amount = float(amount)
            if amount < 0:
                amount = abs(amount)
    except Exception:
        amount = None

    person_name = USER_NAMES.get(
        user_id, update.message.from_user.first_name or "مستخدم"
    )

    # حساب الرصيد المتوقع
    try:
        sheet = get_expense_sheet()
        prev_balance = compute_previous_balance(sheet)
    except Exception:
        prev_balance = None

    balance_preview = "سيتم حسابه عند الحفظ"
    if intent == "expense_create" and amount is not None and prev_balance is not None:
        signed_amount = amount if process == "بيع" else -amount
        new_balance = round(prev_balance + signed_amount, 2)
        sign_str = "+" if signed_amount >= 0 else "-"
        balance_preview = (
            f"{prev_balance} → {new_balance} (التغيير: {sign_str}{abs(signed_amount)})"
        )

    # معاينة تأثير المواشي إن وجد
    livestock_entries = ai_data.get("livestock_entries") or []
    livestock_preview_lines = []
    for e in livestock_entries:
        animal_type = e.get("animal_type") or "-"
        breed = e.get("breed") or "-"
        movement = e.get("movement") or ""
        count = e.get("count")
        try:
            count_val = int(float(count)) if count is not None else None
        except Exception:
            count_val = None
        if count_val is None:
            continue
        minus_moves = {"بيع", "نقص", "نفوق"}
        sign = "-" if movement in minus_moves else "+"
        livestock_preview_lines.append(
            f"{animal_type} | {breed} | الحركة: {movement} | التغيير: {sign}{count_val}"
        )

    livestock_preview = ""
    if livestock_preview_lines:
        livestock_preview = "\n🐑 تأثير المواشي (متوقع):\n" + "\n".join(
            livestock_preview_lines
        )

    amount_txt = str(amount) if amount is not None else "غير معروف (لم أستطع قراءته)"

    if intent == "expense_create":
        preview_msg = (
            "📨 تأكيد العملية المالية\n"
            f"رسالتك:\n\"{text}\"\n\n"
            "سيتم تسجيل هذه العملية في ورقة *Azba Expenses* بالشكل التالي (تقريبي):\n\n"
            f"🗓 التاريخ: {date_str}\n"
            f"🔁 نوع العملية: {process}\n"
            f"🏷 التصنيف: {type_}\n"
            f"📝 البند: {item or '-'}\n"
            f"💰 المبلغ: {amount_txt}\n"
            f"👤 الشخص: {person_name}\n"
            f"📊 الرصيد المتوقع بعد العملية: {balance_preview}"
            f"{livestock_preview}\n\n"
            "إذا موافق، أرسل /confirm\n"
            "إذا لا، أرسل /cancel"
        )
    elif intent == "livestock_change":
        preview_msg = (
            "📨 تأكيد تعديل المواشي\n"
            f"رسالتك:\n\"{text}\"\n\n"
            "سيتم تطبيق التغييرات التالية على تبويب \"المواشي - إجمالي\":\n"
            f"{livestock_preview or 'لا توجد تغييرات واضحة'}\n\n"
            "لن يتم تسجيل عملية مالية في Azba Expenses (إلا إذا احتجتها لاحقاً).\n\n"
            "إذا موافق، أرسل /confirm\n"
            "إذا لا، أرسل /cancel"
        )
    elif intent == "livestock_baseline":
        # هذا الفرع عادة يُستخدم من handle_message مباشرة، لكن نتركه هنا للاكتمال
        preview_msg = (
            "📨 تأكيد تسجيل المواشي (حصر كامل)\n"
            f"رسالتك:\n\"{text}\"\n\n"
            f"{livestock_preview or 'لا توجد بيانات أعداد'}\n\n"
            "إذا موافق، أرسل /confirm\n"
            "إذا لا، أرسل /cancel"
        )
    else:
        preview_msg = (
            "لم أستطع تحديد نوع العملية بشكل واضح، جرب تعيد صياغة الرسالة أو استخدم /help."
        )

    update.message.reply_text(preview_msg)


# ================== COMMANDS ==================
def start_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك باستخدام هذا البوت.")
        return
    update.message.reply_text(
        "👋 أهلاً، هذا بوت المحاسبة للمزرعة.\n"
        "اكتب بشكل طبيعي، وأنا أوصل الكلام للذكاء الاصطناعي وهو يحدد المطلوب:\n"
        "- تسجيل عملية مالية\n"
        "- سؤال عن مبلغ\n"
        "- حصر للمواشي\n"
        "- تعديل أعداد المواشي\n"
        "- أو كشف بأعداد المواشي الحالية\n\n"
        "ثم أنفّذ لك اللي تريده على Google Sheets."
    )


def help_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك باستخدام هذا البوت.")
        return

    text = (
        "📋 أمثلة على ما يمكنك كتابته:\n\n"
        "💰 عمليات مالية:\n"
        "  - شريت علف بـ 1000\n"
        "  - بعت 3 أبقار بـ 4000\n\n"
        "📊 أسئلة مالية:\n"
        "  - كم صرفت على العلف هذا الشهر؟\n"
        "  - كم دخل من بيع الأضاحي هذه السنة؟\n\n"
        "🐑 مواشي:\n"
        "  - سجل العدد الكلي للمواشي كالتالي: عدد (60) حري ...\n"
        "  - نفق 2 حري\n"
        "  - اعطني كشف المواشي\n\n"
        "أوامر سريعة:\n"
        "  /balance - عرض الرصيد الحالي\n"
        "  /undo - التراجع عن آخر عملية مالية (مع عكس تعديل المواشي)\n"
        "  /week - ملخص آخر 7 أيام\n"
        "  /month - ملخص هذا الشهر\n"
        "  /status - ملخص اليوم + الأسبوع + الشهر\n"
        "  /livestock - عرض أعداد المواشي الحالية مباشرة\n"
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
    ai_data = pending.get("ai") or {}
    intent = ai_data.get("intent") or "other"

    # نزيلها من pending فوراً
    del PENDING_MESSAGES[user_id]

    # ========= 1) حصر كامل للمواشي =========
    if intent == "livestock_baseline":
        livestock_entries = ai_data.get("livestock_entries") or []
        if not isinstance(livestock_entries, list) or not livestock_entries:
            update.message.reply_text("❌ لا توجد بيانات مواشي صالحة للحفظ.")
            return

        date_str = choose_date_from_ai(ai_data.get("date"), text)

        try:
            sheet = get_livestock_summary_sheet()
            sheet.clear()
            sheet.append_row(
                ["نوع الحيوان", "السلالة", "العدد الحالي"],
                value_input_option="USER_ENTERED",
            )

            saved = 0
            for e in livestock_entries:
                animal_type = e.get("animal_type") or ""
                breed = e.get("breed") or ""
                count = e.get("count")
                try:
                    count_val = int(float(count)) if count is not None else None
                except Exception:
                    count_val = None
                if count_val is None or count_val <= 0:
                    continue
                sheet.append_row(
                    [animal_type, breed, count_val],
                    value_input_option="USER_ENTERED",
                )
                saved += 1

            if saved == 0:
                update.message.reply_text(
                    "❌ لم يتم حفظ أي بند، تأكد من صياغة رسالة الحصر."
                )
            else:
                update.message.reply_text(
                    f"✅ تم تحديث أعداد المواشي في تبويب \"المواشي - إجمالي\" ({saved} بنود).\n"
                    f"التاريخ (للمعلومية فقط): {date_str}"
                )
        except Exception as e:
            print("ERROR rebuilding livestock summary:", repr(e))
            update.message.reply_text(
                f"❌ حدث خطأ أثناء تحديث تبويب \"المواشي - إجمالي\":\n{e}"
            )
        return

    # ========= 2) تعديل مواشي بدون عملية مالية =========
    if intent == "livestock_change":
        livestock_entries = ai_data.get("livestock_entries") or []
        if not isinstance(livestock_entries, list) or not livestock_entries:
            update.message.reply_text("❌ لا توجد تغييرات مواشي واضحة لتطبيقها.")
            return

        applied = 0
        for e in livestock_entries:
            animal_type = e.get("animal_type") or ""
            breed = e.get("breed") or ""
            movement = e.get("movement") or ""
            count = e.get("count")
            try:
                count_val = int(float(count)) if count is not None else None
            except Exception:
                count_val = None
            if count_val is None or count_val <= 0:
                continue
            update_livestock_summary(animal_type, breed, count_val, movement)
            applied += 1

        if applied == 0:
            update.message.reply_text("❌ لم يتم تطبيق أي تغيير، راجع صياغة الرسالة.")
        else:
            update.message.reply_text(
                f"✅ تم تطبيق {applied} تغيير/تغييرات على أعداد المواشي في تبويب \"المواشي - إجمالي\"."
            )
        return

    # ========= 3) عملية مالية (مع احتمال تعديل مواشي) =========
    if intent == "expense_create":
        date_str = choose_date_from_ai(ai_data.get("date"), text)
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
            sheet = get_expense_sheet()
            rows = sheet.get_all_values()
        except Exception as e:
            update.message.reply_text(f"❌ خطأ في الوصول إلى Google Sheets: {e}")
            return

        prev_balance = compute_balance_from_rows(rows)
        next_row_index = len(rows) + 1

        signed_amount = amount if process == "بيع" else -amount
        new_balance = round(prev_balance + signed_amount, 2)

        # --- تعديل المواشي + تسجيل الميتا ---
        livestock_entries = ai_data.get("livestock_entries") or []
        livestock_msg_lines = []
        for e in livestock_entries:
            animal_type = e.get("animal_type") or ""
            breed = e.get("breed") or ""
            movement = e.get("movement") or ""
            count = e.get("count")
            try:
                count_val = int(float(count)) if count is not None else None
            except Exception:
                count_val = None
            if count_val is None or count_val <= 0:
                continue

            try:
                update_livestock_summary(animal_type, breed, count_val, movement)
                minus_moves = {"بيع", "نقص", "نفوق"}
                sign = -1 if movement in minus_moves else 1
                delta_int = sign * count_val
                log_livestock_meta(next_row_index, animal_type, breed, delta_int)
                sign_str = "+" if delta_int >= 0 else "-"
                livestock_msg_lines.append(
                    f"{animal_type or '-'} | {breed or '-'} | التغيير: {sign_str}{abs(delta_int)} (الحركة: {movement})"
                )
            except Exception as e:
                print("ERROR updating livestock summary from expense:", repr(e))
                livestock_msg_lines.append(
                    f"{animal_type or '-'} | {breed or '-'} | ⚠️ لم أستطع تحديثه (خطأ داخلي)"
                )

        try:
            sheet.append_row(
                [date_str, process, type_, item, amount, note, person_name, new_balance],
                value_input_option="USER_ENTERED",
            )
        except Exception as e:
            print("ERROR saving to sheet:", repr(e))
            update.message.reply_text(f"❌ خطأ في الحفظ داخل Google Sheets:\n{e}")
            return

        sign_str = "+" if signed_amount >= 0 else "-"
        livestock_msg = ""
        if livestock_msg_lines:
            livestock_msg = "\n🐑 تعديل المواشي:\n" + "\n".join(livestock_msg_lines)

        msg = (
            "✅ تم حفظ العملية في ورقة *Azba Expenses*:\n\n"
            f"🗓 التاريخ: {date_str}\n"
            f"🔁 نوع العملية: {process}\n"
            f"🏷 التصنيف: {type_}\n"
            f"📝 البند: {item or '-'}\n"
            f"💰 المبلغ: {amount}\n"
            f"👤 الشخص: {person_name}\n"
            f"📊 الرصيد بعد العملية: {new_balance} (التغيير: {sign_str}{abs(signed_amount)})"
            f"{livestock_msg}"
        )
        update.message.reply_text(msg)
        return

    # أي intent آخر
    update.message.reply_text(
        "لم أستطع تنفيذ هذه العملية بعد التأكيد، لأن نوعها غير مدعوم حالياً."
    )


def balance_command(update, context):
    user_id = update.message.from_user.id
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
        return

    try:
        sheet = get_expense_sheet()
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
        sheet = get_expense_sheet()
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

    livestock_undo_msg = ""
    meta_row_idx, meta = fetch_livestock_meta_for_row(last_row_index)
    if meta:
        try:
            animal_type = meta.get("animal_type") or ""
            breed = meta.get("breed") or ""
            delta_int = int(float(meta.get("delta", 0)))
            if delta_int != 0:
                if delta_int < 0:
                    movement = "إضافة"
                    count = abs(delta_int)
                    sign_str = "+"
                else:
                    movement = "نقص"
                    count = delta_int
                    sign_str = "-"
                if count > 0:
                    update_livestock_summary(animal_type, breed, count, movement)
                    livestock_undo_msg = (
                        f"\n🐑 تم عكس تعديل المواشي: {animal_type or '-'} | "
                        f"{breed or '-'} | {sign_str}{count}"
                    )
                    if meta_row_idx:
                        delete_meta_row(meta_row_idx)
        except Exception as e:
            print("ERROR undoing livestock from meta:", repr(e))

    try:
        sheet.delete_rows(last_row_index)
        update.message.reply_text(
            "↩️ تم التراجع عن آخر عملية وحذفها من Google Sheets:\n"
            f"{date_str} | {process} | {type_} | {item or '-'} | {amount}\n"
            f"الرصيد في الصف المحذوف كان: {balance_value}"
            f"{livestock_undo_msg}\n"
            "إذا كان هذا الحذف بالخطأ، تحتاج تعيد إدخال العملية مرة أخرى."
        )
    except Exception as e:
        print("ERROR deleting last row:", repr(e))
        update.message.reply_text(f"❌ تعذر حذف آخر عملية:\n{e}")


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


def livestock_status_command(update, context):
    if not authorized(update):
        update.message.reply_text("❌ غير مصرح لك")
    else:
        reply_livestock_status(update)


# ================== MESSAGE HANDLER ==================
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
        update.message.reply_text(
            "❌ صار خطأ أثناء تحليل الرسالة بالذكاء الاصطناعي، حاول مرة ثانية."
        )
        return

    intent = ai_data.get("intent") or "other"
    print("AI_INTENT:", intent)

    # 1) كشف المواشي
    if intent == "livestock_status":
        reply_livestock_status(update)
        return

    # 2) حصر كامل للمواشي → نحتاج تأكيد
    if intent == "livestock_baseline":
        livestock_entries = ai_data.get("livestock_entries") or []
        if not isinstance(livestock_entries, list) or not livestock_entries:
            update.message.reply_text("❌ لم أستطع فهم أعداد المواشي من الرسالة.")
            return

        lines = []
        for e in livestock_entries:
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

        PENDING_MESSAGES[user_id] = {"text": text, "ai": ai_data}

        update.message.reply_text(
            "📨 تأكيد تسجيل المواشي (حصر كامل)\n"
            f"رسالتك:\n\"{text}\"\n\n"
            "سيتم تحديث الأعداد التالية في تبويب \"المواشي - إجمالي\":\n"
            + "\n".join(lines)
            + "\n\nإذا موافق، أرسل /confirm\n"
            "إذا لا، أرسل /cancel"
        )
        return

    # 3) تعديل مواشي فقط → تأكيد
    if intent == "livestock_change":
        livestock_entries = ai_data.get("livestock_entries") or []
        if not isinstance(livestock_entries, list) or not livestock_entries:
            update.message.reply_text("❌ لم أستطع فهم تغييرات المواشي من الرسالة.")
            return

        PENDING_MESSAGES[user_id] = {"text": text, "ai": ai_data}
        send_preview_message(update, user_id, text, ai_data)
        return

    # 4) استعلام مالي
    if intent == "financial_query":
        answer_query_from_ai(update, ai_data, text)
        return

    # 5) عملية مالية (مع أو بدون مواشي)
    if intent == "expense_create":
        PENDING_MESSAGES[user_id] = {"text": text, "ai": ai_data}
        send_preview_message(update, user_id, text, ai_data)
        return

    # 6) أي شيء آخر
    update.message.reply_text(
        "ℹ️ لم أفهم طلبك بشكل واضح، جرب تكتبها بطريقة أبسط أو استخدم /help."
    )


# ================== HEALTH SERVER (لـ Render) ==================
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


# ================== MAIN ==================
def main():
    # سيرفر صحة لـ Render
    server_thread = threading.Thread(target=start_health_server, daemon=True)
    server_thread.start()

    print("Starting Telegram bot...")
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
    dp.add_handler(CommandHandler("livestock", livestock_status_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # نحذف أي Webhook قديم
    try:
        updater.bot.delete_webhook()
        me = updater.bot.get_me()
        print(f"Bot connected as @{me.username}")
    except Exception as e:
        print("ERROR connecting to Telegram:", repr(e))

    updater.start_polling()
    print("Bot is now polling for updates...")
    updater.idle()


if __name__ == "__main__":
    main()
