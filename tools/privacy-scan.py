#!/usr/bin/env python3
"""privacy-scan —— 发布前的自动脱敏扫描(通用规则,可随仓库公开)。

分工:
  privacy-scan.py   通用正则 + 通用专名类别,可以放在公开仓,CI 每次跑
  lexicon-scan.py   私有词表(真实主机名/人名/地址/内部项目名),词表不进仓库,只在本机跑

判定三类处置:
  A 硬禁 → 个人/主机/实例标识、家目录、卷路径、内网地址、私有 OS 名、真实公司人名
  B 泛化 → NAS→server、软路由→edge gateway、刷流→scheduled job
  C 保留 → Git / Markdown / SQLite / Docker / MCP / age / RFC 5737 网段

用法:
  python3 tools/privacy-scan.py                # 扫 git 跟踪文件(排除数据目录)
  python3 tools/privacy-scan.py --strict       # 追加"疑似命中原样列示"的一档
  python3 tools/privacy-scan.py --paths docs bin tools
  python3 tools/privacy-scan.py --list
退出码: 0 = 干净, 1 = 有命中
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 这些目录/前缀属于数据层或产物层,永远不进公开仓,也就不参与扫描
# 扫描器自身是规则表(里面全是"违规样例"),不参与自我扫描
EXCLUDE_FILES = ("tools/privacy-scan.py",)

EXCLUDE_PREFIX = ("atoms/", "views/", "state/", "inbox/", "facts/", "hosts/",
                  "bundle/", "snapshots/", "legacy/", ".git/", "__pycache__/")

RULES: list[dict] = [
    dict(level="block", name="私网 IPv4",
         rx=r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b",
         hint="文档网段改用 RFC 5737:192.0.2.0/24、198.51.100.0/24、203.0.113.0/24"),
    dict(level="block", name="主机绝对路径",
         rx=r"(?<![\w/$.])/(?:vol\d+|volume\d+|mnt|media|Users)(?:/|$)",
         hint="改成相对路径或 $BRAIN_ROOT / /data 这类约定路径"),
    dict(level="block", name="家目录绝对路径",
         rx=r"(?:^|[\s\"'`(=])/(?:home|root)/|/home/[a-z_]",
         hint="家目录路径不进公开文档"),
    dict(level="block", name="Windows 用户路径",
         rx=r"[A-Za-z]:\\\\?Users\\\\?",
         hint="同上"),
    dict(level="block", name="MAC 地址",
         rx=r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b",
         hint="物理地址不进仓库"),
    dict(level="block", name="邮箱地址",
         rx=r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b",
         allow=r"@(?:example\.(?:com|org|invalid)|users\.noreply\.github\.com)$",
         hint="只允许 example.* 与 noreply 邮箱"),
    dict(level="block", name="内网域名后缀",
         rx=r"[\w-]+\.(?:local|lan|internal|home\.arpa)\b",
         hint="内网主机名一律换成 <host> 占位"),
    dict(level="block", name="个人称呼 / 人格名",
         rx=r"哥哥|小助手|我的主人|妹妹",
         hint="L4 叙事层不得出现任何个人称呼"),
    dict(level="block", name="私有 OS / 设备厂商",
         rx=r"飞牛|fnOS|fnos|iStoreOS|iStore|群晖|Synology|铁威马|绿联|威联通|QNAP|极空间|黑群",
         hint="B 类泛化:私有 NAS 系统 → self-hosted Linux server"),
    dict(level="block", name="私有网络设备角色(具体化)",
         rx=r"软路由|刷流|分身容器",
         hint="B 类泛化:软路由 → edge gateway;刷流 → scheduled seeding job"),
    dict(level="strict", name="公网 IPv4 字面量",
         rx=r"\b(?!(?:127|0|224|255)\.)(?!(?:192\.0\.2|198\.51\.100|203\.0\.113)\.)(?:\d{1,3}\.){3}\d{1,3}\b",
         hint="真实公网地址不得出现(版本号四段式请自行确认)"),
    dict(level="strict", name="host:port 组合",
         rx=r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[\w-]+\.[a-z]{2,}):\d{2,5}\b",
         hint="示例端口写成 <port> 或 127.0.0.1:8080 这类通用值"),
    dict(level="strict", name="内部实例 ID",
         rx=r"\b(?=[A-Z0-9]{7}\b)[A-Z0-9]*\d[A-Z0-9]*\b",
         hint="agent / task / 容器短 ID 不进公开仓"),
]

ALLOW_COMMON = [
    r"127\.0\.0\.1", r"0\.0\.0\.0", r"192\.0\.2\.", r"198\.51\.100\.", r"203\.0\.113\.",
    r"RFC ?5737", r"localhost", r"\.example\b",
]


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        return sorted(p for p in ROOT.rglob("*") if p.is_file())
    return [ROOT / p for p in out.stdout.split("\0") if p]


def looks_binary(p: Path) -> bool:
    try:
        chunk = p.open("rb").read(4096)
    except OSError:
        return True
    return b"\0" in chunk


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="同时跑 strict 档规则")
    ap.add_argument("--paths", nargs="*", default=None, help="只扫这些子路径")
    ap.add_argument("--list", action="store_true", help="打印规则表")
    ap.add_argument("--max-per-rule", type=int, default=12)
    ap.add_argument("--by-file", action="store_true", help="按文件汇总命中数")
    a = ap.parse_args()

    if a.list:
        for r in RULES:
            print(f"[{r['level']:6}] {r['name']}: {r['rx']}")
            print(f"         → {r['hint']}")
        return 0

    rules = [r for r in RULES if r["level"] == "block" or a.strict]
    files = [p for p in tracked_files() if p.is_file() and not looks_binary(p)]
    rel = [p.relative_to(ROOT).as_posix() for p in files]
    if a.paths:
        files = [p for p, r in zip(files, rel) if any(r == x.rstrip("/") or r.startswith(x.rstrip("/") + "/") for x in a.paths)]
    files = [p for p in files
             if not p.relative_to(ROOT).as_posix().startswith(EXCLUDE_PREFIX)
             and p.relative_to(ROOT).as_posix() not in EXCLUDE_FILES]

    total, hits_by_rule = 0, {}
    for p in files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if any(re.search(al, line) for al in ALLOW_COMMON) and not re.search(
                    r"192\.168\.\d|(?:^|[^\d.])10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d", line):
                continue
            for r in rules:
                for m in re.finditer(r["rx"], line):
                    frag = m.group(0)
                    if r.get("allow") and re.search(r["allow"], line):
                        continue
                    total += 1
                    hits_by_rule.setdefault(r["name"], []).append(
                        (p.relative_to(ROOT).as_posix(), i, frag, line.strip()[:110])
                    )

    for r in rules:
        items = hits_by_rule.get(r["name"])
        if not items:
            continue
        print(f"\n■ {r['name']}  —— {len(items)} 处")
        print(f"  → {r['hint']}")
        seen = set()
        shown = 0
        for path, ln, frag, line in items:
            key = (path, ln)
            if key in seen:
                continue
            seen.add(key)
            if shown >= a.max_per_rule:
                print(f"  … 另有 {len(items) - shown} 处(用 --max-per-rule 调大)")
                break
            print(f"  {path}:{ln}  [{frag[:40]}]  {line}")
            shown += 1

    if a.by_file and total:
        per: dict[str, int] = {}
        for items in hits_by_rule.values():
            for path, ln, _, _ in items:
                per[path] = per.get(path, 0) + 1
        print("\n■ 按文件")
        for path, n in sorted(per.items(), key=lambda kv: -kv[1]):
            print(f"  {n:4d}  {path}")

    print(f"\n扫描 {len(files)} 个文件,命中 {total} 处")
    if total:
        print("✗ 有命中,清理后重跑")
        return 1
    print("✓ 通用规则零命中")
    return 0


if __name__ == "__main__":
    sys.exit(main())
