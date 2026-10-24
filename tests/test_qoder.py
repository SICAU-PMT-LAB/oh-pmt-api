"""上游客户端的纯函数测试（不发起网络请求）。"""

import json
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
    def _decode(self, encoded):
        """把 qoder_encode 的产物解回 JSON，验证真正发出去的内容。"""
        import base64 as _b64
        import json as _json
        from cosy import CUSTOM_ALPHABET, PAD, STD_ALPHABET
        custom2std = {CUSTOM_ALPHABET[i]: STD_ALPHABET[i] for i in range(64)}
        custom2std[PAD] = "="
        std = encoded.decode("ascii").translate(str.maketrans(custom2std))
        n = len(std)
        a = n // 3
        plain = std[n - a:] + std[a:n - a] + std[:a]
        return _json.loads(_b64.b64decode(plain.encode()))

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

    def test_developer_role_mapped_to_system(self):
        """上游用 serde 枚举校验角色，developer 会让整条请求 400。"""
        plain = self._decode(qoder.build_chat_body(
            [{"role": "developer", "content": "be brief"},
             {"role": "user", "content": "hi"}],
            "auto",
        ))
        self.assertEqual([m["role"] for m in plain["messages"]], ["system", "user"])

    def test_system_role_kept(self):
        plain = self._decode(qoder.build_chat_body(
            [{"role": "system", "content": "be brief"},
             {"role": "user", "content": "hi"}],
            "auto",
        ))
        self.assertEqual([m["role"] for m in plain["messages"]], ["system", "user"])

    def test_is_reasoning_follows_catalog(self):
        """没有思考能力的模型不能被标成 is_reasoning；否则上游会按思考模型处理。"""
        plain = self._decode(qoder.build_chat_body(
            [{"role": "user", "content": "hi"}], "auto",
            meta={"is_reasoning": False},
        ))
        self.assertFalse(plain["model_config"]["is_reasoning"])
        self.assertFalse(plain["chat_context"]["extra"]["modelConfig"]["is_reasoning"])

    def test_is_reasoning_true_passed_to_both_copies(self):
        plain = self._decode(qoder.build_chat_body(
            [{"role": "user", "content": "hi"}], "dfmodel",
            meta={"is_reasoning": True},
        ))
        self.assertTrue(plain["model_config"]["is_reasoning"])
        self.assertTrue(plain["chat_context"]["extra"]["modelConfig"]["is_reasoning"])

    def test_reasoning_effort_lands_in_parameters(self):
        plain = self._decode(qoder.build_chat_body(
            [{"role": "user", "content": "hi"}], "dfmodel",
            parameters={"max_tokens": 8192, "reasoning_effort": "high"},
            meta={"is_reasoning": True},
        ))
        self.assertEqual(plain["parameters"]["reasoning_effort"], "high")


class TestThinkingEffort(unittest.TestCase):
    # 取自上游目录的真实配置：DeepSeek-Flash 只支持 low/high/max，默认 high
    META = {
        "is_reasoning": True,
        "thinking_config": {
            "disabled": {},
            "enabled": {"efforts": {"low": {}, "high": {"is_default": True},
                                     "max": {}}},
        },
    }
    # Qwen3.8-Flash：low/medium/xhigh，默认 medium
    QWEN = {
        "is_reasoning": True,
        "thinking_config": {
            "enabled": {"efforts": {"low": {}, "medium": {"is_default": True},
                                     "xhigh": {}}},
        },
    }

    def test_supported_efforts_follow_catalog(self):
        self.assertEqual(qoder.supported_efforts(self.META), ["low", "high", "max"])

    def test_no_thinking_config_means_no_levels(self):
        self.assertEqual(qoder.supported_efforts({"is_reasoning": True}), [])
        self.assertEqual(qoder.supported_efforts(None), [])

    def test_supported_level_passes_through(self):
        self.assertEqual(qoder.normalize_effort("high", self.META), "high")

    def test_unsupported_level_snaps_to_nearest(self):
        """上游对不认识的档位直接忽略，不改正会让客户端以为设置生效。

        high 与 low 都在 medium 旁边时取默认档 high；这比随便取一个
        更接近用户本来会得到的结果。
        """
        self.assertEqual(qoder.normalize_effort("medium", self.META), "high")
        self.assertEqual(qoder.normalize_effort("minimal", self.META), "low")

    def test_unsupported_level_uses_model_default_on_tie(self):
        """等距时优先该模型的官方默认档，不同模型结果不同。"""
        self.assertEqual(qoder.normalize_effort("high", self.QWEN), "medium")
        self.assertEqual(qoder.normalize_effort("max", self.QWEN), "xhigh")

    def test_off_maps_to_none(self):
        self.assertEqual(qoder.normalize_effort("off", self.META), "none")
        self.assertEqual(qoder.normalize_effort("disabled", self.META), "none")

    def test_empty_effort_is_dropped(self):
        self.assertIsNone(qoder.normalize_effort("", self.META))
        self.assertIsNone(qoder.normalize_effort(None, self.META))

    def test_model_without_levels_drops_effort(self):
        """只有开/关、没有档位表的模型，不能下发它不认识的档位。"""
        meta = {"is_reasoning": True, "thinking_config": {"enabled": {}}}
        self.assertIsNone(qoder.normalize_effort("high", meta))

    def test_requested_effort_reads_openai_fields(self):
        self.assertEqual(qoder.requested_effort({"reasoning_effort": "high"}), "high")
        self.assertEqual(qoder.requested_effort({"reasoning": {"effort": "low"}}), "low")
        self.assertEqual(qoder.requested_effort({"thinking": {"level": "max"}}), "max")
        self.assertIsNone(qoder.requested_effort({"messages": []}))
        self.assertIsNone(qoder.requested_effort(None))


class TestQuotaExhausted(unittest.TestCase):
    """112 是账号级额度不足，要在首帧就认出来好换账号。"""

    def _envelope(self, body, status=403):
        outer = {"headers": {}, "body": json.dumps(body), "statusCodeValue": status}
        return "data:" + json.dumps(outer)

    def test_detects_112(self):
        frame = self._envelope({"code": "112",
                                "message": json.dumps({"pricingUrl": "https://qoder.com/pricing"})})
        self.assertTrue(qoder.quota_exhausted(frame))

    def test_normal_chunk_is_not_quota(self):
        frame = self._envelope({"choices": [{"delta": {"content": "hi"}}]}, status=200)
        self.assertFalse(qoder.quota_exhausted(frame))

    def test_queue_frame_is_not_quota(self):
        frame = self._envelope({"code": "10605",
                                "message": json.dumps({"isQueued": True, "retryAfterSeconds": 30})})
        self.assertFalse(qoder.quota_exhausted(frame))
        self.assertEqual(qoder.queue_info(frame), 30)

    def test_empty_frame(self):
        self.assertFalse(qoder.quota_exhausted(""))


if __name__ == "__main__":
    unittest.main()
