"""HTTP 服务：OpenAI 兼容接口 + 管理 API + 成员控制台。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import convert
from qoder import resolve_model
import guard
import providers
import qoder
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
# 会话签名密钥：优先用环境变量，否则用固定串（重启后会话仍有效）
_SESSION_KEY = _env("SESSION_KEY").encode() or b"oh-pmt-api-local-session-key"


def _mask(secret: str, keep: int = 6) -> str:
    """凭证展示用的掩码：只留头尾，中间固定长度，不泄露真实长度差。"""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return secret[:2] + "…"
    return f"{secret[:keep]}…{secret[-4:]}"


def _sign(value: str) -> str:
    sig = hmac.new(_SESSION_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{sig}"


def _unsign(signed: str) -> str | None:
    if not signed or "." not in signed:
        return None
    value, _, sig = signed.rpartition(".")
    want = hmac.new(_SESSION_KEY, value.encode(), hashlib.sha256).hexdigest()[:32]
    if not secrets.compare_digest(sig, want):
        return None
    return value


# 排队时网关自己等，不把 429 甩给客户端（客户端重试几次就放弃了）
QUEUE_MAX_WAIT = 180      # 最多等这么多秒
QUEUE_MAX_ROUNDS = 6      # 最多重试这么多轮

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
    if account is None:
        return []
    try:
        acc = ensure_fresh_token(account)
        items = prov.list_models(acc)
    except Exception:  # noqa: BLE001
        return []
    for m in items:
        m.setdefault("provider", prov.slug)
    with _MODEL_LOCK:
        _MODEL_CACHE[key] = (time.time(), items)
    return items


def all_models() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for prov in providers.PROVIDERS.values():
        if not prov.available:
            continue
        for region in prov.regions:
            for m in models_for_region(region, provider=prov.slug):
                out.setdefault(m["id"], {**m, "region": region,
                                         "provider": prov.slug})
    return out


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
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, retry_after: int = 0):
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
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
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
        # 兼容本机运维：无任何管理员账号时本机直连放行
        if not get_store().get_admin() and self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        self._error(401, "admin_denied", "请先用管理员账号登录")
        return False

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
        """用 API Key 鉴权（SDK 调用和控制台链接都走这里）。"""
        header = self.headers.get("Authorization", "")
        raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not raw:
            raw = (self._query().get("key") or [""])[0]
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
            if path == "/api/members":
                if not self._auth_admin():
                    return
                return self._send_json(200, {"members": self._members_view()})
            if path == "/api/analytics":
                if not self._auth_admin():
                    return
                return self._send_json(200, self._analytics())
            if path == "/api/usage":
                if not self._auth_admin():
                    return
                return self._send_json(200, {"recent": get_store().recent_usage(100)})

            # 成员控制台数据
            if path == "/api/me":
                ctx = self._auth_member()
                if ctx is None:
                    return
                return self._send_json(200, self._member_dashboard(ctx))
            if path == "/api/me/usage":
                ctx = self._auth_member()
                if ctx is None:
                    return
                return self._send_json(200, {
                    "recent": get_store().usage_for_member(ctx["member"]["id"], 100)
                })

            # 根路径：已登录的管理员进后台，已登录的成员进成员页，否则去登录
            if path == "/":
                member = self._session()
                if member and member.get("is_admin"):
                    return self._redirect("/admin")
                if member:
                    return self._redirect("/me")
                if not get_store().get_admin() and self.client_address[0] in ("127.0.0.1", "::1"):
                    return self._serve_html("admin.html")
                return self._redirect("/login")
            if path == "/admin":
                if not self._auth_admin_page():
                    return
                return self._serve_html("admin.html")
            if path.startswith("/accounts/"):
                if not self._auth_admin_page():
                    return
                return self._serve_html("account.html")
            if path.startswith("/assets/"):
                return self._serve_static(path)
            if path in ("/console", "/me"):
                return self._serve_html("console.html")
            if path == "/login":
                member = self._session()
                if member:
                    return self._redirect("/admin" if member.get("is_admin") else "/me")
                return self._serve_html("login.html")
            return self._error(404, "not_found", "接口不存在")
        except Exception as e:  # noqa: BLE001
            return self._error(500, "internal_error", str(e))

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
            if path == "/api/members":
                if not self._auth_admin():
                    return
                return self._create_member()
            if path == "/api/keys":
                if not self._auth_admin():
                    return
                return self._create_key()
            return self._error(404, "not_found", "接口不存在")
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)
        except qoder.UpstreamError as e:
            return self._error(400, "upstream_error", str(e))
        except providers.Unsupported as e:
            return self._error(400, "provider_unsupported", str(e))
        except Exception as e:  # noqa: BLE001
            return self._error(500, "internal_error", str(e))

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not self._auth_admin():
            return
        body = self._read_json()
        try:
            if path.startswith("/api/members/"):
                mid = int(path.rsplit("/", 1)[-1])
                fields = {k: body[k] for k in
                          ("name", "email", "enabled", "limit_daily", "limit_weekly",
                           "preferred_account_id", "model_allowlist", "note")
                          if k in body}
                store = get_store()
                username = (body.get("username") or "").strip()
                if username:
                    other = store.get_member_by_username(username)
                    if other and other["id"] != mid:
                        return self._error(409, "username_taken", "该账号已存在")
                    fields["username"] = username
                password = body.get("password") or ""
                if fields:
                    store.update_member(mid, **fields)
                if password:
                    store.set_password(mid, password)
                return self._send_json(200, {"ok": True})
            if path.startswith("/api/keys/"):
                kid = int(path.rsplit("/", 1)[-1])
                fields = {k: body[k] for k in
                          ("enabled", "ip_allowlist", "model_allowlist", "note")
                          if k in body}
                get_store().update_key(kid, **fields)
                return self._send_json(200, {"ok": True})
        except ValueError:
            return self._error(400, "bad_request", "ID 无效")
        return self._error(404, "not_found", "接口不存在")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._auth_admin():
            return
        try:
            if path.startswith("/api/members/"):
                get_store().delete_member(int(path.rsplit("/", 1)[-1]))
                return self._send_json(200, {"ok": True})
            if path.startswith("/api/keys/"):
                get_store().delete_key(int(path.rsplit("/", 1)[-1]))
                return self._send_json(200, {"ok": True})
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
        data = []
        for mid, m in all_models().items():
            if allowed and mid not in allowed:
                continue
            full = m.get("name") or mid
            data.append({
                # 客户端普遍直接展示 id，所以 id 给完整名称；
                # 上游只认缩写 key，请求进来时由 resolve_model 换回去
                "id": full,
                "object": "model",
                "name": full,
                "display_name": full,
                "upstream_key": mid,
                "owned_by": f"qoder-{m.get('region', 'global')}",
            })
        return self._send_json(200, {"object": "list", "data": data})

    # ---------------- 登录 ----------------

    def _do_login(self):
        body = self._read_json()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        store = get_store()
        member = store.get_member_by_username(username)

        # 用户名不存在时也跑一次哈希，避免用响应时间探出账号是否存在
        ok = Store.verify_password(
            password, member["password_hash"] if member else Store.hash_password("x"))
        if not member or not ok:
            return self._error(401, "login_failed", "账号或密码错误")
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
            )
            return self._error(status, code, message, retry_after=retry_after)

        try:
            guard.check_ip(key, ip)
            guard.check_quota(store, member)
        except guard.Denied as e:
            return fail(e.status, e.code, e.message)

        body = self._read_json()
        requested = (body.get("model") or "").strip()
        messages = body.get("messages") or []
        if not requested or not messages:
            return fail(400, "bad_request", "缺少 model 或 messages")

        # 客户端可以用完整名称或缩写，这里统一换成上游 key
        catalog = all_models()
        model = resolve_model(requested, catalog) or requested
        ctx["model"] = model

        try:
            guard.check_model(key, member, model, catalog)
        except guard.Denied as e:
            return fail(e.status, e.code, e.message)

        stream = bool(body.get("stream"))
        # 模型目录带上了厂商，路由直接用模型所属厂商；目录里没有这个模型时
        # 按 Qoder 的命名习惯猜区域，交给 Qoder 适配器处理。
        meta = catalog.get(model) or {}
        provider = providers.get(meta.get("provider"))
        want = meta.get("region") or ("cn" if model.endswith("-cn") else "global")
        prefer = member.get("preferred_account_id") if member else None
        account = store.pick_account(want, provider=provider.slug, prefer_id=prefer)
        if account is None:
            for alt in provider.regions:
                if alt == want:
                    continue
                account = store.pick_account(alt, provider=provider.slug,
                                             prefer_id=prefer)
                if account is not None:
                    break
        if account is None:
            return fail(503, "no_account",
                        f"{provider.label} 的 {provider.region_label(want)} 区域"
                        f"没有可用账号")
        ctx["account_id"] = account["id"]

        try:
            account = ensure_fresh_token(account)
            parameters = {"max_tokens": body.get("max_tokens")
                          or body.get("max_completion_tokens") or 8192}
            # 思考档位：客户端传什么由这里的 normalize_effort 对齐到该模型
            # 官方支持的集合，不支持的档位上游会静默忽略、回落默认档
            effort = qoder.normalize_effort(qoder.requested_effort(body), meta)
            if effort:
                parameters["reasoning_effort"] = effort
            payload = provider.build_chat_body(
                messages, model, stream=True,
                tools=body.get("tools"),
                parameters=parameters,
                meta=meta,
            )
            # 上游排队时返回 HTTP 200，但首帧就是 10605 错误信封。
            # 官方客户端会显示"排队中"并继续等；普通客户端只会重试几次就放弃，
            # 所以把等待收进网关。流式请求先开流再等，期间用注释帧保活并
            # 通知排队进度，客户端不至于因为长时间无数据而超时。
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self._stream_started = True

            deadline = time.time() + QUEUE_MAX_WAIT
            round_no = 0
            tried = {account["id"]}
            while True:
                lines = provider.chat_stream(account, payload, model)
                first = next(iter(lines), None)
                frame = first or ""
                wait = qoder.queue_info(frame)
                # 排队和额度不足都是账号级的：上游对这个账号限流，或者这个账号
                # 买不起这个模型的套餐。同一个密钥下的其他账号往往立刻能用，
                # 先把同区域还没试过的账号试一遍。与排队一样在首帧就发现，
                # 那时流还没开始写，换账号对客户端是透明的。
                if wait or qoder.quota_exhausted(frame):
                    spare = store.pick_account(want, provider=provider.slug,
                                               exclude=tried)
                    if spare is not None:
                        tried.add(spare["id"])
                        account = ensure_fresh_token(spare)
                        ctx["account_id"] = account["id"]
                        continue
                if not wait:
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
                    )
                    if stream:
                        self._stream_queue_giveup(round_no - 1, wait)
                        return
                    return self._error(429, code, msg, retry_after=wait)
                if stream:
                    self._stream_queue_notice(round_no, wait, left)
                time.sleep(min(wait, max(1, left)))

            # 首帧已经被取出来判断过是否排队，要放回流的开头。
            # 这里必须用默认参数固定住上游迭代器：写成闭包直接引用 lines
            # 会被下面那句 lines = _replay() 重新绑定成 _replay 自身，
            # 于是 yield from 迭代到自己身上，抛 ValueError: generator already executing
            def _replay(upstream=lines, head=first):
                if head is not None:
                    yield head
                yield from upstream

            lines = _replay()
        except qoder.UpstreamError as e:
            if e.is_queued:
                code, msg, wait = "upstream_busy", "上游模型繁忙，请稍后重试", e.retry_after()
            elif e.is_rate_limited:
                code, msg, wait = "upstream_rate_limited", "上游限流，请稍后重试", e.retry_after()
            else:
                store.set_account_cooldown(account["id"], 60, str(e))
                code, msg, wait = "upstream_error", str(e), 0
            store.record_usage(
                key_id=key["id"], member_id=key["member_id"],
                account_id=account["id"], model=model,
                prompt_tokens=0, completion_tokens=0,
                status=429 if code != "upstream_error" else 502,
                error=code, ip=ip,
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
            return self._error(429 if code != "upstream_error" else 502,
                               code, msg, retry_after=wait)

        store.touch_account(account["id"])
        holder: dict = {}
        failure = {"code": "", "status": 200}

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
                except convert.UpstreamStatusError as e:
                    failure = {"code": "upstream_status", "status": 502}
                    err = {"error": {"code": "upstream_status",
                                     "message": str(e.body)[:300]}}
                    self.wfile.write(convert.to_sse(json.dumps(err)))
                    self.wfile.write(convert.DONE)
                    self.wfile.flush()
                self.close_connection = True
            else:
                self._send_json(200, convert.aggregate(lines, model, holder))
        finally:
            usage = holder.get("usage") or {}
            store.record_usage(
                key_id=key["id"],
                member_id=key["member_id"],
                account_id=account["id"],
                model=model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                credits=float(holder.get("credits") or 0),
                credits_original=float(holder.get("credits_original") or 0),
                status=failure["status"],
                error=failure["code"],
                ip=ip,
            )

    def _account_detail(self, account_id: int) -> dict:
        """单个账号的完整信息，账号详情页用。"""
        store = get_store()
        acc = store.get_account(account_id)
        if not acc:
            return {}
        prov = providers.get(acc.get("provider"))
        view = next((a for a in Handler._accounts_view(self) if a["id"] == account_id), {})
        models = []
        for mid, m in all_models().items():
            if m.get("provider") != prov.slug:
                continue
            models.append({"id": mid, "name": m.get("name") or mid})
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

    def _add_account(self):
        body = self._read_json()
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
        targets = ([store.get_account(int(body["id"]))] if body.get("id")
                   else store.list_accounts())
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
            targets = [a for a in store.list_accounts()
                       if a["enabled"]
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

    def _create_member(self):
        body = self._read_json()
        name = (body.get("name") or "").strip()
        if not name:
            return self._error(400, "bad_request", "成员名字不能为空")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if username and get_store().get_member_by_username(username):
            return self._error(409, "username_taken", "该账号已存在")
        mid = get_store().create_member(
            name=name,
            email=(body.get("email") or "").strip(),
            limit_daily=float(body.get("limit_daily") or 0),
            limit_weekly=float(body.get("limit_weekly") or 0),
            preferred_account_id=body.get("preferred_account_id") or None,
            model_allowlist=(body.get("model_allowlist")
                             if body.get("model_allowlist") is not None
                             else "qfmodel,gfmodel,dfmodel"),
            note=(body.get("note") or "").strip(),
            username=username or None,
            password=password or None,
        )
        return self._send_json(200, {"ok": True, "id": mid})

    def _create_key(self):
        body = self._read_json()
        member_id = body.get("member_id")
        if not member_id:
            return self._error(400, "bad_request", "必须指定成员")
        key_id, raw = get_store().create_key(
            member_id=int(member_id),
            ip_allowlist=(body.get("ip_allowlist") or "").strip(),
            model_allowlist=(body.get("model_allowlist") or "").strip(),
            note=(body.get("note") or "").strip(),
        )
        return self._send_json(200, {"ok": True, "id": key_id, "key": raw})

    # ---------------- 视图 ----------------

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
                # 网关记账：请求量 / Token / 积分，按模型拆分
                "gateway_consumed": st.get("credits", 0.0),
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
        out = []
        for m in store.list_members():
            st = guard.quota_status(store, m)
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
                "limit_daily": st["limit_daily"],
                "limit_weekly": st["limit_weekly"],
                "used_today": st["used_today"],
                "used_week": st["used_week"],
                "left_today": st["left_today"],
                "left_week": st["left_week"],
                "used_total": round(store.credits_total(m["id"]), 4),
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
                    "credits": round(float(r["credits"] or 0), 4),
                } for r in by_model],
                "model_allowlist": m["model_allowlist"],
                "note": m["note"],
                "preferred_account_id": m["preferred_account_id"],
                "preferred_account": pref["name"] or pref["email"] if pref else "",
                "keys": [{"id": k["id"], "prefix": k["key_prefix"],
                          "key": k["key_value"] or "",
                          "enabled": bool(k["enabled"]),
                          "ip_allowlist": k["ip_allowlist"],
                          "model_allowlist": k["model_allowlist"],
                          "note": k["note"]}
                         for k in keys],
            })
        return out

    def _analytics(self) -> dict:
        store = get_store()
        catalog = all_models()
        members = {m["id"]: m["name"] for m in store.list_members()}
        accounts = {a["id"]: (a["name"] or a["email"]) for a in store.list_accounts()}

        def model_name(mid: str) -> str:
            return (catalog.get(mid) or {}).get("name") or mid or "未知"

        per_member = []
        for row in store.member_summary():
            mid = row["member_id"]
            n = int(row["n"] or 0)
            ok = int(row["ok"] or 0)
            per_member.append({
                "id": mid,
                "name": members.get(mid, "未关联"),
                "requests": n,
                "errors": int(row["err"] or 0),
                "success_rate": round(ok * 100.0 / n, 2) if n else None,
                "credits": round(float(row["credits"] or 0), 4),
                "tokens": int(row["tokens"] or 0),
            })
        per_member.sort(key=lambda x: x["credits"], reverse=True)

        # 账号归属：每个账号的积分与请求量
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
                "credits": st["credits"],
                "requests": st["requests"],
                "tokens": st["tokens"],
                "success_rate": st["success_rate"],
            })
        by_account.sort(key=lambda x: x["credits"], reverse=True)

        by_model = [{
            "model": model_name(m["model"]),
            "id": m["model"] or "",
            "requests": int(m["n"] or 0),
            "errors": int(m["err"] or 0),
            "success_rate": (round(int(m["ok"] or 0) * 100.0 / int(m["n"]), 2)
                             if int(m["n"] or 0) else None),
            "prompt_tokens": int(m["prompt_tokens"] or 0),
            "completion_tokens": int(m["completion_tokens"] or 0),
            "credits": round(float(m["credits"] or 0), 4),
            "tokens": int(m["tokens"] or 0),
        } for m in store.model_summary()]

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
            cell = agg.setdefault(slug, {"credits": 0.0, "requests": 0,
                                         "ok": 0, "tokens": 0})
            cell["credits"] += float(row["credits"] or 0)
            cell["requests"] += int(row["n"] or 0)
            cell["ok"] += int(row["ok"] or 0)
            cell["tokens"] += int(row["tokens"] or 0)
        for slug, cell in agg.items():
            meta = prov_stats.get(slug) or {}
            provider_rows.append({
                "slug": slug,
                "label": meta.get("label", slug),
                "color": meta.get("color", "#8b6b2f"),
                "credits": round(cell["credits"], 4),
                "requests": cell["requests"],
                "tokens": cell["tokens"],
                "success_rate": (round(cell["ok"] * 100.0 / cell["requests"], 2)
                                 if cell["requests"] else None),
            })
        provider_rows.sort(key=lambda r: r["credits"], reverse=True)

        return {
            "daily": store.daily_series(14),
            "hourly": store.hourly_series(24),
            "model_pivot": store.models_pivot(14),
            "token_hist": store.token_distribution(),
            "heatmap": store.usage_heatmap(14),
            "by_provider": provider_rows,
            "by_model": by_model,
            "by_member": per_member,
            "by_account": by_account,
            "totals": {
                "requests": all_time["requests"],
                "errors": all_time["errors"],
                "success_rate": all_time["success_rate"],
                "tokens": all_time["tokens"],
                "credits": all_time["credits"],
                "requests_7d": sum(d["requests"] for d in store.daily_series(7)),
                "credits_7d": round(sum(m["credits"] for m in per_member), 4),
                "tokens_7d": sum(m["tokens"] for m in per_member),
            },
        }

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
            models = {mid: {"name": mid, "provider": providers.DEFAULT_PROVIDER}
                      for mid in DEFAULT_MODELS.split(",") if mid}
        today = store.usage_totals(day_start())
        all_time = store.usage_totals(0)
        return {
            "accounts": len(accounts),
            "available": sum(1 for a in accounts
                             if a["enabled"] and a["cooldown_until"] < time.time()),
            "members": len(members),
            "keys": len(store.list_keys()),
            "models": [{"id": mid, "name": models[mid].get("name") or mid,
                        "provider": models[mid].get("provider", "qoder")}
                       for mid in sorted(models)],
            "model_ids": sorted(models),
            "models_provisional": provisional,
            "providers": providers.describe_all(),
            "provider_stats": self._provider_stats(accounts),
            "credits_remaining": round(
                sum(a["credits_remaining"] for a in accounts), 2),
            "credits_total": round(sum(a["credits_total"] for a in accounts), 2),
            "credits_used": round(sum(a["credits_used"] for a in accounts), 2),
            # 请求量 / Token / 积分 · 成功率
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

    def _member_dashboard(self, ctx: dict) -> dict:
        store = get_store()
        member = ctx["member"]
        key = ctx.get("key")
        accounts = {a["id"]: a for a in store.list_accounts()}
        allowed = guard.allowed_models(key or {}, member)
        models = all_models()
        return {
            "member": {
                "id": member["id"], "name": member["name"],
                "email": member["email"], "username": member.get("username") or "",
                "model_allowlist": sorted(allowed),
            },
            "key": {
                "prefix": (key or {}).get("key_prefix", ""),
                "value": (key or {}).get("key_value", ""),
                "ip_allowlist": (key or {}).get("ip_allowlist", ""),
            },
            "quota": guard.quota_status(store, member),
            "preferred_account": (
                accounts.get(member["preferred_account_id"]) or {}
            ).get("name", ""),
            "models": [{"id": mid, "name": m.get("name", mid)}
                       for mid, m in sorted(models.items())
                       if not allowed or mid in allowed],
            "daily": store.daily_series(14),
            "recent": store.usage_for_member(member["id"], 20),
        }

    # ---------------- 静态页面 ----------------

    def _auth_admin_page(self) -> bool:
        """管理员页面：没有管理员会话就跳到统一登录页。"""
        member = self._session()
        if member and member.get("is_admin"):
            return True
        if not get_store().get_admin() and self.client_address[0] in ("127.0.0.1", "::1"):
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
