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

    def test_only_implemented_vendors_available(self):
        # qoder 与 workbuddy 有真实协议；cline / commandcode 仍是占位
        self.assertTrue(providers.get("qoder").available)
        self.assertTrue(providers.get("workbuddy").available)
        for slug in ("cline", "commandcode"):
            self.assertFalse(providers.get(slug).available, slug)

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(providers.get("nope").slug, "qoder")
        self.assertEqual(providers.get(None).slug, "qoder")

    def test_pending_provider_refuses_connect(self):
        with self.assertRaises(providers.Unsupported):
            providers.get("cline").connect("key", "default")

    def test_workbuddy_exposes_both_regions(self):
        wb = providers.get("workbuddy")
        self.assertIn("cn", wb.regions)
        self.assertIn("global", wb.regions)
        # 两个区域的凭证互不通用，签到只有国内版有
        self.assertTrue(wb.supports_checkin)
        row = wb.describe()
        self.assertEqual([r["id"] for r in row["regions"]], ["cn", "global"])
        # 凭证是导出的 JSON 文件，没有统一前缀，也没有「去这里申请」的地址
        self.assertEqual(row["credential_prefix"], "")
        self.assertEqual(row["credential_url"], "")

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


class WorkbuddyAdapterTest(unittest.TestCase):
    """WorkBuddy 的凭证是导出的文件，两种格式都要认，区域以文件里的 domain 为准。"""

    WORKBUDDY_FILE = (
        '{"account": {"uid": "u-1", "nickname": "159****8770"},'
        ' "auth": {"accessToken": "at", "refreshToken": "rt",'
        ' "expiresAt": 1795486350000, "domain": "www.codebuddy.cn"}}'
    )
    COCKPIT_FILE = '[' + WORKBUDDY_FILE + ']'

    def test_parse_single_file(self):
        import workbuddy
        info = workbuddy.parse_credential(self.WORKBUDDY_FILE)
        self.assertEqual(info["uid"], "u-1")
        self.assertEqual(info["access_token"], "at")
        self.assertEqual(info["refresh_token"], "rt")
        self.assertEqual(info["region"], "cn")
        # 毫秒时间戳要归一成秒
        self.assertEqual(int(info["expires_at"]), 1795486350)

    def test_parse_cockpit_array(self):
        import workbuddy
        info = workbuddy.parse_credential(self.COCKPIT_FILE)
        self.assertEqual(info["uid"], "u-1")

    def test_global_domain_maps_to_global_region(self):
        import workbuddy
        text = self.WORKBUDDY_FILE.replace("www.codebuddy.cn", "www.workbuddy.ai")
        self.assertEqual(workbuddy.parse_credential(text)["region"], "global")

    def test_empty_or_broken_file_is_rejected(self):
        import workbuddy
        with self.assertRaises(workbuddy.UpstreamError):
            workbuddy.parse_credential("")
        with self.assertRaises(workbuddy.UpstreamError):
            workbuddy.parse_credential("{not json")

    def test_developer_role_becomes_system(self):
        import json as _json
        import workbuddy
        body = workbuddy.build_chat_body(
            [{"role": "developer", "content": "x"},
             {"role": "user", "content": "y"}], "m")
        self.assertEqual([m["role"] for m in _json.loads(body)["messages"]],
                         ["system", "user"])

    def test_tool_choice_object_becomes_name(self):
        import json as _json
        import workbuddy
        body = workbuddy.build_chat_body(
            [{"role": "user", "content": "y"}], "m",
            parameters={"tool_choice": {"type": "function",
                                        "function": {"name": "ping"}}})
        self.assertEqual(_json.loads(body)["tool_choice"], "ping")

    def test_brand_leadin_scrubbed_but_body_kept(self):
        import workbuddy
        text = ("You are Claude Code, Anthropic's official CLI for Claude.\n"
                "# Tone\nBe concise.")
        out = workbuddy._scrub_brand_claims(text)
        self.assertNotIn("Claude Code", out)
        self.assertIn("Be concise.", out)
        # 只改开头那一段，正文里的其它内容不能动
        self.assertEqual(workbuddy._scrub_brand_claims("Be concise."), "Be concise.")

    def test_machine_id_is_stable_per_account(self):
        import workbuddy
        a = workbuddy._machine_id("u-1")
        self.assertEqual(a, workbuddy._machine_id("u-1"))
        self.assertNotEqual(a, workbuddy._machine_id("u-2"))

    def test_catalog_entries_are_complete(self):
        import workbuddy
        models = workbuddy.list_models({})
        self.assertTrue(models)
        for m in models:
            self.assertTrue(m["id"])
            self.assertTrue(m["name"])
            self.assertGreater(m["context_length"], 0)


class WorkbuddyQuotaShapeTest(unittest.TestCase):
    """额度分组要归成三块：套餐 = plan_base + plan_bonus，赠送 = addon + benefit。"""

    def test_groups_are_folded(self):
        import workbuddy
        payload = {
            "code": 0,
            "data": {
                "TotalCredits": "2100",
                "SubscriptionPackageName": "体验版",
                "CreditGroups": [
                    {"Kind": "plan_base", "CycleTotalCapacity": "500",
                     "CycleUsedCapacity": "100"},
                    {"Kind": "plan_bonus", "CycleTotalCapacity": "100",
                     "CycleUsedCapacity": "0"},
                    {"Kind": "addon", "CycleTotalCapacity": "200",
                     "CycleUsedCapacity": "50"},
                    {"Kind": "benefit", "CycleTotalCapacity": "1300",
                     "CycleUsedCapacity": "0"},
                ],
            },
        }
        import json as _json
        with mock.patch.object(workbuddy, "_request",
                               return_value=(200, _json.dumps(payload).encode())):
            q = workbuddy.fetch_quota({"region": "cn", "access_token": "t"})
        self.assertEqual(q["base_total"], 600)
        self.assertEqual(q["base_used"], 100)
        self.assertEqual(q["addon_total"], 1500)
        self.assertEqual(q["addon_used"], 50)
        self.assertEqual(q["total"], 2100)
        self.assertEqual(q["used"], 150)
        self.assertEqual(q["remaining"], 1950)
        self.assertEqual(q["plan"], "体验版")

    def test_upstream_error_surfaces(self):
        import workbuddy
        with mock.patch.object(workbuddy, "_request",
                               return_value=(403, b'{"code":1}')):
            with self.assertRaises(workbuddy.UpstreamError):
                workbuddy.fetch_quota({"region": "cn", "access_token": "t"})


class WorkbuddySsePassThroughTest(unittest.TestCase):
    """WorkBuddy 的 SSE 没有信封，帧本身就是 OpenAI chunk。"""

    def test_plain_chunks_pass_through(self):
        import convert
        lines = [
            'data: {"id":"c1","choices":[{"delta":{"content":"你"}}]}',
            '',
            'data: {"id":"c1","choices":[{"delta":{"content":"好"}}],'
            '"usage":{"prompt_tokens":3,"completion_tokens":1,"credit":0.5}}',
            'data: [DONE]',
        ]
        holder = {}
        chunks = list(convert.iter_chunks(iter(lines), holder))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(holder["credits"], 0.5)
        out = convert.aggregate(iter(lines), "m", {})
        self.assertEqual(out["choices"][0]["message"]["content"], "你好")

    def test_qoder_envelope_still_works(self):
        import convert
        inner = '{"choices":[{"delta":{"content":"hi"}}],"usage":{"credits":1.5}}'
        lines = ['data: {"body": %s, "statusCodeValue": 200}'
                 % __import__("json").dumps(inner)]
        holder = {}
        chunks = list(convert.iter_chunks(iter(lines), holder))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(holder["credits"], 1.5)


class WorkbuddyEffortTest(unittest.TestCase):
    """思考档位必须透传：上游不传就完全不思考，裁剪等于让客户端的设置失效。"""

    def test_effort_aliases(self):
        import workbuddy
        cases = {
            "high": "high", "HIGH": "high", "max": "high",
            "low": "low", "minimal": "low",
            "medium": "medium", "default": "medium",
            "off": "none", "disabled": "none", "none": "none",
            "": None, None: None,
        }
        for raw, want in cases.items():
            self.assertEqual(workbuddy._normalize_effort({"reasoning_effort": raw}),
                             want, raw)

    def test_object_forms(self):
        import workbuddy
        self.assertEqual(workbuddy._normalize_effort(
            {"thinking": {"type": "enabled"}}), "medium")
        self.assertEqual(workbuddy._normalize_effort(
            {"thinking": {"type": "disabled"}}), "none")
        self.assertEqual(workbuddy._normalize_effort(
            {"thinking": {"effort": "low"}}), "low")
        self.assertEqual(workbuddy._normalize_effort(
            {"reasoning": {"effort": "high"}}), "high")

    def test_body_carries_effort(self):
        import json as _json
        import workbuddy
        body = _json.loads(workbuddy.build_chat_body(
            [{"role": "user", "content": "x"}], "m",
            parameters={"reasoning_effort": "high"}))
        self.assertEqual(body["reasoning_effort"], "high")
        # 不传就不该出现这个键：上游把「没有档位」当成不思考
        body2 = _json.loads(workbuddy.build_chat_body(
            [{"role": "user", "content": "x"}], "m"))
        self.assertNotIn("reasoning_effort", body2)

    def test_provider_passes_effort_through_untrimmed(self):
        import providers
        prov = providers.get("workbuddy")
        # 目录里没有档位声明也要透传，Qoder 的规则会在这里裁成 None
        self.assertEqual(prov.normalize_effort("high", {}), "high")
        self.assertTrue(prov.wants_effort_objects)
        self.assertFalse(providers.get("qoder").wants_effort_objects)


class WorkbuddyBrandAnchorTest(unittest.TestCase):
    """上游只拦 system 消息开头那段自称，改写后正文要一字不动。"""

    def test_harness_leadin_replaced(self):
        import workbuddy
        text = ("You are an AI agent powered by DeepSeek Harness.\n\n"
                "# Tone\nBe concise.")
        out = workbuddy._scrub_brand_claims(text)
        self.assertNotIn("DeepSeek Harness", out)
        self.assertIn("Be concise.", out)
        self.assertTrue(out.startswith("You are a coding assistant"))

    def test_only_the_leadin_is_touched(self):
        import workbuddy
        text = "First line is fine.\n\nYou are an AI agent powered by DeepSeek Harness."
        self.assertEqual(workbuddy._scrub_brand_claims(text), text)

    def test_leading_newlines_do_not_defeat_the_scrub(self):
        import workbuddy
        out = workbuddy._scrub_brand_claims(
            "\nYou are an AI agent powered by DeepSeek Harness.\nrest")
        self.assertNotIn("DeepSeek Harness", out)
        self.assertIn("rest", out)
