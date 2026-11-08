"""Tiny SQLite storage. Keeps each chat's mailbox, which mails were already announced, and web-view links."""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time


class DB:
    def __init__(self, path: str):
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.c.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    chat_id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    acc_id TEXT,
                    address TEXT NOT NULL,
                    password TEXT,
                    token TEXT,
                    created REAL
                );
                CREATE TABLE IF NOT EXISTS seen(
                    chat_id INTEGER, msg_id TEXT,
                    PRIMARY KEY(chat_id, msg_id)
                );
                CREATE TABLE IF NOT EXISTS links(
                    key TEXT PRIMARY KEY, chat_id INTEGER, msg_id TEXT, created REAL
                );
                """
            )
            self.c.commit()

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def get_user(self, chat_id):
        rows = self._all("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        return rows[0] if rows else None

    def all_users(self):
        return self._all("SELECT * FROM users")

    def set_user(self, chat_id, acc):
        with self.lock:
            self.c.execute("DELETE FROM seen WHERE chat_id=?", (chat_id,))
            self.c.execute("DELETE FROM links WHERE chat_id=?", (chat_id,))
            self.c.execute(
                "INSERT OR REPLACE INTO users(chat_id,provider,acc_id,address,password,token,created) VALUES(?,?,?,?,?,?,?)",
                (chat_id, acc["provider"], acc.get("acc_id", ""), acc["address"], acc.get("password", ""),
                 acc.get("token", ""), time.time()),
            )
            self.c.commit()

    def update_token(self, chat_id, token):
        self._x("UPDATE users SET token=? WHERE chat_id=?", (token, chat_id))

    def del_user(self, chat_id):
        with self.lock:
            self.c.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
            self.c.execute("DELETE FROM seen WHERE chat_id=?", (chat_id,))
            self.c.execute("DELETE FROM links WHERE chat_id=?", (chat_id,))
            self.c.commit()

    def is_seen(self, chat_id, msg_id):
        return bool(self._all("SELECT 1 FROM seen WHERE chat_id=? AND msg_id=?", (chat_id, str(msg_id))))

    def mark_seen(self, chat_id, msg_id):
        self._x("INSERT OR IGNORE INTO seen(chat_id,msg_id) VALUES(?,?)", (chat_id, str(msg_id)))

    def make_link(self, chat_id, msg_id):
        rows = self._all("SELECT key FROM links WHERE chat_id=? AND msg_id=?", (chat_id, str(msg_id)))
        if rows:
            return rows[0]["key"]
        key = secrets.token_urlsafe(18)
        self._x("INSERT INTO links(key,chat_id,msg_id,created) VALUES(?,?,?,?)", (key, chat_id, str(msg_id), time.time()))
        return key

    def get_link(self, key):
        rows = self._all("SELECT * FROM links WHERE key=?", (key,))
        return rows[0] if rows else None

    def cleanup(self):
        self._x("DELETE FROM links WHERE created < ?", (time.time() - 7 * 86400,))
