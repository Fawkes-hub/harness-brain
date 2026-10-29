#!/usr/bin/env python3
"""lexicon-scan —— 用「不进仓库的私有词表」扫真实专名。

为什么分成两个扫描器:
  真实主机名/人名/街道/内部项目名不可能写进公开仓的正则里——那本身就是泄露。
  所以通用规则放 privacy-scan.py(随仓库公开),真实词表放仓外私有文件,只在本机跑。

词表位置(按优先级):
  1. --lexicon 参数
  2. 环境变量 BRAIN_LEXICON
  3. <仓库父目录>/harness-brain-private/lexicon.txt   ← 建议位置,不在任何 git 仓内

词表格式,一行一条:
  <词>                    # 必须零命中
  <词>\t<glob[,glob]>      # 允许出现在这些路径 glob 里(例如 install.sh 里的示例)
  # 开头为注释
ASCII 词按词边界匹配(不分大小写),含中文的词按子串匹配。

用法:
  python3 tools/lexicon-scan.py
  python3 tools/lexicon-scan.py --paths docs bin
退出码: 0 = 零命中, 1 = 有命中, 2 = 词表不存在
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_PREFIX = ("atoms/", "views/", "state/", "inbox/", "facts/", "hosts/",
                  "bundle/", "snapshots/", "legacy/", ".git/", "__pycache__/")


def default_lexicon() -> Path:
    env = os.environ.get("BRAIN_LEXICON")
    if env:
        return Path(env)
    return ROOT.parent / "harness-brain-private" / "lexicon.txt"


def load(path: Path) -> list[tuple[str, list[str]]]:
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        term = parts[0].strip()
        allow = [x.strip() for x in parts[1].split(",") if x.strip()] if len(parts) > 1 else []
        if term:
            entries.append((term, allow))
    return entries


def is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        return sorted(p for p in ROOT.rglob("*") if p.is_file())
    return [ROOT / p for p in out.stdout.split("\0") if p]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lexicon", default=None)
    ap.add_argument("--paths", nargs="*", default=None)
    a = ap.parse_args()

    lex = Path(a.lexicon) if a.lexicon else default_lexicon()
    if not lex.exists():
        print(f"✗ 词表不存在: {lex}\n  先建一份,一行一个词;建议放在仓库之外并 chmod 600")
        return 2
    entries = load(lex)
    pats = []
    for term, allow in entries:
        rx = re.compile(r"\b%s\b" % re.escape(term), re.I) if is_ascii(term) else re.compile(re.escape(term))
        pats.append((term, allow, rx))

    files = [p for p in tracked_files() if p.is_file()]
    skip = {"tools/privacy-scan.py", "tools/lexicon-scan.py"}
    files = [p for p in files
             if not p.relative_to(ROOT).as_posix().startswith(EXCLUDE_PREFIX)
             and p.relative_to(ROOT).as_posix() not in skip]
    if a.paths:
        files = [p for p in files
                 if any(p.relative_to(ROOT).as_posix() == x.rstrip("/")
                        or p.relative_to(ROOT).as_posix().startswith(x.rstrip("/") + "/") for x in a.paths)]

    total = 0
    by_term: dict[str, list[tuple[str, int, str]]] = {}
    for p in files:
        rp = p.relative_to(ROOT).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for term, allow, rx in pats:
                if not rx.search(line):
                    continue
                if any(fnmatch.fnmatch(rp, g) for g in allow):
                    continue
                total += 1
                by_term.setdefault(term, []).append((rp, i, line.strip()[:110]))

    for term, items in sorted(by_term.items(), key=lambda kv: -len(kv[1])):
        print(f"\n■ {term}  —— {len(items)} 处")
        for path, ln, line in items[:8]:
            print(f"  {path}:{ln}  {line}")
        if len(items) > 8:
            print(f"  … 另有 {len(items) - 8} 处")

    print(f"\n词表 {len(entries)} 条 / 扫描 {len(files)} 个文件 / 命中 {total} 处")
    if total:
        print("✗ 私有专名未清干净")
        return 1
    print("✓ 私有词表零命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
