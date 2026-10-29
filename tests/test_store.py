"""v2 数据层测试。跑法:python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brainlib.model import Atom, ID_RE, content_hash, new_id, now_iso, parse_atom, render_atom
from brainlib.store import Store


def mk(home: Path, body="内容", **kw) -> Atom:
    a = Atom(id=kw.pop("id", new_id()), kind=kw.pop("kind", "fact"), body=body,
             occurred_at=kw.pop("occurred_at", now_iso()), received_at=now_iso(),
             author={"host": "t", "harness": "u", "role": "assistant"},
             tags=kw.pop("tags", ["test"]), **kw)
    a.hash = content_hash(a.kind, a.tags, a.ttl, a.supersedes, a.body)
    return a


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-"))
        self.st = Store(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestModel(Base):
    def test_id_regex(self):
        for _ in range(50):
            self.assertRegex(new_id(), ID_RE)

    def test_roundtrip(self):
        a = mk(self.tmp, "代理地址 198.51.100.7", tags=["network", "proxy"], ttl="2027-01-01")
        b = parse_atom(render_atom(a))
        self.assertEqual((a.id, a.kind, a.body, a.tags, a.ttl), (b.id, b.kind, b.body, b.tags, b.ttl))
        self.assertEqual(a.hash, b.recompute_hash())

    def test_bad_frontmatter_raises(self):
        with self.assertRaises(ValueError):
            parse_atom("没有 frontmatter")


class TestStore(Base):
    def test_put_and_replay(self):
        a = mk(self.tmp)
        self.assertEqual(self.st.put_atom(a), "created")
        self.assertEqual(self.st.put_atom(a), "replay")
        self.assertEqual(len(self.st.load_atoms()), 1)

    def test_same_id_different_body_conflicts(self):
        a = mk(self.tmp, "第一版")
        self.st.put_atom(a)
        b = mk(self.tmp, "第二版", id=a.id)
        with self.assertRaises(FileExistsError):
            self.st.put_atom(b)

    def test_degenerate_id_rejected(self):
        a = mk(self.tmp, id="m_short")
        with self.assertRaises(ValueError):
            self.st.put_atom(a)


class TestTombstone(Base):
    def test_seq_monotonic_and_last_wins(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        t1 = self.st.append_tombstone(a.id, "deprecated", "过期", "t/u")
        t2 = self.st.append_tombstone(a.id, "active", "恢复", "t/u")
        self.assertEqual((t1["seq"], t2["seq"]), (1, 2))
        self.assertEqual(self.st.status_map()[a.id]["status"], "active")
        shown, _ = self.st.visible()
        self.assertEqual([x.id for x in shown], [a.id], "恢复后应重新可见")

    def test_deprecated_hidden(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        self.st.append_tombstone(a.id, "deprecated", "过期", "t/u")
        shown, _ = self.st.visible()
        self.assertEqual(shown, [])

    def test_invalid_status(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        with self.assertRaises(ValueError):
            self.st.append_tombstone(a.id, "bogus", "", "t/u")


class TestFold(Base):
    def test_supersede_hides_old(self):
        old = mk(self.tmp, "旧")
        new = mk(self.tmp, "新", supersedes=old.id)
        self.st.put_atom(old); self.st.put_atom(new)
        shown, notes = self.st.visible()
        self.assertEqual([x.id for x in shown], [new.id])
        self.assertEqual(notes, {})

    def test_missing_target_visible_with_note(self):
        a = mk(self.tmp, "孤儿", supersedes="m_zzzzzzzzzz")
        self.st.put_atom(a)
        shown, notes = self.st.visible()
        self.assertEqual([x.id for x in shown], [a.id])
        self.assertIn("取代目标缺失", notes[a.id])

    def test_cycle_does_not_hang_and_is_flagged(self):
        a = mk(self.tmp, "A")
        b = mk(self.tmp, "B", supersedes=a.id)
        self.st.put_atom(a); self.st.put_atom(b)
        # 人为制造 A→B 的环:直接改 A 的 supersedes 后重写
        p = self.st.atom_paths()[0]
        txt = p.read_text(encoding="utf-8")
        a2 = parse_atom(txt); a2.supersedes = b.id; a2.hash = a2.recompute_hash()
        from brainlib.store import atomic_write
        atomic_write(p, render_atom(a2))
        shown, notes = self.st.visible()          # 只要不 hang 就算过
        self.assertTrue(any("取代环" in v for v in notes.values()))

    def test_multi_tail_flagged(self):
        root = mk(self.tmp, "根")
        k1 = mk(self.tmp, "尾1", supersedes=root.id)
        k2 = mk(self.tmp, "尾2", supersedes=root.id)
        for a in (root, k1, k2):
            self.st.put_atom(a)
        shown, notes = self.st.visible()
        self.assertEqual({x.id for x in shown}, {k1.id, k2.id})
        self.assertTrue(any("多尾" in v for v in notes.values()))


class TestViews(Base):
    def test_rebuild_is_idempotent(self):
        self.st.put_atom(mk(self.tmp, "规则", kind="rule", tags=["rule"]))
        r1 = self.st.rebuild()
        body1 = (self.tmp / "views" / "rules.md").read_text(encoding="utf-8")
        r2 = self.st.rebuild(force=True)
        body2 = (self.tmp / "views" / "rules.md").read_text(encoding="utf-8")
        self.assertEqual(r1, r2)
        self.assertEqual(body1, body2)

    def test_revision_changes_on_write(self):
        self.st.put_atom(mk(self.tmp))
        r1 = self.st.rebuild()
        self.st.put_atom(mk(self.tmp))
        r2 = self.st.rebuild()
        self.assertNotEqual(r1, r2)

    def test_revision_file_written_last(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        self.st.rebuild()
        self.assertTrue((self.tmp / "views" / ".revision").exists())
        self.assertFalse((self.tmp / "views" / ".staging").exists())

    def test_stale_topic_removed(self):
        a = mk(self.tmp, tags=["alpha"])
        self.st.put_atom(a)
        self.st.rebuild()
        self.assertTrue(any("alpha" in p.name for p in (self.tmp / "views" / "topics").iterdir()))
        self.st.append_tombstone(a.id, "deprecated", "", "t/u")
        self.st.rebuild()
        self.assertEqual(list((self.tmp / "views" / "topics").iterdir()), [])


class TestVerify(Base):
    def test_clean_passes(self):
        self.st.put_atom(mk(self.tmp))
        self.st.rebuild()
        self.assertEqual(self.st.verify(), [])

    def test_bad_hash_detected(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        p = self.st.atom_paths()[0]
        p.write_text(p.read_text(encoding="utf-8").replace("hash: sha256:", "hash: sha256:dead"), encoding="utf-8")
        self.assertTrue(any("hash 不一致" in x for x in self.st.verify()))

    def test_dangling_supersedes_detected(self):
        a = mk(self.tmp, supersedes="m_zzzzzzzzzz")
        self.st.put_atom(a)
        self.st.rebuild()
        self.assertTrue(any("supersedes 目标不存在" in x for x in self.st.verify()))

    def test_revision_mismatch_detected(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        self.st.rebuild()
        (self.tmp / "views" / ".revision").write_text("sha256:deadbeef\n", encoding="utf-8")
        self.assertTrue(any("需 reindex" in x for x in self.st.verify()))

    def test_tombstone_seq_order_detected(self):
        a = mk(self.tmp)
        self.st.put_atom(a)
        # 手工写一个乱序的 tombstones.jsonl
        self.st.tomb_path.write_text(
            json.dumps({"id": a.id, "status": "deprecated", "seq": 5, "at": now_iso(),
                        "reason": "", "author": "t/u"}, ensure_ascii=False) + "\n"
            + json.dumps({"id": a.id, "status": "active", "seq": 3, "at": now_iso(),
                          "reason": "", "author": "t/u"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.assertTrue(any("seq" in x for x in self.st.verify()))



class TestSafety(Base):
    def test_occurred_at_cannot_traverse(self):
        a = mk(self.tmp, occurred_at="../../../../tmp/evil")
        with self.assertRaises(ValueError):
            self.st.put_atom(a)
        self.assertEqual(self.st.load_atoms(), {})

    def test_month_folder_from_valid_occurred_at(self):
        a = mk(self.tmp, occurred_at="2025-03-04T10:00:00+08:00")
        self.st.put_atom(a)
        self.assertTrue((self.tmp / "atoms" / "202503" / f"{a.id}.md").exists())
        self.assertEqual(list((self.tmp / "atoms").glob("2025*/**/*.md"))[0].name, f"{a.id}.md")

    def test_unicode_tags_do_not_collide(self):
        for tag in ("中文", "另一个中文", "a b", "a_b"):
            self.st.put_atom(mk(self.tmp, tags=[tag]))
        self.st.rebuild()
        topics = list((self.tmp / "views" / "topics").iterdir())
        self.assertEqual(len(topics), 4, [p.name for p in topics])
        self.assertEqual(len({p.name for p in topics}), 4)



if __name__ == "__main__":
    unittest.main()
