"""A board given as a database location (CORE-33): in UNBLOCKD_BOARDS / TICKETS_DIR it is listed next to folder boards
and works from every front door (REST, MCP, CLI, the notifier); a CLI --dir may be one too. Folder discovery is unchanged.
SQLite always; Postgres / MongoDB when UNBLOCKD_TEST_PG / UNBLOCKD_TEST_MONGO(_RS) are set."""
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import boards as bd
from unblockd import cli, connectors, notify, sql
from unblockd.boards import Boards, entries
from unblockd.store import Store

PG = os.environ.get("UNBLOCKD_TEST_PG")
MONGO = os.environ.get("UNBLOCKD_TEST_MONGO_RS") or os.environ.get("UNBLOCKD_TEST_MONGO")
try:
	import mcp  # noqa: F401
except ImportError:
	mcp = None


class DbBoardsTest(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		self.addCleanup(sql.disconnect)
		self.tmp = Path(tmp.name)
		self.root = self.tmp / "projects"
		folder = self.root / "game" / "tickets"
		folder.mkdir(parents=True)
		(folder / "unblockd.json").write_text(json.dumps({"title": "Game"}), encoding="utf-8")
		Store(folder).create({"title": "a folder ticket"}, prefix="G")
		self.db = "sqlite:///" + (self.tmp / "u.db").as_posix() + "#payments"
		Store(self.db).create({"title": "a database ticket"}, prefix="PAY")
		self.boards = Boards([str(self.root)], [self.db])

	def test_listed_next_to_folder_boards(self):
		self.assertEqual(list(self.boards.all()), ["payments", "game"])  # explicit entries first, as for folders
		got = {b["id"]: b for b in self.boards.summary()}
		self.assertEqual((got["payments"]["tickets"], got["game"]["tickets"]), (1, 1))
		self.assertEqual(got["payments"]["path"], self.db)
		self.assertEqual([t["id"] for t in self.boards.get("payments").list()], ["PAY-1"])
		self.assertEqual(self.boards.get(None).backend.kind, "sql")  # the first board is the default, whatever it is

	def test_the_password_never_shows_and_a_broken_one_hides_only_itself(self):
		try:
			import psycopg  # noqa: F401
		except ImportError:
			self.skipTest("psycopg not installed")
		dead = f"postgres://u:s3cret@127.0.0.1:1/x{uuid.uuid4().hex[:6]}#ops"
		b = Boards([str(self.root)], [dead, self.db])
		self.addCleanup(connectors._DOWN.pop, connectors.split(dead)[0], None)
		with mock.patch.object(sql, "CONNECT_S", 2):
			got = {x["id"]: x for x in b.summary()}
			t0 = time.monotonic()
			again = {x["id"]: x for x in b.summary()}
		self.assertLess(time.monotonic() - t0, 1)  # the dead server is not waited for on every listing
		self.assertIn("next try", again["ops"]["error"])
		self.assertEqual(got["ops"]["path"], dead.replace("s3cret", "***"))
		self.assertIn("error", got["ops"])
		self.assertNotIn("s3cret", json.dumps(got))
		self.assertEqual(got["payments"]["tickets"], 1)
		self.assertIn("name the board", Boards([], ["sqlite:///x.db"]).summary()[0]["error"])  # no #board: says what is missing

	def test_a_new_database_board_is_created_on_first_use(self):
		new = "sqlite:///" + (self.tmp / "u.db").as_posix() + "#fresh"
		b = Boards([], [new])
		self.assertEqual(b.summary()[0]["tickets"], 0)
		b.get("fresh").create({"title": "first"}, prefix="F")
		self.assertEqual(Boards([], [new]).summary()[0]["tickets"], 1)

	def test_env_lists_survive_the_colon_separator(self):
		pg = "postgres://u:p@h:5432/db#b"
		self.assertEqual(entries(f"/a/tickets:{pg}:sqlite:////x/y.db#c:mongodb+srv://u:p@c.net/db?w=1#d:/b", ":"),
		                 ["/a/tickets", pg, "sqlite:////x/y.db#c", "mongodb+srv://u:p@c.net/db?w=1#d", "/b"])
		self.assertEqual(entries(r"C:\a\tickets;sqlite:///C:/x.db#c;;", ";"), [r"C:\a\tickets", "sqlite:///C:/x.db#c"])
		self.assertEqual(entries("/a:/b", ":"), ["/a", "/b"])  # folders split as before
		env = {"UNBLOCKD_BOARDS": os.pathsep.join([str(self.root / "game" / "tickets"), self.db]), "TICKETS_DIR": "", "UNBLOCKD_ROOT": ""}
		with mock.patch.dict(os.environ, env):
			self.assertEqual(list(bd.from_env().all()), ["game", "payments"])
		with mock.patch.dict(os.environ, {"TICKETS_DIR": self.db, "UNBLOCKD_BOARDS": "", "UNBLOCKD_ROOT": ""}):
			self.assertEqual(bd.from_env().get(None).list()[0]["id"], "PAY-1")

	def test_rest(self):
		from unblockd import web
		self.addCleanup(setattr, web, "BOARDS", web.BOARDS)
		web.BOARDS = self.boards
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		self.addCleanup(srv.server_close)
		self.addCleanup(srv.shutdown)
		base = f"http://127.0.0.1:{srv.server_port}"

		def call(method, path, body=None):
			req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
			                             headers={"Content-Type": "application/json"})
			try:
				with urllib.request.urlopen(req) as r:
					return r.status, json.loads(r.read())
			except urllib.error.HTTPError as e:
				return e.code, json.loads(e.read())
		self.assertEqual([b["id"] for b in call("GET", "/api/boards")[1]], ["payments", "game"])
		code, t = call("POST", "/api/b/payments/tickets", {"prefix": "PAY", "title": "from the web", "body": "why"})
		self.assertEqual((code, t["id"]), (201, "PAY-2"))
		code, t = call("PATCH", "/api/b/payments/tickets/PAY-2", {"priority": "P1"})
		self.assertEqual((code, t["priority"]), (200, "P1"))
		self.assertEqual(call("POST", "/api/b/payments/tickets/PAY-2/notes", {"text": "hi"})[0], 200)
		self.assertEqual(len(call("GET", "/api/b/payments/tickets")[1]), 2)
		self.assertEqual(call("GET", "/api/b/payments/activity")[0], 200)
		self.assertEqual(call("GET", "/api/needs")[0], 200)
		self.assertEqual([x["id"] for x in call("GET", "/api/b/game/tickets")[1]], ["G-1"])  # the folder board is untouched

	@unittest.skipIf(mcp is None, "mcp not installed")
	def test_mcp(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = self.boards
		self.assertIn("payments", [b["id"] for b in m.list_boards()])
		m.create_ticket(board="payments", agent="Omri", title="via mcp", prefix="PAY")
		got = m.claim_next("w-1", board="payments")
		self.assertEqual(got["id"], "PAY-1")
		m.complete("PAY-1", "w-1", "cause -> fix -> tests", board="payments")
		self.assertEqual(m.get_ticket("PAY-1", board="payments")["status"], "done")

	def test_cli_dir_takes_a_database_location(self):
		def run(*args):
			out = io.StringIO()
			with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}):
				self.assertEqual(cli.main(["--dir", self.db, *args]), 0)
			return json.loads(out.getvalue())
		self.assertEqual(run("create", "--prefix", "PAY", "--title", "from the cli")["id"], "PAY-2")
		self.assertEqual([t["id"] for t in run("list")], ["PAY-1", "PAY-2"])
		self.assertEqual(run("claim", "--agent", "w-1")["id"], "PAY-1")

	def test_the_notifier_reads_a_database_board(self):
		n = notify.Notifier(Boards([], [self.db]), "http://127.0.0.1:1/never")
		sent = []
		n._send = lambda bid, lines, events: sent.append((bid, lines))
		n.poll()  # first sight: history is not replayed
		s = self.boards.get("payments")
		s.create({"title": "busy"}, prefix="PAY")  # keeps the board from running dry
		s.claim("w-1", tid="PAY-1")
		s.complete("PAY-1", "w-1", "did it")
		n.poll()
		self.assertTrue(any("PAY-1" in line and "did it" in line for _, lines in sent for line in lines), sent)


class ReachTest(unittest.TestCase):
	def test_a_failed_server_is_not_retried_for_a_while(self):
		from unblockd.store import TicketError
		url, calls = "postgres://x/" + uuid.uuid4().hex, []
		self.addCleanup(connectors._DOWN.pop, url, None)

		def fail():
			calls.append(1)
			raise TicketError("can't connect to x")
		for _ in range(3):
			with self.assertRaisesRegex(TicketError, "can't connect"):
				connectors.reach(url, fail)
		self.assertEqual(len(calls), 1)
		later = time.monotonic() + connectors.RETRY_S + 1
		with mock.patch.object(connectors.time, "monotonic", lambda: later):
			self.assertEqual(connectors.reach(url, lambda: "up"), "up")  # tried again after RETRY_S, and it came back
		self.assertNotIn(url, connectors._DOWN)
		self.assertEqual(connectors.reach(url, lambda: "up"), "up")


class OtherDatabasesTest(unittest.TestCase):
	def check(self, loc):
		b = Boards([], [loc])
		bid = connectors.split(loc)[1]
		b.get(bid).create({"title": "x"}, prefix="X")
		self.assertEqual(b.summary()[0]["tickets"], 1)
		self.assertNotIn(":test@", b.summary()[0]["path"])

	@unittest.skipUnless(PG, "set UNBLOCKD_TEST_PG")
	def test_postgres(self):
		loc = f"{PG}#d{uuid.uuid4().hex[:10]}"
		self.addCleanup(lambda: connectors.open_backend(loc).drop())
		self.check(loc)

	@unittest.skipUnless(MONGO, "set UNBLOCKD_TEST_MONGO or UNBLOCKD_TEST_MONGO_RS")
	def test_mongo(self):
		from unblockd import mongo
		from tests.test_mongo import with_db
		url = with_db(MONGO, "ub_test_" + uuid.uuid4().hex[:8])
		self.addCleanup(lambda: mongo._client(url)[0].drop_database(mongo._client(url)[1].name))
		self.check(url + "#ops")


if __name__ == "__main__":
	unittest.main()
