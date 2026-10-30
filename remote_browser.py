"""Owner-only Telegram commands for READING public web pages through a separate
browser service. Scope (do not read more into it):
- public pages only, and only sites on the browser service's explicit
  allowed-sites list (anything else is refused there); no logins, no buttons,
  no typing, no purchases;
- one command at a time, the first one after idle can take up to ~2 minutes;
- Amazon (including Associates and short links) is refused by the browser
  service at every navigation hop, and a site that redirects to a different
  site is not followed.

Inert unless both REMOTE_BROWSER_URL (https) and REMOTE_BROWSER_TOKEN are set:
register() then adds nothing and the bot behaves exactly as before.

Each command is forwarded to POST {REMOTE_BROWSER_URL}/tasks-exec with the
shared bearer token. The work runs in a background task, so the bot's update
loop is never held while the browser works. If the outcome is not known, the
SAME task id is kept and /result fetches it without running the command again.
The unresolved id is kept in a local file only; it does not survive a redeploy.
A message redelivered by Telegram reuses its id, but a fresh retry typed by the
user after a redeploy gets a new id and can run the (read-only) command again.

Every command also works in plain Egyptian Arabic text (no slash, e.g.
"تصفح <رابط>" or "النتيجة") through handle_arabic(), wired from bot.py.
"""
import asyncio
import json
import logging
import os
import re
import uuid

import requests
from telegram.ext import CommandHandler

logger = logging.getLogger("remote_browser")

_busy = False            # one command at a time; set synchronously before the job starts
_pending = None          # payload (with its id) whose outcome is still unknown
_tasks = set()           # strong refs to background jobs
_STATE = os.environ.get("REMOTE_BROWSER_STATE", "/tmp/remote_browser_pending.json")
_NS = uuid.UUID("6f1c2d5e-0b7a-4c1e-9d3a-52a1f0c7b9e4")
_CMDS = {"browse": "browse", "page": "page", "links": "links", "images": "images",
         "price": "price", "text": "text", "back": "back", "closebrowser": "close", "click": "click"}
MAX_TEXT = 3500
WAIT_ROUNDS = 3          # each round waits up to ~100s server side; same id is resent
CONNECT_TIMEOUT = 60      # a sleeping free instance can take ~45-60s to accept
BACKOFF = (5, 10, 20)     # pause after a network error or 5xx, same id
READ_TIMEOUT = 120


def _save_pending(p):
    """Write-ahead record of the unresolved job (survives a process restart)."""
    try:
        if p is None:
            if os.path.exists(_STATE):
                os.remove(_STATE)
        else:
            with open(_STATE, "w") as f:
                json.dump(p, f)
    except OSError as exc:
        logger.warning("could not persist browser job state: %s", type(exc).__name__)


def _load_pending():
    global _pending
    try:
        with open(_STATE) as f:
            p = json.load(f)
        if isinstance(p, dict) and p.get("id") and p.get("cmd"):
            _pending = p
    except (OSError, ValueError):
        pass


def _config():
    url = os.environ.get("REMOTE_BROWSER_URL", "").strip().rstrip("/")
    token = os.environ.get("REMOTE_BROWSER_TOKEN", "")
    if not url.startswith("https://") or not token:
        return None
    return url, token


def _post(url, token, payload):
    r = requests.post(url + "/tasks-exec", json=payload,
                      headers={"Authorization": "Bearer " + token},
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    try:
        body = r.json()
    except Exception:
        body = {}
    return r.status_code, body


def format_result(cmd, result):
    result = result or {}
    if cmd == "links":
        lines = ["الرابط الحالي: %s" % result.get("url", "")]
        for i, el in enumerate(result.get("elements") or []):
            if i >= 40:
                lines.append("...")
                break
            kind = "رابط" if el.get("kind") == "link" else "زر"
            lines.append("%d. [%s] %s %s" % (i, kind, (el.get("text") or "")[:60],
                                              el.get("href") or ""))
        lines.append("للضغط على رابط: اكتب اضغط ورقمه (مثال: اضغط 2)  (الأزرار غير مدعومة هنا)")
        out = "\n".join(lines)
    elif cmd == "images":
        imgs = result.get("images") or []
        out = "المصدر: %s\n" % result.get("source") + "\n".join(imgs[:10])
    elif cmd == "price":
        p = result.get("price") or {}
        out = "السعر الظاهر في الصفحة: %s\nمرشحات: %s\nالسعر قبل الخصم: %s" % (
            p.get("buybox_price"), ", ".join(p.get("price_candidates") or []), p.get("list_price"))
    elif cmd == "text":
        out = "%s\n%s\n\n%s" % (result.get("title", ""), result.get("url", ""), result.get("text", ""))
    elif cmd == "close":
        out = "تم إغلاق المتصفح."
    else:
        out = "العنوان: %s\nالرابط: %s" % (result.get("title", ""), result.get("url", ""))
    return out[:MAX_TEXT]


async def _pause(i):
    await asyncio.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])


async def run_command(cmd, args, payload=None):
    """Returns user-facing text. Never raises. A given payload is resent with its
    original id (the server never runs an id twice)."""
    global _pending
    cfg = _config()
    if cfg is None:
        return "ربط المتصفح غير مفعّل."
    url, token = cfg
    if payload is None:
        payload = {"id": uuid.uuid4().hex, "cmd": cmd, "args": args}
    _pending = payload
    _save_pending(payload)   # unresolved until a terminal outcome is seen
    loop = asyncio.get_event_loop()
    errors = 0
    rounds = 0
    while rounds < WAIT_ROUNDS and errors <= len(BACKOFF):
        try:
            code, body = await loop.run_in_executor(None, _post, url, token, payload)
        except Exception as exc:
            logger.warning("remote browser request failed: %s", type(exc).__name__)
            await _pause(errors)
            errors += 1
            continue  # same id: the server never re-executes, it returns the stored row
        if code == 202:
            rounds += 1
            continue
        if code == 200 and body.get("status") in ("done", "failed", "error", "cancelled"):
            _pending = None
            _save_pending(None)
            if body.get("status") == "done":
                return format_result(payload["cmd"], body.get("result"))
            err = str(body.get("error") or body.get("status"))[:600]
            return "تعذر التنفيذ: " + err
        if code in (400, 401, 404, 405):   # rejected before anything was queued
            _pending = None
            _save_pending(None)
        if code == 400:
            err = str(body.get("error") or "")[:300]
            extra = "\nأمازون غير متاح عبر هذا الربط." if "Amazon" in err else ""
            return "مرفوض: " + err + extra
        if code in (401, 404, 405):
            return "ربط المتصفح غير مفعّل أو الرمز غير صحيح."
        logger.warning("remote browser service answered HTTP %s", code)
        await _pause(errors)
        errors += 1  # 5xx or unexpected body: outcome unknown, keep the same id pending
    return ("نتيجة الأمر غير معروفة لحد دلوقتي (ممكن يكون اتنفّذ أو لسه شغال). "
            "رقم المهمة: %s. اكتب النتيجة (أو /result) أجيب النتيجة من غير ما أعيد التنفيذ. "
            "لو فضلت غير معروفة لازم تتراجع يدويًا قبل أي أمر جديد."
            % payload["id"][:8])


async def _job(message, cmd, args, payload=None):
    global _busy
    try:
        text = await run_command(cmd, args, payload)
    except Exception as exc:   # never leave the busy flag set
        logger.warning("browser job crashed: %s", type(exc).__name__)
        text = "حصل خطأ داخلي. اكتب النتيجة (أو /result) أجيب حالة المهمة لو كانت اتبعتت."
    finally:
        _busy = False
    try:
        await message.reply_text(text)
    except Exception as exc:
        logger.warning("could not send browser result: %s", type(exc).__name__)


def _spawn(message, cmd, args, payload=None):
    global _busy
    _busy = True
    task = asyncio.create_task(_job(message, cmd, args, payload))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _dispatch(update, name, args):
    """Run one already-validated browser command. Shared by /commands and Arabic."""
    msg = update.effective_message
    if msg is None:
        return
    if name == "result":
        if _pending is None:
            await msg.reply_text("مفيش مهمة معلّقة.")
            return
        if _busy:
            await msg.reply_text("في مهمة شغالة دلوقتي، استنى لما تخلص.")
            return
        await msg.reply_text("بجيب نتيجة نفس المهمة (من غير إعادة تنفيذ)...")
        _spawn(msg, _pending["cmd"], _pending["args"], _pending)
        return
    if _pending is not None:
        await msg.reply_text("في مهمة سابقة نتيجتها لسه غير معروفة. اكتب النتيجة الأول.")
        return
    if _busy:
        await msg.reply_text("في أمر شغال دلوقتي، استنى لما يخلص.")
        return
    await msg.reply_text("جارٍ التنفيذ... (أول مرة بعد فترة قد تأخذ حتى دقيقتين)")
    # id derived from the Telegram update: a redelivered update maps to the same id
    pl = {"id": uuid.uuid5(_NS, "tg-update-%s" % update.update_id).hex,
          "cmd": _CMDS[name], "args": args}
    _spawn(msg, pl["cmd"], args, pl)


def _make_handler(name, is_owner):
    async def handler(update, context):
        if not is_owner(update) or update.effective_message is None:
            return
        args = {}
        if name == "browse":
            if not context.args:
                await update.effective_message.reply_text(
                    "اكتب: تصفح وبعدين الرابط. مثال: تصفح https://example.com")
                return
            args = {"url": context.args[0]}
        elif name == "click":
            if not context.args or not context.args[0].isdigit():
                await update.effective_message.reply_text(
                    "اكتب: اضغط ورقم الرابط من أمر اللينكات. مثال: اضغط 3")
                return
            args = {"index": int(context.args[0])}
        await _dispatch(update, name, args)
    return handler


def configured():
    """True when the browser link is enabled (both env vars present)."""
    return _config() is not None


# ---- Egyptian-Arabic phrasing of the same commands (plain text, no slash) ----

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_AR_EXACT = {
    "النتيجة": "result", "النتيجه": "result", "نتيجة": "result",
    "هات النتيجة": "result", "هات النتيجه": "result", "جيب النتيجة": "result",
    "الصفحة": "page", "الصفحه": "page",
    "اللينكات": "links", "لينكات": "links", "الروابط": "links",
    "الصور": "images",
    "السعر": "price", "بكام": "price", "بكام ده": "price", "بكام دي": "price",
    "النص": "text",
    "ارجع": "back", "رجوع": "back", "ارجع ورا": "back",
    "اقفل المتصفح": "closebrowser", "قفل المتصفح": "closebrowser",
    "اقفلي المتصفح": "closebrowser",
}
_AR_BROWSE_RE = re.compile(
    r"^(?:تصفح|افتح|افتحلي|افتحي|ادخل على|خش على|ادخل)\s+(\S+)\s*$")
_AR_CLICK_RE = re.compile(
    r"^(?:اضغط|ادوس|دوس)\s+(?:على\s+)?(?:الرابط\s+)?(?:رقم\s+)?(\d+)\s*$")
_URLISH_RE = re.compile(r"^(https?://|www\.|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


async def handle_arabic(update, text):
    """Dispatch an Arabic-phrased browser command. False if the text is not one.

    Callers gate on the owner already; this gates on the link being configured.
    """
    if not configured():
        return False
    t = text.strip().translate(_AR_DIGITS)
    if not t:
        return False
    m = _AR_CLICK_RE.match(t)
    if m:
        await _dispatch(update, "click", {"index": int(m.group(1))})
        return True
    m = _AR_BROWSE_RE.match(t)
    if m:
        url = m.group(1)
        if not _URLISH_RE.match(url):
            return False   # "افتح ..." with no link is ordinary conversation
        await _dispatch(update, "browse", {"url": url})
        return True
    name = _AR_EXACT.get(t) or _AR_EXACT.get(t.rstrip("؟?."))
    if name:
        await _dispatch(update, name, {})
        return True
    return False


def register(telegram_app, is_owner):
    """Adds owner-only handlers when configured; otherwise does nothing."""
    if _config() is None:
        logger.info("remote browser link not configured; commands not registered")
        return False
    names = list(_CMDS) + ["result"]
    taken = set()
    for hs in telegram_app.handlers.values():
        for h in hs:
            if isinstance(h, CommandHandler):
                taken |= set(h.commands)
    clash = [n for n in names if n in taken]
    if clash:
        logger.error("remote browser not registered, command name clash: %s", clash)
        return False
    _load_pending()
    for name in names:
        telegram_app.add_handler(CommandHandler(name, _make_handler(name, is_owner)))
    logger.info("remote browser commands registered (owner-only)")
    return True
