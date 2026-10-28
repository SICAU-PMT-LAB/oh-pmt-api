"""模型来源路由：可用模型决定能走哪几家，优先账号决定先走哪一家。

对外一个模型只有一个名字（客户端不该知道后面接的是哪家账号池），
但网关内部必须按 (厂商, 上游 key) 分别判断能不能用。这两条是核心需求，
错了不会报错、只会静默把请求打到不该用的账号上，所以逐条钉住。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402

# 合并后的目录：同一个显示名，两家上游各自的 key 不同
CATALOG = {
    "deepseek-v4.1-flash": {
        "id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash",
        "provider": "qoder", "region": "global",
        "routes": [
            {"provider": "qoder", "region": "global", "model": "dfmodel"},
            {"provider": "workbuddy", "region": "cn",
             "model": "deepseek-v4.1-flash"},
        ],
    },
    "qwen-3.8-flash": {
        "id": "qwen-3.8-flash", "name": "Qwen-3.8-Flash",
        "provider": "qoder", "region": "global",
        "routes": [
            {"provider": "qoder", "region": "global", "model": "qfmodel"},
        ],
    },
}


def routes(model, *specs, prefer=None):
    # 不传 specs 表示「不限」，对应 allowed_models 返回的 None
    allowed = set(specs) if specs else None
    return [(r["provider"], r["model"]) for r in
            guard.allowed_routes(allowed, model, CATALOG, prefer=prefer)]


class RouteAllowTest(unittest.TestCase):
    def test_empty_allowlist_allows_every_source(self):
        self.assertEqual(routes("deepseek-v4.1-flash"),
                         [("qoder", "dfmodel"),
                          ("workbuddy", "deepseek-v4.1-flash")])

    def test_vendor_prefix_picks_one_source(self):
        """只勾 WorkBuddy 的 DeepSeek，Qoder 同款就不能被调用。"""
        self.assertEqual(routes("deepseek-v4.1-flash",
                                "workbuddy:deepseek-v4.1-flash"),
                         [("workbuddy", "deepseek-v4.1-flash")])

    def test_vendor_prefix_is_exact(self):
        """前缀写错厂商不能碰巧命中另一家的同名 key。"""
        self.assertEqual(routes("deepseek-v4.1-flash",
                                "qoder:deepseek-v4.1-flash"), [])

    def test_bare_upstream_key_is_legacy_compatible(self):
        """旧数据存的是裸上游 key，升级后必须继续能用。"""
        self.assertEqual(routes("deepseek-v4.1-flash", "dfmodel"),
                         [("qoder", "dfmodel")])

    def test_legacy_qoder_triple_still_works(self):
        """老成员的白名单是 qfmodel,gfmodel,dfmodel，别在升级后失效。"""
        self.assertEqual(routes("qwen-3.8-flash", "qfmodel", "gfmodel", "dfmodel"),
                         [("qoder", "qfmodel")])

    def test_display_name_writes_allow_all_vendors(self):
        """不关心厂商时可以只写模型名，含义是「哪家都行」。"""
        self.assertEqual(routes("deepseek-v4.1-flash", "DeepSeek-V4.1-Flash"),
                         [("qoder", "dfmodel"),
                          ("workbuddy", "deepseek-v4.1-flash")])

    def test_unknown_model_has_no_sources(self):
        self.assertEqual(routes("nonexistent", "whatever"), [])


class RouteOrderTest(unittest.TestCase):
    def test_preferred_vendor_goes_first(self):
        """优先账号属于 WorkBuddy 时，两家都能跑也要先试 WorkBuddy。"""
        self.assertEqual(routes("deepseek-v4.1-flash", prefer="workbuddy"),
                         [("workbuddy", "deepseek-v4.1-flash"),
                          ("qoder", "dfmodel")])

    def test_preferred_vendor_without_access_is_ignored(self):
        """优先 WorkBuddy 但只开了 Qoder 的模型，就得走 Qoder。"""
        self.assertEqual(
            routes("deepseek-v4.1-flash", "qoder:dfmodel", prefer="workbuddy"),
            [("qoder", "dfmodel")])

    def test_preferred_vendor_still_yields_two_sources(self):
        """优先那家只是一试，失败了还要能换到另一家。"""
        got = routes("deepseek-v4.1-flash", prefer="workbuddy")
        self.assertEqual(len(got), 2)

    def test_no_preference_keeps_catalog_order(self):
        self.assertEqual(routes("deepseek-v4.1-flash", prefer=None),
                         routes("deepseek-v4.1-flash"))


class CheckModelTest(unittest.TestCase):
    """白名单把某模型的所有来源都关掉时，必须明确拒绝而不是打出去。"""

    def test_denied_when_no_source_allowed(self):
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model({"model_allowlist": "workbuddy:glm-5.3"}, None,
                              "qwen-3.8-flash", CATALOG)
        self.assertEqual(ctx.exception.code, "model_forbidden")

    def test_passes_when_one_source_allowed(self):
        guard.check_model(
            {"model_allowlist": "workbuddy:deepseek-v4.1-flash"}, None,
            "deepseek-v4.1-flash", CATALOG)

    def test_unknown_model_denied_by_nonempty_allowlist(self):
        with self.assertRaises(guard.Denied):
            guard.check_model({"model_allowlist": "dfmodel"}, None,
                              "made-up-model", CATALOG)

    def test_message_names_models_not_vendors(self):
        """拒绝文案只报对外模型名。

        白名单内部按 `厂商:上游 key` 存，但那是网关自己的路由信息。
        回给客户端的话，调用方就能从报错里看出后面接的是哪家账号池。
        """
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model({"model_allowlist": "workbuddy:glm-5.3"}, None,
                              "qwen-3.8-flash", CATALOG)
        msg = ctx.exception.message
        self.assertIn("qwen-3.8-flash", msg)
        self.assertNotIn("workbuddy", msg)
        self.assertNotIn(":", msg)

    def test_message_lists_display_names(self):
        """能用的模型要翻成显示名，不能把内部 id 或上游 key 扬出去。"""
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model(
                {"model_allowlist": "qoder:dfmodel"}, None,
                "qwen-3.8-flash", CATALOG)
        msg = ctx.exception.message
        self.assertIn("DeepSeek-V4.1-Flash", msg)
        self.assertNotIn("dfmodel", msg)

    def test_message_without_catalog_hides_vendor_specs(self):
        """目录拿不到时，带冒号的来源写法也不能当模型名回出去。"""
        with self.assertRaises(guard.Denied) as ctx:
            guard.check_model({"model_allowlist": "qoder:dfmodel,b"}, None, "c")
        self.assertNotIn("qoder", ctx.exception.message)
        self.assertIn("b", ctx.exception.message)


class SourceLabelTest(unittest.TestCase):
    """管理端要把「同一模型的各家来源」摆出来，得能还原成一行一行的配置项。"""

    def test_sources_carry_vendor_and_key(self):
        rows = guard.model_sources("deepseek-v4.1-flash", CATALOG)
        self.assertEqual([(r["provider"], r["upstream_key"]) for r in rows],
                         [("qoder", "dfmodel"),
                          ("workbuddy", "deepseek-v4.1-flash")])

    def test_spec_is_round_trippable(self):
        """界面勾出来的字符串必须能被 route_allowed 原样认回来。"""
        names = guard.model_names("deepseek-v4.1-flash", CATALOG)
        for row in guard.model_sources("deepseek-v4.1-flash", CATALOG):
            self.assertTrue(guard.route_allowed(
                {row["spec"]}, {"provider": row["provider"],
                                "model": row["upstream_key"]}, names), row["spec"])


if __name__ == "__main__":
    unittest.main()
