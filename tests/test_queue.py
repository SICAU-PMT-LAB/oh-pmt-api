"""上游排队限流不能当成凭证失效，也不能先回 200 再报错。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qoder import UpstreamError, queue_info  # noqa: E402

QUEUE_BODY = (
    '{"code":"403","message":"{\\"code\\":\\"10605\\",\\"message\\":'
    '\\"{\\\\\\"isQueued\\\\\\":true,\\\\\\"retryAfterSeconds\\\\\\":30}\\"}"}'
)


class QueueErrorTest(unittest.TestCase):
    def test_403_queue_is_not_auth_dead(self):
        e = UpstreamError(403, QUEUE_BODY)
        self.assertTrue(e.is_queued)
        self.assertFalse(e.is_auth_dead)
        self.assertTrue(e.is_rate_limited)

    def test_retry_after_parsed(self):
        self.assertEqual(UpstreamError(403, QUEUE_BODY).retry_after(), 30)

    def test_retry_after_default(self):
        self.assertEqual(UpstreamError(403, '{"code":"10605"}').retry_after(), 30)

    def test_real_auth_failure_still_detected(self):
        for body in ('{"code":"TOKEN_EXPIRE"}',
                     '{"code":"12153"}',
                     '{"message":"Offline user session not found"}'):
            e = UpstreamError(403, body)
            self.assertFalse(e.is_queued, body)
            self.assertTrue(e.is_auth_dead, body)

    def test_401_is_auth_dead(self):
        self.assertTrue(UpstreamError(401, "unauthorized").is_auth_dead)

    def test_plain_403_is_not_auth_dead(self):
        e = UpstreamError(403, '{"code":"9999","message":"nope"}')
        self.assertFalse(e.is_queued)
        self.assertFalse(e.is_auth_dead)


class FirstFrameQueueTest(unittest.TestCase):
    """排队时上游给 HTTP 200，错误在首帧，要在开流前识别。"""

    # 真实上游帧的转义层数很深，这里用同样形状的样本
    QUEUE_FRAME = open(
        os.path.join(os.path.dirname(__file__), "queue_frame.txt")
    ).read().strip().replace(":30}", ":45}")

    def test_detect_queue_in_frame(self):
        self.assertEqual(queue_info(self.QUEUE_FRAME), 45)

    def test_normal_frame_not_queue(self):
        self.assertEqual(queue_info('data:{"body":"{\\"choices\\":[]}"}'), 0)

    def test_empty_frame(self):
        self.assertEqual(queue_info(""), 0)

    def test_default_wait(self):
        self.assertEqual(queue_info('data:{"body":"{\\"code\\":\\"10605\\"}"}'), 30)


if __name__ == "__main__":
    unittest.main()
