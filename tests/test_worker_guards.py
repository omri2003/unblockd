"""Missing tools are reported and routed around; a worker session stops at the board's limit; docs come back after the resend window."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd.store import Store, TicketError

try:
	import mcp
except ImportError:  # CI runs the store tests without the mcp package
	mcp = None

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "agents": [{"name": "gui", "tags": ["ui"]}, {"name": "backend", "tags": ["ui"]}]}


class MissingToolTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "tickets"
		d.mkdir()
		(d / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(d)
		self.s.create({"title": "fix login button", "tags": ["ui"]}, tid="UI-1")

	def tearDown(self):
		self.tmp.cleanup()

	def test_reported_skipped_by_that_profile_and_cleared_by_a_human(self):
		s = self.s
		s.claim("gui-1", tid="UI-1")
		with self.assertRaisesRegex(TicketError, "needs `why`"):
			s.release("UI-1", "gui-1", missing="browser")
		t = s.release("UI-1", "gui-1", missing="browser", why="must click the login button to test the fix")
		self.assertEqual([(n["tool"], n["profile"]) for n in t["needs"]], [("browser", "gui")])
		self.assertEqual((t["notes"][-1]["kind"], t["notes"][-1].get("pinned")), ("warning", True))
		self.assertIn("must click the login button", t["notes"][-1]["text"])
		r = s.claim_next("gui-2")  # same profile: skipped, and it waits on a human, so no "conflict" retry loop
		self.assertEqual((r["ticket"], r["reason"], r["ids"]["waiting_on_human"]), (None, "waiting", ["UI-1"]))
		with self.assertRaisesRegex(TicketError, "has no browser"):
			s.claim("gui-2", tid="UI-1")
		self.assertEqual(s.claim("backend-1", tid="UI-1")["id"], "UI-1")  # another profile may have it
		s.release("UI-1", "backend-1")
		s.update("UI-1", {"needs": []})  # the human gave gui a browser
		self.assertEqual(s.claim("gui-2", tid="UI-1")["id"], "UI-1")

	def test_needs_you_lists_it(self):
		from unblockd import web
		from unblockd.boards import Boards
		self.s.claim("gui-1", tid="UI-1")
		self.s.release("UI-1", "gui-1", missing="browser", why="click it")
		web.BOARDS = Boards(dirs=[str(self.s.dir)])
		items = [i for g in web._needs() for i in g["items"]]
		self.assertEqual([(i["id"], i["kind"], i["needs"][0]["tool"]) for i in items], [("UI-1", "stuck", "browser")])


@unittest.skipIf(mcp is None, "mcp not installed")
class SessionTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name)
		(d / "POLICY.md").write_text("how we work", encoding="utf-8")
		t = d / "tickets"
		t.mkdir()
		(t / "unblockd.json").write_text(json.dumps({"docs": ["POLICY.md"], "session_tickets": 2, "docs_resend": {"claims": 9, "minutes": 30}}), encoding="utf-8")
		import os
		self.env = mock.patch.dict(os.environ, {"TICKETS_DIR": str(t)})
		self.env.start()
		from unblockd import mcp_server as m
		self.m = m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		for i in range(1, 5):
			m.BOARDS.get(None).create({"title": str(i)}, tid=f"A-{i}")
		m.SENT.clear()
		m.CLAIMS.clear()
		m.DEDUPE = True

	def tearDown(self):
		self.m.DEDUPE = False
		self.env.stop()
		self.tmp.cleanup()

	def test_session_limit_and_resend_window(self):
		m = self.m
		pol = lambda r: [d for d in r["context"]["docs"] if d["path"] == "POLICY.md"][0]
		r1 = m.claim_next("w")
		self.assertEqual(pol(r1)["content"], "how we work")
		m.complete(r1["id"], "w", "done")
		r2 = m.claim_next("w")
		self.assertIsNone(pol(r2)["content"])  # within the window: not re-sent
		m.complete(r2["id"], "w", "done")
		self.assertEqual(m.claim_next("w")["reason"], "session_done")  # session_tickets = 2
		m.CLAIMS.clear()  # a new session
		for k, (h, n, at) in list(m.SENT[("", "w")].items()):
			m.SENT[("", "w")][k] = (h, n, at - 31 * 60)  # sent 31 minutes ago
		r3 = m.claim_next("w")
		self.assertEqual(pol(r3)["content"], "how we work")  # past the window: sent again
		m.complete(r3["id"], "w", "done")
		self.assertEqual(pol(m.claim_next("w", fresh=True))["content"], "how we work")  # fresh: sent again


if __name__ == "__main__":
	unittest.main()
