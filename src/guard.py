"""访问控制：IP 白名单、模型白名单、成本限额。"""

from __future__ import annotations

import ipaddress
import time

from store import day_end, day_start
import plans


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
    """取真实客户端 IP。只有信任反代时才读 X-Forwarded-For。

    XFF 取**最右**一段，不是最左。nginx 的 $proxy_add_x_forwarded_for 拼成
    「调用方自带的值, $remote_addr」，真实来源在最后。取最左的话，调用方只要
    自己发一个 X-Forwarded-For 就能决定被当成谁：IP 白名单可以随便糊弄过去，
    登录失败限速也能靠每次换个前缀无限刷新。
    """
    if trust_forwarded:
        xff = handler.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.rsplit(",", 1)[-1].strip()
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


def allowed_models(key: dict, member: dict | None) -> set[str] | None:
    """有效模型白名单 = Key 的白名单 ∩ 成员的白名单。

    返回 None 表示两边都没限制，返回集合（包括空集）表示只允许集合里的
    写法。空集是「一个都不许」，不能退回「不限」——否则成员被限制了一组
    模型、密钥又限制了另一组，两边不重叠时反而全放开。

    以前用空集兼表「不限」和「一个都不许」，route_allowed 看到空集就当作
    不限，于是不相交的 Key 与成员组合能调用任意模型。所以这里必须把
    两种含义分开。
    """
    key_spec = (key.get("model_allowlist") or "").strip()
    member_spec = ((member or {}).get("model_allowlist") or "").strip()
    key_set = {m.strip() for m in key_spec.split(",") if m.strip()} if key_spec else None
    member_set = ({m.strip() for m in member_spec.split(",") if m.strip()}
                  if member_spec else None)
    if key_set is not None and member_set is not None:
        return key_set & member_set
    return key_set if key_set is not None else member_set


def model_sources(model: str, catalog: dict | None = None) -> list[dict]:
    """一个模型对外只有一个名字，背后可能有好几家上游能跑。

    返回每一家来源，附带界面与白名单共用的 spec 写法
    `厂商:上游key`。管理端按这个列表逐条勾选，网关按同一种写法判断。
    历史数据里存的是裸上游 key（dfmodel），所以 spec 之外的裸写法也要认。
    """
    entry = (catalog or {}).get(model) or {}
    out = []
    for route in entry.get("routes") or []:
        out.append({
            "provider": route["provider"],
            "region": route.get("region", ""),
            "upstream_key": route["model"],
            "spec": f"{route['provider']}:{route['model']}",
        })
    return out


def model_names(model: str, catalog: dict | None = None) -> set[str]:
    """一个模型所有能拿来写白名单的名字（小写）。

    合并后的内部 id、上游给的显示名、改名前的旧名都算——客户端和旧数据
    里写哪一个都应该能认出来。
    """
    entry = (catalog or {}).get(model) or {}
    names = {model.lower()}
    for value in (entry.get("name"), *(entry.get("aliases") or [])):
        if value:
            names.add(str(value).strip().lower())
    return names


def route_allowed(allowed: set[str] | None, route: dict, names: set[str]) -> bool:
    """这条 (厂商, 上游 key) 来源是否被白名单允许。

    `allowed` 为 None 表示不限（Key 和成员都没写限制）；空集表示一个都不许。

    白名单里可以写四种写法，含义都是「允许这个模型的某一家来源」：
      - `qoder:dfmodel`        指定这一家
      - `dfmodel`              旧数据，按上游 key 认（只命中这一家）
      - `DeepSeek-V4.1-Flash`  不关心厂商，哪家都行
      - `deepseek-v4.1-flash`  合并后的内部 id，同上
    """
    if allowed is None:
        return True
    if f"{route['provider']}:{route['model']}" in allowed:
        return True
    if route["model"] in allowed:
        return True
    return any(spec.lower() in names for spec in allowed)


def allowed_routes(allowed: set[str] | None, model: str,
                   catalog: dict | None = None,
                   prefer: str | None = None) -> list[dict]:
    """这个模型现在可以走哪几家上游，按尝试顺序返回。

    可用模型是最终依据（没勾到的厂商一律不能用），优先账号只决定顺序：
    优先账号所属的那家排到前面先试，它不通时后面的来源顶上。

    目录条目没有 routes 时（老格式、或目录里只有名字）把模型自己当成
    唯一一条来源，白名单按字符串比，与合并前的行为一致。
    """
    entry = (catalog or {}).get(model) or {}
    routes = entry.get("routes") or []
    if not routes:
        if not entry:
            return []
        routes = [{"provider": entry.get("provider") or "",
                   "region": entry.get("region") or "",
                   "model": entry.get("upstream_key") or model}]
    names = model_names(model, catalog)
    got = [r for r in routes if route_allowed(allowed, r, names)]
    if prefer:
        got.sort(key=lambda r: 0 if r["provider"] == prefer else 1)
    return got


def check_model(key: dict, member: dict | None, model: str, catalog: dict | None = None):
    """确认这个模型至少还有一家来源被允许。"""
    allowed = allowed_models(key, member)
    if allowed is None:
        return
    # 没传目录时没有来源信息可用，只能按模型名直比
    if not catalog:
        if model in allowed:
            return
        raise Denied(403, "model_forbidden", _forbidden(model, allowed))
    if allowed_routes(allowed, model, catalog):
        return
    raise Denied(403, "model_forbidden", _forbidden(model, allowed, catalog))


def _forbidden(model: str, allowed: set[str],
               catalog: dict | None = None) -> str:
    """拒绝文案只报对外模型名。

    白名单内部按来源存（同一个模型可能有好几家上游），但那是网关自己的
    路由信息：调用方只需要知道「这个模型没开」，不该从报错里看到后面接的
    是哪家厂商、哪个上游 key。所以这里把允许的来源翻回显示名再拼文案。
    """
    names = [meta.get("name") or mid
             for mid, meta in sorted((catalog or {}).items())
             if allowed_routes(allowed, mid, catalog)]
    if not names:
        # 目录对不上（老数据、目录为空）时退回白名单里的裸模型名：
        # 那是管理员自己写的写法，带冒号的来源写法一律不露出去
        names = [s for s in sorted(allowed) if ":" not in s]
    if not names:
        return f"该密钥不允许使用模型 {model}，请联系管理员开通"
    return f"该密钥不允许使用模型 {model}，可用：{', '.join(names)}"


def tier_limits(member: dict) -> tuple[float, float]:
    """成员的 (日限额, 总限额)，单位元。

    日与周是两个独立窗口，各自回退：member.limit_daily / limit_total
    非 0 时是管理员的手动覆盖，直接生效；哪一个是 0 就单独回退到档位，
    不会被另一个窗口带着走。以前两个值共用一个零判断，只设了日限额的
    成员会变成「周限额 0 = 不限」，等于把周限额放开了。

    档位也查不到（老数据没填 tier）时才算不限，保持 0 = 不限。
    """
    daily = float(member.get("limit_daily") or 0)
    total = float(member.get("limit_total") or 0)
    tier = (member.get("tier") or "").strip().lower()
    plan = plans.PLAN_BY_SLUG.get(tier)
    if plan:
        # 只补没设过的那个窗口，设过的（含 0 以外的负数保护后）不动
        if daily <= 0:
            daily = float(plan["daily"])
        if total <= 0:
            total = float(plan["weekly"])
    return daily, total


def quota_status(store, member: dict, models: dict | None = None,
                 names: dict[str, str] | None = None,
                 overrides: dict | None = None) -> dict:
    """成员当前的额度占用，供拦截判断和界面展示。

    百分比不是一个除法，而是**各模型百分比之和**：
    `Σ(该模型已花 / 该模型额度)`。每个模型按自己那份额度折算成占总量的
    多少，花掉的份额相加就是已占用的池子。举例：a 额度 ¥100、d 额度 ¥10，
    a 花 ¥50 占 50%，再花 d ¥4 占 40%，池子就用掉 90%，只剩 10%——这 10%
    既可以继续花 ¥10 在 a 上，也可以花 ¥1 在 d 上。

    所以总额度 ¥100 是**对外口径**，真正限制能花多少的是上面这个和。
    默认每个模型的额度就是档位那一份，此时和恰好等于「总花费 / 档位额度」，
    与不分模型时的行为完全一致；把某个模型调小，它自己占的份额就变大，
    池子按比例提前用尽。

    `used_today` / `used_total` 仍是实打实的金额（界面要显示花了多少钱），
    `pct_today` / `pct_total` 才是判断依据。额度为 0 的模型不计入和，
    等于这个模型的花费不推进池子。
    """
    mid = member["id"]
    daily_limit, total_limit = tier_limits(member)
    # 档位额度只被 plan 通道消耗：api 通道走的是另一本账（赠礼余额），
    # 同一笔花费既扣余额又占档位额度等于停两次。
    used_today = store.cost_since(mid, day_start(), channel="plan")
    used_total = store.cost_total(mid, channel="plan")

    # models 可能是 all_models() 的目录（值是元数据），也可能已经是
    # {id: 显示名}；这里统一成后者，model_quota 只认名字。
    labels = {mid: (meta.get("name") or mid) if isinstance(meta, dict) else meta
              for mid, meta in (models or {}).items()}
    mq = model_quota(store, member, labels, names, overrides)
    pct_today = mq["pct_daily"]
    pct_total = mq["pct_total"]

    def left(pct: float | None, limit: float) -> float | None:
        """按剩余百分比折回金额，界面上「还剩多少」与百分比一致。"""
        if pct is None or limit <= 0:
            return None
        return round(max(0.0, limit * (100.0 - min(pct, 100.0)) / 100.0), 6)

    return {
        "tier": (member.get("tier") or "plus"),
        "limit_daily": daily_limit,
        "limit_total": total_limit,
        "used_today": round(used_today, 6),
        "used_total": round(used_total, 6),
        "left_today": left(pct_today, daily_limit),
        "left_total": left(pct_total, total_limit),
        "pct_today": pct_today,
        "pct_total": pct_total,
        # 日限额到点重置，总限额不重置
        "reset_at": day_end() if daily_limit else None,
    }


def api_quota_status(store, member: dict) -> dict:
    """API 通道的状态：余额还有多少，没有"额度百分比"概念。"""
    bal = store.api_balance(member["id"])
    return {"kind": "api", "balance": bal, "exhausted": bal <= 0}


def model_quota(store, member: dict, models: dict[str, str],
                names: dict[str, str] | None = None,
                overrides: dict[str, dict] | None = None) -> dict:
    """按模型的额度与已用，以及累加出来的百分比。

    一次调用按 `本次金额 / 该模型额度` 折算成份额，各模型份额相加减去已
    用的就是剩下的池子，池子见底（累加到 100%）就停整个 plan 通道。
    每个模型自己到 100% 时只停那一个模型。

    例：a 额度 ¥100、d 额度 ¥10。a 花 ¥50 → 占 50%，再花 d ¥4 → 占 40%，
    合计 90%，只剩 10%：还能花 ¥10 在 a 上，或 ¥1 在 d 上。总额度 ¥100
    是对外口径，真正卡住能花多少的就是这个和。

    默认每个模型的额度都是档位那一份，此时和恰好等于「总花费 / 档位额度」，
    与不分模型时的行为完全一致。

    `models` 是 {对外 id: 显示名}，只算这个成员可用的模型；历史行里出现、
    目录里已经没有的模型也计入总百分比（额度取档位默认），否则它们花的
    钱会从额度里凭空消失。
    """
    mid = member["id"]
    base_daily, base_total = tier_limits(member)
    # 不传就从库里读：调用方忘了传时，界面不能把改过的额度显示成默认值
    if overrides is None:
        overrides = store.model_limits(mid)
    names = names or {}

    def label(raw: str) -> str:
        raw = raw or ""
        return names.get(raw) or names.get(raw.lower()) or raw

    def limits_for(model_id: str, model_label: str) -> tuple[float, float]:
        # 额度按对外名字存：管理员在成员弹窗里看到的就是模型名，
        # 同一个模型背后可能有多条上游来源，按 id 存反而分不清。
        ov = (overrides.get(model_label) or overrides.get(model_id) or {})
        daily = ov.get("limit_daily")
        total = ov.get("limit_total")
        return (base_daily if daily is None else float(daily),
                base_total if total is None else float(total))

    # 只看 plan 通道：api 通道走另一本账，占同一份额度会重复计算
    today = store.member_model_summary(mid, since=day_start(),
                                       until=day_end(), channel="plan")
    all_time = store.member_model_summary(mid, since=0, channel="plan")

    rows: dict[str, dict] = {}
    # 先按目录铺一遍：没调用过的模型也要在界面上有一行额度
    for model_id, model_label in models.items():
        daily, total = limits_for(model_id, model_label)
        rows[model_label] = {
            "id": model_id, "label": model_label,
            "limit_daily": daily, "limit_total": total,
            "used_today": 0.0, "used_total": 0.0,
            "overridden": bool(overrides.get(model_label)
                               or overrides.get(model_id)),
        }
    # 再按实际用量累加。历史上用过的模型即使已下架也要算进总百分比。
    for bucket, field in ((today, "used_today"), (all_time, "used_total")):
        for r in bucket:
            raw = r["model"] or ""
            model_label = label(raw)
            cell = rows.get(model_label)
            if cell is None:
                daily, total = limits_for(raw, model_label)
                cell = rows[model_label] = {
                    "id": raw, "label": model_label,
                    "limit_daily": daily, "limit_total": total,
                    "used_today": 0.0, "used_total": 0.0,
                    "overridden": bool(overrides.get(model_label)
                                       or overrides.get(raw)),
                }
            cell[field] += float(r["cost"] or 0)

    for cell in rows.values():
        # 限额字段叫 limit_daily / limit_total，已用叫 used_today / used_total，
        # 百分比叫 pct_daily / pct_total：日窗口对的是「今天」（从 0 点算），
        # 总额度不重置。三组名字各自与库里的列、与界面上的词对齐。
        for limit_key, used_key, pct_key, blocked_key in (
                ("limit_daily", "used_today", "pct_daily", "blocked_daily"),
                ("limit_total", "used_total", "pct_total", "blocked_total")):
            limit = cell[limit_key]
            used = cell[used_key]
            cell[pct_key] = (round(used * 100.0 / limit, 2)
                             if limit > 0 else None)
            cell[blocked_key] = bool(limit > 0 and used >= limit)
        # 缺口按最大的那个窗口报，界面上一句「这个模型已用完」就够
        cell["blocked"] = cell["blocked_daily"] or cell["blocked_total"]

    def acc(window: str) -> float | None:
        """各模型百分比相加，就是已经占掉多少份额度池。

        额度为 0（不限）的模型没有分母，不计入——花多少都不推进池子。
        一个可计入的模型都没有时返回 None，表示"没有额度这回事"，
        而不是 0（0 会被界面读成"一点没用"）。
        """
        terms = [pct for pct in
                 (c[f"pct_{window}"] for c in rows.values())
                 if pct is not None]
        return round(sum(terms), 2) if terms else None

    return {
        "models": sorted(rows.values(), key=lambda c: -c["used_total"]),
        "pct_daily": acc("daily"),
        "pct_total": acc("total"),
        "limit_daily": base_daily,
        "limit_total": base_total,
        "used_today": round(sum(c["used_today"] for c in rows.values()), 6),
        "used_total": round(sum(c["used_total"] for c in rows.values()), 6),
    }


def check_quota(store, member: dict | None, key: dict | None = None,
                models: dict | None = None,
                names: dict[str, str] | None = None):
    """按密钥通道做准入。

    plan 通道：各模型份额之和（见 model_quota）到 100% 就拒。这是整个
    通道的闸，不分是哪个模型；某个模型自己用完另走 check_model_quota。
    api 通道：余额 <= 0 就拒绝，不参与额度百分比。

    `models` 决定哪些模型各自有一份额度，传 all_models() 的目录或
    {id: 显示名} 都行。不传也能判：此时只按历史用过的模型算份额，
    没用过的模型贡献 0，不影响结论。
    """
    if not member:
        return
    kind = (key or {}).get("kind") or "plan"
    if kind == "api":
        st = api_quota_status(store, member)
        if st["exhausted"]:
            raise Denied(
                429, "api_balance_exhausted",
                "API 通道余额不足，请联系管理员发放",
            )
        return

    st = quota_status(store, member, models, names)
    # None 表示一个可计入的模型都没有（额度全是 0）＝ 不限
    if st["pct_today"] is not None and st["pct_today"] >= 100.0:
        raise Denied(
            429, "quota_daily_exceeded",
            f"今日额度已用完（{st['pct_today']:.1f}%），"
            f"{time.strftime('%H:%M', time.localtime(st['reset_at']))} 重置",
        )
    if st["pct_total"] is not None and st["pct_total"] >= 100.0:
        raise Denied(
            429, "quota_total_exceeded",
            f"额度已用完（{st['pct_total']:.1f}%），请联系管理员调整档位",
        )


def check_model_quota(store, member: dict | None, key: dict | None,
                      model: str,
                      names: dict[str, str] | None = None,
                      overrides: dict[str, dict] | None = None):
    """这一个模型自己的额度用完了就拒，别的模型不受影响。

    模型额度默认就是整份档位额度，所以默认情况下它到 100% 的时刻与
    check_quota 的总额度到顶是同一时刻，多这一道不会改变现有行为；
    管理员把某个模型改小时，它自己的 100% 才会提前先到。

    ``model`` 传对外显示名；``names`` 用来把历史行里的上游 key 折成
    同一个名字，不传就只能按原样比。
    """
    if not member:
        return
    if ((key or {}).get("kind") or "plan") != "plan":
        # api 通道按余额扣，不参与按模型的额度
        return
    if overrides is None:
        overrides = store.model_limits(member["id"])
    names = names or {}
    base_daily, base_total = tier_limits(member)
    ov = overrides.get(model) or {}
    limit_daily = ov.get("limit_daily")
    limit_total = ov.get("limit_total")
    limit_daily = base_daily if limit_daily is None else float(limit_daily)
    limit_total = base_total if limit_total is None else float(limit_total)
    if limit_daily <= 0 and limit_total <= 0:
        return

    rows = store.member_model_summary(member["id"], channel="plan")
    today = store.member_model_summary(member["id"], since=day_start(),
                                       until=day_end(), channel="plan")

    def used_of(bucket: list[dict]) -> float:
        return sum(float(r["cost"] or 0) for r in bucket
                   if (names.get(r["model"] or "")
                       or names.get((r["model"] or "").lower())
                       or (r["model"] or "")) == model)

    used_daily = used_of(today)
    used_total = used_of(rows)

    if limit_daily > 0 and used_daily >= limit_daily:
        raise Denied(
            429, "model_quota_exceeded",
            f"模型 {model} 的今日额度已用完"
            f"（{used_daily * 100.0 / limit_daily:.1f}%），"
            f"{time.strftime('%H:%M', time.localtime(day_end()))} 重置",
        )
    if limit_total > 0 and used_total >= limit_total:
        raise Denied(
            429, "model_quota_exceeded",
            f"模型 {model} 的额度已用完"
            f"（{used_total * 100.0 / limit_total:.1f}%），请联系管理员调整",
        )
