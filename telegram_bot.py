# telegram_bot.py
import os
import json
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ["BASE_URL"]
PORT = int(os.environ.get("PORT", "8443"))

ALLOWED_USERS = {47329648, 6894180427}
USER_NAMES = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

def is_allowed(update: Update) -> bool:
    user = update.effective_user
    return user and user.id in ALLOWED_USERS

def start(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    update.message.reply_text("✅ Bot is online")

def handle_message(update: Update, context: CallbackContext):
    if not is_allowed(update):
        return
    text = update.message.text
    update.message.reply_text(f"Received: {text}")

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
    )

    updater.bot.set_webhook(f"{BASE_URL}/{BOT_TOKEN}")
    updater.idle()

if __name__ == "__main__":
    main()
