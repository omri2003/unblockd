"""AI-57: complete(closes=[ids]) closes tickets whose work landed under another one; list(stale_days=N) / work_plan flag idle todo tickets."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import cli, web
from unblockd.boards import Boards
from unblockd.store import Store, TicketError

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "pillars": [{"key": "CORE"}], "agents": [{"name": "backend", "pillars": ["CORE"]}]}


class ClosesTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		self.cfg(CFG)
		for i in range(1, 6):
			self.s.create({"title": f"t{i}", "pillar": "CORE"}, tid=f"CORE-{i}")

	def cfg(self, c):
		(self.dir / "unblockd.json").write_text(json.dumps(c), encoding="utf-8")
		self.s = Store(self.dir)

	def test_closes_moves_them_to_done_under_the_ticket(self):
		self.s.claim("backend-1", tid="CORE-1")
		out = self.s.complete("CORE-1", "backend-1", "rules landed\nmore lines", refs=["PR 9"], closes=["CORE-2", "CORE-3"])
		self.assertEqual(out["result"]["closes"], ["CORE-2", "CORE-3"])
		self.assertEqual([c["id"] for c in out["closed"]], ["CORE-2", "CORE-3"])
		for x in ("CORE-2", "CORE-3"):
			t = self.s.get(x)
			self.assertEqual(t["status"], "done")
			self.assertTrue(t["done_at"])
			self.assertEqual(t["notes"][-1]["text"], "Done under CORE-1: rules landed")
			self.assertEqual((t["result"]["under"], t["result"]["by"], t["result"]["refs"]), ("CORE-1", "backend-1", ["PR 9"]))
			self.assertNotIn("unreported", t)  # it has a summary: not "finished without a summary"
			self.assertEqual(self.s.activity(x)[0]["note"], "done under CORE-1")

	def test_all_or_nothing(self):
		self.s.claim("backend-1", tid="CORE-1")
		self.s.claim("backend-2", tid="CORE-4")
		self.s.update("CORE-5", {"status": "done"})
		with self.assertRaisesRegex(TicketError, "CORE-1 not completed: no ticket CORE-9; CORE-4 is claimed by backend-2.*; CORE-5 is already done"):
			self.s.complete("CORE-1", "backend-1", "s", closes=["CORE-2", "CORE-9", "CORE-4", "CORE-5"])
		self.assertEqual((self.s.get("CORE-1")["status"], self.s.get("CORE-2")["status"]), ("doing", "todo"))  # nothing changed
		self.assertEqual(self.s.get("CORE-1")["claim"]["agent"], "backend-1")
		for bad, msg in ((["CORE-1"], "itself"), (["not an id"], "list of ticket ids"), ([f"CORE-{i}" for i in range(10, 40)], "at most 20")):
			with self.assertRaisesRegex(TicketError, msg):
				self.s.complete("CORE-1", "backend-1", "s", closes=bad)

	def test_guardrails(self):  # owner, 2026-11-26: closes is never a way around a person's decision or a blocker
		self.s.ask("CORE-2", "backend-2", "keep the old flag?")
		self.s.create({"title": "proposal", "pillar": "CORE"}, tid="CORE-6", agent="backend-3")  # open board: not a proposal
		self.s.update("CORE-6", {"owner": "Omri"})
		self.s.update("CORE-4", {"blocked_by": ["CORE-5"]})
		self.s.claim("backend-1", tid="CORE-1")
		with self.assertRaisesRegex(TicketError, "CORE-2 waits on a human.*CORE-6 is owned by Omri.*CORE-4 still waits on CORE-5"):
			self.s.complete("CORE-1", "backend-1", "s", closes=["CORE-2", "CORE-6", "CORE-4"])
		self.assertEqual(self.s.get("CORE-1")["status"], "doing")  # nothing changed
		self.cfg({**CFG, "agent_tickets": "propose"})
		self.s.create({"title": "idea", "pillar": "CORE"}, tid="CORE-7", agent="backend-3")
		with self.assertRaisesRegex(TicketError, "CORE-7 is a proposal"):
			self.s.complete("CORE-1", "backend-1", "s", closes=["CORE-7"])

	def test_review_board_and_own_claim(self):
		self.cfg({**CFG, "review": True})
		self.s.claim("backend-1", tid="CORE-1")
		self.s.claim("backend-1", tid="CORE-2")  # the worker's own claim on a closed ticket ends with it
		self.s.complete("CORE-1", "backend-1", "s", closes=["CORE-2"])
		t = self.s.get("CORE-2")
		self.assertEqual((t["status"], t["claim"]), ("review", None))  # the same gate as the ticket itself

	def test_front_doors(self):
		self.s.claim("backend-1", tid="CORE-1")
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(io.StringIO()) as out:
			self.assertEqual(cli.main(["--dir", str(self.dir), "complete", "CORE-1", "--agent", "backend-1", "--summary", "s", "--closes", "CORE-2"]), 0)
		self.assertEqual(json.loads(out.getvalue())["closed"][0]["id"], "CORE-2")
		old, web.BOARDS = web.BOARDS, Boards(dirs=[str(self.dir)])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		try:
			r = cli.Remote(f"http://127.0.0.1:{srv.server_port}")
			r.claim("backend-1", tid="CORE-3")
			self.assertEqual(r.complete("CORE-3", "backend-1", "s", closes=["CORE-4"])["closed"][0]["status"], "done")
			self.assertEqual(r.list(stale_days=1), [])  # nothing idle yet
		finally:
			srv.shutdown()
			srv.server_close()
			web.BOARDS = old
		try:
			from unblockd import mcp_server
		except ImportError:
			self.skipTest("mcp not installed")
		old, mcp_server.BOARDS = mcp_server.BOARDS, Boards(dirs=[str(self.dir)])
		try:
			self.s.create({"title": "t6", "pillar": "CORE"}, tid="CORE-6")
			mcp_server.claim_ticket("CORE-5", "backend-1")
			r = mcp_server.complete("CORE-5", "backend-1", "s", closes=["CORE-6"])
		finally:
			mcp_server.BOARDS = old
		self.assertEqual(r["closed"][0]["id"], "CORE-6")


class StaleTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(self.dir)
		for i in (1, 2, 3):
			self.s.create({"title": f"t{i}", "pillar": "CORE"}, tid=f"CORE-{i}")
		self.age("CORE-1", "CORE-2", "CORE-3", days=12)

	def age(self, *ids, days):
		"""Pretend the tickets were last written and logged `days` ago."""
		old = (date.today() - timedelta(days=days)).isoformat()
		for x in ids:
			p = self.dir / f"{x}.json"
			t = json.loads(p.read_text(encoding="utf-8"))
			t["updated"] = t["created"] = old
			p.write_text(json.dumps(t), encoding="utf-8")
		log = self.dir / ".unblockd" / "activity.jsonl"
		ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
		lines = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
		log.write_text("".join(json.dumps({**e, "ts": ts if e.get("ticket") in ids else e["ts"]}) + "\n" for e in lines), encoding="utf-8")
		self.s = Store(self.dir)

	def test_list_stale_days(self):
		self.s.add_note("CORE-2", "Omri", "still wanted")  # an event (and a write) today
		rows = self.s.list(stale_days=10)
		self.assertEqual([(t["id"], t["idle_days"]) for t in rows], [("CORE-1", 12), ("CORE-3", 12)])
		self.assertEqual(self.s.list(stale_days=13), [])
		self.s.claim("backend-1", tid="CORE-3")  # doing: not a stale todo
		self.assertEqual([t["id"] for t in self.s.list(stale_days=10)], ["CORE-1"])
		with self.assertRaisesRegex(TicketError, "stale_days must be a whole number"):
			self.s.list(stale_days=0)

	def test_work_plan_marks_them(self):
		self.s.add_note("CORE-2", "Omri", "still wanted")
		plan = self.s.work_plan(per_agent=5)
		rows = {t["id"]: t for t in plan["agents"][0]["tickets"]}
		self.assertEqual(rows["CORE-1"]["idle_days"], 12)  # the board's stale_days (7) is the bar
		self.assertIn("landed under another ticket", rows["CORE-1"]["stale_hint"])
		self.assertNotIn("idle_days", rows["CORE-2"])
		self.assertEqual(plan["stale"], 2)
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(io.StringIO()) as out:
			self.assertEqual(cli.main(["--dir", str(self.dir), "list", "--stale-days", "10"]), 0)
		self.assertEqual([(t["id"], t["idle_days"]) for t in json.loads(out.getvalue())], [("CORE-1", 12), ("CORE-3", 12)])
		try:
			from unblockd import mcp_server
		except ImportError:
			self.skipTest("mcp not installed")
		old, mcp_server.BOARDS = mcp_server.BOARDS, Boards(dirs=[str(self.dir)])
		try:
			self.assertEqual([t["id"] for t in mcp_server.list_tickets(stale_days=10)], ["CORE-1", "CORE-3"])
			self.assertEqual(mcp_server.work_plan()["agents"][0]["tickets"][0]["idle_days"], 12)
		finally:
			mcp_server.BOARDS = old


if __name__ == "__main__":
	unittest.main()
