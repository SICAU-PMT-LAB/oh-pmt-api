"""计费两条通道的测试：plan 走档位百分比，api 走预存余额。

覆盖三件事：
1. store 的 api 余额账本是纯追加的，冲正靠反向记账而不是改行
2. guard 按密钥的 kind 分流：plan 查档位额度，api 查余额
3. 成员详情接口把 tier / api 余额 / 密钥 kind 一起带出来
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402
import server  # noqa: E402
import store as store_mod  # noqa: E402


class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pmt-billing-")
        self.store = store_mod.Store(os.path.join(self.tmp, "t.db"))
        self.mid = self.store.create_member(name="甲", password="pw")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_member_defaults_to_plus(self):
        m = self.store.get_member(self.mid)
        self.assertEqual(m["tier"], "plus")
        self.assertEqual(guard.tier_limits(m), (15.0, 75.0))

    def test_tier_can_be_changed(self):
        self.store.update_member(self.mid, tier="pro")
        m = self.store.get_member(self.mid)
        self.assertEqual(guard.tier_limits(m), (30.0, 150.0))

    def test_api_balance_starts_at_zero(self):
        self.assertEqual(self.store.api_balance(self.mid), 0.0)

    def test_topup_and_charge(self):
        self.store.api_topup(self.mid, 50.0, "初始发放")
        self.assertEqual(self.store.api_balance(self.mid), 50.0)
        self.store.charge_api(self.mid, 12.5, "调用 A")
        self.assertEqual(self.store.api_balance(self.mid), 37.5)

    def test_charge_is_always_negative(self):
        # charge_api 传正数也要扣减，避免调用方写错符号导致反向充值
        self.store.api_topup(self.mid, 10.0)
        self.store.charge_api(self.mid, 3.0)
        self.assertEqual(self.store.api_balance(self.mid), 7.0)

    def test_ledger_is_newest_first(self):
        self.store.api_topup(self.mid, 10.0, "第一笔")
        self.store.api_topup(self.mid, 20.0, "第二笔")
        rows = self.store.api_ledger(self.mid)
        self.assertEqual([r["note"] for r in rows], ["第二笔", "第一笔"])

    def test_ledger_is_append_only(self):
        # 冲正写反向行，原行保留：账本可审计
        self.store.api_topup(self.mid, 10.0, "误充")
        self.store.api_topup(self.mid, -10.0, "冲正")
        self.assertEqual(self.store.api_balance(self.mid), 0.0)
        self.assertEqual(len(self.store.api_ledger(self.mid)), 2)

    def test_topup_total_counts_only_grants(self):
        """累计发放只算正数入账：花掉的钱和冲正的负数都不扣它。"""
        self.assertEqual(self.store.api_topup_total(self.mid), 0.0)
        self.store.api_topup(self.mid, 30.0)
        self.store.charge_api(self.mid, 12.5)
        self.store.api_topup(self.mid, 5.0)
        self.store.api_topup(self.mid, -5.0, "冲正")
        self.assertEqual(self.store.api_topup_total(self.mid), 35.0)
        self.assertEqual(self.store.api_balance(self.mid), 17.5)

    def test_key_kind_defaults_to_plan(self):
        self.store.create_key(member_id=self.mid, note="默认")
        keys = self.store.list_keys(self.mid)
        self.assertEqual(keys[0]["kind"], "plan")

    def test_key_kind_can_be_api(self):
        self.store.create_key(member_id=self.mid, note="预存", kind="api")
        keys = self.store.list_keys(self.mid)
        self.assertEqual(keys[0]["kind"], "api")

    def test_plan_key_denied_when_tier_quota_spent(self):
        kid, _raw = self.store.create_key(member_id=self.mid, kind="plan")
        key = self.store.get_key(kid)
        member = self.store.get_member(self.mid)
        # plus 档日额度 15 元，花掉 16 元后当天就该被拦
        self.store.record_usage(key_id=key["id"], member_id=self.mid,
                                account_id=None, model="m",
                                prompt_tokens=0, completion_tokens=0,
                                cost=16.0)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, member, key)
        self.assertEqual(ctx.exception.code, "quota_daily_exceeded")

    def test_api_key_ignores_tier_quota(self):
        # api 通道不看档位：即使档位额度用光，有余额就还能用
        plan_kid, _ = self.store.create_key(member_id=self.mid, kind="plan")
        plan_key = self.store.get_key(plan_kid)
        self.store.record_usage(key_id=plan_key["id"], member_id=self.mid,
                                account_id=None, model="m",
                                prompt_tokens=0, completion_tokens=0,
                                cost=999.0)
        api_kid, _ = self.store.create_key(member_id=self.mid, kind="api")
        api_key = self.store.get_key(api_kid)
        self.store.api_topup(self.mid, 5.0)
        member = self.store.get_member(self.mid)
        guard.check_quota(self.store, member, api_key)  # 不抛异常

    def test_api_key_denied_when_balance_exhausted(self):
        kid, _ = self.store.create_key(member_id=self.mid, kind="api")
        key = self.store.get_key(kid)
        member = self.store.get_member(self.mid)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, member, key)
        self.assertEqual(ctx.exception.code, "api_balance_exhausted")

    def test_api_status_reports_balance(self):
        self.store.api_topup(self.mid, 8.0)
        member = self.store.get_member(self.mid)
        st = guard.api_quota_status(self.store, member)
        self.assertEqual(st["kind"], "api")
        self.assertEqual(st["balance"], 8.0)
        self.assertFalse(st["exhausted"])


class ModelQuotaTest(unittest.TestCase):
    """按模型的额度：默认跟档位，可单独改小，改小不影响别的模型与历史。"""

    MODELS = {"a": "模型 A", "b": "模型 B"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pmt-model-quota-")
        self.store = store_mod.Store(os.path.join(self.tmp, "t.db"))
        self.mid = self.store.create_member(name="乙", password="pw")
        self.member = self.store.get_member(self.mid)
        self.kid, _ = self.store.create_key(member_id=self.mid, kind="plan")
        self.key = self.store.get_key(self.kid)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spend(self, model: str, cost: float):
        self.store.record_usage(key_id=self.kid, member_id=self.mid,
                                account_id=None, model=model,
                                prompt_tokens=0, completion_tokens=0,
                                cost=cost)

    def test_default_limit_is_the_tier_amount(self):
        # 每个模型默认拿整份档位额度：不加新数字，与改造前的拦截行为一致
        mq = guard.model_quota(self.store, self.member, self.MODELS)
        self.assertEqual(mq["limit_daily"], 15.0)
        self.assertEqual(mq["limit_total"], 75.0)
        for row in mq["models"]:
            self.assertEqual(row["limit_daily"], 15.0)
            self.assertEqual(row["used_total"], 0.0)

    def test_sum_of_percentages_matches_total_spend(self):
        # 默认额度相同时，各模型百分比之和恰好等于总花费占档位的比例
        self._spend("a", 3.0)
        self._spend("b", 1.5)
        mq = guard.model_quota(self.store, self.member, self.MODELS)
        self.assertAlmostEqual(mq["pct_daily"], 30.0, places=2)
        self.assertAlmostEqual(mq["pct_total"], 6.0, places=2)

    def test_smaller_override_blocks_only_that_model(self):
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 1.0}})
        self._spend("模型 A", 1.2)
        self._spend("模型 B", 0.5)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model_quota(self.store, self.member, self.key,
                                    "模型 A")
        self.assertEqual(ctx.exception.code, "model_quota_exceeded")
        # 别的模型不受影响
        guard.check_model_quota(self.store, self.member, self.key, "模型 B")

    def test_override_does_not_touch_history(self):
        self._spend("模型 A", 2.0)
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 0.5}})
        # 历史行原样留着，改额度只影响之后的判断
        rows = self.store.member_model_summary(self.mid)
        self.assertAlmostEqual(rows[0]["cost"], 2.0)
        with self.assertRaises(guard.Denied):
            guard.check_model_quota(self.store, self.member, self.key, "模型 A")

    def test_api_key_skips_model_quota(self):
        # api 通道按余额扣，不参与按模型的额度
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 0.5}})
        self._spend("模型 A", 2.0)
        api_kid, _ = self.store.create_key(member_id=self.mid, kind="api")
        api_key = self.store.get_key(api_kid)
        guard.check_model_quota(self.store, self.member, api_key, "模型 A")

    def test_upstream_key_folds_into_display_name(self):
        # 库里存上游 key，额度按对外名字算，不折就永远匹配不上
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 1.0}})
        self._spend("dfmodel", 1.5)
        with self.assertRaises(guard.Denied):
            guard.check_model_quota(self.store, self.member, self.key, "模型 A",
                                    names={"dfmodel": "模型 A"})

    def test_share_pool_uses_per_model_ratio(self):
        """池子按「本次金额 / 该模型额度」折算，不是按总金额。

        a 额度 ¥100、d 额度 ¥10：a 花 ¥50 占 50%，d 花 ¥4 占 40%，
        池子共 90%，只剩 10%（还能花 ¥10 在 a，或 ¥1 在 d）。
        """
        self.store.update_member(self.mid, limit_daily=100.0,
                                 limit_total=1000.0)
        self.store.set_model_limits(self.mid, {
            "模型 A": {"daily": 100.0}, "模型 D": {"daily": 10.0}})
        self.member = self.store.get_member(self.mid)
        models = {"a": "模型 A", "d": "模型 D"}
        self._spend("模型 A", 50.0)
        st = guard.quota_status(self.store, self.member, models)
        self.assertAlmostEqual(st["pct_today"], 50.0, places=2)
        self._spend("模型 D", 4.0)
        st = guard.quota_status(self.store, self.member, models)
        self.assertAlmostEqual(st["pct_today"], 90.0, places=2)
        # 金额照实报，但剩下的按份额折回来：100 * 10% = ¥10
        self.assertAlmostEqual(st["used_today"], 54.0, places=4)
        self.assertAlmostEqual(st["left_today"], 10.0, places=4)
        # 90% 还没满，通道照常放行
        guard.check_quota(self.store, self.member, self.key, models)
        # 再花 ¥10 到 100%，整个通道停
        self._spend("模型 A", 10.0)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, self.member, self.key, models)
        self.assertEqual(ctx.exception.code, "quota_daily_exceeded")

    def test_unlimited_model_does_not_advance_the_pool(self):
        """额度 0 表示不限：没有分母，花多少都不推进池子。"""
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 0}})
        self.member = self.store.get_member(self.mid)
        self._spend("模型 A", 500.0)
        mq = guard.model_quota(self.store, self.member, self.MODELS)
        row = [r for r in mq["models"] if r["label"] == "模型 A"][0]
        self.assertIsNone(row["pct_daily"])
        # 其余模型仍有额度，池子只按它们算
        self.assertAlmostEqual(mq["pct_daily"], 0.0, places=2)

    def test_no_model_has_a_limit_means_unlimited(self):
        """一个可计入的模型都没有时返回 None，不是 0。"""
        # 只有一个模型，且它的额度是 0：池子里没有任何可计入的项
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 0,
                                                          "total": 0}})
        self.member = self.store.get_member(self.mid)
        one = {"a": "模型 A"}
        st = guard.quota_status(self.store, self.member, one)
        self.assertIsNone(st["pct_today"])
        self.assertIsNone(st["left_today"])
        # 没有额度这回事，不该拦
        guard.check_quota(self.store, self.member, self.key, one)

    def test_limits_are_whole_set_replacement(self):
        self.store.set_model_limits(self.mid, {"模型 A": {"daily": 1.0},
                                               "模型 B": {"total": 2.0}})
        self.assertEqual(set(self.store.model_limits(self.mid)),
                         {"模型 A", "模型 B"})
        self.store.set_model_limits(self.mid, {"模型 B": {"daily": 3.0}})
        got = self.store.model_limits(self.mid)
        self.assertEqual(set(got), {"模型 B"})
        self.assertEqual(got["模型 B"]["limit_daily"], 3.0)


class BillingApiTest(unittest.TestCase):
    """走 Handler 的接口层：tier 校验与充值入参。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pmt-billing-api-")
        self._old = server.STORE
        server.STORE = store_mod.Store(os.path.join(self.tmp, "t.db"))
        self.store = server.STORE
        self.mid = self.store.create_member(name="乙", password="pw")

    def tearDown(self):
        server.STORE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _handler(self, path, body):
        calls = {}

        class Fake(server.Handler):
            def __init__(self):
                self.path = path
                self._body = body

            def _read_json(self):
                return self._body

            def _send_json(self, status, payload):
                calls["status"] = status
                calls["payload"] = payload
                return True

            def _error(self, status, code, message):
                calls["status"] = status
                calls["payload"] = {"error": {"code": code, "message": message}}
                return True

        return Fake(), calls

    def test_topup_route_adds_balance(self):
        h, calls = self._handler(f"/api/members/{self.mid}/topup",
                                 {"amount": 30, "note": "试用"})
        h._topup_member()
        self.assertEqual(calls["status"], 200)
        self.assertEqual(calls["payload"]["balance"], 30.0)

    def test_admin_can_see_topup_total(self):
        """管理页的「累计发放」靠这个字段，没有它只能拿余额冒充。"""
        self.store.api_topup(self.mid, 30.0)
        self.store.charge_api(self.mid, 8.0)
        detail = server.Handler._member_detail(self._handler("", {})[0], self.mid)
        self.assertEqual(detail["api"]["balance"], 22.0)
        self.assertEqual(detail["api"]["topup_total"], 30.0)

    def test_topup_rejects_zero(self):
        h, calls = self._handler(f"/api/members/{self.mid}/topup", {"amount": 0})
        h._topup_member()
        self.assertEqual(calls["status"], 400)
        self.assertEqual(calls["payload"]["error"]["code"], "bad_amount")

    def test_topup_rejects_unknown_member(self):
        h, calls = self._handler("/api/members/9999/topup", {"amount": 10})
        h._topup_member()
        self.assertEqual(calls["status"], 404)

    def test_negative_topup_reverses(self):
        self.store.api_topup(self.mid, 20.0)
        h, calls = self._handler(f"/api/members/{self.mid}/topup", {"amount": -20})
        h._topup_member()
        self.assertEqual(calls["payload"]["balance"], 0.0)


class ModelLimitEditTest(unittest.TestCase):
    """按模型的日 / 周额度是两个独立窗口，改一个不能带动另一个。

    真实事故：给某个模型只设了日限额 20，保存后这个成员所有模型的周限额
    都变成了档位金额 100。原因是「可编辑额度」当时取的是额度树里生效值
    （含档位默认），前端当成管理员设过的数字原样提交回去了。
    """

    MODEL = "Test-Model"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pmt-modelimit-")
        self._old = server.STORE
        server.STORE = store_mod.Store(os.path.join(self.tmp, "t.db"))
        self.store = server.STORE
        self.mid = self.store.create_member(name="丙", password="pw",
                                            tier="plus")
        # 目录要有一个模型，接口才会为它铺出一行可编辑额度
        self._catalog_patch = mock.patch.object(
            server, "all_models", lambda: {self.MODEL.lower(): {
                "id": self.MODEL.lower(), "name": self.MODEL,
                "routes": [{"provider": "qoder", "model": "dfmodel"}]}})
        self._catalog_patch.start()
        self.addCleanup(self._catalog_patch.stop)

    def tearDown(self):
        server.STORE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _editable(self):
        h = server.Handler.__new__(server.Handler)
        return h._member_detail(self.mid)["model_quota_editable"]

    def _edit(self, daily, total):
        h = server.Handler.__new__(server.Handler)
        h.path = f"/api/members/{self.mid}"
        h._body = {"model_limits": {self.MODEL: {"daily": daily,
                                                 "total": total}}}
        h._read_json = lambda: h._body
        h._auth_admin = lambda: True          # 鉴权不在这个用例的关注范围
        sent = {}
        h._send_json = lambda status, payload: sent.update(status=status)
        h._error = lambda status, code, message: sent.update(
            status=status, code=code)
        h.do_PATCH()
        return sent

    def test_untouched_model_reports_no_override(self):
        row = self._editable()[0]
        self.assertFalse(row["overridden"])
        # 没改过的模型不能回数字，否则前端会当成管理员设过的值提交
        self.assertIsNone(row["limit_daily"])
        self.assertIsNone(row["limit_total"])
        # 实际生效值另开字段，详情页要用
        self.assertEqual(row["effective_daily"], 15.0)
        self.assertEqual(row["effective_total"], 75.0)

    def test_daily_only_leaves_weekly_on_tier(self):
        self.assertEqual(self._edit(20, None)["status"], 200)
        row = self._editable()[0]
        self.assertEqual(row["limit_daily"], 20.0)
        self.assertIsNone(row["limit_total"])
        self.assertEqual(row["effective_daily"], 20.0)
        # 周限额仍按档位算，没有被日限额带动
        self.assertEqual(row["effective_total"], 75.0)

    def test_weekly_only_leaves_daily_on_tier(self):
        self._edit(None, 40)
        row = self._editable()[0]
        self.assertIsNone(row["limit_daily"])
        self.assertEqual(row["limit_total"], 40.0)
        self.assertEqual(row["effective_daily"], 15.0)

    def test_windows_are_independent_in_guard(self):
        self._edit(20, None)
        m = self.store.get_member(self.mid)
        rows = guard.model_quota(self.store, m, {self.MODEL.lower(): self.MODEL})
        row = rows["models"][0]
        self.assertEqual(row["limit_daily"], 20.0)
        self.assertEqual(row["limit_total"], 75.0)

    def test_other_models_are_not_affected(self):
        """改一个模型不能把兄弟模型的额度写成自由值。"""
        self._edit(20, None)
        self.assertEqual(set(self.store.model_limits(self.mid)), {self.MODEL})
        only = self.store.model_limits(self.mid)[self.MODEL]
        self.assertEqual(only["limit_daily"], 20.0)
        self.assertIsNone(only["limit_total"])


if __name__ == "__main__":
    unittest.main()
