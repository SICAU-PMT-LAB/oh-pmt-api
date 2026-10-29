"""福利账号：任意 OpenAI 兼容上游，凭 api-key + base-url 接入。

与 Qoder / WorkBuddy 的区别在于协议没有私有部分——就是标准的
`POST {base}/chat/completions` 与 `GET {base}/models`，所以这里不需要
签名、不需要换 token，凭证本身就是可用的 API Key。

额度是 ∞：这些账号的请求仍然照常计量（usage 里有 token 与成本），
但计费时按 0 元计，不占成员的档位百分比。判断依据是账号的
`welfare=1` 标记，不是厂商名——同一家上游将来也可能接入非福利账号。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

# 探测模型是否真的能跑：用一个极小的非流式请求。有些上游 /models 里列着
# 但实际没有配额或需要额外开通，只看 /models 会导入一批用不了的模型。
PROBE_PROMPT = "hi"
PROBE_MAX_TOKENS = 1


class UpstreamError(Exception):
    def __init__(self, status: int, body: str = "", detail: str = ""):
        self.status = status
        self.body = body
        self.detail = detail or body
        super().__init__(f"upstream {status}: {str(body)[:200]}")


def normalize_base_url(raw: str) -> str:
    """把用户填的 base-url 收敛成「不带结尾斜杠、以 /v1 结尾」的形式。

    常见写法有 https://x.com/v1、https://x.com/v1/、https://x.com 三种，
    直接拼 /chat/completions 会出现双斜杠或缺 /v1。

    已经带了路径的地址按原样用：用户直接粘了 `.../v1/chat/completions` 或
    某个代理前缀时，再补 /v1 只会拼出一个不存在的路径。
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # 已经指向具体端点或带了版本前缀：不再补 /v1
    if url.endswith("/v1") or "/v1/" in url:
        return url
    return url + "/v1"


def normalize_key(raw: str) -> str:
    """凭证可以是明文，也可以是 base64。

    粘贴 base64 时常常夹带换行或空格（从网页 base64 工具复制就是如此），
    所以先去掉所有空白再判定。判定失败就按明文处理——明文 key 里出现
    `=` 结尾或整串都落在 base64 字符集内是可能的，不能一律强行解码。
    """
    text = "".join((raw or "").split())
    if not text:
        return ""
    # 明文 key 常见形态（sk-xxx、cr_xxx）含 - 与 _，也在 base64 字符集里，
    # 所以必须靠长度与能否解出文本双重判断，避免把明文当 base64 解坏。
    if len(text) < 16 or len(text) % 4 != 0:
        return text
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return text
    # 解出来的必须是可打印 ASCII 的 token 形态，否则说明原本就是明文
    if not decoded or not all(33 <= ord(ch) <= 126 for ch in decoded):
        return text
    return decoded


def _headers(key: str) -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _request(url: str, key: str, body: bytes | None = None,
             timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST" if body else "GET")
    for k, v in _headers(key).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise UpstreamError(0, str(e), f"network error: {e.reason}") from e


def list_models(account: dict) -> list[dict]:
    """拉取上游模型清单，只返回 id/name。"""
    base = normalize_base_url(account.get("base_url") or "")
    key = account.get("api_key") or account.get("credential") or ""
    if not base:
        raise UpstreamError(0, "", "缺少 base-url")
    status, raw = _request(f"{base}/models", key)
    text = raw.decode("utf-8", "replace")
    if status != 200:
        raise UpstreamError(status, text, "拉取模型列表失败")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise UpstreamError(0, text, f"模型列表不是合法 JSON：{e.msg}") from e
    items = data.get("data") if isinstance(data, dict) else data
    out = []
    for m in items if isinstance(items, list) else []:
        if isinstance(m, str):
            out.append({"id": m, "name": m})
            continue
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if mid:
            out.append({"id": mid, "name": m.get("name") or mid})
    return out


def probe_model(account: dict, model: str) -> tuple[bool, str]:
    """发一个最小请求，确认这个模型真的能出结果。

    返回 (可用, 说明)。非流式 + max_tokens=1，成本可以忽略；失败时把上游
    的报错原样带出来，管理员能看出是没开通、欠费还是模型名不对。
    """
    base = normalize_base_url(account.get("base_url") or "")
    key = account.get("api_key") or account.get("credential") or ""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }).encode()
    try:
        status, raw = _request(f"{base}/chat/completions", key, body, timeout=60)
    except UpstreamError as e:
        return False, e.detail[:200]
    text = raw.decode("utf-8", "replace")
    if status != 200:
        return False, f"HTTP {status}: {text[:200]}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False, f"返回的不是 JSON：{text[:120]}"
    if isinstance(data, dict) and data.get("error"):
        return False, str(data["error"])[:200]
    if isinstance(data, dict) and data.get("choices"):
        return True, ""
    return False, f"返回里没有 choices：{text[:120]}"


def chat_stream(account: dict, body: bytes, model: str):
    """发起流式对话，返回 SSE 行迭代器。"""
    base = normalize_base_url(account.get("base_url") or "")
    key = account.get("api_key") or account.get("credential") or ""
    req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST")
    for k, v in _headers(key).items():
        req.add_header(k, v)
    req.add_header("Accept", "text/event-stream")
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


def build_chat_body(messages: list[dict], model: str, *, stream: bool = True,
                    tools: list | None = None, parameters: dict | None = None,
                    meta: dict | None = None) -> bytes:
    """标准 OpenAI 请求体。福利账号是直通上游，不做字段改写。"""
    params = dict(parameters or {})
    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    max_tokens = params.pop("max_tokens", None)
    if max_tokens:
        body["max_tokens"] = max_tokens
    if tools:
        body["tools"] = tools
    for key in ("temperature", "top_p", "stop", "presence_penalty",
                "frequency_penalty", "seed", "response_format", "tool_choice",
                "reasoning_effort", "reasoning", "thinking"):
        if key in params and params[key] is not None:
            body[key] = params[key]
    if body.get("stream"):
        body["stream_options"] = {"include_usage": True}
    return json.dumps(body, ensure_ascii=False).encode()
