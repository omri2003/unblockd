"""AI-36: a note on a ticket you hold reaches you in the next heartbeat reply, once (store, MCP, REST, CLI)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd import cli
from unblockd.store import Store

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}], "default_owner": "Claude",
       "pillars": [{"key": "COMBAT"}], "agents": [{"name": "backend", "pillars": ["COMBAT"]}]}


class InboxTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(self.dir)
		self.s.create({"title": "hud", "pillar": "COMBAT", "touches": ["scripts/hud.gd"]}, tid="CMB-1")
		self.s.create({"title": "parry", "pillar": "COMBAT", "touches": ["scripts/parry.gd"]}, tid="CMB-2")
		self.s.add_note("CMB-1", "Omri", "old note, before the claim")
		p = self.dir / "CMB-1.json"  # back-date it: notes before the claim are not news
		t = json.loads(p.read_text(encoding="utf-8"))
		t["notes"][0]["at"] = "2020-01-01T00:00:00+00:00"
		p.write_text(json.dumps(t), encoding="utf-8")
		self.s.claim("backend-1", tid="CMB-1")
		self.s.claim("backend-2", tid="CMB-2")

	def tearDown(self):
		self.tmp.cleanup()

	def test_a_request_reaches_the_holder_within_one_heartbeat(self):
		self.assertIsNone(self.s.inbox("backend-1"))
		self.s.add_note("CMB-1", "backend-2", "CMB-2 needs 3 lines in scripts/hud.gd: emit parried(dmg) after _on_hit" + chr(10) + "line 2 detail")
		self.s.add_note("CMB-1", "backend-1", "my own progress")  # own notes never count, and do not hide the others
		self.s.add_note("CMB-1", "Omri", "also check the font")
		box = self.s.inbox("backend-1", mark=True)
		self.assertEqual(box["count"], 2)
		self.assertEqual([(n["ticket"], n["n"], n["by"], n["text"]) for n in box["notes"]],
		                 [("CMB-1", 2, "backend-2", "CMB-2 needs 3 lines in scripts/hud.gd: emit parried(dmg) after _on_hit"),
		                  ("CMB-1", 4, "Omri", "also check the font")])
		self.assertIsNone(self.s.inbox("backend-2"))  # CMB-2's holder got nothing
		self.assertIsNone(self.s.inbox("backend-1"))  # shown once
		self.s.add_note("CMB-1", "Omri", "one more")
		self.assertEqual([n["n"] for n in self.s.inbox("backend-1")["notes"]], [5])

	def test_mcp_and_cli_heartbeat_replies(self):
		self.s.add_note("CMB-1", "backend-2", "first line" + chr(10) + "second line")
		out = io.StringIO()
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(out):
			self.assertEqual(cli.main(["--dir", str(self.dir), "heartbeat", "CMB-1", "--agent", "backend-1"]), 0)
		self.assertEqual(json.loads(out.getvalue())["inbox"]["notes"][0]["text"], "first line")
		self.s.add_note("CMB-1", "Omri", "for the MCP")
		try:
			from unblockd import mcp_server
		except ImportError:
			self.skipTest("mcp not installed")
		from unblockd.boards import Boards
		old, mcp_server.BOARDS = mcp_server.BOARDS, Boards(dirs=[str(self.dir)])
		try:
			r = mcp_server.heartbeat("CMB-1", "backend-1", "testing")
			again = mcp_server.heartbeat("CMB-1", "backend-1", "testing")
		finally:
			mcp_server.BOARDS = old
		self.assertEqual([n["text"] for n in r["inbox"]["notes"]], ["for the MCP"])
		self.assertNotIn("inbox", again)


if __name__ == "__main__":
	unittest.main()
