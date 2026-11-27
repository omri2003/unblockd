"""The system database (CORE-34): unblockd's own data. SQLite by default with no settings; Postgres when UNBLOCKD_TEST_PG is set.
Registered boards appear in the board list next to scanned ones, from every front door that lists boards."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import boards as bd
from unblockd import cli, sql
from unblockd import system as sy
from unblockd.boards import Boards
from unblockd.errors import NotFound, TicketError
from unblockd.store import Store

PG = os.environ.get("UNBLOCKD_TEST_PG")


class SystemDbTest(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		self.addCleanup(sql.disconnect)
		self.tmp = Path(tmp.name)
		self.root = self.tmp / "projects"
		game = self.root / "game" / "tickets"
		game.mkdir(parents=True)
		(game / "unblockd.json").write_text(json.dumps({"title": "Game"}), encoding="utf-8")
		self.other = self.tmp / "elsewhere" / "tickets"  # a folder board nothing scans
		self.other.mkdir(parents=True)
		(self.other / "unblockd.json").write_text(json.dumps({"title": "Ops"}), encoding="utf-8")
		Store(self.other).create({"title": "an ops ticket"}, prefix="OPS")
		self.sys = sy.System(str(self.tmp / "sys" / "system.db"))
		self.addCleanup(self.sys.close)
		self.boards = Boards([str(self.root)], [], [], system=self.sys)

	def test_a_fresh_start_with_no_settings_creates_it(self):
		home = self.tmp / "home"
		with mock.patch.dict(os.environ, {"UNBLOCKD_SYSTEM_DB": "", "UNBLOCKD_SYSTEM_DB_FILE": ""}), mock.patch("pathlib.Path.home", return_value=home):
			s = bd.from_env().system
			self.addCleanup(s.close)
			self.assertEqual(s.url, "sqlite:///" + (home / ".unblockd" / "system.db").as_posix())
			self.assertFalse((home / ".unblockd" / "system.db").exists())  # nothing until first use
			self.assertEqual(s.info(), {"url": s.url, "kind": "sqlite", "schema": sy.SCHEMA, "boards": 0, "connectors": 0})
		self.assertTrue((home / ".unblockd" / "system.db").is_file())
		db = sqlite3.connect(home / ".unblockd" / "system.db")
		self.addCleanup(db.close)
		self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}, {"us_meta", "us_orgs", "us_connectors", "us_boards"})
		self.assertEqual(db.execute("SELECT id FROM us_orgs").fetchall(), [("default",)])  # every row names its org (CORE-69 adds users to it)

	def test_env_and_file_forms(self):
		f = self.tmp / "url.txt"
		f.write_text(str(self.tmp / "x.db") + "\n", encoding="utf-8")
		with mock.patch.dict(os.environ, {"UNBLOCKD_SYSTEM_DB": "", "UNBLOCKD_SYSTEM_DB_FILE": str(f)}):
			self.assertEqual(sy.from_env().url, "sqlite:///" + (self.tmp / "x.db").as_posix())
		with mock.patch.dict(os.environ, {"UNBLOCKD_SYSTEM_DB": "a.db", "UNBLOCKD_SYSTEM_DB_FILE": str(f)}):
			with self.assertRaisesRegex(TicketError, "both UNBLOCKD_SYSTEM_DB and UNBLOCKD_SYSTEM_DB_FILE"):
				sy.from_env()
		with mock.patch.dict(os.environ, {"UNBLOCKD_SYSTEM_DB": "", "UNBLOCKD_SYSTEM_DB_FILE": str(self.tmp / "missing")}):
			with self.assertRaisesRegex(TicketError, "UNBLOCKD_SYSTEM_DB_FILE=.*can't read it"):
				sy.from_env()
		self.assertEqual(sy.normalize("postgresql://u@h/db"), "postgres://u@h/db")
		with self.assertRaisesRegex(TicketError, "SQLite or Postgres"):
			sy.System("mongodb://h/db")
		with self.assertRaisesRegex(TicketError, "unsupported system database"):
			sy.System("mysql://u:secret@h/db")

	def test_registered_boards_are_listed_after_the_scanned_ones(self):
		self.assertEqual(list(self.boards.all()), ["game"])
		got = self.boards.register("ops", str(self.other), title="Ops")
		self.assertEqual((got["location"], got["connector"]), (self.other.absolute().as_posix(), None))
		self.assertEqual(list(self.boards.all()), ["game", "ops"])
		rows = {b["id"]: b for b in self.boards.summary()}
		self.assertEqual((rows["ops"]["tickets"], rows["ops"].get("registered"), rows["game"].get("registered")), (1, True, None))
		self.assertEqual(self.boards.get("ops").list()[0]["id"], "OPS-1")
		with self.assertRaisesRegex(TicketError, "already listed"):
			self.boards.register("game", str(self.other))
		with self.assertRaisesRegex(TicketError, "already registered"):
			self.boards.register("ops", str(self.other))
		third = self.tmp / "third" / "tickets"
		third.mkdir(parents=True)
		self.sys.add_board("game", str(third))  # straight into the table (bypassing register): the clash gets -2, nothing hides
		self.assertEqual(list(self.boards.all()), ["game", "game-2", "ops"])
		self.sys.remove_board("game")
		scanned = self.root / "game" / "tickets"
		self.sys.add_board("again", str(scanned))  # a scanned folder registered too: listed once, under its scanned id
		self.assertEqual(list(self.boards.all()), ["game", "ops"])
		self.assertEqual(self.sys.remove_board("ops")["removed"], "ops")
		self.assertTrue((self.other / "OPS-1.json").is_file())  # unregistering leaves the tickets alone
		with self.assertRaises(NotFound):
			self.sys.remove_board("ops")

	def test_boards_in_a_connector(self):
		db = "sqlite:///" + (self.tmp / "boards.db").as_posix()
		self.assertEqual(self.sys.add_connector("main", db)["kind"], "sqlite")
		got = self.boards.register("pay", connector="main", title="Payments")
		self.assertEqual((got["tickets"], got.get("new")), (0, True))  # registering creates the database board, and says so
		self.boards.register("hr", "people", connector="main")  # key in the database differs from the id
		self.assertNotIn("new", self.boards.register("pay2", "pay", connector="main"))  # an existing board: nothing new
		self.assertEqual(self.boards.all()["pay"], db + "#pay")
		self.assertEqual(self.boards.all()["hr"], db + "#people")
		self.boards.get("pay").create({"title": "first"}, prefix="PAY")
		self.assertEqual(Store(db + "#pay").list()[0]["id"], "PAY-1")
		self.assertEqual(sorted(sql.boards(db)), ["pay", "people"])  # created by register, not later by opening
		with self.assertRaisesRegex(TicketError, "holds registered boards \\(hr, pay, pay2\\)"):
			self.sys.remove_connector("main")
		with self.assertRaisesRegex(TicketError, "already exists"):
			self.sys.add_connector("main", db)
		with self.assertRaises(NotFound):
			self.boards.register("x", connector="nope")
		for bad, why in (("postgres://u:pw@h/db", "password"), (db + "#b", "without #board"), ("/a/folder", "database address")):
			with self.assertRaisesRegex(TicketError, why):
				self.sys.add_connector("bad", bad)
		with self.assertRaisesRegex(TicketError, "add the database as a connector"):
			self.sys.add_board("pw", "postgres://u:pw@h/db#b")
		with self.assertRaisesRegex(TicketError, "name the board"):
			self.sys.add_board("nob", db)
		with self.assertRaisesRegex(TicketError, "no folder"):
			self.sys.add_board("gone", str(self.tmp / "gone"))
		with self.assertRaisesRegex(TicketError, "bad board id"):
			self.sys.add_board("Bad Id", str(self.other))

	def test_a_registered_board_is_never_created_by_opening_it(self):
		db = "sqlite:///" + (self.tmp / "boards.db").as_posix()
		self.sys.add_connector("main", db)
		self.boards.register("pay", connector="main")
		self.sys.add_board("typo", "pya", connector="main")  # straight into the table: its #board was never created
		rows = {b["id"]: b for b in self.boards.summary()}
		self.assertIn("no board 'pya'", rows["typo"]["error"])
		self.assertIn("system remove-board typo", rows["typo"]["error"])
		with self.assertRaisesRegex(TicketError, "does not exist"):
			self.boards.get("typo")
		self.assertEqual(sql.boards(db), ["pay"])  # nothing quietly made
		self.assertEqual(rows["pay"]["tickets"], 0)  # the others are fine

	def test_a_broken_entry_hides_only_itself(self):
		db = "sqlite:///" + (self.tmp / "boards.db").as_posix()
		self.sys.add_connector("main", db)
		self.boards.register("pay", connector="main")
		self.sys.c.q("DELETE FROM us_connectors")  # its connector went missing
		rows = {b["id"]: b for b in self.boards.summary()}
		self.assertIn("no connector 'main'", rows["pay"]["error"])
		self.assertEqual(rows["game"]["tickets"], 0)
		with self.assertRaisesRegex(TicketError, "no connector 'main'"):
			self.boards.get("pay")
		broken = Boards([str(self.root)], [], [], system=sy.System(str(self.tmp)))  # a folder, not a database file
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			self.assertEqual(list(broken.all()), ["game"])  # the scanned boards keep working
		self.assertIn("registered boards are not listed", err.getvalue())

	def test_newer_schema_refused(self):
		self.sys.info()
		self.sys.c.q("UPDATE us_meta SET v = '99' WHERE k = 'schema'")
		self.sys.close()
		with self.assertRaisesRegex(TicketError, "newer unblockd \\(schema 99"):
			self.sys.info()

	def test_cli(self):
		env = {"UNBLOCKD_SYSTEM_DB": self.sys.url, "UNBLOCKD_SYSTEM_DB_FILE": "", "UNBLOCKD_ROOT": str(self.root), "TICKETS_DIR": "", "UNBLOCKD_BOARDS": ""}

		def run(*args):
			out = io.StringIO()
			with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
				code = cli.main(["system", *args])
			return code, json.loads(out.getvalue())
		self.assertEqual(run("add-board", "ops", "--location", str(self.other))[1]["id"], "ops")
		self.assertEqual([b["id"] for b in run("boards")[1]], ["ops"])
		self.assertEqual(run("info")[1]["boards"], 1)
		code, err = run("add-board", "game", "--location", str(self.other))
		self.assertEqual((code, err["code"]), (1, "refused"))
		with mock.patch.dict(os.environ, env):
			self.assertEqual(list(bd.from_env().all()), ["game", "ops"])
		self.assertEqual(run("remove-board", "ops")[1]["removed"], "ops")
		self.assertEqual(run("connectors")[1], [])

	def test_rest_lists_and_opens_a_registered_board(self):
		from unblockd import web
		self.addCleanup(setattr, web, "BOARDS", web.BOARDS)
		web.BOARDS = self.boards
		self.boards.register("ops", str(self.other))
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		self.addCleanup(srv.server_close)
		self.addCleanup(srv.shutdown)
		base = f"http://127.0.0.1:{srv.server_address[1]}"
		got = json.load(urllib.request.urlopen(base + "/api/boards"))
		self.assertEqual([(b["id"], b.get("registered")) for b in got], [("game", None), ("ops", True)])
		self.assertEqual([t["id"] for t in json.load(urllib.request.urlopen(base + "/api/b/ops/tickets"))], ["OPS-1"])


@unittest.skipUnless(PG, "set UNBLOCKD_TEST_PG to a Postgres URL to run the system database on Postgres")
class SystemDbPostgresTest(unittest.TestCase):
	def test_postgres(self):
		s = sy.System(PG)
		self.addCleanup(s.close)
		self.assertEqual(s.info()["kind"], "postgres")
		tag = uuid.uuid4().hex[:8]
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		folder = Path(tmp.name) / "tickets"
		folder.mkdir()
		bid, con = f"pg-{tag}", f"c-{tag}"
		s.add_connector(con, "sqlite:///" + (Path(tmp.name) / "b.db").as_posix())
		self.addCleanup(lambda: s.c.q("DELETE FROM us_connectors WHERE name = ?", (con,)))
		s.add_board(bid, str(folder))
		s.add_board(bid + "-db", connector=con)
		self.addCleanup(lambda: s.c.q("DELETE FROM us_boards WHERE id LIKE ?", (bid + "%",)))
		got = Boards([], [], [], system=s).all()
		self.assertEqual(got[bid], folder)
		self.assertTrue(got[bid + "-db"].endswith("#" + bid + "-db"))
		with self.assertRaisesRegex(TicketError, "holds registered boards"):
			s.remove_connector(con)
		s.remove_board(bid + "-db")
		s.remove_board(bid)
		self.assertEqual(s.remove_connector(con), {"removed": con})


if __name__ == "__main__":
	unittest.main()
