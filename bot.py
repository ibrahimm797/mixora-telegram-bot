import asyncio
import csv
import logging
import os
import time
from collections import deque

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
        "GEMINI_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash"
    ).split(",")
    if m.strip()
]
STORE_URL = "https://cezma.com/store/mixorabuy"

WELCOME = (
    "أهلا بيك في Mixora Buy! "
    "قولي إنت بتدور على إيه (مثلا: ساعة، سماعة، كاميرا مراقبة) وأنا أرشحلك من منتجاتنا."
)
FALLBACK = "وصلتني رسالتك، بس في مشكلة فنية دلوقتي. جرب تاني بعد شوية."

# Short per-chat memory so follow-ups like "تفاصيل" make sense.
HISTORY: dict[int, deque] = {}
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

SYSTEM_PROMPT = f"""إنت مساعد المبيعات بتاع متجر Mixora Buy المصري، وبترد على الناس على تيليجرام.

قواعد صارمة:
- رد دايما بالعامية المصرية، بشكل ودود ومختصر (٣-٥ سطور بالكتير).
- رشح منتجات من الكتالوج اللي تحت بس. ممنوع منعا باتا تخترع منتجات أو مواصفات أو أسعار مش موجودة.
- الأسعار مش موجودة عندك، فقول للعميل إن السعر على صفحة المنتج في المتجر.
- لما ترشح منتج، اكتب اسمه ولينكه من الكتالوج زي ما هو بالظبط، على سطر لوحده.
- لو المنتج له لينك أمازون في الكتالوج، ممكن تذكره كاختيار تاني.
- لو مفيش حاجة في الكتالوج تناسب طلبه، قوله بصراحة ووجهه للمتجر كله: {STORE_URL}
- لينك المتجر العام: {STORE_URL}
- الرد نص عادي من غير Markdown ولا جداول ولا إيموجيز كتير.
- متردش على أسئلة مش ليها علاقة بالتسوق غير بجملة قصيرة بترجعه للموضوع.

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
                    timeout=40,
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
    if not GEMINI_API_KEY:
        await update.message.reply_text(FALLBACK)
        return
    try:
        reply = await asyncio.to_thread(ask_gemini, update.message.chat_id, text)
    except Exception:
        logger.exception("Gemini call failed")
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
