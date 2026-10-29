"""备份子系统的单元测试。

一套测试覆盖:配置与脱敏、快照内容、本地目的地、WebDAV 目的地(用本地 stub 服务器)、
保留策略、hash chain、调度时间计算、调度线程到点触发。
**不依赖真实 age / 真实网络**:加密用桩脚本,WebDAV 用进程内 stub。
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from brainlib import backup as B


# ----------------------------------------------------------------- 夹具

def write_stub_age(dirpath: Path) -> tuple[Path, Path]:
    """假 age:把输入原样拷成输出(测试里能直接当 tar.gz 解开看内容)。"""
    age = dirpath / "age"
    age.write_text("""#!/bin/sh
out=""; inp=""
while [ $# -gt 0 ]; do
  case "$1" in
    -r) shift 2 ;;
    -o) out=$2; shift 2 ;;
    *) inp=$1; shift ;;
  esac
done
cp "$inp" "$out"
""")
    age.chmod(0o755)
    keygen = dirpath / "age-keygen"
    keygen.write_text("""#!/bin/sh
out=""; while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2;; *) shift;; esac; done
printf '# created: test\\n# public key: age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\\nAGE-SECRET-KEY-TEST\\n' > "$out"
""")
    keygen.chmod(0o755)
    return age, keygen


def make_library(root: Path, atoms: int = 2) -> None:
    (root / "atoms" / "202610").mkdir(parents=True, exist_ok=True)
    (root / "views" / "topics").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "profiles").mkdir(parents=True, exist_ok=True)
    for i in range(1, atoms + 1):
        (root / "atoms" / "202610" / f"m_000000000{i}.md").write_text(
            f"---\nid: m_000000000{i}\nkind: fact\n---\nbody {i}\n", encoding="utf-8")
    (root / "tombstones.jsonl").write_text('{"seq":1,"id":"m_9","status":"expired"}\n', encoding="utf-8")
    (root / "views" / "INDEX.md").write_text("派生内容,不该进快照\n", encoding="utf-8")
    (root / "state" / "age.key").write_text("# public key: age1x\nSECRET\n", encoding="utf-8")
    (root / "state" / "audit.jsonl").write_text('{"at":"x"}\n', encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-test-"))
        self.root = self.tmp / "lib"
        make_library(self.root)
        self.age, self.keygen = write_stub_age(self.tmp)
        self.bk = B.Backup(self.root)
        cfg = self.bk.load()
        cfg["cipher"]["cmd"] = str(self.age)
        cfg["cipher"]["recipient"] = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
        cfg["destinations"] = [{"id": "d1", "type": "local", "enabled": True,
                                "label": "本地", "path": str(self.tmp / "out")}]
        self.bk.save(cfg)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# ----------------------------------------------------------------- 配置

class TestConfig(Base):
    def test_defaults_and_roundtrip(self):
        cfg = self.bk.load()
        self.assertEqual(cfg["schedule"]["mode"], "off")
        self.assertEqual(cfg["retention"]["daily"], 30)
        cfg["schedule"]["mode"] = "daily"
        cfg["destinations"][0]["password"] = "hunter2"
        self.bk.save(cfg)
        again = self.bk.load()
        self.assertEqual(again["schedule"]["mode"], "daily")
        self.assertEqual(again["destinations"][0]["password"], "hunter2")

    def test_public_never_leaks_password(self):
        cfg = self.bk.load()
        cfg["destinations"].append({"id": "d2", "type": "webdav", "url": "https://dav.example/x/",
                                    "username": "u", "password": "s3cret"})
        self.bk.save(cfg)
        pub = self.bk.public()
        blob = json.dumps(pub, ensure_ascii=False)
        self.assertNotIn("s3cret", blob)
        d2 = [d for d in pub["destinations"] if d["id"] == "d2"][0]
        self.assertTrue(d2["password_set"])
        self.assertEqual(d2["password"], "")

    def test_recipient_validation(self):
        with self.assertRaises(B.DestinationError):
            self.bk.import_recipient("not-a-key")
        self.assertEqual(self.bk.import_recipient("age1" + "a" * 40), "age1" + "a" * 40)

    def test_cfg_file_is_private(self):
        self.bk.save(self.bk.load())
        self.assertEqual(oct(self.bk.cfg_path.stat().st_mode & 0o777), oct(0o600))


# ----------------------------------------------------------------- 快照

class TestSnapshot(Base):
    def test_snapshot_excludes_views_and_private_key(self):
        plain = self.tmp / "s.tar.gz"
        stats = self.bk.make_snapshot(plain)
        self.assertEqual(stats["atoms"], 2)
        self.assertEqual(stats["tombstones"], 1)
        names = tarfile.open(plain, "r:gz").getnames()
        self.assertFalse([n for n in names if n.startswith("views/")])
        self.assertNotIn("state/age.key", names)
        self.assertIn("state/audit.jsonl", names)
        self.assertIn("atoms/202610/m_0000000001.md", names)

    def test_snapshot_refuses_plaintext_when_age_missing(self):
        cfg = self.bk.load()
        cfg["cipher"]["cmd"] = str(self.tmp / "no-such-age")
        self.bk.save(cfg)
        plain = self.tmp / "s.tar.gz"
        self.bk.make_snapshot(plain)
        with self.assertRaises(B.DestinationError):
            self.bk.encrypt(plain, self.tmp / "s.age")
        self.assertFalse((self.tmp / "s.age").exists())


# ----------------------------------------------------------------- 本地目的地

class TestLocalDest(Base):
    def test_put_verify_listing_delete(self):
        cfg = {"id": "d1", "type": "local", "path": str(self.tmp / "out")}
        d = B.make_destination(cfg)
        d.put_verified("a.bin", b"hello")
        self.assertEqual(d.get("a.bin"), b"hello")
        self.assertIn("a.bin", d.listing())
        d.delete("a.bin")
        self.assertNotIn("a.bin", d.listing())

    def test_readback_mismatch_is_detected(self):
        cfg = {"id": "d1", "type": "local", "path": str(self.tmp / "out")}
        d = B.make_destination(cfg)

        def lying_get(name: str) -> bytes:
            return b"truncated"

        d.get = lying_get        # type: ignore[assignment]
        with self.assertRaises(B.DestinationError):
            d.put_verified("a.bin", b"hello")


# ----------------------------------------------------------------- WebDAV stub

def make_dav_stub() -> tuple[ThreadingHTTPServer, dict, threading.Thread]:
    store: dict[str, bytes] = {}
    colls: set[str] = set("/dav/")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):    # noqa: D401
            pass

        def _p(self) -> str:
            return urllib.parse.unquote(urllib.parse.urlparse(self.path).path)

        def _auth_ok(self) -> bool:
            want = "Basic " + __import__("base64").b64encode(b"u:p").decode()
            return self.headers.get("Authorization") == want

        def do_MKCOL(self):
            if not self._auth_ok():
                return self.send_error(401)
            colls.add(self._p().rstrip("/") + "/")
            self.send_response(201)
            self.end_headers()

        def do_PUT(self):
            if not self._auth_ok():
                return self.send_error(401)
            n = int(self.headers.get("Content-Length") or 0)
            store[self._p()] = self.rfile.read(n)
            self.send_response(201)
            self.end_headers()

        def do_GET(self):
            if not self._auth_ok():
                return self.send_error(401)
            body = store.get(self._p())
            if body is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            if not self._auth_ok():
                return self.send_error(401)
            if store.pop(self._p(), None) is None:
                return self.send_error(404)
            self.send_response(204)
            self.end_headers()

        def do_PROPFIND(self):
            if not self._auth_ok():
                return self.send_error(401)
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            base = self._p().rstrip("/") + "/"
            items = [h for h in store if h.startswith(base)]
            xml = ['<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">']
            xml.append(f"<d:response><d:href>{base}</d:href></d:response>")
            for h in items:
                xml.append(f"<d:response><d:href>{urllib.parse.quote(h)}</d:href></d:response>")
            xml.append("</d:multistatus>")
            raw = "".join(xml).encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, store, t


class TestWebDav(unittest.TestCase):
    def setUp(self):
        self.httpd, self.store, self.thread = make_dav_stub()
        port = self.httpd.server_address[1]
        self.dest = B.WebDavDest({"id": "w1", "type": "webdav",
                                  "url": f"http://127.0.0.1:{port}/dav/sub/",
                                  "username": "u", "password": "p"})

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_put_get_listing_delete(self):
        self.assertEqual(self.dest.test(), "连通")
        self.dest.put_verified("brain-000001.age", b"cipher-1")
        self.assertEqual(self.dest.get("brain-000001.age"), b"cipher-1")
        self.assertIn("brain-000001.age", self.dest.listing())
        self.dest.delete("brain-000001.age")
        self.assertNotIn("brain-000001.age", self.dest.listing())

    def test_wrong_password_surfaces_error(self):
        self.dest.cfg["password"] = "wrong"
        with self.assertRaises(B.DestinationError):
            self.dest.put("brain-000002.age", b"x")


# ----------------------------------------------------------------- 保留 & 执行

class TestRun(Base):
    def test_run_chain_and_retention(self):
        r1 = self.bk.run("manual")
        self.assertTrue(r1["ok"], r1)
        self.assertEqual((r1["seq"], r1["atoms"], r1["tombstones"]), (1, 2, 1))
        r2 = self.bk.run("manual")
        self.assertEqual(r2["prev_cipher_sha256"], r1["cipher_sha256"])

        cfg = self.bk.load()
        cfg["retention"]["daily"] = 2
        self.bk.save(cfg)
        self.bk.run("manual")
        out = self.tmp / "out"
        self.assertEqual(sorted(p.name for p in out.glob("brain-*.age")),
                         ["brain-000002.age", "brain-000003.age"])

    def test_manifest_sidecar_written(self):
        self.bk.run("manual")
        man = json.loads((self.tmp / "out" / "brain-000001.json").read_text())
        self.assertEqual(man["seq"], 1)
        self.assertEqual(man["atoms"], 2)
        self.assertEqual(len(man["cipher_sha256"]), 64)

    def test_failed_destination_does_not_stop_others(self):
        cfg = self.bk.load()
        cfg["destinations"].append({"id": "d2", "type": "local", "enabled": True,
                                    "label": "坏", "path": "/proc/none/x"})
        self.bk.save(cfg)
        r = self.bk.run("manual")
        self.assertFalse(r["ok"])
        self.assertTrue(r["destinations"]["d1"]["ok"])
        self.assertFalse(r["destinations"]["d2"]["ok"])
        self.assertTrue(r["destinations"]["d2"]["error"])

    def test_disabled_or_empty_config_reports_clearly(self):
        cfg = self.bk.load()
        cfg["enabled"] = False
        self.bk.save(cfg)
        self.assertEqual(self.bk.run()["error"], "备份已停用")
        cfg["enabled"] = True
        cfg["destinations"] = []
        self.bk.save(cfg)
        self.assertEqual(self.bk.run()["error"], "没有启用的目的地")


# ----------------------------------------------------------------- 调度

class TestSchedule(unittest.TestCase):
    now = dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc)   # 周四

    def n(self, sched, **kw):
        return B.compute_next_run(sched, self.now, **kw)

    def test_off(self):
        self.assertEqual(self.n({"mode": "off"}), "")

    def test_daily_today_or_tomorrow(self):
        self.assertTrue(self.n({"mode": "daily", "at": "23:00", "jitter_min": 0})
                        .startswith("2026-10-29T23:00"))
        self.assertTrue(self.n({"mode": "daily", "at": "03:40", "jitter_min": 0})
                        .startswith("2026-10-30T03:40"))

    def test_every_hours(self):
        self.assertTrue(self.n({"mode": "every", "every_hours": 6})
                        .startswith("2026-10-29T18:00"))

    def test_weekly(self):
        # 2026-10-29 周四 → 下一个周日 11-01
        self.assertTrue(self.n({"mode": "weekly", "weekday": 6, "at": "04:00", "jitter_min": 0})
                        .startswith("2026-11-01T04:00"))
        # 周一当天已过 04:00 → 下周一 11-02
        self.assertTrue(self.n({"mode": "weekly", "weekday": 0, "at": "04:00", "jitter_min": 0})
                        .startswith("2026-11-02T04:00"))

    def test_jitter_is_deterministic_and_bounded(self):
        sched = {"mode": "daily", "at": "03:40", "jitter_min": 45}
        a = self.n(sched, seed="lib-a")
        b = self.n(sched, seed="lib-a")
        c = self.n(sched, seed="lib-b")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        t = dt.datetime.fromisoformat(a)
        self.assertGreaterEqual(t, dt.datetime(2026, 10, 30, 3, 40, tzinfo=dt.timezone.utc))
        self.assertLessEqual(t, dt.datetime(2026, 10, 30, 4, 25, tzinfo=dt.timezone.utc))


class TestScheduler(Base):
    def test_tick_runs_when_due_and_recomputes_next(self):
        cfg = self.bk.load()
        cfg["schedule"]["mode"] = "daily"
        cfg["schedule"]["at"] = "03:40"
        cfg["schedule"]["jitter_min"] = 0
        self.bk.save(cfg)
        st = self.bk.state()
        st["next_run"] = "2020-01-01T00:00:00+00:00"      # 早就过期 → 该补跑
        self.bk.save_state(st)

        sched = B.Scheduler(self.bk, tick=999)
        report = sched.tick_once()
        self.assertIsNotNone(report)
        self.assertTrue(report["ok"], report)
        st2 = self.bk.state()
        self.assertEqual(st2["last_seq"], 1)
        self.assertTrue(st2["next_run"])

    def test_not_due_when_disabled(self):
        cfg = self.bk.load()
        cfg["enabled"] = False
        self.bk.save(cfg)
        sched = B.Scheduler(self.bk, tick=999)
        self.assertIsNone(sched.tick_once())


if __name__ == "__main__":
    unittest.main()


class TestTokenStoreRobustness(unittest.TestCase):
    """残缺的 tokens.json 不该让 serve 起不来(实测踩过:手写的 {"v":1} 直接 KeyError)。"""

    def test_missing_tokens_key_is_tolerated(self):
        from brainlib import auth
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tokens.json"
            p.write_text('{"v": 1}', encoding="utf-8")
            store = auth.TokenStore(p)
            store.init()
            token, rec = store.issue("t", "t", "admin", ["admin"], 1)
            self.assertTrue(token.startswith("bk_"))
            self.assertIsNotNone(store.verify(token)[0])

    def test_broken_json_is_tolerated(self):
        from brainlib import auth
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tokens.json"
            p.write_text('{not json', encoding="utf-8")
            store = auth.TokenStore(p)
            store.init()
            token, _ = store.issue("t", "t", "admin", ["admin"], 1)
            self.assertIsNotNone(store.verify(token)[0])
