"""brief 渲染的唯一实现:CLI 与 serve 共用,避免两处漂移。"""
from __future__ import annotations

from pathlib import Path

from .store import Store


def render_brief(root: Path, store: Store, budget: int = 6000,
                 index_rows: int = 12, recent: int = 5, include_persona: bool = False) -> tuple[str, int, str | None]:
    """返回 (文本, 丢弃段数, 错误码)。"""
    store.rebuild()
    sections: list[tuple[str, str]] = []

    if include_persona:
        p = root / "profiles" / f"{__import__('os').environ.get('BRAIN_ROLE', 'assistant')}.md"
        if not p.exists():
            p = root / "profiles" / "default.md"
        if p.exists():
            sections.append(("角色", p.read_text(encoding="utf-8").strip()))

    rules = root / "views" / "rules.md"
    if rules.exists():
        sections.append(("规范", rules.read_text(encoding="utf-8").strip()))

    idx = root / "views" / "INDEX.md"
    if idx.exists():
        rows = [r for r in idx.read_text(encoding="utf-8").strip().splitlines() if not r.startswith("# ")]
        sections.append(("事实索引", "\n".join(rows[: index_rows + 2])))

    host_topic = root / "views" / "topics" / "host.md"
    if host_topic.exists():
        lines = host_topic.read_text(encoding="utf-8").splitlines()
        sections.append(("主机笔记", "\n".join(lines[: 2 + recent * 6])))

    dropped = 0
    while sections:
        out = "\n\n".join(f"# 【共享记忆库 · {t}】\n{b}" for t, b in sections)
        if len(out) <= budget:
            return out, dropped, None
        sections.pop()
        dropped += 1
    return "", dropped, "budget_too_small"
