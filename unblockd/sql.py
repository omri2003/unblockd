"""The SQL connector: many boards in one SQL database, SQLite (stdlib) or Postgres (`pip install psycopg[binary]`, imported
only when a postgres:// URL is opened). Same methods as files.FileBackend, so store.Store runs on it unchanged.

Concurrency: the board lock (`lock()`) is a database transaction holding the board's row (Postgres: SELECT ... FOR UPDATE on
ub_boards, so one board's lock never blocks another board; SQLite: BEGIN IMMEDIATE, which holds the whole database file for
the moment). Everything Store does while it holds the lock runs in that one transaction: it commits together, or a failure
rolls all of it back (owner decision CORE-27 #1: all-or-nothing). A lock dies with its connection, so there is no stale-lock
stealing: a crashed worker's lock is released by the database. Writes made without the lock (an event, an attachment) are
short transactions of their own that also take the board row, so the per-board version and event number only ever grow in
commit order (notify can read events by number without missing one that committed late).

Tables (prefix ub_, so they can share a database with others): ub_meta (schema version), ub_boards (config JSON, version =
bumped by every ticket / claim / attachment write, evseq = last event number), ub_tickets (one JSON document per ticket),
ub_claims, ub_events, ub_files (attachments, bytes in the database), ub_docs (small runtime documents, e.g. the PR cache).
Tickets are stored as JSON text, not jsonb: the document comes back byte for byte as written (key order kept), like a file.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import TicketError

SCHEMA = 1  # bump with a migration step in _migrate (CORE-38)
BUSY_S = 10  # how long a write waits for the board lock before "try again", like the files connector
CONNECT_S = 10  # seconds to reach a Postgres server
PAGE = 500

# Per process: the ticket documents read so far per board ((board key, parse) -> (board version, {id: ver}, {id: parsed ticket})):
# an unchanged board version returns it as is; otherwise only (id, ver) is read and the rows whose ver changed are fetched.
# Never filled once the transaction holding the lock has written (it might still roll back): see tickets().
_CACHE: dict[tuple, tuple] = {}
_LOCAL = threading.local()  # this thread's open connections, by URL: every Store in a thread shares them

TABLES = """
CREATE TABLE IF NOT EXISTS ub_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ub_boards (board TEXT PRIMARY KEY, config TEXT, version BIGINT NOT NULL DEFAULT 0,
	evseq BIGINT NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ub_tickets (board TEXT NOT NULL, id TEXT NOT NULL, doc TEXT NOT NULL, ver BIGINT NOT NULL,
	PRIMARY KEY (board, id));
CREATE TABLE IF NOT EXISTS ub_claims (board TEXT NOT NULL, id TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (board, id));
CREATE TABLE IF NOT EXISTS ub_events (board TEXT NOT NULL, n BIGINT NOT NULL, ts TEXT NOT NULL, ticket TEXT, doc TEXT NOT NULL,
	PRIMARY KEY (board, n));
CREATE TABLE IF NOT EXISTS ub_files (board TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, data {BLOB} NOT NULL,
	size BIGINT NOT NULL, PRIMARY KEY (board, id, name));
CREATE TABLE IF NOT EXISTS ub_docs (board TEXT NOT NULL, name TEXT NOT NULL, doc TEXT NOT NULL, PRIMARY KEY (board, name));
CREATE INDEX IF NOT EXISTS ub_events_ts ON ub_events (board, ts);
CREATE INDEX IF NOT EXISTS ub_events_ticket ON ub_events (board, ticket, n);
"""
BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def redact(url: str) -> str:
	"""A database URL with its password replaced, safe to show or log."""
	u = urlsplit(url)
	if u.password is None:
		return url
	return url.replace(f":{u.password}@", ":***@", 1)


class _Conn:
	"""One DB-API connection plus the transaction depth (lock() and _tx() nest; only the outermost begins and commits)."""
	def __init__(self, url: str):
		self.url, self.depth, self.dirty = url, 0, False  # dirty: this transaction changed tickets / claims / attachments
		if url.startswith("sqlite:"):
			self.kind = "sqlite"
			path = url[len("sqlite:///"):]  # sqlite:///C:/x.db on Windows, sqlite:////var/x.db = /var/x.db
			self.db = sqlite3.connect(unquote(path), timeout=BUSY_S, isolation_level=None, check_same_thread=False)
			self.db.execute("PRAGMA journal_mode=WAL")  # readers never wait for the writer
			self.db.execute("PRAGMA synchronous=NORMAL")
			self.db.execute(f"PRAGMA busy_timeout={BUSY_S * 1000}")
		else:
			self.kind = "postgres"
			try:
				import psycopg  # noqa: PLC0415 - optional: only boards on Postgres need it
			except ImportError:
				raise TicketError("this board is on Postgres, which needs the psycopg package: pip install \"psycopg[binary]\" "
				                  "(the Docker image includes it)") from None
			def go():
				try:
					return psycopg.connect(url, autocommit=True, connect_timeout=CONNECT_S)
				except psycopg.Error as e:
					raise TicketError(f"can't connect to {redact(url)}: {str(e).strip().splitlines()[0]}") from None
			from .connectors import reach  # noqa: PLC0415 - connectors imports this module lazily
			self.db = reach(url, go)

	def __del__(self):  # a thread's connections go when the thread does (threading.local drops them)
		try:
			self.db.close()
		except Exception:  # noqa: BLE001 - interpreter shutdown, already closed
			pass

	def many(self, sql: str, rows: list[tuple]) -> None:
		if not rows:
			return
		if self.kind == "postgres":
			with self.db.cursor() as cur:
				cur.executemany(sql.replace("?", "%s"), rows)
		else:
			self.db.executemany(sql, rows)

	def q(self, sql: str, args=()) -> list[tuple]:
		if self.kind == "postgres":
			sql = sql.replace("?", "%s")
		try:
			cur = self.db.execute(sql, args)
			return cur.fetchall() if cur.description else []
		except Exception as e:  # noqa: BLE001 - mapped to the store's error when it is a lock wait
			if _busy(e):
				raise TicketError("another claim is in progress; try again") from None
			raise

	def begin(self, lock_board: str | None = None) -> None:
		if self.kind == "sqlite":
			self.q("BEGIN IMMEDIATE")  # the database's write lock, waited for up to busy_timeout
		else:
			self.q("BEGIN")
			self.q(f"SET LOCAL lock_timeout = '{BUSY_S}s'")
		if lock_board is not None and self.kind == "postgres":
			self.q("SELECT board FROM ub_boards WHERE board = ? FOR UPDATE", (lock_board,))

	def end(self, ok: bool) -> None:
		self.db.execute("COMMIT" if ok else "ROLLBACK")


def _busy(e: Exception) -> bool:
	if isinstance(e, sqlite3.OperationalError):
		return "locked" in str(e) or "busy" in str(e)
	return type(e).__name__ in ("LockNotAvailable", "QueryCanceled")


def connect(url: str) -> _Conn:
	"""This thread's connection to `url` (opened on first use)."""
	conns = getattr(_LOCAL, "conns", None)
	if conns is None:
		conns = _LOCAL.conns = {}
	c = conns.get(url)
	if c is None:
		c = conns[url] = _Conn(url)
		_schema(c)
	return c


def disconnect(url: str | None = None) -> None:
	"""Close this thread's connection to url (all of them when None)."""
	conns = getattr(_LOCAL, "conns", {})
	for u in [url] if url else list(conns):
		c = conns.pop(u, None)
		if c is not None:
			c.db.close()


def _schema(c: _Conn) -> None:
	ddl = TABLES.replace("{BLOB}", "BLOB" if c.kind == "sqlite" else "BYTEA")
	c.begin()
	ok = False
	try:
		for stmt in filter(str.strip, ddl.split(";")):
			c.q(stmt)
		row = c.q("SELECT v FROM ub_meta WHERE k = 'schema'")
		if not row:
			c.q("INSERT INTO ub_meta (k, v) VALUES ('schema', ?)", (str(SCHEMA),))
		elif int(row[0][0]) > SCHEMA:
			raise TicketError(f"{redact(c.url)} was set up by a newer unblockd (schema {row[0][0]}, this one knows {SCHEMA}): upgrade unblockd")
		ok = True
	finally:
		c.end(ok)


class SqlBackend:
	"""One board in a SQL database. url: sqlite:///<path> or postgres://user:pass@host/db; board: its id in that database."""
	kind = "sql"

	def __init__(self, url: str, board: str, parse=lambda t: t, create: bool = True):
		if not BOARD_RE.match(board or ""):
			raise TicketError(f"bad board id {board!r}: a-z, 0-9, - _ (up to 40)")
		if url.startswith("postgresql://"):
			url = "postgres://" + url[len("postgresql://"):]
		if not (url.startswith("sqlite:///") or url.startswith("postgres://")):
			raise TicketError(f"unsupported database URL {redact(url)!r}: use sqlite:///path/to/file.db or postgres://user:pass@host/db")
		self.url, self.board, self.parse = url, board, parse
		self.key = f"{redact(url)}#{board}"
		self.dir = self.rt = None
		self._seen = None  # the board version read with the last tickets(): what version() reports
		c = self.c
		if not c.q("SELECT 1 FROM ub_boards WHERE board = ?", (board,)):
			if not create:
				raise TicketError(f"no board {board!r} in {redact(url)}")
			with self._tx():
				c.q("INSERT INTO ub_boards (board, config, created) VALUES (?, NULL, ?) ON CONFLICT (board) DO NOTHING",
				    (board, _now()))

	@property
	def c(self) -> _Conn:
		return connect(self.url)

	def project_root(self) -> Path | None:
		return None  # no repo linked (docs / architecture / file suggestions come with CORE-36)

	@contextmanager
	def _tx(self, lock: bool = False):
		"""A transaction: the outermost begins (taking the board row when lock) and commits, or rolls back on an error."""
		c = self.c
		if c.depth:
			c.depth += 1
			try:
				yield c
			finally:
				c.depth -= 1
			return
		c.begin(self.board if lock else None)
		c.depth, c.dirty = 1, False
		ok = False
		try:
			if not lock and c.kind == "postgres":  # a write without the lock still queues behind the holder (ordered versions / events)
				c.q("SELECT board FROM ub_boards WHERE board = ? FOR UPDATE", (self.board,))
			yield c
			ok = True
		finally:
			c.depth, c.dirty = 0, False
			c.end(ok)

	def _bump(self, c: _Conn) -> int:
		c.dirty = True
		c.q("UPDATE ub_boards SET version = version + 1 WHERE board = ?", (self.board,))
		return c.q("SELECT version FROM ub_boards WHERE board = ?", (self.board,))[0][0]

	# ---------- config ----------
	def read_config(self) -> tuple[dict | None, str]:
		row = self.c.q("SELECT config FROM ub_boards WHERE board = ?", (self.board,))
		where = f"board {self.board} settings"
		if not row:
			raise TicketError(f"board {self.board!r} was deleted from {redact(self.url)}")
		return (json.loads(row[0][0]) if row[0][0] else None), where

	def write_config(self, raw: dict) -> None:
		with self._tx() as c:
			c.q("UPDATE ub_boards SET config = ? WHERE board = ?", (json.dumps(raw, ensure_ascii=False), self.board))

	# ---------- tickets ----------
	def tickets(self) -> dict[str, dict]:
		"""{id: ticket} in id order. Re-reads only the rows whose ver changed since the last call in this process."""
		c = self.c
		# Inside the lock, before this transaction wrote anything, the rows ARE the committed ones (every writer waits for the
		# lock), so the cache is used and kept as outside. Once it wrote (dirty), rows may roll back: the committed cache is
		# still the base (a cached (id, ver) is never reused for other content), but nothing read now is kept.
		dirty = bool(c.depth and c.dirty)
		ver = c.q("SELECT version FROM ub_boards WHERE board = ?", (self.board,))[0][0]
		self._seen = object() if dirty else ver
		hit = _CACHE.get((self.key, self.parse))
		if not dirty and hit and hit[0] == ver:  # nothing was written since: no need to scan the rows (versions only grow, in commit order)
			return hit[2]
		vers = dict(c.q("SELECT id, ver FROM ub_tickets WHERE board = ?", (self.board,)))
		old_v, old_t = hit[1:] if hit else ({}, {})
		need = [i for i, v in vers.items() if old_v.get(i) != v]
		got = {}
		for k in range(0, len(need), PAGE):
			chunk = need[k:k + PAGE]
			marks = ",".join("?" * len(chunk))
			for i, doc, v in c.q(f"SELECT id, doc, ver FROM ub_tickets WHERE board = ? AND id IN ({marks})", (self.board, *chunk)):
				got[i] = (v, self.parse(json.loads(doc)))
		new_v, new_t = {}, {}
		for i in sorted(vers):
			if i in got:
				new_v[i], new_t[i] = got[i]
			elif old_v.get(i) == vers[i]:
				new_v[i], new_t[i] = vers[i], old_t[i]
			# else: rewritten between the two reads; the next call fetches it
		if not dirty:
			_CACHE[(self.key, self.parse)] = (ver, new_v, new_t)  # the version read BEFORE the rows: a later write only makes it older
		return new_t

	def version(self):
		"""Changes whenever a ticket, a claim or an attachment changed (as read by the last tickets()); unique inside a transaction."""
		return self._seen if self._seen is not None else object()

	def exists(self, tid: str) -> bool:
		return bool(self.c.q("SELECT 1 FROM ub_tickets WHERE board = ? AND id = ?", (self.board, tid)))

	def read(self, tid: str) -> dict | None:
		row = self.c.q("SELECT doc FROM ub_tickets WHERE board = ? AND id = ?", (self.board, tid))
		return self.parse(json.loads(row[0][0])) if row else None

	def write(self, t: dict) -> None:
		doc = json.dumps(t, ensure_ascii=False)
		with self._tx() as c:
			v = self._bump(c)
			c.q("INSERT INTO ub_tickets (board, id, doc, ver) VALUES (?, ?, ?, ?) "
			    "ON CONFLICT (board, id) DO UPDATE SET doc = excluded.doc, ver = excluded.ver", (self.board, t["id"], doc, v))

	def delete(self, tid: str) -> None:
		"""The ticket and its attachments."""
		with self._tx() as c:
			c.q("DELETE FROM ub_tickets WHERE board = ? AND id = ?", (self.board, tid))
			c.q("DELETE FROM ub_files WHERE board = ? AND id = ?", (self.board, tid))
			self._bump(c)

	def numbers(self, prefix: str) -> list[int]:
		pat = re.compile(rf"^{re.escape(prefix)}-(\d+)")
		rows = self.c.q("SELECT id FROM ub_tickets WHERE board = ? AND id LIKE ?", (self.board, f"{prefix}-%"))
		return [int(m[1]) for (i,) in rows if (m := pat.match(i))]

	# ---------- the board lock ----------
	@contextmanager
	def lock(self, steal=None):  # steal: files only (a database releases a dead connection's lock itself)
		with self._tx(lock=True):
			yield

	# ---------- claims ----------
	def claims(self) -> dict:
		return {i: json.loads(d) for i, d in self.c.q("SELECT id, doc FROM ub_claims WHERE board = ? ORDER BY id", (self.board,))}

	def save_claims(self, claims: dict) -> None:
		with self._tx() as c:
			cur = dict(c.q("SELECT id, doc FROM ub_claims WHERE board = ?", (self.board,)))
			new = {i: json.dumps(v) for i, v in claims.items()}
			if cur == new:
				return
			for i in set(cur) - set(new):
				c.q("DELETE FROM ub_claims WHERE board = ? AND id = ?", (self.board, i))
			for i, d in new.items():
				if cur.get(i) != d:
					c.q("INSERT INTO ub_claims (board, id, doc) VALUES (?, ?, ?) ON CONFLICT (board, id) DO UPDATE SET doc = excluded.doc",
					    (self.board, i, d))
			self._bump(c)

	# ---------- activity ----------
	def append_event(self, line: dict, locked=None, cap=None) -> None:  # cap: files only (history is cleaned by age, CORE-37)
		with self._tx() as c:
			c.q("UPDATE ub_boards SET evseq = evseq + 1 WHERE board = ?", (self.board,))
			n = c.q("SELECT evseq FROM ub_boards WHERE board = ?", (self.board,))[0][0]
			c.q("INSERT INTO ub_events (board, n, ts, ticket, doc) VALUES (?, ?, ?, ?, ?)",
			    (self.board, n, line.get("ts", ""), line.get("ticket"), json.dumps(line, ensure_ascii=False)))

	def events_newest_first(self, ticket: str | None = None):
		"""Every event (only one ticket's: ticket=), newest first, read a page at a time (stop iterating early to read less)."""
		below, one = None, ("AND ticket = ? ", (ticket,)) if ticket else ("", ())
		while True:
			if below is None:
				rows = self.c.q(f"SELECT n, doc FROM ub_events WHERE board = ? {one[0]}ORDER BY n DESC LIMIT {PAGE}", (self.board, *one[1]))
			else:
				rows = self.c.q(f"SELECT n, doc FROM ub_events WHERE board = ? {one[0]}AND n < ? ORDER BY n DESC LIMIT {PAGE}", (self.board, *one[1], below))
			for n, d in rows:
				yield json.loads(d)
			if len(rows) < PAGE:
				return
			below = rows[-1][0]

	def events_after(self, cursor):
		"""(events numbered after `cursor`, new cursor). None = start at the newest now (history is never replayed)."""
		if cursor is None:
			return [], self.c.q("SELECT evseq FROM ub_boards WHERE board = ?", (self.board,))[0][0]
		rows = self.c.q("SELECT n, doc FROM ub_events WHERE board = ? AND n > ? ORDER BY n", (self.board, cursor))
		return [json.loads(d) for _, d in rows], (rows[-1][0] if rows else cursor)

	# ---------- attachments (bytes in the database) ----------
	def attachment_names(self) -> dict[str, list[str]]:
		out: dict[str, list[str]] = {}
		for i, name in self.c.q("SELECT id, name FROM ub_files WHERE board = ? ORDER BY id, name", (self.board,)):
			out.setdefault(i, []).append(name)
		return out

	def list_attachments(self, tid: str) -> list[dict]:
		return [{"name": n, "bytes": s} for n, s in self.c.q("SELECT name, size FROM ub_files WHERE board = ? AND id = ? ORDER BY name", (self.board, tid))]

	def put_attachment(self, tid: str, name: str, data: bytes) -> None:
		with self._tx() as c:
			c.q("INSERT INTO ub_files (board, id, name, data, size) VALUES (?, ?, ?, ?, ?) "
			    "ON CONFLICT (board, id, name) DO UPDATE SET data = excluded.data, size = excluded.size", (self.board, tid, name, data, len(data)))
			self._bump(c)

	def get_attachment(self, tid: str, name: str) -> bytes | None:
		row = self.c.q("SELECT data FROM ub_files WHERE board = ? AND id = ? AND name = ?", (self.board, tid, name))
		return bytes(row[0][0]) if row else None

	def delete_attachment(self, tid: str, name: str) -> bool:
		with self._tx() as c:
			if not c.q("SELECT 1 FROM ub_files WHERE board = ? AND id = ? AND name = ?", (self.board, tid, name)):
				return False
			c.q("DELETE FROM ub_files WHERE board = ? AND id = ? AND name = ?", (self.board, tid, name))
			self._bump(c)
			return True

	# ---------- small runtime documents ----------
	def get_doc(self, name: str) -> dict:
		row = self.c.q("SELECT doc FROM ub_docs WHERE board = ? AND name = ?", (self.board, name))
		return json.loads(row[0][0]) if row else {}

	def put_doc(self, name: str, doc: dict) -> None:
		with self._tx() as c:
			c.q("INSERT INTO ub_docs (board, name, doc) VALUES (?, ?, ?) ON CONFLICT (board, name) DO UPDATE SET doc = excluded.doc",
			    (self.board, name, json.dumps(doc)))

	# ---------- a whole board at once (migrate) ----------
	def bulk_load(self, cfg: dict | None, tickets: list[dict], files: list[tuple], events: list[dict]) -> None:
		"""Write a board into this EMPTY one in one transaction: settings, tickets as given (rev kept), attachments
		[(id, name, bytes)] and events oldest first. migrate uses it instead of thousands of single writes."""
		with self._tx(lock=True) as c:
			if cfg is not None:
				c.q("UPDATE ub_boards SET config = ? WHERE board = ?", (json.dumps(cfg, ensure_ascii=False), self.board))
			v = self._bump(c)
			c.many("INSERT INTO ub_tickets (board, id, doc, ver) VALUES (?, ?, ?, ?)",
			       [(self.board, t["id"], json.dumps(t, ensure_ascii=False), v) for t in tickets])
			c.many("INSERT INTO ub_files (board, id, name, data, size) VALUES (?, ?, ?, ?, ?)", [(self.board, i, n, d, len(d)) for i, n, d in files])
			n0 = c.q("SELECT evseq FROM ub_boards WHERE board = ?", (self.board,))[0][0]
			c.many("INSERT INTO ub_events (board, n, ts, ticket, doc) VALUES (?, ?, ?, ?, ?)",
			       [(self.board, n0 + k, e.get("ts", ""), e.get("ticket"), json.dumps(e, ensure_ascii=False)) for k, e in enumerate(events, 1)])
			c.q("UPDATE ub_boards SET evseq = ? WHERE board = ?", (n0 + len(events), self.board))

	# ---------- the board itself ----------
	def drop(self) -> None:
		"""Delete the board and every row it has, in one transaction (owner decision CORE-27 #2: deleting a board deletes its rows)."""
		with self._tx() as c:
			for t in ("ub_tickets", "ub_claims", "ub_events", "ub_files", "ub_docs", "ub_boards"):
				c.q(f"DELETE FROM {t} WHERE board = ?", (self.board,))
		for ck in [k for k in _CACHE if k[0] == self.key]:
			_CACHE.pop(ck, None)


def boards(url: str) -> list[str]:
	"""The board ids in a database."""
	return [b for (b,) in connect(url).q("SELECT board FROM ub_boards ORDER BY board")]


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")
