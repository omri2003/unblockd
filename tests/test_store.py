"""python -m unittest  (stdlib only)"""
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd.store import Conflict, NotFound, Store, TicketError


def _make(args):
	d, n = args
	s = Store(Path(d))
	return [s.create({"title": "x"}, prefix="R")["id"] for _ in range(n)]


class StoreTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		legacy = Path(self.tmp.name) / "OLD.md"
		legacy.write_text("old:BUG-9 and BUG-12 were 2D bugs", encoding="utf-8")
		self.s = Store(Path(self.tmp.name) / "tickets", [str(legacy)])

	def tearDown(self):
		self.tmp.cleanup()

	def test_parent_groups_tickets_with_progress(self):
		e = self.s.create({"title": "epic"}, tid="E-1")
		self.s.create({"title": "a", "parent": "E-1"}, tid="E-2")
		self.s.create({"title": "b", "parent": "E-1", "status": "done"}, tid="E-3")
		e = self.s.get("E-1")
		self.assertEqual((e["children"], e["progress"], e["parent"]), (["E-2", "E-3"], {"done": 1, "total": 2}, None))
		self.assertEqual([t["id"] for t in self.s.list(parent="E-1")], ["E-2", "E-3"])
		self.assertEqual(e["effective_status"], "todo")  # a group is not a blocker
		self.assertEqual(self.s.get("E-2")["children"], [])
		self.s.update("E-2", {"parent": None})
		self.assertEqual(self.s.get("E-1")["progress"], {"done": 1, "total": 1})

	def test_rev_counts_writes_and_guards_updates(self):
		t = self.s.create({"title": "x"}, tid="R-1")
		self.assertEqual(t["rev"], 1)
		self.assertEqual(self.s.update("R-1", {"title": "y"})["rev"], 2)
		self.assertEqual(self.s.update("R-1", {"title": "z"}, expected_rev=2)["rev"], 3)  # fresh rev: ok
		with self.assertRaises(Conflict) as cm:
			self.s.update("R-1", {"title": "stale"}, expected_rev=2)
		self.assertIn("reload", str(cm.exception))
		self.assertIsInstance(cm.exception, TicketError)
		self.assertEqual(self.s.get("R-1")["title"], "z")  # the stale write changed nothing
		with self.assertRaises(TicketError):
			self.s.update("R-1", {"rev": 99})  # read-only
		p = Path(self.tmp.name) / "tickets" / "R-1.json"
		d = json.loads(p.read_text())
		del d["rev"]
		p.write_text(json.dumps(d))  # an old file without rev reads as 0
		self.assertEqual(self.s.get("R-1")["rev"], 0)
		self.assertEqual(self.s.update("R-1", {"title": "w"}, expected_rev=0)["rev"], 1)

	def test_claim_and_complete_bump_rev_but_heartbeat_does_not(self):
		self.s.create({"title": "x"}, tid="R-2")
		self.s.claim("a-1", tid="R-2")
		r = self.s.get("R-2")["rev"]
		self.s.heartbeat("R-2", "a-1", "still here")
		self.assertEqual(self.s.get("R-2")["rev"], r)  # heartbeats live in claims.json
		self.s.complete("R-2", "a-1", "done it")
		self.assertGreater(self.s.get("R-2")["rev"], r)
		with self.assertRaises(Conflict):  # the human's form was opened before complete() landed
			self.s.update("R-2", {"body": "my edit"}, expected_rev=r)

	def test_split_sets_parent_on_the_parts(self):
		self.s.create({"title": "big"}, tid="E-1")
		r = self.s.split("E-1", ["a", "b"])
		self.assertEqual([k["parent"] for k in r["children"]], ["E-1", "E-1"])
		self.assertEqual(r["parent"]["progress"], {"done": 0, "total": 2})

	def test_split_twice_refused_until_children_are_done(self):
		self.s.create({"title": "big"}, tid="E-1")
		self.s.split("E-1", ["a", "b"])
		with self.assertRaisesRegex(TicketError, r"E-1 is already split into E-2, E-3: work those, or split one of them"):
			self.s.split("E-1", ["c", "d"])
		self.s.update("E-2", {"status": "done"})
		self.s.update("E-3", {"status": "done"})
		self.assertEqual(len(self.s.split("E-1", ["c", "d"])["children"]), 2)

	def test_created_and_deleted_log_the_actor(self):
		self.s.create({"title": "x"}, tid="E-1", agent="backend-1")
		self.s.create({"title": "y"}, tid="E-2")
		self.s.delete("E-1", agent="backend-1")
		ev = {(e["event"], e["ticket"]): e["agent"] for e in self.s.activity(limit=10)}
		self.assertEqual((ev["created", "E-1"], ev["created", "E-2"], ev["deleted", "E-1"]), ("backend-1", None, "backend-1"))

	def test_delete_cleans_up_and_reports_who_points_at_it(self):
		self.s.create({"title": "gone"}, tid="E-1")
		self.s.create({"title": "waits", "blocked_by": ["E-1"]}, tid="E-2")
		self.s.create({"title": "part", "parent": "E-1"}, tid="E-3")
		self.s.create({"title": "other"}, tid="E-4")
		self.s.claim("w-1", tid="E-1")
		files = self.s.dir / "files" / "E-1"
		files.mkdir(parents=True)
		(files / "spec.md").write_text("x", encoding="utf-8")
		r = self.s.delete("E-1")
		self.assertEqual((r["id"], r["referenced_by"]), ("E-1", ["E-2", "E-3"]))
		self.assertFalse(files.exists())
		self.assertEqual(self.s.agents_now()["working"], [])  # claim gone with it
		self.assertEqual([e["event"] for e in self.s.activity("E-1")[:2]], ["deleted", "dropped"])
		self.assertEqual(self.s.get("E-2")["missing_blockers"], ["E-1"])  # not edited
		self.assertEqual(self.s.delete("E-4")["referenced_by"], [])
		with self.assertRaises(NotFound):
			self.s.delete("E-1")

	def test_activity_rotates_and_reads_across_the_old_file(self):
		self.s.create({"title": "x"}, tid="E-1")
		with mock.patch("unblockd.store.ACTIVITY_MAX", 300):
			for i in range(12):
				self.s._log("note", "E-1", "a", f"n{i}")
		rt = self.s.dir / ".unblockd"
		self.assertTrue((rt / "activity.1.jsonl").exists())
		self.assertLess((rt / "activity.jsonl").stat().st_size, 600)  # bounded
		notes = [e["note"] for e in self.s.activity("E-1", limit=100)]
		self.assertEqual(notes[:3], ["n11", "n10", "n9"])  # newest first, continuing into .1
		nums = [int(n[1:]) for n in notes if n.startswith("n")]
		self.assertEqual(nums, sorted(nums, reverse=True))
		self.assertGreater(len(nums), 3)  # more than the current file holds (cap fits ~3 lines)
		self.assertEqual(len(self.s.activity(limit=2)), 2)
		self.assertEqual(len(self.s.activity(since="2999-01-01T00:00:00Z")), 0)
		self.assertGreater(len(self.s.activity(since="2000-01-01T00:00:00Z", limit=100)), 3)

	def test_parent_refusals(self):
		self.s.create({"title": "a"}, tid="E-1")
		self.s.create({"title": "b", "parent": "E-1"}, tid="E-2")
		self.s.create({"title": "c", "parent": "E-2"}, tid="E-3")
		for tid, par in (("E-1", "E-1"), ("E-1", "E-3"), ("E-1", "E-2"), ("E-3", "NOPE-9")):
			with self.assertRaises(TicketError, msg=f"{tid} -> {par}"):
				self.s.update(tid, {"parent": par})
		with self.assertRaises(TicketError):
			self.s.create({"title": "x", "parent": "NOPE-9"}, tid="E-4")
		self.assertFalse(self.s.dir.joinpath("E-4.json").exists())

	def test_parallel_creates_lose_nothing(self):
		d = Path(self.tmp.name) / "tickets"
		with multiprocessing.Pool(6) as pool:
			ids = sum(pool.map(_make, [(str(d), 10)] * 6), [])
		self.assertEqual(len(ids), len(set(ids)), "same id handed out twice")
		self.assertEqual(len(list(d.glob("R-*.json"))), 60)

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

	def test_done_window_days_default_and_validated(self):
		self.assertEqual(self.s.config["done_window_days"], 7)
		cfg = Path(self.tmp.name) / "tickets" / "unblockd.json"
		cfg.write_text('{"done_window_days": 30}')
		self.assertEqual(Store(cfg.parent).config["done_window_days"], 30)
		for bad in ("0", "-1", "1.5", '"7"', "true"):
			cfg.write_text('{"done_window_days": %s}' % bad)
			with self.assertRaisesRegex(TicketError, "done_window_days"):
				Store(cfg.parent)

	def test_done_at_is_set_on_the_transition_to_done_and_read_only(self):
		self.s.create({"title": "a"}, tid="D-1")
		self.assertEqual(self.s.get("D-1")["done_at"], "")
		self.assertTrue(self.s.update("D-1", {"status": "done"})["done_at"].endswith("+00:00"))
		self.assertEqual(self.s.update("D-1", {"status": "todo"})["done_at"], "")
		self.assertTrue(self.s.create({"title": "b", "status": "done"}, tid="D-2")["done_at"])
		with self.assertRaisesRegex(TicketError, "done_at"):
			self.s.update("D-1", {"done_at": "2020-01-01T00:00:00+00:00"})
		self.s.create({"title": "c"}, tid="D-3")
		self.s.claim("worker", "D-3")
		self.assertTrue(self.s.complete("D-3", "worker", "did it")["done_at"])

	def test_open_blocker_makes_todo_pending_and_done_clears_it(self):
		self.s.create({"title": "Cultivation"}, tid="PORT-1")
		self.s.create({"title": "Essence", "blocked_by": ["PORT-1"]}, tid="EMP-12")
		e = self.s.get("EMP-12")
		self.assertEqual((e["effective_status"], e["open_blockers"]), ("pending", ["PORT-1"]))
		self.assertEqual(self.s.get("PORT-1")["blocks"], ["EMP-12"])
		self.s.update("PORT-1", {"status": "done"})
		self.assertEqual(self.s.get("EMP-12")["effective_status"], "todo")

	def test_pending_is_derived_only(self):
		with self.assertRaisesRegex(TicketError, "derived.*ask_human"):
			self.s.create({"title": "x", "status": "pending"}, tid="P-1")
		self.s.create({"title": "y"}, tid="P-2")
		with self.assertRaisesRegex(TicketError, "derived"):
			self.s.update("P-2", {"status": "pending"})
		path = self.s.dir / "P-2.json"  # an old file that stored pending reads as todo, and is claimable once waiting_on is clear
		path.write_text(json.dumps({**json.loads(path.read_text()), "status": "pending"}))
		self.assertEqual((self.s.get("P-2")["status"], self.s.get("P-2")["effective_status"]), ("todo", "todo"))
		self.assertEqual(self.s.claim("w-1")["id"], "P-2")

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

	def test_unknown_blocker_refused_but_old_dangling_tolerated(self):
		self.s.create({"title": "a"}, tid="B-1")
		with self.assertRaisesRegex(TicketError, "no ticket B-99 on this board"):
			self.s.create({"title": "x", "blocked_by": ["B-1", "B-99"]}, tid="B-2")
		self.assertFalse((self.dir / "B-2.json").exists())
		self.s.create({"title": "x", "blocked_by": ["B-1"]}, tid="B-2")
		with self.assertRaisesRegex(TicketError, "B-98"):
			self.s.update("B-2", {"blocked_by": ["B-1", "B-98"]})
		self.s.delete("B-1")  # B-2's blocker is gone: still editable, id kept
		self.s.update("B-2", {"blocked_by": ["B-1"], "title": "y"})
		with self.assertRaisesRegex(TicketError, "B-97"):
			self.s.update("B-2", {"blocked_by": ["B-1", "B-97"]})
		self.s.split("B-2", ["p", "q"])  # children inherit the dangling id without being refused

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


class CapabilityFieldsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		(Path(self.tmp.name) / "sk.md").write_text("skill", encoding="utf-8")
		d = Path(self.tmp.name) / "tickets"
		d.mkdir()
		self.cfg = {"skills": {"a": {"path": "sk.md"}, "b": {"path": "sk.md"}}, "mcp": {"db": {"hint": "h"}}}
		(d / "unblockd.json").write_text(json.dumps(self.cfg), encoding="utf-8")
		self.d, self.s = d, Store(d)

	def tearDown(self):
		self.tmp.cleanup()

	def test_names_are_checked_against_the_registry(self):
		t = self.s.create({"title": "x", "skills": ["a", "a"], "mcp": ["db"]}, prefix="BUG")
		self.assertEqual((t["skills"], t["mcp"]), (["a"], ["db"]))
		with self.assertRaisesRegex(TicketError, "unknown skills zzz: this board's skills are a, b"):
			self.s.update("BUG-1", {"skills": ["zzz"]})
		with self.assertRaisesRegex(TicketError, "unknown mcp x"):
			self.s.create({"title": "y", "mcp": ["x"]}, prefix="BUG")
		self.assertEqual(self.s.create({"title": "plain"}, prefix="BUG")["skills"], [])  # old tickets / no selection

	def test_removed_registry_entry_keeps_the_ticket_editable_and_split_copies(self):
		self.s.create({"title": "big", "skills": ["a"], "mcp": ["db"]}, prefix="BUG")
		(self.d / "unblockd.json").write_text(json.dumps({"skills": {"b": {"path": "sk.md"}}}), encoding="utf-8")
		s = Store(self.d)  # "a" and "db" are gone from the registry
		s.update("BUG-1", {"skills": ["a", "b"], "title": "bigger"})
		kids = s.split("BUG-1", ["one", "two"])["children"]
		self.assertEqual([(k["skills"], k["mcp"]) for k in kids], [(["a", "b"], ["db"])] * 2)


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
			self.assertEqual(t["notes"][-1]["text"], "first finding")
			code, t = call("update", "BUG-1", "--json", '{"status": "done"}')
			self.assertEqual(t["status"], "done")
			code, ls = call("list", "--status", "done")
			self.assertEqual([x["id"] for x in ls], ["BUG-1"])
			with contextlib.redirect_stderr(io.StringIO()):
				self.assertEqual(call("update", "BUG-1", "--json", '{"status": "maybe"}')[0], 1)

	def test_skill_and_mcp_flags(self):
		import io, contextlib
		from unblockd import cli
		with tempfile.TemporaryDirectory() as d:
			(Path(d) / "sk.md").write_text("s", encoding="utf-8")
			(Path(d) / "tickets").mkdir()
			(Path(d) / "tickets" / "unblockd.json").write_text(json.dumps({"skills": {"a": {"path": "sk.md"}, "b": {"path": "sk.md"}}, "mcp": {"db": {"hint": "h"}}}))
			def call(*a):
				buf = io.StringIO()
				with contextlib.redirect_stdout(buf):
					self.assertEqual(cli.main(["--dir", str(Path(d) / "tickets"), *a]), 0)
				return json.loads(buf.getvalue())
			t = call("create", "--prefix", "BUG", "--title", "x", "--skill", "a", "--skill", "b", "--mcp", "db")
			self.assertEqual((t["skills"], t["mcp"]), (["a", "b"], ["db"]))
			self.assertEqual(call("update", "BUG-1", "--skill", "b")["skills"], ["b"])  # sets, does not add



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

	def test_agent_profile_keys_and_owner_kind_are_validated(self):
		owners = [{"name": "Omri", "kind": "human"}]
		with self.assertRaisesRegex(TicketError, "agent backend: unknown key.*effort.*Allowed: docs, instructions, mcp, model, name, pillars, skills, tags"):
			self._store({"owners": owners, "agents": [{"name": "backend", "effort": "x"}]})
		for kind in ("Human", None):
			with self.assertRaisesRegex(TicketError, "owner Omri: kind must be"):
				self._store({"owners": [{"name": "Omri", "kind": kind}]})
		self._store({"owners": owners, "agents": [{"name": "backend", "model": "m", "instructions": "i"}]})

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
