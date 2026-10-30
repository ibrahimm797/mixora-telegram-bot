"""Owner-only Telegram commands that drive the separate browser service.

Inert unless both REMOTE_BROWSER_URL (https) and REMOTE_BROWSER_TOKEN are set:
register() then adds nothing and the bot behaves exactly as before.

Each command is forwarded to POST {REMOTE_BROWSER_URL}/tasks-exec with the
shared bearer token. The browser service decides what is allowed (public
sites only; Amazon is refused there at every navigation hop). Buttons and
typing are not supported here.
"""
import asyncio
import logging
import os
import uuid

import requests
from telegram.ext import CommandHandler

logger = logging.getLogger("remote_browser")

_lock = asyncio.Lock()
_CMDS = {"browse": "browse", "page": "page", "links": "links", "images": "images",
         "price": "price", "text": "text", "back": "back", "closebrowser": "close", "click": "click"}
MAX_TEXT = 3500
WAIT_ROUNDS = 3          # each round waits up to ~100s server side; same id is resent
CONNECT_TIMEOUT = 25
READ_TIMEOUT = 135


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
        lines.append("للضغط على رابط: /click رقم  (الأزرار غير مدعومة هنا)")
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


async def run_command(cmd, args):
    """Returns user-facing text. Never raises."""
    cfg = _config()
    if cfg is None:
        return "ربط المتصفح غير مفعّل."
    url, token = cfg
    payload = {"id": uuid.uuid4().hex, "cmd": cmd, "args": args}
    loop = asyncio.get_event_loop()
    for _ in range(WAIT_ROUNDS):
        try:
            code, body = await loop.run_in_executor(None, _post, url, token, payload)
        except Exception as exc:
            logger.warning("remote browser request failed: %s", type(exc).__name__)
            continue  # same id: server never re-executes, it returns the stored row
        if code == 202:
            continue
        if code == 200:
            if body.get("status") == "done":
                return format_result(cmd, body.get("result"))
            err = str(body.get("error") or body.get("status"))[:600]
            return "تعذر التنفيذ: " + err
        if code == 400:
            err = str(body.get("error") or "")[:300]
            extra = "\nأمازون غير متاح عبر هذا الربط." if "Amazon" in err else ""
            return "مرفوض: " + err + extra
        if code in (401, 404, 405):
            return "ربط المتصفح غير مفعّل أو الرمز غير صحيح."
        return "الخدمة ردّت بخطأ (%s)." % code
    return "المتصفح لم يرد في الوقت المحدد. لم يُعاد التنفيذ تلقائيًا؛ جرّب الأمر مرة أخرى بعد قليل."


def _make_handler(cmd, is_owner):
    async def handler(update, context):
        if not is_owner(update) or update.effective_message is None:
            return
        args = {}
        if cmd == "browse":
            if not context.args:
                await update.effective_message.reply_text("استخدم: /browse رابط")
                return
            args = {"url": context.args[0]}
        elif cmd == "click":
            if not context.args or not context.args[0].isdigit():
                await update.effective_message.reply_text("استخدم: /click رقم (من /links)")
                return
            args = {"index": int(context.args[0])}
        async with _lock:
            await update.effective_message.reply_text("جارٍ التنفيذ... (أول مرة قد تأخذ حتى دقيقتين)")
            text = await run_command(_CMDS[cmd], args)
        await update.effective_message.reply_text(text)
    return handler



def register(telegram_app, is_owner):
    """Adds owner-only handlers when configured; otherwise does nothing."""
    if _config() is None:
        logger.info("remote browser link not configured; commands not registered")
        return False
    for name, cmd in _CMDS.items():
        telegram_app.add_handler(CommandHandler(name, _make_handler(name, is_owner)))
    logger.info("remote browser commands registered (owner-only)")
    return True
