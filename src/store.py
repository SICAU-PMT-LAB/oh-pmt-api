"""SQLite 存储：账号、API Key、用量记录、限额。"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT NOT NULL,
    region          TEXT NOT NULL,
    email           TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    pat             TEXT NOT NULL,
    access_token    TEXT DEFAULT '',
    refresh_token   TEXT DEFAULT '',
    expires_at      REAL DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    last_error      TEXT DEFAULT '',
    last_used_at    REAL DEFAULT 0,
    cooldown_until  REAL DEFAULT 0,
    created_at      REAL NOT NULL,
    UNIQUE(uid, region)
);

CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    label           TEXT DEFAULT '',
    enabled         INTEGER DEFAULT 1,
    -- 限额：0 表示不限
    token_limit_daily   INTEGER DEFAULT 0,
    token_limit_total   INTEGER DEFAULT 0,
    ip_allowlist    TEXT DEFAULT '',   -- 逗号分隔，支持 CIDR
    model_allowlist TEXT DEFAULT '',   -- 逗号分隔，空=全部
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id          INTEGER,
    account_id      INTEGER,
    model           TEXT DEFAULT '',
    prompt_tokens   INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    status          INTEGER DEFAULT 200,
    ip              TEXT DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_key_time ON usage(key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self):
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ---------------- 账号 ----------------

    def add_account(self, *, uid, region, pat, email="", name="") -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO accounts (uid, region, pat, email, name, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(uid, region) DO UPDATE SET
                     pat=excluded.pat, email=excluded.email, name=excluded.name,
                     last_error='', cooldown_until=0""",
                (uid, region, pat, email, name, time.time()),
            )
            return cur.lastrowid or 0

    def list_accounts(self, region: str | None = None) -> list[dict]:
        with self._conn() as c:
            sql = "SELECT * FROM accounts"
            args: tuple = ()
            if region:
                sql += " WHERE region=?"
                args = (region,)
            return [dict(r) for r in c.execute(sql + " ORDER BY id")]

    def get_account(self, account_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(r) if r else None

    def pick_account(self, region: str) -> dict | None:
        """选一个可用账号：启用中、不在冷却期，优先最久未用。"""
        now = time.time()
        with self._conn() as c:
            r = c.execute(
                """SELECT * FROM accounts
                   WHERE region=? AND enabled=1 AND cooldown_until < ?
                   ORDER BY last_used_at ASC, id ASC LIMIT 1""",
                (region, now),
            ).fetchone()
            return dict(r) if r else None

    def touch_account(self, account_id: int):
        with self._conn() as c:
            c.execute("UPDATE accounts SET last_used_at=? WHERE id=?",
                      (time.time(), account_id))

    def set_account_tokens(self, account_id: int, access_token: str,
                           refresh_token: str, expires_at: float):
        with self._conn() as c:
            c.execute(
                """UPDATE accounts SET access_token=?, refresh_token=?, expires_at=?,
                   last_error='' WHERE id=?""",
                (access_token, refresh_token, expires_at, account_id),
            )

    def set_account_cooldown(self, account_id: int, seconds: int, error: str = ""):
        with self._conn() as c:
            c.execute(
                "UPDATE accounts SET cooldown_until=?, last_error=? WHERE id=?",
                (time.time() + seconds, error[:500], account_id),
            )

    def set_account_enabled(self, account_id: int, enabled: bool):
        with self._conn() as c:
            c.execute("UPDATE accounts SET enabled=? WHERE id=?", (1 if enabled else 0, account_id))

    def delete_account(self, account_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    # ---------------- API Key ----------------

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    def create_key(self, *, label="", token_limit_daily=0, token_limit_total=0,
                   ip_allowlist="", model_allowlist="") -> tuple[int, str]:
        raw = "qk-" + secrets.token_urlsafe(32)
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO api_keys (key_hash, key_prefix, label, token_limit_daily,
                     token_limit_total, ip_allowlist, model_allowlist, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (self.hash_key(raw), raw[:11], label, token_limit_daily,
                 token_limit_total, ip_allowlist, model_allowlist, time.time()),
            )
            return cur.lastrowid, raw

    def list_keys(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM api_keys ORDER BY id")]

    def get_key_by_hash(self, key_hash: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,)).fetchone()
            return dict(r) if r else None

    def get_key(self, key_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            return dict(r) if r else None

    def update_key(self, key_id: int, **fields):
        allowed = {"label", "enabled", "token_limit_daily", "token_limit_total",
                   "ip_allowlist", "model_allowlist"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sql = "UPDATE api_keys SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
        with self._conn() as c:
            c.execute(sql, (*sets.values(), key_id))

    def delete_key(self, key_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM api_keys WHERE id=?", (key_id,))

    # ---------------- 用量 ----------------

    def record_usage(self, *, key_id, account_id, model, prompt_tokens,
                     completion_tokens, status=200, ip=""):
        total = prompt_tokens + completion_tokens
        with self._conn() as c:
            c.execute(
                """INSERT INTO usage (key_id, account_id, model, prompt_tokens,
                     completion_tokens, total_tokens, status, ip, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (key_id, account_id, model, prompt_tokens, completion_tokens,
                 total, status, ip, time.time()),
            )

    def tokens_used_since(self, key_id: int, since: float) -> int:
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(total_tokens),0) FROM usage WHERE key_id=? AND created_at>=?",
                (key_id, since),
            ).fetchone()
            return int(r[0])

    def tokens_used_total(self, key_id: int) -> int:
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(total_tokens),0) FROM usage WHERE key_id=?",
                (key_id,),
            ).fetchone()
            return int(r[0])

    def usage_summary(self, key_id: int | None = None, days: int = 7) -> list[dict]:
        since = time.time() - days * 86400
        with self._conn() as c:
            if key_id:
                rows = c.execute(
                    """SELECT model, COUNT(*) n, SUM(total_tokens) tokens
                       FROM usage WHERE key_id=? AND created_at>=? GROUP BY model""",
                    (key_id, since),
                )
            else:
                rows = c.execute(
                    """SELECT model, COUNT(*) n, SUM(total_tokens) tokens
                       FROM usage WHERE created_at>=? GROUP BY model""", (since,)
                )
            return [dict(r) for r in rows]

    def recent_usage(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in rows]

    def purge_usage_before(self, before: float):
        with self._conn() as c:
            c.execute("DELETE FROM usage WHERE created_at < ?", (before,))

    # ---------------- 设置 ----------------

    def get_setting(self, key: str, default=None):
        with self._conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings (key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
