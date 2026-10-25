"""把上游的 SSE 流转换成 OpenAI 兼容格式。

有两种上游形态，这里都认：

Qoder —— 外面套一层信封，内层已经是 OpenAI chunk：
    data:{"headers":{...},"body":"<内层 OpenAI chunk 的 JSON 字符串>","statusCodeValue":200}
    data:{"body":"[DONE]"}
    event:finish{...}   ← 计时元数据，忽略

WorkBuddy 这类 OpenAI 兼容上游 —— 帧本身就是 chunk，没有信封：
    data:{"id":"...","choices":[{"delta":{...}}]}
    data: [DONE]
"""

from __future__ import annotations

import json
import time
import uuid


class UpstreamStatusError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"upstream status {status}: {str(body)[:200]}")


def _take_usage(usage: dict, holder: dict) -> None:
    """记下最后一帧的 usage。

    Qoder 的字段叫 credits，WorkBuddy 叫 credit，两者都可能带原始价。
    """
    holder["usage"] = usage
    for key in ("credits", "credit"):
        if usage.get(key) is not None:
            holder["credits"] = float(usage[key])
            break
    for key in ("original_credits", "original_credit"):
        if usage.get(key) is not None:
            holder["credits_original"] = float(usage[key])
            break


def _normalize_chunk(payload: str) -> str:
    """把上游 chunk 里不合 OpenAI 语义的字段修掉。

    目前只有一处：WorkBuddy 每帧都带 finish_reason，中间那些是空字符串。
    OpenAI 的约定是中间帧给 null、最后一帧才给 'stop'/'tool_calls' 这类值，
    而空字符串既不是 null 也不是合法值——按 finish_reason 判断流是否正常
    结束的客户端（dsh 就是这样）会认为流没结束，于是丢掉整轮结果重试。

    只在确实需要改的时候重新序列化，其余情况原样返回，省掉一遍 JSON 往返。
    """
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if not isinstance(chunk, dict):
        return payload
    changed = False
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return payload
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason") == "":
            choice["finish_reason"] = None
            changed = True
    if not changed:
        return payload
    return json.dumps(chunk, ensure_ascii=False)


def _data_of(line: str) -> str:
    if not line.startswith("data:"):
        return ""
    return line[5:].strip()


def iter_chunks(lines, usage_holder: dict | None = None):
    """拆信封，产出内层 OpenAI chunk 的 JSON 字符串。"""
    for line in lines:
        payload = _data_of(line)
        if not payload:
            continue
        if payload == "[DONE]":
            return
        try:
            outer = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(outer, dict):
            continue

        # 没有信封的直出富 chunk：原样当内层用
        if not isinstance(outer.get("body"), str):
            if "statusCodeValue" in outer or "headers" in outer:
                continue
            if outer.get("choices") is not None or outer.get("error") is not None:
                usage = outer.get("usage")
                if usage and usage_holder is not None:
                    _take_usage(usage, usage_holder)
                yield _normalize_chunk(payload)
            continue

        status = outer.get("statusCodeValue")
        if status not in (None, 200, "200"):
            body = outer.get("body")
            raise UpstreamStatusError(
                status,
                body if isinstance(body, str) else json.dumps(outer, ensure_ascii=False)[:400],
            )

        body = outer.get("body")
        if not isinstance(body, str):
            continue
        if body == "[DONE]":
            return
        try:
            inner = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(inner, dict) and usage_holder is not None:
            usage = inner.get("usage")
            if usage:
                _take_usage(usage, usage_holder)
        yield _normalize_chunk(body)


def to_sse(chunk_json: str) -> bytes:
    return ("data: " + chunk_json + "\n\n").encode()


DONE = b"data: [DONE]\n\n"


def aggregate(lines, model: str, usage_holder: dict | None = None) -> dict:
    """把流折叠成一个非流式 chat.completion 对象。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = "stop"
    created = int(time.time())

    for chunk_json in iter_chunks(lines, usage_holder):
        try:
            chunk = json.loads(chunk_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("created"):
            created = int(chunk["created"])
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}}
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    usage = (usage_holder or {}).get("usage") or {}
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "credits": float((usage_holder or {}).get("credits") or 0),
        },
    }
