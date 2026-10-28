"""模型名解析：客户端可以用完整名称，上游只认缩写 key。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402
from qoder import display_name, resolve_model  # noqa: E402

CATALOG = {
    "auto": {"name": "Auto"},
    "fast": {"name": "Fast"},
    "qfmodel": {"name": "Qwen3.8-Flash"},
    "gfmodel": {"name": "GLM-5.3-Flash"},
    "dfmodel": {"name": "DeepSeek-Flash"},
    "qmodel_38max": {"name": "Qwen3.8-Max"},
    "kmodel_latest": {"name": "Kimi-K3"},
    "gmodel": {"name": "GLM-5.3"},
}


class ResolveModelTest(unittest.TestCase):
    def test_upstream_key_passes_through(self):
        for k in CATALOG:
            self.assertEqual(resolve_model(k, CATALOG), k)

    def test_full_name_maps_to_key(self):
        """完整名称必须换成上游 key，否则上游返回空内容。"""
        cases = {
            "GLM-5.3-Flash": "gfmodel",
            "DeepSeek-Flash": "dfmodel",
            "Qwen3.8-Flash": "qfmodel",
            "Qwen3.8-Max": "qmodel_38max",
            "Kimi-K3": "kmodel_latest",
            "Auto": "auto",
        }
        for name, want in cases.items():
            self.assertEqual(resolve_model(name, CATALOG), want, name)

    def test_case_insensitive(self):
        self.assertEqual(resolve_model("glm-5.3-flash", CATALOG), "gfmodel")
        self.assertEqual(resolve_model("AUTO", CATALOG), "auto")

    def test_whitespace_and_dots_tolerated(self):
        self.assertEqual(resolve_model("GLM 5.3 Flash", CATALOG), "gfmodel")
        self.assertEqual(resolve_model("glm5.3flash", CATALOG), "gfmodel")

    def test_region_suffix_stripped(self):
        self.assertEqual(resolve_model("dfmodel-cn", CATALOG), "dfmodel")
        self.assertEqual(resolve_model("DeepSeek-Flash-cn", CATALOG), "dfmodel")

    def test_full_name_does_not_collide(self):
        """GLM-5.3 不能误匹配到 GLM-5.3-Flash。"""
        self.assertEqual(resolve_model("GLM-5.3", CATALOG), "gmodel")
        self.assertEqual(resolve_model("GLM-5.3-Flash", CATALOG), "gfmodel")

    def test_unknown_returns_none(self):
        self.assertIsNone(resolve_model("nonexistent", CATALOG))
        self.assertIsNone(resolve_model("", CATALOG))
        self.assertIsNone(resolve_model("   ", CATALOG))

    def test_empty_catalog(self):
        self.assertIsNone(resolve_model("gfmodel", {}))


class DisplayNameTest(unittest.TestCase):
    """上游命名不统一，对外统一成同样风格。"""

    def test_qwen_gets_dash(self):
        self.assertEqual(display_name("qfmodel", "Qwen3.8-Flash"), "Qwen-3.8-Flash")
        self.assertEqual(display_name("qmodel_38max", "Qwen3.8-Max"), "Qwen-3.8-Max")

    def test_other_models_untouched(self):
        self.assertEqual(display_name("gfmodel", "GLM-5.3-Flash"), "GLM-5.3-Flash")
        self.assertEqual(display_name("gmodel", "GLM-5.3"), "GLM-5.3")

    def test_deepseek_flash_unified_with_workbuddy(self):
        """两家上游叫法不同，对外必须是同一个名字。"""
        self.assertEqual(display_name("dfmodel", "DeepSeek-Flash"),
                         "DeepSeek-V4.1-Flash")

    def test_unknown_falls_back_to_raw(self):
        self.assertEqual(display_name("zzz", "Whatever"), "Whatever")

    def test_both_spellings_resolve(self):
        """改名后旧写法 Qwen3.8-Flash 仍要能调通。"""
        cat = {"qfmodel": {"name": "Qwen-3.8-Flash"}}
        for spelling in ("Qwen-3.8-Flash", "Qwen3.8-Flash", "qfmodel", "qwen3.8flash"):
            self.assertEqual(resolve_model(spelling, cat), "qfmodel", spelling)


class GuardCatalogTest(unittest.TestCase):
    """成员白名单写缩写或完整名称都要能通过。"""

    def test_key_in_allowlist(self):
        spec = {"model_allowlist": "gfmodel,dfmodel"}
        guard.check_model(spec, None, "gfmodel", CATALOG)

    def test_full_name_in_allowlist(self):
        spec = {"model_allowlist": "GLM-5.3-Flash,DeepSeek-Flash"}
        guard.check_model(spec, None, "gfmodel", CATALOG)
        guard.check_model(spec, None, "dfmodel", CATALOG)

    def test_other_model_still_denied(self):
        spec = {"model_allowlist": "GLM-5.3-Flash"}
        with self.assertRaises(guard.Denied):
            guard.check_model(spec, None, "gmodel", CATALOG)

    def test_empty_allowlist_allows_all(self):
        guard.check_model({"model_allowlist": ""}, None, "gmodel", CATALOG)


if __name__ == "__main__":
    unittest.main()
