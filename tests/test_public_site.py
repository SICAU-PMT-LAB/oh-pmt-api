"""公开站点与数据页的口径测试。

重点是两件事：
  1. 公开接口不能泄露可识别信息（成员名、密钥、账号凭据）；
  2. 数据页的数字要和账本对得上，别出现「合计比明细少」这类自相矛盾。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import plans  # noqa: E402
import pricing  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402


def fake_usage(store, *, member_id, model, tokens, prompt, out, cache, cost,
               ts=None, status=200):
    with store._conn() as c:
        c.execute(
            """INSERT INTO usage (key_id, member_id, account_id, model,
                prompt_tokens, completion_tokens, total_tokens,
                cache_read_tokens, cache_write_tokens, cost, price_source,
                rate, credits, credits_original, status, error, ip, created_at)
               VALUES (0,?,NULL,?,?,?,?,?,0,?,'builtin',1,0,0,?,?,?,?)""",
            (member_id, model, prompt, out, tokens, cache, cost, status, "",
             "127.0.0.1", ts if ts is not None else time.time()))


class PublicDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        server.STORE = self.store
        self.handler = server.Handler.__new__(server.Handler)

    def tearDown(self):
        server.STORE = None
        os.unlink(self.tmp.name)

    def test_daily_sum_matches_totals(self):
        mid = self.store.create_member(name="张三", username="zs", password="x")
        for i in range(3):
            fake_usage(self.store, member_id=mid, model="deepseek-v4.1-flash",
                       tokens=1000, prompt=800, out=200, cache=100, cost=0.01)
        data = self.handler._public_data()
        self.assertEqual(sum(d["requests"] for d in data["daily"]),
                         data["totals"]["requests"])
        self.assertEqual(sum(d["tokens"] for d in data["daily"]),
                         data["totals"]["tokens"])

    def test_cache_rate_uses_prompt_tokens(self):
        """缓存比例的分母是输入 token，不是总 token。

        把输出算进分母会让命中率凭空变低，看起来像缓存没生效。
        """
        mid = self.store.create_member(name="m", username="m", password="x")
        # 输入 1000，其中 800 命中缓存；输出 5000 不该影响比例
        fake_usage(self.store, member_id=mid, model="deepseek-v4.1-flash",
                   tokens=6000, prompt=1000, out=5000, cache=800, cost=0.02)
        data = self.handler._public_data()
        self.assertEqual(data["totals"]["cache_rate"], 80.0)

    def test_no_identifiable_fields_leak(self):
        mid = self.store.create_member(name="张三", username="zs@sicau.edu.cn",
                                       password="x")
        fake_usage(self.store, member_id=mid, model="deepseek-v4.1-flash",
                   tokens=1000, prompt=800, out=200, cache=0, cost=0.01)
        blob = repr(self.handler._public_data())
        self.assertNotIn("张三", blob)
        self.assertNotIn("zs@sicau.edu.cn", blob)
        # 成员只以序号出现
        self.assertIn("成员 01", blob)

    def test_models_merged_across_upstream_keys(self):
        """同一模型的两个上游 key 在榜上必须是一行。"""
        mid = self.store.create_member(name="m", username="m2", password="x")
        fake_usage(self.store, member_id=mid, model="dfmodel", tokens=100,
                   prompt=80, out=20, cache=0, cost=0.001)
        fake_usage(self.store, member_id=mid, model="deepseek-v4.1-flash",
                   tokens=200, prompt=160, out=40, cache=0, cost=0.002)
        data = self.handler._public_data()
        names = [row["model"] for row in data["leaderboard"]]
        self.assertEqual(len([n for n in names if "eepSeek" in n]), 1, names)

    def test_empty_store_does_not_crash(self):
        data = self.handler._public_data()
        self.assertEqual(data["totals"]["requests"], 0)
        self.assertIsNone(data["totals"]["cache_rate"])
        self.assertEqual(data["leaderboard"], [])
        self.assertEqual(len(data["daily"]), self.handler.PUBLIC_DAYS)

    def test_heatmap_covers_seven_weekdays(self):
        data = self.handler._public_data()
        self.assertEqual(len(data["heatmap"]["grid"]), 7)
        self.assertEqual(len(data["heatmap"]["grid"][0]), 24)

    def test_retention_needs_two_weeks(self):
        """只有一周的数据时不该算出 100% 的留存。"""
        mid = self.store.create_member(name="m", username="m3", password="x")
        fake_usage(self.store, member_id=mid, model="deepseek-v4.1-flash",
                   tokens=100, prompt=80, out=20, cache=0, cost=0.001)
        data = self.handler._public_data()
        self.assertEqual(data["retention"], [])


class PlanTest(unittest.TestCase):
    def test_bigger_plan_allows_more_requests(self):
        price = pricing.BUILTIN["deepseek-v4.1-flash"]
        profile = plans.profile_for("deepseek-v4.1-flash")[0]
        counts = [plans.requests_for_budget(p["daily"], price, profile)
                  for p in plans.PLANS]
        self.assertEqual(counts, sorted(counts))
        self.assertTrue(all(c > 0 for c in counts), counts)

    def test_tier_budgets(self):
        """四档的日/周预算（元）：Go 5/25、Plus 15/75、Pro 30/150、Max 100/500。"""
        got = {p["slug"]: (p["daily"], p["weekly"]) for p in plans.PLANS}
        self.assertEqual(got, {"go": (5.0, 25.0), "plus": (15.0, 75.0),
                               "pro": (30.0, 150.0), "max": (100.0, 500.0)})

    def test_session_cost_matches_pricing_formula(self):
        price = {"input": 1.0, "output": 2.0, "cache_read": 0.1,
                 "cache_write": 1.0}
        profile = (4000, 16000, 2000)
        got = plans.session_cost(price, profile)
        # prompt 含缓存（4000+16000），其中 16000 按缓存价，另加 2000 输出
        expect = (4000 * 1.0 + 16000 * 0.1 + 2000 * 2.0) / 1e6
        self.assertAlmostEqual(got, expect, places=9)

    def test_profile_lookup_prefers_exact_model_id(self):
        profile, source = plans.profile_for("deepseek-v4.1-flash")
        self.assertEqual(source, "known")
        self.assertEqual(profile, plans.REQUEST_PROFILES["deepseek-v4.1-flash"])

    def test_profile_lookup_falls_back_to_default(self):
        profile, source = plans.profile_for("some-unknown-model-xyz")
        self.assertEqual(source, "default")
        self.assertEqual(profile, plans.DEFAULT_PROFILE)

    def test_profile_lookup_matches_display_name(self):
        # 上游 key 不在表里，靠显示名 "Hy4 preview" 命中 hy4-preview
        profile, source = plans.profile_for("", "Hy4 preview")
        self.assertEqual(source, "known")
        self.assertEqual(profile, plans.REQUEST_PROFILES["hy4-preview"])

    def test_cached_input_dominates_cost(self):
        """缓存必须计入折扣，否则请求数会被低估一个量级。"""
        price = pricing.BUILTIN["deepseek-v4.1-flash"]
        profile = plans.REQUEST_PROFILES["deepseek-v4.1-flash"]
        with_cache = plans.session_cost(price, profile)
        fresh, cached, output = profile
        # 全部按 input 原价（缓存不打折）时的成本
        no_discount = ((fresh + cached) * price["input"]
                       + output * price["output"]) / 1e6
        self.assertLess(with_cache, no_discount * 0.2)

    def test_catalog_marks_default_price_rows(self):
        models = {
            "weird-model": {"name": "Weird-Model", "provider": "qoder",
                            "routes": [{"model": "weird-model"}]},
            "deepseek-v4.1-flash": {
                "name": "DeepSeek-V4.1-Flash", "provider": "qoder",
                "routes": [{"model": "deepseek-v4.1-flash"}]},
        }
        rows = plans.catalog(models, {})
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["weird-model"]["estimated"])
        self.assertFalse(by_id["deepseek-v4.1-flash"]["estimated"])

    def test_deepseek_profile_is_cheap_per_hundred_million(self):
        """99% 缓存的画像 + 输入价 1/50 的缓存价，成本被压到几元一亿。"""
        price = {"input": 1.0, "output": 4.0, "cache_read": 0.02,
                 "cache_write": 0.0}
        profile = plans.REQUEST_PROFILES["deepseek-v4.1-flash"]
        per_100m = plans.cost_per_mtok(price, profile) * 100
        self.assertLess(per_100m, 5.0)

    def test_deepseek_profile_has_ninety_nine_percent_cache(self):
        """画像必须是「输入侧 99% 命中缓存」的编程会话形态。"""
        fresh, cached, output = plans.REQUEST_PROFILES["deepseek-v4.1-flash"]
        self.assertGreater(cached / (fresh + cached), 0.99)
        self.assertGreater(output, 0)

    def test_catalog_sorted_by_go_requests_asc(self):
        """额度升序：同一档能跑的轮数越少，排在越前面。"""
        models = {
            mid: {"name": mid, "provider": "qoder", "routes": [{"model": mid}]}
            for mid in ("glm-5.3-flash", "kimi-k3.1-pro")
        }
        rows = plans.catalog(models, {})
        self.assertEqual(rows[0]["id"], "kimi-k3.1-pro")
        self.assertLess(rows[0]["daily"]["go"], rows[1]["daily"]["go"])

    def test_free_model_reports_no_request_limit(self):
        """单价全为 0 时额度用不完：请求数算无限，不能算成 0 次也不该除以 0。"""
        free = {f: 0.0 for f in pricing.PRICE_FIELDS}
        models = {"hy3": {"name": "Hy3", "provider": "workbuddy",
                           "routes": [{"model": "hy3"}]}}
        row = plans.catalog(models, {"hy3": free})[0]
        self.assertIsNone(row["daily"]["go"])
        self.assertIsNone(row["weekly_tokens"]["max"])
        self.assertEqual(row["session_cost"], 0.0)

    def test_free_model_sorts_last(self):
        """按额度升序时免费模型没有次数，排表尾。"""
        free = {f: 0.0 for f in pricing.PRICE_FIELDS}
        models = {
            mid: {"name": mid, "provider": "workbuddy",
                  "routes": [{"model": mid}]}
            for mid in ("hy3", "deepseek-v4.1-flash")
        }
        rows = plans.catalog(models, {"hy3": free})
        self.assertEqual(rows[-1]["id"], "hy3")

    def test_paid_models_have_finite_request_counts(self):
        """Hy3 与 Space-Bunny 都按官方价计费，不再有无限额度那个特例。"""
        for model in ("hy3", "space-bunny"):
            price, _ = pricing.lookup(model)
            self.assertGreater(plans.session_cost(price,
                                  plans.REQUEST_PROFILES[model]), 0, model)
            self.assertIsNotNone(plans.requests_for_budget(
                5.0, price, plans.REQUEST_PROFILES[model]), model)

    def test_kimi_k3_profile_and_price_are_its_own_tier(self):
        """Kimi-K3 与 K3.1-Pro 是两代两价，画像与单价都不能串。"""
        models = {
            mid: {"name": name, "provider": "workbuddy",
                  "routes": [{"model": mid}]}
            for mid, name in (("kimi-k3-1", "Kimi-K3"),
                              ("kimi-k3.1-pro", "Kimi-K3.1-Pro"))
        }
        by_id = {r["id"]: r for r in plans.catalog(models, {})}
        self.assertEqual(by_id["kimi-k3-1"]["price"]["input"], 20.0)
        self.assertEqual(by_id["kimi-k3.1-pro"]["price"]["input"], 13.0)
        self.assertLess(by_id["kimi-k3-1"]["daily"]["go"],
                        by_id["kimi-k3.1-pro"]["daily"]["go"])

    def test_describe_carries_assumptions(self):
        out = plans.describe({"glm-5.3-flash": {
            "name": "GLM-5.3-Flash", "provider": "workbuddy",
            "routes": [{"model": "glm-5.3-flash"}]}}, {})
        self.assertEqual(out["assumptions"]["basis"], "opencode")
        self.assertEqual(out["assumptions"]["window"], "daily")
        self.assertEqual(len(out["plans"]), 4)
        row = out["models"][0]
        self.assertIn("profile", row)
        self.assertEqual(row["tokens_per_request"],
                         row["profile"]["fresh"] + row["profile"]["cached"]
                         + row["profile"]["output"])
        self.assertNotIn("five_hour", row)
        self.assertIn("daily", row)

    def test_plans_payload_exposes_weekly_budget(self):
        """套餐页的「周限额」列直接用各档的周预算金额，四档都要在。"""
        out = plans.describe({}, {})
        got = {p["slug"]: p["weekly"] for p in out["plans"]}
        self.assertEqual(got, {"go": 25.0, "plus": 75.0,
                               "pro": 150.0, "max": 500.0})

    def test_plans_payload_hides_upstream_vendor(self):
        """套餐页与文档页不需要登录，模型行里不能出现厂商与上游 key。"""
        out = plans.describe({
            "deepseek-v4.1-flash": {
                "id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash",
                "provider": "qoder",
                "routes": [{"provider": "qoder", "region": "global",
                            "model": "dfmodel"}]}}, {})
        row = out["models"][0]
        self.assertNotIn("provider", row)
        blob = json.dumps(out["models"], ensure_ascii=False)
        for word in ("qoder", "workbuddy", "dfmodel"):
            self.assertNotIn(word, blob, "公开的模型清单不该出现 %s" % word)


class PublicRouteTest(unittest.TestCase):
    """公开页面必须不需要登录就能打开，后台仍然要登录。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        server.STORE = self.store

    def tearDown(self):
        server.STORE = None
        os.unlink(self.tmp.name)

    def _resolve(self, path):
        """只跑路由判定，不真的写 socket：用假的 session / 写方法。"""
        h = server.Handler.__new__(server.Handler)
        served = {}

        def serve_html(name):
            served["html"] = name
            return name

        def redirect(location, cookie=None):
            served["redirect"] = location
            return location

        def err(status, code, message, retry_after=0):
            served["error"] = (status, code)
            return False

        h.path = path
        h._session = lambda: None
        h._auth_admin_page = lambda: False
        h._serve_html = serve_html
        h._serve_static = lambda p: served.setdefault("static", p)
        h._redirect = redirect
        h._error = err
        h._local_rescue = lambda: False
        try:
            server.Handler.do_GET(h)
        except Exception:  # noqa: BLE001
            pass
        return served

    def test_home_is_public(self):
        self.assertEqual(self._resolve("/")["html"], "home.html")

    def test_plan_is_public(self):
        self.assertEqual(self._resolve("/plan")["html"], "plan.html")

    def test_data_is_public(self):
        self.assertEqual(self._resolve("/data")["html"], "data.html")

    def test_docs_is_public(self):
        self.assertEqual(self._resolve("/docs")["html"], "docs.html")

    def test_admin_still_requires_login(self):
        got = self._resolve("/admin")
        self.assertNotIn("html", got)
        self.assertEqual(got.get("error", (None,))[0], None)
        # 未登录时 _auth_admin_page 返回 False 并已经发过 401/302
    def test_member_page_still_requires_login(self):
        got = self._resolve("/members/1")
        self.assertNotIn("html", got)


if __name__ == "__main__":
    unittest.main()
