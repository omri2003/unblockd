"""The MongoDB connector: many boards in one Mongo database (`pip install pymongo`, imported only when a mongodb:// URL is
opened). Same methods as files.FileBackend, so store.Store runs on it unchanged.

Concurrency: the board lock is a document in ub_locks taken with ONE atomic update that matches only a free or expired lock
(expiry uses the server's clock, `$$NOW`, so machines with skewed clocks agree); a lock left by a crashed worker expires
after LOCK_STALE seconds, like the files connector's stale lock. Every write takes the board lock (a write made without
Store holding it takes it just for itself), so the board version and the event numbers only grow in commit order.
On a replica set or a sharded cluster everything Store does under the lock is also ONE transaction: it all commits or none
of it does (owner decision CORE-27 #1). On a standalone server (no transactions) each write applies on its own, in the order
Store makes them, still serialised by the lock: a crash halfway leaves the writes made so far ("safe write order").

Collections (prefix ub_): ub_meta (schema version), ub_boards {_id: board, config, version, evseq}, ub_locks, ub_tickets
{board, id, ver, t: <the ticket>}, ub_claims, ub_events {board, n, ts, e}, ub_files (attachment bytes in a binary field;
BSON caps a document at 16 MB) and ub_docs. Tickets are stored as native documents (Mongo keeps key order).
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .errors import TicketError

SCHEMA = 1
BUSY_S = 10  # how long a write waits for the board lock before "try again"
CONNECT_MS = 10_000  # how long to look for a server before giving up
LOCK_STALE = 30  # seconds: a lock older than this was left by a crash and may be taken over
BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
PAGE = 500

_CACHE: dict[tuple, tuple] = {}  # (board key, parse) -> (board version, {id: ver}, {id: ticket}); never filled once a lock has written (it could still abort)
_CLIENTS: dict[str, tuple] = {}  # url -> (MongoClient, database, transactions supported); one pool per process
_CLIENTS_LOCK = threading.Lock()
_LOCAL = threading.local()  # per thread and URL: [depth, session or None, lock token]


def redact(url: str) -> str:
	u = urlsplit(url)
	if u.password is None:
		return url
	return url.replace(f":{u.password}@", ":***@", 1)


def _client(url: str):
	with _CLIENTS_LOCK:
		hit = _CLIENTS.get(url)
		if hit:
			return hit
		try:
			import pymongo  # noqa: PLC0415 - optional: only boards on Mongo need it
			from pymongo.errors import PyMongoError  # noqa: PLC0415
		except ImportError:
			raise TicketError("this board is on MongoDB, which needs the pymongo package: pip install pymongo "
			                  "(the Docker image includes it)") from None
		def go():
			cl = None
			try:
				cl = pymongo.MongoClient(url, serverSelectionTimeoutMS=CONNECT_MS, tz_aware=True)
				return cl, cl.admin.command("hello"), cl.get_default_database(default="unblockd")
			except PyMongoError as e:
				if cl is not None:
					cl.close()
				raise TicketError(f"can't connect to {redact(url)}: {_first_line(e)}") from None
		from .connectors import reach  # noqa: PLC0415 - connectors imports this module lazily
		cl, hello, db = reach(url, go)
		tx = bool(hello.get("setName") or hello.get("msg") == "isdbgrid") and os.environ.get("UNBLOCKD_MONGO_TRANSACTIONS") != "0"
		_schema(db)
		_CLIENTS[url] = (cl, db, tx)
		return _CLIENTS[url]


def _first_line(e: Exception) -> str:
	return (str(e).strip().splitlines() or [type(e).__name__])[0][:300]


def _schema(db) -> None:
	from pymongo import ASCENDING  # noqa: PLC0415
	from pymongo.errors import DuplicateKeyError  # noqa: PLC0415
	try:
		db.ub_meta.insert_one({"_id": "schema", "v": SCHEMA})
	except DuplicateKeyError:
		v = db.ub_meta.find_one({"_id": "schema"})["v"]
		if v > SCHEMA:
			raise TicketError(f"this MongoDB database was set up by a newer unblockd (schema {v}, this one knows {SCHEMA}): upgrade unblockd") from None
	db.ub_tickets.create_index([("board", ASCENDING), ("id", ASCENDING)], unique=True)
	db.ub_claims.create_index([("board", ASCENDING), ("id", ASCENDING)], unique=True)
	db.ub_events.create_index([("board", ASCENDING), ("n", ASCENDING)], unique=True)
	db.ub_events.create_index([("board", ASCENDING), ("e.ticket", ASCENDING), ("n", ASCENDING)])
	db.ub_files.create_index([("board", ASCENDING), ("id", ASCENDING), ("name", ASCENDING)], unique=True)
	db.ub_docs.create_index([("board", ASCENDING), ("name", ASCENDING)], unique=True)


def disconnect(url: str | None = None) -> None:
	"""Close the process's client for url (all when None)."""
	with _CLIENTS_LOCK:
		for u in [url] if url else list(_CLIENTS):
			hit = _CLIENTS.pop(u, None)
			if hit:
				hit[0].close()


class MongoBackend:
	"""One board in a MongoDB database. url: mongodb://[user:pass@]host[:port]/[database] (default database: unblockd)."""
	kind = "mongo"

	def __init__(self, url: str, board: str, parse=lambda t: t, create: bool = True):
		if not BOARD_RE.match(board or ""):
			raise TicketError(f"bad board id {board!r}: a-z, 0-9, - _ (up to 40)")
		if not (url.startswith("mongodb://") or url.startswith("mongodb+srv://")):
			raise TicketError(f"unsupported database URL {redact(url)!r}: use mongodb://user:pass@host:27017/db or mongodb+srv://...")
		self.url, self.board, self.parse = url, board, parse
		self.key = f"{redact(url)}#{board}"
		self.dir = self.rt = None
		self._seen = None
		_, self.db, self.transactions = _client(url)
		if self.db.ub_boards.find_one({"_id": board}, {"_id": 1}) is None:
			if not create:
				raise TicketError(f"no board {board!r} in {redact(url)}")
			self.db.ub_boards.update_one({"_id": board}, {"$setOnInsert": {"config": None, "version": 0, "evseq": 0, "created": _now()}}, upsert=True)
		# the lock document exists before anyone takes it ($expr, needed for the server-clock expiry, is not allowed in an upsert)
		self.db.ub_locks.update_one({"_id": board}, {"$setOnInsert": {"holder": None, "until": datetime(1970, 1, 1, tzinfo=timezone.utc)}}, upsert=True)

	def project_root(self) -> Path | None:
		return None

	# ---------- the board lock + the transaction ----------
	def _state(self) -> list:
		st = getattr(_LOCAL, "st", None)
		if st is None:
			st = _LOCAL.st = {}
		return st.setdefault((self.url, self.board), [0, None, None, False])  # depth, session, lock token, dirty

	@property
	def _s(self):
		"""The session of the transaction this thread holds on this board, or None (standalone, or not in the lock)."""
		return self._state()[1]

	def _acquire(self) -> str:
		token, deadline = uuid.uuid4().hex, time.monotonic() + BUSY_S
		free = {"_id": self.board, "$or": [{"holder": None}, {"$expr": {"$lt": ["$until", "$$NOW"]}}]}
		take = [{"$set": {"holder": token, "until": {"$add": ["$$NOW", LOCK_STALE * 1000]}}}]
		while True:
			if self.db.ub_locks.update_one(free, take).modified_count:  # one atomic update: only a free or expired lock matches
				return token
			if self.db.ub_locks.find_one({"_id": self.board}, {"_id": 1}) is None:
				raise TicketError(f"board {self.board!r} was deleted from {redact(self.url)}")
			if time.monotonic() > deadline:
				raise TicketError("another claim is in progress; try again")
			time.sleep(0.02)

	def _release(self, token: str) -> None:
		self.db.ub_locks.update_one({"_id": self.board, "holder": token}, {"$set": {"holder": None}})

	@contextmanager
	def lock(self, steal=None):  # steal: files only (an expired lock document is taken over by the update itself)
		st = self._state()
		if st[0]:
			st[0] += 1
			try:
				yield
			finally:
				st[0] -= 1
			return
		token = self._acquire()
		st[0], st[2], st[3] = 1, token, False
		sess = None
		try:
			if self.transactions:
				sess = self.db.client.start_session()
				sess.start_transaction()
				st[1] = sess
			yield
			if sess is not None:
				sess.commit_transaction()
		except BaseException:
			if sess is not None and sess.in_transaction:
				sess.abort_transaction()
			raise
		finally:
			st[0], st[1], st[2], st[3] = 0, None, None, False
			if sess is not None:
				sess.end_session()
			self._release(token)

	_tx = lock  # every write takes the board lock (nested inside Store's when it holds it)

	def _bump(self) -> int:
		self._state()[3] = True
		from pymongo import ReturnDocument  # noqa: PLC0415
		return self.db.ub_boards.find_one_and_update({"_id": self.board}, {"$inc": {"version": 1}}, return_document=ReturnDocument.AFTER,
		                                             session=self._s)["version"]

	def _board_doc(self) -> dict:
		d = self.db.ub_boards.find_one({"_id": self.board}, session=self._s)
		if d is None:
			raise TicketError(f"board {self.board!r} was deleted from {redact(self.url)}")
		return d

	# ---------- config ----------
	def read_config(self) -> tuple[dict | None, str]:
		return self._board_doc().get("config"), f"board {self.board} settings"

	def write_config(self, raw: dict) -> None:
		with self._tx():
			self.db.ub_boards.update_one({"_id": self.board}, {"$set": {"config": raw}}, session=self._s)

	# ---------- tickets ----------
	def tickets(self) -> dict[str, dict]:
		st = self._state()
		dirty = bool(st[0] and st[3])  # same rule as sql.py: before this lock wrote anything, what it reads is committed
		ver = self._board_doc()["version"]
		self._seen = object() if dirty else ver
		hit = _CACHE.get((self.key, self.parse))
		if not dirty and hit and hit[0] == ver:  # nothing was written since: no need to scan the documents
			return hit[2]
		vers = {d["id"]: d["ver"] for d in self.db.ub_tickets.find({"board": self.board}, {"_id": 0, "id": 1, "ver": 1}, session=self._s)}
		old_v, old_t = hit[1:] if hit else ({}, {})
		need = [i for i, v in vers.items() if old_v.get(i) != v]
		got = {}
		for k in range(0, len(need), PAGE):
			for d in self.db.ub_tickets.find({"board": self.board, "id": {"$in": need[k:k + PAGE]}}, {"_id": 0, "id": 1, "ver": 1, "t": 1}, session=self._s):
				got[d["id"]] = (d["ver"], self.parse(d["t"]))
		new_v, new_t = {}, {}
		for i in sorted(vers):
			if i in got:
				new_v[i], new_t[i] = got[i]
			elif old_v.get(i) == vers[i]:
				new_v[i], new_t[i] = vers[i], old_t[i]
		if not dirty:
			_CACHE[(self.key, self.parse)] = (ver, new_v, new_t)  # the version read BEFORE the documents
		return new_t

	def version(self):
		return self._seen if self._seen is not None else object()

	def exists(self, tid: str) -> bool:
		return self.db.ub_tickets.find_one({"board": self.board, "id": tid}, {"_id": 1}, session=self._s) is not None

	def read(self, tid: str) -> dict | None:
		d = self.db.ub_tickets.find_one({"board": self.board, "id": tid}, {"t": 1}, session=self._s)
		return self.parse(d["t"]) if d else None

	def write(self, t: dict) -> None:
		with self._tx():
			v = self._bump()
			self.db.ub_tickets.update_one({"board": self.board, "id": t["id"]}, {"$set": {"t": t, "ver": v}}, upsert=True, session=self._s)

	def delete(self, tid: str) -> None:
		with self._tx():
			self.db.ub_tickets.delete_one({"board": self.board, "id": tid}, session=self._s)
			self.db.ub_files.delete_many({"board": self.board, "id": tid}, session=self._s)
			self._bump()

	def numbers(self, prefix: str) -> list[int]:
		pat = re.compile(rf"^{re.escape(prefix)}-(\d+)")
		ids = self.db.ub_tickets.find({"board": self.board, "id": {"$regex": f"^{re.escape(prefix)}-"}}, {"_id": 0, "id": 1}, session=self._s)
		return [int(m[1]) for d in ids if (m := pat.match(d["id"]))]

	# ---------- claims ----------
	def claims(self) -> dict:
		return {d["id"]: d["c"] for d in self.db.ub_claims.find({"board": self.board}, {"_id": 0}, session=self._s).sort("id")}

	def save_claims(self, claims: dict) -> None:
		with self._tx():
			cur = {d["id"]: d["c"] for d in self.db.ub_claims.find({"board": self.board}, {"_id": 0}, session=self._s)}
			if cur == claims:
				return
			gone = [i for i in cur if i not in claims]
			if gone:
				self.db.ub_claims.delete_many({"board": self.board, "id": {"$in": gone}}, session=self._s)
			for i, c in claims.items():
				if cur.get(i) != c:
					self.db.ub_claims.update_one({"board": self.board, "id": i}, {"$set": {"c": c}}, upsert=True, session=self._s)
			self._bump()

	# ---------- activity ----------
	def append_event(self, line: dict, locked=None, cap=None) -> None:
		from pymongo import ReturnDocument  # noqa: PLC0415
		with self._tx():
			n = self.db.ub_boards.find_one_and_update({"_id": self.board}, {"$inc": {"evseq": 1}}, return_document=ReturnDocument.AFTER,
			                                          session=self._s)["evseq"]
			self.db.ub_events.insert_one({"board": self.board, "n": n, "ts": line.get("ts", ""), "e": line}, session=self._s)

	def events_newest_first(self, ticket: str | None = None):
		"""Every event (only one ticket's: ticket=), newest first, a page at a time."""
		below, one = None, ({"e.ticket": ticket} if ticket else {})
		while True:
			q = {"board": self.board, **one} if below is None else {"board": self.board, **one, "n": {"$lt": below}}
			rows = list(self.db.ub_events.find(q, {"_id": 0, "n": 1, "e": 1}, session=self._s).sort("n", -1).limit(PAGE))
			for r in rows:
				yield r["e"]
			if len(rows) < PAGE:
				return
			below = rows[-1]["n"]

	def events_after(self, cursor):
		if cursor is None:
			return [], self._board_doc()["evseq"]
		rows = list(self.db.ub_events.find({"board": self.board, "n": {"$gt": cursor}}, {"_id": 0, "n": 1, "e": 1}, session=self._s).sort("n", 1))
		return [r["e"] for r in rows], (rows[-1]["n"] if rows else cursor)

	# ---------- attachments ----------
	def attachment_names(self) -> dict[str, list[str]]:
		out: dict[str, list[str]] = {}
		for d in self.db.ub_files.find({"board": self.board}, {"_id": 0, "id": 1, "name": 1}, session=self._s).sort([("id", 1), ("name", 1)]):
			out.setdefault(d["id"], []).append(d["name"])
		return out

	def list_attachments(self, tid: str) -> list[dict]:
		return [{"name": d["name"], "bytes": d["size"]}
		        for d in self.db.ub_files.find({"board": self.board, "id": tid}, {"_id": 0, "name": 1, "size": 1}, session=self._s).sort("name")]

	def put_attachment(self, tid: str, name: str, data: bytes) -> None:
		with self._tx():
			self.db.ub_files.update_one({"board": self.board, "id": tid, "name": name}, {"$set": {"data": data, "size": len(data)}}, upsert=True, session=self._s)
			self._bump()

	def get_attachment(self, tid: str, name: str) -> bytes | None:
		d = self.db.ub_files.find_one({"board": self.board, "id": tid, "name": name}, {"data": 1}, session=self._s)
		return bytes(d["data"]) if d else None

	def delete_attachment(self, tid: str, name: str) -> bool:
		with self._tx():
			if self.db.ub_files.delete_one({"board": self.board, "id": tid, "name": name}, session=self._s).deleted_count == 0:
				return False
			self._bump()
			return True

	# ---------- small runtime documents ----------
	def get_doc(self, name: str) -> dict:
		d = self.db.ub_docs.find_one({"board": self.board, "name": name}, {"d": 1}, session=self._s)
		return d["d"] if d else {}

	def put_doc(self, name: str, doc: dict) -> None:
		with self._tx():
			self.db.ub_docs.update_one({"board": self.board, "name": name}, {"$set": {"d": doc}}, upsert=True, session=self._s)

	# ---------- a whole board at once (migrate) ----------
	def bulk_load(self, cfg: dict | None, tickets: list[dict], files: list[tuple], events: list[dict]) -> None:
		"""Write a board into this EMPTY one under the board lock with batched inserts (no transaction: a long copy would pass
		the server's transaction time limit; migrate checks the result). The lock is renewed between batches."""
		from pymongo import ReturnDocument  # noqa: PLC0415
		token = self._acquire()

		def renew():
			self.db.ub_locks.update_one({"_id": self.board, "holder": token}, [{"$set": {"until": {"$add": ["$$NOW", LOCK_STALE * 1000]}}}])
		try:
			if cfg is not None:
				self.db.ub_boards.update_one({"_id": self.board}, {"$set": {"config": cfg}})
			v = self.db.ub_boards.find_one_and_update({"_id": self.board}, {"$inc": {"version": 1}}, return_document=ReturnDocument.AFTER)["version"]
			for k in range(0, len(tickets), 1000):
				self.db.ub_tickets.insert_many([{"board": self.board, "id": t["id"], "ver": v, "t": t} for t in tickets[k:k + 1000]])
				renew()
			for i, n, d in files:
				self.db.ub_files.insert_one({"board": self.board, "id": i, "name": n, "data": d, "size": len(d)})
			n0 = self.db.ub_boards.find_one_and_update({"_id": self.board}, {"$inc": {"evseq": len(events)}})["evseq"]  # the document BEFORE
			for k in range(0, len(events), 1000):
				self.db.ub_events.insert_many([{"board": self.board, "n": n0 + j, "ts": e.get("ts", ""), "e": e}
				                               for j, e in enumerate(events[k:k + 1000], k + 1)])
				renew()
		finally:
			self._release(token)

	# ---------- the board itself ----------
	def drop(self) -> None:
		"""Delete the board and everything it has (in one transaction where the server has them)."""
		with self._tx():
			for c in ("ub_tickets", "ub_claims", "ub_events", "ub_files", "ub_docs"):
				self.db[c].delete_many({"board": self.board}, session=self._s)
			self.db.ub_boards.delete_one({"_id": self.board}, session=self._s)
		self.db.ub_locks.delete_one({"_id": self.board})
		for ck in [k for k in _CACHE if k[0] == self.key]:
			_CACHE.pop(ck, None)


def boards(url: str) -> list[str]:
	_, db, _ = _client(url)
	return sorted(d["_id"] for d in db.ub_boards.find({}, {"_id": 1}))


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")
