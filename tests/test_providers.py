"""多厂商账号模型：存储迁移、厂商注册表、账号路由的分组。

这几处的错误都是静默的——老库迁移错了会在下次启动时报错，
厂商过滤错了会把请求打到别家的账号上——所以逐项钉住。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import providers  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402


class ProviderRegistryTest(unittest.TestCase):
    def test_all_four_vendors_registered(self):
        slugs = {p.slug for p in providers.PROVIDERS.values()}
        self.assertEqual(slugs, {"qoder", "workbuddy", "cline", "commandcode"})

    def test_only_qoder_available(self):
        self.assertTrue(providers.get("qoder").available)
        for slug in ("workbuddy", "cline", "commandcode"):
            self.assertFalse(providers.get(slug).available, slug)

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(providers.get("nope").slug, "qoder")
        self.assertEqual(providers.get(None).slug, "qoder")

    def test_pending_provider_refuses_connect(self):
        with self.assertRaises(providers.Unsupported):
            providers.get("cline").connect("key", "default")

    def test_qoder_exposes_both_regions(self):
        q = providers.get("qoder")
        self.assertIn("global", q.regions)
        self.assertIn("cn", q.regions)

    def test_describe_is_json_friendly(self):
        row = providers.get("qoder").describe()
        self.assertEqual(row["credential_prefix"], "pt-")
        self.assertEqual([r["id"] for r in row["regions"]], ["global", "cn"])
        self.assertTrue(row["supports_checkin"])


class MultiProviderStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_same_uid_different_provider_is_separate_account(self):
        a = self.store.add_account(provider="qoder", uid="u1", region="global",
                                   credential="pt-a")
        b = self.store.add_account(provider="cline", uid="u1", region="default",
                                   credential="ck-a")
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.store.list_accounts()), 2)

    def test_same_provider_uid_region_upserts(self):
        a = self.store.add_account(provider="qoder", uid="u1", region="global",
                                   credential="pt-a")
        b = self.store.add_account(provider="qoder", uid="u1", region="global",
                                   credential="pt-b")
        self.assertEqual(a, b)
        self.assertEqual(self.store.get_account(a)["credential"], "pt-b")

    def test_list_filters_by_provider(self):
        self.store.add_account(provider="qoder", uid="u1", region="global",
                               credential="pt-a")
        self.store.add_account(provider="cline", uid="u2", region="default",
                               credential="ck-b")
        self.assertEqual(len(self.store.list_accounts(provider="cline")), 1)
        self.assertEqual(len(self.store.list_accounts(region="global")), 1)

    def test_pick_account_never_crosses_provider(self):
        self.store.add_account(provider="qoder", uid="u1", region="global",
                               credential="pt-a")
        self.assertIsNone(self.store.pick_account("global", provider="cline"))

    def test_pick_prefers_same_provider_even_if_other_has_idle(self):
        q = self.store.add_account(provider="qoder", uid="u1", region="global",
                                   credential="pt-a")
        self.store.add_account(provider="cline", uid="u2", region="global",
                               credential="ck-b")
        self.store.touch_account(q)
        picked = self.store.pick_account("global", provider="cline")
        self.assertEqual(self.store.get_account(picked["id"])["provider"], "cline")


class LegacyMigrationTest(unittest.TestCase):
    """单厂商时代的库必须能升级成 provider + credential 结构。"""

    OLD = """
    CREATE TABLE accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, region TEXT NOT NULL,
        email TEXT DEFAULT '', name TEXT DEFAULT '', pat TEXT NOT NULL,
        access_token TEXT DEFAULT '', refresh_token TEXT DEFAULT '',
        expires_at REAL DEFAULT 0, enabled INTEGER DEFAULT 1,
        last_error TEXT DEFAULT '', last_used_at REAL DEFAULT 0,
        cooldown_until REAL DEFAULT 0, created_at REAL NOT NULL,
        UNIQUE(uid, region));
    """

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        conn = sqlite3.connect(self.tmp.name)
        conn.executescript(self.OLD)
        conn.execute("INSERT INTO accounts (uid,region,pat,email,name,created_at) "
                     "VALUES ('u9','cn','pt-legacy','x@example.com','老账号',123)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_pat_becomes_credential_and_provider_defaults(self):
        store = Store(self.tmp.name)
        acc = store.list_accounts()[0]
        self.assertEqual(acc["provider"], "qoder")
        self.assertEqual(acc["credential"], "pt-legacy")
        self.assertEqual(acc["email"], "x@example.com")
        self.assertEqual(acc["created_at"], 123)

    def test_migrated_account_is_usable(self):
        store = Store(self.tmp.name)
        acc = store.list_accounts()[0]
        self.assertEqual(store.pick_account("cn")["id"], acc["id"])

    def test_migration_is_idempotent(self):
        Store(self.tmp.name)
        store = Store(self.tmp.name)
        self.assertEqual(len(store.list_accounts()), 1)


class LegacyDbAdoptionTest(unittest.TestCase):
    """仓库改名前叫 qoder2api，旧库文件名要被接过来。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(server, "DATA_DIR", self.dir),
            mock.patch.object(server, "DB_PATH", os.path.join(self.dir, "oh-pmt-api.db")),
            mock.patch.object(server, "STORE", None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def test_old_db_file_moved(self):
        legacy = os.path.join(self.dir, "qoder2api.db")
        with open(legacy, "wb") as f:
            f.write(b"x")
        server._adopt_old_db()
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "oh-pmt-api.db")))

    def test_existing_new_db_wins(self):
        legacy = os.path.join(self.dir, "qoder2api.db")
        current = os.path.join(self.dir, "oh-pmt-api.db")
        for path, body in ((legacy, b"old"), (current, b"new")):
            with open(path, "wb") as f:
                f.write(body)
        server._adopt_old_db()
        with open(current, "rb") as f:
            self.assertEqual(f.read(), b"new")

    def test_nothing_to_do(self):
        server._adopt_old_db()   # 两个文件都没有时不应报错


class AccountViewProviderTest(unittest.TestCase):
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

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.unlink(self.tmp.name)

    def test_view_carries_provider_metadata(self):
        self.store.add_account(provider="qoder", uid="u1", region="global",
                               credential="pt-a")
        row = server.Handler._accounts_view(mock.Mock())[0]
        self.assertEqual(row["provider"], "qoder")
        self.assertEqual(row["provider_label"], "Qoder")
        self.assertEqual(row["region_label"], "Qoder 国际版")
        self.assertTrue(row["supports_checkin"])

    def test_provider_stats_groups_by_vendor(self):
        self.store.add_account(provider="qoder", uid="u1", region="global",
                               credential="pt-a", name="A")
        self.store.set_account_credits(1, total=100, used=40, remaining=60, addon=0)
        stats = {s["slug"]: s for s in server.Handler._provider_stats(
            self.store.list_accounts())}
        self.assertEqual(stats["qoder"]["accounts"], 1)
        self.assertEqual(stats["qoder"]["credits_remaining"], 60)
        self.assertEqual(stats["cline"]["accounts"], 0)
        # available 在 describe() 里表示「协议已实现」，账号可用数用另一个键
        self.assertFalse(stats["cline"]["available"])
        self.assertTrue(stats["qoder"]["available"])
        self.assertEqual(stats["qoder"]["available_accounts"], 1)

    def test_detail_view_masks_credential(self):
        aid = self.store.add_account(provider="qoder", uid="u1", region="global",
                                     credential="pt-supersecretvalue")
        detail = server.Handler._account_detail(mock.Mock(), aid)
        self.assertNotIn("supersecretvalue", detail["credential_masked"])
        self.assertTrue(detail["credential_masked"].startswith("pt-"))
        self.assertEqual(detail["provider"], "qoder")
        self.assertIn("totals", detail)

    def test_detail_view_missing_account(self):
        self.assertEqual(server.Handler._account_detail(mock.Mock(), 999), {})


class UpsertKeyTest(unittest.TestCase):
    """成员的可用模型是逗号串；前端改成点选后仍要落成同样格式。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_allowlist_round_trips(self):
        mid = self.store.create_member(name="m", model_allowlist="qfmodel,gfmodel")
        self.store.update_member(mid, model_allowlist="dfmodel")
        self.assertEqual(self.store.get_member(mid)["model_allowlist"], "dfmodel")

    def test_empty_allowlist_means_unrestricted(self):
        mid = self.store.create_member(name="m", model_allowlist="")
        self.assertEqual(self.store.get_member(mid)["model_allowlist"], "")


if __name__ == "__main__":
    unittest.main()
