"""账号额度构成、签到选活动、请求成功率口径的测试。

这些数字直接显示在管理面板上，算错了用户看不出来，所以逐项钉住。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import qoder  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402


class QuotaCompositionTest(unittest.TestCase):
    """「账号的全部额度」必须把套餐内、赠送、共享池都算上。"""

    def _quota(self, payload):
        with mock.patch.object(qoder, "_request", lambda *a, **k: (200, payload)):
            return qoder.fetch_quota({"region": "global", "access_token": "t"})

    def test_addon_counted_in_total(self):
        """免费号套餐是 0，只有赠送额度——这是最容易被漏算的一类。"""
        q = self._quota(b"""{"userType":"personal_standard","isQuotaExceeded":false,
            "userQuota":{"total":0,"used":0,"remaining":0},
            "addOnQuota":{"total":100,"used":0,"remaining":100}}""")
        self.assertEqual(q["total"], 100)
        self.assertEqual(q["remaining"], 100)
        self.assertEqual(q["base_total"], 0)
        self.assertEqual(q["addon_total"], 100)

    def test_base_and_addon_sum(self):
        q = self._quota(b"""{"userType":"pro","userQuota":{"total":300,"used":5,"remaining":295},
            "addOnQuota":{"total":200,"used":20,"remaining":180}}""")
        self.assertEqual(q["total"], 500)
        self.assertEqual(q["used"], 25)
        self.assertEqual(q["remaining"], 475)

    def test_shared_pool_counted(self):
        q = self._quota(b"""{"userQuota":{"total":0,"used":0,"remaining":0},
            "orgResourcePackage":{"cap":2500,"used":1548,"remaining":952,"available":true}}""")
        self.assertEqual(q["shared_total"], 2500)
        self.assertEqual(q["shared_remaining"], 952)
        self.assertEqual(q["remaining"], 952)

    def test_unlimited_shared_pool_ignored(self):
        """cap=-1 是不限量哨兵值，不能当成真实额度显示。"""
        q = self._quota(b"""{"userQuota":{"total":0,"used":0,"remaining":0},
            "orgResourcePackage":{"cap":-1,"used":0,"remaining":999}}""")
        self.assertEqual(q["shared_total"], 0)
        self.assertEqual(q["remaining"], 0)

    def test_expire_placeholder_cleared(self):
        """253402214400000 是「永不过期」，要归零否则界面显示 9999 年。"""
        q = self._quota(b"""{"userQuota":{"total":1,"used":0,"remaining":1},
            "addOnQuota":{},"expiresAt":253402214400000}""")
        self.assertEqual(q["expire_at"], 0)

    def test_expire_parsed_from_millis(self):
        q = self._quota(b"""{"userQuota":{"total":1,"used":0,"remaining":1},
            "expiresAt":1793875689974}""")
        self.assertAlmostEqual(q["expire_at"], 1793875689.974, places=2)


SAMPLE_CAMPAIGNS = {
    "showCampaign": True,
    "campaigns": [
        # 纯展示类活动：没有 reward，不算签到项
        {"campaignId": "v1", "campaignKey": "boost", "actionType": "VIEW_DETAILS",
         "claimStatus": "CLAIMABLE", "placements": []},
        # 已领的积分活动
        {"campaignId": "c-done", "campaignKey": "daily", "actionType": "CLAIM_BENEFIT",
         "claimStatus": "CLAIMED", "benefit": {"kind": "CREDITS", "amount": 100}},
        # 可领的积分活动
        {"campaignId": "c-new", "campaignKey": "daily2", "actionType": "CLAIM_BENEFIT",
         "claimStatus": "CLAIMABLE", "benefit": {"kind": "CREDITS", "amount": 100}},
        # 兑换码类，不归签到管
        {"campaignId": "c-code", "campaignKey": "coupon", "actionType": "CLAIM_BENEFIT",
         "claimStatus": "CLAIMABLE", "benefit": {"kind": "REDEMPTION_CODE", "amount": 0}},
    ],
}


class CheckinCampaignPickTest(unittest.TestCase):
    def test_picks_only_credits_claimable(self):
        with mock.patch.object(qoder, "list_campaigns", lambda a: SAMPLE_CAMPAIGNS):
            state = qoder.claimable_campaigns({"region": "global"})
        self.assertEqual([c["id"] for c in state["claimable"]], ["c-new"])
        self.assertEqual([c["id"] for c in state["claimed"]], ["c-done"])
        self.assertFalse(state["done"])

    def test_done_when_all_claimed(self):
        payload = {"showCampaign": True, "campaigns": [
            {"campaignId": "c1", "campaignKey": "d", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "CLAIMED", "benefit": {"kind": "CREDITS", "amount": 100}}]}
        with mock.patch.object(qoder, "list_campaigns", lambda a: payload):
            state = qoder.claimable_campaigns({"region": "global"})
        self.assertTrue(state["done"])
        self.assertEqual(state["claimable"], [])

    def test_hidden_list_reported(self):
        """showCampaign=false 说明上游没给列表，要如实说明而不是报「今日已签」。"""
        with mock.patch.object(qoder, "list_campaigns",
                               lambda a: {"showCampaign": False, "campaigns": []}):
            state = qoder.claimable_campaigns({"region": "global"})
        self.assertIn("未返回活动列表", state["message"])

    def test_checkin_claims_and_reports_declared(self):
        """到账金额要看额度差额（server 层算），这里只报领取了几个活动。"""
        with mock.patch.object(qoder, "list_campaigns", lambda a: SAMPLE_CAMPAIGNS), \
             mock.patch.object(qoder, "claim_campaign",
                               lambda a, cid: {"status": "CLAIMED", "replayed": False}):
            res = qoder.checkin_account({"region": "global"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["claimed"], 1)
        self.assertEqual(res["declared"], 100)
        self.assertTrue(res["done"])

    def test_replayed_claim_counts_nothing(self):
        """服务端幂等返回 replayed=true 时不能算成新领取。"""
        with mock.patch.object(qoder, "list_campaigns", lambda a: SAMPLE_CAMPAIGNS), \
             mock.patch.object(qoder, "claim_campaign",
                               lambda a, cid: {"status": "CLAIMED", "replayed": True}):
            res = qoder.checkin_account({"region": "global"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["claimed"], 0)
        self.assertEqual(res["declared"], 0)

    def test_already_checked_in(self):
        payload = {"showCampaign": True, "campaigns": [
            {"campaignId": "c1", "campaignKey": "d", "actionType": "CLAIM_BENEFIT",
             "claimStatus": "CLAIMED", "benefit": {"kind": "CREDITS", "amount": 100}}]}
        with mock.patch.object(qoder, "list_campaigns", lambda a: payload):
            res = qoder.checkin_account({"region": "global"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["earned"], 0)
        self.assertEqual(res["message"], "今日已签到")


class CheckinEarnedTest(unittest.TestCase):
    """到账金额必须按额度差额算，不能照抄活动面额。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patch = mock.patch.object(server, "get_store", lambda: self.store)
        self.patch.start()
        self.patch2 = mock.patch.object(server, "ensure_fresh_token", lambda a: a)
        self.patch2.start()
        self.aid = self.store.add_account(uid="u1", region="global", credential="pt-a",
                                          name="A")

    def tearDown(self):
        self.patch2.stop()
        self.patch.stop()
        os.unlink(self.tmp.name)

    def _run(self, before_remaining, after_remaining, result):
        self.store.set_account_credits(self.aid, total=300, used=0,
                                       remaining=before_remaining, addon=0)
        acc = self.store.get_account(self.aid)
        with mock.patch.object(server.qoder, "checkin_account", lambda a: result), \
             mock.patch.object(server, "sync_account_credits",
                               lambda a: {"remaining": after_remaining}):
            return server.checkin_one(acc)

    def test_earned_from_remaining_delta(self):
        res = self._run(200, 300, {"ok": True, "earned": 0, "declared": 100,
                                   "claimed": 1, "done": True,
                                   "message": "已领取 1 个活动"})
        self.assertEqual(res["earned"], 100)
        self.assertIn("到账 100", res["message"])
        self.assertEqual(self.store.get_account(self.aid)["checkin_earned"], 100)

    def test_no_delta_says_not_credited(self):
        """领取成功但额度没动要如实说，不能把面额当到账。"""
        res = self._run(200, 200, {"ok": True, "earned": 0, "declared": 100,
                                    "claimed": 1, "done": True,
                                    "message": "已领取 1 个活动"})
        self.assertEqual(res["earned"], 0)
        self.assertIn("暂未变化", res["message"])

    def test_already_checked_in_earns_nothing(self):
        res = self._run(200, 200, {"ok": True, "earned": 0, "declared": 0,
                                    "claimed": 0, "done": True,
                                    "message": "今日已签到"})
        self.assertEqual(res["earned"], 0)
        self.assertEqual(res["message"], "今日已签到")

    def test_failed_checkin_recorded(self):
        res = self._run(200, 200, {"ok": False, "earned": 0, "declared": 0,
                                   "claimed": 0, "done": False,
                                   "message": "活动列表查询失败"})
        self.assertFalse(res["ok"])
        acc = self.store.get_account(self.aid)
        self.assertEqual(acc["checkin_ok"], 0)
        self.assertIn("失败", acc["checkin_message"])


class UsageStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.mid = self.store.create_member(name="张三")
        self.kid, _ = self.store.create_key(member_id=self.mid)
        self.store.add_account(uid="u1", region="global", credential="pt-a")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _usage(self, status=200, error="", model="gfmodel", credits=1.0,
               prompt=10, completion=5, account_id=1):
        self.store.record_usage(
            key_id=self.kid, member_id=self.mid, account_id=account_id,
            model=model, prompt_tokens=prompt, completion_tokens=completion,
            credits=credits, status=status, error=error)

    def test_success_rate(self):
        self._usage()
        self._usage()
        self._usage(status=502, error="upstream_error")
        t = self.store.usage_totals(0)
        self.assertEqual(t["requests"], 3)
        self.assertEqual(t["ok"], 2)
        self.assertEqual(t["errors"], 1)
        self.assertAlmostEqual(t["success_rate"], 66.67, places=2)

    def test_success_rate_none_without_requests(self):
        self.assertIsNone(self.store.usage_totals(0)["success_rate"])

    def test_failed_request_keeps_no_tokens(self):
        """失败的请求没有 token，不能混进 Token 合计里虚增用量。"""
        self._usage(prompt=100, completion=50)
        self._usage(status=429, error="upstream_busy", prompt=0, completion=0)
        t = self.store.usage_totals(0)
        self.assertEqual(t["tokens"], 150)

    def test_model_summary_splits_by_model(self):
        self._usage(model="gfmodel", credits=1.0)
        self._usage(model="dfmodel", credits=0.25)
        rows = {r["model"]: r for r in self.store.model_summary(0)}
        self.assertEqual(rows["gfmodel"]["n"], 1)
        self.assertAlmostEqual(rows["dfmodel"]["credits"], 0.25)

    def test_account_summary_groups_and_nests_models(self):
        self._usage(model="gfmodel", credits=1.0)
        self._usage(model="dfmodel", credits=0.5)
        self._usage(model="gfmodel", credits=0.5, account_id=2)
        stats = self.store.account_summary(0)
        self.assertEqual(stats[1]["requests"], 2)
        self.assertAlmostEqual(stats[1]["credits"], 1.5)
        self.assertEqual(len(stats[1]["by_model"]), 2)
        self.assertEqual(stats[2]["requests"], 1)

    def test_member_model_summary(self):
        self._usage(model="gfmodel", credits=1.0)
        rows = self.store.member_model_summary(self.mid, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "gfmodel")
        self.assertEqual(rows[0]["tokens"], 15)


class AccountViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patch = mock.patch.object(server, "get_store", lambda: self.store)
        self.patch.start()
        self.account_id = self.store.add_account(uid="u1", region="global", credential="pt-a")

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.tmp.name)

    def test_view_exposes_credit_breakdown_and_stats(self):
        self.store.set_account_credits(
            self.account_id, total=500, used=25, remaining=475, addon=180,
            base_total=300, base_used=5, base_remaining=295,
            addon_total=200, addon_used=20,
            expire_at=1793875689, exceeded=False)
        mid = self.store.create_member(name="m")
        kid, _ = self.store.create_key(member_id=mid)
        self.store.record_usage(key_id=kid, member_id=mid, account_id=self.account_id,
                                model="gfmodel", prompt_tokens=100,
                                completion_tokens=50, credits=0.5)
        a = server.Handler._accounts_view(mock.Mock())[0]
        self.assertEqual(a["credits_total"], 500)
        self.assertEqual(a["credits_base_remaining"], 295)
        self.assertEqual(a["credits_addon_remaining"], 180)
        self.assertEqual(a["requests"], 1)
        self.assertEqual(a["tokens"], 150)
        self.assertAlmostEqual(a["gateway_consumed"], 0.5)
        self.assertEqual(a["by_model"][0]["id"], "gfmodel")

    def test_checkin_state_persisted(self):
        self.store.set_account_checkin(self.account_id, ok=True, earned=100,
                                       message="签到成功，获得 100 积分")
        a = server.Handler._accounts_view(mock.Mock())[0]
        self.assertTrue(a["checkin_ok"])
        self.assertEqual(a["checkin_earned"], 100)
        self.assertIsNotNone(a["checkin_at"])


class MemberViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patch = mock.patch.object(server, "get_store", lambda: self.store)
        self.patch.start()
        self.mid = self.store.create_member(name="张三")
        self.kid, _ = self.store.create_key(member_id=self.mid)
        self.store.add_account(uid="u1", region="global", credential="pt-a")

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.tmp.name)

    def test_member_view_has_per_model_and_success_rate(self):
        for status, error in ((200, ""), (200, ""), (502, "upstream_error")):
            self.store.record_usage(
                key_id=self.kid, member_id=self.mid, account_id=1, model="gfmodel",
                prompt_tokens=10, completion_tokens=5, credits=0.5,
                status=status, error=error)
        m = server.Handler._members_view(mock.Mock())[0]
        self.assertEqual(m["requests"], 3)
        self.assertEqual(m["errors"], 1)
        self.assertAlmostEqual(m["success_rate"], 66.67, places=2)
        self.assertEqual(m["tokens"], 45)      # 3 次 × 15
        self.assertEqual(m["by_model"][0]["requests"], 3)
        self.assertEqual(m["by_model"][0]["prompt_tokens"], 30)


class AnalyticsTotalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patches = [
            mock.patch.object(server, "get_store", lambda: self.store),
            mock.patch.object(server, "all_models", lambda: {}),
        ]
        for p in self.patches:
            p.start()
        self.mid = self.store.create_member(name="张三")
        self.kid, _ = self.store.create_key(member_id=self.mid)
        self.store.add_account(uid="u1", region="global", credential="pt-a")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.unlink(self.tmp.name)

    def test_analytics_reports_success_rate_and_tokens(self):
        self.store.record_usage(key_id=self.kid, member_id=self.mid, account_id=1,
                                model="gfmodel", prompt_tokens=100,
                                completion_tokens=50, credits=0.5)
        self.store.record_usage(key_id=self.kid, member_id=self.mid, account_id=1,
                                model="gfmodel", prompt_tokens=0,
                                completion_tokens=0, status=502,
                                error="upstream_error")
        a = server.Handler._analytics(mock.Mock())
        self.assertEqual(a["totals"]["requests"], 2)
        self.assertEqual(a["totals"]["errors"], 1)
        self.assertAlmostEqual(a["totals"]["success_rate"], 50.0)
        self.assertEqual(a["totals"]["tokens"], 150)
        self.assertEqual(a["by_model"][0]["requests"], 2)
        self.assertEqual(a["by_model"][0]["tokens"], 150)
        self.assertAlmostEqual(a["by_account"][0]["success_rate"], 50.0)


if __name__ == "__main__":
    unittest.main()
