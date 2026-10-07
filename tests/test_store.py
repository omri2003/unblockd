"""python -m unittest  (stdlib only)"""
import json
import tempfile
import unittest
from pathlib import Path

from unblockd.store import NotFound, Store, TicketError


class StoreTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		legacy = Path(self.tmp.name) / "OLD.md"
		legacy.write_text("old:BUG-9 and BUG-12 were 2D bugs", encoding="utf-8")
		self.s = Store(Path(self.tmp.name) / "tickets", [str(legacy)])

	def tearDown(self):
		self.tmp.cleanup()

	def test_crud_and_next_id_continues_legacy_numbering(self):
		t = self.s.create({"title": "Search ignores accents", "priority": "P1"}, prefix="BUG")
		self.assertEqual(t["id"], "BUG-13")  # legacy file had BUG-12
		self.assertEqual(self.s.update("BUG-13", {"status": "doing"})["status"], "doing")
		self.assertEqual(len(self.s.list(status="doing")), 1)
		self.s.delete("BUG-13")
		with self.assertRaises(NotFound):
			self.s.get("BUG-13")

	def test_doing_refused_while_blockers_are_open(self):
		self.s.create({"title": "base"}, tid="UI-1")
		self.s.create({"title": "top", "blocked_by": ["UI-1"]}, tid="UI-2")
		with self.assertRaisesRegex(TicketError, "UI-2 is blocked by UI-1 .still open"):
			self.s.update("UI-2", {"status": "doing"})
		self.s.update("UI-1", {"status": "done"})
		self.assertEqual(self.s.update("UI-2", {"status": "doing"})["status"], "doing")
		self.s.create({"title": "late blocker"}, tid="UI-3")
		self.s.update("UI-2", {"blocked_by": ["UI-3"]})  # already doing: not a transition
		self.assertEqual(self.s.update("UI-2", {"title": "renamed", "status": "doing"})["title"], "renamed")

	def test_wip_limit_validated(self):
		(Path(self.tmp.name) / "tickets" / "unblockd.json").write_text('{"wip_limit": 0}')
		with self.assertRaisesRegex(TicketError, "wip_limit"):
			Store(Path(self.tmp.name) / "tickets")

	def test_open_blocker_makes_todo_pending_and_done_clears_it(self):
		self.s.create({"title": "Cultivation"}, tid="PORT-1")
		self.s.create({"title": "Essence", "blocked_by": ["PORT-1"]}, tid="EMP-12")
		e = self.s.get("EMP-12")
		self.assertEqual((e["effective_status"], e["open_blockers"]), ("pending", ["PORT-1"]))
		self.assertEqual(self.s.get("PORT-1")["blocks"], ["EMP-12"])
		self.s.update("PORT-1", {"status": "done"})
		self.assertEqual(self.s.get("EMP-12")["effective_status"], "todo")

	def test_rejects_blocking_loops(self):
		self.s.create({"title": "A"}, tid="CMB-1")
		self.s.create({"title": "B", "blocked_by": ["CMB-1"]}, tid="CMB-2")
		self.s.create({"title": "C", "blocked_by": ["CMB-2"]}, tid="CMB-3")
		with self.assertRaisesRegex(TicketError, "CMB-1 → CMB-3 → CMB-2 → CMB-1|loop"):
			self.s.update("CMB-1", {"blocked_by": ["CMB-3"]})
		with self.assertRaisesRegex(TicketError, "itself"):
			self.s.update("CMB-1", {"blocked_by": ["CMB-1"]})
		self.assertEqual(self.s.get("CMB-1")["blocked_by"], [])  # refused change left nothing behind
		self.s.update("CMB-3", {"blocked_by": ["CMB-1", "CMB-2"]})  # diamond, not a loop: fine

	def test_rejects_bad_input(self):
		for bad in ("../etc/passwd", "bug-1", "BUG-1/x", ""):
			with self.assertRaises(TicketError):
				self.s.get(bad)
		with self.assertRaises(TicketError):
			self.s.create({"title": "x", "status": "maybe"}, tid="BUG-1")
		with self.assertRaises(TicketError):
			self.s.create({"title": "x", "colour": "red"}, tid="BUG-1")
		with self.assertRaises(TicketError):
			self.s.create({"title": "   "}, tid="BUG-1")
		self.s.create({"title": "x"}, tid="BUG-1")
		with self.assertRaises(TicketError):
			self.s.create({"title": "dup"}, tid="BUG-1")


class DerivedTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name)
		self.s = Store(self.dir)

	def tearDown(self):
		self.tmp.cleanup()

	def test_unblocks_is_transitive_and_ignores_done(self):
		self.s.create({"title": "root"}, tid="A-1")
		self.s.create({"title": "b", "blocked_by": ["A-1"]}, tid="A-2")
		self.s.create({"title": "c", "blocked_by": ["A-2"]}, tid="A-3")
		self.s.create({"title": "d", "blocked_by": ["A-1", "A-3"]}, tid="A-4")  # diamond: counted once
		self.assertEqual(self.s.get("A-1")["unblocks"], 3)
		self.assertEqual(self.s.get("A-4")["unblocks"], 0)
		self.s.update("A-3", {"status": "done"})
		self.assertEqual(self.s.get("A-1")["unblocks"], 2)  # A-2 and A-4 still open, A-3 done

	def test_missing_blockers(self):
		(self.dir / "B-2.json").write_text('{"id":"B-2","title":"x","status":"todo","blocked_by":["B-99"],"updated":"2026-01-01"}', encoding="utf-8")
		t = self.s.get("B-2")
		self.assertEqual(t["missing_blockers"], ["B-99"])
		self.assertEqual(t["open_blockers"], [])

	def test_stale_days_only_for_doing(self):
		from datetime import date, timedelta
		old = (date.today() - timedelta(days=9)).isoformat()
		for tid, status in (("C-1", "doing"), ("C-2", "todo")):
			(self.dir / f"{tid}.json").write_text(
				f'{{"id":"{tid}","title":"x","status":"{status}","blocked_by":[],"updated":"{old}"}}', encoding="utf-8")
		self.assertEqual(self.s.get("C-1")["stale_days"], 9)
		self.assertIsNone(self.s.get("C-2")["stale_days"])
		self.s.update("C-1", {"body": "touched"})
		self.assertEqual(self.s.get("C-1")["stale_days"], 0)

	def test_next_ready_ranks_by_priority_times_unblocks(self):
		self.s.create({"title": "p1 alone", "priority": "P1"}, tid="N-1")           # 4 * 1 = 4
		self.s.create({"title": "p2 unlocks 3", "priority": "P2"}, tid="N-2")       # 2 * 4 = 8
		for i in (3, 4, 5):
			self.s.create({"title": f"waits {i}", "blocked_by": ["N-2"]}, tid=f"N-{i}")
		self.s.create({"title": "doing", "status": "doing", "priority": "P0"}, tid="N-6")  # not ready
		ids = [t["id"] for t in self.s.next_ready()]
		self.assertEqual(ids[:2], ["N-2", "N-1"])
		self.assertNotIn("N-6", ids)
		self.assertNotIn("N-3", ids)  # blocked
		self.assertEqual(self.s.next_ready(limit=1)[0]["score"], 8)
		self.assertEqual(self.s.next_ready(owner="Me"), [])


class CliTest(unittest.TestCase):
	def test_create_note_update_list(self):
		import io, contextlib, json
		from unblockd import cli
		with tempfile.TemporaryDirectory() as d:
			def call(*a):
				buf = io.StringIO()
				with contextlib.redirect_stdout(buf):
					code = cli.main(["--dir", d, *a])
				return code, (json.loads(buf.getvalue()) if code == 0 else None)
			code, t = call("create", "--prefix", "BUG", "--title", "Probe", "--json", '{"priority": "P2"}')
			self.assertEqual((code, t["id"], t["priority"]), (0, "BUG-1", "P2"))
			code, t = call("note", "BUG-1", "first finding")
			self.assertIn("first finding", t["body"])
			code, t = call("update", "BUG-1", "--json", '{"status": "done"}')
			self.assertEqual(t["status"], "done")
			code, ls = call("list", "--status", "done")
			self.assertEqual([x["id"] for x in ls], ["BUG-1"])
			with contextlib.redirect_stderr(io.StringIO()):
				self.assertEqual(call("update", "BUG-1", "--json", '{"status": "maybe"}')[0], 1)



class ConfigTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()

	def tearDown(self):
		self.tmp.cleanup()

	def _store(self, cfg):
		(self.dir / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
		return Store(self.dir)

	def test_defaults_without_a_config_file(self):
		s = Store(self.dir)
		self.assertEqual(s.create({"title": "x"}, prefix="BUG")["owner"], "Claude")  # first agent owner
		self.assertEqual(s.config["pillars"], [])
		s.update("BUG-1", {"pillar": "ANYTHING"})  # no pillars configured: free text

	def test_owners_and_pillars_are_validated(self):
		s = self._store({"owners": [{"name": "Dana", "kind": "human"}, {"name": "Bot", "kind": "agent"}],
		                 "pillars": [{"key": "UI", "name": "Interface"}]})
		t = s.create({"title": "x", "pillar": "UI"}, prefix="UI")
		self.assertEqual(t["owner"], "Bot")
		with self.assertRaisesRegex(TicketError, "one of Dana, Bot"):
			s.update(t["id"], {"owner": "Claude"})
		with self.assertRaisesRegex(TicketError, "one of UI"):
			s.update(t["id"], {"pillar": "ART"})
		s.update(t["id"], {"pillar": None})

	def test_config_legacy_files_and_bad_config(self):
		(self.dir / "OLD.md").write_text("BUG-41 was here", encoding="utf-8")
		self.assertEqual(self._store({"legacy_files": ["OLD.md"]}).next_id("BUG"), "BUG-42")
		with self.assertRaisesRegex(TicketError, "unknown key"):
			self._store({"titel": "typo"})
		(self.dir / "unblockd.json").write_text("{nope", encoding="utf-8")
		with self.assertRaisesRegex(TicketError, "not valid JSON"):
			Store(self.dir)


if __name__ == "__main__":
	unittest.main()
