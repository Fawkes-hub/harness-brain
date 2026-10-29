"""atom 模型:解析、渲染、id 与 hash 的唯一定义。

id 规则是**冻结**的:^[mj]_[0-9a-hjkmnp-tv-z]{10}$(Crockford base32 小写,去掉 i l o u)。
它是所有 supersedes 的长期外部引用,改一次要迁移全库 —— 所以只允许在这里定义一次。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import secrets
from dataclasses import dataclass, field

CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
ID_RE = re.compile(r"^[mj]_[0-9a-hjkmnp-tv-z]{10}$")
HASH_ALGO = "sha256-v1"

KINDS = ("fact", "rule", "pref", "lesson", "host", "project", "idea", "secret-ref", "journal")


def new_id(prefix: str = "m") -> str:
    return f"{prefix}_" + "".join(secrets.choice(CROCKFORD) for _ in range(10))


def norm_body(text: str) -> str:
    """规范化:去首尾空白、行尾统一 \\n、行内连续空白折叠、NFC。"""
    import unicodedata

    text = unicodedata.normalize("NFC", text)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(lines).strip()


def content_hash(kind: str, tags: list[str], ttl: str | None, supersedes: str | None, body: str) -> str:
    """hash 只用于「疑似重复」提示,**不用于判重**;判重只认 id(ADR-0004)。"""
    payload = "|".join([kind, " ".join(sorted(tags)), ttl or "", supersedes or "", norm_body(body)])
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


@dataclass
class Atom:
    id: str
    kind: str
    body: str
    occurred_at: str
    received_at: str
    author: dict = field(default_factory=lambda: {"host": "?", "harness": "?", "role": "?"})
    tags: list[str] = field(default_factory=list)
    confidence: str = "medium"
    source: str = "manual"
    supersedes: str | None = None
    ttl: str | None = None
    section: str = ""
    time_suspect: bool = False
    hash: str = ""
    hash_algo: str = HASH_ALGO

    def recompute_hash(self) -> str:
        return content_hash(self.kind, self.tags, self.ttl, self.supersedes, self.body)


def _fmt_author(a: dict) -> str:
    return "{ host: %s, harness: %s, role: %s }" % (
        a.get("host", "?"), a.get("harness", "?"), a.get("role", "?"))


def render_atom(a: Atom) -> str:
    lines = [
        "---",
        f"id: {a.id}",
        f"occurred_at: {a.occurred_at}",
        f"received_at: {a.received_at}",
        f"author: {_fmt_author(a.author)}",
        f"kind: {a.kind}",
    ]
    if a.section:
        lines.append(f"section: {a.section}")
    lines += [
        f"tags: [{', '.join(a.tags)}]",
        f"confidence: {a.confidence}",
        f"source: {a.source}",
    ]
    if a.supersedes:
        lines.append(f"supersedes: {a.supersedes}")
    if a.ttl:
        lines.append(f"ttl: {a.ttl}")
    if a.time_suspect:
        lines.append("time_suspect: true")
    lines += [f"hash: {a.hash or a.recompute_hash()}", f"hash_algo: {a.hash_algo}", "---", "", a.body, ""]
    return "\n".join(lines)


_AUTHOR_RE = re.compile(r"author:\s*\{(.*)\}")
_TAGS_RE = re.compile(r"tags:\s*\[(.*)\]")


def parse_atom(text: str) -> Atom:
    """解析一个 atom 文件。缺字段抛 ValueError —— 宁可炸,也不要静默读成半条记忆。"""
    if not text.startswith("---"):
        raise ValueError("缺少 frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter 不完整")
    front, body = parts[1], parts[2]
    kv: dict[str, str] = {}
    for line in front.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        kv[k.strip()] = v.strip().strip('"')

    author_raw = _AUTHOR_RE.search(front)
    author = {"host": "?", "harness": "?", "role": "?"}
    if author_raw:
        for seg in author_raw.group(1).split(","):
            if ":" in seg:
                k, v = seg.split(":", 1)
                author[k.strip()] = v.strip()
    tags: list[str] = []
    tm = _TAGS_RE.search(front)
    if tm and tm.group(1).strip():
        tags = [t.strip() for t in tm.group(1).split(",") if t.strip()]

    body = body.strip("\n")
    return Atom(
        id=kv.get("id", ""),
        kind=kv.get("kind", ""),
        body=body,
        occurred_at=kv.get("occurred_at", ""),
        received_at=kv.get("received_at", ""),
        author=author,
        tags=tags,
        confidence=kv.get("confidence", "medium"),
        source=kv.get("source", ""),
        supersedes=kv.get("supersedes") or None,
        ttl=kv.get("ttl") or None,
        section=kv.get("section", ""),
        time_suspect=kv.get("time_suspect", "false").lower() == "true",
        hash=kv.get("hash", ""),
        hash_algo=kv.get("hash_algo", HASH_ALGO),
    )


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
