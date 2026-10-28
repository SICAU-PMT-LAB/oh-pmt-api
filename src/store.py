"""SQLite 存储：账号、成员、API Key、用量（token 与成本）、设置。"""

from __future__ import annotations

import hashlib
import json
import os
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
    -- 档位：go / plus / pro / max，额度由 src/plans.py 的 PLANS 推导。
    -- limit_daily / limit_total 是手动覆盖值，非 0 时优先于档位。
    tier            TEXT DEFAULT 'plus',
    -- 限额（元），0 表示跟随档位。日限额按本地 0 点重置，总限额不重置。
    limit_daily     REAL DEFAULT 0,
    limit_total     REAL DEFAULT 0,
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
    -- 只存 sha256，不存明文。明文只在创建的那一刻回显一次，之后无法再取回。
    -- 丢库不等于丢密钥：hash 反算不出原文，别人也拿不到可用的 key。
    key_prefix      TEXT NOT NULL,
    member_id       INTEGER,
    -- 计费通道：plan = 走档位百分比限额；api = 走赠礼余额
    kind            TEXT DEFAULT 'plan',
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
    -- 命中缓存与写入缓存的 token，计价时用各自的单价
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    -- 本次请求按 token × 单价算出的成本（元），限额按它累计。
    -- 这是**定值**：写进来就是当时那份价目的结果，之后改价、
    -- 换上游路由都不会回头改它。额度百分比也按它累计。
    cost            REAL DEFAULT 0,
    -- 价格来源：override / builtin / default，界面上标出估算值
    price_source    TEXT DEFAULT '',
    -- 已弃用：曾经存“当时生效的倍率”。倍率模式已移除，新行恒为 1；
    -- 列保留是因为生产库还有按倍率算出来的历史行，删列会丢掉那段口径。
    rate            REAL DEFAULT 1,
    -- 上游自己报的积分，只用于对账，不参与限额
    credits         REAL DEFAULT 0,
    credits_original REAL DEFAULT 0,
    -- 200 = 成功；其余是失败（error 存错误码，如 upstream_busy）
    status          INTEGER DEFAULT 200,
    -- 这次请求走的是哪条计费通道：plan（档位百分比）或 api（赠礼余额）。
    -- 不用 join api_keys 现推：密钥删了或事后改 kind 时那条历史就失了归属。
    channel         TEXT DEFAULT '',
    error           TEXT DEFAULT '',
    ip              TEXT DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- API 通道的额度台账。append-only：只有入账（正数）与按次消耗（负数），
-- 余额 = SUM(amount)，不存可变的 balance 列，改错就再记一笔冲正。
CREATE TABLE IF NOT EXISTS api_credits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL,
    amount      REAL NOT NULL,
    note        TEXT DEFAULT '',
    created_at  REAL NOT NULL
);

-- 成员 × 模型的额度。额度树是两层：模型到 100% 单独停掉那个模型，
-- 各模型的百分比再累加成配额百分比，到 100% 就停整个 plan 通道。
--
-- 只存增量：有行就是管理员给这个模型手工改过的上下限，没行就按档位算
-- （见 plans.model_limits）。改额度不是记账，改完只影响之后的请求，
-- 已写进 usage 的 cost 一个字都不动。
CREATE TABLE IF NOT EXISTS member_model_limits (
    member_id   INTEGER NOT NULL,
    model       TEXT NOT NULL,
    limit_daily REAL,
    limit_total REAL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (member_id, model)
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
    """当天 00:00 的时间戳（本地时区）。日限额按这个点重置。"""
    t = time.localtime(ts if ts is not None else time.time())
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def day_end(ts: float | None = None) -> float:
    """下一个 00:00 的时间戳，界面用来告诉成员什么时候重置。"""
    return day_start((ts if ts is not None else time.time()) + 86400)


class Store:
    def __init__(self, path: str):
        self.path = path
        self._init()
        # 库里存着全部上游账号的令牌，同机其他用户不该能读。umask 默认是 022，
        # 建出来的库文件是 644，同组和所有用户都能打开。这里收到仅属主可读写；
        # 只对属主是自己且确实落盘的文件生效，失败不影响启动。
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

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

        # 限额口径从「积分 + 周限额」换成「成本 + 总限额」。单位变了（积分→元），
        # 旧数值只能当近似保留：周限额搬成总限额，日限额原样留着，两者都不清零，
        # 否则升级后等于放开限制。管理员需要按新单位重新核对一遍。
        if "limit_total" not in mem_cols:
            c.execute("ALTER TABLE members ADD COLUMN limit_total REAL DEFAULT 0")
            if "limit_weekly" in mem_cols:
                c.execute("UPDATE members SET limit_total=limit_weekly "
                          "WHERE limit_weekly>0")

        # 档位：老成员没有 tier 字段，统一落到 plus（append-only，只加列不改值）
        if "tier" not in mem_cols:
            c.execute("ALTER TABLE members ADD COLUMN tier TEXT DEFAULT 'plus'")
            c.execute("UPDATE members SET tier='plus' WHERE tier IS NULL OR tier=''")

        # api_keys 区分通道：plan 走档位限额，api 走赠礼余额
        key_cols = columns("api_keys")
        if "kind" not in key_cols:
            c.execute("ALTER TABLE api_keys ADD COLUMN kind TEXT DEFAULT 'plan'")
            c.execute("UPDATE api_keys SET kind='plan' WHERE kind IS NULL OR kind=''")

        # usage 新增记账列
        use_cols = columns("usage")
        for name, decl in (
            ("member_id", "INTEGER"),
            ("credits", "REAL DEFAULT 0"),
            ("credits_original", "REAL DEFAULT 0"),
            ("error", "TEXT DEFAULT ''"),
            ("cache_read_tokens", "INTEGER DEFAULT 0"),
            ("cache_write_tokens", "INTEGER DEFAULT 0"),
            ("cost", "REAL DEFAULT 0"),
            ("price_source", "TEXT DEFAULT ''"),
            ("rate", "REAL DEFAULT 1"),
            ("channel", "TEXT DEFAULT ''"),
        ):
            if name not in use_cols:
                c.execute(f"ALTER TABLE usage ADD COLUMN {name} {decl}")

        # 补上历史行的通道：没记过就按当时那把密钥的 kind 反推一次。
        # 密钥已删的行推不出来（join 不到），一律当 plan，和改造前一致。
        if "channel" not in use_cols:
            c.execute(
                "UPDATE usage SET channel=COALESCE(NULLIF((SELECT kind FROM "
                "api_keys WHERE api_keys.id=usage.key_id),''),'plan') "
                "WHERE channel IS NULL OR channel=''")

        # api_keys.key_value 曾经存明文，为了方便随时查看。数据库被读走就是
        # 全部成员密钥同时泄露，而密钥只需要验签、不需要还原。这里把列连同
        # 历史明文一起删掉：已发出的密钥继续能用（校验靠 key_hash），但谁都
        # 再也取不出明文。想再要明文只能重置出一把新的，且只在那一刻显示一次。
        if "key_value" in columns("api_keys"):
            c.execute("ALTER TABLE api_keys DROP COLUMN key_value")

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

        优先账号按 id 认，**不看区域**：成员绑定的是某个具体账号，而一个对外
        模型在厂商目录里只登记了一条路由（WorkBuddy 的 CATALOG 每个区域都
        一样），路由上的 region 是默认值而不是限制。如果优先账号是国际版、
        路由默认是国内版，按区域过滤就永远选不到它，请求会一直落到别人的
        账号上。调用方拿到账号后必须用 account["region"]，不能用路由上的。

        选中即占用：这里会在同一个事务里把 last_used_at 推到当前时刻，
        所以并发请求不会同时拿到同一个「最久未用」的账号。选完不占用的
        话，两个请求会读到同一个 id，等于没有轮询。
        """
        skip = tuple(exclude or ())
        with self._conn() as c:
            # BEGIN IMMEDIATE 拿写锁，读和写必须在同一个事务里。
            # 时间戳也要在拿到锁之后再取：提前取的话并发线程会写出几乎
            # 相同的时间戳，排序退化成按 id，轮询就变成总压同一个账号。
            c.execute("BEGIN IMMEDIATE")
            now = time.time()
            if prefer_id and prefer_id not in skip:
                r = c.execute(
                    """SELECT * FROM accounts WHERE id=? AND provider=?
                       AND enabled=1 AND cooldown_until < ?""",
                    (prefer_id, provider, now),
                ).fetchone()
                if r:
                    c.execute("UPDATE accounts SET last_used_at=? WHERE id=?",
                              (now, r["id"]))
                    picked = dict(r)
                    picked["last_used_at"] = now
                    return picked
            sql = ("SELECT * FROM accounts WHERE provider=? AND region=? "
                   "AND enabled=1 AND cooldown_until < ?")
            args = [provider, region, now]
            if skip:
                sql += " AND id NOT IN (%s)" % ",".join("?" * len(skip))
                args.extend(skip)
            sql += " ORDER BY last_used_at ASC, id ASC LIMIT 1"
            r = c.execute(sql, args).fetchone()
            if not r:
                return None
            c.execute("UPDATE accounts SET last_used_at=? WHERE id=?", (now, r["id"]))
            picked = dict(r)
            picked["last_used_at"] = now
            return picked

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

    def create_member(self, *, name, email="", limit_daily=0.0, limit_total=0.0,
                      preferred_account_id=None, model_allowlist=DEFAULT_MODELS,
                      note="", username=None, password=None, is_admin=0,
                      tier="plus") -> int:
        pwd_hash = self.hash_password(password) if password else ""
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO members (name, email, limit_daily, limit_total,
                     preferred_account_id, model_allowlist, note, username,
                     password_hash, is_admin, tier, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (name, email, limit_daily, limit_total, preferred_account_id,
                 model_allowlist, note, username, pwd_hash, is_admin, tier,
                 time.time()),
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
        allowed = {"name", "email", "enabled", "limit_daily", "limit_total",
                   "preferred_account_id", "model_allowlist", "note",
                   "username", "is_admin", "tier"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        # 管理员是进后台的唯一钥匙：停用或清掉 is_admin 都会把所有人锁在
        # 门外，而门外没有任何界面能改回来。所以这两个字段对管理员只允许
        # 保持原值，其余字段照常可改。
        with self._conn() as c:
            row = c.execute("SELECT is_admin FROM members WHERE id=?",
                            (member_id,)).fetchone()
            if row and row["is_admin"]:
                if not sets.get("enabled", 1):
                    sets.pop("enabled")
                if "is_admin" in sets and not sets["is_admin"]:
                    sets.pop("is_admin")
            if not sets:
                return
            sql = ("UPDATE members SET " +
                   ", ".join(f"{k}=?" for k in sets) + " WHERE id=?")
            c.execute(sql, (*sets.values(), member_id))

    def delete_member(self, member_id: int) -> bool:
        """删除成员。管理员账号拒绝删除，返回 False。"""
        with self._conn() as c:
            row = c.execute("SELECT is_admin FROM members WHERE id=?",
                            (member_id,)).fetchone()
            if row and row["is_admin"]:
                return False
            c.execute("DELETE FROM members WHERE id=?", (member_id,))
            c.execute("DELETE FROM api_keys WHERE member_id=?", (member_id,))
            c.execute("DELETE FROM member_model_limits WHERE member_id=?",
                      (member_id,))
            return True

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
                   note="", kind="plan") -> tuple[int, str]:
        """建密钥。明文只在这一次返回里出现，库里只留 sha256。

        丢库不等于丢密钥：hash 不能反算出原文，攻击者也没法拿它冒充调用。
        """
        raw = self.generate_key()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO api_keys (key_hash, key_prefix, member_id,
                     ip_allowlist, model_allowlist, note, kind, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (self.hash_key(raw), raw[:16], member_id, ip_allowlist,
                 model_allowlist, note, kind, time.time()),
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

    def reset_key(self, key_id: int) -> str:
        """换一把新密钥，返回明文。id、归属与各项限制不变。

        库里只有 hash，明文一旦没保存就只能靠这个换新的。

        **目前没有任何 HTTP 路由调用它。** 换一把新密钥与「删掉重建」等价，
        而删除与创建都只由成员自己做；管理员两侧都不碰，否则就会有一方
        拿到别人可用的密钥明文。保留这个方泚只是为了让 store 层的能力完
        整，不要在服务端给它接一个口子。
        """
        raw = self.generate_key()
        with self._conn() as c:
            c.execute("UPDATE api_keys SET key_hash=?, key_prefix=? WHERE id=?",
                      (self.hash_key(raw), raw[:16], key_id))
        return raw

    def update_key(self, key_id: int, **fields):
        allowed = {"enabled", "ip_allowlist", "model_allowlist", "note",
                   "member_id", "kind"}
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
                     prompt_tokens, completion_tokens, cache_read_tokens=0,
                     cache_write_tokens=0, cost=0.0, price_source="",
                     rate=1.0, credits=0.0, credits_original=0.0, status=200,
                     error="", ip="", channel=""):
        """记一行用量。

        cost 是本次请求按 token × 单价算出的成本（元），限额按它累计；
        credits 是上游自己报的积分，只用于对账。两者可能都为空（失败请求）。
        channel 是计费通道（plan / api），空格子当 plan。

        这一行写下去就是定值：之后改价不再回头改它。
        """
        total = prompt_tokens + completion_tokens
        with self._conn() as c:
            c.execute(
                """INSERT INTO usage (key_id, member_id, account_id, model,
                     prompt_tokens, completion_tokens, total_tokens,
                     cache_read_tokens, cache_write_tokens, cost, price_source,
                     rate, credits, credits_original, status, error, ip,
                     channel, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key_id, member_id, account_id, model, prompt_tokens,
                 completion_tokens, total, int(cache_read_tokens or 0),
                 int(cache_write_tokens or 0), float(cost or 0),
                 (price_source or "")[:16], float(rate or 1.0), float(credits),
                 float(credits_original), int(status), (error or "")[:80], ip,
                 (channel or "")[:8], time.time()),
            )

    def cost_since(self, member_id: int, since: float,
                   channel: str = "") -> float:
        """成员自 since 起花的金额（元）。

        channel 限定计费通道：档位额度只该被 plan 通道的花费消耗，api 通道
        花的是赠礼余额，两边都算等于对同一笔额度刹两次车。
        """
        sql = "SELECT COALESCE(SUM(cost),0) FROM usage WHERE member_id=? AND created_at>=?"
        args: list = [member_id, since]
        if channel:
            sql += " AND COALESCE(NULLIF(channel,''),'plan')=?"
            args.append(channel)
        with self._conn() as c:
            return float(c.execute(sql, args).fetchone()[0])

    def cost_total(self, member_id: int, channel: str = "") -> float:
        sql = "SELECT COALESCE(SUM(cost),0) FROM usage WHERE member_id=?"
        args: list = [member_id]
        if channel:
            sql += " AND COALESCE(NULLIF(channel,''),'plan')=?"
            args.append(channel)
        with self._conn() as c:
            return float(c.execute(sql, args).fetchone()[0])

    def credits_total(self, member_id: int) -> float:
        """上游积分的累计值，仅用于对账（不参与限额）。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(credits),0) FROM usage WHERE member_id=?",
                (member_id,),
            ).fetchone()
            return float(r[0])

    # ---------- API 通道余额（append-only 台账） ----------

    def api_balance(self, member_id: int) -> float:
        """成员 API 通道余额（元）= 入账减去已消耗。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(amount),0) FROM api_credits WHERE member_id=?",
                (member_id,),
            ).fetchone()
            return round(float(r[0]), 6)

    def api_topup_total(self, member_id: int) -> float:
        """累计发放额，只算正数入账。

        余额是净额，花掉之后看不出当初发了多少；冲正记的是负数，也不该
        从发放总额里扣掉。所以另起一个口径，只累加正数行。
        """
        with self._conn() as c:
            r = c.execute(
                "SELECT COALESCE(SUM(amount),0) FROM api_credits "
                "WHERE member_id=? AND amount>0",
                (member_id,),
            ).fetchone()
            return round(float(r[0]), 6)

    def api_topup(self, member_id: int, amount: float, note: str = "") -> float:
        """给成员发放额度（正数入账）。返回发放后的余额。"""
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_credits (member_id, amount, note, created_at) "
                "VALUES (?,?,?,?)",
                (member_id, float(amount), note, time.time()),
            )
        return self.api_balance(member_id)

    def charge_api(self, member_id: int, amount: float, note: str = "") -> float:
        """从 API 通道扣减（记负数）。返回扣减后的余额。"""
        return self.api_topup(member_id, -abs(float(amount)), note)

    def api_ledger(self, member_id: int, limit: int = 50) -> list[dict]:
        """成员的额度流水（发放与消耗），新的在前。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM api_credits WHERE member_id=? "
                "ORDER BY id DESC LIMIT ?", (member_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def api_balances(self) -> dict[int, float]:
        """所有成员的 API 余额，供列表页一次取全。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT member_id, COALESCE(SUM(amount),0) b FROM api_credits "
                "GROUP BY member_id"
            ).fetchall()
            return {int(r["member_id"]): round(float(r["b"]), 6) for r in rows}

    def credits_by_account(self, since: float = 0) -> dict[int, float]:
        """每个账号被消耗的上游积分（仅对账用）。"""
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
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE created_at>=?
                   GROUP BY model ORDER BY cost DESC, n DESC""", (since,)
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
                     COALESCE(SUM(cost),0) cost,
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
            "cost": round(float(r["cost"] or 0), 6),
            "credits": round(float(r["credits"] or 0), 4),
        }

    def account_summary(self, since: float = 0) -> dict[int, dict]:
        """每个账号的请求量 / Token / 成本，附带按模型拆分。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT account_id, model, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cost),0) cost,
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
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "tokens": 0, "cost": 0.0, "credits": 0.0, "by_model": [],
                })
                n, ok = int(r["n"] or 0), int(r["ok"] or 0)
                cell["requests"] += n
                cell["ok"] += ok
                cell["errors"] += int(r["err"] or 0)
                cell["prompt_tokens"] += int(r["prompt_tokens"] or 0)
                cell["completion_tokens"] += int(r["completion_tokens"] or 0)
                cell["tokens"] += int(r["tokens"] or 0)
                cell["cost"] += float(r["cost"] or 0)
                cell["credits"] += float(r["credits"] or 0)
                cell["by_model"].append({
                    "id": r["model"] or "",
                    "requests": n, "ok": ok, "errors": int(r["err"] or 0),
                    "prompt_tokens": int(r["prompt_tokens"] or 0),
                    "completion_tokens": int(r["completion_tokens"] or 0),
                    "tokens": int(r["tokens"] or 0),
                    "cost": round(float(r["cost"] or 0), 6),
                    "credits": round(float(r["credits"] or 0), 4),
                })
            for cell in out.values():
                cell["cost"] = round(cell["cost"], 6)
                cell["credits"] = round(cell["credits"], 4)
                cell["success_rate"] = (round(cell["ok"] * 100.0 / cell["requests"], 2)
                                        if cell["requests"] else None)
                cell["by_model"].sort(key=lambda m: m["cost"], reverse=True)
            return out

    def daily_series(self, days: int = 14) -> list[dict]:
        """最近 N 天的每日请求数与成本，供图表使用。"""
        out = []
        now = time.time()
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            end = start + 86400
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                         COALESCE(SUM(cost),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE created_at>=? AND created_at<?""", (start, end)
                ).fetchone()
            out.append({
                "date": time.strftime("%m-%d", time.localtime(start)),
                "ts": start,
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "cost": round(float(r["c"]), 6),
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
                         COALESCE(SUM(cost),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE created_at>=? AND created_at<?""", (start, end)
                ).fetchone()
            out.append({
                "hour": time.strftime("%H:00", time.localtime(start)),
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "cost": round(float(r["c"]), 6),
                "tokens": int(r["t"]),
            })
        return out

    def models_pivot(self, days: int = 14, names: dict[str, str] | None = None) -> dict:
        """按模型 × 日期交叉的请求量，给堆叠面积图用。

        只保留请求量最高的 5 个模型，其余并成「其他」，否则图例会挤满。
        调用方传 names（上游 key → 显示名）时先归并再排名：同名模型可能
        来自多家上游，不合并会在图例里出现两条一样的名字。
        """
        start = day_start(time.time() - (days - 1) * 86400)
        with self._conn() as c:
            rows = c.execute(
                """SELECT model, CAST((created_at-?)/86400 AS INTEGER) d,
                     COUNT(*) n FROM usage WHERE created_at>=?
                   GROUP BY model, d""", (start, start)
            ).fetchall()
        label_of = names or {}
        per_day: dict[str, list[int]] = {}
        for r in rows:
            d = int(r["d"])
            if not 0 <= d < days:
                continue
            key = r["model"] or ""
            key = label_of.get(key, key)
            per_day.setdefault(key, [0] * days)[d] += int(r["n"])
        ranked = sorted(per_day.items(), key=lambda kv: -sum(kv[1]))
        keep = [k for k, _ in ranked[:5]]
        labels = [time.strftime("%m-%d", time.localtime(start + i * 86400))
                  for i in range(days)]
        series = {k: per_day[k] for k in keep}
        if len(ranked) > 5:
            rest = [0] * days
            for _, values in ranked[5:]:
                for i, v in enumerate(values):
                    rest[i] += v
            series["其他"] = rest
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
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits,
                     COALESCE(SUM(total_tokens),0) tokens
                   FROM usage WHERE created_at>=? AND account_id IS NOT NULL
                   GROUP BY account_id""", (since,))
            return {int(r["account_id"]): dict(r) for r in rows}

    def unassigned_summary(self, since: float = 0) -> dict:
        """没落到任何账号就失败的请求（no_account 等）的汇总。

        account_summary / provider_summary 都按 account_id 分组并跳过 NULL，
        所以这两类榜的合计会比 usage_totals 少。单独取一份补上。
        """
        with self._conn() as c:
            r = c.execute(
                """SELECT COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits,
                     COALESCE(SUM(total_tokens),0) tokens
                   FROM usage WHERE created_at>=? AND account_id IS NULL""",
                (since,)).fetchone()
        n = int(r["n"] or 0)
        ok = int(r["ok"] or 0)
        return {
            "requests": n,
            "ok": ok,
            "errors": int(r["err"] or 0),
            "success_rate": round(ok * 100.0 / n, 2) if n else None,
            "tokens": int(r["tokens"] or 0),
            "cost": round(float(r["cost"] or 0), 6),
            "credits": round(float(r["credits"] or 0), 4),
        }


    def member_summary(self, since: float | None = None) -> list[dict]:
        since = since if since is not None else time.time() - 7 * 86400
        with self._conn() as c:
            rows = c.execute(
                """SELECT member_id, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits,
                     COALESCE(SUM(total_tokens),0) tokens FROM usage
                   WHERE created_at>=? GROUP BY member_id""", (since,)
            )
            return [dict(r) for r in rows]

    def member_model_summary(self, member_id: int, since: float = 0,
                             until: float | None = None,
                             channel: str | None = None) -> list[dict]:
        """单个成员按模型的请求量 / Token / 成本 / 积分（成员页明细用）。

        until 是右开区的截止时间：模型额度的日窗口只算今天这一整天，
        用 since=今天 0 点、until=明天 0 点。不传就是「since 之后全部」。
        """
        where = "member_id=? AND created_at>=?"
        args: list = [member_id, since]
        if until is not None:
            where += " AND created_at<?"
            args.append(until)
        if channel:
            # 历史行没记过通道的子当 plan，与网关的实际行为一致
            where += " AND COALESCE(NULLIF(channel,''),'plan')=?"
            args.append(channel)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT model, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,
                     COALESCE(SUM(cache_write_tokens),0) cache_write_tokens,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE {where}
                   GROUP BY model ORDER BY cost DESC, n DESC""",
                args,
            )
            return [dict(r) for r in rows]

    # ---------- 成员 × 模型额度 ----------

    def channel_stats(self, member_id: int, since: float = 0) -> dict[str, dict]:
        """按计费通道分开的用量小计：{plan|api: {requests, ok, errors,
        tokens, cost}}。

        两条通道是两套账（plan 扣档位百分比，api 扣赠礼余额），加在一起
        没有意义。通道取自 usage.channel，不 join api_keys：密钥删了
        也要能算回历史。
        """
        out = {ch: {"channel": ch, "requests": 0, "ok": 0, "errors": 0,
                    "tokens": 0, "cost": 0.0}
               for ch in ("plan", "api")}
        with self._conn() as c:
            rows = c.execute(
                """SELECT COALESCE(NULLIF(channel,''),'plan') ch,
                     COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cost),0) cost
                   FROM usage WHERE member_id=? AND created_at>=?
                   GROUP BY ch""", (member_id, since)).fetchall()
        for r in rows:
            cell = out.setdefault(r["ch"], {
                "channel": r["ch"], "requests": 0, "ok": 0, "errors": 0,
                "tokens": 0, "cost": 0.0})
            cell["requests"] = int(r["n"] or 0)
            cell["ok"] = int(r["ok"] or 0)
            cell["errors"] = int(r["err"] or 0)
            cell["tokens"] = int(r["tokens"] or 0)
            cell["cost"] = round(float(r["cost"] or 0), 6)
        return out

    def model_limits(self, member_id: int) -> dict[str, dict]:
        """这个成员手工改过的模型额度，{模型: {limit_daily, limit_total}}。

        没有行的模型不在返回里，调用方按档位推导默认值。
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT model, limit_daily, limit_total FROM member_model_limits "
                "WHERE member_id=?", (member_id,))
            return {r["model"]: {"limit_daily": r["limit_daily"],
                                 "limit_total": r["limit_total"]}
                    for r in rows}

    def set_model_limits(self, member_id: int, overrides: dict[str, dict]):
        """整份替换这个成员的模型额度。

        overrides 里每个模型给 {"daily": 金额, "total": 金额}，值给 None
        表示这一项跟随档位。整份替换而不是逐条合，是因为管理员在弹窗里
        看到的就是全量，删掉一个模型的数字就该回到默认。
        """
        now = time.time()
        with self._conn() as c:
            c.execute("DELETE FROM member_model_limits WHERE member_id=?",
                      (member_id,))
            for model, spec in (overrides or {}).items():
                daily = spec.get("daily")
                total = spec.get("total")
                if daily is None and total is None:
                    continue
                c.execute(
                    "INSERT INTO member_model_limits "
                    "(member_id, model, limit_daily, limit_total, updated_at) "
                    "VALUES (?,?,?,?,?)",
                    (member_id, model, daily, total, now))

    def key_summary(self) -> list[dict]:
        """每把密钥的用量汇总（密钥管理页用）。没有用量的密钥也要出现。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT k.id, k.member_id, k.key_prefix, k.enabled, k.kind,
                     k.ip_allowlist, k.model_allowlist, k.note, k.created_at,
                     COUNT(u.id) n,
                     SUM(CASE WHEN u.status=200 THEN 1 ELSE 0 END) ok,
                     COALESCE(SUM(u.total_tokens),0) tokens,
                     COALESCE(SUM(u.cost),0) cost,
                     MAX(u.created_at) last_used
                   FROM api_keys k LEFT JOIN usage u ON u.key_id = k.id
                   GROUP BY k.id ORDER BY k.id"""
            )
            return [dict(r) for r in rows]

    def member_account_summary(self, member_id: int, since: float = 0) -> list[dict]:
        """单个成员在每个账号上的消耗（成员详情页用）。

        按 账号 × 模型 分组，前端再按账号聚成两层，这样既能看
        某个成员在哪个账号上用掉多少，也能看具体是哪些模型吃掉的。
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT account_id, model, COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE member_id=? AND created_at>=?
                   GROUP BY account_id, model ORDER BY cost DESC, n DESC""",
                (member_id, since),
            )
            return [dict(r) for r in rows]

    def member_daily_by_account(self, member_id: int, days: int = 14,
                                unit_of=None) -> list[dict]:
        """单个成员最近 N 天里每天在各账号上的消耗，画堆叠图用。

        unit_of(account_id) 返回该账号的计费口径，缺省一律按 cost（元）。
        口径为 credit 的账号取上游报的积分，为 token 的取 cost，两种口径
        各算各的，调用方分别画图，不能把积分和元加到一起。

        另带三个不分口径的日合计（tokens / requests / cost），顶部那三张
        卡要对全部请求求总量，不能用按口径拆开的那两个。
        """
        with self._conn() as c:
            out = []
            now = time.time()
            for i in range(days - 1, -1, -1):
                start = day_start(now - i * 86400)
                end = start + 86400
                rows = c.execute(
                    """SELECT account_id, COUNT(*) n,
                         COALESCE(SUM(cost),0) c,
                         COALESCE(SUM(credits),0) cr,
                         COALESCE(SUM(total_tokens),0) tk
                       FROM usage WHERE member_id=? AND created_at>=? AND created_at<?
                       GROUP BY account_id""",
                    (member_id, start, end),
                ).fetchall()
                by_account: dict[int, float] = {}
                total_c, total_n, total_tk = 0.0, 0, 0
                for r in rows:
                    aid = int(r["account_id"] or 0)
                    raw = r["cr"] if (unit_of and unit_of(aid) == "credit") else r["c"]
                    by_account[aid] = by_account.get(aid, 0.0) + round(float(raw), 6)
                    total_c += float(r["c"])
                    total_n += int(r["n"])
                    total_tk += int(r["tk"])
                out.append({
                    "date": time.strftime("%m-%d", time.localtime(start)),
                    "ts": start,
                    "by_account": by_account,
                    "requests": total_n,
                    "tokens": total_tk,
                    "cost": round(total_c, 6),
                })
            return out

    def usage_for_account(self, account_id: int, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM usage WHERE account_id=? ORDER BY id DESC LIMIT ?",
                (account_id, limit))
            return [dict(r) for r in rows]

    def daily_series_for_member(self, member_id: int, days: int = 14) -> list[dict]:
        """单个成员最近 N 天的请求量与成本（成员控制台图表用）。

        不能直接用 daily_series()：那是全站的量，成员看到自己的曲线才对。
        days=0 表示不限天数，从第一条记录那天画到今天。
        """
        out = []
        now = time.time()
        if days <= 0:
            # 「全部」：从最早一条记录所在的那天开始，没有记录就空着
            with self._conn() as c:
                r = c.execute(
                    """SELECT MIN(created_at) m FROM usage WHERE member_id=?""",
                    (member_id,)).fetchone()
            first = r["m"] if r else None
            if not first:
                return []
            span = int((day_start(now) - day_start(first)) // 86400) + 1
            # 一条记录都没有时要回 1 天，不能被 span=0 跳过
            days = max(1, span)
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            end = start + 86400
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         COALESCE(SUM(cost),0) c,
                         COALESCE(SUM(total_tokens),0) t,
                         COALESCE(SUM(prompt_tokens),0) p,
                         COALESCE(SUM(completion_tokens),0) o FROM usage
                       WHERE member_id=? AND created_at>=? AND created_at<?""",
                    (member_id, start, end)).fetchone()
            tokens = int(r["t"])
            cost = round(float(r["c"]), 6)
            out.append({
                "date": time.strftime("%m-%d", time.localtime(start)),
                "requests": int(r["n"]),
                "cost": cost,
                "tokens": tokens,
                "prompt_tokens": int(r["p"]),
                "completion_tokens": int(r["o"]),
            })
        return out

    def daily_series_for_account(self, account_id: int, days: int = 14) -> list[dict]:
        """单个账号最近 N 天的请求量与成本。"""
        out = []
        now = time.time()
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            end = start + 86400
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                         COALESCE(SUM(cost),0) c,
                         COALESCE(SUM(total_tokens),0) t FROM usage
                       WHERE account_id=? AND created_at>=? AND created_at<?""",
                    (account_id, start, end)).fetchone()
            out.append({
                "date": time.strftime("%m-%d", time.localtime(start)),
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "cost": round(float(r["c"]), 6),
                "tokens": int(r["t"]),
            })
        return out

    def account_totals(self, account_id: int) -> dict:
        """单个账号的网关记账合计：请求、成功、失败、Token 入/出、成本。"""
        with self._conn() as c:
            r = c.execute(
                """SELECT COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                     COALESCE(SUM(prompt_tokens),0) pin,
                     COALESCE(SUM(completion_tokens),0) pout,
                     COALESCE(SUM(cache_read_tokens),0) cread,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cost),0) cost,
                     COALESCE(SUM(credits),0) credits
                   FROM usage WHERE account_id=?""", (account_id,)).fetchone()
        n = int(r["n"] or 0)
        ok = int(r["ok"] or 0)
        return {
            "requests": n, "ok": ok, "errors": int(r["err"] or 0),
            "success_rate": round(ok * 100.0 / n, 2) if n else None,
            "prompt_tokens": int(r["pin"] or 0),
            "completion_tokens": int(r["pout"] or 0),
            "cache_read_tokens": int(r["cread"] or 0),
            "tokens": int(r["tokens"] or 0),
            "cost": round(float(r["cost"] or 0), 6),
            "credits": round(float(r["credits"] or 0), 4),
        }

    def recent_usage(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in rows]

    def recent_usage_page(self, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        """整站最近调用的第 offset 条起的一页，返回 (行, 全表总行数)。

        后台「数据」页的最近调用分段用：按 id 倒序翻页，id 就是写入顺序，
        比 created_at 更稳（同一秒里的多条不会互相乱序）。
        """
        with self._conn() as c:
            total = int(c.execute("SELECT COUNT(*) FROM usage").fetchone()[0])
            rows = c.execute(
                "SELECT * FROM usage ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset))
            return [dict(r) for r in rows], total

    def usage_for_member(self, member_id: int, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM usage WHERE member_id=? ORDER BY id DESC LIMIT ?",
                (member_id, limit),
            )
            return [dict(r) for r in rows]

    def usage_page(self, member_id: int, since: float = 0, until: float | None = None,
                   limit: int = 20, offset: int = 0,
                   channel: str = "") -> tuple[list[dict], int]:
        """成员调用明细的一页，返回 (行, 该时间范围内的总行数)。

        走 idx_usage_member_time，翻到第 3000 条也是毫秒级。总行数单独
        查一次 COUNT，前端要拿它算页数和「共 N 条」。
        """
        where = "member_id=? AND created_at>=?"
        args: list = [member_id, since]
        if until:
            where += " AND created_at<?"
            args.append(until)
        if channel:
            where += " AND COALESCE(NULLIF(channel,''),'plan')=?"
            args.append(channel)
        with self._conn() as c:
            total = c.execute(
                f"SELECT COUNT(*) FROM usage WHERE {where}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT * FROM usage WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                args + [limit, offset],
            )
            return [dict(r) for r in rows], int(total)

    def usage_count(self, member_id: int, since: float = 0,
                    channel: str = "") -> int:
        sql = "SELECT COUNT(*) FROM usage WHERE member_id=? AND created_at>=?"
        args: list = [member_id, since]
        if channel:
            sql += " AND COALESCE(NULLIF(channel,''),'plan')=?"
            args.append(channel)
        with self._conn() as c:
            return int(c.execute(sql, args).fetchone()[0])

    def purge_usage_before(self, before: float):
        with self._conn() as c:
            c.execute("DELETE FROM usage WHERE created_at < ?", (before,))

    # ---------------- 公开数据页（/data） ----------------

    def public_daily(self, days: int = 60) -> list[dict]:
        """最近 N 天的整站日线，公开数据页用。"""
        out = []
        now = time.time()
        for i in range(days - 1, -1, -1):
            start = day_start(now - i * 86400)
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n,
                         SUM(CASE WHEN status=200 THEN 0 ELSE 1 END) err,
                         COALESCE(SUM(cost),0) c,
                         COALESCE(SUM(total_tokens),0) t,
                         COALESCE(SUM(cache_read_tokens),0) cr,
                         COUNT(DISTINCT member_id) u
                       FROM usage WHERE created_at>=? AND created_at<?""",
                    (start, start + 86400)).fetchone()
            out.append({
                "ts": start,
                "date": time.strftime("%Y-%m-%d", time.localtime(start)),
                "requests": int(r["n"]),
                "errors": int(r["err"] or 0),
                "cost": round(float(r["c"] or 0), 6),
                "tokens": int(r["t"] or 0),
                "cache_read_tokens": int(r["cr"] or 0),
                "users": int(r["u"] or 0),
            })
        return out

    def public_model_matrix(self, days: int = 60) -> dict:
        """按模型 × 日期交叉的 token / 请求数 / 成本，公开数据页的堆叠图用。

        只取出现过的模型，合并逻辑交给调用方（上游 key 与显示名不同），
        这里返回原始 key 的明细。
        """
        start = day_start(time.time() - (days - 1) * 86400)
        with self._conn() as c:
            rows = c.execute(
                """SELECT model,
                     CAST((created_at-?)/86400 AS INTEGER) d,
                     COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,
                     COALESCE(SUM(cost),0) cost,
                     COUNT(DISTINCT member_id) users
                   FROM usage WHERE created_at>=?
                   GROUP BY model, d""", (start, start)).fetchall()
        start_label = time.strftime("%Y-%m-%d", time.localtime(start))
        return {"start": start_label, "days": days,
                "rows": [dict(r) for r in rows]}

    def public_member_matrix(self, days: int = 60) -> dict:
        """按成员 × 日期的 token / 请求数，公开数据页的“独立用户”图用。"""
        start = day_start(time.time() - (days - 1) * 86400)
        with self._conn() as c:
            rows = c.execute(
                """SELECT member_id,
                     CAST((created_at-?)/86400 AS INTEGER) d,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cost),0) cost,
                     COUNT(*) n
                   FROM usage WHERE created_at>=? AND member_id IS NOT NULL
                   GROUP BY member_id, d""", (start, start)).fetchall()
        return {"start": time.strftime("%Y-%m-%d", time.localtime(start)),
                "days": days, "rows": [dict(r) for r in rows]}

    def public_model_totals(self, days: int = 7) -> list[dict]:
        """近 N 天每个模型的合计：token、请求、成本、缓存读、独立成员。"""
        start = day_start(time.time() - (days - 1) * 86400)
        with self._conn() as c:
            rows = c.execute(
                """SELECT model,
                     COUNT(*) n,
                     SUM(CASE WHEN status=200 THEN 1 ELSE 0 END) ok,
                     COALESCE(SUM(prompt_tokens),0) prompt_tokens,
                     COALESCE(SUM(completion_tokens),0) completion_tokens,
                     COALESCE(SUM(total_tokens),0) tokens,
                     COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,
                     COALESCE(SUM(cache_write_tokens),0) cache_write_tokens,
                     COALESCE(SUM(cost),0) cost,
                     COUNT(DISTINCT member_id) users
                   FROM usage WHERE created_at>=?
                   GROUP BY model""", (start,)).fetchall()
        return [dict(r) for r in rows]

    def public_sessions(self, days: int = 60) -> dict:
        """近似「会话」：同一成员同一模型相隔 30 分钟内算同一次会话。

        没有会话 id 可用，只能按时间间隔切分。用它算会话成本与留存，
        数值是近似值，页面上的方法说明里写清楚。
        """
        start = day_start(time.time() - (days - 1) * 86400)
        gap = 30 * 60
        per_model: dict[str, dict] = {}
        per_member_week: dict[tuple, set] = {}
        with self._conn() as c:
            rows = c.execute(
                """SELECT member_id, model, created_at, total_tokens, cost
                   FROM usage WHERE created_at>=? AND member_id IS NOT NULL
                     AND model<>'' ORDER BY member_id, model, created_at""",
                (start,)).fetchall()
        cur_key = None
        session: list[dict] = []

        def flush():
            if not session:
                return
            mid, model = cur_key
            cell = per_model.setdefault(model, {
                "model": model, "sessions": 0, "tokens": 0, "cost": 0.0})
            cell["sessions"] += 1
            st = sum(r["total_tokens"] for r in session)
            sc = sum(float(r["cost"] or 0) for r in session)
            cell["tokens"] += st
            cell["cost"] += sc
            # 留存：同一周内用过该模型、下周又用过的成员
            week = int((session[0]["created_at"] - start) // (7 * 86400))
            per_member_week.setdefault((model, week), set()).add(mid)

        for r in rows:
            key = (r["member_id"], r["model"])
            if key != cur_key:
                flush()
                cur_key = key
                session = []
            elif session and r["created_at"] - session[-1]["created_at"] > gap:
                flush()
                session = []
            session.append(dict(r))
        flush()

        retention = []
        for model in {m for (m, _w) in per_member_week}:
            weeks = sorted(w for (m, w) in per_member_week if m == model)
            base = 0
            kept = 0
            for w in weeks[:-1]:
                a = per_member_week.get((model, w), set())
                b = per_member_week.get((model, w + 1), set())
                if not a:
                    continue
                base += len(a)
                kept += len(a & b)
            if base:
                retention.append({
                    "model": model,
                    "rate": round(kept * 100.0 / base, 1),
                    "eligible": base,
                })
        retention.sort(key=lambda r: (-r["rate"], -r["eligible"]))
        return {"models": list(per_model.values()), "retention": retention}

    def public_heatmap(self, days: int = 28) -> dict:
        """星期 × 小时的请求热力图，后台用。"""
        start = day_start(time.time() - (days - 1) * 86400)
        grid = [[0] * 24 for _ in range(7)]
        with self._conn() as c:
            rows = c.execute(
                "SELECT created_at FROM usage WHERE created_at>=?", (start,)
            ).fetchall()
        for r in rows:
            t = time.localtime(r["created_at"])
            grid[t.tm_wday][t.tm_hour] += 1
        return {"grid": grid, "days": days,
                "weekdays": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]}

    def public_hourly(self, hours: int = 24) -> list[dict]:
        """最近 N 小时的请求与 token，按整点分桶（公开数据页的活跃度图）。"""
        now = time.time()
        base = now - (int(now) % 3600)
        out = []
        for i in range(hours - 1, -1, -1):
            start = base - i * 3600
            with self._conn() as c:
                r = c.execute(
                    """SELECT COUNT(*) n, COALESCE(SUM(total_tokens),0) t,
                         COALESCE(SUM(cost),0) c, COUNT(DISTINCT member_id) u
                       FROM usage WHERE created_at>=? AND created_at<?""",
                    (start, start + 3600)).fetchone()
            out.append({
                "ts": start,
                "hour": time.strftime("%H:00", time.localtime(start)),
                "requests": int(r["n"]),
                "tokens": int(r["t"]),
                "cost": round(float(r["c"] or 0), 6),
                "users": int(r["u"] or 0),
            })
        return out

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
