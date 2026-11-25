"""API-23: list(ids=[...]) returns exactly those tickets on every front door; a q with no hits says q needs every word."""
import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd import store as st
from unblockd.boards import Boards
from unblockd.store import Store, TicketError

try:
	import mcp
except ImportError:
	mcp = None


class ListIdsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "p" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({"id": "p"}), encoding="utf-8")
		self.s = Store(d)
		for i, title in enumerate(("parry window", "dodge roll", "hud bar", "parry sound"), 1):
			self.s.create({"title": title}, tid=f"A-{i}")
		from unblockd import web
		web.BOARDS = Boards(dirs=[str(d)])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def ids(self, res):
		return [t["id"] for t in res]

	def test_store(self):
		self.assertEqual(self.ids(self.s.list(ids=["A-3", "A-1"])), ["A-3", "A-1"])  # exactly those, in the asked order
		self.assertEqual(self.ids(self.s.list(ids="A-2, a-4 A-2 NOPE-9")), ["A-2", "A-4"])  # comma string, case, dupes, unknown left out
		self.assertEqual(self.ids(self.s.list(ids=["A-1", "A-4"], q="parry sound")), ["A-4"])  # with the other filters: AND
		self.assertEqual(self.ids(self.s.list(ids="A-2 a-2")), ["A-2"])  # one ticket, however it is spelled (review 2026-11-26)
		self.assertEqual(self.s.list(ids=[]), [])
		with self.assertRaisesRegex(TicketError, "ids must be a list"):
			self.s.list(ids=[1, 2])
		with self.assertRaisesRegex(TicketError, "at most 200"):
			self.s.list(ids=[f"A-{i}" for i in range(201)])

	def test_q_hint(self):
		self.assertEqual(self.s.list(q="parry roll"), [])  # every word in ONE ticket
		h = st.q_hint("parry roll")
		self.assertIn("needs ALL its words", h)
		self.assertNotIn("ids=", h)
		self.assertIn("ids=['A-1', 'A-3']", st.q_hint("A-1, a-3"))  # a pasted list of ids
		self.assertEqual(st.q_hint("zzz"), "no ticket contains q")
		self.assertIn("with your other filters", st.q_hint("zzz", True))  # a done ticket may hold q while status=todo hides it (review 2026-11-26)

	def test_rest_remote_cli(self):
		with urllib.request.urlopen(self.base + "/api/tickets?ids=A-4,A-2") as r:
			self.assertEqual(self.ids(json.loads(r.read())), ["A-4", "A-2"])
		self.assertEqual(self.ids(cli.Remote(self.base).list(ids=["A-3", "A-1"])), ["A-3", "A-1"])

		def run(*a):
			out, err = io.StringIO(), io.StringIO()
			with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
				code = cli.main(["--dir", str(self.s.dir), *a])
			return code, json.loads(out.getvalue()), err.getvalue()
		code, out, _ = run("list", "--ids", "A-2,A-1")
		self.assertEqual((code, self.ids(out)), (0, ["A-2", "A-1"]))
		code, out, err = run("list", "-q", "A-1 A-2")
		self.assertEqual((code, out), (0, []))  # stdout stays the JSON list
		self.assertIn("needs ALL its words", err)
		self.assertIn("ids=['A-1', 'A-2']", err)

	@unittest.skipIf(mcp is None, "mcp not installed")
	def test_mcp(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = Boards(dirs=[str(self.s.dir)])
		self.assertEqual(self.ids(m.list_tickets(ids=["A-4", "A-1"])), ["A-4", "A-1"])
		got = m.list_tickets(ids=["A-1", "NOPE-1"])
		self.assertEqual(self.ids(got["tickets"]), ["A-1"])
		self.assertIn("not listed: NOPE-1", got["hint"])
		got = m.list_tickets(q="A-1 A-2")
		self.assertEqual(got["tickets"], [])
		self.assertIn("needs ALL its words", got["hint"])
		self.assertIsInstance(m.list_tickets(q="parry"), list)  # hits: the plain list, as before
		for args in ({"ids": ["A-2"]}, {"q": "nothing here"}):  # both reply shapes pass the SDK's output schema
			res = asyncio.run(m.mcp.call_tool("list_tickets", args))
			self.assertTrue(res, args)


if __name__ == "__main__":
	unittest.main()
