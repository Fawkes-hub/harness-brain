"""v2.1 serve 测试:真起一个 HTTP 服务(端口 0),走真实请求。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brainlib.model import Atom, content_hash, new_id, now_iso
from brainlib.server import Brain, make_handler
from brainlib.store import Store


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="brain-srv-"))
        st = Store(cls.tmp)
        a = Atom(id=new_id(), kind="fact", body="代理地址 198.51.100.7",
                 occurred_at=now_iso(), received_at=now_iso(),
                 author={"host": "server", "harness": "cli", "role": "assistant"},
                 tags=["network"], ttl="2020-01-01")
        a.supersedes = "m_zzzzzzzzzz"          # 制造 dangling,验证 conflicts
        a.hash = content_hash(a.kind, a.tags, a.ttl, a.supersedes, a.body)
        st.put_atom(a)
        st.rebuild()
        cls.atom_id = a.id

        cls.brain = Brain(cls.tmp)
        cls.tok_rw, _ = cls.brain.tokens.issue("mac", "pi", "engineer", ["read", "write"], 30)
        cls.tok_r, _ = cls.brain.tokens.issue("laptop", "cli", "engineer", ["read"], 30)
        cls.tok_admin, _ = cls.brain.tokens.issue("server", "cli", "assistant", ["admin"], 30)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.brain))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        if data:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw


class TestAuth(ServerCase):
    def test_health_no_auth(self):
        code, data = self.req("GET", "/v1/health")
        self.assertEqual(code, 200)
        self.assertTrue(data["ok"])

    def test_brief_requires_token(self):
        self.assertEqual(self.req("GET", "/v1/brief")[0], 401)

    def test_client_bundle_bootstrap_without_auth(self):
        code, data = self.req("GET", "/v1/client")
        self.assertIn(code, (200, 404))   # 测试环境可能没构建 bundle

    def test_bad_token(self):
        self.assertEqual(self.req("GET", "/v1/whoami", token="bk_nope")[0], 401)

    def test_whoami_from_token(self):
        code, data = self.req("GET", "/v1/whoami", token=self.tok_rw)
        self.assertEqual(code, 200)
        self.assertEqual((data["host"], data["harness"], data["role"], data["role_source"]),
                         ("mac", "pi", "engineer", "token"))

    def test_read_only_token_cannot_write(self):
        code, data = self.req("POST", "/v1/remember",
                              {"id": new_id(), "kind": "fact", "body": "x"}, token=self.tok_r)
        self.assertEqual(code, 403)
        self.assertEqual(data["error"]["code"], "forbidden_scope")

    def test_reindex_needs_admin(self):
        self.assertEqual(self.req("POST", "/v1/reindex", {}, token=self.tok_rw)[0], 403)
        self.assertEqual(self.req("POST", "/v1/reindex", {}, token=self.tok_admin)[0], 200)


class TestRemember(ServerCase):
    def test_create_then_replay_then_conflict(self):
        ident = new_id()
        body = {"id": ident, "kind": "fact", "body": "测试事实", "tags": ["t"]}
        self.assertEqual(self.req("POST", "/v1/remember", body, self.tok_rw)[0], 201)
        self.assertEqual(self.req("POST", "/v1/remember", body, self.tok_rw)[0], 200)
        body2 = dict(body, body="换了内容")
        code, data = self.req("POST", "/v1/remember", body2, self.tok_rw)
        self.assertEqual(code, 409)
        self.assertEqual(data["error"]["code"], "conflict_id")

    def test_bad_id_rejected(self):
        code, data = self.req("POST", "/v1/remember", {"id": "m_short", "kind": "fact", "body": "x"},
                              self.tok_rw)
        self.assertEqual(code, 400)
        self.assertEqual(data["error"]["code"], "bad_request")

    def test_author_is_from_token_not_body(self):
        ident = new_id()
        self.req("POST", "/v1/remember",
                 {"id": ident, "kind": "fact", "body": "冒充", "author": {"host": "fake"}},
                 self.tok_rw)
        a = self.brain.store.load_atoms()[ident]
        self.assertEqual(a.author["host"], "mac")


class TestRead(ServerCase):
    def test_brief_and_budget(self):
        code, text = self.req("GET", "/v1/brief?budget=6000", token=self.tok_rw)
        self.assertEqual(code, 200)
        self.assertIn("共享记忆库", text)
        self.assertEqual(self.req("GET", "/v1/brief?budget=1", token=self.tok_rw)[0], 413)

    def test_search_then_forget_then_restore(self):
        ident = new_id()
        self.req("POST", "/v1/remember", {"id": ident, "kind": "fact", "body": "独有词zebra"}, self.tok_rw)
        code, data = self.req("GET", "/v1/search?q=zebra", token=self.tok_rw)
        self.assertEqual(len(data["hits"]), 1)
        self.assertEqual(self.req("POST", "/v1/forget", {"id": ident, "status": "deprecated"}, self.tok_rw)[0], 201)
        self.assertEqual(len(self.req("GET", "/v1/search?q=zebra", token=self.tok_rw)[1]["hits"]), 0)
        self.assertEqual(self.req("POST", "/v1/forget", {"id": ident, "status": "active"}, self.tok_rw)[0], 201)
        self.assertEqual(len(self.req("GET", "/v1/search?q=zebra", token=self.tok_rw)[1]["hits"]), 1)

    def test_stale_and_conflicts(self):
        code, data = self.req("GET", "/v1/stale?within=30d", token=self.tok_rw)
        self.assertTrue(any(x["id"] == self.atom_id for x in data["items"]))
        code, data = self.req("GET", "/v1/conflicts", token=self.tok_rw)
        self.assertTrue(any("取代目标缺失" in x["problem"] for x in data["items"]))


class TestForgetErrors(ServerCase):
    def test_unknown_id_404(self):
        self.assertEqual(self.req("POST", "/v1/forget", {"id": "m_zzzzzzzzzz"}, self.tok_rw)[0], 404)

    def test_retracted_requires_replaced_by(self):
        ident = new_id()
        self.req("POST", "/v1/remember", {"id": ident, "kind": "fact", "body": "要撤回"}, self.tok_rw)
        self.assertEqual(self.req("POST", "/v1/forget", {"id": ident, "status": "retracted"}, self.tok_rw)[0], 400)
        self.assertEqual(self.req("POST", "/v1/forget",
                                  {"id": ident, "status": "retracted", "replaced_by": "m_zzzzzzzzzz"},
                                  self.tok_rw)[0], 400)


class TestUI(ServerCase):
    def raw(self, path, data=None, cookie=None, method=None, headers=None):
        import urllib.parse
        url = f"http://127.0.0.1:{self.port}{path}"
        body = urllib.parse.urlencode(data, doseq=True).encode() if data else None
        r = urllib.request.Request(url, data=body, method=method or ("POST" if data else "GET"))
        if cookie:
            r.add_header("Cookie", f"brain_token={cookie}")
        if body:
            r.add_header("Content-Type", "application/x-www-form-urlencoded")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(r, timeout=10) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def test_root_redirects_to_login(self):
        import urllib.request as ur
        class NoRedirect(ur.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = ur.build_opener(NoRedirect)
        try:
            op.open(f"http://127.0.0.1:{self.port}/")
            self.fail("应当 303")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 303)
            self.assertIn("/login", e.headers["Location"])

    def test_login_page_and_bad_token(self):
        code, html, _ = self.raw("/login")
        self.assertEqual(code, 200)
        self.assertIn("用令牌登录", html)
        code, _, _ = self.raw("/login", {"token": "bk_bogus"})
        self.assertEqual(code, 401)

    def test_login_sets_cookie_then_dashboard(self):
        code, _, hdrs = self.raw("/login", {"token": self.tok_rw})
        self.assertEqual(code, 303)
        self.assertIn("HttpOnly", hdrs.get("Set-Cookie", ""))
        code, html, _ = self.raw("/", cookie=self.tok_rw)
        self.assertEqual(code, 200)
        self.assertIn("共享记忆库", html)
        self.assertIn("最近记录", html)

    def test_atom_and_lists(self):
        for path in (f"/atom/{self.atom_id}", "/rules", "/stale", "/conflicts", "/search?q=x"):
            code, html, _ = self.raw(path, cookie=self.tok_rw)
            self.assertEqual(code, 200, path)
            self.assertIn("<title>", html)

    def test_compose_creates_atom(self):
        code, _, hdrs = self.raw("/compose", {"body": "界面写入测试 zzz", "tags": "ui,test",
                                              "kind": "fact"}, cookie=self.tok_rw)
        self.assertEqual(code, 303)
        self.assertTrue(hdrs["Location"].startswith("/atom/m_"))
        code, html, _ = self.raw("/search?q=zzz", cookie=self.tok_rw)
        self.assertIn("界面写入测试", html)

    def test_management_pages_render(self):
        for path in ("/memories", "/memories?kind=rule&sort=kind", "/topics", "/new",
                     "/review", "/conflicts", "/system", "/search?q=%E4%BB%A3%E7%90%86"):
            code, html, _ = self.raw(path, cookie=self.tok_rw)
            self.assertEqual(code, 200, path)
            self.assertIn("共享记忆库", html)
        code, html, _ = self.raw("/memories", cookie=self.tok_rw)
        self.assertIn("kindfilter", html)
        code, html, _ = self.raw("/memories?kind=fact", cookie=self.tok_rw)
        self.assertIn("kchip active", html)

    def test_login_next_roundtrip(self):
        code, _, hdrs = self.raw("/login", {"token": self.tok_rw, "next": "/memories"})
        self.assertEqual(code, 303)
        self.assertEqual(hdrs["Location"], "/memories")
        html = self.raw("/login?next=%2Fmemories")[1]
        self.assertIn('name="next" value="/memories"', html)

    def test_read_token_has_no_write_actions(self):
        code, html, _ = self.raw(f"/atom/{self.atom_id}", cookie=self.tok_r)
        self.assertEqual(code, 200)
        self.assertNotIn('href="/new?supersedes', html)
        self.assertNotIn("标记为过期", html)

    def test_read_token_cannot_write(self):
        code, _, hdrs = self.raw("/compose", {"body": "只读不该写入", "kind": "fact"},
                                 cookie=self.tok_r)
        self.assertEqual(code, 403)
        self.assertEqual(self.raw("/new", cookie=self.tok_r)[0], 403)

    def test_tokens_admin_only(self):
        self.assertEqual(self.raw("/tokens", cookie=self.tok_r)[0], 403)
        self.assertEqual(self.raw("/activity", cookie=self.tok_r)[0], 403)
        self.assertEqual(self.raw("/tokens", cookie=self.tok_admin)[0], 200)
        self.assertEqual(self.raw("/activity", cookie=self.tok_admin)[0], 200)

    def test_token_issue_shows_once_then_revoke(self):
        import re
        code, html, _ = self.raw("/token-issue",
                                 {"host": "tmp", "harness": "ui", "role": "tester", "ttl": "1",
                                  "scopes": ["read", "write"]}, cookie=self.tok_admin)
        self.assertEqual(code, 200)
        match = re.search(r"bk_[0-9a-z]{24}", html)
        self.assertIsNotNone(match)
        token = match.group(0)
        self.assertNotIn(token, self.raw("/tokens", cookie=self.tok_admin)[1])
        self.assertEqual(self.req("GET", "/v1/whoami", token=token)[0], 200)
        rec = [t for t in self.brain.tokens.list() if t["author"]["host"] == "tmp"][-1]
        code, _, _ = self.raw("/token-revoke", {"id": rec["id"]}, cookie=self.tok_admin)
        self.assertEqual(code, 303)
        self.assertEqual(self.req("GET", "/v1/whoami", token=token)[0], 401)

    def test_forget_restore_ui(self):
        ident = new_id()
        self.req("POST", "/v1/remember", {"id": ident, "kind": "fact", "body": "待遗忘 ui"}, self.tok_rw)
        code, _, hdrs = self.raw("/forget", {"id": ident, "status": "deprecated",
                                             "next": f"/atom/{ident}"}, cookie=self.tok_rw)
        self.assertEqual(code, 303)
        self.assertIn("msg=deprecated", hdrs["Location"])
        self.assertIn("已过期", self.raw(f"/atom/{ident}", cookie=self.tok_rw)[1])
        code, _, hdrs = self.raw("/forget", {"id": ident, "status": "active",
                                             "next": f"/atom/{ident}"}, cookie=self.tok_rw)
        self.assertEqual(code, 303)
        self.assertIn("msg=restored", hdrs["Location"])

    def test_xss_is_escaped(self):
        ident = new_id()
        self.req("POST", "/v1/remember",
                 {"id": ident, "kind": "fact", "body": "<script>alert(1)</script>"}, self.tok_rw)
        html = self.raw(f"/atom/{ident}", cookie=self.tok_rw)[1]
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_search_special_chars_and_404(self):
        self.assertEqual(self.raw("/search?q=%28", cookie=self.tok_rw)[0], 200)
        self.assertEqual(self.raw("/atom/m_zzzzzzzzzz", cookie=self.tok_rw)[0], 404)

    def test_bad_origin_rejected(self):
        code, _, _ = self.raw("/compose", {"body": "csrf", "kind": "fact"},
                              cookie=self.tok_rw, headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)

    def test_audit_written(self):
        self.raw("/compose", {"body": "审计测试条目", "kind": "fact"}, cookie=self.tok_rw)
        audit = (self.tmp / "state" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "remember"', audit)

    def test_api_rejects_bad_numbers(self):
        self.assertEqual(self.req("GET", "/v1/search?q=x&limit=abc", token=self.tok_rw)[0], 400)
        self.assertEqual(self.req("GET", "/v1/stale?within=xyz", token=self.tok_rw)[0], 400)
        self.assertEqual(self.req("GET", "/v1/recall?since=abc", token=self.tok_rw)[0], 400)


if __name__ == "__main__":
    unittest.main()
