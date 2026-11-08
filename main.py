#!/usr/bin/env python3
"""Temp Mail Telegram bot.

Zero pip dependencies (Python 3.9+ standard library only).
Set the BOT_TOKEN environment variable (and optionally OWNER_CHAT_ID), then run:  python main.py
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import providers as P
from db import DB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tempmail")


# --------------------------------------------------------------------------- config
def env(*names, default=""):
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return default


# Built-in defaults so the bot works right after upload. Variables with the same names override them.
BOT_TOKEN = env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", default="8324423596:AAE9g14H_okUDk9EayYxi3uRoSiskcYzCpE")
OWNER_RAW = env("OWNER_CHAT_ID", "CHAT_ID", "ALLOWED_CHAT_IDS", default="7935895229")
ALLOWED = {int(x) for x in re.split(r"[,\s]+", OWNER_RAW) if x.lstrip("-").isdigit()}
TG_API = env("TELEGRAM_API_BASE", default="https://api.telegram.org").rstrip("/")
PORT = int(env("PORT", default="8080"))
POLL_SECONDS = int(env("CHECK_INTERVAL", default="10"))
DB_PATH = env("DB_PATH", default=os.path.join("data", "bot.db"))

_pub = env("PUBLIC_URL")
if not _pub and env("RAILWAY_PUBLIC_DOMAIN"):
    _pub = env("RAILWAY_PUBLIC_DOMAIN")
if _pub and not _pub.startswith("http"):
    _pub = "https://" + _pub
PUBLIC_URL = _pub.rstrip("/")

NAME_RE = re.compile(r"^[a-z0-9._-]{3,30}$")
MAIN_KB = {
    "keyboard": [[{"text": "➕ Generate New / Delete"}, {"text": "🔄 Refresh"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

db: DB | None = None
pool = ThreadPoolExecutor(max_workers=8)
awaiting_name: set[int] = set()
_chat_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


# --------------------------------------------------------------------------- telegram
class TgError(Exception):
    def __init__(self, code, description):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


class Tg:
    def call(self, method, http_timeout=30, **params):
        url = f"{TG_API}/bot{BOT_TOKEN}/{method}"
        r = urllib.request.Request(
            url, data=json.dumps(params).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(r, timeout=http_timeout) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                j = {"ok": False, "error_code": e.code, "description": str(e.reason)}
        except Exception as e:  # network trouble, timeouts
            raise TgError(0, f"network: {type(e).__name__}")
        if not j.get("ok"):
            raise TgError(j.get("error_code", 0), j.get("description", ""))
        return j["result"]

    def send(self, chat_id, text, markup=None):
        params = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = markup
        return self.call("sendMessage", **params)


tg = Tg()


# --------------------------------------------------------------------------- html -> text
class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "hr"}
    SKIP = {"script", "style", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.links, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")) and href not in self.links:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(src: str):
    p = _Text()
    try:
        p.feed(src)
        p.close()
    except Exception:
        pass
    lines = [re.sub(r"[ \t\u00a0\u200b\u200c\u200d\ufeff]+", " ", ln).strip() for ln in "".join(p.out).splitlines()]
    cleaned, blank = [], 0
    for ln in lines:
        if ln:
            cleaned.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1 and cleaned:
                cleaned.append("")
    return "\n".join(cleaned).strip(), p.links


CODE_RE = re.compile(r"(?i)(?:code|otp|pin|passcode|verification|verify)\D{0,40}?(\d{4,8})")
CODE_RE2 = re.compile(r"(?<![\d.])(\d{6})(?![\d.])")


def find_code(subject: str, text: str):
    blob = f"{subject}\n{text}"
    m = CODE_RE.search(blob) or CODE_RE2.search(blob)
    return m.group(1) if m else None


def fmt_from(m) -> str:
    name, addr = (m.get("from_name") or "").strip(), (m.get("from_addr") or "").strip()
    if name and addr:
        return f'"{name}" <{addr}>'
    return f"<{addr}>" if addr else (name or "unknown")


def fmt_time(iso: str) -> str:
    if not iso:
        return ""
    return iso.replace("T", " ")[:16] + " UTC"


# --------------------------------------------------------------------------- mailbox helpers
def call_mail(chat_id, op, *args):
    acc = db.get_user(chat_id)
    if not acc:
        raise P.MailError("no account", status=404)
    prov = P.get_provider(acc["provider"])
    try:
        return getattr(prov, op)(acc, *args)
    except P.MailError as e:
        if e.status == 401 and prov.name != "guerrilla":
            acc["token"] = prov.login(acc)
            db.update_token(chat_id, acc["token"])
            return getattr(prov, op)(acc, *args)
        raise


def new_account(chat_id, username=None):
    old = db.get_user(chat_id)
    acc = P.create_account(username)
    db.set_user(chat_id, acc)
    if old:
        try:
            P.get_provider(old["provider"]).delete_account(old)
        except Exception:
            pass
    try:  # whatever is already in the mailbox (e.g. a welcome mail) should not trigger alerts
        for m in call_mail(chat_id, "list"):
            db.mark_seen(chat_id, m["id"])
    except Exception:
        pass
    return acc


def browser_url(chat_id, msg_id):
    if not PUBLIC_URL:
        return None
    return f"{PUBLIC_URL}/m/{db.make_link(chat_id, msg_id)}"


def msg_buttons(chat_id, msg_id, label="📖 Read here"):
    row = [{"text": label, "callback_data": f"r:{msg_id}"}]
    row_all = [row]
    url = browser_url(chat_id, msg_id)
    if url:
        row_all.append([{"text": "Open in Browser ➡️", "url": url}])
    return {"inline_keyboard": row_all}


# --------------------------------------------------------------------------- screens
def head_text(acc):
    return f"Current email address:\n<code>{esc(acc['address'])}</code>\n\n"


def show_inbox(chat_id):
    acc = db.get_user(chat_id)
    if not acc:
        tg.send(chat_id, "You don't have an email address yet.\nTap <b>➕ Generate New / Delete</b> to create one.",
                MAIN_KB)
        return
    try:
        msgs = call_mail(chat_id, "list")
    except P.MailError as e:
        log.warning("inbox error for %s: %s", chat_id, e)
        tg.send(chat_id, head_text(acc) + "⚠️ Couldn't load the inbox right now. Please tap Refresh again in a few seconds.",
                MAIN_KB)
        return
    if not msgs:
        tg.send(chat_id, head_text(acc) + "Your inbox is empty", MAIN_KB)
        return
    shown = msgs[:8]
    lines, rows = [], []
    for i, m in enumerate(shown, 1):
        subj = m["subject"] or "(no subject)"
        lines.append(f"{i}. {esc(fmt_from(m))}\n    {esc(subj)}")
        row = [{"text": f"📖 {i}. {subj[:24]}", "callback_data": f"r:{m['id']}"}]
        url = browser_url(chat_id, m["id"])
        if url:
            row.append({"text": "➡️ Browser", "url": url})
        rows.append(row)
        db.mark_seen(chat_id, m["id"])
    extra = f"\n\n(+{len(msgs) - len(shown)} older)" if len(msgs) > len(shown) else ""
    tg.send(chat_id, head_text(acc) + f"Inbox ({len(msgs)}):\n\n" + "\n\n".join(lines) + extra,
            {"inline_keyboard": rows})


def show_menu(chat_id):
    acc = db.get_user(chat_id)
    cur = head_text(acc) if acc else "You don't have an email address yet.\n\n"
    rows = [
        [{"text": "➕ Generate new (random)", "callback_data": "g:new"}],
        [{"text": "✏️ Choose my own name", "callback_data": "g:custom"}],
    ]
    if acc:
        rows.append([{"text": "🗑 Delete current email", "callback_data": "g:del"}])
    tg.send(chat_id, cur + "What would you like to do?", {"inline_keyboard": rows})


def do_generate(chat_id, username=None):
    try:
        acc = new_account(chat_id, username)
    except P.MailError as e:
        if e.status == 422 and username:
            tg.send(chat_id, "That name is taken or not allowed. Please send a different name (or /cancel).")
            return False
        log.warning("create failed: %s", e)
        tg.send(chat_id, "⚠️ Couldn't create an email right now (the mail servers are busy). Please try again in a minute.",
                MAIN_KB)
        return True
    awaiting_name.discard(chat_id)
    tg.send(chat_id, f"✅ New email address created:\n<code>{esc(acc['address'])}</code>\n\n"
                     "I'll message you here as soon as a new email arrives.", MAIN_KB)
    return True


def do_delete(chat_id):
    acc = db.get_user(chat_id)
    if not acc:
        tg.send(chat_id, "You don't have an email address to delete.", MAIN_KB)
        return
    try:
        P.get_provider(acc["provider"]).delete_account(acc)
    except Exception:
        pass
    db.del_user(chat_id)
    tg.send(chat_id, "🗑 Your email address was deleted.\nTap <b>➕ Generate New / Delete</b> whenever you need a new one.",
            MAIN_KB)


def do_read(chat_id, msg_id):
    try:
        m = call_mail(chat_id, "read", msg_id)
    except P.MailError:
        tg.send(chat_id, "⚠️ Couldn't open this email. It may have expired.")
        return
    if m["html"]:
        body, links = html_to_text(m["html"])
    else:
        body, links = (m["text"] or "").strip(), []
    if not body:
        body = "(this email has no text)"
    if len(body) > 2600:
        body = body[:2600].rstrip() + "…"
    code = find_code(m["subject"], body)
    parts = [f"📖 <b>{esc(m['subject'] or '(no subject)')}</b>",
             f"From: {esc(fmt_from(m))}"]
    when = fmt_time(m["created"])
    if when:
        parts.append(f"Date: {esc(when)}")
    if code:
        parts += ["", f"🔑 Code: <code>{code}</code>"]
    parts += ["", esc(body)]
    if links:
        parts += ["", "🔗 Links:"] + [esc(u[:160]) for u in links[:5]]
    url = browser_url(chat_id, msg_id)
    markup = {"inline_keyboard": [[{"text": "Open in Browser ➡️", "url": url}]]} if url else None
    tg.send(chat_id, "\n".join(parts), markup)


def notify_new(chat_id, m):
    code = None
    try:
        full = call_mail(chat_id, "read", m["id"])
        text = html_to_text(full["html"])[0] if full["html"] else full["text"]
        code = find_code(full["subject"], text)
    except Exception:
        pass
    lines = ["New email message", "", f"From: {esc(fmt_from(m))}", "",
             f"Subject: {esc(m['subject'] or '(no subject)')}"]
    if code:
        lines += ["", f"🔑 Code: <code>{code}</code>"]
    tg.send(chat_id, "\n".join(lines), msg_buttons(chat_id, m["id"]))


# --------------------------------------------------------------------------- update handling
def allowed(chat_id) -> bool:
    return not ALLOWED or chat_id in ALLOWED


def on_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if not allowed(chat_id):
        tg.send(chat_id, "🔒 This is a private bot.")
        return
    low = text.lower()

    if low.startswith("/cancel"):
        awaiting_name.discard(chat_id)
        tg.send(chat_id, "Cancelled.", MAIN_KB)
        return
    if low.startswith("/start") or low.startswith("/help"):
        awaiting_name.discard(chat_id)
        tg.send(chat_id,
                "👋 <b>Welcome to Temp Mail Bot!</b>\n\n"
                "Get a disposable email address and receive its emails right here in Telegram.\n\n"
                "➕ <b>Generate New / Delete</b> – new random address, your own name, or delete it\n"
                "🔄 <b>Refresh</b> – check your inbox", MAIN_KB)
        if not db.get_user(chat_id):
            do_generate(chat_id)
        else:
            show_inbox(chat_id)
        return
    if "generate new" in low or low.startswith("/new") or low.startswith("/menu"):
        awaiting_name.discard(chat_id)
        show_menu(chat_id)
        return
    if "refresh" in low or low.startswith("/refresh") or low.startswith("/inbox"):
        awaiting_name.discard(chat_id)
        show_inbox(chat_id)
        return
    if low.startswith("/delete"):
        do_delete(chat_id)
        return
    if chat_id in awaiting_name:
        name = low
        if not NAME_RE.match(name):
            tg.send(chat_id, "Name must be 3–30 characters: letters a-z, digits, dot, dash or underscore.\n"
                             "Send another name or /cancel.")
            return
        do_generate(chat_id, name)
        return
    tg.send(chat_id, "Use the buttons below 👇", MAIN_KB)


def on_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data") or ""
    try:
        tg.call("answerCallbackQuery", callback_query_id=cb["id"])
    except TgError:
        pass
    if not allowed(chat_id):
        return
    if data == "g:new":
        awaiting_name.discard(chat_id)
        do_generate(chat_id)
    elif data == "g:custom":
        awaiting_name.add(chat_id)
        tg.send(chat_id, "✏️ Send the name you want for your email (3–30 characters: a-z, 0-9, . _ -).\n"
                         "Example: <code>rahim2026</code>\n\nSend /cancel to stop.")
    elif data == "g:del":
        awaiting_name.discard(chat_id)
        do_delete(chat_id)
    elif data.startswith("r:"):
        do_read(chat_id, data[2:])


def safe_handle(update):
    chat_id = None
    try:
        if "message" in update:
            chat_id = update["message"]["chat"]["id"]
        elif "callback_query" in update:
            chat_id = update["callback_query"]["message"]["chat"]["id"]
        else:
            return
        with _chat_locks[chat_id]:
            if "message" in update:
                on_message(update["message"])
            else:
                on_callback(update["callback_query"])
    except TgError as e:
        log.warning("telegram error: %s", e)
    except Exception:
        log.exception("handler crashed")


# --------------------------------------------------------------------------- background watcher
def check_user(chat_id):
    with _chat_locks[chat_id]:
        try:
            msgs = call_mail(chat_id, "list")
        except P.MailError as e:
            if e.status in (401, 404):  # mailbox expired on the provider side
                acc = new_account(chat_id)
                tg.send(chat_id, "⚠️ Your old mailbox expired.\n\nNew email address:\n"
                                 f"<code>{esc(acc['address'])}</code>", MAIN_KB)
                return
            raise
        fresh = [m for m in msgs if not db.is_seen(chat_id, m["id"])]
        for m in reversed(fresh):
            db.mark_seen(chat_id, m["id"])
            notify_new(chat_id, m)


def check_user_safe(chat_id):
    try:
        check_user(chat_id)
    except TgError as e:
        if e.code in (400, 403):  # user blocked the bot or chat is gone
            log.info("dropping chat %s (%s)", chat_id, e)
            db.del_user(chat_id)
        else:
            log.warning("notify error for %s: %s", chat_id, e)
    except P.MailError as e:
        log.warning("mail check failed for %s: %s", chat_id, e)
    except Exception:
        log.exception("watcher crashed for %s", chat_id)


def watcher_loop():
    wpool = ThreadPoolExecutor(max_workers=5)
    tick = 0
    while True:
        time.sleep(POLL_SECONDS)
        try:
            ids = [u["chat_id"] for u in db.all_users()]
            if ids:
                list(wpool.map(check_user_safe, ids))
            tick += 1
            if tick % 500 == 0:
                db.cleanup()
        except Exception:
            log.exception("watcher loop error")


# --------------------------------------------------------------------------- web view (Open in Browser)
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{title}</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0e1a1f;color:#e8f1f3}}
.w{{max-width:760px;margin:0 auto;padding:16px}}
h1{{font-size:1.15rem;margin:8px 0}} .m{{color:#8fa9b3;font-size:.85rem;margin:2px 0;word-break:break-all}}
iframe{{width:100%;height:78vh;border:0;border-radius:12px;background:#fff;margin-top:12px}}
pre{{white-space:pre-wrap;word-break:break-word;background:#14262d;padding:12px;border-radius:12px}}
</style></head><body><div class="w"><h1>{title}</h1>
<div class="m">From: {sender}</div><div class="m">{when}</div>{body}</div></body></html>"""


def render_page(m) -> str:
    if m["html"]:
        inner = ('<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" '
                 'srcdoc="' + html.escape('<base target="_blank"><meta name="viewport" content="width=device-width,initial-scale=1">'
                                          '<style>body{font-family:system-ui,sans-serif;word-break:break-word;margin:12px}'
                                          'img{max-width:100%;height:auto}</style>' + m["html"], quote=True) + '"></iframe>')
    else:
        inner = "<pre>" + esc(m["text"] or "(this email has no text)") + "</pre>"
    return PAGE.format(title=esc(m["subject"] or "(no subject)"), sender=esc(fmt_from(m)),
                       when=esc(fmt_time(m["created"])), body=inner)


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health"):
            self._send(200, "Temp Mail Bot is running ✅", "text/plain; charset=utf-8")
            return
        if path.startswith("/m/"):
            link = db.get_link(path[3:])
            if not link:
                self._send(404, "Link not found or expired.", "text/plain; charset=utf-8")
                return
            try:
                m = call_mail(link["chat_id"], "read", link["msg_id"])
            except Exception:
                self._send(502, "Couldn't load this email (it may have expired).", "text/plain; charset=utf-8")
                return
            self._send(200, render_page(m))
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")


def start_web():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), WebHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("web server on port %s (public url: %s)", PORT, PUBLIC_URL or "not set")


# --------------------------------------------------------------------------- main
def poll_forever():
    offset = None
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for u in tg.call("getUpdates", http_timeout=45, **params):
                offset = u["update_id"] + 1
                pool.submit(safe_handle, u)
        except TgError as e:
            if e.code == 401:
                log.error("BOT_TOKEN is wrong or revoked. Fix it in your variables.")
                time.sleep(30)
            elif e.code == 409:
                log.error("Another copy of this bot is running with the same token. Stop the other one.")
                time.sleep(10)
            else:
                log.warning("getUpdates: %s", e)
                time.sleep(3)
        except Exception:
            log.exception("poll error")
            time.sleep(3)


def main():
    global db
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing. Add it in your hosting variables (get it from @BotFather).")
        sys.exit(1)
    db = DB(DB_PATH)
    start_web()
    try:
        tg.call("deleteWebhook")
        me = tg.call("getMe")
        log.info("bot started: @%s", me.get("username"))
        tg.call("setMyCommands", commands=[
            {"command": "start", "description": "Start / show my inbox"},
            {"command": "new", "description": "Generate new / delete email"},
            {"command": "refresh", "description": "Refresh inbox"},
            {"command": "delete", "description": "Delete my email"},
        ])
    except TgError as e:
        log.error("startup problem: %s", e)
    for cid in ALLOWED:
        try:
            tg.send(cid, "✅ Temp Mail Bot is online.\nTap <b>🔄 Refresh</b> or send /start.", MAIN_KB)
        except TgError:
            pass
    threading.Thread(target=watcher_loop, daemon=True).start()
    poll_forever()


if __name__ == "__main__":
    main()
