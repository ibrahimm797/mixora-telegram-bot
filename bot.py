import asyncio
import logging
import os

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

WELCOME = (
    "أهلا بيك في Mixora Buy! "
    "ابعتلي اسم أي منتج أو سؤالك وأنا أرد عليك."
)
FALLBACK = "وصلتني رسالتك، هرد عليك في أقرب وقت."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    if N8N_WEBHOOK_URL:
        try:
            resp = requests.post(
                N8N_WEBHOOK_URL,
                json={
                    "chat_id": update.message.chat_id,
                    "from": (update.message.from_user.username
                             if update.message.from_user else ""),
                    "text": text,
                },
                timeout=20,
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            reply = data.get("reply") or FALLBACK
        except Exception:
            logger.exception("n8n webhook call failed")
            reply = FALLBACK
    else:
        reply = FALLBACK
    await update.message.reply_text(reply)


def main() -> None:
    # Python 3.14 no longer creates an implicit event loop; set one explicitly.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    url_path = f"telegram/{TOKEN}"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=f"{BASE_URL}/{url_path}" if BASE_URL else None,
    )


if __name__ == "__main__":
    main()
