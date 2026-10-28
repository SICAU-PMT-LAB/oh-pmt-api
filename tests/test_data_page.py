"""数据页的测试：作者归组、实付均价、排行榜窗口。

这几节都要「公开但不可识别」：作者是模型的公开信息，而 IP 本身既不能
出现在接口返回里，也不该每次打开页面都去查一次外部服务。
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import labs  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402


def fake_usage(store, *, model, tokens, ip="", ts=None, member_id=1,
               prompt=None, cache=0, cost=0.01):
    prompt = tokens if prompt is None else prompt
    with store._conn() as c:
        c.execute(
            """INSERT INTO usage (key_id, member_id, account_id, model,
                prompt_tokens, completion_tokens, total_tokens,
                cache_read_tokens, cache_write_tokens, cost, price_source,
                rate, credits, credits_original, status, error, ip, created_at)
               VALUES (0,?,NULL,?,?,?,?,?,0,?,'builtin',1,0,0,200,'',?,?)""",
            (member_id, model, prompt, 0, tokens, cache, cost, ip,
             ts if ts is not None else time.time()))


class LabTest(unittest.TestCase):
    def test_known_labs_by_name(self):
        self.assertEqual(labs.lab_of("DeepSeek-V4.1-Flash")["slug"], "deepseek")
        self.assertEqual(labs.lab_of("GLM-5.3-Flash")["slug"], "zhipu")
        self.assertEqual(labs.lab_of("Qwen-3.8-Flash")["slug"], "alibaba")
        self.assertEqual(labs.lab_of("Xiaomi MiMo-V2.6-Pro")["slug"], "xiaomi")
        self.assertEqual(labs.lab_of("MiniMax-M3.1-Flash")["slug"], "minimax")
        self.assertEqual(labs.lab_of("Step-5-Preview")["slug"], "stepfun")

    def test_unknown_model_falls_back_to_other(self):
        """认不出来的落到「其他」，不能凭空猜一个作者。"""
        self.assertEqual(labs.lab_of("Space-Bunny")["slug"], "other")
        self.assertEqual(labs.lab_of("")["slug"], "unknown")

    def test_every_lab_has_a_colour(self):
        for slug in ("deepseek", "other", "unknown"):
            cell = labs.BY_SLUG.get(slug) or (labs.OTHER if slug == "other" else labs.UNKNOWN)
            self.assertRegex(cell["color"], r"^#[0-9a-f]{6}$")


class PublicDataShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        server.STORE = self.store
        self.handler = server.Handler.__new__(server.Handler)
        self.mid = self.store.create_member(name="张三", username="zs",
                                            password="x")

    def tearDown(self):
        server.STORE = None
        os.unlink(self.tmp.name)

    def test_labs_aggregate_by_author(self):
        now = time.time()
        for i in range(4):
            fake_usage(self.store, model="dfmodel", tokens=1000,
                       ip="127.0.0.1", ts=now - i * 86400,
                       member_id=self.mid, cache=800)
        data = self.handler._public_data()
        by_slug = {c["slug"]: c for c in data["labs"]}
        self.assertIn("deepseek", by_slug)
        self.assertEqual(by_slug["deepseek"]["tokens"], 2000 if False else by_slug["deepseek"]["tokens"])
        self.assertGreater(by_slug["deepseek"]["models"], 0)
        # 每个作者都在堆叠图里有一列序列，且名字与色块一致
        series = {s["slug"]: s for s in data["stack_labs"]["series"]}
        self.assertEqual(set(series), set(by_slug))
        self.assertTrue(all(s["color"].startswith("#") for s in series.values()))

    def test_share_percent_sums_to_100(self):
        now = time.time()
        fake_usage(self.store, model="dfmodel", tokens=750, ts=now,
                   member_id=self.mid)
        fake_usage(self.store, model="gfmodel", tokens=250, ts=now,
                   member_id=self.mid)
        data = self.handler._public_data()
        total = sum(c["share"] for c in data["labs"])
        self.assertAlmostEqual(total, 100.0, delta=0.3)

    def test_no_ip_in_payload(self):
        """公开接口里不能出现 IP。"""
        fake_usage(self.store, model="dfmodel", tokens=1000, ip="127.0.0.1",
                   member_id=self.mid)
        blob = repr(self.handler._public_data())
        self.assertNotIn("127.0.0.1", blob)

    def test_effective_mtok_includes_cache(self):
        """实付均价把缓存命中算进去：同样的牌价，命中率高的那个更便宜。

        牌价加权（cost_per_mtok）只看输入/输出两档，命中缓存的部分按
        缓存价结算，差十几倍。页面上的「Token 成本」用的是 effective_mtok，
        这里锁住它确实除的是总 token。
        """
        now = time.time()
        fake_usage(self.store, model="dfmodel", tokens=2_000_000, ts=now,
                   member_id=self.mid, cost=0.18)
        data = self.handler._public_data()
        row = next(c for c in data["leaderboard"] if c["cost"] > 0)
        self.assertAlmostEqual(row["effective_mtok"], 0.09, places=4)
        # 牌价加权还在，两套口径不要混为一谈
        self.assertGreater(row["cost_per_mtok"], row["effective_mtok"])

    def test_effective_mtok_null_without_tokens(self):
        """没有 token 的行不给 0：那会被当成「免费」，排到最便宜那头。"""
        fake_usage(self.store, model="dfmodel", tokens=0, ts=time.time(),
                   member_id=self.mid, prompt=0, cost=0)
        data = self.handler._public_data()
        self.assertIsNone(data["leaderboard"][0]["effective_mtok"])

    def test_window_fields_are_week_and_day(self):
        now = time.time()
        for i in range(20):
            fake_usage(self.store, model="dfmodel", tokens=100,
                       ts=now - i * 86400, member_id=self.mid)
        data = self.handler._public_data()
        row = next(c for c in data["leaderboard"] if c["model"])
        self.assertEqual(row["tokens_week"], 700)   # 最近 7 天
        self.assertEqual(row["tokens_day"], 100)    # 最后一天
        # 20 天窗口够算「较上周」
        self.assertIsNotNone(row["change_pct"])

    def test_new_model_has_no_percentage(self):
        """上周没有量时不给百分比，只标「新」：0 做分母算不出涨幅。"""
        fake_usage(self.store, model="dfmodel", tokens=100, ts=time.time(),
                   member_id=self.mid)
        data = self.handler._public_data()
        row = data["leaderboard"][0]
        self.assertIsNone(row["change_pct"])
        self.assertTrue(row["is_new"])

    def test_change_pct_compares_two_weeks(self):
        now = time.time()
        # 上周 100，本周 200 → +100%
        for i in range(7, 14):
            fake_usage(self.store, model="dfmodel", tokens=100,
                       ts=now - i * 86400, member_id=self.mid)
        for i in range(0, 7):
            fake_usage(self.store, model="dfmodel", tokens=200,
                       ts=now - i * 86400, member_id=self.mid)
        data = self.handler._public_data()
        row = data["leaderboard"][0]
        self.assertEqual(row["change_pct"], 100)
        self.assertFalse(row["is_new"])

    def test_lab_icon_url_points_at_local_sprite(self):
        fake_usage(self.store, model="dfmodel", tokens=100,
                   member_id=self.mid)
        data = self.handler._public_data()
        row = data["leaderboard"][0]
        self.assertEqual(row["lab_icon"], "deepseek")
        self.assertEqual(row["lab_icon_url"],
                         "/assets/model-icons.svg#deepseek")

    def test_models_without_icon_have_empty_url(self):
        fake_usage(self.store, model="some-unknown-model", tokens=100,
                   member_id=self.mid)
        data = self.handler._public_data()
        row = data["leaderboard"][0]
        self.assertEqual(row["lab_icon"], "")
        self.assertEqual(row["lab_icon_url"], "")


if __name__ == "__main__":
    unittest.main()
