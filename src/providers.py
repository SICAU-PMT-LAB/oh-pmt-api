"""账号来源厂商的注册表与适配器。

一个厂商就是一组能力：怎么用长期凭证换出可用的调用凭据、怎么列模型、
怎么发一轮对话、怎么读额度。Qoder 与 WorkBuddy 已经把完整协议实现好了
（qoder.py / workbuddy.py），这里把它们包成适配器；Cline / CommandCode
的协议还没拿到，注册成 available=False 的条目，界面按厂商分区显示
「待接入」，补协议时只需要在这里加一个类。

厂商与区域的关系：Qoder 和 WorkBuddy 都有国内版和国际版两套端点，两套
凭证互不通用，所以区域是厂商自己的能力，由各个 Provider 自己声明。
"""

from __future__ import annotations

import time

import qoder
import workbuddy


class Unsupported(Exception):
    """该厂商还没有接入协议，或者不支持某项能力。"""


class Provider:
    """厂商的基础描述。子类实现具体协议。"""

    slug = ""
    label = ""
    short = ""
    # 界面上的强调色，用于厂商卡片和标签
    color = "#8b6b2f"
    # 长期凭证的前缀校验与示例
    credential_prefix = ""
    credential_hint = ""
    credential_url = ""
    # 是否已经能用（有真实协议实现）
    available = False
    # 该厂商支持的区域；第一个是默认值
    regions: dict[str, dict] = {}
    # 是否支持每日签到领额度
    supports_checkin = False

    # ---- 协议能力 ----

    def connect(self, credential: str, region: str) -> dict:
        """校验长期凭证并返回账号身份 {uid, email, name}。

        同时把换来的短期凭据写回账号，由调用方落库。
        """
        raise Unsupported(f"{self.label} 还没有接入协议")

    def refresh(self, account: dict) -> dict | None:
        """续期短期凭据。返回 {access_token, refresh_token, expires_in} 或 None。"""
        raise Unsupported(f"{self.label} 还没有接入协议")

    def list_models(self, account: dict) -> list[dict]:
        raise Unsupported(f"{self.label} 还没有接入协议")

    def chat_stream(self, account: dict, body: bytes, model: str):
        raise Unsupported(f"{self.label} 还没有接入协议")

    def build_chat_body(self, messages: list[dict], model: str, *,
                        stream: bool = True, tools=None,
                        parameters=None, meta=None) -> bytes:
        raise Unsupported(f"{self.label} 还没有接入协议")

    def fetch_quota(self, account: dict) -> dict:
        """额度快照。不支持的厂商返回空快照，界面显示「不统计额度」。"""
        raise Unsupported(f"{self.label} 不提供额度查询")

    def checkin(self, account: dict) -> dict:
        raise Unsupported(f"{self.label} 没有签到活动")

    # ---- 辅助 ----

    def region_label(self, region: str) -> str:
        return (self.regions.get(region) or {}).get("label") or region

    def describe(self) -> dict:
        return {
            "slug": self.slug,
            "label": self.label,
            "short": self.short or self.label,
            "color": self.color,
            "available": self.available,
            "credential_prefix": self.credential_prefix,
            "credential_hint": self.credential_hint,
            "credential_url": self.credential_url,
            "supports_checkin": self.supports_checkin,
            "regions": [{"id": rid, "label": self.region_label(rid)}
                        for rid in self.regions],
        }


class QoderProvider(Provider):
    slug = "qoder"
    label = "Qoder"
    short = "Qoder"
    color = "#8b6b2f"
    credential_prefix = "pt-"
    credential_hint = "PAT，以 pt- 开头"
    credential_url = "https://qoder.com/account/integrations"
    available = True
    supports_checkin = True
    regions = {rid: {"label": info["label"]} for rid, info in qoder.REGIONS.items()}

    def connect(self, credential: str, region: str) -> dict:
        ex = qoder.exchange_pat(credential, region)
        info = qoder.fetch_userinfo(ex["access_token"], region)
        return {
            "uid": info["uid"], "email": info["email"], "name": info["name"],
            "access_token": ex["access_token"],
            "refresh_token": ex["refresh_token"],
            "expires_in": ex.get("expires_in") or 86400,
        }

    def refresh(self, account: dict) -> dict | None:
        token = account.get("refresh_token") or ""
        if not token:
            return None
        new = qoder.refresh_token(token, account["region"])
        if not new.get("access_token"):
            return None
        return new

    def list_models(self, account: dict) -> list[dict]:
        return qoder.list_models(account)

    def chat_stream(self, account: dict, body: bytes, model: str):
        return qoder.chat_stream(account, body, model)

    def build_chat_body(self, messages, model, *, stream=True, tools=None,
                        parameters=None, meta=None):
        return qoder.build_chat_body(messages, model, stream=stream,
                                     tools=tools, parameters=parameters,
                                     meta=meta)

    def fetch_quota(self, account: dict) -> dict:
        return qoder.fetch_quota(account)

    def checkin(self, account: dict) -> dict:
        return qoder.checkin_account(account)


class WorkbuddyProvider(Provider):
    """WorkBuddy（腾讯 CodeBuddy）适配器。

    与 Qoder 不同，凭证不是一个短 token，而是官方面板导出的 JSON 文件
    （workbuddy-<uid>.json 或数组形式的 cockpit-<uid>.json）。文件里带
    access / refresh token，刷新走 /v2/plugin/auth/token/refresh。
    """

    slug = "workbuddy"
    label = "WorkBuddy"
    short = "WorkBuddy"
    color = "#2f6b6b"
    credential_prefix = ""
    credential_hint = "workbuddy-<uid>.json 或 cockpit-<uid>.json 的内容"
    credential_url = ""
    available = True
    supports_checkin = True
    regions = {rid: {"label": info["label"]}
               for rid, info in workbuddy.REGIONS.items()}

    def connect(self, credential: str, region: str) -> dict:
        info = workbuddy.parse_credential(credential)
        # 文件里带了 domain 就以它为准：导出文件的区域比界面上选的可信
        want = workbuddy.detect_region(info.get("domain"), region) if info.get("domain") else region
        account = {"uid": info["uid"], "region": want,
                   "access_token": info["access_token"], "domain": info.get("domain")}
        if info.get("access_token"):
            # 先直接拿这个 token 查额度，能查到说明它还有效，不用刷新
            try:
                workbuddy.fetch_quota(account)
            except workbuddy.UpstreamError:
                info["access_token"] = ""
        if not info.get("access_token"):
            if not info.get("refresh_token"):
                raise workbuddy.UpstreamError(0, "", "凭证已失效，请重新导出")
            fresh = workbuddy.refresh({**account, "refresh_token": info["refresh_token"]})
            if not fresh:
                raise workbuddy.UpstreamError(0, "", "凭证已失效，请重新导出")
            info["access_token"] = fresh["access_token"]
            info["refresh_token"] = fresh["refresh_token"]
        expires_in = 2592000
        if info.get("expires_at"):
            expires_in = max(60, int(info["expires_at"] - time.time()))
        return {
            "uid": info["uid"],
            "email": "", "name": info.get("nickname") or info["uid"][:8],
            "region": want,
            "access_token": info["access_token"],
            "refresh_token": info.get("refresh_token") or "",
            "expires_in": expires_in,
            "domain": info.get("domain") or "",
        }

    def refresh(self, account: dict) -> dict | None:
        return workbuddy.refresh(account)

    def list_models(self, account: dict) -> list[dict]:
        return workbuddy.list_models(account)

    def chat_stream(self, account: dict, body: bytes, model: str):
        return workbuddy.chat_stream(account, body, model)

    def build_chat_body(self, messages, model, *, stream=True, tools=None,
                        parameters=None, meta=None):
        return workbuddy.build_chat_body(messages, model, stream=stream,
                                         tools=tools, parameters=parameters,
                                         meta=meta)

    def fetch_quota(self, account: dict) -> dict:
        return workbuddy.fetch_quota(account)

    def checkin(self, account: dict) -> dict:
        return workbuddy.checkin(account)


class PendingProvider(Provider):
    """协议还没接入的厂商。占位，让界面能按厂商分区并给出接入指引。"""

    available = False
    regions = {"default": {"label": "默认"}}

    def connect(self, credential: str, region: str) -> dict:
        raise Unsupported(
            f"{self.label} 的接入协议尚未实现。账号表已按厂商分开存储，"
            f"补上 {self.slug} 的适配器即可启用这一类账号。")

    def list_models(self, account: dict) -> list[dict]:
        return []


class ClineProvider(PendingProvider):
    slug = "cline"
    label = "Cline"
    color = "#6b4a8f"
    credential_hint = "API Key"
    credential_url = "https://cline.bot/"


class CommandcodeProvider(PendingProvider):
    slug = "commandcode"
    label = "CommandCode"
    color = "#2e5aa8"
    credential_hint = "API Key"
    credential_url = "https://commandcode.ai/docs/provider"


PROVIDERS: dict[str, Provider] = {
    p.slug: p for p in (
        QoderProvider(),
        WorkbuddyProvider(),
        ClineProvider(),
        CommandcodeProvider(),
    )
}

DEFAULT_PROVIDER = "qoder"


def get(slug: str | None) -> Provider:
    """取厂商适配器。未知 slug 返回默认厂商，避免老数据把页面打挂。"""
    return PROVIDERS.get((slug or "").strip()) or PROVIDERS[DEFAULT_PROVIDER]


def describe_all() -> list[dict]:
    return [p.describe() for p in PROVIDERS.values()]


def is_available(slug: str | None) -> bool:
    return get(slug).available
