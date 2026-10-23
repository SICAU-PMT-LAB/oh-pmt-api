"""SQLite 存储：账号、成员、API Key、用量（积分）、设置。"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager

TABLES = """
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
    -- 积分快照，由上游 quota 接口刷新
    credits_total   REAL DEFAULT 0,
    credits_used    REAL DEFAULT 0,
    credits_remaining REAL DEFAULT 0,
    credits_addon   REAL DEFAULT 0,
    plan_name       TEXT DEFAULT '',
    credits_synced_at REAL DEFAULT 0,
    created_at      REAL NOT NULL,
    UNIQUE(uid, region)
);

CREATE TABLE IF NOT EXISTS members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    email           TEXT DEFAULT '',
    -- 登录账号：用户名 + 密码哈希（普通成员和控制台都用它登录）
    username        TEXT UNIQUE,
    password_hash   TEXT DEFAULT '',
    is_admin        INTEGER DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    -- 限额（积分），0 表示不限
    limit_daily     REAL DEFAULT 0,
    limit_weekly    REAL DEFAULT 0,
    -- 优先使用的账号（软绑定，超额可回退到其他账号）
    preferred_account_id INTEGER,
    -- 默认可用模型，逗号分隔；空=全部
    model_allowlist TEXT DEFAULT '',
    note            TEXT DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    member_id       INTEGER,
    enabled         INTEGER DEFAULT 1,
    ip_allowlist    TEXT DEFAULT '',
    model_allowlist TEXT DEFAULT '',
    note            TEXT DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id          INTEGER,
    member_id       INTEGER,
    account_id      INTEGER,
    model           TEXT DEFAULT '',
    prompt_tokens   INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    credits         REAL DEFAULT 0,
    credits_original REAL DEFAULT 0,
    status          INTEGER DEFAULT 200,
    ip              TEXT DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_usage_member_time ON usage(member_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_account_time ON usage(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage(created_at);
"""

# 默认可用模型：三个几乎不消耗积分的 Flash 档
DEFAULT_MODELS = "qfmodel,gfmodel,dfmodel"


def day_start(ts: float | None = None) -> float:
    """当天 00:00 的时间戳（本地时区）。"""
    t = time.localtime(ts if ts is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def week_start(ts: float | None = None) -> float:
    """本周一 00:00 的时间戳。"""
    t = time.localtime(ts if ts is not None else time.time())
    monday = t.tm_mday - (t.tm_wday or 0)
    return time.mktime((t.tm_year, t.tm_mon, monday, 0, 0, 0, 0, 0, -1))


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
            # 先建表（旧库已存在的表不会被改写）
            c.executescript(TABLES)
            # 再补列，否则下面的索引会因为缺列而建失败
            self._migrate(c)
            # 最后建索引
            c.executescript(INDEXES)

    @staticmethod
    def _migrate(c):
        """旧库升级：补上新增的列。CREATE TABLE IF NOT EXISTS 不会改已有表。"""
        def columns(table: str) -> set[str]:
            return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}

        # accounts 新增积分快照列
        acc_cols = columns("accounts")
        for name, decl in (
            ("credits_total", "REAL DEFAULT 0"),
            ("credits_used", "REAL DEFAULT 0"),
            ("credits_remaining", "REAL DEFAULT 0"),
            ("credits_addon", "REAL DEFAULT 0"),
            ("plan_name", "TEXT DEFAULT ''"),
            ("credits_synced_at", "REAL DEFAULT 0"),
        ):
            if name not in acc_cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")

        # api_keys 从 Key 级限额改为成员级
        key_cols = columns("api_keys")
        for name, decl in (
            ("member_id", "INTEGER"),
            ("note", "TEXT DEFAULT ''"),
        ):
            if name not in key_cols:
                c.execute(f"ALTER TABLE api_keys ADD COLUMN {name} {decl}")

        # members 新增账号密码登录
        mem_cols = columns("members")
        for name, decl in (
            ("username", "TEXT"),
            ("password_hash", "TEXT DEFAULT ''"),
            ("is_admin", "INTEGER DEFAULT 0"),
        ):
            if name not in mem_cols:
                c.execute(f"ALTER TABLE members ADD COLUMN {name} {decl}")

        # usage 新增记账列
        use_cols = columns("usage")
        for name, decl in (
            ("member_id", "INTEGER"),
            ("credits", "REAL DEFAULT 0"),
            ("credits_original", "REAL DEFAULT 0"),
        ):
            if name not in use_cols:
                c.execute(f"ALTER TABLE usage ADD COLUMN {name} {decl}")

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
            if cur.lastrowid:
                return cur.lastrowid
            row = c.execute(
                "SELECT id FROM accounts WHERE uid=? AND region=?", (uid, region)
            ).fetchone()
            return int(row["id"])

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

    def pick_account(self, region: str, prefer_id: int | None = None) -> dict | None:
        """选账号：优先指定的那个，不可用时回退到同区域最久未用的。"""
        now = time.time()
        with self._conn() as c:
            if prefer_id:
                r = c.execute(
                    """SELECT * FROM accounts WHERE id=? AND region=? AND enabled=1
                       AND cooldown_until < ?""",
                    (prefer_id, region, now),
                ).fetchone()
                if r:
                    return dict(r)
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

    def set_account_credits(self, account_id: int, *, total: float, used: float,
                            remaining: float, addon: float, plan: str = ""):
        with self._conn() as c:
            c.execute(
                """UPDATE accounts SET credits_total=?, credits_used=?,
                   credits_remaining=?, credits_addon=?, plan_name=?,
                   credits_synced_at=? WHERE id=?""",
                (total, used, remaining, addon, plan, time.time(), account_id),
            )

    def set_account_cooldown(self, account_id: int, seconds: int, error: str = ""):
        with self._conn() as c:
            c.execute(
                "UPDATE accounts SET cooldown_until=?, last_error=? WHERE id=?",
                (time.time() + seconds, error[:500], account_id),
            )

    def set_account_enabled(self, account_id: int, enabled: bool):
        with self._conn() as c:
            c.execute("UPDATE accounts SET enabled=? WHERE id=?",
                      (1 if enabled else 0, account_id))

    def delete_account(self, account_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    # ---------------- 成员 ----------------

    def create_member(self, *, name, email="", limit_daily=0.0, limit_weekly=0.0,
                      preferred_account_id=None, model_allowlist=DEFAULT_MODELS,
                      note="", username=None, password=None, is_admin=0) -> int:
        pwd_hash = self.hash_password(password) if password else ""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO members (name, email, limit_daily, limit_weekly,
                     preferred_account_id, model_allowlist, note, username,
                     password_hash, is_admin, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (name, email, limit_daily, limit_weekly, preferred_account_id,
                 model_allowlist, note, username, pwd_hash, is_admin, time.time()),
            )
            return int(cur.lastrowid)

    # ---------------- 账号密码 ----------------

    @staticmethod
    def hash_password(password: str) -> str:
        """PBKDF2-HMAC-SHA256，带随机 salt，格式 pbkdf2$迭代$salt$hash。"""
        salt = secrets.token_bytes(16)
        rounds = 200_000
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
        return f"pbkdf2${rounds}${salt.hex()}${dk.hex()}"

    @staticmethod
    def verify_password(password: str, stored: str) -> bool:
        if not stored or not stored.startswith("pbkdf2$"):
            return False
        try:
            _, rounds, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(salt_hex), int(rounds))
        except (ValueError, TypeError):
            return False
        return secrets.compare_digest(dk.hex(), hash_hex)

    def set_password(self, member_id: int, password: str):
        with self._conn() as c:
            c.execute("UPDATE members SET password_hash=? WHERE id=?",
                      (self.hash_password(password), member_id))

    def get_member_by_username(self, username: str) -> dict | None:
        if not username:
            return None
        with self._conn() as c:
            row = c.execute("SELECT * FROM members WHERE username=?",
                            (username,)).fetchone()
            return dict(row) if row else None

    def get_admin(self) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM members WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
            return dict(row) if row else None

    def list_members(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM members ORDER BY id")]

    def get_member(self, member_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            return dict(r) if r else None

    def update_member(self, member_id: int, **fields):
        allowed = {"name", "email", "enabled", "limit_daily", "limit_weekly",
                   "preferred_account_id", "model_allowlist", "note",
                   "username", "is_admin"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        sql = "UPDATE members SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
        with self._conn() as c:
            c.execute(sql, (*sets.values(), member_id))

    def delete_member(self, member_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM members WHERE id=?", (member_id,))
            c.execute("DELETE FROM api_keys WHERE member_id=?", (member_id,))

    # ---------------- API Key ----------------

    @staticmethod
    def hash_key(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    KEY_PREFIX = "sicau-PMT-"
    KEY_MARKER = "487"

    @classmethod
    def generate_key(cls) -> str:
        """生成密钥：sicau-PMT- 开头，487 随机藏在中间乱码里。

        487 不紧跟前缀，而是插在随机段的随机位置，看起来就是普通随机字符。
        """
        body = secrets.token_urlsafe(48).replace("-", "").replace("_", "")[:32]
        pos = secrets.randbelow(len(body) - len(cls.KEY_MARKER) + 1)
        if pos == 0:
            pos = 1  # 不要紧跟前缀
        body = body[:pos] + cls.KEY_MARKER + body[pos:]
        return f"{cls.KEY_PREFIX}{body}"

    def create_key(self, *, member_id=None, ip_allowlist="", model_allowlist="",
                   note="") -> tuple[int, str]:
        raw = self.generate_key()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO api_keys (key_hash, key_prefix, member_id,
                     ip_allowlist, model_allowlist, note, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (self.hash_key(raw), raw[:16], member_id, ip_allowlist,
                 model_allowlist, note, time.time()),
            )
            return int(cur.lastrowid), raw

    def list_keys(self, member_id: int | None = None) -> list[dict]:
        with self._conn() as c:
            if member_id:
                rows = c.execute("SELECT * FROM api_keys WHERE member_id=? ORDER BY id",
                                 (member_id,))
            else:
                rows = c.execute("SELECT * FROM api_keys ORDER BY id")
            return [dict(r) for r in rows]

    def get_key_by_hash(self, key_hash: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM api_keys WHERE key_hash=?",
                          (key_hash,)).fetchone()
            return dict(r) if r else None

    def get_key(self, key_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            return dict(r) if r else None

    def update_key(self, key_id: int, **fields):
        allowed = {"enabled", "ip_allowlist", "model_allowlist", "note", "member_id"}
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

    def record_usage(self, *, key_id, member_id, account_id, model,
                     prompt_tokens, completion_tokens, credits=0.0,
                     credits_original=0.0, status=200, ip=""):
        total = prompt_tokens + completion_tokens
        with self._conn() as c:
            c.execute(
                """INSERT INTO usage (key_id, member_id, account_id, model,
                     prompt_tokens, completion_tokens, total_tokens, credits,
                     credits_original, status, ip, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key_id, member_id, account_id, model, prompt_tokens,
                 completion_tokens, total, float(credits), float(credits_original),
                 status, ip, time.time()),
            )

    def credits_since(self, member_id: int, since: float) -> float:
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(credits),0) FROM usage WHERE member_id=? AND created_at>=?",
                (member_id, since),
            ).fetchone()
            return float(r[0])

    def credits_today(self, member_id: int) -> float:
        return self.credits_since(member_id, day_start())

    def credits_week(self, member_id: int) -> float:
        return self.credits_since(member_id, week_start())

    def credits_total(self, member_id: int) -> float:
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(credits),0) FROM usage WHERE member_id=?",
                (member_id,),
            ).fetchone()
            return float(r[0])

    def credits_by_account(self, since: float = 0) -> dict[int, float]:
        """每个账号被消耗的积分（用于账号归属统计）。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT account_id, COALESCE(SUM(credits),0) c FROM usage
                   WHERE created_at>=? GROUP BY account_id""", (since,)
            )
            return {int(r["account_id"]): float(r["c"]) for r in rows}

    def model_summary(self, since: float | None = None) -> list[dict]:
        since = since if since is not None else time.time() - 7 * 86400
        with self._conn() as c:
            rows = c.execute(
                """SELECT model, COUNT(*) n, SUM(total_tokens) tokens,
                     SUM(credits) credits FROM usage WHERE created_at>=?
                   GROUP BY model ORDER BY credits DESC""", (since,)
            )
            return [dict(r) for r in rows]

    def daily_series(self, days: int = 14) -> list[dict]:
        """最近 N 天的每日请求数与积分，供图表使用。"""
        out = []
        now = time.time()
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            end = start + 86400
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n, COALESCE(SUM(credits),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE created_at>=? AND created_at<?""", (start, end)
                ).fetchone()
            out.append({
                "date": time.strftime("%m-%d", time.localtime(start)),
                "requests": int(r["n"]),
                "credits": round(float(r["c"]), 4),
                "tokens": int(r["t"]),
            })
        return out

    def member_summary(self, since: float | None = None) -> list[dict]:
        since = since if since is not None else time.time() - 7 * 86400
        with self._conn() as c:
            rows = c.execute(
                """SELECT member_id, COUNT(*) n, COALESCE(SUM(credits),0) credits,
                     COALESCE(SUM(total_tokens),0) tokens FROM usage
                   WHERE created_at>=? GROUP BY member_id""", (since,)
            )
            return [dict(r) for r in rows]

    def recent_usage(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def usage_for_member(self, member_id: int, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM usage WHERE member_id=? ORDER BY id DESC LIMIT ?",
                (member_id, limit),
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
