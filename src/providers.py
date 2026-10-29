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
import welfare
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
    # 凭证是否由「api-key + base-url」两段组成。只有这类厂商需要额外传地址，
    # 其余厂商的 connect 保持两参数，不为了统一签名给它们加没用的形参。
    needs_base_url = False
    # 是否支持每日签到领额度
    supports_checkin = False
    # 是否需要把客户端原样的 thinking / reasoning 对象转给上游。
    # 直通型上游（WorkBuddy）需要，因为 {"type": "enabled"} 这类写法
    # 本身就是一个档位信息；Qoder 的参数表是封闭的，多塞键会被拒。
    wants_effort_objects = False
    # 列模型目录是否需要一个能连上的账号。Qoder 要拿 job token 再请求接口，
    # 所以没账号就读不到；WorkBuddy 的 CATALOG 是本地静态表，与账号无关。
    # 这一项让成员能勾选模型时不依赖账号是否在冷却或刚接入未同步。
    catalog_needs_account = True
    # 上游的计费单位：credit = 积分（Qoder / WorkBuddy 按积分扣额度），
    # token = 按 token 计费。成员页按账号统计消耗时以它决定记哪一种，
    # 积分账号的 token 数只是过程量，不能和 token 账号的 token 相加。
    billing_unit = "token"
    # 账号池的分流优先级：数字小的先挑。这个顺序决定「成员的优先账号用不了
    # 时先去哪一类账号」，是运维策略而不是协议事实，所以集中在这里声明，
    # 调整档位只改这张表。
    #
    # 它和 PROVIDERS 的字典顺序是两件事：那个顺序是路由顺序（客户端没设
    # 优先账号、几家都能跑同一个模型时先试谁），这个是账号池里的先后。
    #
    # 默认 2 是「其他渠道」那一档：workbuddy 国内(1) 之下、workbuddy 国际(3)
    # 之上。新接一家厂商不写 priority 就落在这里，正好是用户要的位置。
    priority = 2
    # 厂商内部的区域档位：数字小的先挑。没列到的区域取 priority+1，
    # 新增区域不会意外抢到主力位置。
    region_priority: dict[str, int] = {}

    # ---- 协议能力 ----

    def connect(self, credential: str, region: str, base_url: str = "") -> dict:
        """校验长期凭证并返回账号身份 {uid, email, name}。

        同时把换来的短期凭据写回账号，由调用方落库。

        base_url 只有 needs_base_url 的厂商用得到（福利账号是 api-key +
        地址两段凭证）；其余厂商的实现保持两参数，不为了统一签名加没用
        的形参。
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

    def normalize_effort(self, effort, meta: dict | None = None) -> str | None:
        """把客户端要的思考档位对齐到该厂商上游认的写法。

        默认按 Qoder 的规则裁剪（它只在模型声明了档位集合时才下发）。
        WorkBuddy 这类直通上游的实现覆写它：上游对不认识的档位不报错，
        裁剪反而会让客户端的设置悄悄失效。
        """
        return qoder.normalize_effort(effort, meta)

    def fetch_quota(self, account: dict) -> dict:
        """额度快照。不支持的厂商返回空快照，界面显示「不统计额度」。"""
        raise Unsupported(f"{self.label} 不提供额度查询")

    def checkin(self, account: dict) -> dict:
        raise Unsupported(f"{self.label} 没有签到活动")

    # ---- 辅助 ----

    def region_label(self, region: str) -> str:
        return (self.regions.get(region) or {}).get("label") or region

    def rank(self, region: str = "") -> tuple[int, int]:
        """账号池的挑选优先次序，数字小的先挑。

        两个数：厂商档位、区域档位。成员没绑优先账号、或绑定的账号都不可用
        时，账号池按这个次序一档一档找下去，而不是拿所有厂商的账号混在一起
        按最近使用时间轮询。混在一起轮询的问题是：一个能正常跑的主力账号，
        只要最近被用过，就会被排到额度紧张的备用账号后面。

        顺序（数字小的优先）：
            workbuddy 国内 → 其他厂商 → workbuddy 国际 → 福利 → qoder 国内 → qoder 国际
        """
        return (self.priority,
                self.region_priority.get(region, self.priority + 1))

    def describe(self) -> dict:
        return {
            "slug": self.slug,
            "label": self.label,
            "short": self.short or self.label,
            "color": self.color,
            "available": self.available,
            # 账号池档位：数字小的先被分配流量，界面按它排厂商卡片
            "pool_rank": self.priority,
            "credential_prefix": self.credential_prefix,
            "credential_hint": self.credential_hint,
            "credential_url": self.credential_url,
            "supports_checkin": self.supports_checkin,
            "billing_unit": self.billing_unit,
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
    billing_unit = "credit"
    regions = {rid: {"label": info["label"]} for rid, info in qoder.REGIONS.items()}
    # Qoder 额度最紧张、失败率最高，兜底用，能不碰就不碰
    priority = 6
    region_priority = {"cn": 5, "global": 7}

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
    billing_unit = "credit"
    wants_effort_objects = True
    # CATALOG 是本地静态表，没有账号也能列，不依赖账号是否可用
    catalog_needs_account = False
    regions = {rid: {"label": info["label"]}
               for rid, info in workbuddy.REGIONS.items()}
    # 国内版是主力：同一家厂商里先挑国内版账号，国际版只在它顶不住时补位
    region_priority = {"cn": 1, "global": 3}

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

    def normalize_effort(self, effort, meta=None):
        # 上游自己认 reasoning_effort，不认识的档位静默忽略，不需要本地裁剪
        return effort or None

    def fetch_quota(self, account: dict) -> dict:
        return workbuddy.fetch_quota(account)

    def checkin(self, account: dict) -> dict:
        return workbuddy.checkin(account)


class WelfareProvider(Provider):
    """福利账号：任意 OpenAI 兼容上游，凭 api-key + base-url 接入。

    与另外两家的两点不同：
    - 凭证是「api-key + base-url」两段，不是一个打包好的字符串。base-url
      存在 accounts.base_url，api-key 存在 credential。
    - 额度是 ∞。请求照常计量，但计费按 0 元，不占成员档位百分比。
      判断依据是账号的 welfare 标记而非厂商名。
    """

    slug = "welfare"
    label = "福利账号"
    short = "福利"
    color = "#8f5a2f"
    credential_prefix = ""
    credential_hint = "API Key（支持 base64），配合 Base URL 使用"
    credential_url = ""
    available = True
    supports_checkin = False
    billing_unit = "token"
    needs_base_url = True
    # 模型清单要问上游，没有账号读不到
    catalog_needs_account = True
    regions = {"default": {"label": "默认"}}
    # 额度免费但来源临时、随时可能失效，排在正常付费账号之后
    priority = 4

    def connect(self, credential: str, region: str, base_url: str = "") -> dict:
        """校验凭证：拉一次模型列表，能拉通才算接得上。"""
        key = welfare.normalize_key(credential)
        base = welfare.normalize_base_url(base_url)
        if not key:
            raise welfare.UpstreamError(0, "", "请填写 API Key")
        if not base:
            raise welfare.UpstreamError(0, "", "请填写 Base URL")
        account = {"api_key": key, "base_url": base}
        models = welfare.list_models(account)
        if not models:
            raise welfare.UpstreamError(0, "", "这个地址没有返回任何可用模型")
        # uid 取 base-url 的摘要：同一上游重复导入应当覆盖而不是新增一条
        import hashlib
        uid = hashlib.sha256(base.encode()).hexdigest()[:16]
        return {
            "uid": uid, "email": "", "name": base,
            "region": "default",
            "access_token": key, "refresh_token": "", "expires_in": 0,
            "base_url": base,
        }

    def refresh(self, account: dict) -> dict | None:
        # API Key 不过期，没有续期这回事
        return None

    def list_models(self, account: dict) -> list[dict]:
        """只返回管理员当初勾选的模型。

        上游 /models 里会列出这个 key 用不了的条目（没开通、欠费），
        全部灌进目录会让成员看到一堆选了就失败的模型。导入时探测过一遍、
        管理员也现场挑过了，那份名单才是对外承诺。
        """
        picked = [x.strip() for x in
                  (account.get("welfare_models") or "").split(",") if x.strip()]
        if not picked:
            return []
        return [{"id": mid, "name": mid} for mid in picked]

    def list_models_all(self, account: dict) -> list[dict]:
        """上游报的全部模型，只在导入探测时用。"""
        return welfare.list_models(account)

    def probe_model(self, account: dict, model: str) -> tuple[bool, str]:
        return welfare.probe_model(account, model)

    def chat_stream(self, account: dict, body: bytes, model: str):
        return welfare.chat_stream(account, body, model)

    def build_chat_body(self, messages, model, *, stream=True, tools=None,
                        parameters=None, meta=None):
        return welfare.build_chat_body(messages, model, stream=stream,
                                       tools=tools, parameters=parameters,
                                       meta=meta)

    def normalize_effort(self, effort, meta=None):
        # 直通上游，档位原样交给它
        return effort or None


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
        WelfareProvider(),
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


def billing_unit(slug: str | None) -> str:
    """账号所属厂商的计费口径。

    不直接用 get()：它对未知 slug 回退到默认厂商，会把已删除账号或空
    provider 误判成积分。这里未知一律按 token 算，宁可少标一个积分。
    """
    prov = PROVIDERS.get((slug or "").strip())
    return prov.billing_unit if prov else "token"
