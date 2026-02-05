# telegram_bot.py
import os
import logging
import json
import re
from datetime import datetime, date, timedelta

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

import gspread
from google.oauth2.service_account import Credentials
import openai

# ================== CONFIG ==================

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"]           # e.g. "https://ezbarecord.onrender.com"
PORT = int(os.environ.get("PORT", "8000"))

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
SHEET_NAME = os.environ.get("SHEET_NAME", "records")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

ALLOWED_USERS = {47329648, 6894180427}
USER_NAMES = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

openai.api_key = OPENAI_API_KEY

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ================== GOOGLE SHEETS ==================

def get_gsheet():
    if not SPREADSHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise RuntimeError("Missing SPREADSHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON env vars")
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

# expected header row in sheet:
# date | type | category | item | amount | currency | user

# ================== TELEGRAM SETUP ==================

updater = Updater(BOT_TOKEN, use_context=True)
dispatcher = updater.dispatcher

def is_allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)

# ================== OPENAI HELPER ==================

SYSTEM_PROMPT = """
You are an AI assistant for a farm bookkeeping bot called "ezba record".
User messages are mostly in Gulf Arabic, sometimes English or Urdu.

Your job:
1) Understand what the user wants.
2) Decide ONE action:
   - "add_transaction"    → record expense or income
   - "get_report"         → show totals from Google Sheets
   - "help"               → explain how to use the bot
   - "chat"               → small talk, explanation or general answer
3) Return ONLY a single JSON object. No extra text, no markdown.

JSON SCHEMA:

{
  "action": "add_transaction" | "get_report" | "help" | "chat",
  "tx_type": "expense" | "income" | null,
  "category": "<short english label or null>",
  "item": "<short description in user's language or null>",
  "amount": float or null,
  "currency": "AED",
  "note": "<optional note or empty string>",
  "time_range": "today" | "yesterday" | "this_week" | "this_month" | "last_month" | "this_year" | "all_time" | null,
  "categories": [ "<category1>", "<category2>", ... ],
  "report_type": "summary" | "by_category" | null,
  "reply_language": "ar",
  "free_answer": "<string, used only if action='chat' or 'help'>"
}

INTERPRETATION RULES:

- If message sounds like recording money:
  - words like: "مصروف", "صرف", "فاتورة", "دفعت", "اشتريت", "expense", "cost" → tx_type="expense"
  - words like: "بعت", "مبيعات", "دخل", "income", "sale" → tx_type="income"
  - Extract the main money number (amount). If no number → amount=null.
  - Choose category in ENGLISH:
    - "electricity"  for كهرب
    - "water"        for ماي / ماء
    - "feed"         for علف / علايف
    - "medicine"     for دواء / أدوية / بيطري
    - "labor"        for عامل / رواتب
    - "eggs"         for بيض
    - "sheep"        for غنم / خروف
    - "chicken"      for دجاج
    - "other"        otherwise
  - item: short description including what was bought/sold.
  - Use "AED" for currency.
  - In this case: action="add_transaction".

- If message asks questions like:
  - "كم صرفت اليوم؟"
  - "اعرض مصاريف العلف والماء هذا الشهر"
  - "كم دخلت من بيع الغنم هالسنة؟"
  - "show expenses for water this month"
  Then it's a report:
   action="get_report"

  time_range detection:
    اليوم / today          → "today"
    امس / أمس / yesterday  → "yesterday"
    هالاسبوع / هذا الاسبوع / this week  → "this_week"
    هالشهر / هذا الشهر / this month      → "this_month"
    الشهر اللي طاف / last month          → "last_month"
    هالسنة / this year                   → "this_year"
    else                                  → "all_time"

  type vs both:
    if mentions only expense words → report only expenses
    if mentions only income / sale words → report only income
    if talks about overall status or both expenses and income → we will handle both in code,
    you just set report_type depending on if user wants breakdown or just total.

  categories:
    - List the english category labels user requested:
      - "feed", "water", "electricity", "medicine", "labor", "eggs", "sheep", "chicken", "other"
    - If user said "all", leave categories=[].

  report_type:
    - if user wants total only, set "summary"
    - if user wants breakdown by category, set "by_category"

- If user asks "how to use" or "/help" → action="help" and fill free_answer with short Arabic instructions.

- If it's small talk / general question not about money → action="chat" and put the Arabic answer in "free_answer".

IMPORTANT:
- ALWAYS return valid JSON only. No backticks, no markdown.
- If you are unsure, choose the closest reasonable interpretation.
"""

def call_openai_for_intent(user_text: str) -> dict:
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        content = resp.choices[0].message["content"]
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        return {
            "action": "chat",
            "free_answer": "صار خطأ في خدمة الذكاء الاصطناعي، جرّب بعد شوي.",
        }

    try:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No JSON object found")
        json_str = content[start : end + 1]
        data = json.loads(json_str)
        return data
    except Exception as e:
        log.exception("Failed to parse OpenAI JSON: %s\ncontent=%r", e, content)
        return {
            "action": "chat",
            "free_answer": "ما قدرت أفهم الرد من الذكاء الاصطناعي، جرّب تعيد صياغة الرسالة.",
        }

# ================== SHEET HELPERS ==================

def append_transaction_to_sheet(
    tx_type: str,
    category: str,
    item: str,
    amount: float,
    user_name: str,
):
    sheet = get_gsheet()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    row = [
        today_str,
        tx_type,
        category or "other",
        item or "",
        amount,
        "AED",
        user_name,
    ]
    sheet.append_row(row)

def parse_time_range(tr: str):
    today = date.today()
    if tr == "today":
        return today, today
    if tr == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if tr == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if tr == "this_month":
        start = today.replace(day=1)
        return start, today
    if tr == "last_month":
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        start = last_month_end.replace(day=1)
        return start, last_month_end
    if tr == "this_year":
        start = date(today.year, 1, 1)
        return start, today
    return None, None  # all_time

def read_report_from_sheet(
    time_range: str,
    categories: list,
    want_type: str,  # "expense", "income", "both"
):
    sheet = get_gsheet()
    rows = sheet.get_all_values()
    if not rows:
        return {}

    headers = rows[0]
    data_rows = rows[1:]

    idx_date = headers.index("date") if "date" in headers else 0
    idx_type = headers.index("type") if "type" in headers else 1
    idx_cat = headers.index("category") if "category" in headers else 2
    idx_amount = headers.index("amount") if "amount" in headers else 4

    start, end = parse_time_range(time_range)
    totals = {}

    for r in data_rows:
        if len(r) <= max(idx_date, idx_type, idx_cat, idx_amount):
            continue
        d_str = r[idx_date]
        t_type = r[idx_type]
        cat = r[idx_cat] or "other"
        try:
            amt = float(r[idx_amount])
        except Exception:
            continue

        if start and end:
            try:
                d_val = datetime.strptime(d_str, "%Y-%m-%d").date()
            except Exception:
                continue
            if d_val < start or d_val > end:
                continue

        if want_type == "expense" and t_type != "expense":
            continue
        if want_type == "income" and t_type != "income":
            continue

        if cat not in totals:
            totals[cat] = {"expense": 0.0, "income": 0.0}
        totals[cat][t_type] += amt

    if categories:
        for c in categories:
            if c not in totals:
                totals[c] = {"expense": 0.0, "income": 0.0}

    return totals

def summarize_report_text(
    user_question: str,
    time_range: str,
    totals: dict,
    want_type: str,
) -> str:
    if not totals:
        return "ما لقيت أي بيانات، الكل صفر."

    lines = []
    total_exp = 0.0
    total_inc = 0.0

    for cat, vals in totals.items():
        e = vals.get("expense", 0.0)
        i = vals.get("income", 0.0)
        total_exp += e
        total_inc += i

        if want_type == "expense":
            lines.append(f"{cat}: مصاريف {e:.2f}")
        elif want_type == "income":
            lines.append(f"{cat}: دخل {i:.2f}")
        else:
            lines.append(f"{cat}: مصاريف {e:.2f} / دخل {i:.2f}")

    prefix = ""
    if time_range == "today":
        prefix = "تقرير اليوم:\n"
    elif time_range == "yesterday":
        prefix = "تقرير أمس:\n"
    elif time_range == "this_month":
        prefix = "تقرير هذا الشهر:\n"
    elif time_range == "last_month":
        prefix = "تقرير الشهر اللي طاف:\n"
    elif time_range == "this_year":
        prefix = "تقرير هذه السنة:\n"

    extra = ""
    if want_type == "expense":
        extra = f"\nالمجموع الكلي للمصاريف: {total_exp:.2f} درهم."
    elif want_type == "income":
        extra = f"\nالمجموع الكلي للدخل: {total_inc:.2f} درهم."
    else:
        net = total_inc - total_exp
        extra = (
            f"\nإجمالي المصاريف: {total_exp:.2f} درهم.\n"
            f"إجمالي الدخل: {total_inc:.2f} درهم.\n"
            f"الصافي: {net:.2f} درهم."
        )

    return prefix + "\n".join(lines) + extra

# ================== HANDLERS ==================

def start_cmd(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text(
        "✅ ezba bot online\n"
        "اكتب مصروف أو بيع بالعربي، أو اسأل: كم صرفت اليوم، كم دخلت من الغنم هذا الشهر، وهكذا."
    )

def help_cmd(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text(
        "أمثلة:\n"
        "- اشتريت علف دجاج 750\n"
        "- بعت 3 غنم 4200\n"
        "- كم صرفت على العلف هذا الشهر؟\n"
        "- اعرض مصاريف الماء والكهرباء هالسنة\n"
    )

def message_handler(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return

    text = update.message.text or ""
    text = text.strip()
    if not text:
        return

    ai = call_openai_for_intent(text)
    action = ai.get("action")

    user = update.effective_user
    user_name = USER_NAMES.get(user.id, str(user.id)) if user else "unknown"

    if action == "add_transaction":
        tx_type = ai.get("tx_type")
        amount = ai.get("amount")
        category = ai.get("category") or "other"
        item = ai.get("item") or text

        if not tx_type or amount is None:
            update.message.reply_text("ما فهمت المبلغ أو نوع العملية، جرّب تقول:\nاشتريت علف غنم 750")
            return

        try:
            append_transaction_to_sheet(tx_type, category, item, float(amount), user_name)
            kind_ar = "مصروف" if tx_type == "expense" else "دخل"
            update.message.reply_text(
                f"تم تسجيل {kind_ar} ({category}) بمبلغ {float(amount):.2f} ✅"
            )
        except Exception as e:
            log.exception("sheet append failed: %s", e)
            update.message.reply_text("في مشكلة في التخزين في الشيت، جرّب بعد شوي أو بلغ المبرمج.")

    elif action == "get_report":
        time_range = ai.get("time_range") or "all_time"
        cats = ai.get("categories") or []
        report_type = ai.get("report_type") or "summary"

        t_lower = text.lower()
        if any(k in t_lower for k in ["صرف", "مصروف", "مصاريف", "expense"]):
            want_type = "expense"
        elif any(k in t_lower for k in ["دخل", "مبيعات", "بيع", "income"]):
            want_type = "income"
        else:
            want_type = "both"

        try:
            totals = read_report_from_sheet(time_range, cats, want_type)
            msg = summarize_report_text(text, time_range, totals, want_type)
            update.message.reply_text(msg)
        except Exception as e:
            log.exception("sheet report failed: %s", e)
            update.message.reply_text("ما قدرت أطلع التقرير من الشيت، جرّب بعد شوي.")

    elif action == "help":
        ans = ai.get("free_answer") or ""
        if not ans:
            ans = (
                "تقدر تسجّل مصاريف وبيع، وتسأل عن التقارير.\n"
                "أمثلة:\n"
                "اشتريت علف غنم 750\n"
                "بعت 3 غنم 4200\n"
                "كم صرفت اليوم؟"
            )
        update.message.reply_text(ans)

    elif action == "chat":
        ans = ai.get("free_answer") or "تمام، إذا تبي تسجّل مصروف أو تشوف تقرير اكتب لي."
        update.message.reply_text(ans)

    else:
        update.message.reply_text(
            "ما فهمت طلبك، حاول تقول:\n"
            "اشتريت علف غنم 750\n"
            "أو: كم صرفت على العلف هذا الشهر؟"
        )

# ================== DISPATCHER ==================

dispatcher.add_handler(CommandHandler("start", start_cmd))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, message_handler))

# ================== FLASK APP (WEBHOOK SERVER) ==================

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, updater.bot)
        dispatcher.process_update(update)
    except Exception as e:
        log.exception("Error handling update: %s", e)
    return "OK", 200

# ================== MAIN ==================

def main():
    webhook_url = f"{BASE_URL}/{BOT_TOKEN}"
    log.info("Setting Telegram webhook to %s", webhook_url)
    updater.bot.delete_webhook()
    updater.bot.set_webhook(webhook_url)

    log.info("Starting Flask server on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
