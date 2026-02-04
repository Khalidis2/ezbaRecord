# telegram_bot.py
import os
import logging

from flask import Flask, request
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# ================== CONFIG ==================

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"]  # e.g. "https://ezbarecord.onrender.com"
PORT = int(os.environ.get("PORT", "8000"))

ALLOWED_USERS = {47329648, 6894180427}
USER_NAMES = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ================== TELEGRAM SETUP ==================

updater = Updater(BOT_TOKEN, use_context=True)
dispatcher = updater.dispatcher

def is_allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)

# ---- Handlers (put your real logic here) ----

def start_cmd(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text("✅ ezba bot online")

def help_cmd(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text(
        "/help /status /balance /confirm /cancel /undo\n"
        "اكتب المصاريف بالطريقة العادية، والبوت يسجلها 🌿"
    )

def echo_handler(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    text = update.message.text or ""
    # If you add Arabic number normalization later, do it here:
    # from arabic_number_parser import normalize_arabic_numbers
    # text = normalize_arabic_numbers(text)
    update.message.reply_text(f"استلمت: {text}")

dispatcher.add_handler(CommandHandler("start", start_cmd))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, echo_handler))

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
    # Set webhook with correct public URL (NO 0.0.0.0 ANYWHERE)
    webhook_url = f"{BASE_URL}/{BOT_TOKEN}"
    log.info("Setting Telegram webhook to %s", webhook_url)
    updater.bot.delete_webhook()
    updater.bot.set_webhook(webhook_url)

    log.info("Starting Flask server on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
