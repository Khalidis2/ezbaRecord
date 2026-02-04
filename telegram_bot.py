# telegram_bot.py
import os
import json
import logging
from threading import Thread

from flask import Flask
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

# ---------- Configuration ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"]  # e.g. "https://your-service.onrender.com"
# Render provides PORT for health endpoint; keep it as-is.
FLASK_PORT = int(os.environ.get("PORT", "8080"))
# Telegram requires webhook listen port to be one of: 80, 88, 443, 8443
WEBHOOK_LISTEN_PORT = 8443
# Allowed users mapping (keep/change as you need)
ALLOWED_USERS = {47329648, 6894180427}
USER_NAMES = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- Minimal Flask health app (keeps Render happy) ----------
app = Flask(__name__)

@app.route("/")
def health():
    return "OK", 200

def run_flask():
    # Bind Flask to the PORT Render provides so Render's health checks pass.
    app.run(host="0.0.0.0", port=FLASK_PORT)

# ---------- Bot handlers ----------
def is_allowed(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USERS)

def start_cmd(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text("✅ Bot is online")

def help_cmd(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text("/help, /status, /balance, /confirm, /cancel, /undo")

def echo_handler(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    text = update.message.text or ""
    # If you normalize Arabic numbers, call your function here, e.g.:
    # from arabic_number_parser import normalize_arabic_numbers
    # text = normalize_arabic_numbers(text)
    update.message.reply_text(f"Received: {text}")

# ---------- Main ----------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Register handlers (your real handlers should replace/augment these)
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, echo_handler))

    # Start Flask in background so Render sees an open port
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info("Flask health server started on port %s", FLASK_PORT)

    # Start Telegram webhook listener on Telegram-allowed port (8443)
    # Use url_path=BOT_TOKEN so Telegram posts to https://BASE_URL/<BOT_TOKEN>
    log.info("Starting Telegram webhook on port %s", WEBHOOK_LISTEN_PORT)
    updater.start_webhook(
        listen="0.0.0.0",
        port=WEBHOOK_LISTEN_PORT,
        url_path=BOT_TOKEN,
    )

    # Tell Telegram which URL to use
    webhook_url = f"{BASE_URL}/{BOT_TOKEN}"
    log.info("Setting webhook to %s", webhook_url)
    try:
        updater.bot.set_webhook(webhook_url)
    except Exception as e:
        log.exception("Failed to set webhook: %s", e)
        # If webhook fails the process should still run so you can inspect logs.
    updater.idle()

if __name__ == "__main__":
    main()
