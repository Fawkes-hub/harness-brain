#!/usr/bin/env python3
"""release —— 发版门禁 + 打 tag。策略见 docs/VERSIONING.md

用法:
  python3 tools/release.py            # 只自检,不写任何东西
  python3 tools/release.py --apply     # 自检全过则打 annotated tag
  python3 tools/release.py --skip-tests  # 跳过单测(仅本地救急,不用于正式发版)

退出码:0 = 可发版/已发版;1 = 门禁不过
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ok, bad, warn = [], [], []


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


def changelog_versions() -> list[str]:
    t = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", t, re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="自检通过后打 tag")
    ap.add_argument("--skip-tests", action="store_true", help="跳过单测")
    a = ap.parse_args()

    ver = version()
    tag = f"v{ver}"
    print(f"待发版本: {ver} (tag {tag})\n")

    # 1 分支
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    (ok if branch == "main" else bad).append(f"分支 = {branch}" + ("" if branch == "main" else "(应在 main 上发版)"))

    # 2 工作区
    dirty = git("status", "--porcelain")
    (ok if not dirty else bad).append("工作区干净" if not dirty else f"工作区有 {len(dirty.splitlines())} 项未提交改动")

    # 3 CHANGELOG 一致
    cl = changelog_versions()
    if not cl:
        bad.append("CHANGELOG.md 没有形如 '## [x.y.z]' 的版本节")
    else:
        (ok if cl[0] == ver else bad).append(
            f"CHANGELOG 最新版本 = {cl[0]}" + ("" if cl[0] == ver else f",与 __version__ {ver} 不一致")
        )

    # 4 仓内无重复版本字面量
    strays = []
    for p in ROOT.rglob("*.py"):
        if any(x in p.parts for x in (".git", "__pycache__", "tests")):
            continue
        if p == ROOT / "brainlib" / "__init__.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if re.search(r'["\']%s["\']' % re.escape(ver), line):
                strays.append(f"{p.relative_to(ROOT)}:{i}")
    (ok if not strays else bad).append("版本字面量只出现在 __init__.py" if not strays else "多余版本字面量: " + ", ".join(strays[:5]))

    # 5 单测
    if a.skip_tests:
        warn.append("单测已跳过(--skip-tests)")
    else:
        rc, out = sh(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
        tail = [l for l in out.splitlines() if l.startswith("Ran ") or l in ("OK",)]
        (ok if rc == 0 else bad).append("单测 " + (" / ".join(tail) if rc == 0 else "失败:\n" + out[-800:]))

    # 6 docs-check(可选:私有部署里有,公开仓可以没有)
    if not (ROOT / "tools" / "docs-check.py").exists():
        warn.append("没有 docs-check.py,跳过文档一致性检查")
        rc, out = 0, ""
    else:
        rc, out = sh(sys.executable, "tools/docs-check.py")
    (ok if rc == 0 else bad).append("docs-check " + (out.splitlines()[-1] if out else "") if rc == 0 else "docs-check 失败:\n" + out[-800:])

    # 7 数据自检(库存在才跑)
    if (ROOT / "atoms").is_dir():
        rc, out = sh(sys.executable, "bin/brain", "verify")
        last = out.splitlines()[-1] if out else ""
        (ok if rc == 0 else bad).append("brain verify " + last if rc == 0 else "brain verify 失败:\n" + out[-800:])
    else:
        warn.append("没有 atoms/,跳过 brain verify(纯代码仓?正常)")

    # 8 tag 状态
    existing = subprocess.run(["git", "tag", "-l", tag], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    already = False
    if existing:
        head = git("rev-parse", "HEAD")
        tagged = git("rev-parse", f"{tag}^{{commit}}")
        if head == tagged:
            already = True
            warn.append(f"tag {tag} 已指向 HEAD —— 该版本已发过,无待发内容")
        else:
            bad.append(f"tag {tag} 已存在但不指向 HEAD —— 请先把 __version__ 提到新版本")

    # 报告
    for x in ok:
        print(f"  ✓ {x}")
    for x in warn:
        print(f"  ! {x}")
    for x in bad:
        print(f"  ✗ {x}")
    print()

    if bad:
        print("✗ 门禁不通过,未打 tag")
        return 1
    if already:
        print("无待发内容。若要发新版本:先改 brainlib/__init__.py:__version__ 与 CHANGELOG.md。")
        return 0
    if not a.apply:
        print(f"✓ 可以发版。执行 `python3 tools/release.py --apply` 打 tag {tag}")
        return 0

    msg = f"{tag} —— 见 CHANGELOG.md\n\n" + "\n".join("- " + x for x in ok)
    git("tag", "-a", tag, "-m", msg)
    print(f"✓ 已打 annotated tag {tag}")
    if git("remote"):
        print(f"  推送: git push origin main && git push origin {tag}")
    else:
        print("  (尚无远端,仅本地 tag;推送前不要 force-move 它)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
