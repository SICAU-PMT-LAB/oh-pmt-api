"""访问控制与存储的测试。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402
from store import Store, day_start, week_start  # noqa: E402


class TestIPGuard(unittest.TestCase):
    def test_empty_allowlist_permits_all(self):
        guard.check_ip({"ip_allowlist": ""}, "8.8.8.8")

    def test_exact_match(self):
        guard.check_ip({"ip_allowlist": "203.0.113.7"}, "203.0.113.7")

    def test_cidr_match(self):
        guard.check_ip({"ip_allowlist": "10.0.0.0/8"}, "10.5.6.7")

    def test_multiple_entries(self):
        spec = {"ip_allowlist": "10.0.0.0/8, 192.168.1.0/24, 203.0.113.7"}
        guard.check_ip(spec, "192.168.1.50")
        guard.check_ip(spec, "203.0.113.7")

    def test_rejects_outside(self):
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_ip({"ip_allowlist": "10.0.0.0/8"}, "8.8.8.8")
        self.assertEqual(ctx.exception.code, "ip_forbidden")
        self.assertEqual(ctx.exception.status, 403)

    def test_ipv6(self):
        guard.check_ip({"ip_allowlist": "2001:db8::/32"}, "2001:db8::1")
        with self.assertRaises(guard.Denied):
            guard.check_ip({"ip_allowlist": "2001:db8::/32"}, "2001:db9::1")

    def test_mixed_family_does_not_crash(self):
        with self.assertRaises(guard.Denied):
            guard.check_ip({"ip_allowlist": "10.0.0.0/8"}, "2001:db8::1")

    def test_invalid_client_ip(self):
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_ip({"ip_allowlist": "10.0.0.0/8"}, "not-an-ip")
        self.assertEqual(ctx.exception.code, "ip_invalid")


class TestModelGuard(unittest.TestCase):
    def test_key_empty_member_empty_allows_all(self):
        self.assertEqual(guard.allowed_models({"model_allowlist": ""}, None), set())

    def test_key_only(self):
        self.assertEqual(guard.allowed_models({"model_allowlist": "a,b"}, None), {"a", "b"})

    def test_member_only(self):
        self.assertEqual(
            guard.allowed_models({"model_allowlist": ""}, {"model_allowlist": "x"}),
            {"x"})

    def test_intersection_when_both_set(self):
        """Key 与成员同时限制时取交集，权限只会更窄。"""
        got = guard.allowed_models(
            {"model_allowlist": "a,b,c"}, {"model_allowlist": "b,c,d"})
        self.assertEqual(got, {"b", "c"})

    def test_rejects_other(self):
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model({"model_allowlist": "a"}, None, "b")
        self.assertEqual(ctx.exception.code, "model_forbidden")

    def test_whitespace_tolerated(self):
        guard.check_model({"model_allowlist": " a , b "}, None, "a")


class QuotaBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _member(self, **kw):
        mid = self.store.create_member(name="t", **kw)
        return self.store.get_member(mid)

    def _spend(self, member_id, credits, when=None):
        self.store.record_usage(key_id=1, member_id=member_id, account_id=1,
                                model="m", prompt_tokens=10, completion_tokens=10,
                                credits=credits)
        if when is not None:
            with self.store._conn() as c:
                c.execute("UPDATE usage SET created_at=? WHERE member_id=?",
                          (when, member_id))


class TestQuota(QuotaBase):
    def test_zero_means_unlimited(self):
        m = self._member()
        self._spend(m["id"], 10.0)
        guard.check_quota(self.store, m)

    def test_daily_limit(self):
        m = self._member(limit_daily=1.0)
        self._spend(m["id"], 1.5)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, m)
        self.assertEqual(ctx.exception.code, "quota_daily_exceeded")

    def test_weekly_limit(self):
        m = self._member(limit_weekly=2.0)
        self._spend(m["id"], 3.0)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, m)
        self.assertEqual(ctx.exception.code, "quota_weekly_exceeded")

    def test_under_limit_passes(self):
        m = self._member(limit_daily=10.0, limit_weekly=50.0)
        self._spend(m["id"], 1.0)
        guard.check_quota(self.store, m)

    def test_yesterday_excluded_from_daily(self):
        """昨天的消耗不该占用今天的额度。"""
        m = self._member(limit_daily=1.0)
        self._spend(m["id"], 100.0, when=time.time() - 3 * 86400)
        guard.check_quota(self.store, m)

    def test_no_member_skips_check(self):
        guard.check_quota(self.store, None)

    def test_quota_status_shape(self):
        m = self._member(limit_daily=5.0, limit_weekly=20.0)
        self._spend(m["id"], 2.0)
        st = guard.quota_status(self.store, m)
        self.assertAlmostEqual(st["used_today"], 2.0, places=4)
        self.assertAlmostEqual(st["left_today"], 3.0, places=4)
        self.assertAlmostEqual(st["left_week"], 18.0, places=4)


class TestTimeWindows(unittest.TestCase):
    def test_day_start_is_midnight(self):
        t = time.localtime(day_start())
        self.assertEqual((t.tm_hour, t.tm_min, t.tm_sec), (0, 0, 0))

    def test_week_start_is_monday(self):
        t = time.localtime(week_start())
        self.assertEqual(t.tm_wday, 0)
        self.assertEqual((t.tm_hour, t.tm_min, t.tm_sec), (0, 0, 0))

    def test_day_start_in_past(self):
        self.assertLess(day_start(), time.time() + 1)

    def test_week_start_not_after_day_start(self):
        self.assertLessEqual(week_start(), day_start())


class TestStore(QuotaBase):
    def test_key_hashed(self):
        kid, raw = self.store.create_key(member_id=1)
        stored = self.store.get_key(kid)
        self.assertNotEqual(stored["key_hash"], raw)
        self.assertEqual(stored["key_hash"], Store.hash_key(raw))

    def test_lookup_by_hash(self):
        kid, raw = self.store.create_key(member_id=1)
        self.assertEqual(self.store.get_key_by_hash(Store.hash_key(raw))["id"], kid)

    def test_unknown_key(self):
        self.assertIsNone(self.store.get_key_by_hash(Store.hash_key("qk-nope")))

    def test_account_upsert(self):
        a = self.store.add_account(uid="u1", region="global", credential="pt-a")
        b = self.store.add_account(uid="u1", region="global", credential="pt-b")
        self.assertEqual(len(self.store.list_accounts()), 1)
        self.assertEqual(a, b)

    def test_account_credits_snapshot(self):
        aid = self.store.add_account(uid="u1", region="global", credential="pt-a")
        self.store.set_account_credits(aid, total=300, used=5, remaining=295,
                                       addon=200, plan="trial")
        acc = self.store.get_account(aid)
        self.assertEqual(acc["credits_total"], 300)
        self.assertEqual(acc["credits_remaining"], 295)
        self.assertEqual(acc["plan_name"], "trial")

    def test_pick_prefers_bound_account(self):
        a = self.store.add_account(uid="u1", region="global", credential="pt-a")
        b = self.store.add_account(uid="u2", region="global", credential="pt-b")
        self.store.touch_account(b)  # b 刚用过
        self.assertEqual(self.store.pick_account("global", prefer_id=a)["id"], a)

    def test_pick_falls_back_when_bound_disabled(self):
        """绑定的账号不可用时回退到其他账号。"""
        a = self.store.add_account(uid="u1", region="global", credential="pt-a")
        b = self.store.add_account(uid="u2", region="global", credential="pt-b")
        self.store.set_account_enabled(a, False)
        self.assertEqual(self.store.pick_account("global", prefer_id=a)["id"], b)

    def test_pick_skips_cooldown(self):
        a = self.store.add_account(uid="u1", region="global", credential="pt-a")
        self.store.set_account_cooldown(a, 300, "err")
        self.assertIsNone(self.store.pick_account("global"))

    def test_pick_excludes_tried_accounts(self):
        """一个账号排队时换别的账号重试，试过的要跳过、试遍了返回 None。"""
        a = self.store.add_account(uid="u1", region="global", credential="pt-a")
        b = self.store.add_account(uid="u2", region="global", credential="pt-b")
        first = self.store.pick_account("global")
        second = self.store.pick_account("global", exclude={first["id"]})
        self.assertNotEqual(first["id"], second["id"])
        self.assertIn(first["id"], (a, b))
        self.assertIsNone(self.store.pick_account("global", exclude={a, b}))

    def test_list_accounts_filters_by_region(self):
        self.store.add_account(uid="u1", region="global", credential="pt-a")
        self.store.add_account(uid="u2", region="cn", credential="pt-b")
        self.assertEqual([x["region"] for x in self.store.list_accounts("cn")], ["cn"])
        self.assertEqual(len(self.store.list_accounts()), 2)

    def test_member_default_models(self):
        mid = self.store.create_member(name="x")
        m = self.store.get_member(mid)
        self.assertEqual(m["model_allowlist"], "qfmodel,gfmodel,dfmodel")

    def test_delete_member_cascades_keys(self):
        mid = self.store.create_member(name="x")
        self.store.create_key(member_id=mid)
        self.store.delete_member(mid)
        self.assertEqual(len(self.store.list_keys()), 0)

    def test_update_member_ignores_unknown(self):
        mid = self.store.create_member(name="x")
        self.store.update_member(mid, name="y", bogus=1)
        self.assertEqual(self.store.get_member(mid)["name"], "y")

    def test_credits_aggregation(self):
        mid = self.store.create_member(name="x")
        self._spend(mid, 1.5)
        self._spend(mid, 2.5)
        self.assertAlmostEqual(self.store.credits_total(mid), 4.0, places=4)

    def test_credits_by_account(self):
        mid = self.store.create_member(name="x")
        self.store.record_usage(key_id=1, member_id=mid, account_id=7, model="m",
                                prompt_tokens=1, completion_tokens=1, credits=3.0)
        self.assertAlmostEqual(self.store.credits_by_account()[7], 3.0, places=4)

    def test_daily_series_length(self):
        self.assertEqual(len(self.store.daily_series(14)), 14)

    def test_daily_series_counts_today(self):
        mid = self.store.create_member(name="x")
        self._spend(mid, 2.0)
        series = self.store.daily_series(3)
        self.assertEqual(series[-1]["requests"], 1)
        self.assertAlmostEqual(series[-1]["credits"], 2.0, places=2)


if __name__ == "__main__":
    unittest.main()


class TestMigration(unittest.TestCase):
    """旧版数据库（Key 级 token 限额）必须能平滑升级到成员+积分模型。"""

    OLD_SCHEMA = """
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, region TEXT NOT NULL,
        email TEXT DEFAULT '', name TEXT DEFAULT '', pat TEXT NOT NULL,
        access_token TEXT DEFAULT '', refresh_token TEXT DEFAULT '',
        expires_at REAL DEFAULT 0, enabled INTEGER DEFAULT 1,
        last_error TEXT DEFAULT '', last_used_at REAL DEFAULT 0,
        cooldown_until REAL DEFAULT 0, created_at REAL NOT NULL,
        UNIQUE(uid, region));
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL, label TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
        token_limit_daily INTEGER DEFAULT 0, token_limit_total INTEGER DEFAULT 0,
        ip_allowlist TEXT DEFAULT '', model_allowlist TEXT DEFAULT '',
        created_at REAL NOT NULL);
    CREATE TABLE usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER, account_id INTEGER,
        model TEXT DEFAULT '', prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
        status INTEGER DEFAULT 200, ip TEXT DEFAULT '', created_at REAL NOT NULL);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        conn = sqlite3.connect(self.tmp.name)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute(
            "INSERT INTO accounts (uid,region,pat,created_at) VALUES ('u1','global','pt-x',0)")
        conn.execute(
            "INSERT INTO api_keys (key_hash,key_prefix,created_at) VALUES ('h','qk-old',0)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_opens_without_error(self):
        store = Store(self.tmp.name)
        self.assertEqual(len(store.list_accounts()), 1)

    def test_new_columns_present(self):
        store = Store(self.tmp.name)
        acc = store.list_accounts()[0]
        self.assertEqual(acc["credits_remaining"], 0)
        self.assertIn("member_id", store.list_keys()[0])

    def test_can_use_new_features_after_migration(self):
        store = Store(self.tmp.name)
        mid = store.create_member(name="新人")
        aid = store.list_accounts()[0]["id"]
        store.set_account_credits(aid, total=300, used=5, remaining=295, addon=0)
        store.record_usage(key_id=1, member_id=mid, account_id=aid, model="m",
                           prompt_tokens=1, completion_tokens=1, credits=2.5)
        self.assertAlmostEqual(store.credits_total(mid), 2.5, places=4)

    def test_idempotent(self):
        Store(self.tmp.name)
        Store(self.tmp.name)  # 二次打开不应报错
