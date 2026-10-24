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
import qoder
from store import Store, day_start

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("Q2A_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "qoder2api.db")

STORE: Store | None = None
TRUST_FORWARDED = os.environ.get("Q2A_TRUST_FORWARDED", "0") == "1"
SESSION_COOKIE = "q2a_session"
# 会话签名密钥：优先用环境变量，否则从数据库文件路径派生（重启后会话仍有效）
_SESSION_KEY = os.environ.get("Q2A_SESSION_KEY", "").encode() or b"qoder2api-local-session-key"


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
        STORE = Store(DB_PATH)
    return STORE


# ---------------------------------------------------------------------------
# 凭证与积分维护
# ---------------------------------------------------------------------------

def ensure_fresh_token(account: dict) -> dict:
    """必要时刷新 job token。PAT 换来的令牌只有 24 小时。"""
    if account["expires_at"] and account["expires_at"] - time.time() > 300:
        return account

    with _REFRESH_LOCK:
        fresh = get_store().get_account(account["id"])
        if fresh and fresh["expires_at"] and fresh["expires_at"] - time.time() > 300:
            return fresh
        acc = fresh or account

        new = None
        if acc["refresh_token"]:
            try:
                new = qoder.refresh_token(acc["refresh_token"], acc["region"])
            except qoder.UpstreamError:
                new = None
        if not new or not new.get("access_token"):
            ex = qoder.exchange_pat(acc["pat"], acc["region"])
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
    """从上游拉取积分快照并落库。"""
    try:
        acc = ensure_fresh_token(account)
        q = qoder.fetch_quota(acc)
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
    acc = ensure_fresh_token(account)
    before = None
    if account["credits_synced_at"]:
        before = float(account["credits_remaining"] or 0)

    res = qoder.checkin_account(acc)

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


def models_for_region(region: str, account: dict | None = None) -> list[dict]:
    with _MODEL_LOCK:
        hit = _MODEL_CACHE.get(region)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    if account is None:
        account = get_store().pick_account(region)
    if account is None:
        return []
    try:
        acc = ensure_fresh_token(account)
        items = qoder.list_models(acc)
    except Exception:  # noqa: BLE001
        return []
    with _MODEL_LOCK:
        _MODEL_CACHE[region] = (time.time(), items)
    return items


def all_models() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for region in ("global", "cn"):
        for m in models_for_region(region):
            out.setdefault(m["id"], {**m, "region": region})
    return out


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "qoder2api"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("Q2A_ACCESS_LOG") == "1":
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
                return self._send_json(200, {"accounts": self._accounts_view()})
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
        region = "cn" if model.endswith("-cn") else "global"
        prefer = member.get("preferred_account_id") if member else None
        account = store.pick_account(region, prefer) or store.pick_account(
            "cn" if region == "global" else "global", prefer
        )
        if account is None:
            return fail(503, "no_account", f"{region} 区域没有可用账号")
        ctx["account_id"] = account["id"]

        try:
            account = ensure_fresh_token(account)
            parameters = {"max_tokens": body.get("max_tokens")
                          or body.get("max_completion_tokens") or 8192}
            # 思考档位：客户端传什么由这里的 normalize_effort 对齐到该模型
            # 官方支持的集合，不支持的档位上游会静默忽略、回落默认档
            effort = qoder.normalize_effort(qoder.requested_effort(body), catalog.get(model))
            if effort:
                parameters["reasoning_effort"] = effort
            payload = qoder.build_chat_body(
                messages, model, stream=True,
                tools=body.get("tools"),
                parameters=parameters,
                meta=catalog.get(model),
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
                lines = qoder.chat_stream(account, payload, model)
                first = next(iter(lines), None)
                wait = qoder.queue_info(first or "")
                if not wait:
                    break
                # 排队是账号级的（上游对该账号限流），换一个账号往往立刻就能用。
                # 先把同区域的其他账号试一遍，都不行再回到当前账号干等。
                spare = store.pick_account(region, exclude=tried)
                if spare is not None:
                    tried.add(spare["id"])
                    account = ensure_fresh_token(spare)
                    ctx["account_id"] = account["id"]
                    continue
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

    # ---------------- 管理接口实现 ----------------

    def _add_account(self):
        body = self._read_json()
        pat = (body.get("pat") or "").strip()
        region = (body.get("region") or "global").strip()
        if region not in qoder.REGIONS:
            return self._error(400, "bad_region", "区域只能是 global 或 cn")
        if not pat.startswith("pt-"):
            return self._error(400, "bad_pat", "PAT 应以 pt- 开头")

        ex = qoder.exchange_pat(pat, region)
        info = qoder.fetch_userinfo(ex["access_token"], region)
        if not info["uid"]:
            return self._error(400, "uid_missing", "无法获取账号 UID")

        account_id = get_store().add_account(
            uid=info["uid"], region=region, pat=pat,
            email=info["email"], name=info["name"],
        )
        expires_at = time.time() + int(ex.get("expires_in") or 86400)
        get_store().set_account_tokens(
            account_id, ex["access_token"], ex["refresh_token"], expires_at
        )
        acc = get_store().get_account(account_id)
        if acc:
            sync_account_credits(acc)
        return self._send_json(200, {"ok": True, "account": {
            "uid": info["uid"], "email": info["email"],
            "name": info["name"], "region": region,
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
        return self._send_json(200, {"ok": True})

    def _sync_credits(self):
        body = self._read_json()
        targets = ([get_store().get_account(int(body["id"]))] if body.get("id")
                   else get_store().list_accounts())
        out = []
        for acc in targets:
            if not acc:
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
        else:
            targets = [a for a in store.list_accounts() if a["enabled"]]

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
            out.append({
                "id": a["id"], "uid": a["uid"], "region": a["region"],
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
        for aid, st in store.account_summary().items():
            by_account.append({
                "id": aid,
                "name": accounts.get(aid, f"账号 {aid}"),
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
        return {
            "daily": store.daily_series(14),
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
        today = store.usage_totals(day_start())
        all_time = store.usage_totals(0)
        return {
            "accounts": len(accounts),
            "available": sum(1 for a in accounts
                             if a["enabled"] and a["cooldown_until"] < time.time()),
            "members": len(members),
            "keys": len(store.list_keys()),
            "models": [{"id": mid, "name": models[mid].get("name") or mid}
                       for mid in sorted(models)],
            "model_ids": sorted(models),
            "credits_remaining": round(
                sum(a["credits_remaining"] for a in accounts), 2),
            "credits_total": round(sum(a["credits_total"] for a in accounts), 2),
            "credits_used": round(sum(a["credits_used"] for a in accounts), 2),
            # 请求量 / Token / 积分 · 成功率
            "today": today,
            "all_time": all_time,
            "checkin_pending": sum(1 for a in accounts if not a["checkin_at"]),
        }

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
        self.send_header("Location", os.environ.get("Q2A_BASE_PATH", "") + location)
        if cookie:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header(
                "Set-Cookie",
                f"{cookie}; Path=/; Max-Age=43200; HttpOnly; SameSite=Lax{secure}")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _serve_html(self, filename: str):
        path = os.path.join(BASE_DIR, "web", filename)
        if not os.path.exists(path):
            return self._error(404, "not_found", "页面未找到")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    host = os.environ.get("Q2A_HOST", "127.0.0.1")
    port = int(os.environ.get("Q2A_PORT", "8791"))
    get_store()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"qoder2api 监听 http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
