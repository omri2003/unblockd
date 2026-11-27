"""Built-in database upgrades + `db status` / `db upgrade` (CORE-38): an older database is upgraded on start, all or nothing
on SQL; a newer one is refused; UNBLOCKD_AUTO_UPGRADE=0 leaves it to `db upgrade`. SQLite always; Postgres / MongoDB when
UNBLOCKD_TEST_PG / UNBLOCKD_TEST_MONGO(_RS) are set; the Mongo step runner also against a small fake."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd import cli, sql, upgrade
from unblockd import mongo as mg
from unblockd import system as sy
from unblockd.errors import TicketError
from unblockd.store import Store

PG = os.environ.get("UNBLOCKD_TEST_PG")
MONGO = os.environ.get("UNBLOCKD_TEST_MONGO_RS") or os.environ.get("UNBLOCKD_TEST_MONGO")
EXTRA = "CREATE TABLE ub_extra (x TEXT); INSERT INTO ub_extra (x) VALUES ('{NOW}')"


class SqlUpgradeTest(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		self.addCleanup(sql.disconnect)
		self.tmp = Path(tmp.name)
		self.file = self.tmp / "b.db"
		self.url = "sqlite:///" + self.file.as_posix()
		Store(self.url + "#pay").create({"title": "made by schema 1"}, prefix="PAY")  # a database an older unblockd made
		sql.disconnect()
		env = mock.patch.dict(os.environ, {upgrade.ENV: ""})
		env.start()
		self.addCleanup(env.stop)

	def raw(self, q):
		db = sqlite3.connect(self.file)
		try:
			return db.execute(q).fetchall()
		finally:
			db.close()

	def test_an_older_database_upgrades_on_start(self):
		err = io.StringIO()
		with mock.patch.object(sql, "STEPS", sql.STEPS + [EXTRA]), contextlib.redirect_stderr(err):
			self.assertEqual(Store(self.url + "#pay").list()[0]["title"], "made by schema 1")
		self.assertEqual(self.raw("SELECT v FROM ub_meta WHERE k = 'schema'"), [(str(sql.SCHEMA + 1),)])
		self.assertEqual(len(self.raw("SELECT x FROM ub_extra")), 1)
		self.assertIn(f"upgraded {self.url} from schema 1 to {sql.SCHEMA + 1}", err.getvalue())

	def test_all_or_nothing(self):
		bad = "CREATE TABLE ub_extra (x TEXT); CREATE TABLE ub_extra (x TEXT)"  # the second statement fails
		with mock.patch.object(sql, "STEPS", sql.STEPS + [bad]), self.assertRaises(Exception):
			Store(self.url + "#pay")
		self.assertEqual(self.raw("SELECT v FROM ub_meta WHERE k = 'schema'"), [("1",)])
		self.assertEqual(self.raw("SELECT name FROM sqlite_master WHERE name = 'ub_extra'"), [])
		with mock.patch.object(sql, "STEPS", sql.STEPS + [EXTRA]), contextlib.redirect_stderr(io.StringIO()):  # the refused connection was not kept
			Store(self.url + "#pay")
		self.assertEqual(self.raw("SELECT v FROM ub_meta WHERE k = 'schema'"), [("2",)])

	def test_a_newer_database_is_refused(self):
		self.raw("SELECT 1")
		db = sqlite3.connect(self.file)
		db.execute("UPDATE ub_meta SET v = '7' WHERE k = 'schema'")
		db.commit()
		db.close()
		with self.assertRaisesRegex(TicketError, r"set up by a newer unblockd \(schema 7, this one knows 1\): upgrade unblockd"):
			Store(self.url + "#pay")
		self.assertIn("newer", sql.schema(self.url)["error"])  # status reports it instead

	def test_automatic_off_then_db_upgrade(self):
		with mock.patch.dict(os.environ, {upgrade.ENV: "0"}), mock.patch.object(sql, "STEPS", sql.STEPS + [EXTRA]):
			with self.assertRaisesRegex(TicketError, "schema 1 and this unblockd needs 2; automatic upgrades are off .*db upgrade"):
				Store(self.url + "#pay")
			fresh = "sqlite:///" + (self.tmp / "new.db").as_posix()
			self.assertEqual(Store(fresh + "#n").list(), [])  # a new database is set up either way
			self.assertEqual(sql.schema(self.url), {"database": self.url, "kind": "sqlite", "schema": 1, "latest": 2})
			out = io.StringIO()
			with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
				self.assertEqual(cli.main(["db", "upgrade", "--url", self.url]), 0)
			self.assertEqual(json.loads(out.getvalue())[0]["upgraded_from"], 1)
			self.assertEqual(len(Store(self.url + "#pay").list()), 1)
		self.assertIn("newer unblockd", sql.schema(self.url)["error"])  # steps unpatched again: the database is now ahead of this code

	def test_cli_status_lists_every_database(self):
		sysfile = self.tmp / "sys" / "system.db"
		missing = "sqlite:///" + (self.tmp / "never.db").as_posix()
		env = {"UNBLOCKD_SYSTEM_DB": str(sysfile), "UNBLOCKD_BOARDS": os.pathsep.join([self.url + "#pay", missing + "#x"]), "TICKETS_DIR": "", "UNBLOCKD_ROOT": ""}
		out = io.StringIO()
		with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
			self.assertEqual(cli.main(["db", "status"]), 0)
		rows = json.loads(out.getvalue())
		self.assertEqual([r["schema"] for r in rows], [0, 1, 0])
		self.assertIn("not created yet", rows[0]["note"])
		self.assertFalse(sysfile.exists())  # status changes nothing
		self.assertFalse((self.tmp / "never.db").exists())
		with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(cli.main(["db", "upgrade"]), 0)
		self.assertTrue(sysfile.exists())
		sy.System(str(sysfile)).close()


class SystemUpgradeTest(unittest.TestCase):
	def test_system_database_upgrades_and_refuses_newer(self):
		with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
			s = sy.System(str(Path(d) / "s.db"))
			s.info()
			s.close()
			step = "CREATE TABLE us_users (org TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (org, id))"  # how CORE-69 adds users
			with mock.patch.object(sy, "STEPS", sy.STEPS + [step]), contextlib.redirect_stderr(io.StringIO()):
				self.assertEqual(s.info()["boards"], 0)
				s.c.q("SELECT org, id FROM us_users")
				s.close()
			with self.assertRaisesRegex(TicketError, "the system database .* newer unblockd"):
				s.info()
			s.close()


class _Coll:
	def __init__(self):
		self.docs = {}

	def find_one(self, q):
		return self.docs.get(q["_id"])

	def update_one(self, q, u, upsert=False):
		d = self.docs.setdefault(q["_id"], {"_id": q["_id"], "v": 0})
		d["v"] = max(d["v"], u["$max"]["v"])


class _Db:
	def __init__(self):
		self.ub_meta = _Coll()
		self.ran = []


class MongoRunnerTest(unittest.TestCase):
	def test_steps_one_at_a_time(self):
		db = _Db()
		steps = [lambda d: d.ran.append(1), lambda d: d.ran.append(2)]
		self.assertEqual(upgrade.mongo(db, steps, "m")["schema"], 2)
		self.assertEqual(db.ran, [1, 2])
		db.ub_meta.docs["schema"]["v"] = 1
		boom = [lambda d: d.ran.append(1), lambda d: d.ran.append(2), lambda d: 1 / 0]
		with self.assertRaises(ZeroDivisionError):
			upgrade.mongo(db, boom, "m")
		self.assertEqual(db.ub_meta.docs["schema"]["v"], 2)  # the steps that finished count: the next start resumes at step 3
		with mock.patch.dict(os.environ, {upgrade.ENV: "0"}):
			with self.assertRaisesRegex(TicketError, "automatic upgrades are off"):
				upgrade.mongo(db, boom, "m")
		db.ub_meta.docs["schema"]["v"] = 9
		with self.assertRaisesRegex(TicketError, "newer unblockd"):
			upgrade.mongo(db, steps, "m")
		self.assertIn("newer", upgrade.mongo(db, steps, "m", "status")["error"])


@unittest.skipUnless(PG, "set UNBLOCKD_TEST_PG to a Postgres URL")
class PostgresUpgradeTest(unittest.TestCase):
	def test_status_and_upgrade(self):
		sql.connect(PG)
		self.addCleanup(sql.disconnect)
		self.assertEqual(sql.schema(PG)["kind"], "postgres")
		self.assertEqual(sql.schema(PG, "upgrade")["schema"], sql.SCHEMA)


@unittest.skipUnless(MONGO, "set UNBLOCKD_TEST_MONGO to a MongoDB URL")
class MongoUpgradeTest(unittest.TestCase):
	def test_status(self):
		self.assertEqual(mg.schema(MONGO, "upgrade")["schema"], mg.SCHEMA)
		self.assertEqual(mg.schema(MONGO)["kind"], "mongo")


if __name__ == "__main__":
	unittest.main()
