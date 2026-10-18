import asyncio
import csv
import json
import logging
import os
import re
import time
from collections import deque

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from tornado.web import Application as TornadoApp, RequestHandler

logging.basicConfig(level=logging.INFO)
# Do not log request URLs: they contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "GEMINI_MODELS", "gemini-3.6-flash,gemini-3.5-flash"
    ).split(",")
    if m.strip()
]
# gemini-3.8-flash hangs (no bytes until timeout) as of 2026-10-18; keep it
# out of the default list. Override anytime with the GEMINI_MODELS env var.
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "25"))
STORE_URL = "https://cezma.com/store/mixorabuy"
CHANNEL_URL = "https://t.me/mixorabuystore"
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v22.0")

WELCOME = (
    "أهلا بيك في Mixora Buy! "
    "قولي إنت بتدور على إيه (مثلا: ساعة، سماعة، كاميرا مراقبة) وأنا أرشحلك من منتجاتنا."
)
FALLBACK = "وصلتني رسالتك، بس في مشكلة فنية دلوقتي. جرب تاني بعد شوية."
# Gemini returns this exact sentinel when a message is not store-related;
# the webhook then stays silent (user rule: work messages only).
IGNORE_TOKEN = "IGNORE_MESSAGE"

# Short per-chat memory so follow-ups like "تفاصيل" make sense.
HISTORY: dict = {}
HISTORY_TURNS = 4


def load_catalog() -> str:
    lines = []
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.csv")
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            name = (row.get("product_name") or "").strip()
            if not name:
                continue
            cat = (row.get("category_inferred") or "").strip()
            cezma = (row.get("cezma_product_url") or "").strip()
            amz = (row.get("amazon_link") or "").strip()
            line = f"- {name} | القسم: {cat} | لينك المتجر: {cezma}"
            if amz:
                line += f" | لينك أمازون: {amz}"
            lines.append(line)
    catalog = "\n".join(lines)
    logger.info("Loaded catalog: %d products", len(lines))
    return catalog


CATALOG = load_catalog()

SYSTEM_PROMPT = f"""إنت مساعد المبيعات بتاع متجر Mixora Buy المصري، وبترد على الناس على تيليجرام وواتساب.

قواعد صارمة:
- رد دايما بالعامية المصرية، بشكل ودود ومختصر (٣-٥ سطور بالكتير).
- رشح منتجات من الكتالوج اللي تحت بس. ممنوع منعا باتا تخترع منتجات أو مواصفات أو أسعار مش موجودة.
- الأسعار مش موجودة عندك، فقول للعميل إن السعر على صفحة المنتج في المتجر.
- لما ترشح منتج، اكتب اسمه ولينكه من الكتالوج زي ما هو بالظبط، على سطر لوحده.
- لو المنتج له لينك أمازون في الكتالوج، ممكن تذكره كاختيار تاني.
- لو مفيش حاجة في الكتالوج تناسب طلبه، قوله بصراحة ووجهه للمتجر كله: {STORE_URL}
- لينك المتجر العام: {STORE_URL}
- الرد نص عادي من غير Markdown ولا جداول ولا إيموجيز كتير.
- قاعدة صارمة جدا: الرد على رسايل الشغل بس (منتجات المتجر، أقسامه، الطلبات، لينكات الشراء، أسئلة عن Mixora Buy).
- أي رسالة مش ليها علاقة بالمتجر أو التسوق (كلام شخصي، تحية عامة من غير سؤال عن منتج، أي موضوع تاني): متردش عليها خالص واكتب الكلمة دي بس من غير أي حاجة تانية: IGNORE_MESSAGE

معلومات ثابتة عن المتجر (استخدمها لما حد يسأل عن المتجر نفسه، ومتخرجش عنها):
- Mixora Buy متجر إلكتروني مصري متعدد الأقسام على منصة Cezma: {STORE_URL}
- قناة تيليجرام بتاعتنا: {CHANNEL_URL}
- بنعرض منتجات بلينكات شراء، منها لينكات أمازون مصر، واحنا مش بنخزن بضاعة عندنا.
- الشراء بيتم من خلال لينك المنتج الموجود في المتجر أو لينك أمازون.
- أي سؤال عن الشحن أو الدفع أو الاستبدال أو أي تفاصيل مش موجودة هنا: متخترعش إجابة، ووجه العميل لصفحة المتجر {STORE_URL} أو القناة {CHANNEL_URL}.

كتالوج المنتجات:
{CATALOG}
"""


def _extract_text(data: dict) -> tuple[str, str]:
    candidates = data.get("candidates") or []
    for cand in candidates:
        finish = cand.get("finishReason") or ""
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join((p.get("text") or "") for p in parts).strip()
        if text:
            return text, finish
    return "", ""


def ask_gemini(chat_id: int, user_text: str) -> str:
    history = HISTORY.setdefault(chat_id, deque(maxlen=HISTORY_TURNS * 2))
    contents = [
        {"role": role, "parts": [{"text": text}]} for role, text in history
    ]
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2048,
            # Thinking tokens count against maxOutputTokens and truncate
            # the visible reply; disable thinking for short sales replies.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in (1, 2):
            try:
                resp = requests.post(
                    url,
                    headers={
                        "x-goog-api-key": GEMINI_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=GEMINI_TIMEOUT,
                )
            except Exception:
                logger.exception("Gemini request failed (model=%s)", model)
                break
            if resp.status_code == 200:
                text, finish = _extract_text(resp.json())
                if text:
                    if finish and finish != "STOP":
                        logger.warning(
                            "Gemini finish=%s (model=%s)", finish, model
                        )
                    history.append(("user", user_text))
                    history.append(("model", text))
                    return text
                logger.warning("Gemini empty response (model=%s)", model)
                break
            logger.warning(
                "Gemini error %s (model=%s): %s",
                resp.status_code,
                model,
                resp.text[:300],
            )
            if resp.status_code in (429, 500, 503) and attempt == 1:
                time.sleep(2)
                continue
            break

    return FALLBACK


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        await context.bot.send_chat_action(
            chat_id=update.message.chat_id, action="typing"
        )
    except Exception:
        pass
    if not GEMINI_API_KEY:
        await update.message.reply_text(FALLBACK)
        return
    try:
        reply = await asyncio.to_thread(ask_gemini, update.message.chat_id, text)
    except Exception:
        logger.exception("Gemini call failed")
        reply = FALLBACK
    await update.message.reply_text(reply)




def whatsapp_send(wa_id: str, text: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": wa_id,
            "type": "text",
            "text": {"body": text},
        },
        timeout=20,
    )
    if resp.status_code != 200:
        logger.warning("WhatsApp send failed %s: %s", resp.status_code, resp.text[:300])


def whatsapp_mark_read(message_id: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"
    try:
        requests.post(
            url,
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            },
            timeout=10,
        )
    except Exception:
        pass


def handle_whatsapp_payload(data: dict) -> None:
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                if msg.get("type") != "text":
                    continue
                wa_id = (msg.get("from") or "").strip()
                text = ((msg.get("text") or {}).get("body") or "").strip()
                if not wa_id or not text:
                    continue
                try:
                    whatsapp_mark_read(msg.get("id") or "")
                except Exception:
                    pass
                try:
                    reply = ask_gemini(f"wa:{wa_id}", text)
                except Exception:
                    logger.exception("Gemini call failed (whatsapp)")
                    reply = FALLBACK
                if reply.strip() == IGNORE_TOKEN:
                    logger.info("Ignoring non-work WhatsApp message from %s", wa_id)
                    continue
                try:
                    whatsapp_send(wa_id, reply)
                except Exception:
                    logger.exception("WhatsApp send failed")


class WhatsAppWebhookHandler(RequestHandler):
    def get(self) -> None:
        mode = self.get_query_argument("hub.mode", "")
        token = self.get_query_argument("hub.verify_token", "")
        challenge = self.get_query_argument("hub.challenge", "")
        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN and challenge:
            self.write(challenge)
        else:
            self.set_status(403)
        self.finish()

    async def post(self) -> None:
        self.set_status(200)
        self.finish()
        try:
            data = json.loads(self.request.body.decode("utf-8"))
        except Exception:
            return
        # Process in the background; Meta retries on timeouts, so ACK first.
        asyncio.get_event_loop().run_in_executor(None, handle_whatsapp_payload, data)

# ---- Web server: health endpoint + Telegram + WhatsApp webhooks ----

telegram_app = Application.builder().token(TOKEN).updater(None).build()


class HealthHandler(RequestHandler):
    def get(self) -> None:
        # Plain 200 so free uptime pings (and Render) see the bot as up.
        self.write("Mixora Buy bot is running")


class WebhookHandler(RequestHandler):
    async def post(self) -> None:
        try:
            data = json.loads(self.request.body.decode("utf-8"))
        except Exception:
            self.set_status(400)
            self.finish()
            return
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.update_queue.put(update)
        self.set_status(200)
        self.finish()


async def run() -> None:
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if BASE_URL:
        await telegram_app.bot.set_webhook(
            url=f"{BASE_URL}/telegram/{TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
        logger.info("Webhook registered")

    routes = [
        (r"/", HealthHandler),
        (rf"/telegram/{re.escape(TOKEN)}", WebhookHandler),
    ]
    if WHATSAPP_TOKEN and WHATSAPP_PHONE_ID and WHATSAPP_VERIFY_TOKEN:
        routes.append((r"/whatsapp/webhook", WhatsAppWebhookHandler))
        logger.info("WhatsApp webhook enabled at /whatsapp/webhook")
    tornado_app = TornadoApp(routes)
    tornado_app.listen(PORT, address="0.0.0.0")

    async with telegram_app:
        await telegram_app.start()
        logger.info("Bot is up on port %s", PORT)
        await asyncio.Event().wait()  # run forever


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

