"""API-14: errors agents trip on. One error lists every refused field with its rule (no echo of the input), LIMITS match the
validators, add_note takes "progress", tickets carry each blocker's status, board_info has the limits."""
import json
import tempfile
import unittest
from pathlib import Path

from unblockd import store as st
from unblockd.store import Store, TicketError


class ToolDocsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "tickets"
		d.mkdir()
		(d / "unblockd.json").write_text(json.dumps({"owners": [{"name": "Omri", "kind": "human"}]}), encoding="utf-8")
		self.s = Store(d)
		self.t = self.s.create({"title": "parry"}, prefix="CMB")

	def tearDown(self):
		self.tmp.cleanup()

	def test_limits_match_the_validators_at_the_edge(self):
		for k, lim in st.LIMITS.items():
			ok = st.FIELDS[k][1]
			if lim.get("items") and k != "needs":
				item = "A-1" if k == "blocked_by" else "x"
				self.assertTrue(ok([item] * lim["items"]), k)
				self.assertFalse(ok([item] * (lim["items"] + 1)), k)
			if lim.get("chars"):
				if "items" in lim:
					self.assertTrue(ok(["a" * lim["chars"]]), k)
					self.assertFalse(ok(["a" * (lim["chars"] + 1)]), k)
				else:
					self.assertTrue(ok("a" * lim["chars"]), k)
					self.assertFalse(ok("a" * (lim["chars"] + 1)), k)

	def test_one_error_lists_every_problem_without_the_input(self):
		secret = "x" * 251
		with self.assertRaises(TicketError) as e:
			self.s.update(self.t["id"], {"done_when": ["fine", secret], "priority": "high", "tags": ["Has Space"], "touches": "src/a.py"})
		msg = str(e.exception)
		self.assertTrue(msg.startswith("4 problems: "), msg)
		for part in ("invalid done_when[1]: 251 chars, max 200", "invalid priority: one of P0, P1, P2, P3 or null",
		             "invalid tags[0]: lowercase", 'invalid touches: a list of strings, e.g. ["..."]', "(limits: board_info)"):
			self.assertIn(part, msg)
		self.assertNotIn(secret, msg)
		self.assertNotIn("high", msg)
		self.assertEqual(self.s.get(self.t["id"])["priority"], None)  # nothing written
		with self.assertRaisesRegex(TicketError, r"^invalid title: 201 chars, max 200$"):  # one problem: no count
			self.s.update(self.t["id"], {"title": "a" * 201})
		with self.assertRaisesRegex(TicketError, "invalid owner: one of Omri"):
			self.s.create({"title": "y", "owner": "nobody"}, prefix="CMB")

	def test_progress_is_a_note(self):
		t = self.s.add_note(self.t["id"], "backend-1", "halfway", kind="progress")
		self.assertEqual(t["notes"][-1]["kind"], "note")
		with self.assertRaisesRegex(TicketError, "note, decision, finding, warning \\(progress = note\\)"):
			self.s.add_note(self.t["id"], "backend-1", "x", kind="status")

	def test_blockers_carry_their_status(self):
		a = self.s.create({"title": "a"}, prefix="CMB")
		b = self.s.create({"title": "b"}, prefix="CMB")
		self.s.update(b["id"], {"status": "done"})
		t = self.s.update(self.t["id"], {"blocked_by": [a["id"], b["id"]]})
		got = self.s.with_blockers(t)["blockers"]
		self.assertEqual([(x["id"], x["effective_status"]) for x in got], [(a["id"], "todo"), (b["id"], "done")])
		self.s.delete(a["id"])
		self.assertEqual(self.s.with_blockers(self.s.get(self.t["id"]))["blockers"][0]["effective_status"], "missing")

	def test_board_info_has_the_limits(self):
		try:
			from unblockd import mcp_server
		except ImportError:
			self.skipTest("mcp not installed")
		from unblockd.boards import Boards
		old = mcp_server.BOARDS
		mcp_server.BOARDS = Boards(dirs=[str(self.s.dir)])
		try:
			info = mcp_server.board_info()
		finally:
			mcp_server.BOARDS = old
		self.assertEqual(info["limits"]["fields"]["done_when"], {"items": 20, "chars": 200})


if __name__ == "__main__":
	unittest.main()
