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


class FakeHandler(BaseHTTPRequestHandler):
    """剥离掉网络层，只看鉴权决策逻辑。"""

    def __init__(self, headers=None, addr="203.0.113.9"):
        self.headers = headers or {}
        self.client_address = (addr, 12345)
        self.headers_sent = []
        self.body = b""
        self.status = None
        self.error_code = None

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

    # 把 Handler 上用到的方法绑过来，只测决策逻辑
    _session = server.Handler._session
    _auth_admin = server.Handler._auth_admin
    _key_of = server.Handler._key_of


def make(headers=None, addr="203.0.113.9"):
    return FakeHandler(headers, addr)


class KeyFormatTest(unittest.TestCase):
    """密钥必须是 sicau-PMT- 开头，487 随机藏在中间乱码里。"""

    def test_prefix_and_marker(self):
        for _ in range(200):
            k = Store.generate_key()
            self.assertTrue(k.startswith("sicau-PMT-"), k)
            self.assertIn("487", k, k)

    def test_marker_not_right_after_prefix(self):
        """487 不能紧跟前缀，要藏在后面乱码中间。"""
        positions = set()
        for _ in range(300):
            body = Store.generate_key()[len("sicau-PMT-"):]
            pos = body.find("487")
            self.assertNotEqual(pos, 0, "487 紧跟了前缀")
            positions.add(pos)
        # 位置必须真的随机分布，而不是固定在某一处
        self.assertGreater(len(positions), 3, f"487 位置过于固定：{positions}")

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
            found = store.get_key_by_hash(Store.hash_key(raw))
            self.assertEqual(found["member_id"], mid)
            # 明文保留以便随时查看，但哈希不能等于明文
            self.assertEqual(found["key_value"], raw)
            self.assertNotEqual(found["key_hash"], raw)
        finally:
            os.unlink(tmp.name)


class PasswordTest(unittest.TestCase):
    def setup_method(self):
        pass

    def test_hash_and_verify(self):
        h = Store.hash_password("secret")
        self.assertTrue(Store.verify_password("secret", h))
        self.assertFalse(Store.verify_password("Secret", h))
        self.assertFalse(Store.verify_password("", h))

    def test_plaintext_not_stored(self):
        h = Store.hash_password("secret")
        self.assertNotIn("secret", h)

    def test_salted(self):
        self.assertNotEqual(Store.hash_password("x"), Store.hash_password("x"))

    def test_garbage_hash_rejected(self):
        self.assertFalse(Store.verify_password("x", ""))
        self.assertFalse(Store.verify_password("x", "notahash"))


class MemberLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_login_and_is_admin(self):
        self.store.create_member(name="A", username="craig", password="pw", is_admin=1)
        m = self.store.get_member_by_username("craig")
        self.assertEqual(m["is_admin"], 1)
        self.assertTrue(Store.verify_password("pw", m["password_hash"]))

    def test_ordinary_member_not_admin(self):
        self.store.create_member(name="B", username="zs", password="pw")
        m = self.store.get_member_by_username("zs")
        self.assertEqual(m["is_admin"], 0)

    def test_unknown_username(self):
        self.assertIsNone(self.store.get_member_by_username("nobody"))

    def test_get_admin(self):
        self.assertIsNone(self.store.get_admin())
        self.store.create_member(name="A", username="root", password="pw", is_admin=1)
        self.assertEqual(self.store.get_admin()["username"], "root")

    def test_set_password(self):
        mid = self.store.create_member(name="C", username="c", password="old")
        self.store.set_password(mid, "new")
        m = self.store.get_member(mid)
        self.assertTrue(Store.verify_password("new", m["password_hash"]))
        self.assertFalse(Store.verify_password("old", m["password_hash"]))

    def test_update_member_sets_username(self):
        """漏了 username 白名单会导致设了账号却登不进去。"""
        mid = self.store.create_member(name="F")
        self.store.update_member(mid, username="newname")
        self.assertEqual(self.store.get_member(mid)["username"], "newname")
        self.assertIsNotNone(self.store.get_member_by_username("newname"))

    def test_username_unique(self):
        self.store.create_member(name="D", username="dup", password="pw")
        with self.assertRaises(Exception):
            self.store.create_member(name="E", username="dup", password="pw")


class SessionTest(unittest.TestCase):
    def test_sign_roundtrip(self):
        signed = server._sign("m:7")
        self.assertEqual(server._unsign(signed), "m:7")

    def test_tampered_rejected(self):
        signed = server._sign("m:7")
        self.assertIsNone(server._unsign(signed.replace("m:7", "m:8")))

    def test_garbage_rejected(self):
        self.assertIsNone(server._unsign(""))
        self.assertIsNone(server._unsign("nodelimiter"))

    def test_cookie_does_not_contain_password(self):
        signed = server._sign("m:7")
        self.assertNotIn("Hhy1123", signed)


class AdminPermissionTest(unittest.TestCase):
    """管理接口必须验证 is_admin，不能只凭已登录。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.store.create_member(name="admin", username="a", password="p", is_admin=1)
        self.store.create_member(name="user", username="u", password="p")
        self.patch = mock.patch.object(server, "get_store", lambda: self.store)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.tmp.name)

    def _session_handler(self, member_id):
        h = make({"Cookie": f"{server.SESSION_COOKIE}={server._sign('m:' + str(member_id))}"})
        return h

    def test_admin_session_allowed(self):
        admin = self.store.get_admin()
        self.assertTrue(server.Handler._auth_admin(self._session_handler(admin["id"])))

    def test_member_session_denied(self):
        user = self.store.get_member_by_username("u")
        h = self._session_handler(user["id"])
        self.assertFalse(server.Handler._auth_admin(h))
        self.assertEqual(h.error_code, "admin_denied")

    def test_forged_session_denied(self):
        h = make({"Cookie": f"{server.SESSION_COOKIE}=m:1.deadbeef"})
        self.assertFalse(server.Handler._auth_admin(h))

    def test_no_session_denied(self):
        h = make()
        self.assertFalse(server.Handler._auth_admin(h))

    def test_disabled_member_session_invalid(self):
        user = self.store.get_member_by_username("u")
        h = self._session_handler(user["id"])
        self.store.update_member(user["id"], enabled=False)
        self.assertFalse(server.Handler._auth_admin(h))


class MigrationMembersTest(unittest.TestCase):
    """旧库没有 username/password_hash/is_admin，必须能补上。"""

    OLD = """
    CREATE TABLE members (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        email TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
        limit_daily REAL DEFAULT 0, limit_weekly REAL DEFAULT 0,
        preferred_account_id INTEGER, model_allowlist TEXT DEFAULT '',
        note TEXT DEFAULT '', created_at REAL NOT NULL);
    CREATE TABLE api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL UNIQUE,
        key_prefix TEXT NOT NULL, member_id INTEGER, enabled INTEGER DEFAULT 1,
        ip_allowlist TEXT DEFAULT '', model_allowlist TEXT DEFAULT '',
        note TEXT DEFAULT '', created_at REAL NOT NULL);
    CREATE TABLE usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER, member_id INTEGER,
        account_id INTEGER, model TEXT DEFAULT '', prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
        credits REAL DEFAULT 0, credits_original REAL DEFAULT 0,
        status INTEGER DEFAULT 200, ip TEXT DEFAULT '', created_at REAL NOT NULL);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        c = sqlite3.connect(self.tmp.name)
        c.executescript(self.OLD)
        c.execute("INSERT INTO members (name, created_at) VALUES ('老成员', 0)")
        c.commit()
        c.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_opens_and_adds_columns(self):
        store = Store(self.tmp.name)
        m = store.list_members()[0]
        self.assertEqual(m["username"] or "", "")
        self.assertEqual(m["is_admin"], 0)

    def test_can_set_password_after_migration(self):
        store = Store(self.tmp.name)
        mid = store.list_members()[0]["id"]
        store.update_member(mid, username="old")
        store.set_password(mid, "pw")
        self.assertTrue(Store.verify_password("pw", store.get_member(mid)["password_hash"]))


if __name__ == "__main__":
    unittest.main()


class KeyVisibleTest(unittest.TestCase):
    """管理员要能随时查看完整密钥，不用重建。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_key_stored_in_plaintext(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        self.assertEqual(self.store.get_key(kid)["key_value"], raw)

    def test_still_hashed_for_lookup(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        k = self.store.get_key(kid)
        self.assertEqual(k["key_hash"], Store.hash_key(raw))
        self.assertNotEqual(k["key_hash"], raw)

    def test_list_keys_exposes_value(self):
        mid = self.store.create_member(name="m")
        _, raw = self.store.create_key(member_id=mid)
        self.assertEqual(self.store.list_keys(mid)[0]["key_value"], raw)

    def test_lookup_by_hash_still_works(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        self.assertEqual(self.store.get_key_by_hash(Store.hash_key(raw))["id"], kid)

    def test_migration_adds_key_value(self):
        import sqlite3
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            c = sqlite3.connect(tmp.name)
            c.executescript("""
            CREATE TABLE members (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              email TEXT DEFAULT '', enabled INTEGER DEFAULT 1, limit_daily REAL DEFAULT 0,
              limit_weekly REAL DEFAULT 0, preferred_account_id INTEGER,
              model_allowlist TEXT DEFAULT '', note TEXT DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT,
              key_hash TEXT NOT NULL UNIQUE, key_prefix TEXT NOT NULL, member_id INTEGER,
              enabled INTEGER DEFAULT 1, ip_allowlist TEXT DEFAULT '',
              model_allowlist TEXT DEFAULT '', note TEXT DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER,
              member_id INTEGER, account_id INTEGER, model TEXT DEFAULT '',
              prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
              total_tokens INTEGER DEFAULT 0, credits REAL DEFAULT 0,
              credits_original REAL DEFAULT 0, status INTEGER DEFAULT 200,
              ip TEXT DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """ )
            c.execute("INSERT INTO api_keys (key_hash,key_prefix,created_at) "
                      "VALUES ('h','sicau-PMT-old',0)")
            c.commit(); c.close()
            st = Store(tmp.name)
            # 旧密钥没有明文，返回空串但不报错
            self.assertEqual(st.list_keys()[0]["key_value"] or "", "")
        finally:
            os.unlink(tmp.name)


class MemberDashboardKeyTest(unittest.TestCase):
    """会话登录时也要拿到自己的密钥，不能是 None。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patch = mock.patch.object(server, "get_store", lambda: self.store)
        self.patch.start()
        self.patch2 = mock.patch.object(server, "all_models", lambda: {})
        self.patch2.start()

    def tearDown(self):
        self.patch2.stop()
        self.patch.stop()
        os.unlink(self.tmp.name)

    def _ctx(self, member_id):
        h = make({"Cookie": f"{server.SESSION_COOKIE}={server._sign('m:' + str(member_id))}"})
        return server.Handler._auth_member(h)

    def test_session_ctx_carries_key(self):
        mid = self.store.create_member(name="s", username="s")
        _, raw = self.store.create_key(member_id=mid)
        ctx = self._ctx(mid)
        self.assertIsNotNone(ctx["key"])
        self.assertEqual(ctx["key"]["key_value"], raw)

    def test_dashboard_exposes_key_value(self):
        mid = self.store.create_member(name="s", username="s")
        _, raw = self.store.create_key(member_id=mid)
        data = server.Handler._member_dashboard(make(), self._ctx(mid))
        self.assertEqual(data["key"]["value"], raw)
