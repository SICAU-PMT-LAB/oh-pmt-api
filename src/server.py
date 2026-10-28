"""HTTP 服务：OpenAI 兼容接口 + 管理 API + 成员控制台。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import sys
import traceback
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import convert
from qoder import resolve_model
import guard
import labs
import plans
import pricing
import providers
import qoder
import workbuddy
from store import DEFAULT_MODELS, Store, day_start


def _env(name: str, default: str = "") -> str:
    """配置读取：新名字 PMT_* 优先，老名字 Q2A_* 继续认。

    仓库从 qoder2api 改名成 oh-pmt-api，但线上 systemd unit 和 env 文件里
    写的是 Q2A_*，保留老名字可以让人不动服务器就能升级；两边都没写时用默认值。
    """
    return os.environ.get("PMT_" + name) or os.environ.get("Q2A_" + name) or default


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = _env("DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "oh-pmt-api.db")

STORE: Store | None = None
APP_VERSION = "2.0"
TRUST_FORWARDED = _env("TRUST_FORWARDED", "0") == "1"
SESSION_COOKIE = "q2a_session"


def _session_key() -> bytes:
    """会话签名密钥。

    优先用 PMT_SESSION_KEY。没配时自动生成一个随机密钥存在数据目录，
    不要用写死在源码里的常量：源码是公开的，任何人都能拿它签一个管理员
    cookie。存到文件里既避免重启后所有人被踢下线，也避免了固定密钥。
    """
    env = _env("SESSION_KEY")
    if env:
        return env.encode()
    path = os.path.join(DATA_DIR, "session.key")
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved.encode()
    except OSError:
        pass
    fresh = secrets.token_urlsafe(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        # 仅属主可读：里面有签名密钥
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(fresh)
    except OSError:
        # 写不进去（只读挂载等）也只能用本次进程内的临时密钥，
        # 代价是重启后会话失效，但不至于退回固定密钥
        pass
    return fresh.encode()


_SESSION_KEY = _session_key()


def _mask(secret: str, keep: int = 6) -> str:
    """凭证展示用的掩码：只留头尾，中间固定长度，不泄露真实长度差。

    WorkBuddy 这类凭证是整份 JSON 文件，逐个字符截断既看不出是什么、又会把
    文件里的 token 漏出来一小截，所以整个换成一句描述。
    """
    if not secret:
        return ""
    text = secret.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return "账号文件"
        auth = parsed
        if isinstance(parsed, list):
            auth = parsed[0] if parsed else {}
        if isinstance(auth, dict) and isinstance(auth.get("auth"), dict):
            auth = auth["auth"]
        token = str(auth.get("token") or auth.get("accessToken") or "") if isinstance(auth, dict) else ""
        tail = f"，token 尾号 {token[-4:]}" if token else ""
        return f"账号文件（已保存{tail}）"
    if len(text) <= keep * 2:
        return text[:2] + "…"
    return f"{text[:keep]}…{text[-4:]}"


def _sign(value: str) -> str:
    sig = hmac.new(_SESSION_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{sig}"


def _unsign(signed: str) -> str | None:
    """验签。只认当前密钥，不再接受任何写在源码里的固定密钥。

    以前为了「升级不踢人」保留过一份硬编码的旧密钥，那等于把签名能力
    公开：源码是公开的，任何人都能拿它签一个管理员 cookie。改名前签发
    的会话需要重新登录一次，这个代价必须付。
    """
    if not signed or "." not in signed:
        return None
    value, _, sig = signed.rpartition(".")
    want = hmac.new(_SESSION_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]
    if secrets.compare_digest(sig, want):
        return value
    return None


# 登录失败限速：同一个来源地址连续错这么多次就等一会儿再试。
# 没有这道门时，管理员密码可以无限次猜。
# 新成员的初始密码。管理员只负责建账号并把它告诉成员，成员登录后自己改；
# 改完的密码管理员既看不到，也没有接口能改，只能重置回这个值。
DEFAULT_PASSWORD = "123456"

LOGIN_MAX_FAILS = 8
LOGIN_LOCK_SECONDS = 300
_login_fails: dict[str, list] = {}   # ip -> [失败次数, 首次失败时间]
_login_lock = threading.Lock()


def login_blocked(ip: str) -> int:
    """还要等多少秒才能再试；0 表示可以试。"""
    with _login_lock:
        rec = _login_fails.get(ip)
        if not rec:
            return 0
        count, first = rec
        left = first + LOGIN_LOCK_SECONDS - time.time()
        if left <= 0:
            _login_fails.pop(ip, None)
            return 0
        return int(left) if count >= LOGIN_MAX_FAILS else 0


def login_failed(ip: str):
    now = time.time()
    with _login_lock:
        rec = _login_fails.get(ip)
        if not rec or now - rec[1] > LOGIN_LOCK_SECONDS:
            _login_fails[ip] = [1, now]
        else:
            rec[0] += 1
        # 条目会随 IP 数量增长，超过一定规模时清掉已过期的
        if len(_login_fails) > 4096:
            for k, v in list(_login_fails.items()):
                if now - v[1] > LOGIN_LOCK_SECONDS:
                    _login_fails.pop(k, None)


def login_ok(ip: str):
    with _login_lock:
        _login_fails.pop(ip, None)


# 排队时网关自己等，不把 429 甩给客户端（客户端重试几次就放弃了）
QUEUE_MAX_WAIT = 180      # 最多等这么多秒
QUEUE_MAX_ROUNDS = 6      # 最多重试这么多轮
# 请求体上限。长上下文对话可能有几 MB，给到 32 MB 足够；
# 不设上限时一个声明了巨大 Content-Length 的请求就能吃光内存。
MAX_BODY_BYTES = 32 * 1024 * 1024

_MODEL_CACHE: dict[str, tuple[float, list]] = {}
_MODEL_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()


def get_store() -> Store:
    global STORE
    if STORE is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _adopt_old_db()
        STORE = Store(DB_PATH)
    return STORE


def _adopt_old_db():
    """仓库改名前叫 qoder2api，把旧名字的库文件接过来，别让老数据凭空消失。

    只在旧文件存在、新文件不存在时移动；两个都在说明已经迁移过或用户自己放了
    新库，这时不动任何文件。
    """
    legacy = os.path.join(DATA_DIR, "qoder2api.db")
    if not os.path.exists(legacy) or os.path.exists(DB_PATH):
        return
    for suffix in ("", "-wal", "-shm"):
        src = legacy + suffix
        if os.path.exists(src):
            os.replace(src, DB_PATH + suffix)


# ---------------------------------------------------------------------------
# 凭证与积分维护
# ---------------------------------------------------------------------------

def ensure_fresh_token(account: dict) -> dict:
    """必要时续期调用凭据。Qoder 的 job token 只有 24 小时。

    不依赖短期凭据的厂商（适配器直接返回空）原样返回账号。
    """
    prov = providers.get(account.get("provider"))
    if account["expires_at"] and account["expires_at"] - time.time() > 300:
        return account
    if not prov.available:
        raise providers.Unsupported(f"{prov.label} 还没有接入协议")

    with _REFRESH_LOCK:
        fresh = get_store().get_account(account["id"])
        if fresh and fresh["expires_at"] and fresh["expires_at"] - time.time() > 300:
            return fresh
        acc = fresh or account

        new = None
        try:
            new = prov.refresh(acc)
        except Exception:  # noqa: BLE001
            new = None
        if not new or not new.get("access_token"):
            ex = prov.connect(acc["credential"], acc["region"])
            new = {"access_token": ex["access_token"],
                   "refresh_token": ex["refresh_token"],
                   "expires_in": ex.get("expires_in", 86400)}

        expires_at = time.time() + int(new.get("expires_in") or 86400)
        get_store().set_account_tokens(
            acc["id"], new["access_token"], new.get("refresh_token", ""), expires_at
        )
        acc = dict(acc)
        acc["access_token"] = new["access_token"]
        acc["refresh_token"] = new.get("refresh_token", "")
        acc["expires_at"] = expires_at
        return acc


def sync_account_credits(account: dict) -> dict | None:
    """从上游拉取额度快照并落库。厂商不支持额度查询时返回 None。"""
    prov = providers.get(account.get("provider"))
    try:
        acc = ensure_fresh_token(account)
        q = prov.fetch_quota(acc)
    except Exception:  # noqa: BLE001
        return None
    get_store().set_account_credits(
        account["id"],
        total=q["total"], used=q["used"], remaining=q["remaining"],
        addon=q["addon_remaining"], plan=q["plan"],
        base_total=q["base_total"], base_used=q["base_used"],
        base_remaining=q["base_remaining"],
        addon_total=q["addon_total"], addon_used=q["addon_used"],
        shared_total=q["shared_total"], shared_used=q["shared_used"],
        shared_remaining=q["shared_remaining"],
        expire_at=q["expire_at"], exceeded=q["exceeded"],
    )
    return q


def checkin_one(account: dict) -> dict:
    """给单个账号签到并落库。

    「到账多少积分」按签到前后上游额度的差额算：活动面额只是预期值，
    实际可能延迟到账、或按人去重后本号不发。
    """
    store = get_store()
    prov = providers.get(account.get("provider"))
    acc = ensure_fresh_token(account)
    before = None
    if account["credits_synced_at"]:
        before = float(account["credits_remaining"] or 0)

    res = prov.checkin(acc)

    # 发放有秒级延迟，刷一次拿到新额度；刷不到就保留原快照
    q = sync_account_credits(account)
    earned = 0.0
    message = res.get("message") or ""
    if q is not None and before is not None:
        delta = float(q["remaining"]) - before
        if delta > 0.005:
            earned = round(delta, 4)
            message = f"签到成功，到账 {earned:g} 积分"
        elif res.get("claimed"):
            # 领取成功但额度没动：如实说清楚，别把面额当到账
            message = (f"已领取 {res['claimed']} 个活动（面额 "
                       f"{res.get('declared', 0):g} 积分），上游额度暂未变化")
    store.set_account_checkin(account["id"], ok=res["ok"],
                              earned=earned, message=message)
    return {**res, "earned": earned, "message": message,
            "account_id": account["id"],
            "name": account["name"] or account["email"] or acc["uid"][:8],
            "quota": q}


def price_overrides(store=None) -> dict[str, dict[str, float]]:
    """管理员在定价页改过的单价，存 settings，覆盖内置表。"""
    store = store or get_store()
    return pricing.normalize_overrides(store.get_setting("model_prices", {}) or {})


def price_for(route: dict, meta: dict, overrides: dict | None = None):
    """这条来源的价目与来源标记。每次请求都查一次，不做缓存：

    查表是几次字典命中，比缓存失效逻辑便宜得多；而改价后必须立即生效。
    overrides 不传时自己去 settings 读一次：计费路径必须用管理员改过的
    单价，否则定价页上改了也不生效。
    """
    if overrides is None:
        overrides = price_overrides()
    return pricing.lookup(
        route.get("model") or "", meta.get("name") or "",
        meta.get("id") or route.get("model") or "", overrides)


# 成员调用明细一次最多能翻到多少条。这不是容量上限——远端 sqlite 走
# idx_usage_member_time，翻到 3000 条实测 0.19ms，500 人各 3000 条也只占
# 238MB。限制它是因为再往前的记录用户不会看，白占页面和网络。
USAGE_PAGE_MAX = int(_env("USAGE_PAGE_MAX", "3000") or 3000)


def _model_quota_row(rows: list[dict], mid: str, meta: dict) -> dict | None:
    """从额度树里取出某个模型那一行。

    定位用显示名（额度树按对外名字建键），取不到返回 None，调用方
    按档位默认处理。
    """
    label = meta.get("name") or mid
    for row in rows:
        if row.get("label") == label or row.get("id") == mid:
            return row
    return None


def _model_quota_editable(store, member: dict, catalog: dict, allow,
                          rows: list[dict]) -> list[dict]:
    """成员弹窗里能改额度的模型，以及当前生效的额度。

    两个坑：

    1. 不能拿「额度树里那一行」当编辑值。额度树按对外名字建键，目录和
       历史行用的是上游 key，认不出来时那一行不存在，取到的是空——
       前端把空当成 0 提交回去，就是把默认额度写成了自由值。
    2. 只有 `overridden` 为真的模型才带数字。默认额度由前端按下拉档位
       自己算，接口回一份档位值会被当成管理员手工设置过的数字。
    """
    overrides = store.model_limits(member["id"])
    out = []
    for mid, meta in sorted(catalog.items()):
        if not guard.allowed_routes(allow, mid, catalog):
            continue
        name = meta.get("name") or mid
        row = _model_quota_row(rows, mid, meta) or {}
        # 库里按显示名存，老数据也可能存过上游 key，两种都认
        ov = overrides.get(name) or overrides.get(mid) or {}
        daily = ov.get("limit_daily")
        total = ov.get("limit_total")
        overridden = daily is not None or total is not None
        out.append({
            "id": mid, "name": name,
            "limit_daily": daily if overridden else None,
            "limit_total": total if overridden else None,
            # 额度树那一行的结果照旧带上：详情页要显示「这个模型实际能用多少」
            "effective_daily": row.get("limit_daily"),
            "effective_total": row.get("limit_total"),
            "overridden": overridden,
        })
    return out


def member_quota(store, member: dict, models: dict, names: dict | None = None,
                 allowed: set[str] | None = None) -> dict:
    """成员视角的额度树：总量 + 每个可用模型各自的额度与已用。

    `models` 传目录（all_models() 的返回值），这里的每个可用模型对应一个
    额度格；额度默认就是档位额度，管理员按模型改过的值存在
    member_model_limits 里。

    百分比的口径见 guard.model_quota：单模型到 100% 只停那个模型，
    各模型百分比累加到 100% 就停整个 plan 通道。
    """
    names = names or public_model_names(models, store)
    labels = {mid: (meta.get("name") or mid) for mid, meta in models.items()
              if allowed is None or guard.allowed_routes(allowed, mid, models)}
    mq = guard.model_quota(store, member, labels, names,
                           store.model_limits(member["id"]))
    # 对外口径仍是档位那一份数与百分比，累加出来的两个就是网关实际的拦截面
    base = guard.quota_status(store, member)
    return {**base,
            "pct_today": mq["pct_daily"],
            "pct_total": mq["pct_total"],
            "model_quota": mq}


def _cache_rate(cache_read: int, prompt: int) -> float | None:
    """缓存命中率，0-100。

    分母是「输入 + 缓存读」：缓存只作用于输入侧，输出不参与，
    把它算进分母会让命中率凭空变低。没有输入时返回 None，
    界面显示成「—」而不是 0%，避免看起来像命中率为零。
    """
    base = prompt + cache_read
    if base <= 0:
        return None
    return round(cache_read * 100.0 / base, 1)


def _clamp_int(raw, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def provider_label(slug: str) -> str:
    """厂商显示名。已删除账号的 provider 可能为空，界面上给个占位。"""
    prov = providers.PROVIDERS.get((slug or "").strip())
    return prov.label if prov else (slug or "未分配账号")


def provider_color(slug: str) -> str:
    prov = providers.PROVIDERS.get((slug or "").strip())
    return prov.color if prov else "#8c8677"


def models_for_region(region: str, account: dict | None = None,
                      provider: str = providers.DEFAULT_PROVIDER) -> list[dict]:
    prov = providers.get(provider)
    if not prov.available:
        return []
    key = f"{prov.slug}:{region}"
    with _MODEL_LOCK:
        hit = _MODEL_CACHE.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    if account is None:
        account = get_store().pick_account(region, provider=prov.slug)
    if account is None and prov.catalog_needs_account:
        return []
    try:
        acc = ensure_fresh_token(account) if account else None
        items = prov.list_models(acc)
    except Exception:  # noqa: BLE001
        return []
    for m in items:
        m.setdefault("provider", prov.slug)
    with _MODEL_LOCK:
        _MODEL_CACHE[key] = (time.time(), items)
    return items


def all_models() -> dict[str, dict]:
    """合并各厂商的模型目录，按显示名去重。

    两家上游对同一个模型给的 key 完全不同（Qoder 的 dfmodel 和
    WorkBuddy 的 deepseek-v4.1-flash 都是 DeepSeek-V4.1-Flash）。如果按
    key 去重，客户端就只能用其中一家的名字，另一家的账号池永远用不上。
    所以这里以显示名为身份：同名模型合成一条，用 routes 记下所有能跑的
    (provider, region, key)，第一个是默认路由。
    """
    out: dict[str, dict] = {}
    for prov in providers.PROVIDERS.values():
        if not prov.available:
            continue
        # 静态目录的厂商只取第一个区域：CATALOG 每区域都一样，否则同名
        # 模型会出现两条同厂商路由，成员页上重复显示同一个厂商。
        # 路由上的 region 只是默认值，选账号时会在该厂商所有区域里找。
        regions = list(prov.regions)
        if not prov.catalog_needs_account:
            regions = regions[:1]
        for region in regions:
            for m in models_for_region(region, provider=prov.slug):
                name = (m.get("name") or m["id"]).strip()
                # 已下架的模型不进目录：上游可能还能跑，但同族已有新版，
                # 留着只会让客户端选错。历史账目不靠目录，不受影响。
                if pricing.retired(m["id"], name, name.lower()):
                    continue
                # 元数据跟着来源走：Qoder 的 thinking_config 与 WorkBuddy 的
                # 档位集合不通用，换厂商时得用那一家自己的那份
                route = {"provider": prov.slug, "region": region,
                         "model": m["id"], "meta": m}
                hit = out.get(name.lower())
                if hit is None:
                    out[name.lower()] = {
                        **m, "id": name.lower(),
                        "upstream_key": m["id"],
                        "region": region, "provider": prov.slug,
                        "routes": [route],
                    }
                    continue
                hit["routes"].append(route)
    # 同名模型只要有一家已被下架就整条不展示：客户端看到的是一个名字，
    # 不能“这家能跑那家已下线”让人猜。
    return {mid: meta for mid, meta in out.items()
            if not any(pricing.retired(r["model"], meta.get("name"), mid)
                       for r in meta["routes"])}


def stable_model_names(store=None) -> dict[str, str]:
    """上游 key → 显示名，不依赖账号可用性。

    all_models() 要先拿到一个可用账号才能读到目录，某个厂商全部账号在冷却
    或刚接入未同步时目录就是空的。历史 usage 行存的是上游 key，若只按目录
    反查，这两家就会在统计表里重新裂成两行。

    所以这里把三份合起来：手工维护的静态表 + 实时目录 + 之前读到过的名字。
    第三份存在 settings 里，账号全挂掉的窗口内历史模型名仍然认得出来。
    """
    names: dict[str, str] = {}
    known: dict[str, str] = {}
    if store is not None:
        known = store.get_setting("model_names", {}) or {}
        if isinstance(known, dict):
            names.update({str(k): str(v) for k, v in known.items()})
    for mid, meta in all_models().items():
        label = meta.get("name") or mid
        names[mid] = label
        for route in meta.get("routes") or []:
            names[route["model"]] = label
    # Qoder：display_name() 认整套上游 key，包括目录没覆盖到的
    for key in qoder.DISPLAY_NAMES:
        names.setdefault(key, qoder.display_name(key, key))
    # WorkBuddy：CATALOG 是静态表，与账号状态无关
    for mid, label, *_ in workbuddy.CATALOG:
        names.setdefault(mid, label)
    if store is not None and names != known:
        store.set_setting("model_names", names)
    return names


def public_model_names(models: dict | None = None,
                       store=None) -> dict[str, str]:
    """上游 key → 对外显示名。成员侧所有模型字段都要先过这一道。

    usage 行存的是上游 key（dfmodel / gfmodel 这种），那是网关自己的路由
    信息：成员看到它就能反推出请求落在哪家账号池上。接口按这张表把名字翻
    成对外的那一个；翻译不到就原样返回，因为历史行里已存的显示名本来就
    是对外写法。
    """
    names = stable_model_names(store)
    for mid, meta in (models or {}).items():
        label = meta.get("name") or mid
        for key in (mid, mid.lower(), *(r["model"] for r in
                                        meta.get("routes") or [])):
            names[key] = label
            names[key.lower()] = label
    return names


def clear_model_cache():
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "oh-pmt-api"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if _env("ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    # ---------------- 基础工具 ----------------

    def _send_json(self, status: int, payload, extra_headers: dict | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, retry_after: int = 0):
        # 流式响应开写之后，响应头和部分 SSE 已经发出去了。这时再写一个
        # 完整 HTTP 响应会在流尾部粘上 "HTTP/1.1 500 ..." 的报文，
        # 客户端把它当成 SSE 数据解析，整轮结果作废。只能作为一条
        # SSE 错误事件收尾。
        if getattr(self, "_stream_started", False):
            err = {"error": {"code": code, "message": message, "type": code}}
            try:
                self.wfile.write(convert.to_sse(json.dumps(err, ensure_ascii=False)))
                self.wfile.write(convert.DONE)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        extra = {"Retry-After": str(retry_after)} if retry_after else None
        self._send_json(status, {"error": {"code": code, "message": message,
                                           "type": code}},
                        extra_headers=extra)

    def _stream_queue_notice(self, round_no: int, wait: int, left: float):
        """流已开启但上游在排队，发一条进度事件让客户端知道还在等。"""
        ev = {"type": "queue", "round": round_no,
              "retry_after": wait, "remaining": int(left),
              "message": f"上游模型繁忙，{wait} 秒后重试（剩余 {int(left)} 秒）"}
        try:
            self.wfile.write((f": queued round={round_no} wait={wait}\n\n").encode())
            self.wfile.write(convert.to_sse(json.dumps(ev, ensure_ascii=False)))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise

    def _stream_queue_giveup(self, rounds: int, wait: int):
        """等到上限仍未就绪，在流内告知并结束。"""
        err = {"error": {"code": "upstream_busy",
                         "message": f"上游模型繁忙，已重试 {rounds} 轮仍未就绪，请稍后再试",
                         "type": "upstream_busy", "retry_after": wait}}
        try:
            self.wfile.write(convert.to_sse(json.dumps(err, ensure_ascii=False)))
            self.wfile.write(convert.DONE)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _read_json(self) -> dict:
        # HTTP/1.1 keep-alive 下，没被读走的字节会被当成下一个请求的起始行。
        # 这两种情形都必须先关连接再返回空体，否则能把垃圾字节砌进下一轮报文，
        # 在反向代理和源站之间制造出理解不一致（请求走私）。
        if self.headers.get("Transfer-Encoding", "").lower().find("chunked") >= 0:
            self.close_connection = True
            return {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return {}
        if not length:
            return {}
        if length < 0 or length > MAX_BODY_BYTES:
            # 没有上限时，一个声明了巨大 Content-Length 的请求就能让
            # rfile.read() 把内存吃满。越过上限的请求直接当空体处理，
            # 由调用方按「缺少参数」拒绝，不再读流。
            self.close_connection = True
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _client_ip(self) -> str:
        return guard.client_ip(self, TRUST_FORWARDED)

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    # ---------------- 鉴权 ----------------

    def _auth_key(self) -> tuple[dict, dict | None] | None:
        header = self.headers.get("Authorization", "")
        raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not raw:
            raw = self.headers.get("x-api-key", "").strip()
        if not raw:
            self._error(401, "missing_api_key", "缺少 API Key")
            return None
        record = get_store().get_key_by_hash(Store.hash_key(raw))
        if not record:
            self._error(401, "invalid_api_key", "API Key 无效")
            return None
        if not record["enabled"]:
            self._error(403, "api_key_disabled", "API Key 已停用")
            return None
        member = None
        if record["member_id"]:
            member = get_store().get_member(record["member_id"])
            if not member or not member["enabled"]:
                self._error(403, "member_disabled", "所属成员已停用")
                return None
        return record, member

    def _session(self) -> dict | None:
        """读取登录会话：Cookie 里存的是签名后的 member_id，不存密码。"""
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        got = cookies.get(SESSION_COOKIE)
        if not got:
            return None
        raw = _unsign(got.value)
        if not raw or not raw.startswith("m:"):
            return None
        try:
            member = get_store().get_member(int(raw[2:]))
        except (ValueError, TypeError):
            return None
        if not member or not member["enabled"]:
            return None
        return member

    def _auth_admin(self) -> bool:
        """管理员鉴权：必须是已登录且 is_admin 的账号。"""
        member = self._session()
        if member and member.get("is_admin"):
            return True
        if self._local_rescue():
            return True
        self._error(401, "admin_denied", "请先用管理员账号登录")
        return False

    def _local_rescue(self) -> bool:
        """本机直连的底线通道。

        没有任何可登录的管理员时，本机直连放行管理接口。已停用、没设账号
        或没设密码的管理员都算不可登录：这几种状态都会把人锁在门外，而
        门外没有任何界面能改回来。

        用真实客户端 IP 判断，不是 socket 对端地址：线上是 nginx 反代到
        127.0.0.1，看对端地址会把每个外部请求都当成本机，等于把后台
        完全放开。
        """
        if self._client_ip() not in ("127.0.0.1", "::1"):
            return False
        admin = get_store().get_admin()
        if not admin:
            return True
        return not (admin.get("enabled") and admin.get("username")
                    and admin.get("password_hash"))

    def _auth_member(self) -> dict | None:
        """成员控制台鉴权：已登录的会话，或成员自己的 API Key。"""
        member = self._session()
        if member:
            return {"member": member, "key": self._key_of(member["id"])}
        return self._auth_by_key()

    def _key_of(self, member_id: int) -> dict | None:
        keys = get_store().list_keys(member_id)
        return keys[0] if keys else None

    def _auth_by_key(self) -> dict | None:
        """用 API Key 鉴权（SDK 调用和控制台都走这里）。

        只认 Authorization 头。曾经允许 ?key= 查询串，但 URL 会进 nginx 访问
        日志、浏览器历史和 Referer，等于把密钥写到一堆没人清理的地方。
        """
        header = self.headers.get("Authorization", "")
        raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not raw:
            raw = self.headers.get("x-api-key", "").strip()
        if not raw:
            self._error(401, "missing_api_key", "请提供 API Key")
            return None
        record = get_store().get_key_by_hash(Store.hash_key(raw))
        if not record or not record["member_id"]:
            self._error(401, "invalid_api_key", "API Key 无效")
            return None
        member = get_store().get_member(record["member_id"])
        if not member:
            self._error(404, "member_not_found", "成员不存在")
            return None
        return {"key": record, "member": member}

    # ---------------- 路由 ----------------

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._send_json(200, {"status": "ok", "time": int(time.time())})
            if path == "/v1/models":
                return self._v1_models()

            # 管理接口
            if path == "/api/status":
                if not self._auth_admin():
                    return
                return self._send_json(200, self._admin_status())
            if path == "/api/accounts":
                if not self._auth_admin():
                    return
                return self._send_json(200, {
                    "accounts": self._accounts_view(),
                    "providers": providers.describe_all(),
                })
            if path.startswith("/api/accounts/"):
                if not self._auth_admin():
                    return
                try:
                    aid = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._error(400, "bad_request", "ID 无效")
                detail = self._account_detail(aid)
                if not detail:
                    return self._error(404, "account_not_found", "账号不存在")
                return self._send_json(200, detail)
            # 调用明细分页：/api/members/<id>/usage?days=7&page=1&size=20
            # 封顶 USAGE_PAGE_MAX 条（默认 3000），再深的翻页没有意义也不给
            if path.startswith("/api/members/") and path.endswith("/usage"):
                if not self._auth_admin():
                    return
                try:
                    mid = int(path.split("/")[3])
                except (ValueError, IndexError):
                    return self._error(400, "bad_request", "ID 无效")
                q = parse_qs(urlparse(self.path).query)
                days = _clamp_int(q.get("days", ["0"])[0], 0, 3650, 0)
                page = _clamp_int(q.get("page", ["1"])[0], 1, 10_000, 1)
                size = _clamp_int(q.get("size", ["20"])[0], 1, 200, 20)
                since = 0.0
                if days:
                    since = day_start(time.time() - (days - 1) * 86400)
                cap = USAGE_PAGE_MAX
                offset = (page - 1) * size
                if offset >= cap:
                    rows, total = [], 0
                else:
                    rows, total = get_store().usage_page(
                        mid, since=since, limit=min(size, cap - offset), offset=offset)
                capped = min(total, cap)
                return self._send_json(200, {
                    "rows": rows,
                    "total": total,
                    "capped": capped,
                    "cap": cap,
                    "page": page,
                    "size": size,
                    "days": days,
                    "pages": max(1, -(-capped // size)),
                })

            if path.startswith("/api/members/"):
                if not self._auth_admin():
                    return
                try:
                    mid = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._error(400, "bad_request", "ID 无效")
                detail = self._member_detail(mid)
                if not detail:
                    return self._error(404, "member_not_found", "成员不存在")
                return self._send_json(200, detail)
            if path == "/api/members":
                if not self._auth_admin():
                    return
                return self._send_json(200, {"members": self._members_view()})
            if path == "/api/keys":
                if not self._auth_admin():
                    return
                return self._send_json(200, {"keys": self._keys_view()})
            if path == "/api/analytics":
                if not self._auth_admin():
                    return
                return self._send_json(200, self._analytics())
            if path == "/api/usage":
                if not self._auth_admin():
                    return
                # 最近调用分页：/api/usage?page=1&size=100
                # 「数据」页底部那张表用，默认每页 100 条。
                q = parse_qs(urlparse(self.path).query)
                page = _clamp_int(q.get("page", ["1"])[0], 1, 100_000, 1)
                size = _clamp_int(q.get("size", ["100"])[0], 1, 500, 100)
                rows, total = get_store().recent_usage_page(
                    limit=size, offset=(page - 1) * size)
                return self._send_json(200, {
                    "recent": rows,
                    "total": total,
                    "page": page,
                    "size": size,
                    "pages": max(1, -(-total // size)),
                })
            if path == "/api/pricing":
                if not self._auth_admin():
                    return
                return self._send_json(200, self._pricing_view())

            # 公开接口：站点首页 / 数据页 / 套餐页用的数据，不需要登录。
            # 只含聚合后的数字与模型名，没有成员、账号、密钥任何可识别信息。
            if path == "/api/public/plans":
                return self._send_json(200, plans.describe(
                    all_models(), price_overrides()))
            if path == "/api/public/data":
                return self._send_json(200, self._public_data())
            if path == "/api/public/compare":
                names = (parse_qs(urlparse(self.path).query).get("model") or [])[:4]
                if len(names) < 2:
                    return self._send_json(400, {"error": "需要两个 model 参数"})
                return self._send_json(200, self._public_compare(names))
            if path == "/api/public/site":
                store = get_store()
                accounts = store.list_accounts()
                members = store.list_members()
                # 曾经在这里回过 "providers": [厂商名...]。那是账号池的构成，
                # 而这一条不需要登录，等于把“我们买了哪几家”挂在公网上。
                return self._send_json(200, {
                    "version": APP_VERSION,
                    "models": len(all_models()),
                    "members": len(members),
                    "accounts": len(accounts),
                    "server_time": int(time.time()),
                })

            # 成员控制台数据
            if path == "/api/me":
                ctx = self._auth_member()
                if ctx is None:
                    return
                q = self._query()
                days = _clamp_int(q.get("days", ["14"])[0], 0, 3650, 14)
                return self._send_json(200,
                                       self._member_dashboard(ctx, days=days))
            if path == "/api/me/pricing":
                # 成员自己的模型单价表：只列白名单内的来源，只读
                ctx = self._auth_member()
                if ctx is None:
                    return
                return self._send_json(
                    200, self._pricing_view(ctx.get("key"), ctx["member"]))
            if path == "/api/me/usage":
                ctx = self._auth_member()
                if ctx is None:
                    return
                return self._send_json(200, self._self_usage(ctx, self._query()))

            # 公开页面：首页、套餐页、数据页。都不需要登录，
            # 未登录也能看，登录后从顶栏进控制台
            if path == "/":
                return self._serve_html("home.html")
            if path in ("/plan", "/plan/"):
                return self._serve_html("plan.html")
            if path in ("/data", "/data/"):
                return self._serve_html("data.html")
            if path.startswith("/data/compare"):
                # /data/compare/<a>/<b>，模型名写在路径里（用斜杠分隔两段）。
                # 名字里的空格与点在这种写法下没法表达，所以页面拿到路径后
                # 自己按 slug 去匹配，后端只校验至少有两段。
                return self._serve_html("compare.html")
            if path == "/docs":
                return self._serve_html("docs.html")
            if path.startswith("/members/"):
                # 单个成员的消耗详情，独立成页
                if not self._auth_admin_page():
                    return
                return self._serve_html("member.html")
            if path.startswith("/accounts/vendor/"):
                # 某个厂商下的账号列表，独立成页：手机上一屏放不下
                # 展开的面板，跳转后还能后退回到厂商总览
                if not self._auth_admin_page():
                    return
                return self._serve_html("vendor.html")
            if path.startswith("/accounts/"):
                if not self._auth_admin_page():
                    return
                return self._serve_html("account.html")
            if path == "/favicon.ico":
                # 浏览器默认会要这个，直接给 png 图标省一条 404
                return self._serve_static("/assets/logo-32.png")
            if path.startswith("/assets/"):
                return self._serve_static(path)
            if path in ("/console", "/me"):
                return self._serve_html("console.html")
            if path == "/login":
                member = self._session()
                if member:
                    return self._redirect("/admin" if member.get("is_admin") else "/me")
                return self._serve_html("login.html")
            # 后台入口：原来挂在根路径，现在根路径是公开首页了。
            # 本机直连仍然能抢救式打开后台（管理员账号弄坏时用）。
            if path in ("/admin", "/admin/"):
                if not self._auth_admin_page():
                    return
                return self._serve_html("admin.html")
            return self._error(404, "not_found", "接口不存在")
        except Exception:  # noqa: BLE001
            # 堆栈只进服务端日志。str(e) 里可能带文件路径、SQL 片段或上游 URL，
            # 回给客户端等于免费的情报泄露。
            traceback.print_exc(file=sys.stderr)
            return self._error(500, "internal_error", "服务端内部错误，请查看服务日志")

    def do_HEAD(self):
        """只回头部。

        首页用 HEAD 探测 /assets/demo.mp4 是否存在，以决定渲染视频还是占位
        画面。标准库默认对 HEAD 回 501，所以这里显式实现：与 GET 走同一套
        路由，但不写响应体。
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                return self._do_login()
            if path == "/api/logout":
                return self._do_logout()
            if path == "/v1/chat/completions":
                return self._chat_completions()

            if path == "/api/accounts":
                if not self._auth_admin():
                    return
                return self._add_account()
            if path == "/api/accounts/import":
                if not self._auth_admin():
                    return
                return self._import_accounts()
            if path == "/api/accounts/action":
                if not self._auth_admin():
                    return
                return self._account_action()
            if path == "/api/accounts/sync":
                if not self._auth_admin():
                    return
                return self._sync_credits()
            if path == "/api/accounts/checkin":
                if not self._auth_admin():
                    return
                return self._checkin()
            # 给成员充 api 余额：/api/members/<id>/topup
            if path.startswith("/api/members/") and path.endswith("/topup"):
                if not self._auth_admin():
                    return
                return self._topup_member()
            if path == "/api/members":
                if not self._auth_admin():
                    return
                return self._create_member()
            if path == "/api/keys":
                # 密钥对管理员只读：能看能停用，不能建、不能重置。
                # 创建与重置会在响应里交出明文，而明文一旦落到别人手里，
                # 「库里只存 hash」这层保护就形同虚设。
                if not self._auth_admin():
                    return
                return self._error(403, "admin_key_immutable",
                                   "密钥由成员自己创建，管理员不能代建")
            if path == "/api/me/password":
                # 成员用原密码换新密码。管理员改不了这一项，只能重置成
                # 初始密码；也正因如此，管理员永远看不到成员自己设的密码。
                ctx = self._auth_member()
                if ctx is None:
                    return
                return self._change_own_password(ctx)
            if path == "/api/me/keys":
                # 成员给自己的账号建密钥。明文只在这一个响应里出现，库里
                # 只落 hash，之后谁都取不回来（包括管理员）。
                ctx = self._auth_member()
                if ctx is None:
                    return
                return self._create_own_key(ctx)
            if path == "/api/pricing":
                if not self._auth_admin():
                    return
                return self._save_pricing()
            return self._error(404, "not_found", "接口不存在")
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)
        except qoder.UpstreamError as e:
            return self._error(400, "upstream_error", str(e))
        except providers.Unsupported as e:
            return self._error(400, "provider_unsupported", str(e))
        except Exception:  # noqa: BLE001
            # 堆栈只进服务端日志。str(e) 里可能带文件路径、SQL 片段或上游 URL，
            # 回给客户端等于免费的情报泄露。
            traceback.print_exc(file=sys.stderr)
            return self._error(500, "internal_error", "服务端内部错误，请查看服务日志")

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not self._auth_admin():
            return
        body = self._read_json()
        try:
            if path.startswith("/api/members/"):
                mid = int(path.rsplit("/", 1)[-1])
                # 管理员不能被停用，也不能被取消管理员身份：这是进后台的
                # 唯一入口，改掉之后门外没有任何界面能改回来。
                target = get_store().get_member(mid)
                if not target:
                    return self._error(404, "member_not_found", "成员不存在")
                if target.get("is_admin"):
                    if body.get("enabled") is False or body.get("enabled") == 0:
                        return self._error(403, "admin_immutable",
                                           "管理员账号不能停用")
                    if "is_admin" in body and not body["is_admin"]:
                        return self._error(403, "admin_immutable",
                                           "不能取消管理员身份")
                fields = {k: body[k] for k in
                          ("name", "email", "enabled", "limit_daily", "limit_total",
                           "preferred_account_id", "model_allowlist", "note")
                          if k in body}
                if "tier" in body:
                    # 档位决定额度口径，写进库之前先确认是已知的四档之一
                    tier = str(body["tier"] or "").strip().lower()
                    if tier not in plans.PLAN_BY_SLUG:
                        return self._error(
                            400, "bad_tier",
                            f"档位只能是 {'/'.join(plans.PLAN_BY_SLUG)}")
                    fields["tier"] = tier
                store = get_store()
                username = (body.get("username") or "").strip()
                if username:
                    other = store.get_member_by_username(username)
                    if other and other["id"] != mid:
                        return self._error(409, "username_taken", "该账号已存在")
                    fields["username"] = username
                reset = bool(body.get("reset_password"))
                if fields:
                    store.update_member(mid, **fields)
                if "model_limits" in body:
                    # 按模型的额度：{"模型名": {"daily": 金额或 null, "total": ...}}
                    # 整份替换，不传就不动。不传具体模型就是回到档位默认。
                    raw = body.get("model_limits")
                    if not isinstance(raw, dict):
                        return self._error(400, "bad_request",
                                           "model_limits 必须是对象")
                    clean = {}
                    for name, spec in raw.items():
                        if not isinstance(spec, dict):
                            continue
                        cell = {}
                        for window in ("daily", "total"):
                            value = spec.get(window)
                            if value is None or value == "":
                                cell[window] = None
                                continue
                            try:
                                num = float(value)
                            except (TypeError, ValueError):
                                return self._error(
                                    400, "bad_request",
                                    f"{name} 的{window}额度不是数字")
                            if num < 0:
                                return self._error(
                                    400, "bad_request",
                                    f"{name} 的{window}额度不能为负")
                            cell[window] = num
                        clean[str(name)] = cell
                    store.set_model_limits(mid, clean)
                if reset:
                    # 管理员只能把密码重置回初始密码，不能指定任意内容：
                    # 这样他永远不掌握成员实际在用的密码。
                    store.set_password(mid, DEFAULT_PASSWORD)
                    return self._send_json(
                        200, {"ok": True, "reset": True,
                              "default_password": DEFAULT_PASSWORD})
                return self._send_json(200, {"ok": True})
            if path.startswith("/api/keys/") and path.endswith("/reset"):
                # 重置会产生明文，与代建同一个问题。成员自己也不需要重置：
                # 删掉重建就能得到一把新的，效果相同。
                return self._error(403, "admin_key_immutable",
                                   "密钥不能重置，删掉重建即可；管理员只能停用")
            if path.startswith("/api/keys/"):
                kid = int(path.rsplit("/", 1)[-1])
                if not get_store().get_key(kid):
                    return self._error(404, "key_not_found", "密钥不存在")
                # 管理员只能启停。重置会产出明文，和代建一样不该由管理员做。
                fields = {k: body[k] for k in ("enabled",)
                          if k in body}
                get_store().update_key(kid, **fields)
                return self._send_json(200, {"ok": True})
        except ValueError:
            return self._error(400, "bad_request", "ID 无效")
        return self._error(404, "not_found", "接口不存在")

    def do_DELETE(self):
        path = urlparse(self.path).path
        # 成员删自己的密钥走 /api/me/keys/<id>，其余删除动作仅管理员
        if path.startswith("/api/me/keys/"):
            ctx = self._auth_member()
            if ctx is None:
                return
            try:
                kid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._error(400, "bad_request", "ID 无效")
            key = get_store().get_key(kid)
            # 只能删自己名下那把：不比对归属就等于谁都能删别人的密钥
            if not key or key["member_id"] != ctx["member"]["id"]:
                return self._error(404, "key_not_found", "密钥不存在")
            get_store().delete_key(kid)
            return self._send_json(200, {"ok": True})
        if not self._auth_admin():
            return
        try:
            if path.startswith("/api/members/"):
                if not get_store().delete_member(int(path.rsplit("/", 1)[-1])):
                    return self._error(403, "admin_immutable", "管理员账号不能删除")
                return self._send_json(200, {"ok": True})
            if path.startswith("/api/keys/"):
                # 删除也归成员自己：管理员不能代建密钥，自然也没有「
                # 代删除」的语义。要废掉一把泄露的密钥就停用它。
                return self._error(403, "admin_key_immutable",
                                   "密钥由成员自己删除，管理员只能停用")
            if path.startswith("/api/accounts/"):
                get_store().delete_account(int(path.rsplit("/", 1)[-1]))
                return self._send_json(200, {"ok": True})
        except ValueError:
            return self._error(400, "bad_request", "ID 无效")
        return self._error(404, "not_found", "接口不存在")

    # ---------------- OpenAI 兼容 ----------------

    def _v1_models(self):
        auth = self._auth_key()
        if auth is None:
            return
        key, member = auth
        allowed = guard.allowed_models(key, member)
        catalog = all_models()
        data = []
        for mid, m in catalog.items():
            # 只列出这个密钥真的能跑到的来源，一家都不剩就算模型不可用
            routes = guard.allowed_routes(allowed, mid, catalog)
            if not routes:
                continue
            full = m.get("name") or mid
            data.append({
                # 客户端普遍直接展示 id，所以 id 给完整名称；
                # 上游只认各家自己的 key，请求进来时按 routes 换回去
                "id": full,
                "object": "model",
                "name": full,
                "display_name": full,
                # 对外只有模型这一层：厂商、上游 key、区域都是网关自己的
                # 路由信息，不列给客户端。owned_by 是 OpenAI 协议必填字段，
                # 填服务名而不是厂商，否则客户端界面上就会看到账号池归属
                "owned_by": "oh-pmt-api",
            })
        return self._send_json(200, {"object": "list", "data": data})

    # ---------------- 登录 ----------------

    def _do_login(self):
        body = self._read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        store = get_store()
        ip = self._client_ip()
        wait = login_blocked(ip)
        if wait:
            return self._error(429, "login_locked",
                               f"尝试次数过多，请 {wait} 秒后再试", retry_after=wait)
        member = store.get_member_by_username(username)

        # 用户名不存在时也跑一次哈希，避免用响应时间探出账号是否存在
        ok = Store.verify_password(
            password, member["password_hash"] if member else Store.hash_password("x"))
        if not member or not ok:
            login_failed(ip)
            return self._error(401, "login_failed", "账号或密码错误")
        login_ok(ip)
        if not member["enabled"]:
            return self._error(403, "member_disabled", "该账号已停用")

        cookie = f"{SESSION_COOKIE}={_sign('m:' + str(member['id']))}"
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            f"{cookie}; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax{secure}")
        payload = json.dumps({
            "ok": True, "is_admin": bool(member.get("is_admin")),
            "name": member["name"], "redirect": "/admin" if member.get("is_admin") else "/me",
        }).encode()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _do_logout(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        payload = b'{"ok": true}'
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _chat_completions(self):
        self._stream_started = False
        auth = self._auth_key()
        if auth is None:
            return
        key, member = auth
        ip = self._client_ip()
        store = get_store()
        # 记录用：走到哪一步都记一行，失败也记，概况才能算真实成功率
        ctx = {"model": "", "account_id": None}

        def fail(status, code, message, retry_after=0):
            store.record_usage(
                key_id=key["id"], member_id=key["member_id"],
                account_id=ctx["account_id"], model=ctx["model"],
                prompt_tokens=0, completion_tokens=0,
                status=status, error=code, ip=ip,
                channel=(key.get("kind") or "plan"),
            )
            return self._error(status, code, message, retry_after=retry_after)

        # 读一次目录供额度判断：模型目录要账号才能读，读不到时为空，
        # 此时只按历史用过的模型算份额，结论不受影响（见 guard.check_quota）。
        catalog_now = all_models()
        names_now = public_model_names(catalog_now, store)

        try:
            guard.check_ip(key, ip)
            # 这里判整个 plan 通道还有没有份额：各模型「已花 / 该模型额度」
            # 之和到 100% 就停。模型名要读完 body 才知道，但判断本身不需要
            # 知道这次要调哪个模型——已经占掉多少份额与本次调谁无关。
            guard.check_quota(store, member, key, catalog_now, names_now)
        except guard.Denied as e:
            return fail(e.status, e.code, e.message)

        body = self._read_json()
        requested = (body.get("model") or "").strip()
        messages = body.get("messages") or []
        if not requested or not messages:
            return fail(400, "bad_request", "缺少 model 或 messages")

        # 客户端可以用完整名称或缩写，这里统一换成上游 key
        catalog = catalog_now
        model = resolve_model(requested, catalog) or requested
        ctx["model"] = model

        # 已下架的模型直接拒，不往下走。目录里已经不列了，但客户端配置里
        # 还存着旧名字的时候要知道是“下架”而不是“不存在”或“没权限”。
        # 先看猜到的内部 id，再看客户端原本写的名字：两者命中任一都算。
        if pricing.retired(model, requested, (catalog.get(model) or {}).get("name")):
            return fail(410, "model_retired",
                        f"模型 {requested} 已下架，请改用新版本；"
                        f"调用 /v1/models 可看当前可用列表")

        try:
            guard.check_model(key, member, model, catalog)
        except guard.Denied as e:
            return fail(e.status, e.code, e.message)

        # 现在知道是哪个模型了，查它的单模型额度。放在 check_model 之后：
        # 没开的模型该报 model_forbidden，而不是泄露出它有额度。
        # 只查这一个：日/总额度（不分模型）在读 body 之前已查过。
        try:
            guard.check_model_quota(
                store, member, key,
                (catalog.get(model) or {}).get("name") or model,
                names=public_model_names(catalog, store),
                overrides=store.model_limits(member["id"]))
        except guard.Denied as e:
            return fail(e.status, e.code, e.message)

        stream = bool(body.get("stream"))
        # 对外一个模型只有一个名字，背后可能是好几家上游（Qoder 的 dfmodel 与
        # WorkBuddy 的 deepseek-v4.1-flash 都叫 DeepSeek-V4.1-Flash）。
        # 可用模型是最终依据，优先账号只决定先试哪一家；后面失败时要能换到
        # 另一家，所以这里把「允许的来源」按顺序排好，逐个试账号。
        meta = catalog.get(model) or {}
        allowed = guard.allowed_models(key, member)
        prefer = member.get("preferred_account_id") if member else None
        prefer_provider = (store.get_account(prefer) or {}).get("provider") \
            if prefer else None
        routes = guard.allowed_routes(allowed, model, catalog, prefer=prefer_provider)
        account = provider = None
        # 目录里没有这个模型时不猜厂商，直接交给默认厂商处理（老行为）
        if not routes and not meta.get("routes"):
            routes = [{"provider": meta.get("provider") or providers.DEFAULT_PROVIDER,
                       "region": meta.get("region") or (
                           "cn" if model.endswith("-cn") else "global"),
                       "model": model}]
        want = meta.get("region") or ("cn" if model.endswith("-cn") else "global")
        chosen: dict | None = None
        for route in routes:
            cand = providers.get(route["provider"])
            # 优先账号只在它自己的账号池里找，不按路由的区域过滤：成员绑的是
            # 某个具体账号，路由上的区域只是默认值。选了跨区域的账号之后，
            # want 必须跟着走，否则后续换账号会又跑回默认区域。
            hit = store.pick_account(route["region"], provider=cand.slug,
                                     prefer_id=prefer)
            if hit is None:
                for alt in cand.regions:
                    if alt == route["region"]:
                        continue
                    hit = store.pick_account(alt, provider=cand.slug,
                                             prefer_id=prefer)
                    if hit is not None:
                        route = {**route, "region": alt}
                        break
            if hit is not None:
                account, provider = hit, cand
                want = hit.get("region") or route["region"]
                # 每家上游认的模型 key 不同，按实际选中的厂商换回它自己的 key
                model = route["model"]
                ctx["model"] = model
                chosen = route
                break
        if account is None:
            if not routes:
                # 白名单把这个模型的每一家来源都关掉了
                return fail(403, "model_forbidden",
                            f"该密钥不允许使用模型 {meta.get('name') or model}")
            first = providers.get(routes[0]["provider"])
            return fail(503, "no_account",
                        self._no_account_reason(first, routes[0]["region"]))
        ctx["account_id"] = account["id"]

        # 这次请求允许走的其他来源。当前厂商的账号全部不可用时（冷却、排队、
        # 连不上、额度不够），换到下一家厂商重连同一个模型：对客户端仍然是
        # 一个模型名、一条流。
        pending = [r for r in routes
                   if not (r["provider"] == provider.slug and r is chosen)]
        tried = {account["id"]}

        def build_payload(prov, model_key: str) -> bytes:
            """按厂商组装上游请求体。换厂商时上游模型 key 和参数写法都要跟着换。"""
            parameters = {"max_tokens": body.get("max_tokens")
                          or body.get("max_completion_tokens") or 8192}
            # 思考档位交给厂商自己对齐：Qoder 要裁到模型声明的集合，
            # WorkBuddy 直通上游（不传就完全不思考，所以必须透传）。
            # thinking / reasoning 这两个对象原样带上，只传 effort 会丢掉
            # {"type": "enabled"} 这类「开思考但不指定档位」的写法。
            if prov.wants_effort_objects:
                # 循环变量不能叫 key：外层 key 是 API key 记录，
                # 被覆盖后 finally 里的 key["id"] 会抛 string indices 错误，
                # 而那时流已经发完，客户端会收到一串污染数据的错误帧
                for field in ("reasoning", "thinking"):
                    if isinstance(body.get(field), dict):
                        parameters[field] = body[field]
            # 每家上游的模型元数据不一样（Qoder 的 thinking_config、WorkBuddy
            # 的档位集合）。换厂商时要用那一家自己的条目，否则会把上一家的
            # 思考档位写法发给另一家。
            route_meta = {**meta}
            for r in meta.get("routes") or []:
                if r.get("provider") == prov.slug and r.get("model") == model_key:
                    route_meta.update(r.get("meta") or {})
                    break
            effort = prov.normalize_effort(qoder.requested_effort(body), route_meta)
            if effort:
                parameters["reasoning_effort"] = effort
            return prov.build_chat_body(
                messages, model_key, stream=True,
                tools=body.get("tools"),
                parameters=parameters,
                meta=route_meta,
            )

        def switch_vendor():
            """换到下一个还能用的厂商，返回新的一组运行状态；换不了返回 None。

            返回 (account, provider, payload, model, want)，调用方直接解包。
            """
            while pending:
                route = pending.pop(0)
                cand = providers.get(route["provider"])
                hit = store.pick_account(route["region"], provider=cand.slug,
                                         prefer_id=prefer, exclude=tried)
                if hit is None:
                    for alt in cand.regions:
                        if alt == route["region"]:
                            continue
                        hit = store.pick_account(alt, provider=cand.slug,
                                                 prefer_id=prefer, exclude=tried)
                        if hit is not None:
                            route = {**route, "region": alt}
                            break
                if hit is None:
                    continue
                tried.add(hit["id"])
                ctx["model"] = route["model"]
                ctx["account_id"] = hit["id"]
                fresh = ensure_fresh_token(hit)
                return (fresh, cand, build_payload(cand, route["model"]),
                        route["model"], hit.get("region") or route["region"])
            return None

        def switch_account(exclude: set[int]):
            """同一厂商内换一个账号，换不了返回 None。

            优先级高的是同一个区域的账号；区域类错误（国际版要求首条必须是
            system）在同一区域重试没用，所以区域以外也找一轮。
            """
            order = [want] + [r for r in provider.regions if r != want]
            for region in order:
                hit = store.pick_account(region, provider=provider.slug,
                                         exclude=exclude)
                if hit is not None:
                    return hit
            return None

        try:
            account = ensure_fresh_token(account)
            payload = build_payload(provider, model)
            # 上游排队时返回 HTTP 200，但首帧就是 10605 错误信封。
            # 官方客户端会显示"排队中"并继续等；普通客户端只会重试几次就放弃，
            # 所以把等待收进网关。流式请求先开流再等，期间用注释帧保活并
            # 通知排队进度，客户端不至于因为长时间无数据而超时。
            if stream:
                # 先置位再写响应头：写头这一步本身也可能半途失败，
                # 那之后同样不能再补发一个完整的 HTTP 错误响应
                self._stream_started = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

            # 连不上、凭证失效这类问题换一家厂商可能就好了，所以外面再套
            # 一层厂商循环；换不了就把最后一次的异常往外报。
            while True:
                deadline = time.time() + QUEUE_MAX_WAIT
                round_no = 0
                ready = False
                try:
                    while True:
                        lines = provider.chat_stream(account, payload, model)
                        first = next(iter(lines), None)
                        frame = first or ""
                        wait = qoder.queue_info(frame)
                        # 排队和额度不足都是账号级的：上游对这个账号限流，或
                        # 这个账号买不起这个模型的套餐。同一密钥下的其他账号
                        # 往往立刻能用，先把同厂商还没试过的账号试一遍。
                        if wait or qoder.quota_exhausted(frame):
                            spare = switch_account(tried)
                            if spare is not None:
                                tried.add(spare["id"])
                                account = ensure_fresh_token(spare)
                                want = account.get("region") or want
                                ctx["account_id"] = account["id"]
                                continue
                            # 这家没账号可换了，换一家厂商重连同一个模型
                            swapped = switch_vendor()
                            if swapped:
                                account, provider, payload, model, want = swapped
                                break
                        if not wait:
                            ready = True
                            break
                        round_no += 1
                        left = deadline - time.time()
                        if left <= 0 or round_no > QUEUE_MAX_ROUNDS:
                            code = "upstream_busy"
                            msg = f"上游模型繁忙，已等待 {round_no - 1} 轮仍未就绪"
                            store.record_usage(
                                key_id=key["id"], member_id=key["member_id"],
                                account_id=account["id"], model=model,
                                prompt_tokens=0, completion_tokens=0,
                                status=429, error=code, ip=ip,
                                channel=(key.get("kind") or "plan"),
                            )
                            if stream:
                                self._stream_queue_giveup(round_no - 1, wait)
                                return
                            return self._error(429, code, msg, retry_after=wait)
                        if stream:
                            self._stream_queue_notice(round_no, wait, left)
                        time.sleep(min(wait, max(1, left)))
                    if ready:
                        break
                except qoder.UpstreamError as e:
                    # 请求被渠道校验拦下（WorkBuddy 的 11128）看着像是请求本身的
                    # 问题，实际是账号所属渠道的规则差异：国际版要求首条必须是
                    # system，国内版不查。只换同厂商的账号，不给账号加冷却；
                    # 没有别的账号就原样报错，别拐到另一家厂商白跑一趟。
                    if e.is_request_rejected:
                        spare = switch_account(tried)
                        if spare is None:
                            raise
                        tried.add(spare["id"])
                        account = ensure_fresh_token(spare)
                        want = account.get("region") or want
                        ctx["account_id"] = account["id"]
                        continue
                    # 其他错误（连不上、凭证失效、限流）先看能不能换厂商
                    swapped = switch_vendor()
                    if not swapped:
                        raise
                    account, provider, payload, model, want = swapped

            # 首帧已经被取出来判断过是否排队，要放回流的开头。
            # 这里必须用默认参数固定住上游迭代器：写成闭包直接引用 lines
            # 会被下面那句 lines = _replay() 重新绑定成 _replay 自身，
            # 于是 yield from 迭代到自己身上，抛 ValueError: generator already executing
            def _replay(upstream=lines, head=first):
                if head is not None:
                    yield head
                yield from upstream

            lines = _replay()
        except (qoder.UpstreamError, convert.UpstreamStatusError) as e:
            # convert.UpstreamStatusError 是上游把错误塞在 SSE 信封里回来的
            # （statusCodeValue 非 200，例如 403/105「登录过期」）。它在取首帧
            # 或折叠流时抛出，和 qoder.UpstreamError 一样属于上游侧故障，必须
            # 在同一个地方接住，否则会冒成 500、账本还按 200 记一行。
            #
            # 上游原文会写进服务端日志与账号的冷却原因，但**不回给客户端**：
            # 那里面常带渠道名、接口地址和账号标识，等于把账号池的构成
            # 直接从错误响应里送出去。客户端只拿一个能据此重试或报障的码。
            detail = str(e.body if isinstance(e, convert.UpstreamStatusError)
                         else e)
            if isinstance(e, convert.UpstreamStatusError):
                store.set_account_cooldown(account["id"], 60, detail)
                code, msg, wait = "upstream_status", "上游服务暂时不可用，请稍后重试", 0
            elif e.is_queued:
                code, msg, wait = "upstream_busy", "上游模型繁忙，请稍后重试", e.retry_after()
            elif e.is_rate_limited:
                code, msg, wait = "upstream_rate_limited", "上游限流，请稍后重试", e.retry_after()
            elif e.is_request_rejected:
                # 请求内容被上游拦下：账号是好的，换账号重发也一样被拦，
                # 所以不加冷却也不重试。原因要说给客户端，但不能带上游原文。
                code, msg, wait = "upstream_rejected", "请求被上游拒绝，请调整请求内容后重试", 0
            else:
                store.set_account_cooldown(account["id"], 60, detail)
                code, msg, wait = "upstream_error", "上游服务出错，请稍后重试", 0
            print(f"[upstream] {code} {detail[:500]}", file=sys.stderr)
            store.record_usage(
                key_id=key["id"], member_id=key["member_id"],
                account_id=account["id"], model=model,
                prompt_tokens=0, completion_tokens=0,
                status=429 if code not in ("upstream_error", "upstream_status") else 502,
                error=code, ip=ip,
                channel=(key.get("kind") or "plan"),
            )
            if getattr(self, "_stream_started", False):
                err = {"error": {"code": code, "message": msg, "type": code}}
                try:
                    self.wfile.write(convert.to_sse(json.dumps(err, ensure_ascii=False)))
                    self.wfile.write(convert.DONE)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True
                return
            return self._error(
                429 if code not in ("upstream_error", "upstream_status") else 502,
                code, msg, retry_after=wait)

        store.touch_account(account["id"])
        holder: dict = {}
        failure = {"code": "", "status": 200}

        def billing():
            """把这一轮的 token 按当时价目折成成本，返回 (cost, 来源)。

            append-only 的边界就在这里：价目在请求结束这一刻取一次，
            算出来的 cost 写进 usage 行后就固定了。之后管理员改单价，
            只影响新请求，旧行的 cost 与它贡献的额度百分比都不会变。

            可重复调用：非流式要在序列化前先算一次（让客户端看到的 cost 与
            账本一致），finally 里再算一次。每次都用 holder 里的最新用量，
            两处不会对不上。
            """
            usage = holder.get("usage") or {}
            holder["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
            holder["completion_tokens"] = int(usage.get("completion_tokens") or 0)
            holder["cache_read"] = int(holder.get("cache_read_tokens") or 0)
            holder["cache_write"] = int(holder.get("cache_write_tokens") or 0)
            # 按实际跑这个请求的那家来源计价：同一个对外模型在两家厂商的
            # 上游 key 不同，单价也可能不同。换过厂商时 model 已经改成了
            # 最终那家的 key，用它对目录里那条 route 取价。
            price, price_source = price_for({"model": model}, meta)
            cost = pricing.cost_of(
                price, prompt_tokens=holder["prompt_tokens"],
                completion_tokens=holder["completion_tokens"],
                cache_read_tokens=holder["cache_read"],
                cache_write_tokens=holder["cache_write"])
            holder["cost"] = cost
            return cost, price_source

        try:
            if stream:
                if not getattr(self, "_stream_started", False):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                try:
                    for chunk_json in convert.iter_chunks(lines, holder):
                        self.wfile.write(convert.to_sse(chunk_json))
                        self.wfile.flush()
                    self.wfile.write(convert.DONE)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # 客户端提前断开（关页面、切网络）在流式调用里很常见，
                    # 不是网关故障；不接住会每个未读完的请求打一份 traceback
                    failure = {"code": "client_disconnected", "status": 499}
                except convert.UpstreamStatusError as e:
                    failure = {"code": "upstream_status", "status": 502}
                    # 流已经开始写了，只能以一条错误事件收尾。上游原文进
                    # 日志，不进客户端：里面可能带渠道名与账号标识。
                    print(f"[upstream] upstream_status {str(e.body)[:500]}",
                          file=sys.stderr)
                    err = {"error": {"code": "upstream_status",
                                    "message": "上游服务暂时不可用，请稍后重试"}}
                    self.wfile.write(convert.to_sse(json.dumps(err)))
                    self.wfile.write(convert.DONE)
                    self.wfile.flush()
                self.close_connection = True
            else:
                # 先折叠流（会读完最后一帧的 usage），再算钱，最后才序列化：
                # 顺序倒了的话 aggregate 拿到的 cost 是 0，客户端看到的价
                # 和账本里的价就对不上。结算以 finally 那次为准，这里只是
                # 先把同一个数告诉客户端。
                try:
                    payload = convert.aggregate(lines, model, holder)
                except convert.UpstreamStatusError as e:
                    # 流还没开始写，能回一个正常的错误响应。不接住会冒成
                    # 500，而且 finally 里那行账会按 status=200 记下去。
                    failure = {"code": "upstream_status", "status": 502}
                    print(f"[upstream] upstream_status {str(e.body)[:500]}",
                          file=sys.stderr)
                    return self._error(502, "upstream_status",
                                       "上游服务暂时不可用，请稍后重试")
                billing()
                payload["usage"]["cost"] = round(holder["cost"], 8)
                self._send_json(200, payload)
        finally:
            cost, price_source = billing()
            store.record_usage(
                key_id=key["id"],
                member_id=key["member_id"],
                account_id=account["id"],
                model=model,
                prompt_tokens=holder["prompt_tokens"],
                completion_tokens=holder["completion_tokens"],
                cache_read_tokens=holder["cache_read"],
                cache_write_tokens=holder["cache_write"],
                cost=cost,
                price_source=price_source,
                credits=float(holder.get("credits") or 0),
                credits_original=float(holder.get("credits_original") or 0),
                status=failure["status"],
                error=failure["code"],
                ip=ip,
                channel=(key.get("kind") or "plan"),
            )
            # api 通道按次扣余额。plan 通道不扣：它走档位百分比，
            # 已经由 usage 的 cost 累计体现，再扣一次会重复计费。
            if key.get("kind") == "api" and key.get("member_id") and cost > 0:
                store.charge_api(key["member_id"], cost, f"调用 {model}")

    def _account_detail(self, account_id: int) -> dict:
        """单个账号的完整信息，账号详情页用。"""
        store = get_store()
        acc = store.get_account(account_id)
        if not acc:
            return {}
        prov = providers.get(acc.get("provider"))
        view = next((a for a in Handler._accounts_view(self) if a["id"] == account_id), {})
        models = []
        for m in all_models().values():
            # 同名模型会被合并成一条并带 routes，所以要按 routes 判断
            # 这家厂商能不能跑，只看 m["provider"] 会漏掉非默认路由
            if not any(r["provider"] == prov.slug for r in m.get("routes") or []):
                continue
            models.append({"id": m["id"], "name": m.get("name") or m["id"]})
        models.sort(key=lambda m: m["name"])
        return {
            **view,
            "credential_hint": prov.credential_hint,
            "credential_url": prov.credential_url,
            "credential_masked": _mask(acc.get("credential") or ""),
            "uid": acc["uid"],
            "created_at": acc["created_at"],
            "expires_in": max(0, int((acc["expires_at"] or 0) - time.time())),
            "totals": store.account_totals(account_id),
            "daily": store.daily_series_for_account(account_id, 14),
            "recent": store.usage_for_account(account_id, 40),
            "models": models,
        }

    # ---------------- 管理接口实现 ----------------

    def _add_account(self, body: dict | None = None):
        body = self._read_json() if body is None else body
        prov = providers.get(body.get("provider") or providers.DEFAULT_PROVIDER)
        credential = (body.get("credential") or body.get("pat") or "").strip()
        region = (body.get("region") or next(iter(prov.regions))).strip()
        if not prov.available:
            return self._error(400, "provider_pending",
                               f"{prov.label} 的接入协议尚未实现")
        if region not in prov.regions:
            return self._error(400, "bad_region",
                               f"{prov.label} 没有这个区域：{region}")
        if prov.credential_prefix and not credential.startswith(prov.credential_prefix):
            return self._error(400, "bad_credential",
                               f"凭证应以 {prov.credential_prefix} 开头")
        if not credential:
            return self._error(400, "bad_credential", "请填写凭证")

        try:
            info = prov.connect(credential, region)
        except providers.Unsupported:
            raise
        except qoder.UpstreamError as e:
            return self._error(400, "credential_rejected", str(e))
        if not info.get("uid"):
            return self._error(400, "uid_missing", "无法获取账号 UID")
        # 适配器可能从凭证里读出真实区域（WorkBuddy 的导出文件带 domain），
        # 与界面上选的不一致时以凭证为准
        region = info.get("region") or region

        account_id = get_store().add_account(
            provider=prov.slug, uid=info["uid"], region=region,
            credential=credential, email=info.get("email", ""),
            name=info.get("name", ""),
        )
        expires_at = time.time() + int(info.get("expires_in") or 86400)
        get_store().set_account_tokens(
            account_id, info.get("access_token", ""),
            info.get("refresh_token", ""), expires_at
        )
        acc = get_store().get_account(account_id)
        if acc:
            sync_account_credits(acc)
        clear_model_cache()
        return self._send_json(200, {"ok": True, "account": {
            "uid": info["uid"], "email": info.get("email", ""),
            "name": info.get("name", ""), "region": region,
            "provider": prov.slug,
        }})

    def _import_accounts(self):
        """从导出的账号文件批量接入。

        body: {"provider": "workbuddy", "files": [{"name": "cockpit-xxx.json",
               "content": "<文件原文>"}, ...]}

        一个 cockpit 文件里可能有多条账号，所以逐个接；单个失败不影响其余，
        结果逐条返回，前端能看出哪条进来了、哪条为什么没进来。
        """
        body = self._read_json()
        slug = (body.get("provider") or providers.DEFAULT_PROVIDER).strip()
        prov = providers.get(slug)
        if not prov.available:
            return self._error(400, "provider_pending",
                               f"{prov.label} 的接入协议尚未实现")
        files = body.get("files")
        if not isinstance(files, list) or not files:
            return self._error(400, "bad_request", "请选择要导入的文件")

        results = []
        for item in files:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "未命名文件")
            content = item.get("content")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False)
            content = str(content or "")
            if not content.strip():
                results.append({"file": name, "ok": False, "error": "文件是空的"})
                continue
            # 一个文件可能是单账号对象，也可能是数组
            entries = []
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as e:
                results.append({"file": name, "ok": False,
                                "error": f"不是合法 JSON：{e.msg}"})
                continue
            if isinstance(parsed, list):
                entries = parsed
            else:
                entries = [parsed]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                label = name if len(entries) == 1 else f"{name} #{len(results) + 1}"
                payload = {"provider": prov.slug,
                           "region": (body.get("region") or "").strip(),
                           "credential": json.dumps(entry, ensure_ascii=False)}
                result = self._accept_account(payload)
                results.append({"file": label, **result})
        imported = sum(1 for r in results if r.get("ok"))
        return self._send_json(200, {
            "results": results, "imported": imported,
            "failed": len(results) - imported,
            "accounts": self._accounts_view(),
        })

    def _accept_account(self, payload: dict) -> dict:
        """接一个账号，返回 {ok, uid, name, error}。不改 HTTP 状态。"""
        prov = providers.get(payload.get("provider") or providers.DEFAULT_PROVIDER)
        credential = (payload.get("credential") or "").strip()
        region = (payload.get("region") or "").strip() or next(iter(prov.regions))
        if region not in prov.regions:
            return {"ok": False, "error": f"{prov.label} 没有这个区域：{region}"}
        try:
            info = prov.connect(credential, region)
        except providers.Unsupported as e:
            return {"ok": False, "error": str(e)}
        except qoder.UpstreamError as e:
            return {"ok": False, "error": str(e)[:300]}
        if not info.get("uid"):
            return {"ok": False, "error": "无法获取账号 UID"}
        region = info.get("region") or region
        account_id = get_store().add_account(
            provider=prov.slug, uid=info["uid"], region=region,
            credential=credential, email=info.get("email", ""),
            name=info.get("name", ""),
        )
        expires_at = time.time() + int(info.get("expires_in") or 86400)
        get_store().set_account_tokens(
            account_id, info.get("access_token", ""),
            info.get("refresh_token", ""), expires_at)
        acc = get_store().get_account(account_id)
        quota = sync_account_credits(acc) if acc else None
        clear_model_cache()
        return {"ok": True, "account_id": account_id, "uid": info["uid"],
                "name": info.get("name") or info["uid"][:8], "region": region,
                "quota": quota}

    def _account_action(self):
        body = self._read_json()
        action = body.get("action")
        try:
            account_id = int(body.get("id"))
        except (TypeError, ValueError):
            return self._error(400, "bad_request", "账号 ID 无效")
        store = get_store()
        if action == "enable":
            store.set_account_enabled(account_id, True)
            store.set_account_cooldown(account_id, 0, "")
        elif action == "disable":
            store.set_account_enabled(account_id, False)
        elif action == "reset":
            store.set_account_cooldown(account_id, 0, "")
            acc = store.get_account(account_id)
            if acc:
                store.set_account_tokens(account_id, acc["access_token"],
                                         acc["refresh_token"], 0)
        else:
            return self._error(400, "bad_action", "未知操作")
        clear_model_cache()
        return self._send_json(200, {"ok": True})

    def _sync_credits(self):
        body = self._read_json()
        store = get_store()
        only = (body.get("provider") or "").strip()
        targets = ([store.get_account(int(body["id"]))] if body.get("id")
                   else [a for a in store.list_accounts()
                         if not only or a.get("provider") == only])
        out = []
        for acc in targets:
            if not acc:
                continue
            prov = providers.get(acc.get("provider"))
            if not prov.available:
                out.append({"id": acc["id"], "ok": False,
                            "error": f"{prov.label} 暂不支持额度查询"})
                continue
            q = sync_account_credits(acc)
            out.append({"id": acc["id"], "ok": bool(q), "quota": q})
        return self._send_json(200, {"results": out})

    def _checkin(self):
        """一键签到 / 单账号签到。body 带 id 就只签那个账号。"""
        body = self._read_json()
        store = get_store()
        if body.get("id"):
            try:
                targets = [store.get_account(int(body["id"]))]
            except (TypeError, ValueError):
                return self._error(400, "bad_request", "账号 ID 无效")
            targets = [a for a in targets if a]
            if not targets:
                return self._error(404, "account_not_found", "账号不存在")
            # 单账号签到也要尊重厂商能力，否则点了只会报错
            for a in targets:
                if not providers.get(a.get("provider")).supports_checkin:
                    return self._error(
                        400, "no_checkin",
                        f"{providers.get(a.get('provider')).label} 没有签到活动")
        else:
            # 带 provider 就只签这一家的账号（厂商页面上的按钮）
            only = (body.get("provider") or "").strip()
            targets = [a for a in store.list_accounts()
                       if a["enabled"]
                       and (not only or a.get("provider") == only)
                       and providers.get(a.get("provider")).supports_checkin]
            if not targets:
                return self._send_json(200, {"results": [], "earned": 0,
                                             "accounts": self._accounts_view()})

        results = []
        for acc in targets:
            try:
                results.append(checkin_one(acc))
            except Exception as e:  # noqa: BLE001
                store.set_account_checkin(acc["id"], ok=False, message=str(e)[:200])
                results.append({
                    "account_id": acc["id"],
                    "name": acc["name"] or acc["email"],
                    "ok": False, "earned": 0, "done": False,
                    "message": f"签到异常：{e}", "quota": None,
                })
        return self._send_json(200, {
            "results": results,
            "earned": round(sum(r.get("earned") or 0 for r in results), 4),
            "accounts": self._accounts_view(),
        })

    def _topup_member(self):
        """给成员的 API 通道发放额度。amount 可正可负：负数用于冲正记错的账。"""
        path = urlparse(self.path).path
        try:
            mid = int(path.rsplit("/", 2)[-2])
        except ValueError:
            return self._error(400, "bad_member", "成员 id 不合法")
        store = get_store()
        if not store.get_member(mid):
            return self._error(404, "member_not_found", "成员不存在")
        body = self._read_json()
        try:
            amount = float(body.get("amount") or 0)
        except (TypeError, ValueError):
            return self._error(400, "bad_amount", "金额必须是数字")
        if amount == 0:
            return self._error(400, "bad_amount", "金额不能为 0")
        note = (body.get("note") or "").strip()[:120]
        balance = store.api_topup(mid, amount, note)
        return self._send_json(200, {"ok": True, "balance": balance})

    def _create_member(self):
        body = self._read_json()
        name = (body.get("name") or "").strip()
        if not name:
            return self._error(400, "bad_request", "成员名字不能为空")
        username = (body.get("username") or "").strip()
        if username and get_store().get_member_by_username(username):
            return self._error(409, "username_taken", "该账号已存在")
        tier = str(body.get("tier") or "plus").strip().lower()
        if tier not in plans.PLAN_BY_SLUG:
            return self._error(400, "bad_tier",
                               f"档位只能是 {'/'.join(plans.PLAN_BY_SLUG)}")
        mid = get_store().create_member(
            name=name,
            email=(body.get("email") or "").strip(),
            tier=tier,
            limit_daily=float(body.get("limit_daily") or 0),
            limit_total=float(body.get("limit_total") or 0),
            preferred_account_id=body.get("preferred_account_id") or None,
            # 不传可用模型就是「不限」。以前默认写死 Qoder 的三个 Flash 档，
            # 账号来自别的厂商时新成员一上手就被判 model_forbidden。
            model_allowlist=body.get("model_allowlist") or "",
            note=(body.get("note") or "").strip(),
            username=username or None,
            # 初始密码固定成 DEFAULT_PASSWORD。管理员不替成员定密码，
            # 成员登录后自己改，改了之后管理员也看不到、改不了。
            password=DEFAULT_PASSWORD,
        )
        return self._send_json(200, {"ok": True, "id": mid,
                                     "default_password": DEFAULT_PASSWORD})

    # 成员自助管理密钥：每人一把。密钥就是成员的调用凭据，不属于管理员的
    # 可见范围，所以这里只允许动自己名下那把。
    # 成员可建多把密钥，各自命名，方便按用途区分（一个客户端一把）。
    # 上限只是防止无限刷，够用即可。
    MAX_KEYS_PER_MEMBER = 20

    def _create_own_key(self, ctx: dict):
        member = ctx["member"]
        store = get_store()
        mine = store.list_keys(member["id"])
        if len(mine) >= self.MAX_KEYS_PER_MEMBER:
            return self._error(409, "key_limit",
                               f"你已经有 {len(mine)} 把密钥了，"
                               f"最多 {self.MAX_KEYS_PER_MEMBER} 把，"
                               f"删掉不用的再建新的")
        body = self._read_json()
        # 名字是成员自己起的标签，只用于在列表里区分
        name = (body.get("name") or body.get("note") or "").strip()[:40]
        # 通道：plan 走档位额度，api 走赠礼余额。成员可以自己建两种密钥，
        # 但 api 密钥没有余额就调不通（网关会 429），所以不会白给用量。
        kind = (body.get("kind") or "plan").strip().lower()
        if kind not in ("plan", "api"):
            return self._error(400, "bad_kind", "通道只能是 plan 或 api")
        # 余额为 0 时不许建 api 密钥：这条通道的准入只看余额，建出来第一
        # 次调用就会被 429 挡掉，而成员看到的却是一把「创建成功」的密钥，
        # 只会以为什么配置错了。真要按余额扣就得先让管理员发放。
        if kind == "api" and store.api_balance(member["id"]) <= 0:
            return self._error(
                409, "api_balance_empty",
                "API 通道还没有余额，需要管理员先发放才能建这把密钥；"
                "现在调用请用 PLAN 通道的密钥",
            )
        key_id, raw = store.create_key(
            member_id=member["id"],
            ip_allowlist="",      # 来源限制属于平台策略，成员界面上不开放
            model_allowlist="",   # 可用模型由管理员在成员上配置
            note=name,
            kind=kind,
        )
        # 明文只在这里出现一次
        return self._send_json(200, {"ok": True, "id": key_id,
                                     "name": name, "kind": kind,
                                     "prefix": raw[:16], "key": raw,
                                     "shown_once": True})

    def _change_own_password(self, ctx: dict):
        body = self._read_json()
        old = body.get("old_password") or ""
        new = body.get("new_password") or ""
        member = ctx["member"]
        if not Store.verify_password(old, member.get("password_hash") or ""):
            return self._error(403, "bad_password", "原密码不正确")
        if len(new) < 6:
            return self._error(400, "weak_password", "新密码至少 6 位")
        if new == old:
            return self._error(400, "same_password", "新密码不能和原密码相同")
        get_store().set_password(member["id"], new)
        # 密码变了，旧会话一并作废，重新登录才能继续用
        return self._send_json(200, {"ok": True})

    # ---------------- 定价 ----------------

    def _pricing_view(self, key: dict | None = None,
                      member: dict | None = None) -> dict:
        """定价页的数据：目录里每个模型的每一家来源都列出实际生效的单价。

        同一对外模型在两家厂商的上游 key 不同，价目也可能不同，所以管理端
        按 (厂商, 上游 key) 逐条列，而不是按模型只列一行。

        传 key/member 时是成员自己看的那张表：只给他白名单内、且**按模型**
        去重后的一行。厂商、上游 key、来源 spec 都是路由与勾选粒度，成员
        不需要也不应该看到，所以这几列在成员视图里根本不返回。
        """
        store = get_store()
        overrides = price_overrides(store)
        models = all_models()
        for_member = key is not None or member is not None
        allowed = (guard.allowed_models(key or {}, member) if for_member else None)
        rows = []
        for mid, meta in sorted(models.items()):
            name = meta.get("name") or mid
            routes = guard.allowed_routes(allowed, mid, models)
            if not for_member:
                # allowed_routes 返回的是目录里的 route（key 叫 model），
                # 这里换成界面与白名单共用的那种写法
                for route in routes:
                    price, kind = pricing.lookup(
                        route["model"], name, mid, overrides)
                    rows.append({
                        "model_id": mid,
                        "model_name": name,
                        "provider": route["provider"],
                        "provider_label": provider_label(route["provider"]),
                        "provider_color": provider_color(route["provider"]),
                        "upstream_key": route["model"],
                        "spec": f"{route['provider']}:{route['model']}",
                        "price": price,
                        "source": kind,
                    })
                continue
            if not routes:
                continue
            # 成员按模型一行。同一个模型后面有几家上游时，实际按哪家的价
            # 收取决于这次请求分到了哪个账号，两边价目不一致就标成区间，
            # 别拿其中一份冒充全部。
            prices = [pricing.lookup(r["model"], name, mid, overrides)
                      for r in routes]
            first = prices[0][0]
            row = {"model_id": mid, "model_name": name, "price": dict(first),
                   "source": prices[0][1]}
            if any(p[0] != first for p in prices[1:]):
                row["price_range"] = {
                    f: [min(p[0].get(f, 0) for p in prices),
                        max(p[0].get(f, 0) for p in prices)]
                    for f in pricing.PRICE_FIELDS}
            rows.append(row)
        view = {
            "rows": rows,
            "overrides": overrides,
            "default_price": pricing.DEFAULT_PRICE,
            "fields": list(pricing.PRICE_FIELDS),
            "currency": pricing.CURRENCY,
            "currency_symbol": pricing.CURRENCY_SYMBOL,
            "retired": sorted(pricing.RETIRED),
            # 参考值：opencode Go 公布的价格表，方便对账
            "builtin": pricing.BUILTIN,
        }
        if for_member:
            # 成员只看自己那几行单价：覆盖表与内置参考表是全量口径，
            # 下架名单与默认价也是运营信息，一并去掉
            for k in ("overrides", "builtin", "retired", "default_price"):
                view.pop(k, None)
        return view

    def _save_pricing(self):
        """保存定价覆盖。body: {"prices": {"上游 key": {input, output, ...}}}

        传空对象表示清空该条覆盖（回到内置表）；整份 prices 为空则清空全部。
        只接受四个单价字段，其余键忽略。

        只改“以后怎么算”：已经写进 usage 的 cost 是定值，不会被这次保存影响。
        这是 append-only 的直接后果——改价不等于重算历史账。
        """
        body = self._read_json()
        raw = body.get("prices")
        if not isinstance(raw, dict):
            return self._error(400, "bad_request", "缺少 prices")
        clean: dict[str, dict[str, float]] = {}
        for key, price in raw.items():
            name = str(key or "").strip()
            if not name or not isinstance(price, dict):
                continue
            row = {}
            for f in pricing.PRICE_FIELDS:
                if f not in price:
                    continue
                try:
                    value = float(price[f])
                except (TypeError, ValueError):
                    return self._error(400, "bad_request",
                                       f"{name} 的 {f} 不是数字")
                if value < 0:
                    return self._error(400, "bad_request",
                                       f"{name} 的 {f} 不能为负")
                row[f] = value
            if row:
                clean[name] = row
        get_store().set_setting("model_prices", clean)
        return self._send_json(200, {"ok": True, "count": len(clean),
                                     "overrides": clean})

    # ---------------- 视图 ----------------

    @staticmethod
    def _no_account_reason(provider, region: str) -> str:
        """没有可用账号时，说清到底是没配、全停了、还是都在冷却。

        这段文字会作为 503 的 message 回给 API 调用方，所以不能写厂商名、
        区域和上游错误原文：那些都是账号池的构成信息。具体是哪家、冷却到
        多久、报了什么，管理员在账号页看得到。
        """
        label = "该模型当前"
        rows = [a for a in STORE.list_accounts(region, provider=provider.slug)] \
            if STORE else []
        if not rows:
            return f"{label}没有可用账号，请联系管理员开通"
        now = time.time()
        enabled = [a for a in rows if a.get("enabled")]
        if not enabled:
            return f"{label}没有可用账号，请联系管理员开通"
        cooling = [a for a in enabled if (a.get("cooldown_until") or 0) > now]
        if len(cooling) == len(enabled):
            left = int(min(a["cooldown_until"] for a in cooling) - now) + 1
            return f"{label}繁忙，{left} 秒后重试"
        return f"{label}没有可用账号，请联系管理员开通"

    def _accounts_view(self) -> list[dict]:
        store = get_store()
        stats = store.account_summary()
        all_members = store.list_members()
        # 账号 -> 绑定到它的成员名字
        bound_map: dict[int, list[str]] = {}
        for m in all_members:
            pid = m["preferred_account_id"]
            if pid:
                bound_map.setdefault(pid, []).append(m["name"])
        out = []
        for a in store.list_accounts():
            st = stats.get(a["id"]) or {}
            prov = providers.get(a.get("provider"))
            out.append({
                "id": a["id"], "uid": a["uid"], "region": a["region"],
                "provider": prov.slug,
                "provider_label": prov.label,
                "provider_color": prov.color,
                "provider_available": prov.available,
                "supports_checkin": prov.supports_checkin,
                "region_label": prov.region_label(a["region"]),
                "email": a["email"], "name": a["name"],
                "enabled": bool(a["enabled"]),
                "cooldown": max(0, int(a["cooldown_until"] - time.time())),
                "last_error": a["last_error"],
                "expires_in": max(0, int(a["expires_at"] - time.time())),
                # 全部额度：套餐内 + 赠送/签到 + 共享池
                "credits_total": a["credits_total"],
                "credits_used": a["credits_used"],
                "credits_remaining": a["credits_remaining"],
                "credits_addon": a["credits_addon"],
                "credits_base_total": a["credits_base_total"],
                "credits_base_used": a["credits_base_used"],
                "credits_base_remaining": a["credits_base_remaining"],
                "credits_addon_total": a["credits_addon_total"],
                "credits_addon_used": a["credits_addon_used"],
                "credits_addon_remaining": a["credits_addon"],
                "credits_shared_total": a["credits_shared_total"],
                "credits_shared_used": a["credits_shared_used"],
                "credits_shared_remaining": a["credits_shared_remaining"],
                "credits_expire_at": a["credits_expire_at"],
                "credits_exceeded": bool(a["credits_exceeded"]),
                "plan_name": a["plan_name"],
                "synced_ago": (max(0, int(time.time() - a["credits_synced_at"]))
                               if a["credits_synced_at"] else None),
                # 网关记账：请求量 / Token / 成本，按模型拆分
                "gateway_cost": st.get("cost", 0.0),
                "gateway_credits": st.get("credits", 0.0),
                "requests": st.get("requests", 0),
                "errors": st.get("errors", 0),
                "success_rate": st.get("success_rate"),
                "tokens": st.get("tokens", 0),
                "by_model": st.get("by_model", []),
                "checkin_at": a["checkin_at"] or None,
                "checkin_ok": bool(a["checkin_ok"]),
                "checkin_earned": a["checkin_earned"],
                "checkin_message": a["checkin_message"],
                "bound_members": bound_map.get(a["id"], []),
            })
        return out

    def _members_view(self) -> list[dict]:
        store = get_store()
        accounts = {a["id"]: a for a in store.list_accounts()}
        api_balances = store.api_balances()
        catalog = all_models()
        names = public_model_names(catalog, store)
        out = []
        for m in store.list_members():
            # 百分比用累加口径：每个模型的已花/它自己的额度，再相加。
            # 这是网关真正拿来拦的数，列表上显示的应该就是它。
            quota = member_quota(store, m, catalog, names)
            st = {k: v for k, v in quota.items() if k != "model_quota"}
            keys = store.list_keys(m["id"])
            pref = accounts.get(m["preferred_account_id"] or 0)
            # 该成员的请求量 / Token / 积分，总盘 + 按模型
            by_model = store.member_model_summary(m["id"])
            requests = sum(int(r["n"] or 0) for r in by_model)
            errors = sum(int(r["err"] or 0) for r in by_model)
            tokens = sum(int(r["tokens"] or 0) for r in by_model)
            ok = sum(int(r["ok"] or 0) for r in by_model)
            out.append({
                "id": m["id"], "name": m["name"], "email": m["email"],
                "username": m.get("username") or "",
                "is_admin": bool(m.get("is_admin")),
                "enabled": bool(m["enabled"]),
                "tier": st["tier"],
                "api_balance": api_balances.get(m["id"], 0.0),
                "limit_daily": st["limit_daily"],
                "limit_total": st["limit_total"],
                "used_today": st["used_today"],
                "used_total": st["used_total"],
                "left_today": st["left_today"],
                "left_total": st["left_total"],
                "pct_today": st["pct_today"],
                "pct_total": st["pct_total"],
                "requests": requests,
                "errors": errors,
                "success_rate": (round(ok * 100.0 / requests, 2) if requests else None),
                "tokens": tokens,
                "by_model": [{
                    "id": r["model"] or "",
                    "requests": int(r["n"] or 0),
                    "ok": int(r["ok"] or 0),
                    "errors": int(r["err"] or 0),
                    "prompt_tokens": int(r["prompt_tokens"] or 0),
                    "completion_tokens": int(r["completion_tokens"] or 0),
                    "tokens": int(r["tokens"] or 0),
                    "cost": round(float(r["cost"] or 0), 6),
                    "credits": round(float(r["credits"] or 0), 4),
                } for r in by_model],
                "model_allowlist": m["model_allowlist"],
                "note": m["note"],
                # 成员级手工额度（0 = 跟随档位），上面的 limit_daily/limit_total
                # 是含档位回退的生效值。两个不一样才说明被手工覆盖过。
                "override_daily": float(m.get("limit_daily") or 0),
                "override_total": float(m.get("limit_total") or 0),
                "model_quota": quota["model_quota"]["models"],
                "preferred_account_id": m["preferred_account_id"],
                "preferred_account": pref["name"] or pref["email"] if pref else "",
                "keys": [{"id": k["id"], "prefix": k["key_prefix"],
                          "kind": k.get("kind") or "plan",
                          "enabled": bool(k["enabled"]),
                          "ip_allowlist": k["ip_allowlist"],
                          "model_allowlist": k["model_allowlist"],
                          "note": k["note"]}
                         for k in keys],
            })
        return out

    def _keys_view(self) -> list[dict]:
        """密钥管理页的数据。带用量，并标出所属成员名。"""
        store = get_store()
        members = {m["id"]: m["name"] for m in store.list_members()}
        out = []
        for k in store.key_summary():
            out.append({
                "id": k["id"],
                "member_id": k["member_id"],
                "member": members.get(k["member_id"] or 0, ""),
                "prefix": k["key_prefix"],
                # 管理员看不到明文：密钥是成员自己的东西，库里也只有 hash。
                # 成员创建时自己保存，丢了就由成员自己重置。
                "key": "",
                "enabled": bool(k["enabled"]),
                "ip_allowlist": k["ip_allowlist"] or "",
                "model_allowlist": k["model_allowlist"] or "",
                "note": k["note"] or "",
                "requests": int(k["n"] or 0),
                "ok": int(k["ok"] or 0),
                "tokens": int(k["tokens"] or 0),
                "cost": round(float(k["cost"] or 0), 6),
                "last_used": k["last_used"],
                "created_at": k["created_at"],
            })
        return out

    def _analytics(self) -> dict:
        store = get_store()
        catalog = all_models()
        members = {m["id"]: m["name"] for m in store.list_members()}
        accounts = {a["id"]: (a["name"] or a["email"]) for a in store.list_accounts()}

        # usage 行存的是上游 key（dfmodel / deepseek-v4.1-flash），而目录合并后
        # 同一个模型只有一条显示名。建一份反向表，让历史行也归到同一行，
        # 否则同一个模型会在表里拆成两行，看起来像两个不同的模型。
        key_to_name = stable_model_names(store)

        def model_name(mid: str) -> str:
            return key_to_name.get(mid) or (catalog.get(mid) or {}).get("name") \
                or mid or "未知"

        per_member = []
        for row in store.member_summary():
            mid = row["member_id"]
            n = int(row["n"] or 0)
            ok = int(row["ok"] or 0)
            # 只剩真实存在的成员：已删除成员 / 未关联记录要占名额归不到人，
            # 混进成员榜会和顶部「成员 N 人」对不上。
            if mid not in members:
                continue
            per_member.append({
                "id": mid,
                "name": members[mid],
                "requests": n,
                "errors": int(row["err"] or 0),
                "success_rate": round(ok * 100.0 / n, 2) if n else None,
                "cost": round(float(row["cost"] or 0), 6),
                "tokens": int(row["tokens"] or 0),
            })
        per_member.sort(key=lambda x: x["cost"], reverse=True)

        # 账号归属：每个账号的成本与请求量
        by_account = []
        account_meta = {a["id"]: a for a in store.list_accounts()}
        for aid, st in store.account_summary().items():
            meta = account_meta.get(aid) or {}
            prov = providers.get(meta.get("provider"))
            by_account.append({
                "id": aid,
                "name": accounts.get(aid, f"账号 {aid}"),
                "provider": prov.slug,
                "provider_label": prov.label,
                "provider_color": prov.color,
                "cost": st["cost"],
                "requests": st["requests"],
                "tokens": st["tokens"],
                "success_rate": st["success_rate"],
            })
        # 没分配账号就失败的请求（no_account 等）不带 account_id，
        # account_summary 会跳过它们，补一行让各维度合计对上总请求量
        orphan = store.unassigned_summary()
        if orphan["requests"]:
            by_account.append({
                "id": None, "name": "未分配到账号",
                "provider": "", "provider_label": "无",
                "provider_color": "var(--ink-faint)",
                **orphan,
            })
        by_account.sort(key=lambda x: x["cost"], reverse=True)

        by_model: dict[str, dict] = {}
        for m in store.model_summary():
            name = model_name(m["model"])
            cell = by_model.setdefault(name, {
                "model": name, "id": m["model"] or "", "requests": 0,
                "ok": 0, "errors": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "tokens": 0, "cost": 0.0,
            })
            cell["requests"] += int(m["n"] or 0)
            cell["ok"] += int(m["ok"] or 0)
            cell["errors"] += int(m["err"] or 0)
            cell["prompt_tokens"] += int(m["prompt_tokens"] or 0)
            cell["completion_tokens"] += int(m["completion_tokens"] or 0)
            cell["tokens"] += int(m["tokens"] or 0)
            cell["cost"] += float(m["cost"] or 0)
        by_model_rows = []
        for cell in by_model.values():
            cell["cost"] = round(cell["cost"], 6)
            cell["success_rate"] = (round(cell["ok"] * 100.0 / cell["requests"], 2)
                                    if cell["requests"] else None)
            by_model_rows.append(cell)
        by_model_rows.sort(key=lambda x: x["cost"], reverse=True)
        by_model = by_model_rows

        all_time = store.usage_totals(0)
        # 数据页的图：近 14 天趋势、近 24 小时、模型堆叠、Token 分布、热力图
        provider_rows = []
        prov_stats = {p["slug"]: p for p in
                      Handler._provider_stats(store.list_accounts())}
        prov_usage = store.provider_summary()
        account_prov = {a["id"]: a.get("provider", "qoder")
                        for a in store.list_accounts()}
        agg: dict[str, dict] = {}
        for aid, row in prov_usage.items():
            slug = account_prov.get(aid, "qoder")
            cell = agg.setdefault(slug, {"cost": 0.0, "requests": 0,
                                         "ok": 0, "tokens": 0})
            cell["cost"] += float(row["cost"] or 0)
            cell["requests"] += int(row["n"] or 0)
            cell["ok"] += int(row["ok"] or 0)
            cell["tokens"] += int(row["tokens"] or 0)
        # 没落到任何账号的请求也计入厂商榜，否则厂商合计比总请求量少
        orphan = store.unassigned_summary()
        if orphan["requests"]:
            cell = agg.setdefault("unknown", {"cost": 0.0, "requests": 0,
                                              "ok": 0, "tokens": 0})
            cell["cost"] += float(orphan["cost"] or 0)
            cell["requests"] += int(orphan["requests"] or 0)
            cell["ok"] += int(orphan["ok"] or 0)
            cell["tokens"] += int(orphan["tokens"] or 0)
        for slug, cell in agg.items():
            meta = prov_stats.get(slug) or {}
            provider_rows.append({
                "slug": slug,
                "label": meta.get("label") or ("未分配" if slug == "unknown" else slug),
                "color": meta.get("color", "var(--ink-faint)"),
                "cost": round(cell["cost"], 6),
                "requests": cell["requests"],
                "tokens": cell["tokens"],
                "success_rate": (round(cell["ok"] * 100.0 / cell["requests"], 2)
                                 if cell["requests"] else None),
            })
        provider_rows.sort(key=lambda r: r["cost"], reverse=True)

        return {
            "daily": store.daily_series(14),
            "hourly": store.hourly_series(24),
            "model_pivot": store.models_pivot(14, key_to_name),
            "token_hist": store.token_distribution(),
            "heatmap": store.usage_heatmap(14),
            "by_provider": provider_rows,
            "by_model": by_model,
            "by_member": per_member,
            "by_account": by_account,
            # 模型表现在按显示名合并，前端用它把上游 key 也对回显示名
            "model_names": key_to_name,
            "totals": {
                "requests": all_time["requests"],
                "errors": all_time["errors"],
                "success_rate": all_time["success_rate"],
                "tokens": all_time["tokens"],
                "requests_7d": sum(d["requests"] for d in store.daily_series(7)),
                # 从日线算，不要从按成员的榜累加：榜里不含已删除成员的记录
                "cost": all_time["cost"],
                "cost_7d": round(sum(d["cost"] for d in store.daily_series(7)), 6),
                "tokens_7d": sum(d["tokens"] for d in store.daily_series(7)),
            },
        }

    # ---------------- 公开页面数据（/data） ----------------

    # 公开数据页的维度：哪些模型、多少天。页面上都给成固定值，
    # 不做参数化，避免每次请求都重新扫全表。
    PUBLIC_DAYS = 60

    def _public_data(self) -> dict:
        """公开数据页的全部数字。

        不需要登录：这是对外的「用量看板」，只给聚合后的 token / 请求 /
        金额与模型名，不带成员、账号、密钥任何可识别信息。成员名一律以
        「成员 03」这种序号出现。
        """
        store = get_store()
        catalog = all_models()
        key_to_name = stable_model_names(store)
        names = {mid: (meta.get("name") or mid) for mid, meta in catalog.items()}

        def display(key: str) -> str:
            if not key:
                return "未知模型"
            return key_to_name.get(key) or names.get(key) or key

        days = self.PUBLIC_DAYS
        daily = store.public_daily(days)
        hourly = store.public_hourly(48)
        heatmap = store.public_heatmap(28)
        matrix = store.public_model_matrix(days)
        member_matrix = store.public_member_matrix(days)
        # 榜单与上面的堆叠图用同一个窗口（PUBLIC_DAYS）。若这里改成 7 天，
        # cost_per_session 就会变成「7 天成本 ÷ 60 天会话数」，算出来的数
        # 只是看着合理，实际上没有任何意义。
        totals = store.public_model_totals(days)
        sessions = store.public_sessions(days)

        labels = [d["date"] for d in daily]
        index = {d["date"]: i for i, d in enumerate(daily)}

        # ---- 按模型 × 日期的 token 与请求堆叠 ----
        per_model_tokens: dict[str, list[int]] = {}
        per_model_requests: dict[str, list[int]] = {}
        per_model_users: dict[str, list[int]] = {}
        per_model_cost: dict[str, list[float]] = {}
        start = matrix["start"]
        for row in matrix["rows"]:
            d = int(row["d"])
            if not 0 <= d < days:
                continue
            label = time.strftime("%Y-%m-%d", time.localtime(
                day_start(time.time() - (days - 1) * 86400) + d * 86400))
            pos = index.get(label)
            if pos is None:
                continue
            name = display(row["model"])
            per_model_tokens.setdefault(name, [0] * days)[pos] += int(row["tokens"])
            per_model_requests.setdefault(name, [0] * days)[pos] += int(row["n"])
            per_model_users.setdefault(name, [0] * days)[pos] += int(row["users"] or 0)
            per_model_cost.setdefault(name, [0.0] * days)[pos] += float(row["cost"] or 0)

        def stack(per: dict, top: int = 9) -> dict:
            ranked = sorted(per.items(), key=lambda kv: -sum(kv[1]))[:top]
            series = [{"name": k, "values": [round(v, 6) for v in vals]}
                      for k, vals in ranked]
            if len(per) > top:
                rest = [0] * days
                for k, vals in per.items():
                    if k in {n for n, _ in ranked}:
                        continue
                    for i, v in enumerate(vals):
                        rest[i] += v
                series.append({"name": "其他", "values": [round(v, 6) for v in rest]})
            return {"labels": labels, "series": series}

        # ---- 成员 × 日期的 token，成员按「成员 01」编号 ----
        member_ids = sorted({int(r["member_id"]) for r in member_matrix["rows"]})
        member_label = {mid: f"成员 {i + 1:02d}" for i, mid in enumerate(member_ids)}
        per_member_tokens: dict[str, list[int]] = {}
        for row in member_matrix["rows"]:
            d = int(row["d"])
            if not 0 <= d < days:
                continue
            label = time.strftime("%Y-%m-%d", time.localtime(
                day_start(time.time() - (days - 1) * 86400) + d * 86400))
            pos = index.get(label)
            if pos is None:
                continue
            name = member_label.get(int(row["member_id"]), "其他")
            per_member_tokens.setdefault(name, [0] * days)[pos] += int(row["tokens"])

        # ---- 按模型作者 × 日期的 token（市场份额图） ----
        # 只取当日有量的作者，颜色与图标来自 labs 表；名称用作者自己的
        # 写法（「阿里」这种中文），与榜单卡片上的作者名一致。
        per_lab_tokens: dict[str, list[int]] = {}
        per_lab_meta: dict[str, dict] = {}
        base_ts = day_start(time.time() - (days - 1) * 86400)
        for row in matrix["rows"]:
            d = int(row["d"])
            if not 0 <= d < days:
                continue
            label = time.strftime("%Y-%m-%d", time.localtime(base_ts + d * 86400))
            pos = index.get(label)
            if pos is None:
                continue
            name = display(row["model"])
            lab = labs.lab_of(name)
            per_lab_tokens.setdefault(lab["slug"], [0] * days)[pos] += int(row["tokens"])
            per_lab_meta.setdefault(lab["slug"], lab)

        lab_series = {
            "labels": labels,
            "series": [
                {"name": per_lab_meta[slug]["label"],
                 "slug": slug,
                 "color": per_lab_meta[slug]["color"],
                 "icon": per_lab_meta[slug]["icon"],
                 "values": [round(v, 6) for v in vals]}
                for slug, vals in sorted(per_lab_tokens.items(),
                                         key=lambda kv: -sum(kv[1]))
            ],
        }
        lab_totals: dict[str, dict] = {}
        for item in lab_series["series"]:
            total = int(sum(item["values"]))
            if not total:
                continue
            slug = item["slug"]
            cell = lab_totals.setdefault(slug, {
                "slug": slug, "label": item["name"],
                "color": item["color"], "icon": item["icon"],
                "icon_url": (f"/assets/model-icons.svg#{item['icon']}"
                             if item["icon"] else ""),
                "tokens": 0, "models": 0,
            })
            cell["tokens"] += total
        all_tokens = sum(c["tokens"] for c in lab_totals.values()) or 1
        for cell in lab_totals.values():
            cell["share"] = round(cell["tokens"] * 100.0 / all_tokens, 1)
        # 模型数量在榜单算完之后再补，那一轮才知道每个作者有几个模型

        # ---- 模型榜（与图表同窗口） ----
        merged: dict[str, dict] = {}
        for row in totals:
            name = display(row["model"])
            cell = merged.setdefault(name, {
                "model": name, "requests": 0, "ok": 0, "tokens": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "cost": 0.0, "users": 0,
            })
            for key in ("requests", "tokens", "prompt_tokens",
                        "completion_tokens", "cache_read_tokens",
                        "cache_write_tokens"):
                cell[key] += int(row["n"] if key == "requests" else row[key])
            cell["ok"] += int(row["ok"] or 0)
            cell["cost"] += float(row["cost"] or 0)
            cell["users"] += int(row["users"] or 0)
        # 会话聚合（会话成本、留存）也按显示名合并
        session_by_model: dict[str, dict] = {}
        for cell in sessions["models"]:
            name = display(cell["model"])
            acc = session_by_model.setdefault(
                name, {"model": name, "sessions": 0, "tokens": 0, "cost": 0.0})
            acc["sessions"] += int(cell["sessions"])
            acc["tokens"] += int(cell["tokens"])
            acc["cost"] += float(cell["cost"])
        retention: dict[str, dict] = {}
        for cell in sessions["retention"]:
            name = display(cell["model"])
            acc = retention.setdefault(name, {"model": name, "rate": 0.0,
                                              "eligible": 0})
            # 各上游 key 合并后按可留存人数加权平均
            total = acc["eligible"] + int(cell["eligible"])
            if total:
                acc["rate"] = round(
                    (acc["rate"] * acc["eligible"] + cell["rate"] * cell["eligible"])
                    / total, 1)
            acc["eligible"] = total

        leaderboard = []
        for cell in merged.values():
            lead = session_by_model.get(cell["model"]) or {}
            sessions_n = int(lead.get("sessions") or 0)
            # 公开页的缓存比例按「命中缓存的输入 / 全部输入」算。
            # 这里 prompt_tokens 已含缓存部分，与计费口径一致：
            # 因此分母就是 prompt_tokens 本身，不再额外加缓存。
            cell["cache_rate"] = (round(cell["cache_read_tokens"] * 100.0
                                        / cell["prompt_tokens"], 1)
                                  if cell["prompt_tokens"] else None)
            cell["success_rate"] = (round(cell["ok"] * 100.0 / cell["requests"], 1)
                                    if cell["requests"] else None)
            cell["cost"] = round(cell["cost"], 6)
            # 「实付均价」：窗口内真金白银花掉的成本 ÷ 总 token。这是唯一
            # 把缓存命中算进去的单价——牌价加权（下方 cost_per_mtok）只看
            # 输入/输出两档，命中率高的模型会被显示成比实际贵十倍。
            cell["effective_mtok"] = (round(cell["cost"] * 1e6 / cell["tokens"], 4)
                                      if cell["tokens"] and cell["cost"] else None)
            cell["cost_per_session"] = (round(cell["cost"] / sessions_n, 6)
                                        if sessions_n else None)
            cell["tokens_per_session"] = (int(cell["tokens"] / sessions_n)
                                          if sessions_n else None)
            cell["sessions"] = sessions_n
            price, kind = pricing.lookup(cell["model"], cell["model"], "",
                                        price_overrides(store))
            cell["price"] = price
            cell["price_source"] = kind
            # 「每 100 万 token 的牌价」用输入价的加权口径：一次典型请求里
            # 输入占九成以上（缓存命中后的输入 + 少量输出），单看输出价会
            # 高估好几倍。它不含缓存价，只用来横向比「同样的 token 构成下
            # 谁更便宜」；页面上真正展示的是上面那个实付均价。
            cell["cost_per_mtok"] = round(
                price["input"] * 0.9 + price["output"] * 0.1, 4)
            ret = retention.get(cell["model"]) or {}
            cell["retention"] = ret.get("rate")
            cell["retention_eligible"] = int(ret.get("eligible") or 0)
            # 作者（lab）与图标：市场份额那一节按作者分组，排行卡片用作者的图标
            lab = labs.lab_of(cell["model"])
            cell["lab"] = lab["slug"]
            cell["lab_label"] = lab["label"]
            cell["lab_color"] = lab["color"]
            cell["lab_icon"] = lab["icon"]
            cell["lab_icon_url"] = (f"/assets/model-icons.svg#{lab['icon']}"
                                    if lab["icon"] else "")
            # 「较上周」：拿同一个窗口内的最近 7 天与前 7 天比。窗口不足
            # 两周时没有对比基线，两个字段都给 None，页面不显示涨幅也不
            # 标「新」——那两种写法都会把“没数据”说成“涨了”。
            series = per_model_tokens.get(cell["model"]) or []
            # 排行榜的两个窗口：本周（最近 7 天）与当日（最后一天）。
            # 卡片上的数字用它们，切换「每周 / 每日」时换的是同一批模型的
            # 不同窗口，而不是把 60 天的合计硬当成「每日」。
            cell["tokens_week"] = int(sum(series[-7:])) if series else 0
            cell["tokens_day"] = int(series[-1]) if series else 0
            if len(series) >= 14:
                now_week = sum(series[-7:])
                prev_week = sum(series[-14:-7])
                cell["change_pct"] = (round((now_week - prev_week) * 100.0
                                            / prev_week)
                                      if prev_week else None)
                cell["is_new"] = bool(now_week and not prev_week)
            else:
                cell["change_pct"] = None
                cell["is_new"] = False
            leaderboard.append(cell)
        leaderboard.sort(key=lambda c: (-c["tokens"], -c["requests"]))

        # 只列真的有花费的会话：成本为 0 的要么是失败的重试，要么是上游没报
        # token 的空调用，把它们排进榜里会显示成 ¥0 霸占前几名。
        session_rows = sorted(
            (c for c in session_by_model.values() if c["sessions"] and c["cost"] > 0),
            key=lambda c: -(c["cost"] / c["sessions"]))
        cache_rows = sorted(
            (c for c in merged.values() if c["cache_rate"] is not None),
            key=lambda c: -c["cache_rate"])
        retention_rows = sorted(
            (c for c in retention.values() if c["eligible"] >= 1),
            key=lambda c: (-c["rate"], -c["eligible"]))

        overall = store.usage_totals(0)
        cache_read = sum(int(m.get("cache_read_tokens") or 0) for m in merged.values())
        prompt_all = sum(int(m.get("prompt_tokens") or 0) for m in merged.values())
        uncached = max(0, prompt_all - cache_read)
        # 每个作者名下有几个模型，榜单算完才知道
        labs_of_model = {c["model"]: c["lab"] for c in leaderboard}
        for c in lab_totals.values():
            c["models"] = sum(1 for slug in labs_of_model.values()
                              if slug == c["slug"])
        return {
            "generated_at": int(time.time()),
            "days": days,
            "daily": daily,
            "hourly": hourly,
            "heatmap": heatmap,
            "labels": labels,
            "stack_models": stack(per_model_tokens),
            "stack_users": stack(per_model_users),
            "stack_requests": stack(per_model_requests, top=7),
            "stack_members": stack(per_member_tokens, top=8),
            "stack_cost": stack(per_model_cost, top=7),
            "stack_labs": lab_series,
            "labs": sorted(lab_totals.values(), key=lambda c: -c["tokens"]),
            "leaderboard": leaderboard,
            "session_costs": [
                {"model": c["model"],
                 "cost_per_session": round(c["cost"] / c["sessions"], 6),
                 "tokens_per_session": int(c["tokens"] / c["sessions"]),
                 "sessions": c["sessions"]}
                for c in session_rows],
            "cache_rates": [
                {"model": c["model"], "rate": c["cache_rate"],
                 "cached": c["cache_read_tokens"],
                 "uncached": max(0, c["prompt_tokens"] - c["cache_read_tokens"])}
                for c in cache_rows],
            "retention": retention_rows,
            "totals": {
                "requests": overall["requests"],
                "errors": overall["errors"],
                "tokens": overall["tokens"],
                "cost": overall["cost"],
                "models": len(leaderboard),
                "members": len(member_ids),
                "cache_read_tokens": cache_read,
                "uncached_tokens": uncached,
                "cache_rate": (round(cache_read * 100.0 / prompt_all, 1)
                               if prompt_all else None),
            },
            "assumptions": plans.describe(catalog, price_overrides(store))["assumptions"],
        }

    def _public_compare(self, names: list[str]) -> dict:
        """两个模型的对照数据，公开页 /data/compare/<a>/<b> 用。

        只给聚合数字与单价：不出现账号、成员、密钥。名字按显示名匹配，
        匹配不上就返回 error —— 页面据此显示「模型不存在」，而不是拼一张
        空表出来让人以为一切正常。
        """
        data = self._public_data()
        by_name = {c["model"]: c for c in data["leaderboard"]}

        def find(raw: str):
            name = (raw or "").strip()
            if name in by_name:
                return by_name[name]
            # URL 里习惯写 kebab-case，模型名里可能是空格或点
            norm = re.sub(r"[^a-z0-9]", "", name.lower())
            for key, cell in by_name.items():
                if re.sub(r"[^a-z0-9]", "", key.lower()) == norm:
                    return cell
            return None

        picked = []
        for raw in names:
            cell = find(raw)
            if cell is None:
                return {"error": f"没有找到模型：{raw}"}
            picked.append(cell)

        # 每根柱子背后的每日 token：两模型各取一条 60 天的线
        store = get_store()
        days = data["days"]
        matrix = store.public_model_matrix(days)
        key_to_name = stable_model_names(store)
        wanted = {c["model"]: [0] * days for c in picked}
        for row in matrix["rows"]:
            name = (key_to_name.get(row["model"]) or row["model"]
                    or "未知模型")
            if name in wanted:
                d = int(row["d"])
                if 0 <= d < days:
                    wanted[name][d] += int(row["tokens"])

        models = []
        for cell in picked:
            series = wanted[cell["model"]]
            models.append({
                "model": cell["model"],
                "lab": cell["lab"],
                "lab_label": cell["lab_label"],
                "lab_color": cell["lab_color"],
                "lab_icon": cell["lab_icon"],
                "lab_icon_url": cell["lab_icon_url"],
                "tokens": cell["tokens"],
                "tokens_week": cell["tokens_week"],
                "tokens_day": cell["tokens_day"],
                "change_pct": cell["change_pct"],
                "share": round(cell["tokens"] * 100.0
                               / (data["totals"]["tokens"] or 1), 2),
                "users": cell["users"],
                "sessions": cell["sessions"],
                "cost": cell["cost"],
                "cost_per_mtok": cell["cost_per_mtok"],
                "effective_mtok": cell["effective_mtok"],
                "price": cell["price"],
                "cache_rate": cell["cache_rate"],
                "retention": cell["retention"],
                "retention_eligible": cell["retention_eligible"],
                "requests": cell["requests"],
                "success_rate": cell["success_rate"],
                "daily": series,
            })
        return {"models": models, "labels": data["labels"],
                "days": days, "generated_at": data["generated_at"]}

    @staticmethod
    def models_view(models: dict, price_overrides: dict | None = None) -> list[dict]:
        """模型目录的对外形状，管理界面与 /v1/models 共用同一份“来源”描述。

        一个模型只有一条记录，`sources` 列出背后每一家上游（厂商 + 区域 +
        上游 key + 写白名单用的 spec）。管理端按 sources 逐条勾选，网关按同一种
        spec 判断能不能走，所以两边不会对不上。

        每条来源带上它的单价：同一个对外模型在两家厂商的上游 key 不同，
        价目也可能不同，定价页要能逐条看到实际算的是哪一份。
        """
        out = []
        for mid in sorted(models):
            meta = models[mid]
            sources = guard.model_sources(mid, models)
            for src in sources:
                price, source_kind = pricing.lookup(
                    src["upstream_key"], meta.get("name") or mid, mid,
                    price_overrides)
                src["price"] = price
                src["price_source"] = source_kind
            out.append({
                "id": mid,
                "name": meta.get("name") or mid,
                "provider": meta.get("provider", providers.DEFAULT_PROVIDER),
                "sources": sources,
                # 历史 usage 行存的是上游 key（dfmodel 这种），前端
                # 靠这个列表把老记录对回合并后的显示名
                "upstream_keys": [r["model"] for r in
                                  meta.get("routes") or []],
            })
        return out

    def _admin_status(self) -> dict:
        store = get_store()
        accounts = store.list_accounts()
        members = store.list_members()
        models = all_models() if accounts else {}
        # 上游目录读不到时（账号刚接入还没同步、或全部在冷却），成员页仍然要能
        # 配置模型，所以退回默认的那几个档位并标记为 provisional。
        provisional = False
        if not models:
            provisional = True
            # 退回默认档位时也要带一条来源，否则成员页勾不出可用厂商
            models = {mid: {"name": mid, "provider": providers.DEFAULT_PROVIDER,
                            "routes": [{"provider": providers.DEFAULT_PROVIDER,
                                        "region": "global", "model": mid}]}
                      for mid in DEFAULT_MODELS.split(",") if mid}
        today = store.usage_totals(day_start())
        all_time = store.usage_totals(0)
        return {
            "accounts": len(accounts),
            "available": sum(1 for a in accounts
                             if a["enabled"] and a["cooldown_until"] < time.time()),
            "members": len(members),
            "keys": len(store.list_keys()),
            "models": self.models_view(models, price_overrides(store)),
            "model_ids": sorted(models),
            "models_provisional": provisional,
            "providers": providers.describe_all(),
            "provider_stats": self._provider_stats(accounts),
            "credits_remaining": round(
                sum(a["credits_remaining"] for a in accounts), 2),
            "credits_total": round(sum(a["credits_total"] for a in accounts), 2),
            "credits_used": round(sum(a["credits_used"] for a in accounts), 2),
            # 请求量 / Token / 成本 · 成功率
            "today": today,
            "all_time": all_time,
            "checkin_pending": sum(1 for a in accounts if not a["checkin_at"]
                                   and providers.get(a["provider"]).supports_checkin),
        }

    @staticmethod
    def _provider_stats(accounts: list[dict]) -> list[dict]:
        """按厂商聚合账号数、可用数、额度，供账号页的厂商卡片用。"""
        now = time.time()
        out = []
        for prov in providers.PROVIDERS.values():
            rows = [a for a in accounts if a.get("provider") == prov.slug]
            total = sum(float(a["credits_total"] or 0) for a in rows)
            remaining = sum(float(a["credits_remaining"] or 0) for a in rows)
            out.append({
                **prov.describe(),
                "accounts": len(rows),
                # 别用 available：describe() 里那个是同名字段表示「协议已实现」
                "available_accounts": sum(1 for a in rows
                                          if a["enabled"] and a["cooldown_until"] < now),
                "credits_total": round(total, 2),
                "credits_remaining": round(remaining, 2),
                "credits_used": round(sum(float(a["credits_used"] or 0)
                                          for a in rows), 2),
            })
        return out

    def _member_detail(self, member_id: int) -> dict:
        """单个成员的详情：额度、按账号拆开的消耗、按模型拆开的消耗、密钥与近期请求。

        管理员靠这个判断谁在吃资源、吃的是哪个上游账号，从而决定以后
        怎么分配额度。

        **消耗按账号所属厂商的计费口径分别记。** Qoder 与 WorkBuddy 的上游
        是按积分扣额度的，这类账号的消耗就记成积分（credits），金额那列只是
        按 token 价估算出来的参考值；其他厂商按 token 计费，记成元（cost）与
        token 数。两种口径不能相加，也不是同一个量纲，所以账号、厂商、模型
        三个维度都带上 unit，前端分开画图；需要跨口径合计时只能看请求数。
        """
        store = get_store()
        member = store.get_member(member_id)
        if not member:
            return {}
        st = guard.quota_status(store, member)
        accounts = {a["id"]: a for a in store.list_accounts()}
        # 按模型的额度：默认就是档位额度，管理员可按模型单独改
        catalog = all_models()
        model_allow = guard.allowed_models({}, member)
        full_quota = member_quota(store, member, catalog,
                                  public_model_names(catalog, store))
        st = {k: v for k, v in full_quota.items() if k != "model_quota"}
        model_quota_rows = full_quota["model_quota"]["models"]
        # 弹窗里能改哪些模型：目录里存在、且这个成员可用的那些。
        # 已下架但历史上用过的模型不给改，额度按档位默认算就行。
        editable = _model_quota_editable(store, member, catalog, model_allow,
                                         model_quota_rows)

        by_account_model = store.member_account_summary(member_id)
        # 账号被删掉后它的历史消耗还在库里，靠积分有没有记过来反推是哪类厂商
        hist_credits: dict[int, float] = {}
        for r in by_account_model:
            aid = int(r["account_id"] or 0)
            hist_credits[aid] = hist_credits.get(aid, 0.0) + float(r["credits"] or 0)

        def unit_of(aid: int) -> str:
            acc = accounts.get(aid)
            if acc:
                return providers.billing_unit(acc.get("provider"))
            return "credit" if hist_credits.get(aid, 0.0) > 0 else "token"

        # 先按账号聚成两层：账号 → {合计，各模型}
        acc_map: dict[int, dict] = {}
        prov_map: dict[str, dict] = {}
        for r in by_account_model:
            aid = int(r["account_id"] or 0)
            acc = accounts.get(aid) or {}
            unit = unit_of(aid)
            pslug = acc.get("provider") or ""
            # 两种「不是正常账号」的情况要分开：请求根本没选到账号
            # （no_account、全在冷却），消耗为 0；或者账号之后被删了，
            # 那次调用的 token 和积分是真实发生的，只是账号记录不在了。
            slot = acc_map.setdefault(aid, {
                "account_id": aid,
                "unbound": not acc and not aid,
                "deleted": bool(aid) and not acc,
                "name": (acc.get("name") or acc.get("email")
                         or ("已删除账号 #%d" % aid if aid else "未分配账号")),
                "provider": pslug,
                "region": acc.get("region") or "",
                "unit": unit,
                "requests": 0, "errors": 0, "tokens": 0,
                "cost": 0.0, "credits": 0.0,
                "models": [],
            })
            pslot = None if not acc else prov_map.setdefault(pslug, {
                "slug": pslug,
                "label": provider_label(pslug),
                "color": provider_color(pslug),
                "unit": providers.billing_unit(pslug),
                "accounts": 0, "requests": 0, "errors": 0, "tokens": 0,
                "cost": 0.0, "credits": 0.0,
            })
            for target in (slot, pslot):
                if target is None:
                    continue
                target["requests"] += int(r["n"] or 0)
                target["errors"] += int(r["err"] or 0)
                target["tokens"] += int(r["tokens"] or 0)
                target["cost"] += float(r["cost"] or 0)
                target["credits"] += float(r["credits"] or 0)
            slot["models"].append({
                "model": r["model"] or "（未记录）",
                "requests": int(r["n"] or 0),
                "ok": int(r["ok"] or 0),
                "errors": int(r["err"] or 0),
                "prompt_tokens": int(r["prompt_tokens"] or 0),
                "completion_tokens": int(r["completion_tokens"] or 0),
                "tokens": int(r["tokens"] or 0),
                "cost": round(float(r["cost"] or 0), 6),
                "credits": round(float(r["credits"] or 0), 4),
            })
        for slot in acc_map.values():
            slot["accounts"] = 1
            slot["credits_remaining"] = round(
                float((accounts.get(slot["account_id"]) or {}).get("credits_remaining") or 0), 2)
            slot["credits_total"] = round(
                float((accounts.get(slot["account_id"]) or {}).get("credits_total") or 0), 2)
        for pslot in prov_map.values():
            pslot["accounts"] = sum(1 for a in acc_map.values() if a["provider"] == pslot["slug"])

        # 排序按口径各自的价值：积分账号看积分，token 账号看元
        by_account = list(acc_map.values())
        for a in by_account:
            a["cost"] = round(a["cost"], 6)
            a["credits"] = round(a["credits"], 4)
            a["amount"] = a["credits"] if a["unit"] == "credit" else a["cost"]
            a["models"].sort(key=lambda x: -(
                x["credits"] if a["unit"] == "credit" else x["cost"]))
        by_account.sort(key=lambda x: (0 if x["unit"] == "credit" else 1, -x["amount"]))

        by_provider = list(prov_map.values())
        # 厂商下面再挂一层模型：用量明细要能按厂商展开，省一次请求
        prov_models: dict[str, dict[str, dict]] = {}
        for slot in acc_map.values():
            if slot.get("unbound"):
                continue
            bucket = prov_models.setdefault(slot["provider"], {})
            for m in slot["models"]:
                cell = bucket.setdefault(m["model"], {
                    "model": m["model"], "requests": 0, "ok": 0, "errors": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "tokens": 0, "cost": 0.0, "credits": 0.0,
                })
                for f in ("requests", "ok", "errors", "prompt_tokens",
                          "completion_tokens", "tokens"):
                    cell[f] += m[f]
                cell["cost"] += m["cost"]
                cell["credits"] += m["credits"]
        for p in by_provider:
            p["cost"] = round(p["cost"], 6)
            p["credits"] = round(p["credits"], 4)
            p["amount"] = p["credits"] if p["unit"] == "credit" else p["cost"]
            rows = list(prov_models.get(p["slug"], {}).values())
            for cell in rows:
                cell["unit"] = p["unit"]
                cell["cost"] = round(cell["cost"], 6)
                cell["credits"] = round(cell["credits"], 4)
                cell["amount"] = (cell["credits"] if p["unit"] == "credit"
                                  else cell["cost"])
            rows.sort(key=lambda x: -x["amount"])
            p["models"] = rows
        by_provider.sort(key=lambda x: (0 if x["unit"] == "credit" else 1, -x["amount"]))

        by_model = store.member_model_summary(member_id)
        models = [{
            "model": r["model"] or "（未记录）",
            "requests": int(r["n"] or 0),
            "ok": int(r["ok"] or 0),
            "errors": int(r["err"] or 0),
            "prompt_tokens": int(r["prompt_tokens"] or 0),
            "completion_tokens": int(r["completion_tokens"] or 0),
            "tokens": int(r["tokens"] or 0),
            "cost": round(float(r["cost"] or 0), 6),
            "credits": round(float(r["credits"] or 0), 4),
        } for r in by_model]

        # 按模型 × 口径拆开，画两张榜：一个模型可能同时被两家厂商跑过，
        # 那一行的积分与元要分别落在各自的榜里
        model_unit: dict[tuple[str, str], dict] = {}
        for slot in acc_map.values():
            # 未分配账号的行没有上游，谈不上结算口径，不进模型榜
            if slot.get("unbound"):
                continue
            unit = slot["unit"]
            for m in slot["models"]:
                key = (m["model"], unit)
                cell = model_unit.setdefault(key, {
                    "model": key[0], "unit": unit,
                    "requests": 0, "ok": 0, "errors": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "tokens": 0, "cost": 0.0, "credits": 0.0,
                })
                for f in ("requests", "ok", "errors", "prompt_tokens",
                          "completion_tokens", "tokens"):
                    cell[f] += m[f]
                cell["cost"] += m["cost"]
                cell["credits"] += m["credits"]
        by_model_unit = list(model_unit.values())
        for cell in by_model_unit:
            cell["cost"] = round(cell["cost"], 6)
            cell["credits"] = round(cell["credits"], 4)
            cell["amount"] = cell["credits"] if cell["unit"] == "credit" else cell["cost"]
        by_model_unit.sort(key=lambda x: (0 if x["unit"] == "credit" else 1, -x["amount"]))

        requests = sum(m["requests"] for m in models)
        errors = sum(m["errors"] for m in models)
        ok = sum(m["ok"] for m in models)

        # 每种口径的合计：积分账号只累计积分，token 账号只累计 token 与元
        billing: dict[str, dict] = {
            "credit": {"unit": "credit", "accounts": 0, "requests": 0,
                       "tokens": 0, "cost": 0.0, "credits": 0.0},
            "token": {"unit": "token", "accounts": 0, "requests": 0,
                      "tokens": 0, "cost": 0.0, "credits": 0.0},
        }
        for a in by_account:
            # 没落到账号的请求根本没走到上游，不属于任何一种结算方式，
            # 只计进 unbound，否则某个口径的请求数会比实际调用还多
            if a.get("unbound"):
                continue
            b = billing[a["unit"]]
            b["accounts"] += 1
            b["requests"] += a["requests"]
            b["tokens"] += a["tokens"]
            b["cost"] += a["cost"]
            b["credits"] += a["credits"]
        for b in billing.values():
            b["cost"] = round(b["cost"], 6)
            b["credits"] = round(b["credits"], 4)

        # 每日 × 账号，画堆叠趋势图用；两种口径各画一张，缺的账号补 0
        daily = store.member_daily_by_account(member_id, 14, unit_of=unit_of)
        acc_ids = [a["account_id"] for a in by_account]

        def daily_rows(unit: str) -> list[dict]:
            ids = [a["account_id"] for a in by_account if a["unit"] == unit]
            return [{"date": d["date"],
                     "by_account": {aid: d["by_account"].get(aid, 0) for aid in ids},
                     "total": round(sum(d["by_account"].get(aid, 0) for aid in ids), 6),
                     # 量本身不分口径：顶部卡片要的是全部请求的量
                     "requests": d.get("requests", 0),
                     "tokens": d.get("tokens", 0),
                     "cost": d.get("cost", 0.0)}
                    for d in daily]

        return {
            "member": {
                "id": member["id"], "name": member["name"],
                "email": member["email"], "username": member.get("username") or "",
                "is_admin": bool(member.get("is_admin")),
                "enabled": bool(member["enabled"]),
                "tier": st["tier"],
                "note": member["note"],
                "model_allowlist": member["model_allowlist"],
                "preferred_account_id": member["preferred_account_id"],
                "created_at": member["created_at"],
                # 成员级手工额度（0 = 跟随档位）。quota 里的是生效值，
                # 这里给原始列：两者不一致时才看得出被手工覆盖过，
                # 否则界面上永远是个看不见也改不掉的数字。
                "limit_daily": float(member.get("limit_daily") or 0),
                "limit_total": float(member.get("limit_total") or 0),
            },
            "quota": st,
            # 每个模型自己的额度与已用（含目录里没有的历史模型）
            "model_quota": model_quota_rows,
            "model_quota_editable": editable,
            # API 通道：余额与流水。plan 走档位百分比，api 走这里。
            # topup_total 是累计发放额（只算正数），界面上「累计发放」用它，
            # 没有它这一格就只能拿余额冒充，花多少都显示成 0。
            # 成员详情接口把 topup_total 一起给出来，管理页的「累计发放」
            # 要用它，否则只能拿余额冒充。
            "api": {
                "balance": store.api_balance(member_id),
                "topup_total": store.api_topup_total(member_id),
                "ledger": store.api_ledger(member_id, 20),
            },
            "totals": {
                "requests": requests,
                "ok": ok,
                "errors": errors,
                "success_rate": (round(ok * 100.0 / requests, 2) if requests else None),
                "tokens": sum(m["tokens"] for m in models),
                "prompt_tokens": sum(m.get("prompt_tokens", 0) for m in models),
                "completion_tokens": sum(m.get("completion_tokens", 0)
                                         for m in models),
                "cost": round(sum(m["cost"] for m in models), 6),
                "credits": round(sum(m["credits"] for m in models), 4),
                # 落到真实账号的才有账号数；未分配的失败请求不算一个账号
                "accounts": sum(1 for a in by_account if not a.get("unbound")),
            },
            "by_account": [a for a in by_account if not a.get("unbound")],
            # 没落到账号就失败的请求：单独一行，不占账号位的占比
            "unbound": [a for a in by_account if a.get("unbound")],
            "by_provider": by_provider,
            "by_model": models,
            "by_model_unit": by_model_unit,
            "billing": billing,
            "daily": {"credit": daily_rows("credit"),
                      "token": daily_rows("token")},
            # 不分口径的日总量：顶部卡片看的是全部请求，不管账号按哪种计费
            "daily_all": [{"date": d["date"], "requests": d.get("requests", 0),
                           "tokens": d.get("tokens", 0), "cost": d.get("cost", 0.0)}
                          for d in daily],
            "account_ids": acc_ids,
            "keys": [{"id": k["id"], "prefix": k["key_prefix"],
                      "kind": k.get("kind") or "plan",
                      "enabled": bool(k["enabled"]), "note": k["note"],
                      "model_allowlist": k["model_allowlist"],
                      "ip_allowlist": k["ip_allowlist"]}
                     for k in store.list_keys(member_id)],
            # 首页调用明细：真正的分页数据走 /api/members/<id>/usage
            "recent": store.usage_for_member(member_id, 20),
            "recent_total": store.usage_count(member_id),
        }

    def _self_usage(self, ctx: dict, q: dict) -> dict:
        """成员自己的调用明细，分页口径与管理员看的那个接口一致。"""
        member_id = ctx["member"]["id"]
        store = get_store()
        days = _clamp_int(q.get("days", ["0"])[0], 0, 3650, 0)
        page = _clamp_int(q.get("page", ["1"])[0], 1, 10_000, 1)
        size = _clamp_int(q.get("size", ["20"])[0], 1, 200, 20)
        since = day_start(time.time() - (days - 1) * 86400) if days else 0.0
        # 通道筛选：两条通道是两套账，混在一起看不出哪边在花钱
        channel = (q.get("channel", [""])[0] or "").strip().lower()
        if channel not in ("plan", "api"):
            channel = ""
        total = store.usage_count(member_id, since, channel=channel)
        pages = max(1, (min(total, USAGE_PAGE_MAX) + size - 1) // size)
        page = min(page, pages)
        offset = (page - 1) * size
        rows = []
        if offset < USAGE_PAGE_MAX:
            rows, _ = store.usage_page(
                member_id, since=since, channel=channel,
                limit=min(size, USAGE_PAGE_MAX - offset), offset=offset)
        # 成员看不到账号：只回模型和时间，账号列从前端就不存在。
        # 模型名同样要翻成对外显示名，库里存的是上游 key。
        names = public_model_names(all_models(), store)
        return {
            "rows": [{**{k: r.get(k) for k in
                        ("created_at", "prompt_tokens", "completion_tokens",
                         "total_tokens", "cost", "status", "error")},
                      "channel": (r.get("channel") or "plan"),
                      "model": (names.get(r.get("model") or "")
                                or names.get((r.get("model") or "").lower())
                                or (r.get("model") or ""))} for r in rows],
            "total": total, "capped": min(total, USAGE_PAGE_MAX),
            "cap": USAGE_PAGE_MAX, "page": page, "size": size,
            "days": days, "pages": pages, "channel": channel,
        }

    @staticmethod
    def _member_self_stats(store, member_id: int,
                           names: dict[str, str] | None = None) -> dict:
        """成员自己看的用量：只有模型、元、Token，没有厂商和积分。

        账号是平台侧的概念——成员既不知道也不该关心自己的请求落到哪台
        订阅账号上，更不该看到上游按积分计费的原始数字（那和扣他的元
        是两套账）。所以这里刻意只回模型维度的汇总。

        usage 行存的是上游 key，所以汇总前先按对外显示名合并：同一个模型
        在两家厂商的 key 不同（dfmodel / deepseek-v4.1-flash），不合并
        就会在成员的表里拆成两行，名字还是没见过缩写。
        """
        names = names if names is not None else public_model_names()
        rows = store.member_model_summary(member_id)
        merged: dict[str, dict] = {}
        for r in rows:
            raw = r["model"] or ""
            label = names.get(raw) or names.get(raw.lower()) or raw or "（未记录）"
            cell = merged.setdefault(label, {
                "model": label, "requests": 0, "ok": 0, "errors": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
                "tokens": 0, "cost": 0.0,
            })
            cell["requests"] += int(r["n"] or 0)
            cell["ok"] += int(r["ok"] or 0)
            cell["errors"] += int(r["err"] or 0)
            for f in ("prompt_tokens", "completion_tokens", "cache_read_tokens",
                      "cache_write_tokens", "tokens"):
                cell[f] += int(r[f] or 0)
            cell["cost"] += float(r["cost"] or 0)
        models = []
        for cell in merged.values():
            cell["cost"] = round(cell["cost"], 6)
            cell["success_rate"] = (
                round(cell["ok"] * 100.0 / cell["requests"], 2)
                if cell["requests"] else None)
            # 缓存命中率：读到的缓存 token 占「输入 + 缓存读」的比例。
            # 分母不含输出的原因：缓存只作用于输入侧。
            cell["cache_rate"] = _cache_rate(cell["cache_read_tokens"],
                                             cell["prompt_tokens"])
            models.append(cell)
        models.sort(key=lambda x: -x["cost"])
        requests = sum(m["requests"] for m in models)
        ok = sum(m["ok"] for m in models)
        return {
            "models": models,
            "totals": {
                "requests": requests,
                "ok": ok,
                "errors": sum(m["errors"] for m in models),
                "success_rate": (round(ok * 100.0 / requests, 2)
                                 if requests else None),
                "tokens": sum(m["tokens"] for m in models),
                "prompt_tokens": sum(m["prompt_tokens"] for m in models),
                "completion_tokens": sum(m["completion_tokens"] for m in models),
                "cache_read_tokens": sum(m["cache_read_tokens"] for m in models),
                "cache_write_tokens": sum(m["cache_write_tokens"] for m in models),
                "cache_rate": _cache_rate(
                    sum(m["cache_read_tokens"] for m in models),
                    sum(m["prompt_tokens"] for m in models)),
                "cost": round(sum(m["cost"] for m in models), 6),
            },
        }

    def _member_dashboard(self, ctx: dict, days: int = 14) -> dict:
        store = get_store()
        member = ctx["member"]
        key = ctx.get("key")
        accounts = {a["id"]: a for a in store.list_accounts()}
        allowed = guard.allowed_models(key or {}, member)
        models = all_models()
        names = public_model_names(models, store)
        stats = Handler._member_self_stats(store, member["id"], names)
        # 日线：不分口径的全部请求，成员只看自己。days=0 表示全部历史
        daily_all = store.daily_series_for_member(member["id"], days)

        def label(raw: str) -> str:
            return names.get(raw) or names.get(str(raw).lower()) or raw

        # 模型单价与额度按「模型」一行。额度树是两层的：每个模型有自己的额度
        # 与百分比，累加到 100% 就停整个 plan 通道（见 guard.model_quota）。
        overrides = price_overrides(store)
        permitted = [mid for mid in models
                     if guard.allowed_routes(allowed, mid, models)]
        quota = member_quota(store, member, models, names, allowed)
        quota_rows = {c["label"]: c for c in quota["model_quota"]["models"]}
        price_rows = {}
        for mid in permitted:
            meta = models[mid]
            name = meta.get("name") or mid
            routes = guard.allowed_routes(allowed, mid, models)
            prices = [pricing.lookup(r["model"], name, mid, overrides)
                      for r in routes]
            first = prices[0][0]
            cell = {"id": mid, "name": name, "price": dict(first),
                    "cache_write": float(first.get("cache_write") or 0)}
            if any(p[0] != first for p in prices[1:]):
                cell["price_range"] = {
                    f: [min(p[0].get(f, 0) for p in prices),
                        max(p[0].get(f, 0) for p in prices)]
                    for f in pricing.PRICE_FIELDS}
            price_rows[name] = cell

        return {
            "member": {
                "id": member["id"], "name": member["name"],
                "email": member["email"], "username": member.get("username") or "",
                # 库里存的是来源 spec，这里翻成模型名；直接返回 spec 等于
                # 把「哪家厂商、上游叫什么」写进控制台
                "model_allowlist": Handler._member_allowlist_names(allowed, models),
            },
            "key": {
                "prefix": (key or {}).get("key_prefix", ""),
                # 明文只在创建那一刻回显一次，之后库里只有 hash，这里恒为空。
                # 控制台显示前缀，完整密钥由成员自己在创建时保存。
                "value": "",
            },
            # 成员名下的全部密钥，带各自用量。成员可以建多把、自己命名，
            # 按客户端分开放，删一把不影响其它。
            # 这里不返回 ip_allowlist：来源限制是平台策略，成员不需要看。
            # kind 要带上，且要带上该通道自己的用量：两件事分开统，
            # 否则成员分不清哪把密钥在花哪个池子。
            "keys": [{
                "id": k["id"],
                "name": k["note"] or "",
                "prefix": k["key_prefix"],
                "kind": k.get("kind") or "plan",
                "enabled": bool(k["enabled"]),
                "requests": k["n"],
                "tokens": k["tokens"],
                "cost": k["cost"],
                "last_used": k["last_used"],
                "created_at": k["created_at"],
            } for k in store.key_summary() if k["member_id"] == member["id"]],
            # 对外口径的额度（档位那一份数与百分比）+ 网关实际的累加百分比
            "quota": quota,
            # 两条通道分开的用量小计。plan 扣档位百分比，api 扣赠礼余额，
            # 加在一起没有意义，所以前端分两块展示。
            "channels": store.channel_stats(member["id"]),
            # API 通道余额与流水：成员要能看出自己还有多少、花在哪
            "api": {"balance": store.api_balance(member["id"]),
                    "ledger": store.api_ledger(member["id"], 20)},
            # 模型详情：成员要判断能不能放长文、能不能传图、以及这个模型自己
            # 的额度还剩多少，就靠这几项。price 是算钱用的那三列单价，
            # 模型限额按同一份价目算，两者必须同源。
            "models": [{
                "id": mid,
                "name": m.get("name", mid),
                "context_length": m.get("context_length") or 0,
                "max_output_tokens": m.get("max_output_tokens") or 0,
                "supports_images": bool(m.get("supports_images")),
                "supports_tools": bool(m.get("supports_tools")),
                "is_reasoning": bool(m.get("is_reasoning")),
                "price": price_rows.get(m.get("name", mid), {}).get("price", {}),
                "price_range": price_rows.get(m.get("name", mid), {}).get(
                    "price_range"),
                # 额度：日/两个窗口的金额上限、已用、百分比。
                # 没有调用过的模型也有这一项（默认额度就是这么铺出来的）。
                **{k: v for k, v in
                   (quota_rows.get(m.get("name", mid)) or {}).items()
                   if k in ("limit_daily", "limit_total", "used_today",
                            "used_total", "pct_daily", "pct_total",
                            "blocked", "overridden")},
            } for mid, m in sorted(models.items())
               # 白名单里存的是“厂商:上游key”，不能只比合并后的 id
               if guard.allowed_routes(allowed, mid, models)],
            "daily_range": days,
            "daily": store.daily_series_for_member(member["id"], days),
            # 按模型汇总（成本、请求数、Token、成功率）。成员看的是自己，
            # 所以不拆厂商、不露积分：积分是上游的计费单位，和扣他的元
            # 是两套账，露出来只会让人怀疑「为什么用得多扣得少」。
            "by_model": stats["models"],
            # 不分口径的日总量，给趋势图和顶部卡片用
            "daily_all": daily_all,
            "totals": stats["totals"],
            # 最近调用：只回公开展示需要的字段。account_id 指向哪台订阅
            # 账号、credits 是上游的记账单位、ip 是来源地址，都是平台侧
            # 的信息，不该跟着这个接口出去。
            "recent": [{**{k: r[k] for k in
                        ("created_at", "prompt_tokens",
                         "completion_tokens", "total_tokens", "cost",
                         "status", "error")},
                       # 通道决定这行扣的是档位额度还是赠礼余额，
                       # 不标出来成员会以为两边都扣了
                       "channel": (r.get("channel") or "plan"),
                       "model": label(r["model"] or "")}
                       for r in store.usage_for_member(member["id"], 20)],
        }

    @staticmethod
    def _member_allowlist_names(allowed: set[str] | None, models: dict) -> list[str] | str:
        """白名单翻成成员看得懂的写法。

        库里存的是来源 spec（workbuddy:deepseek-v4.1-flash），那是管理端的
        勾选粒度。成员只该看到「哪些模型能用」，所以只列**当前目录里真的
        有这条来源**的模型名；翻不到就丢掉，不把厂商与上游 key 透出去。
        """
        if allowed is None:
            return "不限"
        out = set()
        for mid, meta in models.items():
            if guard.allowed_routes(allowed, mid, models):
                out.add(meta.get("name") or mid)
        return sorted(out)

    # ---------------- 静态页面 ----------------

    def _auth_admin_page(self) -> bool:
        """管理员页面：没有管理员会话就跳到统一登录页。"""
        member = self._session()
        if member and member.get("is_admin"):
            return True
        if self._local_rescue():
            return True
        return self._redirect("/login")

    def _redirect(self, location: str, cookie: str | None = None) -> bool:
        self.send_response(302)
        self.send_header("Location", _env("BASE_PATH") + location)
        if cookie:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header(
                "Set-Cookie",
                f"{cookie}; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    # 静态资源：只发 web/assets 下的已知扩展名，路径里不允许出现 ".."
    STATIC_TYPES = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".woff2": "font/woff2",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }

    def _serve_static(self, path: str):
        rel = path[len("/assets/"):]
        if not rel or ".." in rel or rel.startswith("/"):
            return self._error(404, "not_found", "资源不存在")
        full = os.path.join(BASE_DIR, "web", "assets", rel)
        ext = os.path.splitext(full)[1].lower()
        if ext not in self.STATIC_TYPES or not os.path.isfile(full):
            return self._error(404, "not_found", "资源不存在")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", self.STATIC_TYPES[ext])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)

    def _serve_html(self, filename: str):
        path = os.path.join(BASE_DIR, "web", filename)
        if not os.path.exists(path):
            return self._error(404, "not_found", "页面未找到")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 页面会随版本改动，浏览器启发式缓存会让人看到旧界面
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not getattr(self, "_head_only", False):
            self.wfile.write(body)


def main():
    host = _env("HOST", "127.0.0.1")
    port = int(_env("PORT", "8791"))
    get_store()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"oh-pmt-api v{APP_VERSION} 监听 http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
