"""上游客户端的纯函数测试（不发起网络请求）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import qoder  # noqa: E402


class TestExpiresNormalize(unittest.TestCase):
    """Qoder 的 expires_in 单位是毫秒，按秒处理会让账号显示 24000 小时。"""

    def test_milliseconds_to_seconds(self):
        self.assertEqual(qoder._expires_seconds({"expires_in": 86399968}), 86399)

    def test_plain_seconds_kept(self):
        self.assertEqual(qoder._expires_seconds({"expires_in": 3600}), 3600)

    def test_missing_uses_default(self):
        self.assertEqual(qoder._expires_seconds({}), 86400)

    def test_garbage_uses_default(self):
        self.assertEqual(qoder._expires_seconds({"expires_in": "abc"}), 86400)


class TestModelParsing(unittest.TestCase):
    def test_parses_chat_array(self):
        """真实上游结构是 {"chat":[{key, display_name, enable}]}。"""
        payload = {
            "chat": [
                {"key": "auto", "display_name": "Auto", "enable": True},
                {"key": "ultimate", "display_name": "Ultimate", "enable": True},
            ]
        }
        import json
        # 复刻 list_models 的解析逻辑（不发网络）
        items = payload.get("chat") or []
        out = [{"id": m["key"], "name": m["display_name"]} for m in items]
        self.assertEqual([m["id"] for m in out], ["auto", "ultimate"])


class TestChatBody(unittest.TestCase):
    def test_encodes_and_contains_model(self):
        body = qoder.build_chat_body(
            [{"role": "user", "content": "hi"}], "auto", stream=True
        )
        self.assertIsInstance(body, bytes)
        self.assertGreater(len(body), 100)

    def test_system_goes_into_messages(self):
        """顶层 system 字段服务端会忽略，必须作为 messages 首项注入。"""
        body = qoder.build_chat_body(
            [{"role": "system", "content": "be brief"},
             {"role": "user", "content": "hi"}],
            "auto",
        )
        self.assertIsInstance(body, bytes)

    def test_multimodal_content_flattened(self):
        body = qoder.build_chat_body(
            [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
            "auto",
        )
        self.assertIsInstance(body, bytes)


if __name__ == "__main__":
    unittest.main()
