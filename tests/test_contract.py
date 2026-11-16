"""The connector contract (tests/contract.py) run against every connector available here.

Postgres runs when UNBLOCKD_TEST_PG is a database URL the tests may write to (CI: a postgres service), e.g.
UNBLOCKD_TEST_PG=postgres://postgres:test@127.0.0.1:55432/postgres; it needs the psycopg package. MongoDB runs with
UNBLOCKD_TEST_MONGO (standalone) / UNBLOCKD_TEST_MONGO_RS (replica set), see tests/test_mongo.py; it needs pymongo.
"""
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.contract import BoardContract
from unblockd import mongo, sql
from unblockd.store import Store

PG = os.environ.get("UNBLOCKD_TEST_PG")
MONGO = os.environ.get("UNBLOCKD_TEST_MONGO")  # a standalone server
MONGO_RS = os.environ.get("UNBLOCKD_TEST_MONGO_RS")  # a replica set


class FilesContract(BoardContract, unittest.TestCase):
	"""Files: a tickets folder; every Store is a fresh handle on it, sharing the lock file like separate processes."""
	def open_board(self, cfg):
		tmp = tempfile.TemporaryDirectory()
		self.addCleanup(tmp.cleanup)
		d = Path(tmp.name) / "tickets"
		d.mkdir()
		(d / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
		self.dir = d
		return lambda: Store(d)

	def backdate_claim(self, tid):
		p = self.dir / ".unblockd" / "claims.json"
		c = json.loads(p.read_text(encoding="utf-8"))
		c[tid]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		p.write_text(json.dumps(c), encoding="utf-8")


class SqlContract(BoardContract):
	"""SQL: every Store opens its own backend; each worker thread has its own database connection, like separate processes."""
	def database(self) -> str:  # pragma: no cover - each driver implements it
		raise NotImplementedError

	def open_board(self, cfg):
		self.url, self.board = self.database(), "b" + uuid.uuid4().hex[:12]
		first = sql.SqlBackend(self.url, self.board)
		first.write_config(cfg)
		self.addCleanup(lambda: (sql.SqlBackend(self.url, self.board).drop(), sql.disconnect(self.url)))
		return lambda: Store(backend=sql.SqlBackend(self.url, self.board, create=False))

	def backdate_claim(self, tid):
		b = sql.SqlBackend(self.url, self.board, create=False)
		c = b.claims()
		c[tid]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		b.save_claims(c)


class SqliteContract(SqlContract, unittest.TestCase):
	def database(self):
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		return "sqlite:///" + (Path(tmp.name) / "board.db").as_posix()


@unittest.skipUnless(PG, "set UNBLOCKD_TEST_PG to a Postgres URL to run the contract on Postgres")
class PostgresContract(SqlContract, unittest.TestCase):
	def database(self):
		return PG



class MongoContract(BoardContract):
	"""MongoDB: one client per process (it pools connections); each Store has its own backend, each thread its own lock state."""
	server = None

	def open_board(self, cfg):
		from tests.test_mongo import with_db
		self.url, self.board = with_db(self.server, "ub_test_" + uuid.uuid4().hex[:8]), "b"
		mongo.MongoBackend(self.url, self.board).write_config(cfg)
		self.addCleanup(lambda: mongo._client(self.url)[0].drop_database(mongo._client(self.url)[1].name))
		return lambda: Store(backend=mongo.MongoBackend(self.url, self.board, create=False))

	def backdate_claim(self, tid):
		b = mongo.MongoBackend(self.url, self.board, create=False)
		c = b.claims()
		c[tid]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		b.save_claims(c)


@unittest.skipUnless(MONGO, "set UNBLOCKD_TEST_MONGO to a standalone MongoDB URL")
class MongoStandaloneContract(MongoContract, unittest.TestCase):
	server = MONGO


@unittest.skipUnless(MONGO_RS, "set UNBLOCKD_TEST_MONGO_RS to a replica-set MongoDB URL")
class MongoReplicaSetContract(MongoContract, unittest.TestCase):
	server = MONGO_RS


if __name__ == "__main__":
	unittest.main()
