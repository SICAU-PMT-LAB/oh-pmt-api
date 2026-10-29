"""按 token 计价与日/周限额的测试。

单价是 per 1M tokens 的人民币元（取自各家官方中文定价页），
成本 = 各类 token × 各自单价 / 100 万，成员的额度也用元表示，到 100% 就停服。
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402
import pricing  # noqa: E402
from store import Store, week_start  # noqa: E402


class CostOfTest(unittest.TestCase):
    def test_plain_input_output(self):
        # 1M input + 1M output，单价 0.15 / 0.60
        cost = pricing.cost_of(
            {"input": 0.15, "output": 0.60},
            prompt_tokens=1_000_000, completion_tokens=1_000_000)
        self.assertAlmostEqual(cost, 0.75, places=8)

    def test_small_request(self):
        cost = pricing.cost_of(
            {"input": 0.15, "output": 0.60},
            prompt_tokens=1000, completion_tokens=500)
        self.assertAlmostEqual(cost, (1000 * 0.15 + 500 * 0.60) / 1e6, places=10)

    def test_cache_read_charged_at_cache_price(self):
        """prompt_tokens 已经包含命中的部分，命中部分要按缓存价而不是原价算。"""
        cost = pricing.cost_of(
            {"input": 1.00, "output": 4.00, "cache_read": 0.10},
            prompt_tokens=1000, completion_tokens=0, cache_read_tokens=800)
        expected = (200 * 1.00 + 800 * 0.10) / 1e6
        self.assertAlmostEqual(cost, expected, places=10)

    def test_cache_write_charged_at_write_price(self):
        cost = pricing.cost_of(
            {"input": 1.00, "output": 4.00, "cache_read": 0.10, "cache_write": 1.25},
            prompt_tokens=1000, completion_tokens=0, cache_write_tokens=400)
        expected = (600 * 1.00 + 400 * 1.25) / 1e6
        self.assertAlmostEqual(cost, expected, places=10)

    def test_cache_tokens_never_exceed_prompt(self):
        """上游偶尔把缓存 token 报得比 prompt 还大，不能算出负的原价部分。"""
        cost = pricing.cost_of(
            {"input": 1.00, "output": 4.00, "cache_read": 0.10},
            prompt_tokens=100, completion_tokens=0, cache_read_tokens=500)
        self.assertAlmostEqual(cost, 100 * 0.10 / 1e6, places=10)

    def test_missing_cache_price_is_free_not_negative(self):
        cost = pricing.cost_of({"input": 1.00, "output": 4.00},
                               prompt_tokens=1000, completion_tokens=0,
                               cache_read_tokens=1000)
        self.assertAlmostEqual(cost, 0.0, places=10)

    def test_zero_tokens_costs_nothing(self):
        self.assertEqual(pricing.cost_of({"input": 1.0}, prompt_tokens=0,
                                         completion_tokens=0), 0.0)


class LookupTest(unittest.TestCase):
    def test_builtin_hit_by_upstream_key(self):
        """智谱官方价：GLM-5.3-Flash 输入 0.8 元/百万。"""
        price, kind = pricing.lookup("glm-5.3-flash")
        self.assertEqual(kind, "builtin")
        self.assertAlmostEqual(price["input"], 0.8, places=4)

    def test_display_name_hits_same_row(self):
        """上游 key 与显示名只差大小写和连字符，归一后应命中同一行。"""
        by_key, _ = pricing.lookup("glm-5.3-flash")
        by_name, kind = pricing.lookup("", "GLM-5.3-Flash")
        self.assertEqual(kind, "builtin")
        self.assertEqual(by_key, by_name)

    def test_qoder_alias_resolves(self):
        """Qoder 的 dfmodel 对到 deepseek-v4.1-flash（与生产库覆盖价一致）。"""
        price, kind = pricing.lookup("dfmodel")
        self.assertEqual(kind, "builtin")
        self.assertAlmostEqual(price["input"], 1.0, places=4)

    def test_unknown_falls_back_to_default(self):
        price, kind = pricing.lookup("no-such-model-xyz")
        self.assertEqual(kind, "default")
        self.assertEqual(price, pricing.DEFAULT_PRICE)

    def test_override_wins(self):
        price, kind = pricing.lookup(
            "glm-5.3-flash", overrides={"glm-5.3-flash": {"input": 9.0}})
        self.assertEqual(kind, "override")
        self.assertAlmostEqual(price["input"], 9.0, places=4)
        # 没写的字段按 0 处理，不继承内置值：覆盖就是整条替换
        self.assertEqual(price["output"], 0.0)

    def test_normalize_overrides_drops_junk(self):
        clean = pricing.normalize_overrides({
            "a": {"input": 1.0, "bogus": 2},
            "b": "not-a-dict",
            "c": {},
        })
        self.assertEqual(clean, {"a": {"input": 1.0}})

    def test_normalize_overrides_rejects_non_dict(self):
        self.assertEqual(pricing.normalize_overrides(None), {})
        self.assertEqual(pricing.normalize_overrides([1, 2]), {})


class OverrideTest(unittest.TestCase):
    """settings 里的覆盖值：只接受四个单价，其余键丢弃。"""

    def test_unknown_fields_are_dropped(self):
        overrides = pricing.normalize_overrides(
            {"glm-5.3-flash": {"input": 2.0, "rate": 1.8}})
        self.assertEqual(overrides["glm-5.3-flash"], {"input": 2.0})
        price, kind = pricing.lookup("glm-5.3-flash", overrides=overrides)
        self.assertEqual(kind, "override")
        self.assertAlmostEqual(price["input"], 2.0)

    def test_builtin_rows_only_hold_unit_prices(self):
        """内置表只有四个单价列。"""
        for key, price in pricing.BUILTIN.items():
            with self.subTest(model=key):
                self.assertTrue(set(price) <= set(pricing.PRICE_FIELDS), key)


class RetiredTest(unittest.TestCase):
    """下架名单只挡新请求，不动历史账目。"""

    def test_retired_names_match_loosely(self):
        for name in ("fast-model", "WorkBuddy 极致", "deep-model",
                     "DeepSeek-V3.2", "deepseek v3 2 volc",
                     "DeepSeek-V4-Flash", "glm-5v-turbo", "GLM-5.2",
                     "auto", "Ultimate", "Sonus", "hunyuan-chat"):
            with self.subTest(name=name):
                self.assertTrue(pricing.retired(name), name)

    def test_kept_models_are_not_retired(self):
        for name in ("hy3", "deepseek-v4.1-flash", "dfmodel",
                     "glm-5.3-flash", "gfmodel", "minimax-m3.1-flash",
                     "space-bunny", "step-5-preview", "mimo-v2.6-pro",
                     "qmodel_38max", "qfmodel", "kimi-k3.1-pro"):
            with self.subTest(name=name):
                self.assertFalse(pricing.retired(name), name)

    def test_any_retired_route_retires_the_model(self):
        """一个对外名字对应多家上游时，任一路由下架就整条下架。"""
        self.assertTrue(pricing.retired("deepseek-v4.1-flash",
                                        "DeepSeek-V4-Flash"))

    def test_retired_models_keep_their_price(self):
        """下架不等于删价：历史行回填和对账还要按当时的口径算得出来。"""
        price, kind = pricing.lookup("glm-5.2")
        self.assertEqual(kind, "builtin")
        self.assertAlmostEqual(price["input"], 8.0)


class LimitTest(unittest.TestCase):
    """日限额本地 0 点重置，周限额周一 0 点重置，任一窗口到 100% 就拒绝。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _member(self, **kw):
        return self.store.get_member(self.store.create_member(name="t", **kw))

    def _spend(self, member_id, cost, when=None):
        self.store.record_usage(key_id=1, member_id=member_id, account_id=1,
                                model="m", prompt_tokens=0, completion_tokens=0,
                                cost=cost)
        if when is not None:
            with self.store._conn() as c:
                c.execute("UPDATE usage SET created_at=? WHERE member_id=?",
                          (when, member_id))

    def test_exactly_at_limit_stops(self):
        """到 100% 就停，不是超过才停。"""
        m = self._member(limit_daily=1.0)
        self._spend(m["id"], 1.0)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, m)
        self.assertEqual(ctx.exception.code, "quota_daily_exceeded")
        self.assertIn("重置", ctx.exception.message)

    def test_just_below_limit_passes(self):
        m = self._member(limit_daily=1.0)
        self._spend(m["id"], 0.9999)
        guard.check_quota(self.store, m)

    def test_week_accumulates_across_days(self):
        m = self._member(limit_daily=100.0, limit_total=1.5)
        # 本周一 0 点的那笔已不占今天的日额度，但仍占本周额度
        self._spend(m["id"], 1.4, when=week_start())
        guard.check_quota(self.store, m)
        self._spend(m["id"], 0.2)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, m)
        self.assertEqual(ctx.exception.code, "quota_total_exceeded")

    def test_zero_limit_means_unlimited(self):
        """额度与档位都为空时表示不限。

        正常成员默认 plus 档，额度由档位推导；这里把 tier 清空，
        模拟管理员显式把某人排除在档位体系之外的情形。
        """
        m = self._member()
        with self.store._conn() as c:
            c.execute("UPDATE members SET tier='' WHERE id=?", (m["id"],))
        m = self.store.get_member(m["id"])
        self.assertEqual(guard.tier_limits(m), (0.0, 0.0))
        self._spend(m["id"], 1000.0)
        guard.check_quota(self.store, m)
        st = guard.quota_status(self.store, m)
        self.assertIsNone(st["pct_today"])
        self.assertIsNone(st["left_total"])


class PricingSettingsTest(unittest.TestCase):
    """管理员改过的价目存在 settings 里，覆盖内置表。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_roundtrip_through_settings(self):
        self.store.set_setting("model_prices", {"kimi-k3": {"input": 5.0}})
        overrides = pricing.normalize_overrides(
            self.store.get_setting("model_prices", {}))
        price, kind = pricing.lookup("kimi-k3", overrides=overrides)
        self.assertEqual(kind, "override")
        self.assertAlmostEqual(price["input"], 5.0, places=4)


if __name__ == "__main__":
    unittest.main()


class BuiltinTableTest(unittest.TestCase):
    """内置价表是人民币口径，防止有人误按美元价改回去。"""

    def test_currency_is_cny(self):
        self.assertEqual(pricing.CURRENCY, "CNY")
        self.assertEqual(pricing.CURRENCY_SYMBOL, "¥")

    def test_prices_are_in_rmb_magnitude(self):
        """国内模型输入价在 0.1~20 元/百万之间；美元价换算过来会低一个量级。"""
        for key, price in pricing.BUILTIN.items():
            with self.subTest(model=key):
                if not any(price.values()):
                    continue  # 全零是限时免费的公测模型，不是漏填
                self.assertGreater(price["input"], 0.05, key)
                self.assertLess(price["input"], 30.0, key)
                self.assertGreaterEqual(price["output"], price["input"], key)

    def test_free_model_costs_nothing(self):
        """免费模型四个单价都是 0，请求数算成无限而不是除以 0。"""
        price = {f: 0.0 for f in pricing.PRICE_FIELDS}
        got, kind = pricing.lookup("some-free-model", overrides={
            "some-free-model": price})
        self.assertEqual(kind, "override")
        self.assertFalse(any(got.values()), got)

    def test_hy3_and_space_bunny_are_paid(self):
        """Hy3 与 Space-Bunny 都按官方价收费，不能当成免费模型。"""
        for model in ("hy3", "space-bunny"):
            price, kind = pricing.lookup(model)
            self.assertEqual(kind, "builtin", model)
            self.assertTrue(all(price[f] > 0 for f in ("input", "output",
                                                       "cache_read")), model)

    def test_kimi_k3_is_not_priced_as_k3_1_pro(self):
        """WorkBuddy 的 kimi-k3-1 显示名也是 Kimi-K3，要落到 K3 那一行。

        两代是两个价档（K3 入 20 出 100，K3.1-Pro 入 13 出 68），\
        曾经因为内置表里抄了一份 13/68 给 kimi-k3-1，目录里的 Kimi-K3
        一直按 K3.1-Pro 计价。
        """
        for names in (("kimi-k3-1", "Kimi-K3", "kimi-k3"),
                      ("", "Kimi-K3", ""),
                      ("kmodel_latest", "Kimi-K3", "kimi-k3")):
            with self.subTest(names=names):
                price, kind = pricing.lookup(*names)
                self.assertEqual(kind, "builtin")
                self.assertEqual(price["input"], 20.0)
                self.assertEqual(price["output"], 100.0)

    def test_deepseek_matches_production_override(self):
        """V4.1-Flash 入 1.0 / 出 4.0 / 缓存命中 0.02，与生产库覆盖值一致。

        内置表若与生产库的 settings.model_prices 不一致，本地新建的库
        会按另一套价计费，对账时两边数字对不上。
        """
        price, kind = pricing.lookup("deepseek-v4.1-flash")
        self.assertEqual(kind, "builtin")
        self.assertEqual(price["input"], 1.0)
        self.assertEqual(price["output"], 4.0)
        self.assertEqual(price["cache_read"], 0.02)

    def test_retired_v4_flash_still_priced_for_history(self):
        """下架的模型价目仍在，历史行回填与对账还要用得到。"""
        price, kind = pricing.lookup("deepseek-v4-flash")
        self.assertEqual(kind, "builtin")
        self.assertEqual(price["input"], 1.0)

    def test_kimi_k31_has_cache_write(self):
        """Kimi K3 系列按 TTL 单独收缓存写入费，写价等于输入价。"""
        price, _ = pricing.lookup("kimi-k3.1-pro")
        self.assertEqual(price["cache_write"], 13.0)

    def test_default_price_is_rmb_scale(self):
        self.assertLess(pricing.DEFAULT_PRICE["input"], 5.0)
        self.assertGreater(pricing.DEFAULT_PRICE["input"], 1.0)
