"""The SQL connector: the shared connector cases (tests/connector_cases.py) plus what is SQL's own (schema guard, URLs,
the optional driver). SQLite always; Postgres too when UNBLOCKD_TEST_PG is a database URL the tests may write to."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.connector_cases import ConnectorCases, in_thread
from unblockd import sql
from unblockd.store import TicketError

PG = os.environ.get("UNBLOCKD_TEST_PG")


class SqlCases(ConnectorCases):
	mod = sql

	def backend(self, board, create=True):
		return sql.SqlBackend(self.url, board, create=create)

	def test_a_database_from_a_newer_unblockd_is_refused(self):
		sql.connect(self.url).q("UPDATE ub_meta SET v = '99' WHERE k = 'schema'")
		try:
			with self.assertRaises(TicketError) as e:
				in_thread(lambda: sql.SqlBackend(self.url, "other"))
			self.assertIn("newer unblockd", str(e.exception))
		finally:
			sql.connect(self.url).q("UPDATE ub_meta SET v = ? WHERE k = 'schema'", (str(sql.SCHEMA),))


class SqliteCases(SqlCases, unittest.TestCase):
	def database(self):
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		return "sqlite:///" + (Path(tmp.name) / "b.db").as_posix()


@unittest.skipUnless(PG, "set UNBLOCKD_TEST_PG to a Postgres URL")
class PostgresCases(SqlCases, unittest.TestCase):
	def database(self):
		return PG


class UrlTest(unittest.TestCase):
	def test_urls(self):
		self.assertEqual(sql.redact("postgres://u:s3cret@db:5432/x"), "postgres://u:***@db:5432/x")
		self.assertEqual(sql.redact("sqlite:///a.db"), "sqlite:///a.db")
		for bad in ("mysql://x/y", "sqlite:a.db", "", "/tmp/a.db"):
			with self.assertRaises(TicketError):
				sql.SqlBackend(bad, "b")
		with self.assertRaises(TicketError):
			sql.SqlBackend("sqlite:///x.db", "Bad Board")

	def test_postgres_without_the_driver_says_how_to_install_it(self):
		with mock.patch.dict(sys.modules, {"psycopg": None}):
			with self.assertRaises(TicketError) as e:
				in_thread(lambda: sql.SqlBackend("postgres://u:p@127.0.0.1:1/x", "b"))
		self.assertIn("pip install", str(e.exception))
		self.assertNotIn(":p@", str(e.exception))

	def test_a_failed_connection_never_shows_the_password(self):
		try:
			import psycopg  # noqa: F401
		except ImportError:
			self.skipTest("psycopg not installed")
		with self.assertRaises(TicketError) as e:
			in_thread(lambda: sql.SqlBackend("postgres://u:hunter2@127.0.0.1:1/x", "b"))
		self.assertNotIn("hunter2", str(e.exception))


if __name__ == "__main__":
	unittest.main()
