import asyncio
import base64
import csv
import io
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# Optional owner-mode skills (all free, no API keys). Guarded so a missing
# package can never take down the store bot.
try:
    import trafilatura
except ImportError:
    trafilatura = None
try:
    import feedparser
except ImportError:
    feedparser = None
try:
    import edge_tts
except ImportError:
    edge_tts = None
try:
    from PIL import Image, ImageDraw, ImageFont, features as pil_features
except ImportError:
    Image = ImageDraw = ImageFont = pil_features = None
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    arabic_reshaper = get_display = None

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
    "- تذكير: قول فكرني... (مثال: فكرني بكرة الساعة 5 أكلم المورد) و /reminders لعرضهم\n"
    "- تريندات مصر: ابعت /trends\n"
    "- ملخص يومك: /briefing (وبيوصلك لوحده كل يوم 9 الصبح)\n"
    "- مهام وملاحظات وفلوس: /todo و /note و /spent و /earned و /money\n"
    "- ذاكرة: قول افتكر... أو /remember عشان أفكر تفضيلاتك\n"
    "- ابعتلي لينك ألخصه، أو فويس نوت أسمعه وأرد عليك\n"
    "- ردود صوتية: ابعت /voice، أو ابدأ رسالتك بكلمة صوت\n"
    "- صورة بنص عربي: ابعت أي صورة منتج واكتب النص في الكابشن\n"
    "- تجربة ردود العملاء: ابعت /store وبعدين سؤالك"
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
VOICE_PREFIX_RE = re.compile(r"^\s*صوت\s+")
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=EG"
FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Cairo.ttf")
# Cairo (OFL, Google Fonts) is downloaded once at startup instead of being
# committed to the repo; the disk is ephemeral, so it re-downloads per deploy.
FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/"
    "Cairo%5Bslnt%2Cwght%5D.ttf"
)
TTS_VOICE = "ar-EG-ShakirNeural"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "botdata.json")
# Owner chat ids that currently get voice replies too (toggled with /voice).
VOICE_ON: set = set()
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


def ensure_font() -> None:
    if os.path.exists(FONT_PATH):
        return
    try:
        resp = requests.get(FONT_URL, timeout=30)
        if resp.status_code == 200 and len(resp.content) > 10000:
            with open(FONT_PATH, "wb") as f:
                f.write(resp.content)
            logger.info("Cairo font downloaded (%d bytes)", len(resp.content))
        else:
            logger.warning("Font download failed: HTTP %s", resp.status_code)
    except Exception:
        logger.exception("Font download failed")


ensure_font()


def load_owner_knowledge() -> str:
    """Private owner knowledge file, delivered as a Render Secret File so it
    never enters the public repo (root copy) - /etc/secrets path first."""
    candidates = [
        "/etc/secrets/owner_knowledge.md",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "owner_knowledge.md"
        ),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            continue
        if text:
            logger.info("Owner knowledge loaded (%d chars)", len(text))
            return text
    logger.info("No owner knowledge file found")
    return ""


OWNER_KNOWLEDGE = load_owner_knowledge()


def _load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_data(data: dict) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        logger.exception("Could not save bot data")

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

if OWNER_KNOWLEDGE:
    OWNER_SYSTEM_PROMPT += (
        "\n\nملف المعرفة الخاص بشغل إبراهيم (معلومات محدثة عن المتجر والقنوات "
        "والقواعد - التزم بيها دايما في إجاباتك وفي أي بوست أو خطة):\n"
        + OWNER_KNOWLEDGE
        + "\n"
    )


def owner_prompt() -> str:
    """Owner system prompt + facts he asked the bot to remember."""
    mem = _load_data().get("memory", [])
    if not mem:
        return OWNER_SYSTEM_PROMPT
    return (
        OWNER_SYSTEM_PROMPT
        + "\n\nحاجات إبراهيم طلب منك تفتكرها عن نفسه وتفضيلاته:\n"
        + "\n".join(f"- {m}" for m in mem[-50:])
        + "\n"
    )

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


def web_search(query: str, max_results: int = 5) -> list:
    """Free web search via the ddgs library (DuckDuckGo, no API key).
    Returns a list of {title, body, href} dicts, or [] on failure."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
    except Exception:
        logger.exception("Web search failed")
        return []
    return [r for r in results if r.get("title")]


def fetch_pages_text(urls: list, max_chars: int = 2500) -> str:
    """Clean article text from result pages via trafilatura (skill: better summaries)."""
    if not trafilatura:
        return ""
    chunks = []
    for url in urls:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                    )
                },
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            text = trafilatura.extract(resp.text)
            if text:
                chunks.append(text[:max_chars])
        except Exception:
            continue
    return "\n\n".join(chunks)


def _fetch_url(url: str):
    return requests.get(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
        timeout=15,
    )


def ask_gemini_audio(chat_id, audio_bytes: bytes) -> str:
    """Transcribe + answer an owner voice note via Gemini inline audio."""
    body = {
        "system_instruction": {"parts": [{"text": owner_prompt()}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "audio/ogg",
                            "data": base64.b64encode(audio_bytes).decode(),
                        }
                    },
                    {
                        "text": (
                            "ده فويس نوت من إبراهيم. اسمعه ورد عليه بالعامية المصرية: "
                            "لو فيه سؤال جاوب عليه، لو طلب نفذه في حدود إمكانياتك، "
                            "ولو كلام عادي رد بشكل طبيعي. ابدأ ردك بسطر قصير: سمعتك بتقول: ..."
                        )
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            resp = requests.post(
                url,
                headers={
                    "x-goog-api-key": GEMINI_API_KEY,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=GEMINI_TIMEOUT * 2,
            )
        except Exception:
            logger.exception("Gemini audio request failed (model=%s)", model)
            continue
        if resp.status_code == 200:
            text, _ = _extract_text(resp.json())
            if text:
                return text
        else:
            logger.warning(
                "Gemini audio error %s (model=%s): %s",
                resp.status_code, model, resp.text[:200],
            )
    return FALLBACK


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


async def trends(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: Egypt Google Trends RSS -> Gemini summary with a store angle."""
    if not _is_owner(update):
        return
    if not feedparser or not GEMINI_API_KEY:
        await update.message.reply_text(FALLBACK)
        return
    feed = await asyncio.to_thread(feedparser.parse, TRENDS_RSS)
    titles = [e.title for e in (feed.entries or [])[:10]]
    if not titles:
        await update.message.reply_text(
            "مش قادر أجيب الترندات دلوقتي. جرب بعد شوية."
        )
        return
    prompt = (
        "دي أحدث ترندات البحث على جوجل في مصر دلوقتي:\n"
        + "\n".join(f"- {t}" for t in titles)
        + "\n\nلخصهم لي في نقاط مختصرة بالعامية المصرية، وفي الآخر قولي: "
        "أنهي ترند ممكن أستغله في محتوى أو منتج لمتجري Mixora Buy، وليه."
    )
    try:
        reply = await asyncio.to_thread(
            ask_gemini,
            f"trends:{update.message.chat_id}",
            prompt,
            owner_prompt(),
            2048,
        )
    except Exception:
        logger.exception("Gemini call failed (/trends)")
        reply = FALLBACK
    await update.message.reply_text(reply)


# ---- Owner personal-assistant features (todos, notes, money, memory, briefing) ----

def _fmt_entry(e: dict) -> str:
    return f"{e.get('text', '')} ({e.get('ts', '')[:16]})"


async def todo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /todo -> list, /todo <text> -> add, /todo done N -> complete."""
    if not _is_owner(update):
        return
    data = _load_data()
    todos = data.setdefault("todos", [])
    args = context.args or []
    if args and args[0] == "done" and len(args) > 1 and args[1].isdigit():
        n = int(args[1]) - 1
        open_todos = [t for t in todos if not t.get("done")]
        if 0 <= n < len(open_todos):
            open_todos[n]["done"] = True
            _save_data(data)
            await update.message.reply_text(f"تمام، خلصت: {open_todos[n]['text']}")
        else:
            await update.message.reply_text("مفيش مهمة بالرقم ده.")
        return
    if not args:
        open_todos = [t for t in todos if not t.get("done")]
        if not open_todos:
            await update.message.reply_text("مفيش مهام مسجلة. ابعت /todo وبعدين المهمة عشان تضيفها.")
            return
        lines = [f"{i+1}. {t['text']}" for i, t in enumerate(open_todos)]
        await update.message.reply_text("مهامك:\n" + "\n".join(lines) + "\n\nلما تخلص واحدة ابعت: /todo done ورقمها")
        return
    text = " ".join(args).strip()
    todos.append({"text": text, "done": False,
                  "ts": datetime.now(CAIRO).isoformat()})
    _save_data(data)
    await update.message.reply_text(f"سجلتها: {text}")


async def note_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /note -> list notes, /note <text> -> save a note."""
    if not _is_owner(update):
        return
    data = _load_data()
    notes = data.setdefault("notes", [])
    args = context.args or []
    if not args:
        if not notes:
            await update.message.reply_text("مفيش ملاحظات. ابعت /note وبعدين الكلام عشان تحفظه.")
            return
        lines = [f"- {_fmt_entry(n)}" for n in notes[-15:]]
        await update.message.reply_text("ملاحظاتك:\n" + "\n".join(lines))
        return
    text = " ".join(args).strip()
    notes.append({"text": text, "ts": datetime.now(CAIRO).isoformat()})
    _save_data(data)
    await update.message.reply_text("حفظتها.")


def _money_summary(entries: list) -> tuple[float, float]:
    spent = sum(e.get("amount", 0) for e in entries if e.get("kind") == "spent")
    earned = sum(e.get("amount", 0) for e in entries if e.get("kind") == "earned")
    return spent, earned


async def money_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /spent N وصف, /earned N وصف, /money -> summary."""
    if not _is_owner(update):
        return
    data = _load_data()
    entries = data.setdefault("money", [])
    cmd = update.message.text.split()[0].lstrip("/").lower()
    args = context.args or []
    if cmd == "money":
        spent, earned = _money_summary(entries)
        recent = "\n".join(
            f"- {'صرف' if e['kind']=='spent' else 'دخل'}: {e['amount']:g} جنيه - {e.get('text','')} ({e.get('ts','')[:10]})"
            for e in entries[-10:]
        ) or "لسه مفيش حاجة مسجلة."
        await update.message.reply_text(
            f"إجمالي الدخل: {earned:g} جنيه\nإجمالي المصروف: {spent:g} جنيه\nالصافي: {earned-spent:g} جنيه\n\nآخر الحركات:\n{recent}"
        )
        return
    if len(args) < 1 or not args[0].replace(".", "", 1).isdigit():
        await update.message.reply_text(f"اكتبها كده: /{cmd} المبلغ الوصف - مثال: /{cmd} 50 مواصلات")
        return
    amount = float(args[0])
    text = " ".join(args[1:]).strip()
    entries.append({"kind": cmd, "amount": amount, "text": text,
                    "ts": datetime.now(CAIRO).isoformat()})
    _save_data(data)
    spent, earned = _money_summary(entries)
    label = "الصرف" if cmd == "spent" else "الدخل"
    await update.message.reply_text(
        f"سجلت {label}: {amount:g} جنيه {text}\nالصافي دلوقتي: {earned-spent:g} جنيه"
    )


async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /remember <fact> -> bot keeps it in every future reply."""
    if not _is_owner(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("اكتب اللي عايزني أفتكره بعد الأمر. مثال: /remember بحب الردود المختصرة")
        return
    data = _load_data()
    mem = data.setdefault("memory", [])
    mem.append(text)
    _save_data(data)
    await update.message.reply_text(f"تمام، هفتكر دايما: {text}")


def build_briefing(chat_id: int) -> str:
    data = _load_data()
    todos = [t for t in data.get("todos", []) if not t.get("done")]
    spent, earned = _money_summary(data.get("money", []))
    today = datetime.now(CAIRO).strftime("%Y-%m-%d")
    jobs = sorted(SCHEDULER.get_jobs(), key=lambda j: j.next_run_time)
    todays = [
        j for j in jobs
        if j.id and j.id.startswith(f"{chat_id}-")
        and j.next_run_time.astimezone(CAIRO).strftime("%Y-%m-%d") == today
    ]
    lines = [f"صباح الخير يا إبراهيم! ملخص يومك ({today}):"]
    lines.append(f"- مهام مفتوحة: {len(todos)}")
    for t in todos[:5]:
        lines.append(f"  . {t['text']}")
    if todays:
        lines.append("- تذكيرات النهاردة:")
        for j in todays:
            when = j.next_run_time.astimezone(CAIRO).strftime("%H:%M")
            note = j.args[1] if len(j.args) > 1 else ""
            lines.append(f"  . {when} - {note}")
    else:
        lines.append("- مفيش تذكيرات النهاردة.")
    lines.append(f"- الصافي المسجل: {earned-spent:g} جنيه")
    lines.append("ابعت /trends لو عايز ترندات النهاردة.")
    return "\n".join(lines)


async def briefing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(build_briefing(update.message.chat_id))


async def send_daily_briefing(chat_id: int) -> None:
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id, text=build_briefing(chat_id)
        )
    except Exception:
        logger.exception("Daily briefing send failed")


URL_RE = re.compile(r"https?://\S+")
LINK_INTENT_RE = re.compile(r"^\s*(لخص|اشرح|ترجم|ايه|ملخص)")


async def summarize_link(update: Update, url: str, question: str) -> None:
    """Owner sent a link: fetch clean text (trafilatura) and summarize."""
    if not trafilatura:
        await update.message.reply_text(FALLBACK)
        return
    try:
        resp = await asyncio.to_thread(_fetch_url, url)
        page_text = trafilatura.extract(resp.text) if resp and resp.status_code == 200 else ""
    except Exception:
        page_text = ""
    if not page_text:
        await update.message.reply_text("مش قادر أفتح اللينك ده دلوقتي. جرب لينك تاني أو ابعتلي النص نفسه.")
        return
    prompt = (
        f"إبراهيم بعت اللينك ده: {url}\n"
        + (f"وسؤاله: {question}\n" if question else "")
        + f"وده محتوى الصفحة:\n{page_text[:12000]}\n\n"
        "لخصهاله بالعامية المصرية في نقاط واضحة" + ("، وجاوب على سؤاله." if question else ".")
    )
    try:
        reply = await asyncio.to_thread(
            ask_gemini, update.message.chat_id, prompt, owner_prompt(), 4096
        )
    except Exception:
        reply = FALLBACK
    await update.message.reply_text(reply)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner voice notes: transcribe + answer via Gemini audio (no new deps)."""
    if not _is_owner(update):
        return
    if not GEMINI_API_KEY:
        await update.message.reply_text(FALLBACK)
        return
    try:
        await context.bot.send_chat_action(chat_id=update.message.chat_id, action="typing")
    except Exception:
        pass
    try:
        media = update.message.voice or update.message.audio
        file = await context.bot.get_file(media.file_id)
        data = await file.download_as_bytearray()
        reply = await asyncio.to_thread(
            ask_gemini_audio, update.message.chat_id, bytes(data)
        )
    except Exception:
        logger.exception("Voice handling failed")
        reply = FALLBACK
    await update.message.reply_text(reply)
    if update.message.chat_id in VOICE_ON:
        await send_voice_reply(update.message, reply)


async def reminders_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: show pending reminders."""
    if not _is_owner(update):
        return
    chat_id = update.message.chat_id
    jobs = sorted(SCHEDULER.get_jobs(), key=lambda j: j.next_run_time)
    mine = [j for j in jobs if j.id and j.id.startswith(f"{chat_id}-")]
    if not mine:
        await update.message.reply_text(
            "مفيش تذكيرات مسجلة. قول: فكرني... وحدد المعاد."
        )
        return
    lines = []
    for j in mine:
        when = j.next_run_time.astimezone(CAIRO).strftime("%Y-%m-%d الساعة %H:%M")
        note = j.args[1] if len(j.args) > 1 else ""
        lines.append(f"- {when}: {note}")
    await update.message.reply_text("التذكيرات الجاية:\n" + "\n".join(lines))


async def voice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: toggle voice replies on/off."""
    if not _is_owner(update):
        return
    chat_id = update.message.chat_id
    if chat_id in VOICE_ON:
        VOICE_ON.discard(chat_id)
        await update.message.reply_text("تمام، الردود الصوتية اتقفلت.")
    else:
        VOICE_ON.add(chat_id)
        await update.message.reply_text(
            "تمام، هرد عليك صوت وكتابة. ابعت /voice تاني لو عايز تقفلها."
        )


async def send_voice_reply(message, text: str) -> bool:
    """Generate an Egyptian Arabic voice note with edge-tts and send it."""
    if not edge_tts:
        return False
    path = f"/tmp/tts-{uuid.uuid4().hex}.mp3"
    try:
        await edge_tts.Communicate(text[:2000], voice=TTS_VOICE).save(path)
        with open(path, "rb") as f:
            await message.reply_voice(voice=f)
        return True
    except Exception:
        logger.exception("TTS failed")
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _shape_arabic(text: str, use_raqm: bool) -> str:
    """Reshape+bidi for legacy renderers; RAQM does this itself."""
    if use_raqm:
        return text
    if arabic_reshaper and get_display:
        return get_display(arabic_reshaper.reshape(text))
    return text


def make_caption_image(image_bytes: bytes, caption: str) -> bytes:
    """Draw an Arabic caption banner on a product photo (Pillow + Cairo font)."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    font_size = max(28, w // 18)
    font = ImageFont.truetype(FONT_PATH, font_size)
    try:
        font.set_variation_by_axes([700])
    except Exception:
        pass
    use_raqm = bool(pil_features and pil_features.check("raqm"))
    direction = "rtl" if use_raqm else None
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    def measure(t: str) -> float:
        return d.textlength(_shape_arabic(t, use_raqm), font=font, direction=direction)

    words = caption.split()
    lines, cur = [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if measure(trial) <= w * 0.9:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if not lines:
        lines = [caption]

    line_h = int(font_size * 1.5)
    band_h = line_h * len(lines) + int(font_size * 0.8)
    d.rectangle([0, h - band_h, w, h], fill=(0, 0, 0, 160))
    y = h - band_h + int(font_size * 0.4)
    for line in lines:
        d.text(
            (w / 2, y),
            _shape_arabic(line, use_raqm),
            font=font,
            fill="white",
            anchor="ma",
            direction=direction,
        )
        y += line_h
    out = Image.alpha_composite(img, overlay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner only: put the caption text on the photo as an Arabic banner."""
    if not _is_owner(update):
        return  # customers keep the old behavior: photos ignored
    caption = (update.message.caption or "").strip()
    if not caption:
        await update.message.reply_text(
            "ابعت الصورة تاني واكتب النص اللي عايزه عليها في الكابشن."
        )
        return
    if not (Image and os.path.exists(FONT_PATH)):
        await update.message.reply_text(FALLBACK)
        return
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        data = await file.download_as_bytearray()
        result = await asyncio.to_thread(
            make_caption_image, bytes(data), caption[:200]
        )
        await update.message.reply_photo(photo=io.BytesIO(result))
    except Exception:
        logger.exception("Caption image failed")
        await update.message.reply_text(FALLBACK)


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
    if is_owner and re.match(r"^\s*(افتكر|متنساش)", text):
        fact = re.sub(r"^\s*(افتكر ان|افتكر إن|افتكر|متنساش ان|متنساش إن|متنساش)\s*", "", text).strip()
        if fact:
            data = _load_data()
            data.setdefault("memory", []).append(fact)
            _save_data(data)
            await update.message.reply_text(f"تمام، هفتكر دايما: {fact}")
            return
    voice_requested = False
    if is_owner and VOICE_PREFIX_RE.match(text):
        voice_requested = True
        text = VOICE_PREFIX_RE.sub("", text, count=1).strip()
        if not text:
            return
    if is_owner and REMINDER_RE.match(text):
        try:
            await handle_reminder(update, text)
        except Exception:
            logger.exception("Reminder handling failed")
            await update.message.reply_text(FALLBACK)
        return
    if is_owner and URL_RE.search(text):
        url = URL_RE.search(text).group(0)
        rest = URL_RE.sub("", text).strip()
        if not rest or LINK_INTENT_RE.match(rest):
            try:
                await summarize_link(update, url, rest)
            except Exception:
                logger.exception("Link summarize failed")
                await update.message.reply_text(FALLBACK)
            return
    try:
        if is_owner:
            reply = await asyncio.to_thread(
                ask_gemini,
                update.message.chat_id,
                text,
                owner_prompt(),
                4096,
            )
            if reply.strip().startswith(SEARCH_TOKEN):
                query = reply.strip()[len(SEARCH_TOKEN):].strip() or text
                results = await asyncio.to_thread(web_search, query)
                if results:
                    lines = "\n".join(
                        f"- {(r.get('title') or '').strip()}: {(r.get('body') or '').strip()}"
                        for r in results
                    )
                    pages = await asyncio.to_thread(
                        fetch_pages_text,
                        [(r.get("href") or "") for r in results[:2]],
                    )
                    follow_up = (
                        f"دورت على الإنترنت عن: {query}\n"
                        f"ودي نتايج البحث:\n{lines}\n\n"
                    )
                    if pages:
                        follow_up += f"وده نص صفحات من النتايج:\n{pages}\n\n"
                    follow_up += f"لخصها وجاوب على سؤالي الأصلي: {text}"
                    reply = await asyncio.to_thread(
                        ask_gemini,
                        update.message.chat_id,
                        follow_up,
                        owner_prompt(),
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
    if is_owner and (voice_requested or update.message.chat_id in VOICE_ON):
        await send_voice_reply(update.message, reply)




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
    telegram_app.add_handler(CommandHandler("trends", trends))
    telegram_app.add_handler(CommandHandler("reminders", reminders_list))
    telegram_app.add_handler(CommandHandler("todo", todo_cmd))
    telegram_app.add_handler(CommandHandler("note", note_cmd))
    telegram_app.add_handler(CommandHandler("spent", money_cmd))
    telegram_app.add_handler(CommandHandler("earned", money_cmd))
    telegram_app.add_handler(CommandHandler("money", money_cmd))
    telegram_app.add_handler(CommandHandler("remember", remember_cmd))
    telegram_app.add_handler(CommandHandler("briefing", briefing_cmd))
    telegram_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    telegram_app.add_handler(CommandHandler("voice", voice_toggle))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, on_photo))

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
        # Daily 9:00 Cairo briefing for every registered owner.
        for owner_id in OWNER_IDS:
            SCHEDULER.add_job(
                send_daily_briefing,
                trigger="cron",
                hour=9,
                minute=0,
                args=[owner_id],
                id=f"briefing-{owner_id}",
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
        await asyncio.Event().wait()  # run forever


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
