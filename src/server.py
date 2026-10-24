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
from store import Store

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
    )
    return q


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

    def _send_json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str):
        self._send_json(status, {"error": {"code": code, "message": message}})

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
        auth = self._auth_key()
        if auth is None:
            return
        key, member = auth
        ip = self._client_ip()
        store = get_store()

        try:
            guard.check_ip(key, ip)
            guard.check_quota(store, member)
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)

        body = self._read_json()
        requested = (body.get("model") or "").strip()
        messages = body.get("messages") or []
        if not requested or not messages:
            return self._error(400, "bad_request", "缺少 model 或 messages")

        # 客户端可以用完整名称或缩写，这里统一换成上游 key
        catalog = all_models()
        model = resolve_model(requested, catalog) or requested

        try:
            guard.check_model(key, member, model, catalog)
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)

        stream = bool(body.get("stream"))
        region = "cn" if model.endswith("-cn") else "global"
        prefer = member.get("preferred_account_id") if member else None
        account = store.pick_account(region, prefer) or store.pick_account(
            "cn" if region == "global" else "global", prefer
        )
        if account is None:
            return self._error(503, "no_account", f"{region} 区域没有可用账号")

        try:
            account = ensure_fresh_token(account)
            payload = qoder.build_chat_body(
                messages, model, stream=True,
                tools=body.get("tools"),
                parameters={"max_tokens": body.get("max_tokens") or 8192},
            )
            lines = qoder.chat_stream(account, payload, model)
        except qoder.UpstreamError as e:
            store.set_account_cooldown(account["id"], 60, str(e))
            return self._error(502, "upstream_error", str(e))

        store.touch_account(account["id"])
        holder: dict = {}

        try:
            if stream:
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
                status=200,
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
        consumed = store.credits_by_account()
        all_members = store.list_members()
        # 账号 -> 绑定到它的成员名字
        bound_map: dict[int, list[str]] = {}
        for m in all_members:
            pid = m["preferred_account_id"]
            if pid:
                bound_map.setdefault(pid, []).append(m["name"])
        out = []
        for a in store.list_accounts():
            out.append({
                "id": a["id"], "uid": a["uid"], "region": a["region"],
                "email": a["email"], "name": a["name"],
                "enabled": bool(a["enabled"]),
                "cooldown": max(0, int(a["cooldown_until"] - time.time())),
                "last_error": a["last_error"],
                "expires_in": max(0, int(a["expires_at"] - time.time())),
                "credits_total": a["credits_total"],
                "credits_used": a["credits_used"],
                "credits_remaining": a["credits_remaining"],
                "credits_addon": a["credits_addon"],
                "plan_name": a["plan_name"],
                "synced_ago": (max(0, int(time.time() - a["credits_synced_at"]))
                               if a["credits_synced_at"] else None),
                "gateway_consumed": round(consumed.get(a["id"], 0.0), 4),
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

        per_member = []
        for row in store.member_summary():
            mid = row["member_id"]
            per_member.append({
                "name": members.get(mid, "未关联"),
                "requests": int(row["n"]),
                "credits": round(float(row["credits"] or 0), 4),
                "tokens": int(row["tokens"] or 0),
            })
        per_member.sort(key=lambda x: x["credits"], reverse=True)

        # 账号归属：每个账号被消耗多少积分
        consumed = store.credits_by_account()
        by_account = [{"name": accounts.get(aid, f"账号 {aid}"),
                       "credits": round(v, 4)} for aid, v in consumed.items()]

        return {
            "daily": store.daily_series(14),
            "by_model": [{"model": (catalog.get(m["model"]) or {}).get("name")
                                   or m["model"] or "未知",
                          "id": m["model"] or "",
                          "requests": int(m["n"]),
                          "credits": round(float(m["credits"] or 0), 4),
                          "tokens": int(m["tokens"] or 0)}
                         for m in store.model_summary()],
            "by_member": per_member,
            "by_account": by_account,
            "totals": {
                "requests": sum(d["requests"] for d in store.daily_series(30)),
                "credits_7d": round(sum(m["credits"] for m in per_member), 4),
            },
        }

    def _admin_status(self) -> dict:
        store = get_store()
        accounts = store.list_accounts()
        members = store.list_members()
        models = all_models() if accounts else {}
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
