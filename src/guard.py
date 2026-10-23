"""访问控制：IP 白名单、模型白名单、token 限额。

这三项是本项目相对 qoder2api-hub 的增量，它的限额是账号级的、
且没有 IP 校验（全仓 grep ip_whitelist 零命中）。
"""

from __future__ import annotations

import ipaddress
import time


class Denied(Exception):
    """请求被拒绝，带 HTTP 状态码与错误码，直接回流给客户端。"""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _parse_networks(spec: str) -> list:
    nets = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            # 允许写裸 IP，自动补 /32 或 /128
            try:
                nets.append(ipaddress.ip_network(
                    ipaddress.ip_address(part).exploded + ("/32" if ":" not in part else "/128"),
                    strict=False,
                ))
            except ValueError:
                continue
    return nets


def client_ip(handler, trust_forwarded: bool) -> str:
    """取真实客户端 IP。只有信任反代时才读 X-Forwarded-For。"""
    if trust_forwarded:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        real = handler.headers.get("X-Real-IP", "")
        if real:
            return real.strip()
    return handler.client_address[0]


def check_ip(key: dict, ip: str):
    nets = _parse_networks(key.get("ip_allowlist", ""))
    if not nets:
        return  # 空白名单 = 不限制
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise Denied(403, "ip_invalid", f"无法识别的来源地址: {ip}")
    for n in nets:
        # 只比较同族地址，避免 v4 与 v6 混比报错
        if n.version == addr.version and addr in n:
            return
    raise Denied(403, "ip_forbidden",
                 f"来源地址 {ip} 不在该 API Key 的允许列表内")


def check_model(key: dict, model: str):
    spec = (key.get("model_allowlist") or "").strip()
    if not spec:
        return  # 空 = 不限制
    allowed = {m.strip() for m in spec.split(",") if m.strip()}
    if model in allowed:
        return
    raise Denied(403, "model_forbidden",
                 f"该 API Key 不允许使用模型 {model}")


def check_quota(store, key: dict):
    """检查日限额与总额限额。0 表示不限。"""
    daily = int(key.get("token_limit_daily") or 0)
    total = int(key.get("token_limit_total") or 0)
    if daily > 0:
        since = time.time() - 86400
        used = store.tokens_used_since(key["id"], since)
        if used >= daily:
            raise Denied(429, "quota_daily_exceeded",
                         f"已达每日 token 上限（{used}/{daily}）")
    if total > 0:
        used = store.tokens_used_total(key["id"])
        if used >= total:
            raise Denied(429, "quota_total_exceeded",
                         f"已达总 token 上限（{used}/{total}）")
