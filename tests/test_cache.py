"""The per-process ticket cache: files stay the truth, so any change on disk is seen on the next call."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from unblockd.store import Store

ROOT = Path(__file__).resolve().parent.parent


class CacheTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.d = Path(self.tmp.name) / "tickets"
		self.s = Store(self.d)

	def tearDown(self):
		self.tmp.cleanup()

	def edit(self, tid, **changes):  # what a hand edit or a git pull does: a new file under the same name
		p = self.d / f"{tid}.json"
		t = json.loads(p.read_text(encoding="utf-8"))
		t.update(changes)
		tmp = p.with_suffix(".x")
		tmp.write_text(json.dumps(t), encoding="utf-8")
		os.replace(tmp, p)

	def test_outside_edits_are_seen(self):
		self.s.create({"title": "aaaa"}, tid="A-1")
		self.assertEqual(self.s.get("A-1")["title"], "aaaa")
		self.edit("A-1", title="bbbb")  # same size, written right after the read
		self.assertEqual(self.s.get("A-1")["title"], "bbbb")
		self.assertEqual(Store(self.d).get("A-1")["title"], "bbbb")  # a new Store (the web server makes one per request) shares the cache
		(self.d / "A-1.json").unlink()
		self.assertEqual(self.s.list(), [])

	def test_other_process_writes_are_seen(self):
		self.s.create({"title": "first"}, tid="A-1")
		self.s.list()
		code = f"from unblockd.store import Store; Store({str(self.d)!r}).update('A-1', {{'title': 'second'}}); Store({str(self.d)!r}).create({{'title': 'new'}}, tid='A-2')"
		subprocess.run([sys.executable, "-c", code], check=True, cwd=ROOT)
		self.assertEqual([(t["id"], t["title"]) for t in self.s.list()], [("A-1", "second"), ("A-2", "new")])

	def test_callers_cannot_change_the_cache(self):
		self.s.create({"title": "x", "tags": ["a"]}, tid="A-1")
		t = self.s.get("A-1")
		t["tags"].append("b")
		t["title"] = "changed"
		self.s.list()[0]["tags"].append("c")
		self.assertEqual((self.s.get("A-1")["title"], self.s.get("A-1")["tags"]), ("x", ["a"]))

	def test_derived_fields_follow_claims_and_attachments(self):
		self.s.create({"title": "x"}, tid="A-1")
		self.assertIsNone(self.s.get("A-1")["claim"])
		self.s.claim("w", tid="A-1")
		self.assertEqual(self.s.get("A-1")["claim"]["agent"], "w")
		(self.d / "files" / "A-1").mkdir(parents=True)
		(self.d / "files" / "A-1" / "n.txt").write_text("hi", encoding="utf-8")
		self.assertEqual(self.s.get("A-1")["files"], ["n.txt"])

	def test_unblocks_counts_each_downstream_ticket_once(self):
		# diamond: A <- B, A <- C, B <- D, C <- D, D <- E (done)
		self.s.create({"title": "a"}, tid="A-1")
		self.s.create({"title": "b", "blocked_by": ["A-1"]}, tid="B-1")
		self.s.create({"title": "c", "blocked_by": ["A-1"]}, tid="C-1")
		self.s.create({"title": "d", "blocked_by": ["B-1", "C-1"]}, tid="D-1")
		self.s.create({"title": "e", "blocked_by": ["D-1"], "status": "done"}, tid="E-1")
		self.assertEqual({t["id"]: t["unblocks"] for t in self.s.list()}, {"A-1": 3, "B-1": 1, "C-1": 1, "D-1": 0, "E-1": 0})

	def test_activity_reads_appends_and_rotation(self):
		self.s.create({"title": "x"}, tid="A-1")
		self.s.add_note("A-1", "omri", "one")
		self.assertEqual(self.s.activity(limit=1)[0]["note"], "one")
		self.s.add_note("A-1", "omri", "two")
		self.assertEqual([e["note"] for e in self.s.activity(limit=2)], ["two", "one"])
		log = self.d / ".unblockd" / "activity.jsonl"
		os.replace(log, log.with_name("activity.1.jsonl"))  # what rotation does
		self.s.add_note("A-1", "omri", "three")
		self.assertEqual([e["note"] for e in self.s.activity(limit=3)], ["three", "two", "one"])
		with open(log, "a", encoding="utf-8") as f:
			f.write('{"ts": "2026-01-01T00:00:00+00:00", "event": "note", "note": "half')  # a writer mid-line
		self.assertEqual(self.s.activity(limit=1)[0]["note"], "three")


if __name__ == "__main__":
	unittest.main()
