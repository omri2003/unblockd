"""The MongoDB connector: the shared connector cases (tests/connector_cases.py) on a standalone server (no transactions:
safe write order) and on a replica set (one transaction per lock), plus Mongo's own: the lock document (stale takeover,
server clock), the schema guard, URLs and the optional driver.

UNBLOCKD_TEST_MONGO = a standalone server, UNBLOCKD_TEST_MONGO_RS = a replica set (CI: services), e.g.
mongodb://127.0.0.1:57018/ and mongodb://127.0.0.1:57017/?directConnection=true; each case uses its own database.
"""
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.connector_cases import ConnectorCases, in_thread
from unblockd import mongo
from unblockd.store import TicketError

STANDALONE = os.environ.get("UNBLOCKD_TEST_MONGO")
RS = os.environ.get("UNBLOCKD_TEST_MONGO_RS")


def with_db(url: str, name: str) -> str:
	"""url pointing at database `name` (keeps the query string)."""
	base, _, query = url.partition("?")
	base = base.rstrip("/")
	if base.count("/") > 2:
		base = base.rsplit("/", 1)[0]
	return f"{base}/{name}" + (f"?{query}" if query else "")


class MongoCases(ConnectorCases):
	mod = mongo
	server = None

	def database(self):
		url = with_db(self.server, "ub_test_" + uuid.uuid4().hex[:8])
		self.addCleanup(lambda: mongo._client(url)[0].drop_database(mongo._client(url)[1].name))
		return url

	def backend(self, board, create=True):
		return mongo.MongoBackend(self.url, board, create=create)

	def test_the_server_mode_is_detected(self):
		self.assertEqual(self.s.backend.transactions, self.atomic)

	def test_a_lock_left_by_a_crashed_worker_is_taken_over(self):
		s = self.s
		s.create({"title": "t"}, prefix="T")
		db, board = s.backend.db, self.boards[0]
		db.ub_locks.update_one({"_id": board}, {"$set": {"holder": "crashed", "until": datetime.now(timezone.utc) - timedelta(seconds=1)}}, upsert=True)
		self.assertEqual(s.update("T-1", {"title": "after"})["title"], "after")  # expired by the server's clock: taken over
		db.ub_locks.update_one({"_id": board}, {"$set": {"holder": "alive", "until": datetime.now(timezone.utc) + timedelta(seconds=60)}})
		with mock.patch.object(mongo, "BUSY_S", 0.5):
			with self.assertRaises(TicketError) as e:
				s.update("T-1", {"title": "blocked"})
		self.assertIn("try again", str(e.exception))
		db.ub_locks.update_one({"_id": board}, {"$set": {"holder": None}})
		self.assertEqual(s.update("T-1", {"title": "free"})["title"], "free")

	def test_a_database_from_a_newer_unblockd_is_refused(self):
		self.s.backend.db.ub_meta.update_one({"_id": "schema"}, {"$set": {"v": 99}})
		mongo.disconnect(self.url)
		try:
			with self.assertRaises(TicketError) as e:
				mongo.MongoBackend(self.url, "other")
			self.assertIn("newer unblockd", str(e.exception))
		finally:
			mongo.disconnect(self.url)
			cl = mongo._CLIENTS.get(self.url)
			self.assertIsNone(cl)
			import pymongo
			c = pymongo.MongoClient(self.url, serverSelectionTimeoutMS=5000)
			c.get_default_database().ub_meta.update_one({"_id": "schema"}, {"$set": {"v": mongo.SCHEMA}})
			c.close()


@unittest.skipUnless(STANDALONE, "set UNBLOCKD_TEST_MONGO to a standalone MongoDB URL")
class StandaloneCases(MongoCases, unittest.TestCase):
	server, atomic = STANDALONE, False


@unittest.skipUnless(RS, "set UNBLOCKD_TEST_MONGO_RS to a replica-set MongoDB URL")
class ReplicaSetCases(MongoCases, unittest.TestCase):
	server, atomic = RS, True


class UrlTest(unittest.TestCase):
	def test_urls(self):
		self.assertEqual(mongo.redact("mongodb://u:s3cret@db:27017/x"), "mongodb://u:***@db:27017/x")
		for bad in ("postgres://x/y", "mongo://x", ""):
			with self.assertRaises(TicketError):
				mongo.MongoBackend(bad, "b")
		with self.assertRaises(TicketError):
			mongo.MongoBackend("mongodb://127.0.0.1:1/x", "Bad Board")

	def test_mongo_without_the_driver_says_how_to_install_it(self):
		with mock.patch.dict(sys.modules, {"pymongo": None, "pymongo.errors": None}):
			with self.assertRaises(TicketError) as e:
				mongo.MongoBackend("mongodb://u:p@127.0.0.1:1/x", "b")
		self.assertIn("pip install pymongo", str(e.exception))

	def test_a_failed_connection_never_shows_the_password(self):
		try:
			import pymongo  # noqa: F401
		except ImportError:
			self.skipTest("pymongo not installed")
		with mock.patch.object(mongo, "CONNECT_MS", 300):
			with self.assertRaises(TicketError) as e:
				in_thread(lambda: mongo.MongoBackend("mongodb://u:hunter2@127.0.0.1:1/x", "b"))
		self.assertNotIn("hunter2", str(e.exception))
		self.assertIn("can't connect", str(e.exception))


if __name__ == "__main__":
	unittest.main()
