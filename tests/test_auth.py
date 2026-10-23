"""鉴权与密钥格式的测试。重点是管理员后台不能被公网随意打开。"""

import os
import sys
import tempfile
import unittest
from http.cookies import SimpleCookie
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import server  # noqa: E402
from store import Store  # noqa: E402
from http.server import BaseHTTPRequestHandler  # noqa: E402


class KeyFormatTest(unittest.TestCase):
    """密钥必须是 sicau-PMT- 开头，且中间含连续的 487。"""

    def test_prefix_and_marker(self):
        for _ in range(200):
            k = Store.generate_key()
            self.assertTrue(k.startswith("sicau-PMT-"), k)
            self.assertIn("487", k, k)

    def test_marker_after_prefix(self):
        """487 要在前缀之后，不能靠前缀本身凑数。"""
        for _ in range(100):
            body = Store.generate_key()[len("sicau-PMT-"):]
            self.assertIn("487", body)

    def test_unique_and_long_enough(self):
        keys = {Store.generate_key() for _ in range(500)}
        self.assertEqual(len(keys), 500)
        self.assertGreaterEqual(len(Store.generate_key()), 40)

    def test_create_key_uses_format(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            store = Store(tmp.name)
            mid = store.create_member(name="m")
            _, raw = store.create_key(member_id=mid)
            self.assertTrue(raw.startswith("sicau-PMT-"))
            self.assertIn("487", raw)
            # 库里只存哈希，且能查回
            found = store.get_key_by_hash(Store.hash_key(raw))
            self.assertEqual(found["member_id"], mid)
            self.assertNotIn(raw, str(found))
        finally:
            os.unlink(tmp.name)


class FakeHandler(BaseHTTPRequestHandler):
    """剥离掉网络层，只看鉴权决策逻辑。"""

    def __init__(self, headers=None, addr="203.0.113.9"):
        self.headers = headers or {}
        self.client_address = (addr, 12345)
        self.headers_sent = []
        self.body = b""
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_sent.append((k, v))

    def end_headers(self):
        pass

    def _error(self, status, code, message):
        self.status = status
        self.error_code = code
        return False

    def _admin_cookie_value(self):
        return server.Handler._admin_cookie_value(self)


def make(headers=None, addr="203.0.113.9"):
    h = FakeHandler(headers, addr)
    h.error_code = None
    return h


class AdminAuthTest(unittest.TestCase):
    TOKEN = "secret-token-abc"

    def setUp(self):
        self.patch = mock.patch.object(server, "ADMIN_TOKEN", self.TOKEN)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _auth(self, h):
        return server.Handler._auth_admin(h)

    def test_no_credential_denied(self):
        h = make()
        self.assertFalse(self._auth(h))
        self.assertEqual(h.status, 401)

    def test_query_token_NOT_accepted(self):
        """?token= 是泄漏路径（浏览器历史/nginx 日志/Referer），必须无效。"""
        h = make()
        h.path = "/api/status?token=" + self.TOKEN
        self.assertFalse(self._auth(h))

    def test_header_token_accepted(self):
        h = make({"x-admin-token": self.TOKEN})
        self.assertTrue(self._auth(h))

    def test_wrong_header_token_denied(self):
        h = make({"x-admin-token": "wrong"})
        self.assertFalse(self._auth(h))

    def test_cookie_session_accepted(self):
        cookie = server.Handler._admin_cookie_value(make())
        h = make({"Cookie": f"q2a_admin={cookie}"})
        self.assertTrue(self._auth(h))

    def test_forged_cookie_denied(self):
        h = make({"Cookie": "q2a_admin=deadbeef"})
        self.assertFalse(self._auth(h))

    def test_cookie_does_not_equal_token(self):
        """下发的 Cookie 不能就是管理令牌本身。"""
        cookie = server.Handler._admin_cookie_value(make())
        self.assertNotEqual(cookie, self.TOKEN)

    def test_no_token_configured_remote_denied(self):
        with mock.patch.object(server, "ADMIN_TOKEN", ""):
            h = make(addr="203.0.113.9")
            self.assertFalse(self._auth(h))
            self.assertEqual(h.status, 403)

    def test_no_token_configured_local_ok(self):
        with mock.patch.object(server, "ADMIN_TOKEN", ""):
            self.assertTrue(self._auth(make(addr="127.0.0.1")))


class AdminPageAuthTest(unittest.TestCase):
    TOKEN = "page-token"

    def setUp(self):
        self.patch = mock.patch.object(server, "ADMIN_TOKEN", self.TOKEN)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def _auth(self, h):
        return server.Handler._auth_admin_page(h)

    def test_anonymous_redirected_to_login(self):
        h = make()
        self.assertFalse(self._auth(h))
        self.assertEqual(h.status, 302)
        loc = dict(h.headers_sent)["Location"]
        self.assertEqual(loc, "/login")

    def test_header_token_sets_httponly_cookie(self):
        h = make({"x-admin-token": self.TOKEN, "X-Forwarded-Proto": "https"})
        self.assertFalse(self._auth(h))  # 下发 Cookie 并重定向
        self.assertEqual(h.status, 302)
        sent = dict(h.headers_sent)["Set-Cookie"]
        self.assertIn("HttpOnly", sent)
        self.assertIn("SameSite=Strict", sent)
        self.assertIn("Secure", sent)
        # Cookie 里不能是原始令牌
        self.assertNotIn(self.TOKEN, sent)

    def test_cookie_grants_access(self):
        cookie = server.Handler._admin_cookie_value(make())
        h = make({"Cookie": f"q2a_admin={cookie}"})
        self.assertTrue(self._auth(h))

    def test_local_without_token_ok(self):
        with mock.patch.object(server, "ADMIN_TOKEN", ""):
            self.assertTrue(self._auth(make(addr="127.0.0.1")))


if __name__ == "__main__":
    unittest.main()
