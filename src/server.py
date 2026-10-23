"""HTTP 服务：OpenAI 兼容接口 + 管理 API。"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import convert
import guard
import qoder
from store import Store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("Q2A_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "qoder2api.db")

STORE: Store | None = None
TRUST_FORWARDED = os.environ.get("Q2A_TRUST_FORWARDED", "0") == "1"
ADMIN_TOKEN = os.environ.get("Q2A_ADMIN_TOKEN", "")

# 模型清单缓存：region -> (timestamp, list)
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
# 凭证维护
# ---------------------------------------------------------------------------

def ensure_fresh_token(account: dict) -> dict:
    """必要时刷新 job token。PAT → jobToken 只有 24 小时，不刷新就失效。"""
    if account["expires_at"] and account["expires_at"] - time.time() > 300:
        return account

    with _REFRESH_LOCK:
        fresh = get_store().get_account(account["id"])
        if fresh and fresh["expires_at"] and fresh["expires_at"] - time.time() > 300:
            return fresh
        acc = fresh or account

        # 优先用 refresh_token，失败回退到 PAT 重换
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
    except Exception:
        return []
    with _MODEL_LOCK:
        _MODEL_CACHE[region] = (time.time(), items)
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "qoder2api"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具 ----------------

    def log_message(self, fmt, *args):
        if os.environ.get("Q2A_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    def _send_json(self, status: int, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
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

    # ---------------- 鉴权 ----------------

    def _auth_key(self) -> dict | None:
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
        return record

    def _auth_admin(self) -> bool:
        token = self.headers.get("x-admin-token", "")
        if not ADMIN_TOKEN:
            # 未设管理令牌时只允许本机访问
            if self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            self._error(403, "admin_locked", "未配置管理令牌，仅限本机访问")
            return False
        if token != ADMIN_TOKEN:
            self._error(401, "admin_denied", "管理令牌错误")
            return False
        return True

    # ---------------- 路由 ----------------

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/health":
                return self._send_json(200, {"status": "ok", "time": int(time.time())})
            if path == "/v1/models":
                return self._v1_models()
            if path == "/api/status":
                if not self._auth_admin():
                    return
                return self._send_json(200, self._admin_status())
            if path == "/api/accounts":
                if not self._auth_admin():
                    return
                return self._send_json(200, {"accounts": self._accounts_view()})
            if path == "/api/keys":
                if not self._auth_admin():
                    return
                return self._send_json(200, {"keys": self._keys_view()})
            if path == "/api/usage":
                if not self._auth_admin():
                    return
                return self._send_json(200, {
                    "summary": get_store().usage_summary(),
                    "recent": get_store().recent_usage(100),
                })
            if path in ("/", "/admin"):
                return self._serve_dashboard()
            return self._error(404, "not_found", "接口不存在")
        except Exception as e:  # noqa: BLE001
            return self._error(500, "internal_error", str(e))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/v1/chat/completions":
                return self._chat_completions()
            if path == "/api/accounts":
                if not self._auth_admin():
                    return
                return self._add_account()
            if path == "/api/keys":
                if not self._auth_admin():
                    return
                return self._create_key_route()
            if path == "/api/accounts/action":
                if not self._auth_admin():
                    return
                return self._account_action()
            return self._error(404, "not_found", "接口不存在")
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)
        except Exception as e:  # noqa: BLE001
            return self._error(500, "internal_error", str(e))

    def do_PATCH(self):
        path = urlparse(self.path).path
        if not self._auth_admin():
            return
        if path.startswith("/api/keys/"):
            try:
                key_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return self._error(400, "bad_request", "Key ID 无效")
            body = self._read_json()
            fields = {}
            for k in ("label", "enabled", "token_limit_daily", "token_limit_total",
                      "ip_allowlist", "model_allowlist"):
                if k in body:
                    fields[k] = body[k]
            get_store().update_key(key_id, **fields)
            return self._send_json(200, {"ok": True})
        return self._error(404, "not_found", "接口不存在")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._auth_admin():
            return
        try:
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
        if not self._auth_key():
            return
        seen: dict[str, dict] = {}
        for region in ("global", "cn"):
            for m in models_for_region(region):
                seen.setdefault(m["id"], {"id": m["id"], "object": "model",
                                          "owned_by": f"qoder-{region}"})
        return self._send_json(200, {"object": "list", "data": list(seen.values())})

    def _chat_completions(self):
        key = self._auth_key()
        if key is None:
            return
        ip = self._client_ip()

        try:
            guard.check_ip(key, ip)
            guard.check_quota(get_store(), key)
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)

        body = self._read_json()
        model = (body.get("model") or "").strip()
        messages = body.get("messages") or []
        if not model or not messages:
            return self._error(400, "bad_request", "缺少 model 或 messages")

        try:
            guard.check_model(key, model)
        except guard.Denied as e:
            return self._error(e.status, e.code, e.message)

        stream = bool(body.get("stream"))
        region = "cn" if model.endswith("-cn") else "global"

        account = get_store().pick_account(region) or get_store().pick_account(
            "cn" if region == "global" else "global"
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
            get_store().set_account_cooldown(account["id"], 60, str(e))
            return self._error(502, "upstream_error", str(e))
        except Exception as e:  # noqa: BLE001
            get_store().set_account_cooldown(account["id"], 60, str(e))
            return self._error(500, "proxy_error", str(e))

        get_store().touch_account(account["id"])
        usage_holder: dict = {}
        started = time.time()

        try:
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for chunk_json in convert.iter_chunks(lines, usage_holder):
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
                result = convert.aggregate(lines, model, usage_holder)
                self._send_json(200, result)
        finally:
            usage = usage_holder.get("usage") or {}
            get_store().record_usage(
                key_id=key["id"], account_id=account["id"], model=model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                status=200, ip=ip,
            )
            _ = started

    # ---------------- 管理接口 ----------------

    def _add_account(self):
        body = self._read_json()
        pat = (body.get("pat") or "").strip()
        region = (body.get("region") or "global").strip()
        if region not in qoder.REGIONS:
            return self._error(400, "bad_region", "区域只能是 global 或 cn")
        if not pat.startswith("pt-"):
            return self._error(400, "bad_pat", "PAT 应以 pt- 开头")
        try:
            ex = qoder.exchange_pat(pat, region)
            info = qoder.fetch_userinfo(ex["access_token"], region)
        except qoder.UpstreamError as e:
            return self._error(400, "pat_invalid", str(e))

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

    def _create_key_route(self):
        body = self._read_json()
        key_id, raw = get_store().create_key(
            label=(body.get("label") or "").strip(),
            token_limit_daily=int(body.get("token_limit_daily") or 0),
            token_limit_total=int(body.get("token_limit_total") or 0),
            ip_allowlist=(body.get("ip_allowlist") or "").strip(),
            model_allowlist=(body.get("model_allowlist") or "").strip(),
        )
        return self._send_json(200, {"ok": True, "id": key_id, "key": raw})

    def _accounts_view(self) -> list[dict]:
        out = []
        for a in get_store().list_accounts():
            out.append({
                "id": a["id"], "uid": a["uid"], "region": a["region"],
                "email": a["email"], "name": a["name"],
                "enabled": bool(a["enabled"]),
                "cooldown": max(0, int(a["cooldown_until"] - time.time())),
                "last_error": a["last_error"],
                "expires_in": max(0, int(a["expires_at"] - time.time())),
            })
        return out

    def _keys_view(self) -> list[dict]:
        store = get_store()
        out = []
        for k in store.list_keys():
            out.append({
                "id": k["id"], "prefix": k["key_prefix"], "label": k["label"],
                "enabled": bool(k["enabled"]),
                "token_limit_daily": k["token_limit_daily"],
                "token_limit_total": k["token_limit_total"],
                "ip_allowlist": k["ip_allowlist"],
                "model_allowlist": k["model_allowlist"],
                "used_today": store.tokens_used_since(k["id"], time.time() - 86400),
                "used_total": store.tokens_used_total(k["id"]),
            })
        return out

    def _admin_status(self) -> dict:
        accounts = get_store().list_accounts()
        models = []
        if accounts:  # 没有账号时不必去上游拉清单
            for region in ("global", "cn"):
                models.extend(models_for_region(region))
        return {"accounts": len(accounts),
                "available": sum(1 for a in accounts
                                 if a["enabled"] and a["cooldown_until"] < time.time()),
                "keys": len(get_store().list_keys()),
                "models": sorted({m["id"] for m in models})}

    def _serve_dashboard(self):
        path = os.path.join(BASE_DIR, "web", "index.html")
        if not os.path.exists(path):
            return self._error(404, "not_found", "管理界面未找到")
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
