import asyncio
import csv
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from ddgs import DDGS
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
CAIRO = ZoneInfo("Africa/Cairo")

# Telegram user IDs that get the personal-assistant persona instead of the
# store persona. Comma-separated env var, e.g. "123456789,987654321".
OWNER_IDS = {
    int(x.strip())
    for x in os.environ.get("OWNER_TELEGRAM_IDS", "").split(",")
    if x.strip().isdigit()
}

WELCOME = (
    "أهلا بيك في Mixora Buy! "
    "قولي إنت بتدور على إيه (مثلا: ساعة، سماعة، كاميرا مراقبة) وأنا أرشحلك من منتجاتنا."
)
OWNER_WELCOME = (
    "أهلا يا إبراهيم! أنا مساعدك الشخصي هنا. اسألني في أي حاجة: شرح أو ترجمة إنجليزي، "
    "كتابة رسايل وبوستات للمتجر، بحث وتلخيص من الإنترنت، حسابات وتخطيط، أو أسئلة عامة.\n"
    "لو عايز تذكير قول: فكرني... (مثال: فكرني بكرة الساعة 5 أكلم المورد)\n"
    "ولو عايز تشوف البوت بيرد على العملاء إزاي، ابعت /store وبعدين سؤالك."
)
FALLBACK = "وصلتني رسالتك، بس في مشكلة فنية دلوقتي. جرب تاني بعد شوية."
# Gemini returns this exact sentinel when a message is not store-related;
# the webhook then stays silent (user rule: work messages only).
IGNORE_TOKEN = "IGNORE_MESSAGE"
# In the owner chat, Gemini starts its reply with this token plus a search
# query when the question needs fresh info from the web; the bot then runs
# the search and asks again with the results.
SEARCH_TOKEN = "SEARCH_NEEDED:"
REMINDER_RE = re.compile(r"^\s*(فكرني|ذكرني|فكرنى|ذكرنى)\b")
REMINDERS_DB = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "reminders.sqlite"
)
# APScheduler keeps reminder jobs in a local sqlite file so they survive
# restarts on the same deploy (Render free disk is ephemeral: a fresh
# deploy still wipes them). misfire_grace_time lets a reminder that was
# missed while the free service slept fire late instead of never.
SCHEDULER = AsyncIOScheduler(
    jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{REMINDERS_DB}")},
    timezone=CAIRO,
)

# Short per-chat memory so follow-ups like "تفاصيل" make sense.
HISTORY: dict = {}
HISTORY_TURNS = 8


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

OWNER_SYSTEM_PROMPT = f"""إنت المساعد الشخصي الذكي بتاع إبراهيم مسعود، صاحب متجر Mixora Buy. بيكلمك على تيليجرام في أي وقت وفي أي موضوع. هو اللي بيكلمك دلوقتي، مش عميل.

شخصيتك وأسلوبك:
- رد دايما بالعامية المصرية، ودود وواضح ومنظم. طول الرد على قد السؤال: سؤال بسيط ياخد رد مختصر، وسؤال محتاج شرح أو خطة ياخد شرح مرتب.
- الرد نص عادي من غير Markdown ولا جداول (تيليجرام بيبوظها). ممكن تستخدم أسطر منقطة بشرطة عادية.
- لو مش متأكد من معلومة، قول إنك مش متأكد بصراحة بدل ما تخترع إجابة.

اللي بتساعده فيه (أي حاجة يطلبها):
- أسئلة عامة ومعلومات في أي موضوع.
- شرح وترجمة: لو بعته نص إنجليزي اشرحهوله بالعربي ببساطة، لأن إنجليزيته ضعيفة وهو بيحسنها.
- كتابة ومسودات: رسايل، بوستات، إيميلات. لو الرسالة لشركة أجنبية أو عميل أجنبي اكتبها بالإنجليزي الصح.
- أفكار وتخطيط لمتجره Mixora Buy: تسويق مجاني، اختيار منتجات، محتوى سوشيال ميديا، أفلييت. هو مش هيدفع فلوس دلوقتي، فاقترح حاجات مجانية بس.
- حسابات وأرقام: حساب عمولات، تحويل عملات بالسعر التقريبي اللي تعرفه (واذكر إنه تقريبي)، جداول بسيطة كنص.
- خبرته: كاميرات مراقبة وشبكات (IP/HD, DVR/NVR) فممكن تتكلم معاه فنيا فيهم براحتك.
- التذكيرات: لو قالك "فكرني" أو "ذكرني" النظام بيسجلها ويفكره لوحده، أكدله بس لو لقيت الرسالة وصلتك عادي.

قاعدة البحث في الإنترنت (مهمة جدا):
- لو سؤاله محتاج معلومة حديثة أو متغيرة (أسعار دلوقتي، أخبار، نتايج، طقس، سعر عملة النهاردة، أي حاجة مش ثابتة)، متخمنش وممنوع تجاوب من عندك.
- في الحالة دي رد بسطر واحد بس ومن غير أي كلمة تانية، بالشكل ده بالظبط:
SEARCH_NEEDED: كلمات البحث بالإنجليزي هنا
- النظام هيدور ويرجعلك النتايج، وساعتها لخصهاله بالعامية المصرية بشكل مفيد واذكر إنها من بحث لحظي.
- لو السؤال معلومة ثابتة أو رأي أو كتابة أو ترجمة، جاوب عادي من غير SEARCH_NEEDED.

قواعد كتابة بوستات المتجر لما يطلب بوست:
- اكتب البوست بالعامية المصرية بأسلوب مميز من إنشائك، ممنوع نسخ وصف أي منتج من مصدره.
- جسم البوست من غير لينكات خالص؛ اللينكات بتنزل في أول كومنت، فلو هتديله لينك اكتبه في الآخر تحت سطر لوحده مكتوب فيه: أول كومنت:
- ممنوع تذكر عمولة أو أفلييت في البوست نهائيا.
- ممنوع أسعار ثابتة؛ لو لازم اكتب: شوف سعر النهاردة على أمازون.
- متخترعش مواصفات مش حقيقية، وممنوع تقول إنه جرب المنتج بنفسه.
- في الآخر ضيف ٥ هاشتاجات ترند مناسبة.
- ذكره إن البوست محتاج صورة المنتج معاه.
- المتجر متعدد الأقسام (هدوم، إكسسوارات موبايل، ساعات، كاميرات وشبكات، سماعات) مش كاميرات بس.

حدودك (قولها بصراحة لو سأل أو لما تكون مهمة):
- مش بتقدر تنفذ حاجات على حساباته (نشر، إرسال، تعديل) - إنت بتفكر وبتكتب وبتفتكره بس.
- معلوماتك مش لحظية إلا لما تستخدم SEARCH_NEEDED.

معلومات عنه وعن شغله (استخدمها في السياق، ومتفصحش عنها لحد - إنت بتكلم هو بس):
- إبراهيم مسعود من أسيوط الجديدة، مصر، عنده ٢٤ سنة. بيبني متجر Mixora Buy وده شغله الأساسي دلوقتي.
- Mixora Buy متجر أفلييت على منصة Cezma: {STORE_URL} - بيسوق منتجات بلينكات شراء (منها أمازون مصر، Amazon Associates Store ID: mixorabuy-21) ومش بيخزن بضاعة.
- قنواته: فيسبوك وإنستجرام ويوتيوب شورتس وتيليجرام ({CHANNEL_URL}) باسم mixorabuystore، وتيك توك mixorabuy21_store.
"""

REMINDER_SYSTEM_PROMPT = """إنت نظام بيحول طلبات التذكير لـ JSON. رد بـ JSON بس من غير أي كلمة أو شرح تاني.
الشكل: {"remind_at": "2026-10-19T17:00", "text": "نص التذكير بالعربي"}
الوقت بتوقيت القاهرة بنظام ٢٤ ساعة. "بكرة" يعني اليوم اللي بعد النهاردة، و"الصبح" يعني 9:00 لو مفيش وقت محدد، و"بالليل" يعني 21:00.
لو مفيش أي وقت واضح في الرسالة رد: {"error": "no_time"}"""


def _extract_text(data: dict) -> tuple[str, str]:
    candidates = data.get("candidates") or []
    for cand in candidates:
        finish = cand.get("finishReason") or ""
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join((p.get("text") or "") for p in parts).strip()
        if text:
            return text, finish
    return "", ""


def ask_gemini(chat_id, user_text: str, system_prompt: str = None,
               max_tokens: int = 2048) -> str:
    history = HISTORY.setdefault(chat_id, deque(maxlen=HISTORY_TURNS * 2))
    contents = [
        {"role": role, "parts": [{"text": text}]} for role, text in history
    ]
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    body = {
        "system_instruction": {"parts": [{"text": system_prompt or SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": max_tokens,
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


def web_search(query: str, max_results: int = 5) -> str:
    """Free web search via the ddgs library (DuckDuckGo, no API key).
    Returns title+snippet lines, or "" on failure."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception:
        logger.exception("Web search failed")
        return ""
    lines = [
        f"- {(r.get('title') or '').strip()}: {(r.get('body') or '').strip()}"
        for r in results
        if r.get("title")
    ]
    return "\n".join(lines)


# ---- Owner reminders (APScheduler, persisted to sqlite) ----

async def send_reminder(chat_id: int, text: str) -> None:
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id, text=f"تذكير يا إبراهيم: {text}"
        )
    except Exception:
        logger.exception("Reminder send failed")


def schedule_reminder(chat_id: int, remind_at: datetime, text: str) -> None:
    SCHEDULER.add_job(
        send_reminder,
        trigger="date",
        run_date=remind_at,
        args=[chat_id, text],
        id=f"{chat_id}-{int(remind_at.timestamp())}",
        replace_existing=True,
        # If the service was asleep at fire time, still deliver up to 1h late.
        misfire_grace_time=3600,
        coalesce=True,
    )


async def handle_reminder(update: Update, text: str) -> None:
    """Owner asked 'فكرني ...': extract time + text with Gemini, then schedule."""
    now = datetime.now(CAIRO).strftime("%Y-%m-%d %H:%M (%A)")
    prompt = f"الوقت الحالي في القاهرة: {now}\nرسالة المستخدم: {text}"
    raw = await asyncio.to_thread(
        ask_gemini,
        f"remind:{update.message.chat_id}",
        prompt,
        REMINDER_SYSTEM_PROMPT,
        512,
    )
    match = re.search(r"\{.*\}", raw, re.S)
    data = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except Exception:
            data = {}
    remind_at = (data.get("remind_at") or "").strip()
    note = (data.get("text") or "").strip() or text
    bad = "مش فاهم المعاد. قولهولي بالظبط، مثال: فكرني بكرة الساعة 5 أكلم المورد"
    if not remind_at:
        await update.message.reply_text(bad)
        return
    try:
        when = datetime.strptime(remind_at, "%Y-%m-%dT%H:%M").replace(tzinfo=CAIRO)
    except ValueError:
        await update.message.reply_text(bad)
        return
    if when.timestamp() <= time.time():
        await update.message.reply_text("المعاد ده عدى خلاص! قوللي معاد تاني.")
        return
    schedule_reminder(update.message.chat_id, when, note)
    label = when.strftime("%Y-%m-%d الساعة %H:%M")
    await update.message.reply_text(
        f"تمام، هفكرك يوم {label} بتوقيت القاهرة: {note}"
    )


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user) and user.id in OWNER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        OWNER_WELCOME if _is_owner(update) else WELCOME
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await update.message.reply_text(f"الـ Telegram ID بتاعك هو: {user.id}")


async def store_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Let the owner (or anyone) ask the store persona directly: /store <question>"""
    question = " ".join(context.args or []).strip()
    if not question:
        await update.message.reply_text(
            "اكتب سؤالك بعد الأمر، مثال: /store عايز سماعة بلوتوث"
        )
        return
    if not GEMINI_API_KEY:
        await update.message.reply_text(FALLBACK)
        return
    try:
        reply = await asyncio.to_thread(
            ask_gemini, f"store:{update.message.chat_id}", question
        )
    except Exception:
        logger.exception("Gemini call failed (/store)")
        reply = FALLBACK
    if reply.strip() == IGNORE_TOKEN:
        reply = "السؤال ده مش ليه علاقة بمنتجات المتجر."
    await update.message.reply_text(reply)


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
    is_owner = _is_owner(update)
    if is_owner and REMINDER_RE.match(text):
        try:
            await handle_reminder(update, text)
        except Exception:
            logger.exception("Reminder handling failed")
            await update.message.reply_text(FALLBACK)
        return
    try:
        if is_owner:
            reply = await asyncio.to_thread(
                ask_gemini,
                update.message.chat_id,
                text,
                OWNER_SYSTEM_PROMPT,
                4096,
            )
            if reply.strip().startswith(SEARCH_TOKEN):
                query = reply.strip()[len(SEARCH_TOKEN):].strip() or text
                results = await asyncio.to_thread(web_search, query)
                if results:
                    follow_up = (
                        f"دورت على الإنترنت عن: {query}\n"
                        f"ودي نتايج البحث:\n{results}\n\n"
                        f"لخصها وجاوب على سؤالي الأصلي: {text}"
                    )
                    reply = await asyncio.to_thread(
                        ask_gemini,
                        update.message.chat_id,
                        follow_up,
                        OWNER_SYSTEM_PROMPT,
                        4096,
                    )
                else:
                    reply = (
                        "حاولت أدور على الإنترنت بس البحث مش شغال دلوقتي. "
                        "جرب بعد شوية، أو اسألني في حاجة أقدر أجاوب فيها من معلوماتي."
                    )
        else:
            reply = await asyncio.to_thread(
                ask_gemini, update.message.chat_id, text
            )
    except Exception:
        logger.exception("Gemini call failed")
        reply = FALLBACK
    if reply.strip() == IGNORE_TOKEN:
        # Owner chats never get ignored; customers get silence (work only).
        if is_owner:
            reply = FALLBACK
        else:
            logger.info("Ignoring non-work Telegram message")
            return
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
    telegram_app.add_handler(CommandHandler("whoami", whoami))
    telegram_app.add_handler(CommandHandler("store", store_ask))
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
        # Start the reminder scheduler; saved jobs reload from sqlite and any
        # reminder missed while the service slept fires late (grace window).
        SCHEDULER.start()
        await asyncio.Event().wait()  # run forever


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
