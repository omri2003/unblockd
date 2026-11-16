"""Moving a board between connectors (connectors.migrate, CLI `migrate`): files -> SQLite -> files gives the same ticket files
byte for byte; files -> Postgres / Mongo (when their test URLs are set) pass the same check; a non-empty target is refused
unless --force; claims are not copied; settings that can't work at the target stop the move before anything is written."""
import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from unblockd import cli, connectors, knowledge as kn, mongo, sql
from unblockd.store import Store, TicketError

CFG = {"title": "Payments", "owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}]}
PG = os.environ.get("UNBLOCKD_TEST_PG")
MONGO = os.environ.get("UNBLOCKD_TEST_MONGO_RS") or os.environ.get("UNBLOCKD_TEST_MONGO")
OLD = {"id": "OLD-1", "title": "written by an old unblockd", "status": "pending", "body": "the why\n\n— omri, 2026-01-02T03:04:05+00:00: a note in the body",
       "created": "2026-01-01", "updated": "2026-01-02"}  # no rev, notes inside the body, a stored `pending`


class MigrateTest(unittest.TestCase):
	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp())
		self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
		self.addCleanup(sql.disconnect)
		self.src = self.tmp / "a" / "tickets"
		self.src.mkdir(parents=True)
		(self.src / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		s = Store(self.src)
		a = s.create({"title": "parent", "body": "why it matters", "priority": "P1", "done_when": ["it works"]}, prefix="PAY")
		s.add_note(a["id"], "Omri", "a human note", kind="decision")
		s.split(a["id"], ["one", "two"], sequential=True)
		s.claim("w-1", tid="PAY-2")
		kn.attach(s, "PAY-2", "log.txt", "line one\nline two")
		s.complete("PAY-2", "w-1", "cause -> fix -> tests", files=["a.py"], refs=["https://example.com/pr/1"], handoff="read a.py")
		s.claim("w-2", tid="PAY-3")  # a live claim: runtime state, not copied
		(self.src / "OLD-1.json").write_bytes((json.dumps(OLD, indent=2, ensure_ascii=False) + "\n").encode())  # LF, as unblockd writes files
		self.events = len(s.activity(limit=10_000))

	def make_other(self) -> Path:
		other = self.tmp / ("o" + uuid.uuid4().hex[:6]) / "tickets"
		other.mkdir(parents=True)
		(other / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		Store(other).create({"title": "only this"}, prefix="X")
		return other

	def files_of(self, d: Path) -> dict:
		return {p.name: p.read_bytes() for p in sorted(d.glob("*-*.json"))}

	def sqlite(self, board="pay") -> str:
		return "sqlite:///" + (self.tmp / "db.sqlite").as_posix() + "#" + board

	def round_trip(self, db: str) -> None:
		r = connectors.migrate(str(self.src), db)
		self.assertEqual((r["tickets"], r["attachments"], r["events"]), (4, 1, self.events))
		back = self.tmp / ("back-" + uuid.uuid4().hex[:6]) / "tickets"
		back.mkdir(parents=True)
		connectors.migrate(db, str(back))
		self.assertEqual(self.files_of(back), self.files_of(self.src))  # byte for byte, the old-format file too
		self.assertEqual(json.loads((back / "unblockd.json").read_text(encoding="utf-8")), CFG)
		self.assertEqual((back / "files" / "PAY-2" / "log.txt").read_bytes(), b"line one\nline two")
		s = Store(backend=connectors.open_backend(db, create=False))
		self.assertEqual(sorted(t["id"] for t in s.list()), ["OLD-1", "PAY-1", "PAY-2", "PAY-3"])
		self.assertEqual(s.get("PAY-2")["result"]["handoff"], "read a.py")
		self.assertEqual(s.get("OLD-1")["notes"][0]["text"], "a note in the body")  # parsed on read, stored as it was
		self.assertIsNone(s.get("PAY-3")["claim"])  # claims are not copied ...
		self.assertEqual(s.get("PAY-3")["status"], "doing")  # ... so its worker's ticket is orphaned until a human moves it
		self.assertEqual(len(s.activity(limit=10_000)), self.events)
		self.assertEqual(Store(back).get("PAY-1")["rev"], Store(self.src).get("PAY-1")["rev"])

	def test_files_to_sqlite_and_back_is_byte_for_byte(self):
		self.round_trip(self.sqlite())

	@unittest.skipUnless(PG, "set UNBLOCKD_TEST_PG")
	def test_files_to_postgres_and_back(self):
		db = f"{PG}#m{uuid.uuid4().hex[:10]}"
		self.addCleanup(lambda: connectors.open_backend(db).drop())
		self.round_trip(db)

	@unittest.skipUnless(MONGO, "set UNBLOCKD_TEST_MONGO or UNBLOCKD_TEST_MONGO_RS")
	def test_files_to_mongo_and_back(self):
		from tests.test_mongo import with_db
		url = with_db(MONGO, "ub_test_" + uuid.uuid4().hex[:8])
		self.addCleanup(lambda: mongo._client(url)[0].drop_database(mongo._client(url)[1].name))
		self.round_trip(url + "#pay")

	def test_a_target_with_a_board_is_refused_unless_forced(self):
		db = self.sqlite()
		connectors.migrate(str(self.src), db)
		with self.assertRaises(TicketError) as e:
			connectors.migrate(str(self.src), db)
		self.assertIn("--force", str(e.exception))
		other = self.tmp / "b" / "tickets"
		other.mkdir(parents=True)
		(other / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		Store(other).create({"title": "only this"}, prefix="X")
		connectors.migrate(str(other), db, force=True)  # replaces: the old board's tickets are gone
		self.assertEqual([t["id"] for t in Store(backend=connectors.open_backend(db)).list()], ["X-1"])
		folder = self.tmp / "c" / "tickets"
		folder.mkdir(parents=True)
		connectors.migrate(db, str(folder))
		with self.assertRaises(TicketError):
			connectors.migrate(str(self.src), str(folder))  # a folder that has a board is refused too
		connectors.migrate(str(self.src), str(folder), force=True)
		self.assertEqual(self.files_of(folder), self.files_of(self.src))

	def test_bad_moves_are_refused_before_anything_is_written(self):
		db = self.sqlite()
		with self.assertRaises(TicketError):
			connectors.migrate(str(self.src), str(self.src))
		with self.assertRaises(TicketError):
			connectors.migrate(str(self.tmp / "nowhere"), db)
		with self.assertRaises(TicketError):
			connectors.migrate(str(self.src), "sqlite:///" + (self.tmp / "x.db").as_posix())  # no #board
		with self.assertRaises(TicketError):
			connectors.migrate("sqlite:///" + (self.tmp / "x.db").as_posix() + "#none", str(self.src))  # no such board
		skill = self.src.parent / "SKILL.md"
		skill.write_text("# a skill", encoding="utf-8")
		(self.src / "unblockd.json").write_text(json.dumps({**CFG, "skills": {"s": {"path": "SKILL.md"}}}), encoding="utf-8")
		with self.assertRaises(TicketError) as e:
			connectors.migrate(str(self.src), db)
		self.assertIn("no project folder", str(e.exception))
		self.assertFalse((self.tmp / "db.sqlite").exists())  # refused before the target was even opened
		connectors.migrate(str(self.make_other()), db)
		with self.assertRaises(TicketError):
			connectors.migrate(str(self.src), db, force=True)
		self.assertEqual([t["id"] for t in Store(backend=connectors.open_backend(db)).list()], ["X-1"])  # --force wiped nothing

	def test_the_cli_moves_a_board(self):
		db = self.sqlite("cli")
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			self.assertEqual(cli.main(["migrate", "--from", str(self.src), "--to", db]), 0)
		self.assertEqual(json.loads(out.getvalue())["tickets"], 4)
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			self.assertEqual(cli.main(["migrate", "--from", str(self.src), "--to", db]), 1)
		self.assertIn("--force", err.getvalue())

	def test_locations_hide_the_password(self):
		self.assertEqual(connectors.describe("postgres://u:s3cret@h/db#b"), "postgres://u:***@h/db#b")
		self.assertEqual(connectors.describe("tickets"), "tickets")
		with self.assertRaises(TicketError) as e:
			connectors.split("postgres://u:s3cret@h/db")
		self.assertNotIn("s3cret", str(e.exception))


if __name__ == "__main__":
	unittest.main()
