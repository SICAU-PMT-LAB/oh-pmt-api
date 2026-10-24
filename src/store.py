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
    -- 账号来源厂商：qoder / workbuddy / cline / commandcode
    provider        TEXT NOT NULL DEFAULT 'qoder',
    uid             TEXT NOT NULL,
    region          TEXT NOT NULL,
    email           TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    -- 长期凭证：Qoder 是 pt- 开头的 PAT，其他厂商是各自的 API Key
    credential      TEXT NOT NULL,
    access_token    TEXT DEFAULT '',
    refresh_token   TEXT DEFAULT '',
    expires_at      REAL DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    last_error      TEXT DEFAULT '',
    last_used_at    REAL DEFAULT 0,
    cooldown_until  REAL DEFAULT 0,
    -- 积分快照，由上游 quota 接口刷新（total/used/remaining 是全部额度合计）
    credits_total   REAL DEFAULT 0,
    credits_used    REAL DEFAULT 0,
    credits_remaining REAL DEFAULT 0,
    credits_addon   REAL DEFAULT 0,
    -- 分项：基础套餐 / 赠送与签到 / 共享池
    credits_base_total REAL DEFAULT 0,
    credits_base_used REAL DEFAULT 0,
    credits_base_remaining REAL DEFAULT 0,
    credits_addon_total REAL DEFAULT 0,
    credits_addon_used REAL DEFAULT 0,
    credits_shared_total REAL DEFAULT 0,
    credits_shared_used REAL DEFAULT 0,
    credits_shared_remaining REAL DEFAULT 0,
    credits_expire_at REAL DEFAULT 0,
    credits_exceeded INTEGER DEFAULT 0,
    plan_name       TEXT DEFAULT '',
    credits_synced_at REAL DEFAULT 0,
    -- 最近一次签到结果（目前只有 Qoder 有签到活动）
    checkin_at      REAL DEFAULT 0,
    checkin_ok      INTEGER DEFAULT 0,
    checkin_earned  REAL DEFAULT 0,
    checkin_message TEXT DEFAULT '',
    created_at      REAL NOT NULL,
    -- 同一厂商下同一个 uid 在同一个区域只算一个账号
    UNIQUE(provider, uid, region)
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
    -- 明文密钥，为了让管理员随时能查看；数据库泄露即等于密钥泄露
    key_value       TEXT DEFAULT '',
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
    -- 200 = 成功；其余是失败（error 存错误码，如 upstream_busy）
    status          INTEGER DEFAULT 200,
    error           TEXT DEFAULT '',
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


def _short_num(value) -> str:
    """图表刻度用的紧凑写法：1200 → 1.2k，64000 → 64k。"""
    v = float(value or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:g}M"
    if v >= 1000:
        return f"{v / 1000:g}k"
    return f"{v:g}"


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

        # accounts 新增积分快照列 / 厂商列
        acc_cols = columns("accounts")
        # 老库是单厂商的：pat 列改名为 credential，再补 provider 并换成
        # (provider, uid, region) 唯一约束。SQLite 改不了约束，只能重建表。
        # 两种老版本都要接：最初的库只有基础列，后来的库多了积分快照列。
        if "provider" not in acc_cols or "credential" not in acc_cols:
            leg = "pat"
            # 源列：credential 来自 pat（或已是 credential），provider 固定 'qoder'，
            # 其他列老库里没有就用默认值 0 / ''。
            src = (("id", "id"), ("uid", "uid"), ("region", "region"),
                   ("email", "email"), ("name", "name"),
                   ("credential", leg if leg in acc_cols else "''"),
                   ("access_token", "access_token"),
                   ("refresh_token", "refresh_token"),
                   ("expires_at", "expires_at"), ("enabled", "enabled"),
                   ("last_error", "last_error"),
                   ("last_used_at", "last_used_at"),
                   ("cooldown_until", "cooldown_until"),
                   ("credits_total", "credits_total"),
                   ("credits_used", "credits_used"),
                   ("credits_remaining", "credits_remaining"),
                   ("credits_addon", "credits_addon"),
                   ("credits_base_total", "credits_base_total"),
                   ("credits_base_used", "credits_base_used"),
                   ("credits_base_remaining", "credits_base_remaining"),
                   ("credits_addon_total", "credits_addon_total"),
                   ("credits_addon_used", "credits_addon_used"),
                   ("credits_shared_total", "credits_shared_total"),
                   ("credits_shared_used", "credits_shared_used"),
                   ("credits_shared_remaining", "credits_shared_remaining"),
                   ("credits_expire_at", "credits_expire_at"),
                   ("credits_exceeded", "credits_exceeded"),
                   ("plan_name", "plan_name"),
                   ("credits_synced_at", "credits_synced_at"),
                   ("checkin_at", "checkin_at"),
                   ("checkin_ok", "checkin_ok"),
                   ("checkin_earned", "checkin_earned"),
                   ("checkin_message", "checkin_message"),
                   ("created_at", "created_at"))
            targets = "provider, " + ", ".join(t for t, _ in src)
            sources = "'qoder', " + ", ".join(
                s if s in acc_cols or s == "''" else "0" for _, s in src)
            c.executescript(f"""
            ALTER TABLE accounts RENAME TO accounts_legacy;
            CREATE TABLE accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                provider        TEXT NOT NULL DEFAULT 'qoder',
                uid             TEXT NOT NULL,
                region          TEXT NOT NULL,
                email           TEXT DEFAULT '',
                name            TEXT DEFAULT '',
                credential      TEXT NOT NULL DEFAULT '',
                access_token    TEXT DEFAULT '',
                refresh_token   TEXT DEFAULT '',
                expires_at      REAL DEFAULT 0,
                enabled         INTEGER DEFAULT 1,
                last_error      TEXT DEFAULT '',
                last_used_at    REAL DEFAULT 0,
                cooldown_until  REAL DEFAULT 0,
                credits_total   REAL DEFAULT 0,
                credits_used    REAL DEFAULT 0,
                credits_remaining REAL DEFAULT 0,
                credits_addon   REAL DEFAULT 0,
                credits_base_total REAL DEFAULT 0,
                credits_base_used REAL DEFAULT 0,
                credits_base_remaining REAL DEFAULT 0,
                credits_addon_total REAL DEFAULT 0,
                credits_addon_used REAL DEFAULT 0,
                credits_shared_total REAL DEFAULT 0,
                credits_shared_used REAL DEFAULT 0,
                credits_shared_remaining REAL DEFAULT 0,
                credits_expire_at REAL DEFAULT 0,
                credits_exceeded INTEGER DEFAULT 0,
                plan_name       TEXT DEFAULT '',
                credits_synced_at REAL DEFAULT 0,
                checkin_at      REAL DEFAULT 0,
                checkin_ok      INTEGER DEFAULT 0,
                checkin_earned  REAL DEFAULT 0,
                checkin_message TEXT DEFAULT '',
                created_at      REAL NOT NULL,
                UNIQUE(provider, uid, region)
            );
            INSERT INTO accounts ({targets})
            SELECT {sources} FROM accounts_legacy;
            DROP TABLE accounts_legacy;
            """)
            acc_cols = columns("accounts")
        for name, decl in (
            ("credits_total", "REAL DEFAULT 0"),
            ("credits_used", "REAL DEFAULT 0"),
            ("credits_remaining", "REAL DEFAULT 0"),
            ("credits_addon", "REAL DEFAULT 0"),
            ("credits_base_total", "REAL DEFAULT 0"),
            ("credits_base_used", "REAL DEFAULT 0"),
            ("credits_base_remaining", "REAL DEFAULT 0"),
            ("credits_addon_total", "REAL DEFAULT 0"),
            ("credits_addon_used", "REAL DEFAULT 0"),
            ("credits_shared_total", "REAL DEFAULT 0"),
            ("credits_shared_used", "REAL DEFAULT 0"),
            ("credits_shared_remaining", "REAL DEFAULT 0"),
            ("credits_expire_at", "REAL DEFAULT 0"),
            ("credits_exceeded", "INTEGER DEFAULT 0"),
            ("plan_name", "TEXT DEFAULT ''"),
            ("credits_synced_at", "REAL DEFAULT 0"),
            ("checkin_at", "REAL DEFAULT 0"),
            ("checkin_ok", "INTEGER DEFAULT 0"),
            ("checkin_earned", "REAL DEFAULT 0"),
            ("checkin_message", "TEXT DEFAULT ''"),
        ):
            if name not in acc_cols:
                c.execute(f"ALTER TABLE accounts ADD COLUMN {name} {decl}")

        # api_keys 从 Key 级限额改为成员级
        key_cols = columns("api_keys")
        for name, decl in (
            ("member_id", "INTEGER"),
            ("note", "TEXT DEFAULT ''"),
            ("key_value", "TEXT DEFAULT ''"),
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
            ("error", "TEXT DEFAULT ''"),
        ):
            if name not in use_cols:
                c.execute(f"ALTER TABLE usage ADD COLUMN {name} {decl}")

    # ---------------- 账号 ----------------

    def add_account(self, *, uid, region, credential, provider="qoder",
                    email="", name="") -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO accounts (provider, uid, region, credential, email,
                     name, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(provider, uid, region) DO UPDATE SET
                     credential=excluded.credential, email=excluded.email,
                     name=excluded.name, last_error='', cooldown_until=0""",
                (provider, uid, region, credential, email, name, time.time()),
            )
            if cur.lastrowid:
                return cur.lastrowid
            row = c.execute(
                "SELECT id FROM accounts WHERE provider=? AND uid=? AND region=?",
                (provider, uid, region),
            ).fetchone()
            return int(row["id"])

    def list_accounts(self, region: str | None = None,
                      provider: str | None = None) -> list[dict]:
        with self._conn() as c:
            sql = "SELECT * FROM accounts"
            args: list = []
            where = []
            if region:
                where.append("region=?")
                args.append(region)
            if provider:
                where.append("provider=?")
                args.append(provider)
            if where:
                sql += " WHERE " + " AND ".join(where)
            return [dict(r) for r in c.execute(sql + " ORDER BY id", tuple(args))]

    def get_account(self, account_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(r) if r else None

    def pick_account(self, region: str, provider: str = "qoder",
                     prefer_id: int | None = None,
                     exclude: set[int] | None = None) -> dict | None:
        """选账号：优先指定的那个，不可用时回退到同厂商同区域最久未用的。

        exclude 里的账号会被跳过，用于一个账号排队时换到别的账号重试。
        """
        now = time.time()
        skip = tuple(exclude or ())
        with self._conn() as c:
            if prefer_id and prefer_id not in skip:
                r = c.execute(
                    """SELECT * FROM accounts WHERE id=? AND provider=? AND region=?
                       AND enabled=1 AND cooldown_until < ?""",
                    (prefer_id, provider, region, now),
                ).fetchone()
                if r:
                    return dict(r)
            sql = ("SELECT * FROM accounts WHERE provider=? AND region=? "
                   "AND enabled=1 AND cooldown_until < ?")
            args = [provider, region, now]
            if skip:
                sql += " AND id NOT IN (%s)" % ",".join("?" * len(skip))
                args.extend(skip)
            sql += " ORDER BY last_used_at ASC, id ASC LIMIT 1"
            r = c.execute(sql, args).fetchone()
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
                            remaining: float, addon: float, plan: str = "",
                            base_total: float = 0, base_used: float = 0,
                            base_remaining: float = 0, addon_total: float = 0,
                            addon_used: float = 0, shared_total: float = 0,
                            shared_used: float = 0, shared_remaining: float = 0,
                            expire_at: float = 0, exceeded: bool = False):
        with self._conn() as c:
            c.execute(
                """UPDATE accounts SET credits_total=?, credits_used=?,
                   credits_remaining=?, credits_addon=?, plan_name=?,
                   credits_base_total=?, credits_base_used=?, credits_base_remaining=?,
                   credits_addon_total=?, credits_addon_used=?,
                   credits_shared_total=?, credits_shared_used=?,
                   credits_shared_remaining=?, credits_expire_at=?, credits_exceeded=?,
                   credits_synced_at=? WHERE id=?""",
                (total, used, remaining, addon, plan,
                 base_total, base_used, base_remaining,
                 addon_total, addon_used,
                 shared_total, shared_used, shared_remaining,
                 expire_at, 1 if exceeded else 0,
                 time.time(), account_id),
            )

    def set_account_checkin(self, account_id: int, *, ok: bool, earned: float = 0,
                            message: str = ""):
        with self._conn() as c:
            c.execute(
                """UPDATE accounts SET checkin_at=?, checkin_ok=?, checkin_earned=?,
                   checkin_message=? WHERE id=?""",
                (time.time(), 1 if ok else 0, float(earned or 0),
                 message[:300], account_id),
            )

    def set_account_cooldown(self, account_id: int, seconds: int, error: str = ""):
        """seconds<=0 表示立即解除冷却。"""
        until = time.time() + seconds if seconds > 0 else 0
        with self._conn() as c:
            c.execute(
                "UPDATE accounts SET cooldown_until=?, last_error=? WHERE id=?",
                (until, error[:500], account_id),
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
                """INSERT INTO api_keys (key_hash, key_value, key_prefix, member_id,
                     ip_allowlist, model_allowlist, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (self.hash_key(raw), raw, raw[:16], member_id, ip_allowlist,
                 model_allowlist, note, time.time()),
            )
            return int(cur.lastrowid), raw

    def get_key(self, key_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
            return dict(row) if row else None

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
                     credits_original=0.0, status=200, error="", ip=""):
        total = prompt_tokens + completion_tokens
        with self._conn() as c:
            c.execute(
                """INSERT INTO usage (key_id, member_id, account_id, model,
                     prompt_tokens, completion_tokens, total_tokens, credits,
                     credits_original, status, error, ip, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key_id, member_id, account_id, model, prompt_tokens,
                 completion_tokens, total, float(credits), float(credits_original),
                 int(status), (error or "")[:80], ip, time.time()),
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
                """SELECT model, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     SUM(prompt_tokens) prompt_tokens,
                     SUM(completion_tokens) completion_tokens,
                     SUM(total_tokens) tokens,
                     SUM(credits) credits FROM usage WHERE created_at>=?
                   GROUP BY model ORDER BY credits DESC, n DESC""", (since,)
            )
            return [dict(r) for r in rows]

    def usage_totals(self, since: float = 0) -> dict:
        """总请求量、成功数、Token 与积分。"""
        with self._conn() as c:
            r = c.execute(
                """SELECT COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE created_at>=?""", (since,)
            ).fetchone()
        n = int(r["n"] or 0)
        ok = int(r["ok"] or 0)
        return {
            "requests": n,
            "ok": ok,
            "errors": int(r["err"] or 0),
            "success_rate": round(ok * 100.0 / n, 2) if n else None,
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "tokens": int(r["tokens"] or 0),
            "credits": round(float(r["credits"] or 0), 4),
        }

    def account_summary(self, since: float = 0) -> dict[int, dict]:
        """每个账号的请求量 / Token / 积分，附带按模型拆分。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT account_id, model, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE created_at>=? GROUP BY account_id, model""", (since,)
            )
            out: dict[int, dict] = {}
            for r in rows:
                aid = r["account_id"]
                if aid is None:
                    continue
                cell = out.setdefault(int(aid), {
                    "requests": 0, "ok": 0, "errors": 0,
                    "tokens": 0, "credits": 0.0, "by_model": [],
                })
                n, ok = int(r["n"] or 0), int(r["ok"] or 0)
                cell["requests"] += n
                cell["ok"] += ok
                cell["errors"] += int(r["err"] or 0)
                cell["tokens"] += int(r["tokens"] or 0)
                cell["credits"] += float(r["credits"] or 0)
                cell["by_model"].append({
                    "id": r["model"] or "",
                    "requests": n, "ok": ok, "errors": int(r["err"] or 0),
                    "tokens": int(r["tokens"] or 0),
                    "credits": round(float(r["credits"] or 0), 4),
                })
            for cell in out.values():
                cell["credits"] = round(cell["credits"], 4)
                cell["success_rate"] = (round(cell["ok"] * 100.0 / cell["requests"], 2)
                                        if cell["requests"] else None)
                cell["by_model"].sort(key=lambda m: m["credits"], reverse=True)
            return out

    def daily_series(self, days: int = 14) -> list[dict]:
        """最近 N 天的每日请求数与积分，供图表使用。"""
        out = []
        now = time.time()
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            end = start + 86400
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                         COALESCE(SUM(credits),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE created_at>=? AND created_at<?""", (start, end)
                ).fetchone()
            out.append({
                "date": time.strftime("%m-%d", time.localtime(start)),
                "ts": start,
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "credits": round(float(r["c"]), 4),
                "tokens": int(r["t"]),
            })
        return out

    # ---------------- 数据页的图表口径 ----------------

    def hourly_series(self, hours: int = 24) -> list[dict]:
        """最近 N 小时的请求量，按整点分桶。"""
        now = time.time()
        base = now - (int(now) % 3600)
        out = []
        for i in range(hours - 1, -1, -1):
            start = base - i * 3600
            end = start + 3600
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                         COALESCE(SUM(credits),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE created_at>=? AND created_at<?""", (start, end)
                ).fetchone()
            out.append({
                "hour": time.strftime("%H:00", time.localtime(start)),
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "credits": round(float(r["c"]), 4),
                "tokens": int(r["t"]),
            })
        return out

    def models_pivot(self, days: int = 14) -> dict:
        """按模型 × 日期交叉的请求量，给堆叠面积图用。

        只保留请求量最高的 5 个模型，其余并成「其他」，否则图例会挤满。
        """
        start = day_start(time.time() - (days - 1) * 86400)
        with self._conn() as c:
            totals = c.execute(
                """SELECT model, COUNT(*) n FROM usage WHERE created_at>=?
                   GROUP BY model ORDER BY n DESC""", (start,)
            ).fetchall()
            rows = c.execute(
                """SELECT model, CAST((created_at-?)/86400 AS INTEGER) d,
                     COUNT(*) n FROM usage WHERE created_at>=?
                   GROUP BY model, d""", (start, start)
            ).fetchall()
        keep = [t["model"] or "" for t in totals[:5]]
        labels = [time.strftime("%m-%d", time.localtime(start + i * 86400))
                  for i in range(days)]
        series = {m: [0] * days for m in keep}
        if len(totals) > 5:
            series["其他"] = [0] * days
        for r in rows:
            d = int(r["d"])
            if not 0 <= d < days:
                continue
            key = r["model"] or ""
            if key not in series:
                key = "其他"
            series[key][d] += int(r["n"])
        return {"labels": labels,
                "series": [{"model": m, "values": v} for m, v in series.items()]}

    def token_distribution(self, since: float = 0) -> list[dict]:
        """单次请求的 Token 分布（直方图）。"""
        edges = [0, 1000, 4000, 16000, 64000, 256000]
        out = []
        with self._conn() as c:
            for i, lo in enumerate(edges):
                hi = edges[i + 1] if i + 1 < len(edges) else None
                if hi is None:
                    r = c.execute(
                        "SELECT COUNT(*) n FROM usage WHERE created_at>=? "
                        "AND total_tokens>=?", (since, lo)).fetchone()
                    label = f"≥{_short_num(lo)}"
                else:
                    r = c.execute(
                        "SELECT COUNT(*) n FROM usage WHERE created_at>=? "
                        "AND total_tokens>=? AND total_tokens<?",
                        (since, lo, hi)).fetchone()
                    label = f"{_short_num(lo)}–{_short_num(hi)}"
                out.append({"label": label, "count": int(r["n"])})
        return out

    def usage_heatmap(self, days: int = 14) -> dict:
        """星期 × 小时 的请求热力图，用来看使用习惯。"""
        start = day_start(time.time() - (days - 1) * 86400)
        grid = [[0] * 24 for _ in range(7)]
        with self._conn() as c:
            rows = c.execute(
                "SELECT created_at FROM usage WHERE created_at>=?", (start,)
            ).fetchall()
        for r in rows:
            t = time.localtime(r["created_at"])
            grid[t.tm_wday][t.tm_hour] += 1
        return {"grid": grid,
                "weekdays": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]}

    def provider_summary(self, since: float = 0) -> dict[int, dict]:
        """按账号归属汇总，带上账号所属厂商的聚合，供数据页分组。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT account_id, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     COALESCE(SUM(credits),0) credits,
                     COALESCE(SUM(total_tokens),0) tokens
                   FROM usage WHERE created_at>=? AND account_id IS NOT NULL
                   GROUP BY account_id""", (since,))
            return {int(r["account_id"]): dict(r) for r in rows}


    def member_summary(self, since: float | None = None) -> list[dict]:
        since = since if since is not None else time.time() - 7 * 86400
        with self._conn() as c:
            rows = c.execute(
                """SELECT member_id, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(credits),0) credits,
                     COALESCE(SUM(total_tokens),0) tokens FROM usage
                   WHERE created_at>=? GROUP BY member_id""", (since,)
            )
            return [dict(r) for r in rows]

    def member_model_summary(self, member_id: int, since: float = 0) -> list[dict]:
        """单个成员按模型的请求量 / Token / 积分（成员页明细用）。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT model, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE member_id=? AND created_at>=?
                   GROUP BY model ORDER BY credits DESC, n DESC""",
                (member_id, since),
            )
            return [dict(r) for r in rows]

    def usage_for_account(self, account_id: int, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM usage WHERE account_id=? ORDER BY id DESC LIMIT ?",
                (account_id, limit))
            return [dict(r) for r in rows]

    def daily_series_for_account(self, account_id: int, days: int = 14) -> list[dict]:
        """单个账号最近 N 天的请求量与积分。"""
        out = []
        now = time.time()
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            end = start + 86400
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                         COALESCE(SUM(credits),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE account_id=? AND created_at>=? AND created_at<?""",
                    (account_id, start, end)).fetchone()
            out.append({
                "date": time.strftime("%m-%d", time.localtime(start)),
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "credits": round(float(r["c"]), 4),
                "tokens": int(r["t"]),
            })
        return out

    def account_totals(self, account_id: int) -> dict:
        """单个账号的网关记账合计：请求、成功、失败、Token 入/出、积分。"""
        with self._conn() as c:
            r = c.execute(
                """SELECT COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) pin,
                     COALESCE(SUM(completion_tokens),0) pout,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE account_id=?""", (account_id,)).fetchone()
        n = int(r["n"] or 0)
        ok = int(r["ok"] or 0)
        return {
            "requests": n, "ok": ok, "errors": int(r["err"] or 0),
            "success_rate": round(ok * 100.0 / n, 2) if n else None,
            "prompt_tokens": int(r["pin"] or 0),
            "completion_tokens": int(r["pout"] or 0),
            "tokens": int(r["tokens"] or 0),
            "credits": round(float(r["credits"] or 0), 4),
        }

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
