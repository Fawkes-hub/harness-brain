"""存储层:atoms / tombstones / 派生视图。

三条不可退让的性质:
  1. atom 写入是原子的(tmp + fsync + rename),写完永不修改;
  2. 视图是纯函数,重建顺序 staging → replace → **最后**写 .revision;
  3. 状态由 tombstone 的**末条**(按 seq)决定,不是「出现过就隐藏」。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .model import Atom, ID_RE, now_iso, parse_atom, parse_iso, render_atom


def _fsync_dir(p: Path) -> None:
    try:
        fd = os.open(p, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _fsync_dir(path.parent)


def ensure_layout(root: "Path | str") -> list[str]:
    """创建一个空库该有的骨架;已存在的东西一律不碰。返回本次真实创建项。

    为什么要这个:此前 CLI 把「有没有 atoms/」当作「是不是 v2 库」的判据,
    于是全新的空目录下 `brain serve` 会掉进 v1 代码路径并以 AttributeError 退出
    —— 第一次用容器的人第一步就撞墙。空库是合法状态,不该靠猜。
    """
    root = Path(root)
    made: list[str] = []
    for d in ("atoms", "views", "state", "profiles"):
        path = root / d
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            made.append(d + "/")
    tomb = root / "tombstones.jsonl"
    if not tomb.exists():
        tomb.write_text("", encoding="utf-8")
        made.append("tombstones.jsonl")
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("# 派生层与运行时状态不入库\nviews/\nstate/\nprofiles/\n__pycache__/\n", encoding="utf-8")
        made.append(".gitignore")
    return made


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.atoms_dir = self.root / "atoms"
        self.views_dir = self.root / "views"
        self.state_dir = self.root / "state"
        self.tomb_path = self.root / "tombstones.jsonl"
        self.profiles_dir = self.root / "profiles"

    # ---------------- atoms ----------------
    def atom_paths(self) -> list[Path]:
        return sorted(self.atoms_dir.rglob("*.md")) if self.atoms_dir.exists() else []

    def load_atoms(self) -> dict[str, Atom]:
        out: dict[str, Atom] = {}
        for p in self.atom_paths():
            try:
                a = parse_atom(p.read_text(encoding="utf-8"))
            except ValueError as e:
                a = None
                out[f"!bad:{p.name}"] = Atom(id="", kind="", body=f"{p}: {e}",
                                             occurred_at="", received_at="")
            if a is not None:
                out[a.id] = a
        return out

    def put_atom(self, a: Atom) -> str:
        """返回 'created' | 'replay';同 id 不同内容抛 FileExistsError。"""
        ident = a.id if isinstance(a.id, str) else ""
        if not ID_RE.fullmatch(ident):
            raise ValueError(f"id 不合规:{a.id}")
        occ = parse_iso(a.occurred_at) if a.occurred_at else None
        if occ is None:
            raise ValueError("occurred_at 不是合法 ISO 时间")
        path = self.atoms_dir / occ.strftime("%Y%m") / f"{ident}.md"
        if path.exists():
            old = parse_atom(path.read_text(encoding="utf-8"))
            if old.recompute_hash() == a.recompute_hash():
                return "replay"
            raise FileExistsError(f"id 冲突:{a.id}(已存在且内容不同)")
        a.hash = a.hash or a.recompute_hash()
        atomic_write(path, render_atom(a))
        return "created"

    # ---------------- tombstones ----------------
    def load_tombstones(self) -> list[dict]:
        if not self.tomb_path.exists():
            return []
        out = []
        for line in self.tomb_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    def append_tombstone(self, atom_id: str, status: str, reason: str,
                         author: str, replaced_by: str | None = None) -> dict:
        if status not in ("deprecated", "retracted", "active"):
            raise ValueError("status 只能是 deprecated/retracted/active")
        self.tomb_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.state_dir / ".tomb.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            existing = self.load_tombstones()
            seq = (existing[-1]["seq"] if existing else 0) + 1
            rec = {"id": atom_id, "status": status, "seq": seq, "at": now_iso(),
                   "reason": reason, "author": author}
            if replaced_by:
                rec["replaced_by"] = replaced_by
            with open(self.tomb_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            fcntl.flock(lk, fcntl.LOCK_UN)
        return rec

    def status_map(self) -> dict[str, dict]:
        """id → 末条 tombstone(按 seq 最大者)。"""
        best: dict[str, dict] = {}
        for t in self.load_tombstones():
            cur = best.get(t["id"])
            if cur is None or t["seq"] > cur["seq"]:
                best[t["id"]] = t
        return best

    # ---------------- 视图折叠 ----------------
    def visible(self) -> tuple[list[Atom], dict[str, str]]:
        atoms = self.load_atoms()
        bad = {k: v.body for k, v in atoms.items() if k.startswith("!bad:")}
        atoms = {k: v for k, v in atoms.items() if not k.startswith("!bad:")}
        status = self.status_map()
        notes: dict[str, str] = {}

        # 丢弃已遗忘/撤回的
        alive = {i: a for i, a in atoms.items()
                 if status.get(i, {}).get("status") != "deprecated"
                 and status.get(i, {}).get("status") != "retracted"}

        children: dict[str, list[str]] = {}
        for i, a in alive.items():
            if a.supersedes:
                if a.supersedes not in atoms:
                    notes[i] = f"⚠️ 取代目标缺失({a.supersedes})"
                else:
                    children.setdefault(a.supersedes, []).append(i)

        def in_cycle(start: str) -> bool:
            seen, cur = set(), start
            while True:
                a = alive.get(cur)
                if not a or not a.supersedes:
                    return False
                cur = a.supersedes
                if cur == start:
                    return True
                if cur in seen:
                    return False
                seen.add(cur)

        hidden: set[str] = set()
        for parent, kids in children.items():
            live_kids = [k for k in kids if k in alive]
            if not live_kids:
                continue
            if len(live_kids) > 1:
                for k in live_kids:
                    notes[k] = notes.get(k, "") + " ⚠️ 多尾冲突"
            for k in live_kids:
                if k != parent and not in_cycle(parent):
                    hidden.add(parent)

        for i in alive:
            if in_cycle(i):
                notes[i] = notes.get(i, "") + " ⚠️ 取代环"

        shown = [a for i, a in alive.items() if i not in hidden]
        shown.sort(key=lambda a: (a.occurred_at or "", a.id))
        for i, msg in bad.items():
            notes[i] = msg
        return shown, notes

    # ---------------- revision / 重建 ----------------
    def revision(self) -> str:
        atoms = self.load_atoms()
        h = hashlib.sha256()
        h.update(("hash_algo:sha256-v1\n").encode())
        for i in sorted(atoms):
            h.update(f"{i}:{atoms[i].hash or atoms[i].recompute_hash()}\n".encode())
        for t in sorted(self.load_tombstones(), key=lambda x: x["seq"]):
            h.update(json.dumps(t, ensure_ascii=False, sort_keys=True).encode() + b"\n")
        return "sha256:" + h.hexdigest()[:16]

    def current_revision(self) -> str:
        p = self.views_dir / ".revision"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def rebuild(self, force: bool = False) -> str:
        rev = self.revision()
        if not force and rev == self.current_revision():
            return rev
        lock = self.state_dir / ".rebuild.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                rev = self.revision()
                if not force and rev == self.current_revision():
                    return rev
                self._render(rev)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)
        return rev

    def _render(self, rev: str) -> None:
        shown, notes = self.visible()
        staging = self.views_dir / ".staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        by_kind: dict[str, list[Atom]] = {}
        by_tag: dict[str, list[Atom]] = {}
        for a in shown:
            by_kind.setdefault(a.kind, []).append(a)
            for t in a.tags:
                by_tag.setdefault(t, []).append(a)

        def block(a: Atom) -> str:
            when = (a.occurred_at or "")[:10]
            note = notes.get(a.id, "")
            head = f"- `{a.id}` {when} · {a.author.get('host','?')}/{a.author.get('host','')}".rstrip("/")
            head = f"- `{a.id}` {when} · {a.author.get('host','?')}/{a.author.get('harness','?')}"
            if a.section:
                head += f" · {a.section}"
            if note:
                head += " " + note
            return head + "\n" + "\n".join("  " + ln for ln in a.body.splitlines())

        # rules.md
        rules = by_kind.get("rule", []) + by_kind.get("pref", [])
        rules.sort(key=lambda a: (a.section, a.occurred_at or ""))
        cur_section, chunks = None, []
        for a in rules:
            if a.section != cur_section:
                cur_section = a.section
                chunks.append(f"\n## {cur_section}\n" if cur_section else "")
            chunks.append(block(a) + "\n")
        atomic_write(staging / "rules.md",
                     "# 规范与偏好(派生文件,勿手改)\n" + "".join(chunks))

        # INDEX.md
        rows = ["# 事实索引(派生)\n", "| 主题 | 条数 | 最新 |", "| --- | --- | --- |"]
        for tag in sorted(by_tag):
            items = sorted(by_tag[tag], key=lambda a: a.occurred_at or "")
            latest = items[-1].body.splitlines()[0][:80].replace("|", "/")
            rows.append(f"| {tag} | {len(items)} | {latest} |")
        atomic_write(staging / "INDEX.md", "\n".join(rows) + "\n")

        # topics
        (staging / "topics").mkdir()
        used: set[str] = set()
        for tag, items in by_tag.items():
            items = sorted(items, key=lambda a: a.occurred_at or "", reverse=True)
            base = re.sub(r"[^\w.-]+", "_", tag.strip(), flags=re.UNICODE).strip("._") or "topic"
            slug = base[:48]
            if slug in used:
                slug = f"{slug[:40]}-{hashlib.sha1(tag.encode('utf-8')).hexdigest()[:8]}"
            used.add(slug)
            body = f"# {tag}\n\n" + "\n".join(block(a) for a in items)
            atomic_write(staging / "topics" / f"{slug}.md", body)

        _fsync_dir(staging)
        # 逐文件替换;最后写 .revision
        self.views_dir.mkdir(parents=True, exist_ok=True)
        new_files = {p.relative_to(staging) for p in staging.rglob("*") if p.is_file()}
        for rel in new_files:
            dst = self.views_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / rel, dst)
        for old in self.views_dir.rglob("*"):
            if old.is_file() and old.name != ".revision" and old.relative_to(self.views_dir) not in new_files:
                old.unlink()
        shutil.rmtree(staging, ignore_errors=True)
        atomic_write(self.views_dir / ".revision", rev + "\n")
        _fsync_dir(self.views_dir)

    # ---------------- 自检 ----------------
    def verify(self) -> list[str]:
        problems: list[str] = []
        atoms = self.load_atoms()
        for k, a in atoms.items():
            if k.startswith("!bad:"):
                problems.append(f"{k}: {a.body}")
        for i, a in atoms.items():
            if i.startswith("!bad:"):
                continue
            if not ID_RE.match(a.id):
                problems.append(f"{i}: id 不合规")
            if a.hash != a.recompute_hash():
                problems.append(f"{i}: hash 不一致")
            if a.supersedes and a.supersedes not in atoms:
                problems.append(f"{i}: supersedes 目标不存在({a.supersedes})")
        seqs = [t["seq"] for t in self.load_tombstones()]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            problems.append("tombstones.jsonl: seq 不严格递增")
        if self.current_revision() != self.revision():
            problems.append("views/.revision 与数据不一致,需 reindex")
        return problems
