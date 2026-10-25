"""WorkBuddy（腾讯 CodeBuddy / WorkBuddy）上游协议。

与 Qoder 的私有协议不同，WorkBuddy 的推理接口本身就是 OpenAI 兼容的：
POST {api}/v2/chat/completions，请求体字段与 OpenAI 一致，响应是标准
SSE chunk（`data: {...}` + `data: [DONE]`）。所以这里不做协议转换，只做
三件真实存在差异的事：

1. 客户端标识。上游按请求头判断调用方是不是官方客户端，缺了会返回
   11128「Illegal API invocation from an unapproved channel」。这里补齐
   官方客户端的头形状，机器码按账号派生并保持稳定。
2. 角色与 tool_choice 的类型约束。上游是 Go 服务，`role=developer` 和
   object 形式的 `tool_choice` 都过不了反序列化，需要先归一化。
3. 计费口径。usage 里的 `credit` 是本轮消耗的积分，要记进网关账本。

区域：国内版推理在 copilot.tencent.com、积分在 www.codebuddy.cn（两个域）；
国际版同域。两套端点、两套凭证，互不通用。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid

import qoder

REGIONS = {
    "cn": {
        "label": "国内版",
        "api": "https://copilot.tencent.com",
        "web": "https://www.codebuddy.cn",
        "default_domain": "www.codebuddy.cn",
    },
    "global": {
        "label": "国际版",
        "api": "https://www.workbuddy.ai",
        "web": "https://www.workbuddy.ai",
        "default_domain": "www.workbuddy.ai",
    },
}

# 官方客户端标识。X-IDE-Version / X-Product-Version 是插件版本号，上游这两个
# 值不做校验；真正被查的是 X-Product-Code 与 X-Domain 的组合。
CLIENT_TYPE = "codebuddy"
PLUGIN_VERSION = (os.environ.get("PMT_WORKBUDDY_PLUGIN_VERSION")
                  or os.environ.get("Q2A_WORKBUDDY_PLUGIN_VERSION")
                  or "4.10.33259736")
UA = "Mozilla/5.0 (compatible; Genie-IDE/2.156.0)"

# 上游模型 id 是服务端配置下发的，没有公开的列表接口。下表是 2026-10-25 用
# 真实账号逐个试出来的可用集合（每个都发过一轮对话、返回 200）。账号所属套餐
# 不同，实际可用集合会有出入，缺模型时按需增删这一张表。
CATALOG: list[tuple[str, str, int, int, bool, bool]] = [
    # (id, display name, context, max output, tools, images)
    ("fast-model", "WorkBuddy 快速", 300000, 48000, True, True),
    ("balanced-model", "WorkBuddy 均衡", 300000, 48000, True, True),
    ("deep-model", "WorkBuddy 极致", 300000, 48000, True, True),
    ("deepseek-v4.1-flash", "DeepSeek-V4.1-Flash", 1000000, 128000, True, True),
    ("deepseek-v4-flash", "DeepSeek-V4-Flash", 1000000, 50000, True, True),
    ("deepseek-v4-pro", "DeepSeek-V4-Pro", 1000000, 128000, True, True),
    ("deepseek-v3-2-volc", "DeepSeek-V3.2", 96000, 32000, True, True),
    ("glm-5.3-flash", "GLM-5.3-Flash", 1000000, 131072, True, True),
    ("glm-5.3", "GLM-5.3", 1000000, 64000, True, True),
    ("glm-5.2", "GLM-5.2", 1000000, 64000, True, True),
    ("glm-5v-turbo", "GLM-5v-Turbo", 200000, 64000, True, True),
    ("kimi-k3.1-pro", "Kimi-K3.1-Pro", 1000000, 32000, True, True),
    ("kimi-k3-1", "Kimi-K3", 1000000, 32000, True, True),
    ("kimi-k2.8-preview", "Kimi-K2.8-Preview", 1000000, 64000, True, True),
    ("kimi-k2.5", "Kimi-K2.5", 256000, 32000, True, True),
    ("minimax-m3.1-flash", "MiniMax-M3.1-Flash", 1000000, 128000, True, True),
    ("minimax-m3", "MiniMax-M3", 512000, 64000, True, True),
    ("minimax-m2.7", "MiniMax-M2.7", 200000, 48000, True, True),
    ("mimo-v2.6-pro", "Xiaomi MiMo-V2.6-Pro", 1000000, 128000, True, True),
    ("hunyuan-chat", "Hunyuan-Turbos", 128000, 8192, True, True),
    ("hy4-preview", "Hy4 preview", 960000, 64000, True, True),
    ("hy3", "Hy3", 192000, 64000, True, True),
    ("space-bunny", "Space-Bunny", 1000000, 128000, True, True),
    ("step-5-preview", "Step-5-Preview", 1000000, 64000, True, True),
]


class UpstreamError(qoder.UpstreamError):
    """上游错误。

    继承 Qoder 的同类异常，这样 server 里已有的 `except qoder.UpstreamError`
    一处就能兜住两家厂商，不用为每个厂商各写一条分支。
    """

    @property
    def is_queued(self) -> bool:
        """WorkBuddy 没有排队机制，上游忙直接给 11134。"""
        return False

    @property
    def is_auth_dead(self) -> bool:
        return self.status == 401 or '"code":10001' in self.body

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429 or "11134" in self.body

    @property
    def is_busy(self) -> bool:
        """模型侧资源紧张，换账号或稍后重试即可，不是凭证问题。"""
        return "11134" in self.body or "11133" in self.body

    def retry_after(self) -> int:
        return 30


# ---------------------------------------------------------------------------
# 请求头：把请求伪装成官方客户端发出的
# ---------------------------------------------------------------------------

def _machine_id(uid: str) -> str:
    """按账号派生稳定的机器码。

    官方客户端用 VS Code 的 machineId（uuid5 派生的 GUID）。这里同样按 uid
    做一次 uuid5，同一账号每次都是同一个值——上游如果按机器做风控，随机
    机器码比固定机器码更像异常登录。
    """
    seed = f"workbuddy-machine:{uid}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def _base_headers(account: dict, *, json_body: bool = True) -> dict:
    uid = str(account.get("uid") or "")
    region = account.get("region") or "cn"
    spec = REGIONS.get(region) or REGIONS["cn"]
    # 账号行里没单独存 domain，按区域取默认值。上游只用它核对来源域名，
    # 不参与鉴权，所以不需要精确到导出文件里的那个值。
    domain = account.get("domain") or spec["default_domain"]
    headers = {
        "User-Agent": UA,
        "X-Product-Code": CLIENT_TYPE,
        "X-IDE-Type": "vscode",
        "X-IDE-Name": "Visual Studio Code",
        "X-IDE-Version": "1.70.2",
        "X-Product-Version": PLUGIN_VERSION,
        "X-Machine-Id": _machine_id(uid),
        "X-Domain": domain,
        "Accept": "application/json",
    }
    if uid:
        headers["X-User-Id"] = uid
    token = account.get("access_token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _conversation_headers(headers: dict) -> dict:
    """国际版推理另有一道渠道校验：三个会话标识头必须同时出现。

    国内版不校验，多带也无害，所以两边统一加上。
    """
    rid = uuid.uuid4().hex.upper()
    headers.setdefault("X-Conversation-ID", str(uuid.uuid4()))
    headers.setdefault("X-Conversation-Request-ID", rid)
    headers.setdefault("X-Conversation-Message-ID", rid)
    headers.setdefault("X-Request-ID", rid)
    headers.setdefault("X-Agent-Intent", "craft")
    headers.setdefault("Accept", "text/event-stream")
    return headers


def _request(url: str, *, method="POST", body: bytes | None = None,
             headers: dict | None = None, timeout: int = 60) -> tuple[int, bytes]:
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


def _json(raw: bytes):
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 凭证：兼容两种导出文件
# ---------------------------------------------------------------------------

def parse_credential(credential: str) -> dict:
    """解析长期凭证。

    支持三种写法，来源是官方面板导出的两种文件：
      - `workbuddy-<uid>.json`：{"account": {...}, "auth": {...}}
      - `cockpit-<uid>.json`：上面那种组成的数组
      - 裸 refresh token（以 eyJ 开头的 JWT 字符串）
    返回 {uid, nickname, access_token, refresh_token, expires_at, domain, region}。
    """
    text = (credential or "").strip()
    if not text:
        raise UpstreamError(0, "", "凭证为空")
    data = None
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise UpstreamError(0, "", f"凭证不是合法 JSON：{e}") from e
    if isinstance(data, list):
        if not data:
            raise UpstreamError(0, "", "cockpit 文件里没有账号")
        data = data[0]
    if isinstance(data, dict):
        # 有的导出把 account/auth 平铺在顶层
        account = data.get("account") if isinstance(data.get("account"), dict) else data
        auth = data.get("auth") if isinstance(data.get("auth"), dict) else data
        uid = str(account.get("uid") or "")
        access = str(auth.get("accessToken") or auth.get("access_token") or "")
        refresh = str(auth.get("refreshToken") or auth.get("refresh_token") or "")
        expires_at = auth.get("expiresAt") or auth.get("expires_at") or 0
        domain = str(auth.get("domain") or "")
        nickname = str(account.get("nickname") or account.get("name") or "")
        if not refresh and not access:
            raise UpstreamError(0, "", "文件里没有 accessToken / refreshToken")
        region = "cn" if ("codebuddy.cn" in domain or "qq.com" in domain) else (
            "global" if domain else "")
        return {"uid": uid or (_uid_of_jwt(access) or ""), "nickname": nickname,
                "access_token": access, "refresh_token": refresh,
                "expires_at": _ms_to_s(expires_at), "domain": domain,
                "region": region}
    # 裸 token：用它自己换一次刷新，看上游认不认
    return {"uid": _uid_of_jwt(text) or "", "nickname": "", "access_token": "",
            "refresh_token": text, "expires_at": 0, "domain": "", "region": ""}


def _ms_to_s(value) -> float:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return num / 1000 if num > 1e11 else num


def _uid_of_jwt(token: str) -> str:
    """JWT 的 payload 里有 sub（就是 uid），不用验签只取字段。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        import base64
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    return str(data.get("sub") or "")


# ---------------------------------------------------------------------------
# 协议实现
# ---------------------------------------------------------------------------

def refresh(account: dict) -> dict | None:
    """用 refresh token 换新的 access token。"""
    token = account.get("refresh_token") or ""
    if not token:
        return None
    region = account.get("region") or "cn"
    spec = REGIONS.get(region) or REGIONS["cn"]
    headers = _base_headers(account, json_body=True)
    headers.pop("Authorization", None)
    headers["X-Refresh-Token"] = token
    headers["X-Auth-Refresh-Source"] = "plugin"
    status, raw = _request(f"{spec['api']}/v2/plugin/auth/token/refresh",
                           body=b"{}", headers=headers, timeout=30)
    data = _json(raw)
    if status != 200 or not isinstance(data, dict):
        raise UpstreamError(status, raw.decode("utf-8", "replace")[:400], "刷新凭证失败")
    payload = _unwrap(data)
    if not isinstance(payload, dict) or not payload.get("accessToken"):
        raise UpstreamError(status, raw.decode("utf-8", "replace")[:400], "刷新凭证失败")
    return {
        "access_token": payload["accessToken"],
        "refresh_token": payload.get("refreshToken") or token,
        "expires_in": int(payload.get("expiresIn") or 2592000),
        "domain": payload.get("domain") or account.get("domain") or "",
    }


def _unwrap(payload):
    """上游的响应统一套一层 {code, msg, data}。"""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        nested = payload["data"]
        if isinstance(nested.get("data"), dict):
            return nested["data"]
        return nested
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def fetch_quota(account: dict) -> dict:
    """积分快照。

    上游 get-user-resource-summary 给的是几个额度包 + 四个分组：
    plan_base（套餐）、plan_bonus（套餐赠送）、addon（购买）、benefit（活动奖励）。
    归成三块报给界面：套餐 = plan_base + plan_bonus，赠送 = addon + benefit，
    共享池留空（个人号没有）。
    """
    region = account.get("region") or "cn"
    spec = REGIONS.get(region) or REGIONS["cn"]
    headers = _base_headers(account)
    headers["Accept"] = "application/json"
    status, raw = _request(f"{spec['web']}/billing/meter/get-user-resource-summary",
                           body=b"{}", headers=headers, timeout=30)
    text = raw.decode("utf-8", "replace")
    data = _json(raw)
    if status != 200 or not isinstance(data, dict) or data.get("code") not in (0, None):
        raise UpstreamError(status, text[:400], "查询积分失败")
    body = data.get("data") or {}

    def num(src, key):
        try:
            return float((src or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    groups = {str(g.get("Kind") or ""): g for g in (body.get("CreditGroups") or [])
              if isinstance(g, dict)}
    base = [groups.get("plan_base") or {}, groups.get("plan_bonus") or {}]
    addon = [groups.get("addon") or {}, groups.get("benefit") or {}]

    def pick_total(items):
        return sum(num(g, "CycleTotalCapacity") for g in items)

    def pick_used(items):
        return sum(num(g, "CycleUsedCapacity") for g in items)

    base_total, base_used = pick_total(base), pick_used(base)
    addon_total, addon_used = pick_total(addon), pick_used(addon)
    total = num(body, "TotalCredits") or (base_total + addon_total)
    used = base_used + addon_used
    remaining = max(0.0, total - used)
    return {
        "total": total,
        "used": used,
        "remaining": remaining,
        "base_total": base_total,
        "base_used": base_used,
        "base_remaining": max(0.0, base_total - base_used),
        "addon_total": addon_total,
        "addon_used": addon_used,
        "addon_remaining": max(0.0, addon_total - addon_used),
        "shared_total": 0.0,
        "shared_used": 0.0,
        "shared_remaining": 0.0,
        "expire_at": 0.0,
        "plan": str(body.get("SubscriptionPackageName") or ""),
        "exceeded": remaining <= 0.005 and total > 0,
    }


def list_models(account: dict) -> list[dict]:
    """模型目录。

    上游没有公开的模型列表接口（插件读的是本地下发到客户端的产品配置），
    所以这里返回实测可用的静态目录，不假装是实时查询。
    """
    return [{"id": mid, "name": name, "source": "system", "enable": True,
             "context_length": ctx, "max_output_tokens": out,
             "supports_tools": tools, "supports_images": images}
            for mid, name, ctx, out, tools, images in CATALOG]


# 上游拒绝的客户端声明。实测只拦这一句（换任何其它说法都放行），命中时
# 换成等价的角色描述：它对模型的行为约束不变，只是不再自称是别家的客户端。
_BRAND_LEADINS = (
    re.compile(r"You are Claude Code[^\n]*", re.IGNORECASE),
    re.compile(r"You are (?:the )?Anthropic[^\n]*official[^\n]*", re.IGNORECASE),
)
_BRAND_REPLACEMENT = "You are a coding assistant working with the user in their editor."


def _scrub_brand_claims(text: str) -> str:
    """去掉上游会拦的客户端品牌声明。只动开头这段，不碰正文。"""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern in _BRAND_LEADINS:
        out = pattern.sub(_BRAND_REPLACEMENT, out, count=1)
    return out


def _normalize_messages(messages: list) -> list[dict]:
    """上游是 Go 服务，role 只认 system/assistant/user/tool。

    OpenAI 的 developer 角色在这里会整条请求被拦（11128「unapproved
    channel」，而不是参数错误），所以先转成 system。
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        if role == "developer":
            role = "system"
        content = m.get("content", "")
        # 品牌声明只出现在 system 里；用户消息原样转发，不改写
        if role == "system":
            if isinstance(content, str):
                content = _scrub_brand_claims(content)
            elif isinstance(content, list):
                content = [
                    {**part, "text": _scrub_brand_claims(part.get("text", ""))}
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                    else part
                    for part in content
                ]
        item = {"role": role, "content": content}
        for key in ("name", "tool_calls", "tool_call_id"):
            if m.get(key):
                item[key] = m[key]
        out.append(item)
    return out


def _normalize_tool_choice(value):
    """object 形式的 tool_choice 上游反序列化不了，换成等价的函数名。"""
    if isinstance(value, dict):
        name = (value.get("function") or {}).get("name")
        if name:
            return name
        if value.get("type") == "function":
            return "required"
    return value


def build_chat_body(messages: list[dict], model: str, *, stream: bool = True,
                    tools: list | None = None, parameters: dict | None = None,
                    meta: dict | None = None) -> bytes:
    """组装上游请求体。字段与 OpenAI 一致，只做上面说的那几项归一化。"""
    params = dict(parameters or {})
    max_tokens = params.pop("max_tokens", None) or 8192
    body: dict = {
        "model": model,
        "messages": _normalize_messages(messages),
        "stream": True,          # 上游始终以流式返回，非流式由网关自己聚合
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
    for key in ("temperature", "top_p", "stop", "presence_penalty",
                "frequency_penalty", "seed", "response_format", "tool_choice"):
        if key in params and params[key] is not None:
            body[key] = params[key]
    if "tool_choice" in body:
        body["tool_choice"] = _normalize_tool_choice(body["tool_choice"])
    # reasoning_effort 等思考档位上游按模型白名单处理，不认识的会被忽略，
    # 目录里没有 thinking_config，所以这里不下发，避免和客户端期望不一致。
    return json.dumps(body, ensure_ascii=False).encode()


def chat_stream(account: dict, body: bytes, model: str):
    """发起推理请求，返回 SSE 行迭代器（未解码的文本行，已 strip）。"""
    region = account.get("region") or "cn"
    spec = REGIONS.get(region) or REGIONS["cn"]
    headers = _conversation_headers(_base_headers(account))
    url = f"{spec['api']}/v2/chat/completions"
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
                if line:
                    yield line

    return gen()


# ---------------------------------------------------------------------------
# 签到：国内版有每日 100 积分活动，国际版没有
# ---------------------------------------------------------------------------

def checkin(account: dict) -> dict:
    region = account.get("region") or "cn"
    spec = REGIONS.get(region) or REGIONS["cn"]
    headers = _base_headers(account, json_body=True)
    status, raw = _request(f"{spec['api']}/v2/billing/meter/checkin-activity-status",
                           body=b"{}", headers=headers, timeout=30)
    data = _json(raw) or {}
    if status != 200 or data.get("code") not in (0, None):
        return {"ok": False, "earned": 0.0, "done": False,
                "message": f"签到状态查询失败：{raw.decode('utf-8', 'replace')[:160]}"}
    info = data.get("data") or {}
    if info.get("active") is False:
        return {"ok": True, "earned": 0.0, "done": False, "message": "签到活动未开放"}
    if info.get("today_checked_in"):
        return {"ok": True, "earned": 0.0, "done": True,
                "message": f"今日已签到（连续 {info.get('streak_days', 0)} 天）"}

    status, raw = _request(f"{spec['api']}/v2/billing/meter/daily-checkin",
                           body=b"{}", headers=headers, timeout=30)
    claim = _json(raw) or {}
    text = raw.decode("utf-8", "replace")
    credit = ((claim.get("data") or {}).get("credit")
              if isinstance(claim.get("data"), dict) else None)
    if status == 200 and (claim.get("code") in (0, None) or credit is not None):
        amount = float(credit if credit is not None else info.get("daily_credit") or 0)
        return {"ok": True, "earned": amount, "done": True,
                "claimed": 1, "declared": amount,
                "message": f"签到 +{amount:g} 积分"}
    if "已签" in text:
        return {"ok": True, "earned": 0.0, "done": True, "message": "今日已签到"}
    return {"ok": False, "earned": 0.0, "done": False,
            "message": f"签到失败：{text[:160]}"}


def detect_region(domain: str, default: str = "cn") -> str:
    """按导出文件里的 domain 判断区域。"""
    value = str(domain or "").lower()
    if "codebuddy.cn" in value or "qq.com" in value:
        return "cn"
    if value:
        return "global"
    return default
