"""模型作者（lab）。

公开数据页的「市场份额」按**模型作者**统计，不是按上游厂商。按厂商统计会把
「我们从哪几家买了额度」直接挂到公网上，而作者是模型本身的公开信息：DeepSeek
出的模型就是 DeepSeek 的，跟我们背后挂在哪家账号池上无关。

用法是给一个显示名，返回 {slug, label, color, icon}。识别不出来的落到
「其他」，icon 为空时前端用作者名首字兜底。
"""

from __future__ import annotations

OTHER = {"slug": "other", "label": "其他", "color": "#ffb900", "icon": ""}
UNKNOWN = {"slug": "unknown", "label": "未知", "color": "#ff8904", "icon": ""}

# 顺序有意义：先匹配到的算数。每个作者的色块取自同一个调色板，
# 与页面上其余图表的用色一致。
LABS = [
    {"slug": "deepseek", "label": "DeepSeek", "color": "#ed6aff",
     "icon": "deepseek", "match": ("deepseek",)},
    {"slug": "zhipu", "label": "智谱", "color": "#00d3f2",
     "icon": "zhipuai", "match": ("glm", "zhipu", "codegeex")},
    {"slug": "alibaba", "label": "阿里", "color": "#00d5be",
     "icon": "alibaba", "match": ("qwen", "qwq", "qwen3", "tongyi")},
    {"slug": "xiaomi", "label": "小米", "color": "#51a2ff",
     "icon": "xiaomi", "match": ("mimo", "xiaomi")},
    {"slug": "minimax", "label": "MiniMax", "color": "#7c86ff",
     "icon": "minimax", "match": ("minimax", "abab")},
    {"slug": "moonshot", "label": "月之暗面", "color": "#00bc7d",
     "icon": "moonshotai", "match": ("kimi", "moonshot")},
    {"slug": "stepfun", "label": "阶跃星辰", "color": "#a684ff",
     "icon": "stepfun", "match": ("step-", "stepfun", "step3")},
    {"slug": "tencent", "label": "腾讯混元", "color": "#9ae600",
     "icon": "", "match": ("hunyuan", "hy3", "hy-")},
]

BY_SLUG = {lab["slug"]: lab for lab in LABS}


def lab_of(name: str) -> dict:
    """按显示名认作者。名字是唯一线索：usage 行里没有厂商字段。"""
    low = (name or "").strip().lower()
    if not low or low in ("未知模型", "unknown"):
        return UNKNOWN
    for lab in LABS:
        for prefix in lab["match"]:
            if low.startswith(prefix):
                return lab
    return OTHER
