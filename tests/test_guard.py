"""访问控制与存储的测试。"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402
from store import Store  # noqa: E402


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
        """v4 白名单遇到 v6 请求应拒绝，而不是抛 TypeError。"""
        with self.assertRaises(guard.Denied):
            guard.check_ip({"ip_allowlist": "10.0.0.0/8"}, "2001:db8::1")

    def test_invalid_client_ip(self):
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_ip({"ip_allowlist": "10.0.0.0/8"}, "not-an-ip")
        self.assertEqual(ctx.exception.code, "ip_invalid")


class TestModelGuard(unittest.TestCase):
    def test_empty_permits_all(self):
        guard.check_model({"model_allowlist": ""}, "anything")

    def test_exact_match(self):
        guard.check_model({"model_allowlist": "Auto,Ultimate"}, "Ultimate")

    def test_whitespace_tolerated(self):
        guard.check_model({"model_allowlist": " Auto , Ultimate "}, "Auto")

    def test_rejects_other(self):
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model({"model_allowlist": "Auto"}, "Ultimate")
        self.assertEqual(ctx.exception.code, "model_forbidden")


class TestQuota(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _key(self, **kw):
        key_id, _ = self.store.create_key(**kw)
        return self.store.get_key(key_id)

    def test_zero_means_unlimited(self):
        key = self._key()
        self.store.record_usage(key_id=key["id"], account_id=1, model="m",
                                prompt_tokens=10**6, completion_tokens=10**6)
        guard.check_quota(self.store, key)  # 不应抛异常

    def test_daily_limit(self):
        key = self._key(token_limit_daily=100)
        self.store.record_usage(key_id=key["id"], account_id=1, model="m",
                                prompt_tokens=80, completion_tokens=40)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, key)
        self.assertEqual(ctx.exception.code, "quota_daily_exceeded")

    def test_total_limit(self):
        key = self._key(token_limit_total=50)
        self.store.record_usage(key_id=key["id"], account_id=1, model="m",
                                prompt_tokens=30, completion_tokens=30)
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_quota(self.store, key)
        self.assertEqual(ctx.exception.code, "quota_total_exceeded")

    def test_under_limit_passes(self):
        key = self._key(token_limit_daily=1000)
        self.store.record_usage(key_id=key["id"], account_id=1, model="m",
                                prompt_tokens=10, completion_tokens=10)
        guard.check_quota(self.store, key)

    def test_daily_window_excludes_old_usage(self):
        """昨天用超了不应该影响今天的额度。"""
        key = self._key(token_limit_daily=100)
        self.store.record_usage(key_id=key["id"], account_id=1, model="m",
                                prompt_tokens=500, completion_tokens=500)
        with self.store._conn() as c:
            c.execute("UPDATE usage SET created_at=? WHERE key_id=?",
                      (time.time() - 2 * 86400, key["id"]))
        guard.check_quota(self.store, key)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_key_hashed_not_stored_plain(self):
        key_id, raw = self.store.create_key(label="t")
        stored = self.store.get_key(key_id)
        self.assertNotEqual(stored["key_hash"], raw)
        self.assertEqual(stored["key_hash"], Store.hash_key(raw))

    def test_lookup_by_hash(self):
        key_id, raw = self.store.create_key()
        found = self.store.get_key_by_hash(Store.hash_key(raw))
        self.assertEqual(found["id"], key_id)

    def test_unknown_key_returns_none(self):
        self.assertIsNone(self.store.get_key_by_hash(Store.hash_key("qk-nope")))

    def test_account_upsert(self):
        a = self.store.add_account(uid="u1", region="global", pat="pt-a")
        b = self.store.add_account(uid="u1", region="global", pat="pt-b")
        self.assertEqual(len(self.store.list_accounts()), 1)
        self.assertEqual(self.store.get_account(a or b)["pat"], "pt-b")

    def test_same_uid_different_region_coexist(self):
        self.store.add_account(uid="u1", region="global", pat="pt-a")
        self.store.add_account(uid="u1", region="cn", pat="pt-b")
        self.assertEqual(len(self.store.list_accounts()), 2)

    def test_pick_skips_cooldown(self):
        a = self.store.add_account(uid="u1", region="global", pat="pt-a")
        self.store.set_account_cooldown(a, 300, "err")
        self.assertIsNone(self.store.pick_account("global"))

    def test_pick_skips_disabled(self):
        a = self.store.add_account(uid="u1", region="global", pat="pt-a")
        self.store.set_account_enabled(a, False)
        self.assertIsNone(self.store.pick_account("global"))

    def test_pick_prefers_least_recently_used(self):
        a = self.store.add_account(uid="u1", region="global", pat="pt-a")
        b = self.store.add_account(uid="u2", region="global", pat="pt-b")
        self.store.touch_account(a)
        self.assertEqual(self.store.pick_account("global")["id"], b)

    def test_usage_totals(self):
        key_id, _ = self.store.create_key()
        self.store.record_usage(key_id=key_id, account_id=1, model="m",
                                prompt_tokens=10, completion_tokens=5)
        self.store.record_usage(key_id=key_id, account_id=1, model="m",
                                prompt_tokens=20, completion_tokens=5)
        self.assertEqual(self.store.tokens_used_total(key_id), 40)

    def test_update_key_ignores_unknown_fields(self):
        key_id, _ = self.store.create_key()
        self.store.update_key(key_id, label="x", bogus="y")
        self.assertEqual(self.store.get_key(key_id)["label"], "x")


if __name__ == "__main__":
    unittest.main()
