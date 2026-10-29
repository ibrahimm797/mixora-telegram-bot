import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import zlib
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
from telegram import InputFile
from telegram.ext import (
    Application, ChatMemberHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)
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
# Gemini does not always match the exact casing ("SEARCH_needed:" leaked to
# the user once), so detect the marker case-insensitively and never let it
# reach a chat.
SEARCH_TOKEN_RE = re.compile(r"^\s*SEARCH[_ ]NEEDED\s*:?\s*", re.I)


def _strip_search_marker(reply: str):
    """Return the search query if the reply is a search request, else None."""
    m = SEARCH_TOKEN_RE.match(reply or "")
    if m:
        return (reply or "")[m.end():].strip()
    return None

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
        queue_backup()
    except Exception:
        logger.exception("Could not save bot data")


# ---- Durable backup of owner data to a private Telegram channel ----
# Render's free disk is ephemeral: every redeploy wipes botdata.json and the
# reminders sqlite. The bot keeps its state in a private Telegram channel
# (owner + bot only): one pinned pointer message holds the whole state
# compressed inline ("BK0:<base64(zlib(json))>"), or points to a document
# message ("BK1:<file_id>") when the state grows past the text limit.
BACKUP_CHANNEL_ID = os.environ.get("BACKUP_CHANNEL_ID", "").strip()
_BACKUP_TASK = None
BACKUP_INLINE_LIMIT = 3900  # Telegram text limit is 4096 chars


def _backup_chat_id():
    if BACKUP_CHANNEL_ID:
        try:
            return int(BACKUP_CHANNEL_ID)
        except ValueError:
            logger.warning("BACKUP_CHANNEL_ID is not a number: %r", BACKUP_CHANNEL_ID)
    return _load_data().get("backup_channel_id")


def build_state_bundle() -> dict:
    data = _load_data()
    chat_id = _backup_chat_id()
    if chat_id:
        data["backup_channel_id"] = chat_id
    return {
        "v": 1,
        "saved_at": datetime.now(CAIRO).isoformat(),
        "data": data,
        "reminders": data.get("reminders", []),
    }


def _pack_bundle(bundle: dict) -> tuple[str, bytes]:
    raw = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    payload = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")
    if len(payload) <= BACKUP_INLINE_LIMIT:
        return "BK0:" + payload, raw
    return None, raw  # too big: caller sends the raw JSON as a document


def _unpack_inline(payload: str) -> dict:
    raw = zlib.decompress(base64.urlsafe_b64decode(payload.encode("ascii")))
    return json.loads(raw.decode("utf-8"))


def apply_state_bundle(bundle: dict) -> tuple[int, int, int]:
    data = bundle.get("data") or {}
    rems = bundle.get("reminders") or data.get("reminders") or []
    now_ts = time.time()
    future = []
    for r in rems:
        try:
            when = datetime.fromisoformat(r["iso"])
        except Exception:
            continue
        if when.timestamp() > now_ts:
            future.append(r)
            schedule_reminder(int(r["chat_id"]), when, r["text"])
    data["reminders"] = future
    _save_data(data)
    return (
        len([t for t in data.get("todos", []) if not t.get("done")]),
        len(data.get("notes", [])),
        len(future),
    )


async def backup_now() -> None:
    chat_id = _backup_chat_id()
    if not chat_id:
        logger.info("Backup skipped: no backup channel linked yet")
        return
    try:
        bundle = build_state_bundle()
        text, raw = _pack_bundle(bundle)
        if text is None:
            sent = await telegram_app.bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(raw), filename="mixora_backup.json"),
                caption="نسخة احتياطية",
            )
            text = "BK1:" + sent.document.file_id
        chat = await telegram_app.bot.get_chat(chat_id)
        pinned = chat.pinned_message
        if pinned and pinned.text and pinned.text.startswith("BK"):
            await telegram_app.bot.edit_message_text(
                chat_id=chat_id, message_id=pinned.message_id, text=text
            )
        else:
            msg = await telegram_app.bot.send_message(chat_id=chat_id, text=text)
            await telegram_app.bot.pin_chat_message(
                chat_id=chat_id, message_id=msg.message_id, disable_notification=True
            )
        d = bundle["data"]
        logger.info(
            "Backup written to Telegram channel (todos=%d notes=%d reminders=%d)",
            len(d.get("todos", [])), len(d.get("notes", [])),
            len(d.get("reminders", [])),
        )
    except Exception:
        logger.exception("Backup to Telegram channel failed")


async def restore_from_backup() -> bool:
    chat_id = _backup_chat_id()
    if not chat_id:
        logger.info("Restore skipped: no backup channel linked yet")
        return False
    try:
        chat = await telegram_app.bot.get_chat(chat_id)
        pinned = chat.pinned_message
        if not pinned or not pinned.text or not pinned.text.startswith("BK"):
            logger.info("No backup found in channel (fresh start)")
            return False
        if pinned.text.startswith("BK0:"):
            bundle = _unpack_inline(pinned.text[4:])
        else:
            f = await telegram_app.bot.get_file(pinned.text[4:].strip())
            raw = bytes(await f.download_as_bytearray())
            bundle = json.loads(raw.decode("utf-8"))
        todos_n, notes_n, rems_n = apply_state_bundle(bundle)
        logger.info(
            "State restored from Telegram backup (todos=%d notes=%d reminders=%d saved_at=%s)",
            todos_n, notes_n, rems_n, bundle.get("saved_at", "?"),
        )
        return True
    except Exception:
        logger.exception("Restore from Telegram backup failed")
        return False


async def _debounced_backup() -> None:
    await asyncio.sleep(10)
    await backup_now()


def queue_backup() -> None:
    """Debounced: fold bursts of edits into one channel write."""
    global _BACKUP_TASK
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    if _BACKUP_TASK and not _BACKUP_TASK.done():
        _BACKUP_TASK.cancel()
    _BACKUP_TASK = asyncio.create_task(_debounced_backup())


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner adds the bot as admin to the private backup channel -> link it."""
    cm = update.my_chat_member
    if not cm or cm.chat.type != "channel":
        return
    if cm.new_chat_member.status not in ("administrator", "member"):
        return
    data = _load_data()
    if data.get("backup_channel_id") == cm.chat.id:
        return
    data["backup_channel_id"] = cm.chat.id
    _save_data(data)
    logger.info("Backup channel detected: %s (%s)", cm.chat.id, cm.chat.title)
    for oid in OWNER_IDS:
        try:
            await telegram_app.bot.send_message(
                oid,
                "تمام، ربطت قناة النسخ الاحتياطي. من دلوقتي بياناتك "
                "(مهام، ملاحظات، فلوس، ذكريات، تذكيرات) محفوظة حتى لو البوت اتعاد تشغيله.\n"
                "ابعت السطر ده لإنستنكت عشان يثبته نهائيا:\n"
                f"BACKUP_ID {cm.chat.id}",
            )
        except Exception:
            logger.exception("Could not message owner about backup channel")


async def backupstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner: /backupstatus -> what is saved in the backup channel."""
    if not _is_owner(update):
        return
    chat_id = _backup_chat_id()
    if not chat_id:
        await update.message.reply_text(
            "النسخ الاحتياطي لسه مش مظبوط. اعمل قناة تيليجرام خاصة جديدة "
            "وضيفني فيها أدمن، وأنا هربطها لوحدي."
        )
        return
    try:
        chat = await telegram_app.bot.get_chat(chat_id)
        pinned = chat.pinned_message
        if not pinned or not pinned.text or not pinned.text.startswith("BK"):
            await update.message.reply_text("القناة مربوطة بس لسه مفيش نسخة محفوظة.")
            return
        if pinned.text.startswith("BK0:"):
            bundle = _unpack_inline(pinned.text[4:])
            d = bundle.get("data", {})
            open_todos = len([t for t in d.get("todos", []) if not t.get("done")])
            await update.message.reply_text(
                "آخر نسخة محفوظة: "
                + str(bundle.get("saved_at", "?"))[:16]
                + f"\nمهام مفتوحة: {open_todos} | ملاحظات: {len(d.get('notes', []))}"
                + f" | حركات فلوس: {len(d.get('money', []))}"
                + f" | ذكريات: {len(d.get('memory', []))}"
                + f" | تذكيرات جاية: {len(d.get('reminders', []))}"
            )
        else:
            await update.message.reply_text("النسخة محفوظة كملف في القناة (بيانات كبيرة).")
    except Exception:
        logger.exception("backupstatus failed")
        await update.message.reply_text(FALLBACK)

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


def transcribe_voice(audio_bytes: bytes) -> str:
    """Literal Arabic transcript of an owner voice note; '' on silence/failure."""
    body = {
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
                            "انسخ الكلام اللي في التسجيل ده حرفيا بالعربي، من غير أي "
                            "تعليق أو شرح أو ترجمة. لو مفيش كلام واضح اكتب: EMPTY"
                        )
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 512,
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
            logger.exception("Voice transcription failed (model=%s)", model)
            continue
        if resp.status_code == 200:
            text, _ = _extract_text(resp.json())
            if text:
                t = text.strip()
                if t and not t.startswith("EMPTY"):
                    return t
                return ""
        else:
            logger.warning(
                "Transcription error %s (model=%s)", resp.status_code, model
            )
    return ""


# ---- Owner reminders (APScheduler, persisted to sqlite) ----

async def send_reminder(chat_id: int, text: str) -> None:
    try:
        await telegram_app.bot.send_message(
            chat_id=chat_id, text=f"تذكير يا إبراهيم: {text}"
        )
    except Exception:
        logger.exception("Reminder send failed")
    data = _load_data()
    rems = [r for r in data.get("reminders", [])
            if not (int(r.get("chat_id", 0)) == chat_id and r.get("text") == text)]
    if len(rems) != len(data.get("reminders", [])):
        data["reminders"] = rems
        _save_data(data)


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
    data = _load_data()
    data.setdefault("reminders", []).append(
        {"chat_id": update.message.chat_id, "text": note, "iso": when.isoformat()}
    )
    _save_data(data)
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
    if not update.message:
        return
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
        transcript = await asyncio.to_thread(transcribe_voice, bytes(data))
        if transcript:
            handled = await handle_arabic_intent(update, transcript)
            if handled:
                return
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
    if not update.message:
        return
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


# ---- Egyptian-Arabic natural-language intents (owner; no slash needed) ----

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
AR_TRENDS_RE = re.compile(
    r"^(/ترند|ترند|ترندات|الترند|الترندات|trends?|ترند ايه|ايه الترند|إيه الترند|"
    r"ترندات مصر|الترند النهاردة|الترند النهارده)\s*$", re.I)
AR_BRIEFING_RE = re.compile(
    r"^(/ملخص|/بريفينج|الملخص|ملخصي|ملخص النهاردة|ملخص النهارده|"
    r"ايه عندي النهاردة|إيه عندي النهاردة|إيه عندي النهارده|عندي ايه النهاردة|"
    r"جدولي|الاجندة|أجندة اليوم)\s*$")
AR_REMINDERS_RE = re.compile(
    r"^(/تذكيرات|تذكيراتي|ايه تذكيراتي|إيه تذكيراتي|التذكيرات)\s*$")
AR_TODO_LIST_RE = re.compile(
    r"^(/مهام|/تودو|مهامي|المهام|قولي مهامي|ايه مهامي|إيه مهامي|"
    r"عندي مهام ايه|عندي ايه مهام|التودو)\s*$")
AR_TODO_ADD_RE = re.compile(
    r"^(ضيف|اضيف|أضيف|سجل|حط|زود|اكتب)(لي)?\s+(مهمة|مهمّة)\s*[:،]?\s*(.+)$")
AR_TODO_DONE_RE = re.compile(
    r"^(خلصت|شيل|امسح)\s+(المهمة\s+)?(رقم\s+)?(\d+)\s*$")
AR_TODO_DONE_TEXT_RE = re.compile(r"^خلصت\s+(.+)$")
AR_NOTE_LIST_RE = re.compile(
    r"^(/ملاحظة|/ملاحظات|/نوت|ملاحظاتي|قولي ملاحظاتي|ايه ملاحظاتي|إيه ملاحظاتي)\s*$")
AR_NOTE_ADD_RE = re.compile(
    r"^(?:/ملاحظة\s+|(?:سجل|اكتب|حط)(لي)?\s+(?:ملاحظة|نوت)\s*[:،]?\s*"
    r"|ملاحظة\s*[:،]\s*)(.+)$")
AR_MONEY_SUMMARY_RE = re.compile(
    r"^(/فلوس|/حساب|فلوسي|حسابي|مصاريفي|ايه حسابي|إيه حسابي|صرفت كام|"
    r"كام صرفت|الميزانية)\s*$")
AR_SPENT_RE = re.compile(
    r"^(?:/مصروف\s+|سجل\s+مصروف\s+|صرفت\s+|دفعت\s+)"
    r"(\d+(?:\.\d+)?)\s*(?:جنيه|ج|جنية)?\s*(.*)$")
AR_EARN_RE = re.compile(
    r"^(?:/دخل\s+|سجل\s+دخل\s+|كسبت\s+|قبضت\s+|جالي\s+)"
    r"(\d+(?:\.\d+)?)\s*(?:جنيه|ج|جنية)?\s*(.*)$")


async def handle_arabic_intent(update: Update, text: str) -> bool:
    """Run owner commands phrased in plain Egyptian Arabic. True if handled."""
    t = text.strip().translate(_AR_DIGITS)
    if not t:
        return False
    data = _load_data()
    m = re.match(r"^(افتكر|متنساش)\s*(اني|إني|ان|إن)?\s*(.+)$", t)
    if m and m.group(3).strip():
        fact = m.group(3).strip()
        data.setdefault("memory", []).append(fact)
        _save_data(data)
        await update.message.reply_text(f"تمام، هفتكر دايما: {fact}")
        return True
    if AR_TRENDS_RE.match(t):
        await trends(update, None)
        return True
    if AR_BRIEFING_RE.match(t):
        await update.message.reply_text(build_briefing(update.message.chat_id))
        return True
    if AR_REMINDERS_RE.match(t):
        await reminders_list(update, None)
        return True
    if REMINDER_RE.match(t):
        try:
            await handle_reminder(update, t)
        except Exception:
            logger.exception("Reminder handling failed")
            await update.message.reply_text(FALLBACK)
        return True
    if AR_TODO_LIST_RE.match(t):
        open_todos = [x for x in data.get("todos", []) if not x.get("done")]
        if not open_todos:
            await update.message.reply_text("مفيش مهام مسجلة. قول: ضيف مهمة وبعدين المهمة.")
        else:
            lines = [f"{i+1}. {x['text']}" for i, x in enumerate(open_todos)]
            await update.message.reply_text(
                "مهامك:\n" + "\n".join(lines) + "\n\nلما تخلص واحدة قول: خلصت ورقمها"
            )
        return True
    m = AR_TODO_ADD_RE.match(t)
    if m:
        task = m.group(4).strip()
        data.setdefault("todos", []).append(
            {"text": task, "done": False, "ts": datetime.now(CAIRO).isoformat()}
        )
        _save_data(data)
        await update.message.reply_text(f"سجلتها: {task}")
        return True
    m = AR_TODO_DONE_RE.match(t)
    if m:
        n = int(m.group(4)) - 1
        open_todos = [x for x in data.get("todos", []) if not x.get("done")]
        if 0 <= n < len(open_todos):
            open_todos[n]["done"] = True
            _save_data(data)
            await update.message.reply_text(f"تمام، خلصت: {open_todos[n]['text']}")
        else:
            await update.message.reply_text("مفيش مهمة بالرقم ده. ابعت: مهامي")
        return True
    m = AR_TODO_DONE_TEXT_RE.match(t)
    if m:
        frag = m.group(1).strip()
        for x in data.get("todos", []):
            if not x.get("done") and frag and frag in x.get("text", ""):
                x["done"] = True
                _save_data(data)
                await update.message.reply_text(f"تمام، خلصت: {x['text']}")
                return True
        await update.message.reply_text("مش لاقي مهمة مفتوحة فيها الكلام ده. ابعت: مهامي")
        return True
    if AR_NOTE_LIST_RE.match(t):
        notes = data.get("notes", [])
        if not notes:
            await update.message.reply_text("مفيش ملاحظات. قول: سجل ملاحظة وبعدين الكلام.")
        else:
            lines = [f"- {_fmt_entry(n)}" for n in notes[-15:]]
            await update.message.reply_text("ملاحظاتك:\n" + "\n".join(lines))
        return True
    m = AR_NOTE_ADD_RE.match(t)
    if m:
        note = m.group(2).strip()
        data.setdefault("notes", []).append(
            {"text": note, "ts": datetime.now(CAIRO).isoformat()}
        )
        _save_data(data)
        await update.message.reply_text("حفظتها.")
        return True
    if AR_MONEY_SUMMARY_RE.match(t):
        entries = data.get("money", [])
        spent, earned = _money_summary(entries)
        recent = "\n".join(
            f"- {'صرف' if e['kind']=='spent' else 'دخل'}: {e['amount']:g} جنيه - {e.get('text','')} ({e.get('ts','')[:10]})"
            for e in entries[-10:]
        ) or "لسه مفيش حاجة مسجلة."
        await update.message.reply_text(
            f"إجمالي الدخل: {earned:g} جنيه\nإجمالي المصروف: {spent:g} جنيه\n"
            f"الصافي: {earned-spent:g} جنيه\n\nآخر الحركات:\n{recent}"
        )
        return True
    m = AR_SPENT_RE.match(t)
    if m:
        amount = float(m.group(1))
        desc = re.sub(r"^(على|في|لـ|ل)\s+", "", (m.group(2) or "").strip())
        data.setdefault("money", []).append(
            {"kind": "spent", "amount": amount, "text": desc,
             "ts": datetime.now(CAIRO).isoformat()}
        )
        _save_data(data)
        spent, earned = _money_summary(data["money"])
        await update.message.reply_text(
            f"سجلت الصرف: {amount:g} جنيه {desc}\nالصافي دلوقتي: {earned-spent:g} جنيه"
        )
        return True
    m = AR_EARN_RE.match(t)
    if m:
        amount = float(m.group(1))
        desc = re.sub(r"^(على|في|من|لـ|ل)\s+", "", (m.group(2) or "").strip())
        data.setdefault("money", []).append(
            {"kind": "earned", "amount": amount, "text": desc,
             "ts": datetime.now(CAIRO).isoformat()}
        )
        _save_data(data)
        spent, earned = _money_summary(data["money"])
        await update.message.reply_text(
            f"سجلت الدخل: {amount:g} جنيه {desc}\nالصافي دلوقتي: {earned-spent:g} جنيه"
        )
        return True
    return False


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return  # channel service posts (pins etc.) carry no message
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
    if is_owner:
        try:
            if await handle_arabic_intent(update, text):
                return
        except Exception:
            logger.exception("Arabic intent handling failed")
            await update.message.reply_text(FALLBACK)
            return
    voice_requested = False
    if is_owner and VOICE_PREFIX_RE.match(text):
        voice_requested = True
        text = VOICE_PREFIX_RE.sub("", text, count=1).strip()
        if not text:
            return
    if is_owner and re.match(r"^\s*(ترند|ترندات|الترند|الترندات|trends?)\s*$", text, re.I):
        await trends(update, context)
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
            query = _strip_search_marker(reply)
            if query is not None:
                query = query or text
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
                    leftover = _strip_search_marker(reply)
                    if leftover is not None:
                        reply = leftover if len(leftover) > 20 else FALLBACK
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


# ---- Deep-read browser research via the separate public worker ----
# Additive owner-only feature. The worker is a separate Render service with no
# Telegram credentials; it reads 1-2 public pages in a real browser and returns
# claims with verbatim quotes. This bot stays the only Telegram token consumer:
# it polls the worker's durable outbox and delivers ready results itself.
BROWSER_WORKER_URL = os.environ.get(
    "BROWSER_WORKER_URL", "https://mixora-browser-acceptance.onrender.com"
).rstrip("/")
BROWSER_WORKER_TOKEN = os.environ.get("BROWSER_WORKER_TOKEN", "")


def _worker_headers() -> dict:
    return {"Authorization": "Bearer " + BROWSER_WORKER_TOKEN}


def _browse_job_id(owner_id: int, update_id: int) -> str:
    # Must match the worker's private-chat-only validation formula.
    return "tg-" + hashlib.sha256(f"{owner_id}:{update_id}".encode()).hexdigest()[:32]


def _pick_read_urls(results: list) -> list:
    urls = []
    for r in results:
        href = (r.get("href") or "").strip()
        if href.startswith("https://") and href not in urls:
            urls.append(href)
        if len(urls) == 2:
            break
    return urls


def _format_research_message(result: dict) -> str:
    if not result.get("verified"):
        return "ماقدرتش أكمّل القراءة العميقة للصفحات دي. جرب سؤال أبسط أو صفحات تانية."
    lines = ["📖 خلاصة القراءة العميقة (معلومات موثقة بنص منقول حرفيًا):"]
    for claim in (result.get("claims") or [])[:6]:
        lines.append(
            "• " + claim["text"] + "\n\"" + claim["quote"] + "\"\n" + claim["source_url"]
        )
    return "\n\n".join(lines)[:4000]


def _submit_research_job(job_id: str, payload: dict):
    return requests.post(
        f"{BROWSER_WORKER_URL}/research-jobs/{job_id}",
        json=payload,
        headers=_worker_headers(),
        timeout=60,
    )


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    if not update.message:
        return
    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await update.message.reply_text(
            "اكتب سؤالك بعد الأمر، مثال:\n/browse ايه أحدث أسعار ..."
        )
        return
    if not BROWSER_WORKER_TOKEN:
        await update.message.reply_text("خاصية القراءة العميقة لسه مش مفعّلة.")
        return
    results = await asyncio.to_thread(web_search, question, 5)
    urls = _pick_read_urls(results)
    if not urls:
        await update.message.reply_text("مش لاقي صفحات عامة مناسبة للسؤال ده دلوقتي.")
        return
    instruction = (
        "أجب على سؤال المستخدم بالعامية المصرية اعتمادًا على الصفحات المقروءة فقط، "
        "مع اقتباس حرفي دقيق يثبت كل معلومة. السؤال: " + question
    )[:1500]
    payload = {
        "instruction": instruction,
        "scope": "public_page_research",
        "account_access": False,
        "start_urls": urls,
        "delivery": True,
        "owner_id": update.message.chat_id,
        "chat_id": update.message.chat_id,
        "update_id": update.update_id,
    }
    job_id = _browse_job_id(update.message.chat_id, update.update_id)
    try:
        resp = await asyncio.to_thread(_submit_research_job, job_id, payload)
    except Exception:
        logger.exception("browse submit failed")
        await update.message.reply_text("خدمة القراءة العميقة مش متاحة دلوقتي، جرب بعد شوية.")
        return
    if resp.status_code == 202:
        asyncio.create_task(_browser_delivery_loop())
        await update.message.reply_text("بقرأ الصفحات دلوقتي، وهبعتلك الخلاصة الموثقة هنا أول ما تخلص 📖")
    else:
        logger.warning("browse submit rejected: %s %s", resp.status_code, resp.text[:200])
        await update.message.reply_text("مش قادر أبدأ القراءة دلوقتي (ضغط أو الحد اليومي). جرب بعد شوية.")


async def _browser_delivery_loop() -> None:
    # Delivers ready worker results. Claim is compare-and-swap on the worker, so
    # a result is never sent twice; an ack failure leaves the row for review
    # instead of auto-replaying.
    for _ in range(24):
        try:
            pend = await asyncio.to_thread(
                requests.get,
                f"{BROWSER_WORKER_URL}/delivery/pending",
                headers=_worker_headers(),
                timeout=20,
            )
            for job in pend.json().get("jobs", []):
                if job.get("state") != "ready":
                    continue
                claim = await asyncio.to_thread(
                    requests.post,
                    f"{BROWSER_WORKER_URL}/delivery/{job['id']}/claim",
                    headers=_worker_headers(),
                    timeout=20,
                )
                if claim.status_code != 200:
                    continue
                data = claim.json()
                msg = await telegram_app.bot.send_message(
                    chat_id=data["chat_id"],
                    text=_format_research_message(data.get("result") or {}),
                    disable_web_page_preview=True,
                )
                await asyncio.to_thread(
                    requests.post,
                    f"{BROWSER_WORKER_URL}/delivery/{job['id']}/ack",
                    json={"message_id": msg.message_id},
                    headers=_worker_headers(),
                    timeout=20,
                )
        except Exception:
            logger.exception("browser delivery loop tick failed")
        await asyncio.sleep(10)


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
    telegram_app.add_handler(CommandHandler("trend", trends))
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
    telegram_app.add_handler(CommandHandler("backupstatus", backupstatus_cmd))
    telegram_app.add_handler(CommandHandler("browse", browse_cmd))
    telegram_app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

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
        # Restore owner state (todos, notes, money, memory, reminders) from
        # the private Telegram backup channel if one is linked.
        await restore_from_backup()
        # (browser results are delivered by a bounded poller started per /browse job)
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
