"""与 Qoder 上游通信：PAT 交换、token 刷新、模型列表、对话推理。"""

from __future__ import annotations

import json
import os
import re
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
    def is_queued(self) -> bool:
        """上游模型排队限流（10605）。

        排队时上游也返回 403，但这不是凭证问题，账号完全正常，
        只是模型侧资源紧张，稍后重试即可，不能触发账号冷却。
        """
        return "10605" in self.body or '"isQueued":true' in self.body

    @property
    def is_auth_dead(self) -> bool:
        """凭证失效，需要重新用 PAT 换 token。"""
        if self.is_queued:
            return False
        if any(k in self.body for k in
               ("TOKEN_EXPIRE", "12153", "Offline user session not found")):
            return True
        return self.status == 401

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429 or self.is_queued

    @property
    def is_request_rejected(self) -> bool:
        """请求内容本身被拒，换账号重发也没用。

        子类用它标记「这条请求不该重试、也不该给账号加冷却」的错误，
        例如上游对客户端模板的渠道校验。默认没有这类错误。
        """
        return False

    def retry_after(self) -> int:
        """上游要求等待的秒数，拿不到就给 30。"""
        import re
        m = re.search(r"retryAfterSeconds\\\\?\"?\s*:\s*(\d+)", self.body)
        if m:
            return max(1, min(300, int(m.group(1))))
        return 30


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
                entry = {
                    "id": mid,
                    "name": display_name(mid, m.get("display_name") or m.get("name") or mid),
                    "source": m.get("source") or "system",
                    "enable": m.get("enable", True),
                    # 思考能力随目录一起带下来，请求侧要用它判断能发哪些档位
                    "is_reasoning": bool(m.get("is_reasoning")),
                    "thinking_config": m.get("thinking_config") or {},
                }
                if mid in MODEL_ALIASES:
                    entry["aliases"] = MODEL_ALIASES[mid]
                # 上下文与多模态能力补自本地表，上游接口不返回这两项
                meta = MODEL_META.get(mid)
                if meta:
                    entry["context_length"] = meta[0]
                    entry["max_output_tokens"] = meta[1]
                    entry["supports_images"] = meta[2]
                out.append(entry)
    return out


# 上游命名不统一（Qwen3.8-Flash 无连字符、GLM-5.3-Flash 有），
# 对外统一成 "<家族>-<版本>-<档位>" 的写法，只影响展示。
DISPLAY_NAMES = {
    "qmodel_38max": "Qwen-3.8-Max",
    "qfmodel": "Qwen-3.8-Flash",
    "kmodel_latest": "Kimi-K3",
    "kmodel": "Kimi-K2.8-Preview",
    # 和 WorkBuddy 的同款模型对齐：两家上游叫法不同（这里上游是
    # DeepSeek-Flash，那边是 DeepSeek-V4.1-Flash），对外只暴露一个名字，
    # 客户端不必知道自己接的是哪家账号池。
    "dfmodel": "DeepSeek-V4.1-Flash",
}

# 改名后仍要认得的旧名字，由 list_models 挂到目录条目上
MODEL_ALIASES = {
    "dfmodel": ["DeepSeek-Flash"],
}

# 上游的模型列表只给 key、显示名和思考档位，不给上下文长度与多模态能力。
# 这几项成员在控制台要看（判断能不能放长文、能不能传图），所以按官方
# 客户端缓存里的真实数值补一张本地表：(上下文, 最大输出, 是否支持图片)。
# 缺失的模型不猜，前端显示「—」。
MODEL_META = {
    "auto":        (200000, 64000, True),
    "ultimate":    (1000000, 128000, True),
    "efficient":   (200000, 64000, True),
    "fast":        (200000, 64000, True),
    "smodel":      (180000, 64000, True),
    "cmodel":      (180000, 64000, True),
    "qmodel_38max": (180000, 64000, True),
    "qfmodel":     (180000, 64000, True),
    "kmodel_latest": (180000, 64000, True),
    "kmodel":      (180000, 64000, True),
    "gmodel":      (180000, 64000, True),
    "gfmodel":     (1000000, 131072, True),
    "dmodel":      (1000000, 128000, True),
    "dfmodel":     (1000000, 128000, True),
    "mmodel":      (180000, 128000, True),
}


def display_name(upstream_key: str, raw: str) -> str:
    """把上游的 display_name 规范成统一风格。"""
    return DISPLAY_NAMES.get(upstream_key) or raw


def quota_exhausted(frame: str) -> bool:
    """首帧是否是该账号的额度不足（code 112）。

    112 是账号级的：这个账号买不起这个模型的套餐，额度页在 pricingUrl 里。
    同一个密钥下的其他账号可能完全正常，所以遇到它要换账号，而不是让
    整个请求 502。与 10605 一样藏在首帧的信封里，只有 statusCodeValue
    非 200 时才会被 convert.iter_chunks 当作错误抛出来。

    帧内是多重 JSON 转义，不同序列化方式的空白也不一样，所以用正则跳过
    反斜杠和空格，不去数层数。
    """
    return re.search(r"code\\*[\"']?\s*:?\s*\\*[\"']?112\\*[\"']?", frame) is not None


def queue_info(frame: str) -> int:
    """从首帧判断是否在排队。是则返回建议等待秒数，否则 0。

    上游排队时 HTTP 状态是 200，错误藏在第一个 SSE 帧里（code 10605）。
    帧内是多重 JSON 转义，直接定位 retryAfterSeconds 后的第一组数字即可，
    避免去数反斜杠层数。
    """
    if "10605" not in frame and "isQueued" not in frame:
        return 0
    i = frame.find("retryAfterSeconds")
    if i >= 0:
        m = re.match(r"retryAfterSeconds\D*(\d+)", frame[i:])
        if m:
            return max(1, min(300, int(m.group(1))))
    return 30


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
    # 显示名改过之后旧名字还要能用：客户端配置里存着的多半是旧名
    for mid, meta in catalog.items():
        for alias in meta.get("aliases") or []:
            if squash(str(alias).strip().lower()) == target:
                return mid
    return None


EFFORT_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4,
               "xhigh": 5, "max": 6}
EFFORT_OFF = ("none", "off", "disabled", "disable", "false", "0", "no")


def supported_efforts(meta: dict | None) -> list[str]:
    """该模型官方支持的思考档位，取自目录里的 thinking_config。

    空列表表示这个模型只有开/关、没有档位可调。
    """
    tc = (meta or {}).get("thinking_config") or {}
    enabled = tc.get("enabled") if isinstance(tc.get("enabled"), dict) else {}
    efforts = enabled.get("efforts") if isinstance(enabled.get("efforts"), dict) else {}
    return [e for e in EFFORT_RANK if e in efforts]


def default_effort(meta: dict | None) -> str | None:
    """该模型官方默认档位，取自 thinking_config.enabled.efforts 里的 is_default。"""
    tc = (meta or {}).get("thinking_config") or {}
    enabled = tc.get("enabled") if isinstance(tc.get("enabled"), dict) else {}
    efforts = enabled.get("efforts") if isinstance(enabled.get("efforts"), dict) else {}
    for name, cfg in efforts.items():
        if isinstance(cfg, dict) and cfg.get("is_default"):
            return name
    return None


def normalize_effort(effort, meta: dict | None) -> str | None:
    """把客户端请求的档位对齐到该模型支持的集合，None 表示不下发。

    上游对不认识的档位不报错、直接忽略，所以这里先把关：不支持的档位取
    最接近的合法档位，距离相同时优先该模型的默认档，避免客户端以为设置
    生效、实际回到默认档。
    """
    e = str(effort or "").strip().lower()
    if not e:
        return None
    if e in EFFORT_OFF:
        return "none"
    sup = supported_efforts(meta)
    if not sup:
        return None
    if e in sup:
        return e
    if e not in EFFORT_RANK:
        return None
    want = EFFORT_RANK[e]
    fallback = EFFORT_RANK.get(default_effort(meta) or "", want)

    def distance(cand):
        return (abs(EFFORT_RANK[cand] - want), abs(EFFORT_RANK[cand] - fallback))

    return min(sup, key=distance)


def requested_effort(body: dict | None) -> str | None:
    """从 OpenAI 风格请求体取思考档位，兼容几种常见写法。"""
    if not isinstance(body, dict):
        return None
    effort = body.get("reasoning_effort")
    if not effort and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort")
    if not effort and isinstance(body.get("thinking"), dict):
        thinking = body["thinking"]
        effort = thinking.get("effort") or thinking.get("level")
    return effort or None


def build_chat_body(messages: list[dict], model: str, *, stream: bool = True,
                    tools: list | None = None, parameters: dict | None = None,
                    meta: dict | None = None) -> bytes:
    """把 OpenAI 风格 messages 转成 Qoder 私有请求体，并做自定义编码。

    meta 是该模型在目录里的元数据，用来声明 is_reasoning；缺失时按不支持思考处理。
    """
    import uuid as _uuid

    # system 必须放进 messages 数组，顶层 system 字段服务端会忽略
    normalized = []
    for m in messages:
        role = m.get("role", "user")
        # OpenAI 的 developer 角色等同于 system，但上游用 serde 枚举校验，
        # 只认 system/assistant/user/tool，原样转发会整条请求被拒
        if role == "developer":
            role = "system"
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
    is_reasoning = bool((meta or {}).get("is_reasoning"))

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
                "modelConfig": {"key": model, "is_reasoning": is_reasoning},
                "originalContent": str(last_user)[:2000],
            },
            "features": [],
            "text": str(last_user)[:2000],
        },
        "model_config": {"key": model, "source": "system", "is_reasoning": is_reasoning},
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
    """查询账号积分余额。返回总额度、已用、剩余和附加额度。

    上游 /api/v2/quota/usage 的结构是 userQuota（套餐内）+ addOnQuota（赠送/
    签到附加），部分账号还会有共享池（orgResourcePackage 等）。判断「账号的
    全部额度」必须把这几块都算上，只看 userQuota 会把免费号的 200 赠送额度
    显示成 0。
    """
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

    def _num(src: dict, key: str) -> float:
        try:
            return float((src or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    quota = data.get("userQuota") or {}
    addon = data.get("addOnQuota") or {}

    # 共享额度池：团队/企业号才有，字段名各家写法不一，取第一个命中的。
    shared = None
    for key in ("orgResourcePackage", "sharedQuota", "teamQuota", "poolQuota"):
        src = data.get(key)
        if isinstance(src, dict):
            shared = src
            break
    shared_total = _num(shared, "cap") or _num(shared, "total") if shared else 0.0
    shared_used = _num(shared, "used") if shared else 0.0
    shared_remaining = _num(shared, "remaining") if shared else 0.0
    if shared and shared_total < 0:      # cap=-1 表示不限量，不当作真实额度
        shared_total = shared_used = shared_remaining = 0.0
    if shared and shared.get("available") is False:
        shared_total = shared_used = shared_remaining = 0.0

    base_total, base_used = _num(quota, "total"), _num(quota, "used")
    addon_total, addon_used = _num(addon, "total"), _num(addon, "used")

    # 到期时间：上游给的是毫秒；253402214400000 是「永不过期」占位，归 0
    expire_ms = data.get("expiresAt") or 0
    try:
        expire_at = float(expire_ms) / 1000 if float(expire_ms) > 1e11 else float(expire_ms)
    except (TypeError, ValueError):
        expire_at = 0.0
    if expire_at >= 253402214400:
        expire_at = 0.0

    return {
        "total": base_total + addon_total + shared_total,
        "used": base_used + addon_used + shared_used,
        "remaining": (base_total - base_used) + (addon_total - addon_used)
                     + shared_remaining,
        "base_total": base_total,
        "base_used": base_used,
        "base_remaining": _num(quota, "remaining"),
        "addon_total": addon_total,
        "addon_used": addon_used,
        "addon_remaining": _num(addon, "remaining"),
        "shared_total": shared_total,
        "shared_used": shared_used,
        "shared_remaining": shared_remaining,
        "expire_at": expire_at,
        "plan": data.get("userType") or "",
        "exceeded": bool(data.get("isQuotaExceeded")),
    }


# ---------------------------------------------------------------------------
# 每日签到：活动平台 /sash/api/v1/me/campaigns
# ---------------------------------------------------------------------------
# 官方把「每日领取 100 Credits」做成了活动平台上的活动，旧版直签接口
# /sash/api/v1/me/daily-check-in 在现行账号上已经 404。所以签到分两步：
#   1) 列出活动，找 claimStatus=CLAIMABLE 且 benefit.kind=CREDITS 的
#   2) 逐个 POST /campaigns/{id}/claim（服务端幂等，重复领取返回 replayed）
# 列表接口对请求头敏感：缺 UA / cosy-clienttype / cosy-version 时服务端不报错，
# 只是返回 showCampaign=false 的空列表。所以这里用桌面端的头组合。
DESKTOP_CLIENT_TYPE = "10"
DESKTOP_VERSION = (os.environ.get("PMT_DESKTOP_VERSION")
                   or os.environ.get("Q2A_DESKTOP_VERSION") or "0.4.8")


def _sash_headers(account: dict, *, json_body: bool = False) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Qoder",
        "Authorization": f"Bearer {account['access_token']}",
        "cosy-clienttype": DESKTOP_CLIENT_TYPE,
        "cosy-version": DESKTOP_VERSION,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def list_campaigns(account: dict) -> dict:
    """列出该账号可见的官方活动。返回原始结构，调用方自己挑可领取的。"""
    url = f"{REGIONS[account['region']]['openapi']}/sash/api/v1/me/campaigns"
    status, raw = _request(url, headers=_sash_headers(account), timeout=20)
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "活动列表查询失败")
    return json.loads(text)


def claim_campaign(account: dict, campaign_id: str) -> dict:
    """领取单个活动奖励。上游是幂等的，已领过会返回 replayed=true。"""
    url = (f"{REGIONS[account['region']]['openapi']}"
           f"/sash/api/v1/me/campaigns/{campaign_id}/claim")
    status, raw = _request(url, method="POST", body=b"{}",
                           headers=_sash_headers(account, json_body=True), timeout=20)
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "领取失败")
    return json.loads(text)


def claimable_campaigns(account: dict) -> dict:
    """把活动列表整理成 {claimable:[…], claimed:[…], done:bool, message:str}。

    只认可领取 Credits 类活动（benefit.kind=CREDITS 或金额>0）；纯展示类活动
    （actionType=VIEW_DETAILS 且没有 reward）不算签到项，否则「今天签没签」
    永远判不准。
    """
    data = list_campaigns(account)
    claimable, claimed = [], []
    for c in data.get("campaigns") or []:
        if not isinstance(c, dict):
            continue
        benefit = c.get("benefit") if isinstance(c.get("benefit"), dict) else {}
        try:
            amount = int(benefit.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        kind = str(benefit.get("kind") or "").upper()
        if str(c.get("actionType") or "") != "CLAIM_BENEFIT" and not amount:
            continue
        if kind and kind != "CREDITS":
            continue          # 券 / 兑换码类不归签到管
        item = {
            "id": c.get("campaignId") or "",
            "key": c.get("campaignKey") or c.get("campaignId") or "",
            "amount": amount,
            "status": str(c.get("claimStatus") or ""),
        }
        if item["status"] == "CLAIMABLE":
            claimable.append(item)
        elif item["status"] == "CLAIMED":
            claimed.append(item)

    return {
        "show": bool(data.get("showCampaign")),
        "claimable": claimable,
        "claimed": claimed,
        "done": bool(claimed) and not claimable,
        "message": ("" if data.get("showCampaign")
                    else "上游未返回活动列表，本区域可能未开放签到"),
    }


def checkin_account(account: dict) -> dict:
    """给一个账号签到。返回 {ok, earned, message, done, campaigns}。"""
    try:
        state = claimable_campaigns(account)
    except UpstreamError as e:
        # e 的文案本身就是「活动列表查询失败」，别再加一遍前缀
        return {"ok": False, "earned": 0.0, "done": False,
                "message": str(e), "campaigns": {}}
    if not state["claimable"]:
        if state["claimed"]:
            return {"ok": True, "earned": 0.0, "done": True,
                    "message": "今日已签到", "campaigns": state}
        return {"ok": True, "earned": 0.0, "done": False,
                "message": state["message"] or "本账号当前没有可领取的签到活动",
                "campaigns": state}

    claimed, errors = [], []
    for item in state["claimable"]:
        try:
            res = claim_campaign(account, item["id"])
        except UpstreamError as e:
            errors.append(f"{item['key']}: {e}")
            continue
        failure = str(res.get("failureCode") or "").upper()
        if failure:
            errors.append(f"{item['key']}: {failure}")
            continue
        if str(res.get("status") or "").upper() in ("CLAIMED", "GRANTED", "SUCCESS"):
            if not res.get("replayed"):
                claimed.append(item)

    if errors and not claimed:
        return {"ok": False, "earned": 0.0, "done": False,
                "message": "；".join(errors)[:200], "campaigns": state}
    # 「到账多少积分」由调用方按额度差额判定（要刷新后再比一次，见 server）。
    # 活动面额只能当预期值：写着 +100 也可能延迟到账或按人去重不发。
    declared = sum(float(c["amount"] or 0) for c in claimed)
    if claimed:
        detail = f"已领取 {len(claimed)} 个活动（面额 {declared:g} 积分）"
    else:
        detail = "今日已签到"
    return {
        "ok": True, "earned": 0.0, "declared": declared, "claimed": len(claimed),
        "done": True,
        "message": detail + ("；" + "；".join(errors)[:150] if errors else ""),
        "campaigns": state,
    }
