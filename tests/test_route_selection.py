"""选账号的端到端测试：可用模型是最终依据，优先账号只决定先试哪家。

这三条是这次要修的 bug 的反面：
  1. 只允许 WorkBuddy 的 DeepSeek 时，请求不能落到 Qoder 的 dfmodel；
  2. 优先账号属于哪家，就先试哪家的账号；
  3. 一家全挂掉时，要能换到另一家重连同款模型。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import guard  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402


def catalog():
    """合并后的目录：一个显示名，两家上游各自的 key 不同。"""
    return {
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


class RouteSelectionTest(unittest.TestCase):
    """只测「能不能选到这个来源」的判断，不碰网络。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.qid = self.store.add_account(provider="qoder", uid="q1",
                                          region="global", credential="pt-x",
                                          name="q1")
        self.wid = self.store.add_account(provider="workbuddy", uid="w1",
                                          region="cn", credential="{}",
                                          name="w1")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def pick(self, allowed, model="deepseek-v4.1-flash", prefer=None):
        routes = guard.allowed_routes(allowed, model, catalog(), prefer=prefer)
        for route in routes:
            hit = self.store.pick_account(route["region"],
                                          provider=route["provider"])
            if hit is not None:
                return hit["provider"], route["model"]
        return None

    def test_only_workbuddy_allowed_never_touches_qoder(self):
        got = self.pick({"workbuddy:deepseek-v4.1-flash"})
        self.assertEqual(got, ("workbuddy", "deepseek-v4.1-flash"))

    def test_only_qoder_allowed_never_touches_workbuddy(self):
        got = self.pick({"qoder:dfmodel"})
        self.assertEqual(got, ("qoder", "dfmodel"))

    def test_preferred_vendor_goes_first(self):
        """两家都开了时，优先账号所属厂商排第一。"""
        self.assertEqual(self.pick(None, prefer="workbuddy"),
                         ("workbuddy", "deepseek-v4.1-flash"))
        self.assertEqual(self.pick(None, prefer="qoder"),
                         ("qoder", "dfmodel"))

    def test_preferred_vendor_without_model_access_is_skipped(self):
        """优先 WorkBuddy，但只开了 Qoder 的来源，只能走 Qoder。"""
        self.assertEqual(
            self.pick({"qoder:dfmodel"}, prefer="workbuddy"), ("qoder", "dfmodel"))

    def test_model_not_on_preferred_vendor_falls_to_other(self):
        """Qwen 只有 Qoder 有，优先账号是 WorkBuddy 也得走 Qoder。"""
        self.assertEqual(
            self.pick(None, "qwen-3.8-flash", prefer="workbuddy"),
            ("qoder", "qfmodel"))

    def test_failover_to_other_vendor(self):
        """Qoder 全部在冷却时，请求要换到 WorkBuddy，而不是直接失败。"""
        self.store.set_account_cooldown(self.qid, 600, "test")
        self.assertEqual(self.pick(None, prefer="qoder"),
                         ("workbuddy", "deepseek-v4.1-flash"))

    def test_no_source_available_at_all(self):
        self.store.set_account_cooldown(self.qid, 600, "test")
        self.store.set_account_cooldown(self.wid, 600, "test")
        self.assertIsNone(self.pick(None))


class StatusShapeTest(unittest.TestCase):
    """/api/status 要给管理端每家来源，否则界面只能显示一个厂商。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        server.STORE = self.store

    def tearDown(self):
        os.unlink(self.tmp.name)
        server.STORE = None

    def test_sources_listed_per_model(self):
        rows = server.Handler.models_view(catalog())
        ds = next(r for r in rows if r["id"] == "deepseek-v4.1-flash")
        self.assertEqual([s["spec"] for s in ds["sources"]],
                         ["qoder:dfmodel", "workbuddy:deepseek-v4.1-flash"])
        self.assertEqual(ds["name"], "DeepSeek-V4.1-Flash")

    def test_single_vendor_model_has_one_source(self):
        rows = server.Handler.models_view(catalog())
        qw = next(r for r in rows if r["id"] == "qwen-3.8-flash")
        self.assertEqual([s["provider"] for s in qw["sources"]], ["qoder"])

    def test_every_spec_passes_its_own_guard(self):
        """界面勾出来的每一条来源，网关都必须认。"""
        cat = catalog()
        for mid, meta in cat.items():
            for src in guard.model_sources(mid, cat):
                guard.check_model({"model_allowlist": src["spec"]}, None, mid, cat)

    def test_member_dashboard_sees_provider_scoped_allowlist(self):
        """成员只勾了某一家厂商的来源时，控制台仍要能列出这个模型。

        白名单里存的是 `厂商:上游key`，拿合并后的内部 id 去比永远不命中，
        成员自己看到的可用模型列表会变成空的。
        """
        mid = self.store.create_member(
            name="t", model_allowlist="workbuddy:deepseek-v4.1-flash")
        member = self.store.get_member(mid)
        kid, _raw = self.store.create_key(member_id=mid)
        key = self.store.get_key(kid)
        with mock.patch.object(server, "all_models", catalog):
            dash = server.Handler._member_dashboard(
                server.Handler.__new__(server.Handler),
                {"member": member, "key": key})
        self.assertEqual([m["id"] for m in dash["models"]],
                         ["deepseek-v4.1-flash"])

    def test_member_dashboard_empty_allowlist_lists_everything(self):
        mid = self.store.create_member(name="t", model_allowlist="")
        member = self.store.get_member(mid)
        kid, _raw = self.store.create_key(member_id=mid)
        key = self.store.get_key(kid)
        with mock.patch.object(server, "all_models", catalog):
            dash = server.Handler._member_dashboard(
                server.Handler.__new__(server.Handler),
                {"member": member, "key": key})
        self.assertEqual(len(dash["models"]), len(catalog()))




class FailoverThroughHandlerTest(unittest.TestCase):
    """把整条调用链路跑一遍：请求打到哪家、客户端收到什么。"""

    def setUp(self):
        import io
        import json
        self.io, self.json = io, json
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        server.STORE = self.store
        server.clear_model_cache()
        self.qid = self.store.add_account(provider="qoder", uid="q1",
                                          region="global", credential="pt-x",
                                          name="q1")
        self.wid = self.store.add_account(provider="workbuddy", uid="w1",
                                          region="cn", credential="{}",
                                          name="w1")
        self.store.set_account_tokens(self.qid, "t", "r", 9e11)
        self.store.set_account_tokens(self.wid, "t", "r", 9e11)
        self.calls = []
        self._patch = [
            mock.patch.object(providers.get("qoder"), "list_models",
                              lambda a: [{"id": "dfmodel",
                                          "name": "DeepSeek-V4.1-Flash"}]),
            mock.patch.object(providers.get("qoder"), "build_chat_body",
                              lambda m, k, **kw: self.json.dumps({"m": k}).encode()),
            mock.patch.object(providers.get("workbuddy"), "build_chat_body",
                              lambda m, k, **kw: self.json.dumps({"m": k}).encode()),
        ]
        for p in self._patch:
            p.start()
        self.mid = self.store.create_member(name="t", model_allowlist="")
        _, self.raw = self.store.create_key(member_id=self.mid)

    def tearDown(self):
        for p in self._patch:
            p.stop()
        server.STORE = None
        server.clear_model_cache()
        os.unlink(self.tmp.name)

    def _stream_ok(self, prov):
        def gen(account, body, model):
            self.calls.append((prov, account["provider"], model))
            yield ('data: {"id":"c1","object":"chat.completion.chunk",'
                   '"choices":[{"index":0,"delta":{"content":"hi"}}]}')
            yield "data: [DONE]"
        return gen

    def test_down_vendor_fails_over_to_the_other(self):
        """Qoder 连不上时，同一个请求要换到 WorkBuddy 跑完，客户端只看得到流。"""
        import qoder as q

        def broken(account, body, model):
            self.calls.append(("qoder", account["provider"], model))
            raise q.UpstreamError(0, "", "network error: refused")
            yield
        with mock.patch.object(providers.get("qoder"), "chat_stream", broken), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._stream_ok("workbuddy")):
            out = self._post("DeepSeek-V4.1-Flash", stream=True)
        self.assertEqual([c[0] for c in self.calls], ["qoder", "workbuddy"])
        self.assertIn(b'"content":"hi"', out)
        self.assertIn(b"[DONE]", out)
        self.assertNotIn(b"network error", out)

    def test_request_rejection_does_not_fail_over(self):
        """请求内容被拒（渠道校验）换一家也一样被拦，不该白跑一趟。"""
        import workbuddy

        def rejected(account, body, model):
            self.calls.append(("qoder", account["provider"], model))
            raise workbuddy.UpstreamError(403, '{"code":"11128"}')
            yield
        with mock.patch.object(providers.get("qoder"), "chat_stream", rejected), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._stream_ok("workbuddy")):
            out = self._post("DeepSeek-V4.1-Flash", stream=True)
        self.assertEqual([c[0] for c in self.calls], ["qoder"])
        self.assertIn(b"upstream_rejected", out)

    def test_request_rejection_tries_another_account_same_vendor(self):
        """渠道校验（11128）是账号所属渠道的规则，应当换同厂商账号重试。

        真实案例：国际版要求首条消息必须是 system，国内版不查。
        """
        import workbuddy

        second = self.store.add_account(provider="workbuddy", uid="w2",
                                        region="cn", credential="{}",
                                        name="w2")
        self.store.set_account_tokens(second, "t", "r", 9e11)

        def rejected(account, body, model):
            self.calls.append(("workbuddy", account["uid"], model))
            raise workbuddy.UpstreamError(403, '{"code":"11128"}')
            yield

        def ok(account, body, model):
            self.calls.append(("workbuddy", account["uid"], model))
            yield ('data: {"id":"c1","object":"chat.completion.chunk",'
                   '"choices":[{"index":0,"delta":{"content":"hi"}}]}')
            yield "data: [DONE]"

        self.store.update_member(self.mid,
                                 model_allowlist="workbuddy:deepseek-v4.1-flash")
        self.store.update_member(self.mid, preferred_account_id=self.wid)

        def stream(account, body, model):
            return rejected(account, body, model) if len(self.calls) == 0 \
                else ok(account, body, model)

        with mock.patch.object(providers.get("workbuddy"), "chat_stream",
                               side_effect=stream):
            out = self._post("DeepSeek-V4.1-Flash", stream=True)
        self.assertEqual(len(self.calls), 2, "应当换到第二个账号")
        self.assertNotEqual(self.calls[0][1], self.calls[1][1])
        self.assertIn(b'"content":"hi"', out)
        self.assertNotIn(b"upstream_rejected", out)

    def test_allowlist_restricts_to_one_vendor(self):
        """只允许 WorkBuddy 的来源时，Qoder 根本不该被调用。"""
        self.store.update_member(self.mid,
                                 model_allowlist="workbuddy:deepseek-v4.1-flash")
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._stream_ok("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._stream_ok("workbuddy")):
            self._post("DeepSeek-V4.1-Flash", stream=True)
        self.assertEqual([c[0] for c in self.calls], ["workbuddy"])

    def test_preferred_account_picks_vendor_first(self):
        self.store.update_member(self.mid, preferred_account_id=self.wid)
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._stream_ok("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._stream_ok("workbuddy")):
            self._post("DeepSeek-V4.1-Flash", stream=True)
        self.assertEqual(self.calls[0][0], "workbuddy")

    def test_forbidden_response_hides_vendor(self):
        """拒绝报文里不能出现厂商名或上游 key。

        白名单内部按 `厂商:上游 key` 存，但客户端只该知道哪个模型没开。
        这条从 Handler 入口走完整链路，盯的是真正回给调用方的字节。
        """
        self.store.update_member(self.mid, model_allowlist="workbuddy:glm-5.3")
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._stream_ok("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._stream_ok("workbuddy")):
            out = self._post("DeepSeek-V4.1-Flash", stream=False)
        text = out.decode()
        self.assertIn("model_forbidden", text)
        self.assertNotIn("workbuddy", text)
        self.assertNotIn("qoder", text)
        self.assertNotIn("dfmodel", text)

    def _usage_stream(self, prov, prompt=1000, completion=500, credit=0.5):
        """带 usage 的流：成本要按这一帧的 token 算出来。"""
        def gen(account, body, model):
            self.calls.append((prov, account["provider"], model))
            yield ('data: {"id":"c1","object":"chat.completion.chunk",'
                   '"choices":[{"index":0,"delta":{"content":"hi"}}]}')
            usage = self.json.dumps({
                "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {"cached_tokens": 400},
                "credit": credit,
            })
            yield 'data: {"id":"c1","choices":[],"usage":' + usage + '}'
            yield "data: [DONE]"
        return gen

    def test_request_cost_is_recorded_from_tokens(self):
        """一次请求的账要按 token × 单价落到 usage.cost 上。"""
        import pricing
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._usage_stream("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._usage_stream("workbuddy")):
            self._post("DeepSeek-V4.1-Flash", stream=True)
        row = self.store.recent_usage(1)[0]
        self.assertEqual(row["prompt_tokens"], 1000)
        self.assertEqual(row["completion_tokens"], 500)
        self.assertEqual(row["cache_read_tokens"], 400)
        price, _kind = pricing.lookup("dfmodel")
        expected = pricing.cost_of(price, prompt_tokens=1000,
                                   completion_tokens=500, cache_read_tokens=400)
        self.assertAlmostEqual(row["cost"], expected, places=10)
        self.assertGreater(row["cost"], 0)
        # 上游积分照旧记着，只用于对账
        self.assertAlmostEqual(row["credits"], 0.5, places=4)

    def test_price_change_does_not_rewrite_old_rows(self):
        """改价只影响之后的请求，旧行的 cost 不变。"""
        import pricing
        self.store.set_setting("model_prices",
                               {"dfmodel": {"input": 10.0, "output": 20.0}})
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._usage_stream("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._usage_stream("workbuddy")):
            self._post("DeepSeek-V4.1-Flash", stream=True)
        row = self.store.recent_usage(1)[0]
        first = row["cost"]
        self.assertGreater(first, 0)

        self.store.set_setting("model_prices",
                               {"dfmodel": {"input": 30.0, "output": 60.0}})
        again = self.store.recent_usage(1)[0]
        self.assertAlmostEqual(again["cost"], first, places=10)
        # 额度也按已经写下的 cost 累计，不跟着新价走
        self.assertAlmostEqual(self.store.cost_total(self.mid), first, places=10)

    def test_admin_price_override_reaches_billing(self):
        """定价页改过的单价要真的用在计费上，不能只改显示。"""
        import pricing
        self.store.set_setting("model_prices",
                               {"dfmodel": {"input": 10.0, "output": 20.0}})
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._usage_stream("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._usage_stream("workbuddy")):
            self._post("DeepSeek-V4.1-Flash", stream=True)
        row = self.store.recent_usage(1)[0]
        self.assertEqual(row["price_source"], "override")
        expected = pricing.cost_of({"input": 10.0, "output": 20.0},
                                   prompt_tokens=1000, completion_tokens=500,
                                   cache_read_tokens=400)
        self.assertAlmostEqual(row["cost"], expected, places=10)
        self.assertGreater(row["cost"], 0)

    def test_retired_model_is_refused_without_calling_upstream(self):
        """下架的模型直接 410，不打上游、不记成功账。"""
        with mock.patch.object(providers.get("workbuddy"), "chat_stream",
                               self._stream_ok("workbuddy")), \
                mock.patch.object(providers.get("qoder"), "chat_stream",
                                  self._stream_ok("qoder")):
            out = self._post("deepseek-v4-flash", stream=True)
        self.assertEqual(self.calls, [])
        self.assertIn(b"model_retired", out)
        row = self.store.recent_usage(1)[0]
        self.assertEqual(row["status"], 410)

    def test_daily_limit_blocks_before_calling_upstream(self):
        """额度到 100% 时不该再打到上游，直接 429。"""
        self.store.update_member(self.mid, limit_daily=0.000001)
        self.store.record_usage(key_id=1, member_id=self.mid, account_id=self.qid,
                                model="dfmodel", prompt_tokens=0,
                                completion_tokens=0, cost=0.001)
        with mock.patch.object(providers.get("qoder"), "chat_stream",
                               self._stream_ok("qoder")), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream",
                                  self._stream_ok("workbuddy")):
            out = self._post("DeepSeek-V4.1-Flash", stream=True)
        self.assertEqual(self.calls, [])
        self.assertIn(b"quota_daily_exceeded", out)

    def test_member_sees_only_own_costs(self):
        """成员控制台的曲线只能算自己的账，不能拿全站数字充数。"""
        other = self.store.create_member(name="other")
        self.store.record_usage(key_id=1, member_id=other, account_id=self.qid,
                                model="dfmodel", prompt_tokens=0,
                                completion_tokens=0, cost=5.0)
        self.store.record_usage(key_id=1, member_id=self.mid, account_id=self.qid,
                                model="dfmodel", prompt_tokens=0,
                                completion_tokens=0, cost=0.25)
        series = self.store.daily_series_for_member(self.mid, 3)
        self.assertAlmostEqual(series[-1]["cost"], 0.25, places=6)

    def test_nonstream_upstream_error_is_reported_not_500(self):
        """非流式时上游报错要回 502（能正常回错误体），不能炸成 500。

        历史上非流式分支没接 UpstreamStatusError，上游 403/105「登录过期」
        直接冒到 socketserver，客户端拿到 500，账本还按 status=200 记一行。
        """
        import convert

        def expired(account, body, model):
            raise convert.UpstreamStatusError(
                403, '{"code":"105","msg":"channel qoder-global acc=u42"}')
            yield
        with mock.patch.object(providers.get("qoder"), "chat_stream", expired), \
                mock.patch.object(providers.get("workbuddy"), "chat_stream", expired):
            out = self._post("DeepSeek-V4.1-Flash", stream=False)
        self.assertIn(b'"upstream_status"', out)
        self.assertNotIn(b"Traceback", out)
        # 上游原文可能带渠道名和账号标识，不能跟着错误响应回给客户端
        for word in (b"qoder-global", b"acc=u42", b"105"):
            self.assertNotIn(word, out)
        row = self.store.recent_usage(limit=1)[0]
        self.assertEqual(row["status"], 502)

    def _post(self, model, stream):
        body = self.json.dumps({
            "model": model, "stream": stream,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode()
        h = server.Handler.__new__(server.Handler)
        h.headers = {"Authorization": f"Bearer {self.raw}",
                     "Content-Length": str(len(body))}
        h.client_address = ("127.0.0.1", 1)
        h._stream_started = False
        h.close_connection = False
        h.rfile = self.io.BytesIO(body)
        h.wfile = self.io.BytesIO()
        h.send_response = lambda c, m=None: None
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None
        h._chat_completions()
        return h.wfile.getvalue()


if __name__ == "__main__":
    unittest.main()


class MemberDetailTest(unittest.TestCase):
    """成员详情：要能看出这个成员在哪个上游账号上花掉多少。

    分配额度靠这两个问题回答：谁在吃资源、吃的是哪家账号。
    所以按账号和按模型两条维度都得算对，未绑定账号的请求也不能丢。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.a1 = self.store.add_account(provider="qoder", uid="q1", region="cn",
                                         credential="pt-x", name="Qoder 主号")
        self.a2 = self.store.add_account(provider="workbuddy", uid="w1", region="cn",
                                         credential="{}", name="WorkBuddy A")
        self.store.create_member(name="张三", username="zs", password="pw",
                                 limit_daily=10, limit_total=70)
        self.mid = [m for m in self.store.list_members() if m["name"] == "张三"][0]["id"]

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _use(self, account_id, model, prompt, completion, cost, credits=0.0):
        self.store.record_usage(key_id=None, member_id=self.mid,
                                account_id=account_id, model=model,
                                prompt_tokens=prompt, completion_tokens=completion,
                                cost=cost, credits=credits, price_source="builtin")

    def test_account_summary_splits_cost_across_accounts(self):
        """两个账号各用一次不同模型，成本要分别落在各自账号下。"""
        self._use(self.a1, "glm-5.3", 1000, 500, 0.02)
        self._use(self.a2, "kimi-k3", 2000, 800, 0.05)
        rows = self.store.member_account_summary(self.mid)
        by = {}
        for r in rows:
            by.setdefault(r["account_id"], {})[r["model"]] = r
        self.assertEqual(set(by), {self.a1, self.a2})
        self.assertAlmostEqual(by[self.a1]["glm-5.3"]["cost"], 0.02, places=6)
        self.assertAlmostEqual(by[self.a2]["kimi-k3"]["cost"], 0.05, places=6)
        self.assertEqual(by[self.a1]["glm-5.3"]["tokens"], 1500)

    def test_account_summary_keeps_unbound_requests(self):
        """没有账号的请求（失败提前返回）也要出现，account_id 记 0。"""
        self._use(None, "glm-5.3", 100, 0, 0.0)
        rows = self.store.member_account_summary(self.mid)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["account_id"])

    def test_account_summary_only_that_member(self):
        self.store.create_member(name="李四", username="ls", password="pw")
        other = [m for m in self.store.list_members() if m["name"] == "李四"][0]["id"]
        self._use(self.a1, "glm-5.3", 1000, 0, 0.01)
        self.store.record_usage(key_id=None, member_id=other, account_id=self.a1,
                                model="glm-5.3", prompt_tokens=9000, completion_tokens=0,
                                cost=9.99)
        rows = self.store.member_account_summary(self.mid)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cost"], 0.01, places=6)

    def test_daily_by_account_groups_per_day(self):
        series = self.store.member_daily_by_account(self.mid, 3)
        self.assertEqual(len(series), 3)
        self._use(self.a1, "glm-5.3", 1000, 0, 0.03)
        self._use(self.a2, "glm-5.3", 1000, 0, 0.07)
        series = self.store.member_daily_by_account(self.mid, 3)
        today = series[-1]
        self.assertAlmostEqual(today["by_account"][self.a1], 0.03, places=6)
        self.assertAlmostEqual(today["by_account"][self.a2], 0.07, places=6)
        # 前两天空着，不能凭空冒出账号
        self.assertEqual(series[0]["by_account"], {})

    def test_detail_view_aggregates_by_account_then_model(self):
        """接口把 账号 × 模型 的细行聚成「账号 → 模型列表」两层。"""
        self._use(self.a1, "glm-5.3", 1000, 0, 0.02)
        self._use(self.a1, "kimi-k3", 1000, 0, 0.06)
        self._use(self.a2, "glm-5.3", 1000, 0, 0.01)
        detail = server.Handler._member_detail(self._new_handler(), self.mid)
        self.assertEqual(detail["member"]["name"], "张三")
        self.assertEqual(len(detail["by_account"]), 2)
        top = detail["by_account"][0]
        # 成本高的账号排前面
        self.assertEqual(top["account_id"], self.a1)
        self.assertAlmostEqual(top["cost"], 0.08, places=6)
        self.assertEqual(len(top["models"]), 2)
        # 账号内的模型也按成本降序
        self.assertEqual(top["models"][0]["model"], "kimi-k3")
        self.assertAlmostEqual(detail["totals"]["cost"], 0.09, places=6)
        self.assertEqual(detail["totals"]["accounts"], 2)
        # 按模型合计跨账号
        by_model = {m["model"]: m for m in detail["by_model"]}
        self.assertAlmostEqual(by_model["glm-5.3"]["cost"], 0.03, places=6)

    def test_detail_view_unknown_member_is_empty(self):
        self.assertEqual(server.Handler._member_detail(self._new_handler(), 99999), {})

    def _new_handler(self):
        h = server.Handler.__new__(server.Handler)
        self._store_patch = mock.patch.object(server, "get_store",
                                              return_value=self.store)
        self._store_patch.start()
        self.addCleanup(self._store_patch.stop)
        return h


class MemberBillingUnitTest(unittest.TestCase):
    """成员详情的消耗按账号所属厂商的计费口径分开记。

    Qoder / WorkBuddy 的上游按积分扣额度，这类账号的消耗记积分；其他厂商
    按 token 计费，记元。两种量纲不能相加，所以每一处都要带 unit，
    并且只能在本口径内算占比。
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = Store(self.tmp.name)
        self.qoder = self.store.add_account(provider="qoder", uid="q1", region="cn",
                                            credential="pt-x", name="Qoder 主号")
        self.wb = self.store.add_account(provider="workbuddy", uid="w1", region="cn",
                                         credential="{}", name="WorkBuddy A")
        self.cline = self.store.add_account(provider="cline", uid="c1",
                                            region="default", credential="ck-x",
                                            name="Cline 备用")
        self.mid = self.store.create_member(name="张三")
        self._store_patch = mock.patch.object(server, "get_store", lambda: self.store)
        self._store_patch.start()
        self.addCleanup(self._store_patch.stop)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _use(self, account_id, model, cost, credits, prompt=100, completion=50):
        self.store.record_usage(key_id=None, member_id=self.mid,
                                account_id=account_id, model=model,
                                prompt_tokens=prompt, completion_tokens=completion,
                                cost=cost, credits=credits, price_source="builtin")

    def _detail(self):
        return server.Handler._member_detail(self._new_handler(), self.mid)

    def _new_handler(self):
        return server.Handler.__new__(server.Handler)

    def test_units_follow_account_provider(self):
        d = self._detail()
        units = {}
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self._use(self.wb, "deepseek-v4.1-flash", 0.02, 3.0)
        self._use(self.cline, "glm-5.3", 0.05, 0.0)
        units = {a["account_id"]: a["unit"] for a in self._detail()["by_account"]}
        self.assertEqual(units[self.qoder], "credit")
        self.assertEqual(units[self.wb], "credit")
        self.assertEqual(units[self.cline], "token")

    def test_credit_and_token_are_not_summed(self):
        """积分与元分账：积分口径里只累计积分，元口径里只累计元。"""
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self._use(self.cline, "glm-5.3", 0.05, 0.0)
        b = self._detail()["billing"]
        self.assertEqual(b["credit"]["accounts"], 1)
        self.assertAlmostEqual(b["credit"]["credits"], 2.0, places=4)
        self.assertEqual(b["token"]["accounts"], 1)
        self.assertAlmostEqual(b["token"]["cost"], 0.05, places=6)
        # 积分账号的估算成本不进 token 口径的合计
        self.assertAlmostEqual(b["token"]["credits"], 0.0, places=4)

    def test_account_amount_uses_own_unit(self):
        self._use(self.qoder, "dfmodel", 0.01, 2.5)
        self._use(self.cline, "glm-5.3", 0.05, 0.0)
        rows = {a["account_id"]: a for a in self._detail()["by_account"]}
        self.assertAlmostEqual(rows[self.qoder]["amount"], 2.5, places=4)
        self.assertAlmostEqual(rows[self.cline]["amount"], 0.05, places=6)

    def test_provider_board_splits_by_unit(self):
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self._use(self.wb, "glm-5.3", 0.02, 4.0)
        self._use(self.cline, "glm-5.3", 0.05, 0.0)
        prov = {p["slug"]: p for p in self._detail()["by_provider"]}
        self.assertEqual(prov["qoder"]["unit"], "credit")
        self.assertAlmostEqual(prov["qoder"]["amount"], 2.0, places=4)
        self.assertEqual(prov["workbuddy"]["accounts"], 1)
        self.assertEqual(prov["cline"]["unit"], "token")
        self.assertAlmostEqual(prov["cline"]["amount"], 0.05, places=6)

    def test_same_model_in_two_units_stays_two_rows(self):
        """同一个模型名两家都在跑时，积分行与元行分开，不能合并成一行。"""
        self._use(self.qoder, "glm-5.3", 0.01, 2.0)
        self._use(self.cline, "glm-5.3", 0.05, 0.0)
        rows = {(x["model"], x["unit"]): x for x in self._detail()["by_model_unit"]}
        self.assertAlmostEqual(rows[("glm-5.3", "credit")]["amount"], 2.0, places=4)
        self.assertAlmostEqual(rows[("glm-5.3", "token")]["amount"], 0.05, places=6)
        # 出入 token 要一起带上，明细表按它分列
        self.assertEqual(rows[("glm-5.3", "token")]["prompt_tokens"], 100)

    def test_daily_series_split_by_unit(self):
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self._use(self.cline, "glm-5.3", 0.05, 0.0)
        daily = self._detail()["daily"]
        self.assertAlmostEqual(daily["credit"][-1]["by_account"][self.qoder], 2.0, places=4)
        self.assertAlmostEqual(daily["token"][-1]["by_account"][self.cline], 0.05, places=6)
        # 每种口径的数据里不能混进另一种口径的账号
        self.assertNotIn(self.cline, daily["credit"][-1]["by_account"])
        self.assertNotIn(self.qoder, daily["token"][-1]["by_account"])

    def test_deleted_account_keeps_its_history(self):
        """账号删掉后历史消耗还在：不能当成「没落到账号」，也不能算进未分配。

        这类行有真实的 token 与积分消耗，只是账号记录不在了。
        """
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self.store.record_usage(key_id=None, member_id=self.mid, account_id=9999,
                                model="auto", prompt_tokens=30,
                                completion_tokens=50, cost=0.004, credits=0.0,
                                status=200)
        d = self._detail()
        gone = [a for a in d["by_account"] if a["account_id"] == 9999]
        self.assertEqual(len(gone), 1)
        self.assertTrue(gone[0].get("deleted"))
        self.assertFalse(gone[0].get("unbound"))
        self.assertEqual(gone[0]["requests"], 1)
        self.assertEqual(gone[0]["tokens"], 80)
        # 它仍然出现在各自口径的榜里，不会凭空消失
        models = {(x["model"], x["unit"]) for x in d["by_model_unit"]}
        self.assertIn(("auto", "token"), models)
        self.assertEqual(d["unbound"], [])

    def test_unbound_requests_not_in_model_or_provider_boards(self):
        """没选到账号的失败请求没有消耗，不该出现在模型榜和厂商榜里。"""
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self.store.record_usage(key_id=None, member_id=self.mid, account_id=None,
                                model="ultimate", prompt_tokens=0,
                                completion_tokens=0, status=403,
                                error="no_account")
        d = self._detail()
        self.assertNotIn("ultimate", {x["model"] for x in d["by_model_unit"]})
        self.assertEqual([p["slug"] for p in d["by_provider"]], ["qoder"])
        # 但它仍算一次失败请求，要进合计
        self.assertEqual(d["totals"]["requests"], 2)
        self.assertEqual(d["totals"]["errors"], 1)

    def test_unbound_requests_kept_out_of_account_boards(self):
        """没落到账号的失败请求不算某个账号，也不算某种结算方式。"""
        self._use(self.qoder, "dfmodel", 0.01, 2.0)
        self.store.record_usage(key_id=None, member_id=self.mid, account_id=None,
                                model="glm-5.3", prompt_tokens=0,
                                completion_tokens=0, status=503,
                                error="no_account")
        d = self._detail()
        self.assertEqual([a["account_id"] for a in d["by_account"]], [self.qoder])
        self.assertEqual(len(d["unbound"]), 1)
        self.assertEqual(d["unbound"][0]["requests"], 1)
        # 请求总量 = 各口径之和 + 未分配，三条线对得上
        billed = sum(v["requests"] for v in d["billing"].values())
        self.assertEqual(billed + d["unbound"][0]["requests"], d["totals"]["requests"])
        self.assertEqual(d["totals"]["accounts"], 1)



class UsagePagingTest(unittest.TestCase):
    """调用明细的分页：走 idx_usage_member_time，按天数过滤，封顶可配。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))
        self.mid = self.store.create_member(name="pager")
        self.aid = self.store.add_account(
            uid="u1", region="global", credential="pt-x",
            provider="qoder", name="acc")
        # 30 条，每条往前推一天，方便按天数筛。record_usage 的 created_at
        # 是写死的 time.time()，只能事后改
        now = time.time()
        for i in range(30):
            self.store.record_usage(key_id=None, member_id=self.mid,
                                    account_id=self.aid, model="m%d" % (i % 3),
                                    prompt_tokens=10, completion_tokens=5,
                                    cost=0.01, credits=0.1)
        with self.store._conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM usage WHERE member_id=? ORDER BY id DESC",
                (self.mid,)).fetchall()]
            for i, rid in enumerate(ids):
                c.execute("UPDATE usage SET created_at=? WHERE id=?",
                          (now - i * 86400, rid))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_page_slices_without_overlap(self):
        p1, total = self.store.usage_page(self.mid, limit=10, offset=0)
        p2, _ = self.store.usage_page(self.mid, limit=10, offset=10)
        self.assertEqual(total, 30)
        self.assertEqual(len(p1), 10)
        self.assertEqual(len(p2), 10)
        # 两页不能有重复行
        self.assertFalse({r["id"] for r in p1} & {r["id"] for r in p2})

    def test_ordered_newest_first(self):
        rows, _ = self.store.usage_page(self.mid, limit=5)
        ts = [r["created_at"] for r in rows]
        self.assertEqual(ts, sorted(ts, reverse=True))

    def test_days_filter(self):
        rows, total = self.store.usage_page(self.mid, since=time.time() - 6 * 86400,
                                            limit=50)
        self.assertLessEqual(total, 8)
        self.assertEqual(len(rows), total)

    def test_offset_past_end_returns_empty(self):
        rows, total = self.store.usage_page(self.mid, limit=10, offset=100)
        self.assertEqual(rows, [])
        self.assertEqual(total, 30)

    def test_isolation_between_members(self):
        other = self.store.create_member(name="other")
        rows, total = self.store.usage_page(other, limit=10)
        self.assertEqual((rows, total), ([], 0))

    def test_usage_count_matches_page_total(self):
        _, total = self.store.usage_page(self.mid, limit=5)
        self.assertEqual(self.store.usage_count(self.mid), total)


class UsagePageApiTest(unittest.TestCase):
    """GET /api/members/<id>/usage 的参数夹取与封顶。"""

    def test_clamp_int_bounds(self):
        self.assertEqual(server._clamp_int("7", 0, 3650, 0), 7)
        self.assertEqual(server._clamp_int("-5", 0, 3650, 0), 0)
        self.assertEqual(server._clamp_int("99999", 1, 200, 20), 200)
        self.assertEqual(server._clamp_int("oops", 1, 200, 20), 20)
        self.assertEqual(server._clamp_int(None, 1, 200, 20), 20)

    def test_cap_is_configurable_and_sane(self):
        # 3000 是「翻不到更早」的策略值，不是容量上限：
        # 500 人各 3000 条也只占约 238MB，翻到第 3000 条约 0.2ms
        self.assertGreaterEqual(server.USAGE_PAGE_MAX, 1000)


class MemberSelfViewTest(unittest.TestCase):
    """成员自己看到的用量：只有模型和元，没有账号、厂商、积分。

    账号是平台侧的调度细节，积分是上游的计费单位。这两样露给成员，
    他只会去算「为什么用了这么多积分才扣这么点钱」，而那是两套账。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))
        self.mid = self.store.create_member(name="me")
        self.aid = self.store.add_account(
            uid="u1", region="global", credential="pt-x",
            provider="qoder", name="acc")
        for i, (model, cost, cred, tok) in enumerate([
                ("dfmodel", 0.5, 3.0, 1000),
                ("dfmodel", 0.25, 1.5, 500),
                ("gfmodel", 0.1, 0.6, 200)]):
            self.store.record_usage(key_id=None, member_id=self.mid,
                                    account_id=self.aid, model=model,
                                    prompt_tokens=tok, completion_tokens=0,
                                    cost=cost, credits=cred, status=200)

    def stats(self, names=None):
        """用自定的名字表聚合，避免测试依赖上游目录。

        线上目录认得 gfmodel（那是 Qoder 的 GLM key），本地空目录认不得。
        让测试自己给名字表，两台机器上的结论就一致了。
        """
        return server.Handler._member_self_stats(self.store, self.mid, names)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_stats_aggregate_by_model_only(self):
        st = self.stats({"dfmodel": "DeepSeek-V4.1-Flash",
                         "deepseek-v4.1-flash": "DeepSeek-V4.1-Flash"})
        # 上游 key 在这里已经翻成对外显示名了：dfmodel 与
        # deepseek-v4.1-flash 是同一个模型，成员看到的必须是名字而不是缩写
        rows = {m["model"]: m for m in st["models"]}
        self.assertEqual(sorted(rows),
                         ["DeepSeek-V4.1-Flash", "gfmodel"])
        deep = rows["DeepSeek-V4.1-Flash"]
        self.assertEqual(deep["requests"], 2)
        self.assertEqual(deep["tokens"], 1500)
        self.assertEqual(deep["cost"], 0.75)
        self.assertEqual(st["totals"]["requests"], 3)
        self.assertEqual(st["totals"]["tokens"], 1700)
        self.assertEqual(st["totals"]["success_rate"], 100.0)

    def test_upstream_keys_are_translated_to_display_names(self):
        """两家上游的同款模型在成员表里必须合成一行、显示对外名字。"""
        self.store.record_usage(key_id=None, member_id=self.mid,
                                account_id=self.aid,
                                model="deepseek-v4.1-flash",
                                prompt_tokens=100, completion_tokens=0,
                                cost=0.1, status=200)
        st = server.Handler._member_self_stats(self.store, self.mid)
        rows = {m["model"]: m for m in st["models"]}
        self.assertNotIn("dfmodel", rows)
        self.assertEqual(rows["DeepSeek-V4.1-Flash"]["requests"], 3)

    def test_no_provider_or_credit_leaks(self):
        st = server.Handler._member_self_stats(self.store, self.mid)
        blob = str(st)
        for word in ("credits", "provider", "account", "qoder", "dfmodel"):
            self.assertNotIn(word, blob, "成员视图不该出现 %s" % word)

    def test_unknown_upstream_key_passes_through(self):
        """目录里认不出的 key 原样返回，不猜也不报错。

        gfmodel 这类机器名在接入前就已经进了历史 usage 行，名称表里也
        没有它。猜错会把两个模型混成一行，保留原写法至少不丢信息。
        """
        self.assertIn("gfmodel", str(self.stats({})))

    def test_dashboard_hides_routing_fields(self):
        """控制台接口不能带出 account_id / credits / ip 与厂商字段。"""
        mid = self.store.create_member(name="d", model_allowlist="")
        kid, _raw = self.store.create_key(member_id=mid)
        key = self.store.get_key(kid)
        with mock.patch.object(server, "get_store", lambda: self.store):
            dash = server.Handler._member_dashboard(
                server.Handler.__new__(server.Handler),
                {"member": self.store.get_member(mid), "key": key})
        blob = str(dash)
        for word in ("account_id", "credits", "ip'", "provider", "spec",
                     "qoder", "workbuddy", "dfmodel"):
            self.assertNotIn(word, blob, "控制台不该出现 %s" % word)
        self.assertEqual(dash["member"]["model_allowlist"], "不限")

    def test_other_members_rows_are_excluded(self):
        other = self.store.create_member(name="other")
        self.store.record_usage(key_id=None, member_id=other, account_id=self.aid,
                                model="dfmodel", prompt_tokens=999,
                                completion_tokens=0, cost=99.0, credits=99.0)
        st = server.Handler._member_self_stats(self.store, self.mid)
        self.assertEqual(st["totals"]["requests"], 3)
        self.assertAlmostEqual(st["totals"]["cost"], 0.85)

    def test_self_usage_fields_hide_account(self):
        h = server.Handler.__new__(server.Handler)
        with mock.patch.object(server, "get_store", lambda: self.store):
            out = h._self_usage({"member": self.store.get_member(self.mid)},
                                {"days": ["0"], "page": ["1"], "size": ["2"]})
        self.assertEqual(out["total"], 3)
        self.assertEqual(len(out["rows"]), 2)
        for row in out["rows"]:
            self.assertNotIn("account_id", row)
            self.assertNotIn("credits", row)
            self.assertIn("total_tokens", row)
        # 调用明细里的模型名也是对外写法
        self.assertIn("DeepSeek-V4.1-Flash",
                      [r["model"] for r in out["rows"]])

    def test_member_pricing_is_one_row_per_model(self):
        """成员单价表按模型一行，不带厂商、上游 key 与白名单 spec。"""
        with mock.patch.object(server, "all_models", lambda: {
            "deepseek-v4.1-flash": {
                "id": "deepseek-v4.1-flash", "name": "DeepSeek-V4.1-Flash",
                "provider": "qoder",
                "routes": [
                    {"provider": "qoder", "region": "global",
                     "model": "dfmodel"},
                    {"provider": "workbuddy", "region": "cn",
                     "model": "deepseek-v4.1-flash"}]},
        }):
            h = server.Handler.__new__(server.Handler)
            with mock.patch.object(server, "get_store", lambda: self.store):
                view = h._pricing_view({"model_allowlist": ""}, {"model_allowlist": ""})
        self.assertEqual(len(view["rows"]), 1)
        row = view["rows"][0]
        self.assertEqual(row["model_name"], "DeepSeek-V4.1-Flash")
        for hidden in ("provider", "provider_label", "provider_color",
                       "upstream_key", "spec"):
            self.assertNotIn(hidden, row)
        # 运营口径的字段也不该跟着成员视图出去
        for hidden in ("overrides", "builtin", "retired", "default_price"):
            self.assertNotIn(hidden, view)

    def test_self_usage_page_out_of_range_is_clamped(self):
        h = server.Handler.__new__(server.Handler)
        with mock.patch.object(server, "get_store", lambda: self.store):
            out = h._self_usage({"member": self.store.get_member(self.mid)},
                                {"days": ["0"], "page": ["999"], "size": ["2"]})
        # 越界页夹到最后一页，不会返回空表让成员以为没有记录
        self.assertEqual(out["page"], out["pages"])
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["total"], 3)


class CacheRateTest(unittest.TestCase):
    """缓存命中率：分母是「输入 + 缓存读」，没有输入时给 None 而不是 0。"""

    def test_no_input_returns_none(self):
        # 没有输入时不能回 0%——那会被读成「一次都没命中」
        self.assertIsNone(server._cache_rate(0, 0))

    def test_zero_cache_with_input_is_zero(self):
        self.assertEqual(server._cache_rate(0, 1000), 0.0)

    def test_half_hit(self):
        self.assertEqual(server._cache_rate(500, 500), 50.0)

    def test_output_not_in_denominator(self):
        # 调用方只传输入侧的两个数，输出不进分母
        self.assertEqual(server._cache_rate(100, 300), 25.0)


class MemberDailyRangeTest(unittest.TestCase):
    """成员趋势图的范围：7/30 天固定天数，0 表示全部历史。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.dir, "t.db"))
        self.mid = self.store.create_member(name="r")
        now = time.time()
        for i in range(10):
            self.store.record_usage(key_id=None, member_id=self.mid, account_id=None,
                                    model="m", prompt_tokens=100 + i,
                                    completion_tokens=10, cost=0.01,
                                    cache_read_tokens=50, status=200)
        # 把记录摊到最近 10 天
        with self.store._conn() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM usage WHERE member_id=? ORDER BY id", (self.mid,))]
            for i, rid in enumerate(ids):
                c.execute("UPDATE usage SET created_at=? WHERE id=?",
                          (now - i * 86400, rid))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_fixed_windows(self):
        self.assertEqual(len(self.store.daily_series_for_member(self.mid, 7)), 7)
        self.assertEqual(len(self.store.daily_series_for_member(self.mid, 30)), 30)

    def test_all_covers_from_first_record(self):
        series = self.store.daily_series_for_member(self.mid, 0)
        # 最早一条在 9 天前，所以「全部」是 10 天，不是 14 也不是 30
        self.assertEqual(len(series), 10)
        self.assertGreater(sum(d["tokens"] for d in series), 0)

    def test_all_on_empty_member(self):
        other = self.store.create_member(name="empty")
        self.assertEqual(self.store.daily_series_for_member(other, 0), [])

    def test_prompt_completion_split(self):
        # 趋势图的两种口径都从这三个字段来，入 + 出 必须等于总量
        series = self.store.daily_series_for_member(self.mid, 7)
        hit = [d for d in series if d["tokens"]]
        self.assertTrue(hit)
        self.assertEqual(hit[0]["prompt_tokens"] + hit[0]["completion_tokens"],
                         hit[0]["tokens"])
