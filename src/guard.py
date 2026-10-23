"""访问控制：IP 白名单、模型白名单、积分限额。"""

from __future__ import annotations

import ipaddress

from store import day_start, week_start


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
            try:
                nets.append(ipaddress.ip_network(
                    ipaddress.ip_address(part).exploded
                    + ("/128" if ":" in part else "/32"),
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
        return
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise Denied(403, "ip_invalid", f"无法识别的来源地址: {ip}")
    for n in nets:
        if n.version == addr.version and addr in n:
            return
    raise Denied(403, "ip_forbidden",
                 f"来源地址 {ip} 不在该 API Key 的允许列表内")


def allowed_models(key: dict, member: dict | None) -> set[str]:
    """有效模型白名单 = Key 的白名单 ∩ 成员的白名单。

    两者都为空表示不限制。任一为空时取另一个，都非空时取交集。
    """
    key_spec = (key.get("model_allowlist") or "").strip()
    member_spec = ((member or {}).get("model_allowlist") or "").strip()
    key_set = {m.strip() for m in key_spec.split(",") if m.strip()} if key_spec else None
    member_set = ({m.strip() for m in member_spec.split(",") if m.strip()}
                  if member_spec else None)
    if key_set and member_set:
        return key_set & member_set
    return key_set if key_set else (member_set or set())


def check_model(key: dict, member: dict | None, model: str):
    allowed = allowed_models(key, member)
    if not allowed:
        return
    if model in allowed:
        return
    raise Denied(403, "model_forbidden",
                 f"该密钥不允许使用模型 {model}，可用：{', '.join(sorted(allowed))}")


def quota_status(store, member: dict) -> dict:
    """成员当前用量与限额，供拦截判断和界面展示。"""
    mid = member["id"]
    daily_limit = float(member.get("limit_daily") or 0)
    weekly_limit = float(member.get("limit_weekly") or 0)
    used_today = store.credits_since(mid, day_start())
    used_week = store.credits_since(mid, week_start())
    return {
        "limit_daily": daily_limit,
        "limit_weekly": weekly_limit,
        "used_today": round(used_today, 4),
        "used_week": round(used_week, 4),
        "left_today": round(max(0.0, daily_limit - used_today), 4) if daily_limit else None,
        "left_week": round(max(0.0, weekly_limit - used_week), 4) if weekly_limit else None,
    }


def check_quota(store, member: dict | None):
    """成员积分限额。0 表示不限。"""
    if not member:
        return
    st = quota_status(store, member)
    if st["limit_daily"] > 0 and st["used_today"] >= st["limit_daily"]:
        raise Denied(
            429, "quota_daily_exceeded",
            f"今日积分已用完（{st['used_today']:.2f}/{st['limit_daily']:.2f}）",
        )
    if st["limit_weekly"] > 0 and st["used_week"] >= st["limit_weekly"]:
        raise Denied(
            429, "quota_weekly_exceeded",
            f"本周积分已用完（{st['used_week']:.2f}/{st['limit_weekly']:.2f}）",
        )
