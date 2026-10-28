"""按 token 计价。

每个模型有一张 per 1M tokens 的四列价目表（input / output / cache read /
cache write），一次请求的成本就是把各类 token 乘各自单价再除以 100 万。
成员的额度也用同一币种表示，百分比 = 已花 / 额度，到 100% 就拒绝服务。

账本只追加：cost 在请求结束那一刻按当时的价目写成定值，之后改价、改上游
路由都不会回头改已写下的行。历史记录仍然是当时那份价目算出来的数。

单价用人民币，数字抄自各家**官方中文定价页**（2026-10 抓取）：DeepSeek、
智谱、Moonshot、MiniMax、阿里百炼、小米 MiMo、腾讯混元、阶跃星辰都有
自己的「元/百万 tokens」报价，比拿美元价乘汇率更贴近实际账单。

价格表分两层：这里的内置表是默认值，管理员在「定价」页改过的值存在
settings 里覆盖内置表。查价按上游 key → 对外模型名 → 显示名 的顺序，
都查不到时用 DEFAULT_PRICE 并在界面上标明是默认价。
"""

from __future__ import annotations

# 单价单位：元 / 1M tokens
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
CURRENCY = "CNY"
CURRENCY_SYMBOL = "¥"

# 内置默认价，数字来自各家官方中文定价页（2026-10 抓取），单位人民币元/百万 tokens。
# 官方分档的模型取最低档（最常见的短输入场景），阶梯规则写在 note 里。
# cache_write 为 0 表示该模型不单独收缓存写入费（多数国内模型如此）。
BUILTIN: dict[str, dict[str, float]] = {
    # --- DeepSeek 官方 ---
    # 与生产库 settings.model_prices 的覆盖值保持一致，否则本地新建的库会
    # 读到旧的 1.5/4.5/0.05，计费比线上贵一半。
    "deepseek-v4.1-flash": {"input": 1.0, "output": 4.0, "cache_read": 0.02},
    "deepseek-v4-flash": {"input": 1.0, "output": 4.0, "cache_read": 0.02},
    "deepseek-v4-flash-vision-exp": {"input": 1.0, "output": 4.0, "cache_read": 0.02},
    "deepseek-v4-pro": {"input": 4.5, "output": 13.5, "cache_read": 0.15},
    "deepseek-v3-2-volc": {"input": 1.0, "output": 4.0, "cache_read": 0.02},
    # --- 智谱 GLM 官方 ---
    "glm-5.3-flash": {"input": 0.8, "output": 2.8, "cache_read": 0.23},
    "glm-5.3": {"input": 8.0, "output": 28.0, "cache_read": 2.0},
    "glm-5.2": {"input": 8.0, "output": 28.0, "cache_read": 2.0},
    "glm-5.1": {"input": 6.0, "output": 24.0, "cache_read": 1.3},
    "glm-5": {"input": 4.0, "output": 18.0, "cache_read": 1.0},
    "glm-5v-turbo": {"input": 5.0, "output": 22.0, "cache_read": 1.2},
    # --- Moonshot Kimi 官方 ---
    "kimi-k3.1-pro": {"input": 13.0, "output": 68.0, "cache_read": 1.3,
                      "cache_write": 13.0},
    # K3 与 K3.1-Pro 是两个价档（K3 是前代大旗舰），WorkBuddy 的 kimi-k3-1
    # 显示名也是 Kimi-K3，所以它只能走这一行，别在这里再抄一份 K3.1 的价。
    "kimi-k3": {"input": 20.0, "output": 100.0, "cache_read": 2.0},
    "kimi-k2.7-code": {"input": 6.5, "output": 27.0, "cache_read": 1.3},
    "kimi-k2.8-preview": {"input": 1.0, "output": 4.0, "cache_read": 0.25},
    "kimi-k2.6": {"input": 6.5, "output": 27.0, "cache_read": 1.3},
    "kimi-k2.5": {"input": 6.5, "output": 27.0, "cache_read": 1.3},
    # --- MiniMax 官方：标准档，M3 取 ≤512k 的永久五折价 ---
    "minimax-m3": {"input": 2.1, "output": 8.4, "cache_read": 0.42},
    "minimax-m3.1-flash": {"input": 1.0, "output": 4.0, "cache_read": 0.1},
    "minimax-m2.7": {"input": 2.1, "output": 8.4, "cache_read": 0.42},
    # --- 阿里通义千问官方（华北 2 北京）---
    "qwen3.8-max": {"input": 12.0, "output": 36.0, "cache_read": 1.5,
                    "cache_write": 15.0},
    "qwen3.8-flash": {"input": 0.8, "output": 2.7, "cache_read": 0.16},
    "qwen3.7-max": {"input": 12.0, "output": 36.0, "cache_read": 1.5},
    "qwen3.7-plus": {"input": 2.0, "output": 8.0, "cache_read": 0.4},
    "qwen3.6-plus": {"input": 2.0, "output": 8.0, "cache_read": 0.4},
    "qwen3.5-plus": {"input": 2.0, "output": 8.0, "cache_read": 0.4},
    # --- 小米 MiMo 官方（国内定价）---
    "mimo-v2.6-pro": {"input": 3.0, "output": 6.0, "cache_read": 0.03},
    "mimo-v2.6-flash": {"input": 1.0, "output": 2.0, "cache_read": 0.02},
    "mimo-v2.5": {"input": 1.0, "output": 2.0, "cache_read": 0.02},
    "mimo-v2.5-pro": {"input": 3.0, "output": 6.0, "cache_read": 0.025},
    # --- 腾讯混元官方（TokenHub 广州）---
    "hy3": {"input": 1.0, "output": 4.0, "cache_read": 0.25},
    "hy4-preview": {"input": 6.0, "output": 18.0, "cache_read": 0.3},
    "hunyuan-chat": {"input": 1.0, "output": 4.0, "cache_read": 0.25},
    # --- 阶跃星辰官方 ---
    "step-5-preview": {"input": 7.0, "output": 20.0, "cache_read": 0.35},
    "space-bunny": {"input": 1.0, "output": 4.0, "cache_read": 0.2},
    # --- 其他 ---
    "muse-spark-1.3": {"input": 0.7, "output": 1.4, "cache_read": 0.014},
    # 以下几家没有人民币官方价，按 7 元/美元的固定汇率折算自其美元价，
    # 只作为估算口径；真接入这些上游时按实际账单在定价页改。
    "grok-4.7": {"input": 14.0, "output": 42.0, "cache_read": 3.5},
    "grok-4.6": {"input": 14.0, "output": 42.0, "cache_read": 3.5},
    "claude-haiku-5-5": {"input": 0.7, "output": 3.5, "cache_read": 0.07,
                         "cache_write": 0.875},
    "gpt-6-luna": {"input": 0.7, "output": 3.5, "cache_read": 0.07,
                   "cache_write": 0.875},
    "gpt-5.6-luna": {"input": 1.4, "output": 8.4, "cache_read": 0.14,
                     "cache_write": 1.75},
}

# 查不到价时用的保守默认值：取国内中档模型的价位，宁可高估不要漏账。
# 约合每百万输入 3 元、输出 10 元，对应 GLM-5.3-Flash / MiniMax-M3 一带。
DEFAULT_PRICE: dict[str, float] = {
    "input": 3.0, "output": 10.0, "cache_read": 0.3, "cache_write": 3.0,
}

# Qoder 的上游 key 与 opencode 的模型 id 不是一套写法，这里手工对齐。
# 未列出的 key 会退到显示名匹配，再退到默认价。
ALIASES: dict[str, str] = {
    "qmodel_38max": "qwen3.8-max",
    "qfmodel": "qwen3.8-flash",
    "kmodel_latest": "kimi-k3",
    "kmodel": "kimi-k2.8-preview",
    # WorkBuddy 的上游 key 与 K3 的显示名同款，直接按 key 查价时也要对到 K3
    "kimi-k3-1": "kimi-k3",
    "kimi-k3.1-pro-highspeed": "kimi-k3.1-pro",
    "dfmodel": "deepseek-v4.1-flash",
    # WorkBuddy 的三个通用档位没有公开价，按定位映射到相近的模型
    "fast-model": "minimax-m3.1-flash",
    "balanced-model": "glm-5.3-flash",
    "deep-model": "glm-5.3",
}


# 已下架的模型：仍能调也不能再对外发。
#
# 这些模型上游还挂着（个别甚至还能跑通），但同族已有更新的一代，留着只会
# 让客户端选错、让定价表变成一堆没人用的行。这里按归一化后的写法匹配，
# 所以 `deepseek-v4-flash`、`DeepSeek-V4-Flash`、`deepseek 4 flash` 都算同一个。
#
# 只影响「能不能再发起新请求」：历史 usage 行里的模型名不变，成员详情页
# 照旧能查。要恢复某个模型，从这张表里删掉它一行即可。
RETIRED: tuple[str, ...] = (
    # WorkBuddy 的三个自定义档位（快速/均衡/极致），语义模糊、无法定价。
    # 显示名也列上：目录里筛掉之后客户端再拿中文名字请求，也要能认出是下架。
    "fast-model", "balanced-model", "deep-model",
    "workbuddy 快速", "workbuddy 均衡", "workbuddy 极致",
    # Qoder 的自定义档位：同样说不清背后是什么模型。
    # "Fast" 与 WorkBuddy 的 fast-model 不是同一个东西，但都要下架，所以两行都写。
    "Auto", "Ultimate", "Efficient", "Fast", "Sonus", "Cantus",
    # 同族已有新版的旧模型。带点号的写法要单独写一行：_squash 会把点去掉，
    # "V3.2" 归一后是 v32，跟上游 key 的 v3-2-volc 不是一个串。
    "deepseek-v3-2-volc", "deepseek-v3.2",     # DeepSeek-V3.2
    "deepseek-v4-flash",      # V4.1-Flash 已是替代品
    "deepseek-v4-flash-vision-exp",
    "glm-5.2", "glm-5.1", "glm-5", "glm-5v-turbo",
    "kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code",
    "minimax-m2.7", "minimax-m3",
    "hunyuan-chat",           # 已被 Hy3 取代
    "qwen3.5-plus", "qwen3.6-plus", "qwen3.7-plus", "qwen3.7-max",
)

def _squash(value: str) -> str:
    """归一化模型名：忽略大小写、空格、连字符和点。

    上游的 key（glm-5.3-flash）与显示名（GLM-5.3-Flash）归一后相同，
    所以一张表就能同时按两种写法命中。
    """
    return "".join(ch for ch in str(value or "").lower()
                   if ch not in " -_.:/\t")


_RETIRED_INDEX = {_squash(k) for k in RETIRED}


def retired(*names: str) -> bool:
    """这些名字里有没有已被下架的。

    传多个名字是因为一个对外模型背后可能有好几家上游的 key，任何一个命中
    下架名单就整条下架：客户端看到的是一个模型名，不能“这家能跑那家不能”。
    """
    return any(_squash(n) in _RETIRED_INDEX for n in names if n)


def _norm(price: dict | None) -> dict[str, float]:
    """把一条价目补全成四个单价字段，缺的按 0 算（不单独收缓存费）。"""
    out = {f: 0.0 for f in PRICE_FIELDS}
    for f in PRICE_FIELDS:
        try:
            out[f] = float((price or {}).get(f) or 0)
        except (TypeError, ValueError):
            out[f] = 0.0
    return out


def _index(prices: dict) -> dict[str, dict[str, float]]:
    """建归一化索引。归一化后同名时以先出现的为准。"""
    out: dict[str, dict[str, float]] = {}
    for key, price in prices.items():
        out.setdefault(_squash(key), _norm(price))
    return out


_BUILTIN_INDEX = _index(BUILTIN)
_ALIAS_INDEX = {_squash(k): v for k, v in ALIASES.items()}


def normalize_overrides(raw) -> dict[str, dict[str, float]]:
    """settings 里存的覆盖值，清洗成 {key: 四字段价目}。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, price in raw.items():
        if not isinstance(price, dict):
            continue
        clean = {f: float(price[f]) for f in PRICE_FIELDS
                 if isinstance(price.get(f), (int, float))}
        if clean:
            out[str(key)] = clean
    return out


def _base_price(names: list[str]) -> tuple[dict[str, float], str]:
    """在候选名里找内置价，找不到用默认价。"""
    for name in names:
        key = _squash(name)
        if key and key in _BUILTIN_INDEX:
            return dict(_BUILTIN_INDEX[key]), "builtin"
    return dict(DEFAULT_PRICE), "default"


def lookup(upstream_key: str, display_name: str = "", model_id: str = "",
           overrides: dict | None = None) -> tuple[dict[str, float], str]:
    """查这个模型的价格，返回 (价目, 来源)。

    来源是 `override` / `builtin` / `default`，界面用它标出哪些是估算值。
    顺序：覆盖表 → 内置表 → 别名 → 显示名/对外 id → 默认价。

    覆盖表按整条替换：写了哪几个单价就只按这几个算，没写的当 0（管理员要
    对某个模型完全自定义时不必抄全部字段）。
    """
    overrides = overrides or {}
    override_index = _index(overrides)
    primary = [upstream_key, model_id, display_name]
    alias = ALIASES.get(str(upstream_key or "").strip())
    secondary = [alias] if alias else []

    for group in (primary, secondary):
        for name in group:
            key = _squash(name)
            if key and key in override_index:
                return dict(override_index[key]), "override"
        for name in group:
            key = _squash(name)
            if key and key in _BUILTIN_INDEX:
                return dict(_BUILTIN_INDEX[key]), "builtin"
    return _base_price(primary + secondary)


def cost_of(price: dict, *, prompt_tokens: int = 0, completion_tokens: int = 0,
            cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    """一次请求的成本（元）。

    口径与 OpenAI 兼容上游的 usage 语义对齐：
      - `prompt_tokens` 已经包含命中缓存的部分，要先扣掉再按 input 价算，
        否则缓存 token 会被按原价再收一遍；
      - `completion_tokens` 已经包含思考 token，不再额外加。

    上游没报缓存明细时 cache_* 是 0，全部按 input 价算，属于高估，
    比漏账好。
    """
    p = _norm(price)
    prompt = max(0, int(prompt_tokens or 0))
    cache_read = min(max(0, int(cache_read_tokens or 0)), prompt)
    cache_write = min(max(0, int(cache_write_tokens or 0)), prompt - cache_read)
    fresh = prompt - cache_read - cache_write
    total = (fresh * p["input"]
             + cache_read * p["cache_read"]
             + cache_write * p["cache_write"]
             + max(0, int(completion_tokens or 0)) * p["output"])
    return total / 1_000_000.0
