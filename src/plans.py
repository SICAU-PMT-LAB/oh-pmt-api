"""套餐档位（Plan）的模型化描述。

Go / Plus / Pro / Max 四档只有额度不同，其余权益一致。额度以「每日 / 每周
预算（元）」定义，请求数是把预算按每个模型的单价换算出来的**预估值**。

换算用 opencode.ai/zh/go 那套逐模型 token 画像：一轮编程对话里输入侧九成九
是缓存命中的上下文，每轮真正新增的输入只有几百到一千 token。缓存按缓存价
计价（DeepSeek 是输入价的 1/50），所以「10 元能用 1 亿 token」这种量级是
成立的；若按全部输入都是新输入来估，成本会虚高一个数量级。

额度仅供社团内部成员使用，不对任何外部用户出售，因此页面不展示金额，
只展示各档的用量规模。
"""

from __future__ import annotations

import time

import pricing

PLANS: list[dict] = [
    {"slug": "go", "name": "Go", "daily": 5.0, "weekly": 25.0,
     "tagline": "入门够用",
     "bullets": ["精选实惠的模型", "专为智能体编程调优", "够用的额度与稳定访问"]},
    {"slug": "plus", "name": "Plus", "daily": 15.0, "weekly": 75.0,
     "tagline": "日常主力",
     "bullets": ["流畅使用", "更高额度，支持更长时间的专注编码",
                 "适合更大、更复杂的项目"]},
    {"slug": "pro", "name": "Pro", "daily": 30.0, "weekly": 150.0,
     "tagline": "重度使用",
     "bullets": ["流畅使用", "并行多个代理时的充足余量",
                 "适合整仓重构与长时间任务"]},
    {"slug": "max", "name": "Max", "daily": 100.0, "weekly": 500.0,
     "tagline": "不设限地写",
     "bullets": ["流畅使用", "几乎不需要关心额度",
                 "适合团队与全天候代理"]},
]

PLAN_BY_SLUG = {p["slug"]: p for p in PLANS}

# 每个模型一轮对话的典型用量：(全新输入, 缓存命中, 输出)，单位 token。
#
# 数字取自 opencode.ai/zh/go 公开的估算口径（2026-10）。编程会话里输入侧
# 九成九都是复用的上下文（会话历史、读过的文件、系统提示），每轮真正新增
# 的输入只有几百到一千 token，缓存命中率稳定在 99% 左右。
#
# 本仓库生产库的 deepseek-v4.1-flash 实测均值为 3358 / 66521 / 613（缓存
# 95.2%），缓存率略低于 opencode 的画像，是因为其中混有大量单轮短请求；
# 长会话的缓存率才接近 99%，而配额估算是按「一轮典型编码对话」算的。
REQUEST_PROFILES: dict[str, tuple[int, int, int]] = {
    "deepseek-v4.1-flash": (410, 71300, 310),
    "deepseek-v4-flash": (410, 71300, 310),
    "deepseek-v4-flash-vision-exp": (410, 71300, 310),
    "deepseek-v4-pro": (750, 82000, 290),
    "glm-5.3-flash": (1000, 55000, 200),
    "glm-5.3": (700, 52000, 150),
    "glm-5.2": (700, 52000, 150),
    "glm-5.1": (700, 52000, 150),
    "glm-5": (700, 52000, 150),
    "glm-5v-turbo": (1000, 55000, 200),
    "kimi-k3.1-pro": (1050, 76500, 300),
    "kimi-k3": (1050, 76500, 300),
    "kimi-k2.8-preview": (870, 55000, 200),
    "kimi-k2.7-code": (870, 55000, 200),
    "kimi-k2.6": (870, 55000, 200),
    "kimi-k2.5": (870, 55000, 200),
    "minimax-m3.1-flash": (500, 49400, 360),
    "minimax-m3": (510, 56000, 190),
    "minimax-m2.7": (510, 56000, 190),
    "mimo-v2.6-pro": (790, 86000, 305),
    "mimo-v2.6-flash": (830, 71500, 295),
    "mimo-v2.5": (830, 71500, 295),
    "mimo-v2.5-pro": (790, 86000, 305),
    "hy4-preview": (830, 71500, 295),
    "hy3": (830, 71500, 295),
    "hunyuan-chat": (830, 71500, 295),
    "step-5-preview": (1000, 55000, 200),
    "qwen3.8-max": (420, 66000, 200),
    "qwen3.8-flash": (600, 58000, 200),
    "qwen3.7-plus": (500, 57000, 190),
    "qwen3.6-plus": (500, 57000, 190),
    "qwen3.5-plus": (500, 57000, 190),
    # 上游目录里有、但 opencode 没给画像的，按同族/同价位借用一组。
    "space-bunny": (830, 71500, 295),
    "muse-spark-1.3": (620, 71400, 300),
    "muse-spark-1.2": (620, 71400, 300),
    "longcat-2.0": (920, 88900, 200),
    # 境外模型的缓存复用远不如国内模型（Grok 只缓存两成多），画像差异很大，
    # 借用国内模型那一组会把成本估低一半。
    "grok-4.7": (390, 32500, 120),
    "gpt-6-luna": (1000, 50000, 220),
    "gpt-5.6-luna": (1000, 50000, 220),
    "claude-haiku-5-5": (1100, 55000, 240),
    # 内部档位名（WorkBuddy 的 fast/balanced/deep）跟各自别名走
    "fast-model": (500, 49400, 360),
    "balanced-model": (1000, 55000, 200),
    "deep-model": (700, 52000, 150),
    "auto": (1000, 55000, 200),
}

# 一个都匹配不上时的中性画像：约等于「编码代理的普通一轮」。
DEFAULT_PROFILE = (800, 65000, 250)


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


_PROFILE_BY_NORM = {_norm(k): v for k, v in REQUEST_PROFILES.items()}


def profile_for(*names: str) -> tuple[tuple[int, int, int], str]:
    """查一个模型的 token 画像，返回 (画像, 来源)。

    依次按上游 key、模型 id、显示名的归一化写法去匹配；都没有就用中性画像
    并把来源标成 `default`，前端据此在脚注里说明这几个是估的。
    """
    for name in names:
        if not name:
            continue
        hit = _PROFILE_BY_NORM.get(_norm(name))
        if hit:
            return hit, "known"
    # 退一步：显示名里含某个已知模型的关键片段（如 "Hy4 preview" 含 "hy4"）
    for name in names:
        n = _norm(name)
        if not n:
            continue
        for key, val in sorted(_PROFILE_BY_NORM.items(), key=lambda kv: -len(kv[0])):
            if key and key in n:
                return val, "known"
    return DEFAULT_PROFILE, "default"


def session_cost(price: dict, profile: tuple[int, int, int]) -> float:
    """一轮典型对话的成本（元）。

    `prompt_tokens` 按 OpenAI 的语义是**含缓存**的总输入，所以这里传的是
    全新输入 + 缓存命中的和，缓存部分再由 cost_of 按缓存价扣回来。
    """
    fresh, cached, output = profile
    return pricing.cost_of(
        price, prompt_tokens=fresh + cached,
        completion_tokens=output, cache_read_tokens=cached)


def requests_for_budget(budget: float, price: dict,
                        profile: tuple[int, int, int]) -> int | None:
    """这一档预算够跑多少次；`None` 表示单价全为 0（免费模型额度用不完）。"""
    cost = session_cost(price, profile)
    if cost <= 0:
        return None
    return int(round(budget / cost))


def tokens_per_request(profile: tuple[int, int, int]) -> int:
    return sum(profile)


def cost_per_mtok(price: dict, profile: tuple[int, int, int]) -> float:
    """按画像加权后的「每百万 token 多少钱」，用来做「10 元 1 亿」的对照。"""
    per_req = session_cost(price, profile)
    tokens = tokens_per_request(profile)
    if tokens <= 0:
        return 0.0
    return per_req / tokens * 1e6


def catalog(models: dict, overrides: dict | None = None) -> list[dict]:
    """可开的模型清单，带每档的预估请求数。

    `models` 由调用方传（server.all_models()），这里不反向依赖 server。
    查价落到 DEFAULT_PRICE 的模型照样列，但标出 `estimated`：那几行的
    请求数是按默认价估的，前端在脚注里说明。

    **不要往行里放厂商或上游 key。** 这份清单经 /api/public/plans 公开发给了
    套餐页与文档页，不需要登录，写下 provider 就等于把上游账号池的构成
    挂在公网上。
    """
    overrides = overrides or {}
    rows = []
    for mid, meta in models.items():
        name = meta.get("name") or mid
        route = (meta.get("routes") or [{}])[0]
        upstream = route.get("model") or ""
        price, kind = pricing.lookup(upstream or mid, name, mid, overrides)
        profile, profile_src = profile_for(upstream, mid, name)
        per_req = tokens_per_request(profile)
        row = {
            "id": mid,
            "name": name,
            "context": int(meta.get("context_length") or meta.get("context") or 0),
            "max_output": int(meta.get("max_output_tokens")
                              or meta.get("max_output") or 0),
            "tools": bool(meta.get("supports_tools") or meta.get("tools")),
            "images": bool(meta.get("supports_images") or meta.get("images")),
            "price": price,
            "price_source": kind,
            "estimated": kind == "default",
            "profile": {"fresh": profile[0], "cached": profile[1],
                        "output": profile[2], "source": profile_src},
            "tokens_per_request": per_req,
            "session_cost": round(session_cost(price, profile), 8),
            "cost_per_mtok": round(cost_per_mtok(price, profile), 4),
            "daily": {p["slug"]: requests_for_budget(
                p["daily"], price, profile) for p in PLANS},
            "weekly": {p["slug"]: requests_for_budget(
                p["weekly"], price, profile) for p in PLANS},
            # 每周能烧掉的 token 总量 = 每周请求数 × 每请求 token
            "weekly_tokens": {p["slug"]: _weekly_tokens(
                requests_for_budget(p["weekly"], price, profile), per_req)
                for p in PLANS},
        }
        rows.append(row)
    # 排序：额度升序，能跑的次数少的排前面。免费模型没有次数上限，排最前
    # 也是无限额度那个模型的位置——放在表尾。
    rows.sort(key=lambda r: (r["daily"]["go"] is None,
                             r["daily"]["go"] or 0))
    return rows


def _weekly_tokens(requests: int | None, per_req: int) -> int | None:
    return None if requests is None else requests * per_req


def describe(models: dict, overrides: dict | None = None) -> dict:
    """公开给前端的套餐描述。

    `plans` 带上每档的 `daily` / `weekly` 金额：这一页是给社团成员看的，
    成员能在自己的详情页看到同一组数字，藏起来反而看不懂「每周能花多少」。
    `weekly` 就是页面上「周限额」那一列每行的取值。
    """
    rows = catalog(models, overrides)
    plans = [{k: v for k, v in p.items()} for p in PLANS]
    return {
        "plans": plans,
        "models": rows,
        "assumptions": {
            "basis": "opencode",
            "window": "daily",           # 额度按天/周计，没有 5 小时窗口
            "currency": pricing.CURRENCY,
            "currency_symbol": pricing.CURRENCY_SYMBOL,
            "sample": "逐模型 token 画像取自编码代理的典型会话："
                      "输入九成九命中缓存，每轮新增输入仅数百 token",
            "generated_at": int(time.time()),
        },
    }
