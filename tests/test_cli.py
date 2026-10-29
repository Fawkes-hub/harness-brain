"""CLI 级回归测试 —— 跑真的子进程,专门守住「全新空库」这条最容易断的路径。

这些场景都是实测踩过的坑,单测覆盖不到:

1. 只要设了 `BRAIN_ROOT`,旧代码就把数据根塞进 `sys.path`,于是 import 不到 brainlib,
   容器里(代码 /app、数据 /data)连 `--version` 都跑不起来。
2. 空目录下 `brain serve` 会掉进 v1 代码路径,以 `AttributeError: 'Namespace' object has no
   attribute 'func'` 退出 —— 新手第一步必撞。
3. `brain init` 之后 `verify` 必须五项全过(否则新库一出生就是「脏」的)。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "bin" / "brain"


def run(*args: str, root: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    if root is not None:
        env["BRAIN_ROOT"] = root
    env["BRAIN_URL"] = "http://127.0.0.1:1"   # 确保不会误连真服务
    env.pop("BRAIN_TOKEN", None)
    return subprocess.run([sys.executable, str(CLI), *args],
                          cwd=str(REPO), capture_output=True, text=True, env=env, timeout=120)


class EmptyRootTest(unittest.TestCase):
    def test_version_works_when_brain_root_is_set(self):
        """守住 sys.path 那个坑:数据根与代码根分离时,CLI 仍要能启动。"""
        with tempfile.TemporaryDirectory() as tmp:
            r = run("--version", root=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("brain ", r.stdout)

    def test_init_creates_layout_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run("init", root=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            root = Path(tmp)
            for d in ("atoms", "views", "state", "profiles"):
                self.assertTrue((root / d).is_dir(), f"缺目录 {d}")
            self.assertTrue((root / "tombstones.jsonl").is_file())
            self.assertTrue((root / ".gitignore").is_file())

            v = run("verify", root=tmp)
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
            self.assertIn("五项全过", v.stdout)

    def test_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run("init", root=tmp).returncode, 0)
            second = run("init", root=tmp)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("已经是初始化状态", second.stdout)

    def test_missing_root_without_init_hints_at_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope")
            r = run("verify", root=missing)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("init", (r.stdout + r.stderr))

    def test_cli_never_touches_the_v1_dispatch(self):
        """空库跑 v2 子命令,不能再出现 'Namespace' object has no attribute 'func'。"""
        with tempfile.TemporaryDirectory() as tmp:
            run("init", root=tmp)
            r = run("whoami", root=tmp)
            self.assertNotIn("has no attribute", r.stderr)
            self.assertEqual(r.returncode, 0, r.stderr)


class ServeOnEmptyRootTest(unittest.TestCase):
    """容器第一次启动走的就是这条路:serve 自己把空库初始化。

    守住的坑(2.2.4 实测):`serve` 的自动初始化只建骨架、不建视图,于是
    `views/.revision` 缺失,`docker exec brain brain verify` 一上来就报
    「views/.revision 与数据不一致」——README 第二步就翻车。
    """

    def test_serve_initialises_empty_root_and_verify_passes(self):
        import socket
        import time
        import urllib.request

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["BRAIN_ROOT"] = tmp
            env["BRAIN_URL"] = "http://127.0.0.1:1"
            env.pop("BRAIN_TOKEN", None)
            proc = subprocess.Popen(
                [sys.executable, str(CLI), "serve", "--bind", "127.0.0.1", "--port", str(port)],
                cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                ok = False
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1) as r:
                            if r.status == 200:
                                ok = True
                                break
                    except Exception:
                        time.sleep(0.2)
                self.assertTrue(ok, "serve 没能在 25s 内起来")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

            v = run("verify", root=tmp)
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)
            self.assertIn("五项全过", v.stdout)


class BriefBudgetFlagTest(unittest.TestCase):
    """`--budget` 是文档与 HTTP 接口用的名字,CLI 必须认(否则 README 抄一遍就报错)。"""

    def test_brief_accepts_budget_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run("init", root=tmp).returncode, 0)
            r = run("brief", "--budget", "500", root=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_brief_still_accepts_max_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run("init", root=tmp).returncode, 0)
            r = run("brief", "--max-chars", "500", root=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
