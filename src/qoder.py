"""与 Qoder 上游通信：PAT 交换、token 刷新、模型列表、对话推理。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from cosy import (
    COSY_CLIENT_TYPE,
    COSY_VERSION,
    build_cosy_headers,
    derive_hostname,
    derive_machine_id,
    derive_machine_token,
    detect_machine_os,
    qoder_encode,
)

REGIONS = {
    "global": {
        "label": "Qoder 国际版",
        "base": "https://api3.qoder.sh/",
        "openapi": "https://openapi.qoder.sh",
        "pat_url": "https://qoder.com/account/integrations",
    },
    "cn": {
        "label": "Qoder 国内版",
        "base": "https://gateway.qoder.com.cn/",
        "openapi": "https://openapi.qoder.com.cn",
        "pat_url": "https://qoder.com.cn/account/integrations",
    },
}


class UpstreamError(Exception):
    """上游返回错误，带 HTTP 状态码与原始响应体。"""

    def __init__(self, status: int, body: str, message: str = ""):
        self.status = status
        self.body = body
        super().__init__(message or f"upstream {status}: {body[:200]}")

    @property
    def is_auth_dead(self) -> bool:
        """凭证失效，需要重新用 PAT 换 token。"""
        return self.status in (401, 403) or any(
            k in self.body
            for k in ("TOKEN_EXPIRE", "12153", "Offline user session not found")
        )

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429


def _request(url: str, *, method="GET", body: bytes | None = None,
             headers: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise UpstreamError(0, str(e), f"network error: {e.reason}") from e


def _expires_seconds(data: dict, default: int = 86400) -> int:
    """Qoder 返回的 expires_in 单位是毫秒，归一化成秒。"""
    raw = data.get("expires_in")
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    # 大于一天的毫秒值除以 1000；已经是秒的（<=86400）原样返回
    return value // 1000 if value > 86400 else value


def exchange_pat(pat: str, region: str) -> dict:
    """用 PAT 换 job token。不需要 COSY 签名。"""
    url = f"{REGIONS[region]['openapi']}/api/v1/jobToken/exchange"
    body = json.dumps({"personal_token": pat}).encode()
    status, raw = _request(url, method="POST", body=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "undici",
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": COSY_CLIENT_TYPE,
    })
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "PAT 校验失败，请确认令牌有效且区域选择正确")
    data = json.loads(text)
    if not data.get("token"):
        raise UpstreamError(status, text, "PAT 交换未返回 job token")
    return {
        "access_token": data["token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": data.get("expires_at") or "",
        "expires_in": _expires_seconds(data),
    }


def fetch_userinfo(access_token: str, region: str) -> dict:
    """取账号身份信息，用于展示和指纹派生。"""
    url = f"{REGIONS[region]['openapi']}/api/v1/userinfo"
    status, raw = _request(url, headers={
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "undici",
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": COSY_CLIENT_TYPE,
    })
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "获取账号信息失败")
    info = json.loads(text)
    return {
        "uid": str(info.get("id") or ""),
        "email": info.get("email") or "",
        "name": info.get("name") or info.get("username") or "",
    }


def refresh_token(refresh_token: str, region: str) -> dict:
    """用 jrt- 刷新 job token。失败时调用方应回退到 PAT 重换。"""
    url = f"{REGIONS[region]['openapi']}/api/v1/jobToken/refresh"
    body = json.dumps({"refresh_token": refresh_token}).encode()
    status, raw = _request(url, method="POST", body=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "undici",
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": COSY_CLIENT_TYPE,
    })
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "刷新令牌失败")
    data = json.loads(text)
    return {
        "access_token": data.get("token", ""),
        "refresh_token": data.get("refresh_token", refresh_token),
        "expires_in": _expires_seconds(data),
    }


def list_models(account: dict) -> list[dict]:
    """拉取该账号可见的模型清单。"""
    region = account["region"]
    url = f"{REGIONS[region]['base']}algo/api/v2/model/list?Encode=1"
    headers = build_cosy_headers(
        uid=account["uid"],
        access_token=account["access_token"],
        nickname=account.get("name", ""),
        email=account.get("email", ""),
        machine_id=derive_machine_id(account["uid"]),
        machine_token=derive_machine_token(account["uid"]),
        machine_hostname=derive_hostname(account["uid"]),
        machine_os=detect_machine_os(),
        body=b"",
        url=url,
    )
    headers["accept"] = "application/json"
    status, raw = _request(url, headers=headers)
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "拉取模型列表失败")
    data = json.loads(text)
    # 实际结构：{"chat": [{key, display_name, enable, is_reasoning, ...}, ...]}
    if isinstance(data, dict):
        items = data.get("chat") or data.get("data") or data.get("models") or []
    else:
        items = data
    if isinstance(items, dict):
        items = items.get("list") or items.get("models") or []
    out = []
    for m in items if isinstance(items, list) else []:
        if isinstance(m, str):
            out.append({"id": m, "name": m})
        elif isinstance(m, dict):
            mid = m.get("key") or m.get("id") or m.get("model") or m.get("name")
            if mid:
                out.append({
                    "id": mid,
                    "name": display_name(mid, m.get("display_name") or m.get("name") or mid),
                    "source": m.get("source") or "system",
                    "enable": m.get("enable", True),
                })
    return out


# 上游命名不统一（Qwen3.8-Flash 无连字符、GLM-5.3-Flash 有），
# 对外统一成 "<家族>-<版本>-<档位>" 的写法，只影响展示。
DISPLAY_NAMES = {
    "qmodel_38max": "Qwen-3.8-Max",
    "qfmodel": "Qwen-3.8-Flash",
    "kmodel_latest": "Kimi-K3",
    "kmodel": "Kimi-K2.8-Preview",
}


def display_name(upstream_key: str, raw: str) -> str:
    """把上游的 display_name 规范成统一风格。"""
    return DISPLAY_NAMES.get(upstream_key) or raw


def resolve_model(name: str, catalog: dict) -> str | None:
    """把客户端传的模型名解析成上游 key。

    上游只认缩写（gfmodel 这种），传完整名称会静默返回空内容。
    所以这里允许客户端用完整名称、缩写或大小写变体，统一转成上游 key。
    返回 None 表示没有匹配。
    """
    if not name:
        return None
    raw = name.strip()
    if raw in catalog:
        return raw

    low = raw.lower()
    # 去掉区域后缀再比一次
    base = low[:-3] if low.endswith("-cn") else low
    if base in catalog:
        return base

    for mid, meta in catalog.items():
        full = (meta.get("name") or "").strip().lower()
        if full and (full == base or full == low):
            return mid
    # 宽松匹配：忽略空格和点，例如 "glm 5.3 flash" / "GLM5.3Flash"
    squash = lambda x: x.replace(" ", "").replace(".", "").replace("-", "")
    target = squash(base)
    for mid, meta in catalog.items():
        if squash((meta.get("name") or "").strip().lower()) == target:
            return mid
        if squash(mid) == target:
            return mid
    return None


def build_chat_body(messages: list[dict], model: str, *, stream: bool = True,
                    tools: list | None = None, parameters: dict | None = None) -> bytes:
    """把 OpenAI 风格 messages 转成 Qoder 私有请求体，并做自定义编码。"""
    import uuid as _uuid

    # system 必须放进 messages 数组，顶层 system 字段服务端会忽略
    normalized = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        normalized.append({"role": role, "content": content})

    last_user = next(
        (m["content"] for m in reversed(normalized) if m.get("role") == "user"), ""
    )
    record_id = str(_uuid.uuid4())
    params = {"max_tokens": 8192}
    params.update(parameters or {})

    body = {
        "request_id": str(_uuid.uuid4()),
        "request_set_id": record_id,
        "chat_record_id": record_id,
        "session_id": str(_uuid.uuid4()),
        "stream": stream,
        "chat_task": "FREE_INPUT",
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "code_language": "",
        "chat_prompt": "",
        "image_urls": None,
        "aliyun_user_type": "",
        "system": "",
        "messages": normalized,
        "tools": tools or [],
        "parameters": params,
        "chat_context": {
            "chatPrompt": "",
            "imageUrls": None,
            "extra": {
                "context": [],
                "modelConfig": {"key": model, "is_reasoning": False},
                "originalContent": str(last_user)[:2000],
            },
            "features": [],
            "text": str(last_user)[:2000],
        },
        "model_config": {"key": model, "source": "system", "is_reasoning": False},
        "business": {
            "product": "cli",
            "version": "1.0.0",
            "type": "agent",
            "stage": "start",
            "id": str(_uuid.uuid4()),
            "name": str(last_user)[:30],
            "begin_at": int(__import__("time").time() * 1000),
        },
    }
    return qoder_encode(json.dumps(body, separators=(",", ":")).encode()).encode()


def chat_stream(account: dict, body: bytes, model: str):
    """发起推理请求，返回 SSE 行的迭代器（已解码的文本行）。"""
    region = account["region"]
    url = (
        f"{REGIONS[region]['base']}algo/api/v2/service/pro/sse/"
        f"agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
    )
    headers = build_cosy_headers(
        uid=account["uid"],
        access_token=account["access_token"],
        nickname=account.get("name", ""),
        email=account.get("email", ""),
        machine_id=derive_machine_id(account["uid"]),
        machine_token=derive_machine_token(account["uid"]),
        machine_hostname=derive_hostname(account["uid"]),
        machine_os=detect_machine_os(),
        body=body,
        url=url,
        model_key=model,
    )
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, e.read().decode("utf-8", "replace")) from e
    except urllib.error.URLError as e:
        raise UpstreamError(0, str(e), f"network error: {e.reason}") from e

    def gen():
        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                yield line

    return gen()


def fetch_quota(account: dict) -> dict:
    """查询账号积分余额。返回总额度、已用、剩余和附加额度。"""
    region = account["region"]
    url = f"{REGIONS[region]['openapi']}/api/v2/quota/usage"
    status, raw = _request(url, headers={
        "Authorization": f"Bearer {account['access_token']}",
        "Accept": "application/json",
        "User-Agent": "undici",
        "Cosy-Version": COSY_VERSION,
        "Cosy-ClientType": COSY_CLIENT_TYPE,
    })
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "查询积分失败")
    data = json.loads(text)

    quota = data.get("userQuota") or {}
    addon = data.get("addOnQuota") or {}
    return {
        "total": float(quota.get("total") or 0),
        "used": float(quota.get("used") or 0),
        "remaining": float(quota.get("remaining") or 0),
        "addon_total": float(addon.get("total") or 0),
        "addon_remaining": float(addon.get("remaining") or 0),
        "plan": data.get("userType") or "",
        "exceeded": bool(data.get("isQuotaExceeded")),
    }
