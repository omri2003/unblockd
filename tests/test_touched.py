"""CORE-47: touches vs the real edits. complete(touched=) warns on paths outside touches and in other doing tickets' touches;
a claim lists the doing tickets whose touches meet its own."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import Store

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "pillars": [{"key": "G"}], "agents": [{"name": "backend", "pillars": ["G"]}]}


class TouchedTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(self.dir)
		self.s.create({"title": "combat", "pillar": "G", "touches": ["scripts/combat/"]}, tid="G-1")
		self.s.create({"title": "tuning", "pillar": "G", "touches": ["scripts/tuning.gd"]}, tid="G-2")
		self.s.create({"title": "hud", "pillar": "G", "touches": ["ui/hud.gd", "scripts/tuning.gd"]}, tid="G-3")

	def test_complete_warns_on_extras_and_on_doing_overlap(self):
		self.s.claim("backend-1", tid="G-1")
		self.s.claim("backend-2", tid="G-2")
		out = self.s.complete("G-1", "backend-1", "parry", touched=["scripts/combat/parry.gd", "scripts/tuning.gd", "scripts/enemy.gd"])
		self.assertEqual(out["result"]["touched"], ["scripts/combat/parry.gd", "scripts/tuning.gd", "scripts/enemy.gd"])
		self.assertEqual(out["result"]["outside"], ["scripts/tuning.gd", "scripts/enemy.gd"])  # parry.gd is inside scripts/combat/
		w = out["warnings"]
		self.assertIn("you edited scripts/tuning.gd, scripts/enemy.gd outside G-1's touches", w[0])
		self.assertIn("scripts/tuning.gd also in G-2's touches (backend-2 doing it)", w[1])
		self.assertEqual(len(w), 2)  # G-3 is todo: not in anyone's way yet
		self.assertEqual(out["status"], "done")  # a warning, never a refusal

	def test_inside_touches_and_no_touched_say_nothing(self):
		self.s.claim("backend-1", tid="G-1")
		self.assertNotIn("warnings", self.s.complete("G-1", "backend-1", "s", files=["anything.py"]))  # files alone: no check, as before
		self.s.claim("backend-1", tid="G-2")
		out = self.s.complete("G-2", "backend-1", "s", touched=["scripts/tuning.gd"])
		self.assertNotIn("warnings", out)
		self.assertNotIn("outside", out["result"])

	def test_claim_lists_doing_tickets_it_overlaps(self):
		self.s.claim("backend-1", tid="G-2")
		self.s.update("G-2", {"status": "todo"})  # claim dropped
		self.s.update("G-2", {"status": "doing"})  # a hand move: doing with nobody holding it
		t = self.s.claim("backend-2", tid="G-3")  # no live claim in the way, so it is claimable
		self.assertEqual(t["overlapping"], [{"id": "G-2", "title": "tuning", "agent": None, "files": ["scripts/tuning.gd"], "branch": None}])
		self.assertNotIn("overlapping", self.s.claim("backend-3", tid="G-1"))

	def test_claim_on_another_branch_lists_the_holder(self):
		self.s.claim("backend-1", tid="G-2", branch="a")
		t = self.s.claim("backend-2", tid="G-3", branch="b")
		self.assertEqual(t["overlapping"][0] | {}, {"id": "G-2", "title": "tuning", "agent": "backend-1", "files": ["scripts/tuning.gd"], "branch": "a"})

	def test_front_doors(self):
		self.s.claim("backend-1", tid="G-1")
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(io.StringIO()) as out:
			self.assertEqual(cli.main(["--dir", str(self.dir), "complete", "G-1", "--agent", "backend-1", "--summary", "s", "--touched", "x.gd"]), 0)
		self.assertIn("outside G-1's touches", json.loads(out.getvalue())["warnings"][0])
		try:
			from unblockd import mcp_server
		except ImportError:
			self.skipTest("mcp not installed")
		old, mcp_server.BOARDS = mcp_server.BOARDS, Boards(dirs=[str(self.dir)])
		try:
			mcp_server.claim_ticket("G-2", "backend-1")
			r = mcp_server.complete("G-2", "backend-1", "s", touched=["y.gd"])
		finally:
			mcp_server.BOARDS = old
		self.assertIn("outside G-2's touches", r["warnings"][0])


if __name__ == "__main__":
	unittest.main()
