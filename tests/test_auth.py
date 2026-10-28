"""鉴权与密钥格式的测试。重点是管理员后台不能被公网随意打开。"""

import io
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
    _local_rescue = server.Handler._local_rescue
    _client_ip = server.Handler._client_ip
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
            # 库里只留 hash：明文只在创建那一刻回显一次
            self.assertNotIn("key_value", found)
            self.assertEqual(found["key_hash"], Store.hash_key(raw))
            self.assertEqual(found["key_hash"], Store.hash_key(raw))
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

    def test_admin_cannot_be_disabled(self):
        """管理员是进后台的唯一钥匙，停用他就没人能改回来了。"""
        mid = self.store.create_member(name="boss", username="boss",
                                      password="pw", is_admin=1)
        self.store.update_member(mid, enabled=False)
        self.assertEqual(self.store.get_member(mid)["enabled"], 1)

    def test_admin_cannot_drop_admin_flag(self):
        mid = self.store.create_member(name="boss", username="boss",
                                      password="pw", is_admin=1)
        self.store.update_member(mid, is_admin=0)
        self.assertEqual(self.store.get_member(mid)["is_admin"], 1)

    def test_admin_still_editable(self):
        """只拦停用和取消身份，名字、密码、限额照常能改。"""
        mid = self.store.create_member(name="boss", username="boss",
                                      password="pw", is_admin=1)
        self.store.update_member(mid, name="老板", limit_daily=5, enabled=True)
        m = self.store.get_member(mid)
        self.assertEqual(m["name"], "老板")
        self.assertEqual(m["limit_daily"], 5)

    def test_admin_cannot_be_deleted(self):
        mid = self.store.create_member(name="boss", username="boss",
                                      password="pw", is_admin=1)
        self.assertFalse(self.store.delete_member(mid))
        self.assertIsNotNone(self.store.get_member(mid))

    def test_ordinary_member_can_be_disabled_and_deleted(self):
        mid = self.store.create_member(name="u", username="u", password="pw")
        self.store.update_member(mid, enabled=False)
        self.assertEqual(self.store.get_member(mid)["enabled"], 0)
        self.assertTrue(self.store.delete_member(mid))
        self.assertIsNone(self.store.get_member(mid))


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

    def test_local_rescue_when_admin_disabled(self):
        """库里的管理员被停用（旧版本留下的状态）时，本机直连仍能进后台启回来。"""
        self._force_disable(self.store.get_admin()["id"])
        self.assertTrue(server.Handler._auth_admin(make(addr="127.0.0.1")))
        self.assertFalse(server.Handler._auth_admin(make(addr="203.0.113.9")))

    def test_local_rescue_when_admin_deleted(self):
        """管理员行被删干净时也不能把自己锁在外面。"""
        import sqlite3
        c = sqlite3.connect(self.tmp.name)
        c.execute("DELETE FROM members WHERE is_admin=1")
        c.commit()
        c.close()
        self.assertTrue(server.Handler._auth_admin(make(addr="127.0.0.1")))
        self.assertFalse(server.Handler._auth_admin(make(addr="203.0.113.9")))

    def test_local_rescue_off_when_admin_healthy(self):
        """管理员正常时本机不自动放行，否则本机进程能绕过登录。"""
        self.assertFalse(server.Handler._auth_admin(make(addr="127.0.0.1")))

    def test_local_rescue_when_admin_has_no_password(self):
        """管理员没设密码就登不进去，同样算锁在门外。"""
        self.assertFalse(server.Handler._auth_admin(make(addr="203.0.113.9")))
        import sqlite3
        c = sqlite3.connect(self.tmp.name)
        c.execute("UPDATE members SET password_hash='' WHERE is_admin=1")
        c.commit()
        c.close()
        self.assertTrue(server.Handler._auth_admin(make(addr="127.0.0.1")))
        self.assertFalse(server.Handler._auth_admin(make(addr="203.0.113.9")))

    def test_local_rescue_ignores_proxied_requests(self):
        """nginx 反代到 127.0.0.1，对端地址是本机但来源是外网，不能放行。"""
        self._force_disable(self.store.get_admin()["id"])
        with mock.patch.object(server, "TRUST_FORWARDED", True):
            h = make({"X-Forwarded-For": "203.0.113.9"}, addr="127.0.0.1")
            self.assertFalse(server.Handler._auth_admin(h))
            h2 = make({"X-Forwarded-For": "127.0.0.1"}, addr="127.0.0.1")
            self.assertTrue(server.Handler._auth_admin(h2))

    def _force_disable(self, member_id):
        """绕过 update_member 直接改库，模拟旧版本留下的已停用管理员。"""
        import sqlite3
        c = sqlite3.connect(self.tmp.name)
        c.execute("UPDATE members SET enabled=0 WHERE id=?", (member_id,))
        c.commit()
        c.close()


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


class SessionKeyTest(unittest.TestCase):
    """会话签名密钥不能是写死在源码里的常量。"""

    def test_default_key_is_not_hardcoded(self):
        # 源码是公开的，固定串意味着谁都能签一个管理员 cookie
        self.assertNotEqual(server._SESSION_KEY,
                            b"oh-pmt-api-local-session-key")

    def test_key_is_long_enough(self):
        self.assertGreaterEqual(len(server._SESSION_KEY), 32)


class LoginRateLimitTest(unittest.TestCase):
    """管理员密码不能被无限次猜。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.store.create_member(name="boss", username="boss",
                                 password="right-pass", is_admin=1)
        server.STORE = self.store
        server._login_fails.clear()

    def tearDown(self):
        os.unlink(self.tmp.name)
        server.STORE = None
        server._login_fails.clear()

    def _try(self, password, ip="198.51.100.7"):
        h = FakeHandler({}, ip)
        captured = {}

        def fake_read_json():
            return {"username": "boss", "password": password}

        def fake_error(status, code, message, retry_after=0):
            captured["status"] = status
            captured["code"] = code
            return False

        h._read_json = fake_read_json
        h._error = fake_error
        h._client_ip = lambda: ip
        h.send_response = lambda c: captured.setdefault("ok_status", c)
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None
        h.wfile = type("W", (), {"write": lambda *a: None})()
        server.Handler._do_login(h)
        return captured

    def test_locks_out_after_repeated_failures(self):
        for _ in range(server.LOGIN_MAX_FAILS):
            got = self._try("wrong")
            self.assertEqual(got["status"], 401)
        got = self._try("wrong")
        self.assertEqual(got["status"], 429)
        self.assertEqual(got["code"], "login_locked")

    def test_lock_also_blocks_correct_password(self):
        for _ in range(server.LOGIN_MAX_FAILS):
            self._try("wrong")
        # 锁住期间即使密码正确也不放行，否则暴力破解照样能试
        got = self._try("right-pass")
        self.assertEqual(got["status"], 429)

    def test_other_ip_is_not_affected(self):
        for _ in range(server.LOGIN_MAX_FAILS):
            self._try("wrong", ip="198.51.100.7")
        got = self._try("wrong", ip="203.0.113.99")
        self.assertEqual(got["status"], 401)

    def test_success_clears_counter(self):
        for _ in range(server.LOGIN_MAX_FAILS - 1):
            self._try("wrong")
        self._try("right-pass")
        self.assertEqual(server.login_blocked("198.51.100.7"), 0)


class BodySizeLimitTest(unittest.TestCase):
    """声明一个巨大的 Content-Length 不能让 rfile.read 把内存吃满。"""

    def test_oversized_body_is_not_read(self):
        read_called = []
        h = FakeHandler({"Content-Length": str(server.MAX_BODY_BYTES + 1)})
        h.rfile = type("R", (), {"read": lambda self, n: read_called.append(n) or b""})()
        self.assertEqual(server.Handler._read_json(h), {})
        self.assertEqual(read_called, [])

    def test_huge_declared_length_is_not_read(self):
        read_called = []
        h = FakeHandler({"Content-Length": str(2 ** 40)})
        h.rfile = type("R", (), {"read": lambda self, n: read_called.append(n) or b""})()
        self.assertEqual(server.Handler._read_json(h), {})
        self.assertEqual(read_called, [])

    def test_normal_body_still_parsed(self):
        import io
        h = FakeHandler({"Content-Length": "13"})
        h.rfile = io.BytesIO(b'{"hello":"a"}')
        self.assertEqual(server.Handler._read_json(h), {"hello": "a"})


if __name__ == "__main__":
    unittest.main()


class KeyStorageTest(unittest.TestCase):
    """库里只能留 hash。

    明文只在成员创建密钥的那一刻回显一次，之后谁都取不回来（包括管理员）。
    校验只认 key_hash，明文不落库。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_no_plaintext_column(self):
        import sqlite3
        cols = [r[1] for r in sqlite3.connect(self.tmp.name).execute(
            "PRAGMA table_info(api_keys)")]
        self.assertNotIn("key_value", cols)

    def test_created_key_not_recoverable(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        row = self.store.get_key(kid)
        self.assertNotIn(raw, str(row))
        self.assertEqual(row["key_hash"], Store.hash_key(raw))

    def test_still_hashed_for_lookup(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        k = self.store.get_key(kid)
        self.assertEqual(k["key_hash"], Store.hash_key(raw))
        self.assertNotEqual(k["key_hash"], raw)

    def test_lookup_by_hash_still_works(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        self.assertEqual(self.store.get_key_by_hash(Store.hash_key(raw))["id"], kid)

    def test_reset_rotates_and_invalidates_old(self):
        mid = self.store.create_member(name="m")
        kid, raw = self.store.create_key(member_id=mid)
        fresh = self.store.reset_key(kid)
        self.assertNotEqual(fresh, raw)
        self.assertIsNone(self.store.get_key_by_hash(Store.hash_key(raw)))
        self.assertEqual(
            self.store.get_key_by_hash(Store.hash_key(fresh))["id"], kid)

    def test_reset_keeps_ownership_and_limits(self):
        mid = self.store.create_member(name="m")
        kid, _ = self.store.create_key(member_id=mid, ip_allowlist="10.0.0.0/8",
                                       note="项目 A")
        self.store.reset_key(kid)
        k = self.store.get_key(kid)
        self.assertEqual(k["member_id"], mid)
        self.assertEqual(k["ip_allowlist"], "10.0.0.0/8")
        self.assertEqual(k["note"], "项目 A")

    def test_migration_drops_plaintext_column(self):
        """老库里的明文列要连数据一起删掉，同时不打死已发出的密钥。"""
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
              key_hash TEXT NOT NULL UNIQUE, key_value TEXT DEFAULT '',
              key_prefix TEXT NOT NULL, member_id INTEGER,
              enabled INTEGER DEFAULT 1, ip_allowlist TEXT DEFAULT '',
              model_allowlist TEXT DEFAULT '', note TEXT DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER,
              member_id INTEGER, account_id INTEGER, model TEXT DEFAULT '',
              prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
              total_tokens INTEGER DEFAULT 0, credits REAL DEFAULT 0,
              credits_original REAL DEFAULT 0, status INTEGER DEFAULT 200,
              ip TEXT DEFAULT '', created_at REAL NOT NULL);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            raw = "sicau-PMT-OLDKEY487DEMO"
            c.execute("INSERT INTO api_keys (key_hash,key_value,key_prefix,created_at) "
                      "VALUES (?,?,'sicau-PMT-old',0)",
                      (Store.hash_key(raw), raw))
            c.commit()
            c.close()

            st = Store(tmp.name)
            cols = [r[1] for r in sqlite3.connect(tmp.name).execute(
                "PRAGMA table_info(api_keys)")]
            self.assertNotIn("key_value", cols)
            # 老密钥继续能用：校验走 hash
            self.assertIsNotNone(st.get_key_by_hash(Store.hash_key(raw)))
            self.assertNotIn(raw, str(st.list_keys()))
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
        """会话里要带上自己那把密钥的记录，但里面没有明文。"""
        mid = self.store.create_member(name="s", username="s")
        _, raw = self.store.create_key(member_id=mid)
        ctx = self._ctx(mid)
        self.assertIsNotNone(ctx["key"])
        self.assertEqual(ctx["key"]["key_prefix"], raw[:16])
        self.assertNotIn(raw, str(ctx["key"]))

    def test_dashboard_never_returns_plaintext(self):
        """库里只有 hash，接口也就回吐不出明文。"""
        mid = self.store.create_member(name="s", username="s")
        _, raw = self.store.create_key(member_id=mid)
        data = server.Handler._member_dashboard(make(), self._ctx(mid))
        self.assertEqual(data["key"]["value"], "")
        self.assertEqual(data["key"]["prefix"], raw[:16])
        self.assertNotIn(raw, str(data))

    def test_key_summary_counts_usage(self):
        """密钥管理页要看到每把密钥自己的用量，没用的密钥也要出现。"""
        mid = self.store.create_member(name="m")
        k1, _ = self.store.create_key(member_id=mid, note="a")
        k2, _ = self.store.create_key(member_id=mid, note="b")
        for cost, tok in ((0.5, 1000), (1.25, 2500)):
            self.store.record_usage(key_id=k1, member_id=mid, account_id=None,
                                    model="m", prompt_tokens=tok, completion_tokens=0,
                                    cost=cost, status=200)
        rows = {r["id"]: r for r in self.store.key_summary()}
        self.assertEqual(rows[k1]["n"], 2)
        self.assertEqual(rows[k1]["tokens"], 3500)
        self.assertAlmostEqual(rows[k1]["cost"], 1.75, places=6)
        self.assertIsNotNone(rows[k1]["last_used"])
        # 没被用过的密钥也要列出来，否则新建完就看不见了
        self.assertEqual(rows[k2]["n"], 0)
        self.assertIsNone(rows[k2]["last_used"])

    def test_key_summary_survives_key_without_member(self):
        """密钥可能不挂在任何成员上，统计不能因此丢行。"""
        kid, _ = self.store.create_key(member_id=None, note="orphan")
        rows = {r["id"]: r for r in self.store.key_summary()}
        self.assertIn(kid, rows)
        self.assertIsNone(rows[kid]["member_id"])


class MemberSelfServiceTest(unittest.TestCase):
    """密钥归成员自己管，密码归成员自己改，管理员都只碰得到外壳。"""

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
        self.mid = self.store.create_member(name="s", username="s",
                                            password=server.DEFAULT_PASSWORD)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.unlink(self.tmp.name)

    def _handler(self, body):
        h = server.Handler.__new__(server.Handler)
        h.headers = {"Cookie": f"{server.SESSION_COOKIE}="
                               f"{server._sign('m:' + str(self.mid))}"}
        h._sent = []
        h._send_json = lambda code, payload: h._sent.append((code, payload))
        h._error = lambda code, err, msg, **kw: h._sent.append(
            (code, {"error": {"code": err, "message": msg}}))
        h._read_json = lambda: body
        return h

    def _ctx(self):
        h = server.Handler.__new__(server.Handler)
        h.headers = {"Cookie": f"{server.SESSION_COOKIE}="
                               f"{server._sign('m:' + str(self.mid))}"}
        return server.Handler._auth_member(h)

    def test_create_own_key_returns_plaintext_once(self):
        h = self._handler({})
        server.Handler._create_own_key(h, self._ctx())
        code, payload = h._sent[-1]
        self.assertEqual(code, 200)
        raw = payload["key"]
        self.assertTrue(raw.startswith("sicau-PMT-"))
        self.assertTrue(payload["shown_once"])
        # 库里只有 hash，再查也拿不回明文
        stored = self.store.list_keys(self.mid)[0]
        self.assertNotIn(raw, str(stored))
        self.assertEqual(stored["key_hash"], Store.hash_key(raw))

    def test_member_can_hold_several_named_keys(self):
        """成员按用途建多把密钥，各自命名，互不影响。"""
        for name in ("笔记本", "台式机", "服务器"):
            h = self._handler({"name": name})
            server.Handler._create_own_key(h, self._ctx())
            code, payload = h._sent[-1]
            self.assertEqual(code, 200)
            self.assertEqual(payload["name"], name)
        keys = self.store.list_keys(self.mid)
        self.assertEqual(len(keys), 3)
        self.assertEqual({k["note"] for k in keys},
                         {"笔记本", "台式机", "服务器"})

    def test_key_count_is_capped(self):
        """上限只为防止无限刷，不是「每人一把」。"""
        for _ in range(server.Handler.MAX_KEYS_PER_MEMBER):
            self.store.create_key(member_id=self.mid)
        h = self._handler({})
        server.Handler._create_own_key(h, self._ctx())
        code, payload = h._sent[-1]
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"]["code"], "key_limit")

    def test_own_key_ignores_ip_allowlist_from_client(self):
        """来源 IP 限制是平台策略，成员不能自己设。"""
        h = self._handler({"ip_allowlist": "10.0.0.0/8"})
        server.Handler._create_own_key(h, self._ctx())
        self.assertEqual(h._sent[-1][0], 200)
        self.assertEqual(self.store.list_keys(self.mid)[0]["ip_allowlist"], "")

    def test_api_key_needs_balance_first(self):
        """余额为 0 时不给建 api 密钥。

        真实事故：新成员在控制台看到「API 通道」那个新建按钮，点了拿到
        一把密钥，贴进客户端后每次调用都是 429 api_balance_exhausted。
        这条通道从未发过钱，建出来的密钥永远调不通，应该在创建时就拦住。
        """
        h = self._handler({"kind": "api"})
        server.Handler._create_own_key(h, self._ctx())
        code, payload = h._sent[-1]
        self.assertEqual(code, 409)
        self.assertEqual(payload["error"]["code"], "api_balance_empty")
        self.assertEqual(self.store.list_keys(self.mid), [])

    def test_api_key_can_be_created_after_topup(self):
        self.store.api_topup(self.mid, 10.0, "管理员发放")
        h = self._handler({"kind": "api"})
        server.Handler._create_own_key(h, self._ctx())
        code, payload = h._sent[-1]
        self.assertEqual(code, 200)
        self.assertEqual(payload["kind"], "api")
        self.assertEqual(self.store.list_keys(self.mid)[0]["kind"], "api")

    def test_plan_key_needs_no_balance(self):
        """plan 通道走档位额度，余额为 0 不影响建密钥。"""
        h = self._handler({"kind": "plan"})
        server.Handler._create_own_key(h, self._ctx())
        self.assertEqual(h._sent[-1][0], 200)

    def test_cannot_delete_someone_elses_key(self):
        other = self.store.create_member(name="o", username="o")
        victim, _ = self.store.create_key(member_id=other)
        h = server.Handler.__new__(server.Handler)
        h.path = f"/api/me/keys/{victim}"
        h.headers = {"Cookie": f"{server.SESSION_COOKIE}="
                               f"{server._sign('m:' + str(self.mid))}"}
        h._sent = []
        h._send_json = lambda c, p: h._sent.append((c, p))
        h._error = lambda c, e, m, **kw: h._sent.append((c, {"error": {"code": e}}))
        server.Handler.do_DELETE(h)
        self.assertEqual(h._sent[-1][0], 404)
        self.assertIsNotNone(self.store.get_key(victim))

    def test_member_can_delete_own_key(self):
        kid, _ = self.store.create_key(member_id=self.mid)
        h = server.Handler.__new__(server.Handler)
        h.path = f"/api/me/keys/{kid}"
        h.headers = {"Cookie": f"{server.SESSION_COOKIE}="
                               f"{server._sign('m:' + str(self.mid))}"}
        h._sent = []
        h._send_json = lambda c, p: h._sent.append((c, p))
        server.Handler.do_DELETE(h)
        self.assertEqual(h._sent[-1][0], 200)
        self.assertIsNone(self.store.get_key(kid))

    def test_change_password_requires_old(self):
        h = self._handler({"old_password": "wrong",
                           "new_password": "newpass123"})
        server.Handler._change_own_password(h, self._ctx())
        code, payload = h._sent[-1]
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"]["code"], "bad_password")
        # 原密码仍然有效
        self.assertTrue(Store.verify_password(
            server.DEFAULT_PASSWORD,
            self.store.get_member(self.mid)["password_hash"]))

    def test_change_password_success(self):
        h = self._handler({"old_password": server.DEFAULT_PASSWORD,
                           "new_password": "newpass123"})
        server.Handler._change_own_password(h, self._ctx())
        self.assertEqual(h._sent[-1][0], 200)
        stored = self.store.get_member(self.mid)["password_hash"]
        self.assertTrue(Store.verify_password("newpass123", stored))
        self.assertFalse(Store.verify_password(server.DEFAULT_PASSWORD, stored))

    def test_admin_gets_no_plaintext_keys(self):
        self.store.create_key(member_id=self.mid)
        view = server.Handler._keys_view(
            server.Handler.__new__(server.Handler))
        self.assertTrue(all(k["key"] == "" for k in view))


class AdminKeyBoundaryTest(unittest.TestCase):
    """密钥只能由成员自己建、自己删；管理员只能看前缀和启停。

    代建与重置都要把明文交到管理员手上，那和「库里只存 hash」是矛盾的：
    前者让管理员拥有一把成员正在用的密钥，后者等于让管理员随时能换一把。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix="*.db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patches = [
            mock.patch.object(server, "get_store", lambda: self.store),
        ]
        for p in self.patches:
            p.start()
        self.admin = self.store.create_member(name="a", username="a",
                                              password=server.DEFAULT_PASSWORD,
                                              is_admin=1)
        self.mid = self.store.create_member(name="m", username="m",
                                            password=server.DEFAULT_PASSWORD)
        self.kid, self.raw = self.store.create_key(member_id=self.mid,
                                                   note="成员自己的")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        os.unlink(self.tmp.name)

    def _handler(self, path, body=None, method="do_PATCH"):
        h = server.Handler.__new__(server.Handler)
        h.path = path
        h.headers = {"Cookie": f"{server.SESSION_COOKIE}="
                               f"{server._sign('m:' + str(self.admin))}"}
        h.client_address = ("203.0.113.9", 1234)
        h._sent = []
        h._send_json = lambda code, payload: h._sent.append((code, payload))
        h._error = lambda code, err, msg, **kw: h._sent.append(
            (code, {"error": {"code": err, "message": msg}}))
        h._read_json = lambda: body or {}
        getattr(server.Handler, method)(h)
        return h._sent[-1]

    def test_admin_cannot_create_key_for_member(self):
        code, payload = self._handler("/api/keys", {"member_id": self.mid},
                                      method="do_POST")
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"]["code"], "admin_key_immutable")
        self.assertEqual(len(self.store.list_keys(self.mid)), 1)

    def test_admin_cannot_reset_key(self):
        """重置会交出一把新密钥的明文，和代建一样不该有。"""
        code, payload = self._handler(f"/api/keys/{self.kid}/reset")
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"]["code"], "admin_key_immutable")
        # 旧密钥必须原封不动地继续可用
        self.assertEqual(self.store.get_key(self.kid)["key_hash"],
                         Store.hash_key(self.raw))
        self.assertIsNotNone(self.store.get_key_by_hash(Store.hash_key(self.raw)))

    def test_admin_cannot_delete_key(self):
        code, payload = self._handler(f"/api/keys/{self.kid}",
                                      method="do_DELETE")
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"]["code"], "admin_key_immutable")
        self.assertIsNotNone(self.store.get_key(self.kid))

    def test_admin_can_still_disable_key(self):
        code, _ = self._handler(f"/api/keys/{self.kid}", {"enabled": False})
        self.assertEqual(code, 200)
        self.assertFalse(self.store.get_key(self.kid)["enabled"])

    def test_admin_patch_cannot_rewrite_other_fields(self):
        """启停之外的字段不认：白名单、归属、通道都只能由代码或成员调整。"""
        other = self.store.create_member(name="x", username="x",
                                         password=server.DEFAULT_PASSWORD)
        self._handler(f"/api/keys/{self.kid}", {
            "enabled": True,
            "ip_allowlist": "10.0.0.0/8",
            "model_allowlist": "qoder:dfmodel",
            "member_id": other,
            "kind": "api",
        })
        key = self.store.get_key(self.kid)
        self.assertEqual(key["ip_allowlist"], "")
        self.assertEqual(key["model_allowlist"], "")
        self.assertEqual(key["member_id"], self.mid)
        self.assertEqual(key["kind"], "plan")


class ForgedSessionKeyTest(unittest.TestCase):
    """源码里不能留任何能签出会话的固定密钥。

    曾经为了「升级不踢人」保留过 b"qoder2api-local-session-key"，那等于把
    签名能力连同源码一起公开出去：谁都能算出 m:1.<sig> 冒充 1 号成员，
    而 1 号通常就是管理员。
    """

    KNOWN_LEAKED = [b"qoder2api-local-session-key"]

    def test_no_legacy_key_attribute(self):
        self.assertFalse(hasattr(server, "_LEGACY_SESSION_KEYS"))

    def test_cookie_signed_with_leaked_key_rejected(self):
        import hashlib
        import hmac
        for key in self.KNOWN_LEAKED:
            sig = hmac.new(key, b"m:1", hashlib.sha256).hexdigest()[:32]
            self.assertIsNone(server._unsign(f"m:1.{sig}"))

    def test_only_current_key_verifies(self):
        """换成任意别的密钥签的都必须被拒。"""
        import hashlib
        import hmac
        sig = hmac.new(b"attacker-key", b"m:1", hashlib.sha256).hexdigest()[:32]
        self.assertIsNone(server._unsign(f"m:1.{sig}"))
        self.assertEqual(server._unsign(server._sign("m:1")), "m:1")


class RequestBodyTest(unittest.TestCase):
    """不消费请求体又保持 keep-alive，会把残留字节砌进下一轮报文。

    chunked 请求进不来（只认 Content-Length），超限时也读不干净，
    两种情况都必须关连接，否则反代和源站对边界的理解会不一致。
    """

    class _H:
        def __init__(self, headers, body=b""):
            self.headers = headers
            self.rfile = io.BytesIO(body)
            self.close_connection = False

    def _read(self, headers, body=b""):
        h = self._H(headers, body)
        return server.Handler._read_json(h), h

    def test_chunked_rejected_and_connection_closed(self):
        data, h = self._read({"Transfer-Encoding": "chunked"})
        self.assertEqual(data, {})
        self.assertTrue(h.close_connection)

    def test_oversized_body_closes_connection(self):
        data, h = self._read({"Content-Length": str(server.MAX_BODY_BYTES + 1)})
        self.assertEqual(data, {})
        self.assertTrue(h.close_connection)

    def test_garbage_content_length_closes_connection(self):
        data, h = self._read({"Content-Length": "not-a-number"})
        self.assertEqual(data, {})
        self.assertTrue(h.close_connection)

    def test_normal_body_parsed_and_kept_alive(self):
        data, h = self._read({"Content-Length": "7"}, b'{"a":1}')
        self.assertEqual(data, {"a": 1})
        self.assertFalse(h.close_connection)


class KeyInQueryRejectedTest(unittest.TestCase):
    """密钥不能走查询串：URL 会进反代日志、浏览器历史和 Referer。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.patch = mock.patch.object(server, "get_store", lambda: self.store)

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.tmp.name)

    def test_query_key_alone_is_not_enough(self):
        mid = self.store.create_member(name="m")
        _, raw = self.store.create_key(member_id=mid)
        h = make({"Cookie": ""})
        h.path = "/api/me?key=" + raw
        self.patch.start()
        try:
            self.assertIsNone(server.Handler._auth_by_key(h))
        finally:
            self.patch.stop()

    def test_header_still_works(self):
        mid = self.store.create_member(name="m")
        _, raw = self.store.create_key(member_id=mid)
        h = make({"Authorization": "Bearer " + raw})
        h.path = "/api/me"
        self.patch.start()
        try:
            ctx = server.Handler._auth_by_key(h)
            self.assertIsNotNone(ctx)
            self.assertEqual(ctx["member"]["id"], mid)
        finally:
            self.patch.stop()
