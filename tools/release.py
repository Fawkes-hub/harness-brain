#!/usr/bin/env python3
"""release —— 发版门禁 + 打 tag。策略见 docs/VERSIONING.md

三种模式:
  python3 tools/release.py            发版门禁(默认):分支/工作区/版本一致/单测/verify/tag 状态
  python3 tools/release.py --check     只做一致性检查(CI 用):版本号 == CHANGELOG 最新节,
                                      且版本字面量只出现在 brainlib/__init__.py
  python3 tools/release.py --apply     门禁全过后打 annotated tag

为什么要有 --check:门禁里"tag 必须不存在或无指向"这类判断,在 main 上每次提交后都会成立,
放在 CI 里会让日常提交永远红。CI 只该校验"版本相关的东西自洽",发版动作留给人。

退出码:0 = 通过 / 可发版 / 已发版;1 = 不通过
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ok: list[str] = []
bad: list[str] = []
warn: list[str] = []


def sh(*cmd: str, cwd: Path = ROOT) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def git(*cmd: str) -> str:
    rc, out = sh("git", *cmd)
    if rc != 0:
        raise SystemExit(f"git {' '.join(cmd)} 失败:\n{out}")
    return out


def version() -> str:
    t = (ROOT / "brainlib" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', t, re.M)
    if not m:
        raise SystemExit("brainlib/__init__.py 里找不到 __version__")
    return m.group(1)


def check_consistency(ver: str) -> None:
    """版本号与 CHANGELOG 一致 + 版本字面量只出现在一处。"""
    cl_file = ROOT / "CHANGELOG.md"
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", cl_file.read_text(encoding="utf-8"), re.M)
    if not versions:
        bad.append("CHANGELOG.md 没有形如 '## [x.y.z]' 的版本节")
    elif versions[0] != ver:
        bad.append(f"CHANGELOG 最新版本 = {versions[0]},与 __version__ {ver} 不一致")
    else:
        ok.append(f"CHANGELOG 最新版本 = {versions[0]}")

    strays: list[str] = []
    for p in ROOT.rglob("*.py"):
        if any(x in p.parts for x in (".git", "__pycache__", "tests")):
            continue
        if p == ROOT / "brainlib" / "__init__.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if re.search(r"""["']%s["']""" % re.escape(ver), line):
                strays.append(f"{p.relative_to(ROOT)}:{i}")
    (ok if not strays else bad).append(
        "版本字面量只出现在 brainlib/__init__.py" if not strays
        else "多余版本字面量: " + ", ".join(strays[:5]))


def run_names(*cmd: str) -> tuple[bool, str]:
    rc, out = sh(*cmd)
    return rc == 0, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做版本一致性检查(CI)")
    ap.add_argument("--apply", action="store_true", help="门禁通过后打 tag")
    ap.add_argument("--skip-tests", action="store_true", help="跳过单测(仅救急)")
    a = ap.parse_args()

    ver = version()
    tag = f"v{ver}"
    print(f"版本: {ver} (tag {tag})\n")

    check_consistency(ver)

    if a.check:
        return report(a)

    # 0 先判断是不是「对已有 tag 的验证运行」:CI 在 tag 上是 detached HEAD,
    #   这种运行不该再要求"在 main 上" —— 否则发版当天的 tag 构建永远是红的。
    existing = subprocess.run(["git", "tag", "-l", tag], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
    head = git("rev-parse", "HEAD")
    already = bool(existing) and git("rev-parse", f"{tag}^{{commit}}") == head
    branch = git("rev-parse", "--abbrev-ref", "HEAD")

    if branch == "HEAD" and already:
        warn.append(f"发版验证模式:detached HEAD 且 {tag} 指向当前提交(CI 对 tag 运行时正常)")
    else:
        (ok if branch == "main" else bad).append(
            f"分支 = {branch}" + ("" if branch == "main" else "(应在 main 上发版)"))
        dirty = git("status", "--porcelain")
        (ok if not dirty else bad).append(
            "工作区干净" if not dirty else f"工作区有 {len(dirty.splitlines())} 项未提交改动")

    if a.skip_tests:
        warn.append("单测已跳过(--skip-tests)")
    else:
        good, out = run_names(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
        tail = [l for l in out.splitlines() if l.startswith("Ran ") or l == "OK"]
        (ok if good else bad).append("单测 " + (" / ".join(tail) if good else "失败:\n" + out[-800:]))

    if (ROOT / "atoms").is_dir():
        good, out = run_names(sys.executable, "bin/brain", "verify")
        last = out.splitlines()[-1] if out else ""
        (ok if good else bad).append("brain verify " + last if good else "brain verify 失败:\n" + out[-800:])
    else:
        warn.append("没有 atoms/,跳过 brain verify(纯代码仓正常)")

    if already:
        warn.append(f"tag {tag} 已指向 HEAD —— 该版本已发过,无待发内容")
    elif existing:
        bad.append(f"tag {tag} 已存在但不指向 HEAD —— 请先把 __version__ 提到新版本")

    if bad:
        return report(a)
    if already:
        report(a)
        print("无待发内容。发新版本:改 brainlib/__init__.py:__version__ 与 CHANGELOG.md。")
        return 0
    if not a.apply:
        report(a)
        print(f"✓ 可以发版。执行 `python3 tools/release.py --apply` 打 tag {tag}")
        return 0

    msg = f"{tag} —— 见 CHANGELOG.md\n\n" + "\n".join("- " + x for x in ok)
    git("tag", "-a", tag, "-m", msg)
    report(a)
    print(f"✓ 已打 annotated tag {tag}")
    if git("remote"):
        print(f"  推送: git push origin main && git push origin {tag}")
    else:
        print("  (尚无远端,仅本地 tag;推送前不要 force-move 它)")
    return 0


def report(a) -> int:
    for x in ok:
        print(f"  ✓ {x}")
    for x in warn:
        print(f"  ! {x}")
    for x in bad:
        print(f"  ✗ {x}")
    print()
    if bad:
        print("✗ 不通过")
        return 1
    if a.check:
        print("✓ 版本一致性检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
