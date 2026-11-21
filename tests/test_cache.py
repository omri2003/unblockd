"""The per-process ticket cache: files stay the truth, so any change on disk is seen on the next call."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd import files
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


class ScanReuseTest(unittest.TestCase):
	"""CORE-60: inside the window a read outside the lock reuses the last stat pass; a write by this process, or the lock, stats again."""
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.d = Path(self.tmp.name) / "tickets"
		self.s = Store(self.d)
		self.s.create({"title": "aaaa"}, tid="A-1")
		self.knob = mock.patch.object(files, "SCAN_REUSE_S", 60.0)  # a window no test run outlasts
		self.knob.start()
		self.scans = 0
		real = os.scandir
		def counting(path=".", *a):
			if Path(path) == self.d:
				self.scans += 1
			return real(path, *a)
		self.spy = mock.patch.object(files.os, "scandir", counting)
		self.spy.start()

	def tearDown(self):
		self.spy.stop()
		self.knob.stop()
		self.tmp.cleanup()

	def hand_edit(self, title, in_place=False):  # this process's write counter does not move
		p = self.d / "A-1.json"
		t = json.loads(p.read_text(encoding="utf-8"))
		t["title"] = title + " " * len(title)  # another size: the file's stamp changes even within one clock tick
		if in_place:  # an editor saving over the file: the folder's mtime stays
			with open(p, "r+", encoding="utf-8") as f:
				f.write(json.dumps(t))
				f.truncate()
			return
		(tmp := p.with_suffix(".x")).write_text(json.dumps(t), encoding="utf-8")
		os.replace(tmp, p)  # what every store write and a git checkout do: the folder's mtime changes

	def test_two_reads_in_the_window_stat_once(self):
		self.s.list()
		Store(self.d).get("A-1")  # another Store (the web server makes one per request) shares the pass
		self.assertEqual(self.scans, 1)

	def test_a_write_from_this_process_is_seen_by_the_next_read(self):
		self.s.list()
		Store(self.d).update("A-1", {"title": "bbbb"})
		self.assertEqual(self.s.get("A-1")["title"], "bbbb")
		self.s.create({"title": "new"}, tid="A-2")
		self.assertEqual([t["id"] for t in self.s.list()], ["A-1", "A-2"])
		self.s.delete("A-2")
		self.assertEqual([t["id"] for t in self.s.list()], ["A-1"])

	def test_the_lock_never_reuses_a_stale_pass(self):
		self.s.list()
		self.hand_edit("bbbb", in_place=True)
		self.assertEqual(self.s.get("A-1")["title"].strip(), "aaaa")  # outside the lock: the window's pass (what the 1 s web reuse allows)
		self.assertEqual(self.s._locked(lambda: self.s.get("A-1")["title"]).strip(), "bbbb")
		self.hand_edit("cccc", in_place=True)
		claimed = self.s.claim("w", tid="A-1")  # claim reads under the lock
		self.assertEqual(claimed["title"].strip(), "cccc")

	def test_after_the_window_a_hand_edit_is_seen(self):
		self.s.list()
		self.hand_edit("bbbb", in_place=True)
		with mock.patch.object(files, "SCAN_REUSE_S", 0.0):
			self.assertEqual(self.s.get("A-1")["title"].strip(), "bbbb")

	def test_a_replaced_file_is_seen_by_the_next_read(self):
		self.s.list()
		self.hand_edit("bbbb")  # os.replace changes the folder's mtime: no window to wait out
		self.assertEqual(self.s.get("A-1")["title"].strip(), "bbbb")

	def test_another_process_write_is_seen_by_the_next_read(self):
		# PR #73's CI smoke: the CLI wrote, the page refreshed at once and the web server served its last pass
		self.s.list()
		code = "import sys; from unblockd.store import Store; Store(sys.argv[1]).update('A-1', {'title': 'from cli'})"
		subprocess.run([sys.executable, "-c", code, str(self.d)], cwd=ROOT, check=True)
		self.assertEqual(self.s.get("A-1")["title"], "from cli")
		code = "import sys; from unblockd.store import Store; Store(sys.argv[1]).create({'title': 'new one'}, tid='A-2')"
		subprocess.run([sys.executable, "-c", code, str(self.d)], cwd=ROOT, check=True)
		self.assertEqual([t["id"] for t in self.s.list()], ["A-1", "A-2"])


if __name__ == "__main__":
	unittest.main()
