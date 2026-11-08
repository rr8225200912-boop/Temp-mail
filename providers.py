"""Temp mail providers. Pure standard library, no pip packages needed.

Order of use: mail.tm -> mail.gw -> Guerrilla Mail. If one is down, the next one is tried.
Every provider returns messages in the same simple dict format.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import string
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parseaddr

LOWER = string.ascii_lowercase + string.digits
ALNUM = string.ascii_letters + string.digits
UA = "Mozilla/5.0 (compatible; TempMailBot/1.0)"


def rand(n: int, chars: str) -> str:
    return "".join(secrets.choice(chars) for _ in range(n))


class MailError(Exception):
    def __init__(self, message: str, status: int | None = None, network: bool = False):
        super().__init__(message)
        self.status = status
        self.network = network


def req(method, url, *, body=None, params=None, headers=None, timeout=15):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise MailError(f"HTTP {e.code}", status=e.code)
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        raise MailError(f"network: {type(e).__name__}", network=True)
    if status == 204 or not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _members(d):
    if isinstance(d, dict):
        return d.get("hydra:member") or d.get("member") or []
    return d or []


def _iso_from_ts(v):
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


class MailTm:
    """mail.tm and mail.gw share the same API."""

    def __init__(self, name: str, base: str):
        self.name = name
        self.base = base.rstrip("/")
        self._domain = None

    def _h(self, acc):
        return {"Authorization": "Bearer " + acc["token"]}

    def _get_domain(self):
        if not self._domain:
            items = [x for x in _members(req("GET", self.base + "/domains", params={"page": "1"}))
                     if isinstance(x, dict) and x.get("isActive", True)]
            if not items:
                raise MailError("no active domain")
            self._domain = items[0]["domain"]
        return self._domain

    def create(self, username=None):
        domain = self._get_domain()
        address = f"{username or rand(10, LOWER)}@{domain}"
        password = rand(18, ALNUM)
        try:
            acc = req("POST", self.base + "/accounts", body={"address": address, "password": password})
        except MailError as e:
            if e.status != 422 or not username:
                self._domain = None  # domain may have changed, look it up again next time
            raise
        tk = req("POST", self.base + "/token", body={"address": address, "password": password})
        return {
            "provider": self.name,
            "acc_id": (acc or {}).get("id") or tk.get("id") or "",
            "address": address,
            "password": password,
            "token": tk["token"],
        }

    def login(self, acc):
        tk = req("POST", self.base + "/token", body={"address": acc["address"], "password": acc["password"]})
        return tk["token"]

    def list(self, acc):
        d = req("GET", self.base + "/messages", params={"page": "1"}, headers=self._h(acc))
        out = []
        for m in _members(d):
            f = m.get("from") or {}
            out.append({
                "id": m["id"],
                "from_name": f.get("name") or "",
                "from_addr": f.get("address") or "",
                "subject": m.get("subject") or "",
                "created": m.get("createdAt") or "",
            })
        return out

    def read(self, acc, mid):
        m = req("GET", f"{self.base}/messages/{mid}", headers=self._h(acc))
        f = m.get("from") or {}
        html_part = m.get("html")
        if isinstance(html_part, list):
            html_part = "".join(html_part)
        return {
            "id": mid,
            "from_name": f.get("name") or "",
            "from_addr": f.get("address") or "",
            "subject": m.get("subject") or "",
            "created": m.get("createdAt") or "",
            "html": html_part or "",
            "text": m.get("text") or "",
        }

    def delete_account(self, acc):
        if acc.get("acc_id"):
            req("DELETE", f"{self.base}/accounts/{acc['acc_id']}", headers=self._h(acc))


class Guerrilla:
    name = "guerrilla"

    def __init__(self, base: str):
        self.base = base

    def _call(self, f, **p):
        params = {"f": f, "ip": "127.0.0.1", "agent": "tempmailbot"}
        params.update(p)
        d = req("GET", self.base, params=params)
        if not isinstance(d, dict):
            raise MailError("bad response")
        return d

    def create(self, username=None):
        d = self._call("get_email_address", lang="en")
        sid, addr = d.get("sid_token"), d.get("email_addr")
        if not sid or not addr:
            raise MailError("bad response")
        if username:
            r = self._call("set_email_user", email_user=username, lang="en", sid_token=sid)
            addr = r.get("email_addr") or addr
            sid = r.get("sid_token") or sid
        return {"provider": self.name, "acc_id": addr, "address": addr, "password": "", "token": sid}

    def login(self, acc):
        return acc["token"]

    def list(self, acc):
        d = self._call("check_email", seq="0", sid_token=acc["token"])
        lst = d.get("list")
        if not isinstance(lst, list):
            raise MailError("bad response", status=401)
        out = []
        for m in lst:
            name, addr = parseaddr(m.get("mail_from") or "")
            out.append({
                "id": str(m.get("mail_id")),
                "from_name": name,
                "from_addr": addr or (m.get("mail_from") or ""),
                "subject": m.get("mail_subject") or "",
                "created": _iso_from_ts(m.get("mail_timestamp")),
            })
        return out

    def read(self, acc, mid):
        m = self._call("fetch_email", email_id=mid, sid_token=acc["token"])
        if "mail_body" not in m:
            raise MailError("bad response")
        name, addr = parseaddr(m.get("mail_from") or "")
        return {
            "id": mid,
            "from_name": name,
            "from_addr": addr or (m.get("mail_from") or ""),
            "subject": m.get("mail_subject") or "",
            "created": _iso_from_ts(m.get("mail_timestamp")),
            "html": m.get("mail_body") or "",
            "text": "",
        }

    def delete_account(self, acc):
        return None


PROVIDERS = [
    MailTm("mail.tm", os.environ.get("MAILTM_BASE", "https://api.mail.tm")),
    MailTm("mail.gw", os.environ.get("MAILGW_BASE", "https://api.mail.gw")),
    Guerrilla(os.environ.get("GUERRILLA_BASE", "https://api.guerrillamail.com/ajax.php")),
]


def get_provider(name: str):
    for p in PROVIDERS:
        if p.name == name:
            return p
    return PROVIDERS[0]


def create_account(username=None):
    """Try each provider in turn. A taken/invalid custom name (HTTP 422) is not retried elsewhere."""
    errors = []
    for p in PROVIDERS:
        try:
            return p.create(username)
        except MailError as e:
            if e.status == 422 and username:
                raise
            errors.append(f"{p.name}: {e}")
    raise MailError("; ".join(errors))
