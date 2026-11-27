"""The system database: unblockd's own data, separate from every board's tickets (CORE-34).

Where: `UNBLOCKD_SYSTEM_DB` (or `UNBLOCKD_SYSTEM_DB_FILE`, a file holding it: Docker / Kubernetes secrets) = a SQLite file path,
`sqlite:///path.db` or `postgres://user:pass@host/db`. Unset: a SQLite file at ~/.unblockd/system.db (the Docker image sets
/data/system.db), created on first use, so a fresh install needs no setup.

What it holds (tables prefixed us_, so it may share a database with boards, whose tables are ub_):
- us_meta: the schema version.
- us_orgs: organisations. Only "default" for now; every other row names its org, so organisations, users and memberships
  (CORE-69) are new tables added by an upgrade step, not a rewrite of these.
- us_connectors: named database addresses boards can live in ({org, name, kind sqlite|postgres|mongo, url without password,
  secret = the encrypted password (CORE-41), created}).
- us_boards: the registered boards ({org, id, title, connector, location, settings JSON, created}). connector NULL: location
  is the board's whole location (a folder path or a database URL + #board, no password); connector set: location is the
  board's key in that connector's database.

Registered boards appear in the board list after the boards found by scanning (UNBLOCKD_ROOT, TICKETS_DIR, UNBLOCKD_BOARDS).
Connections are per thread (sql._Conn: SQLite in WAL mode, or psycopg), like the SQL connector's.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .errors import NotFound, TicketError

ORG = "default"
_LOCAL = threading.local()

# Numbered upgrade steps: STEPS[n] takes a database from schema n to n + 1. SCHEMA = len(STEPS). Never edit a released step:
# add one. "{BLOB}" is BLOB on SQLite, BYTEA on Postgres.
STEPS = [
	"""
	CREATE TABLE IF NOT EXISTS us_orgs (id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL);
	CREATE TABLE IF NOT EXISTS us_connectors (org TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL,
		secret TEXT, created TEXT NOT NULL, PRIMARY KEY (org, name));
	CREATE TABLE IF NOT EXISTS us_boards (org TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', connector TEXT,
		location TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL, PRIMARY KEY (org, id));
	INSERT INTO us_orgs (id, name, created) VALUES ('default', 'Default', '{NOW}') ON CONFLICT (id) DO NOTHING;
	""",
]
SCHEMA = len(STEPS)


def env(name: str, default: str | None = None) -> str | None:
	"""A setting from the environment: NAME, or the contents of the file NAME_FILE names (Docker / Kubernetes secrets; one
	trailing newline and surrounding spaces dropped). Both set = refused: which one wins would be a guess."""
	val, path = os.environ.get(name) or None, os.environ.get(name + "_FILE") or None
	if val and path:
		raise TicketError(f"both {name} and {name}_FILE are set: keep one")
	if path:
		try:
			return Path(path).read_text(encoding="utf-8").strip()
		except OSError as e:
			raise TicketError(f"{name}_FILE={path}: can't read it ({e.strerror or e})") from None
	return val if val is not None else default


def default_url() -> str:
	return "sqlite:///" + (Path.home() / ".unblockd" / "system.db").as_posix()


def normalize(url: str) -> str:
	"""sqlite:///..., postgres://... as given (postgresql:// -> postgres://); a bare path = a SQLite file."""
	url = url.strip()
	if url.startswith("postgresql://"):
		url = "postgres://" + url[len("postgresql://"):]
	if url.startswith(("mongodb://", "mongodb+srv://")):
		raise TicketError("the system database is SQLite or Postgres (MongoDB can hold boards, not unblockd's own data)")
	if url.startswith(("sqlite:///", "postgres://")):
		return url
	if "://" in url:
		raise TicketError(f"unsupported system database {_redact(url)!r}: a file path, sqlite:///path.db or postgres://user:pass@host/db")
	return "sqlite:///" + Path(url).expanduser().absolute().as_posix()


def _redact(url: str) -> str:
	from .sql import redact  # noqa: PLC0415
	return redact(url)


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kind_of(url: str) -> str:
	return "sqlite" if url.startswith("sqlite:") else "mongo" if url.startswith("mongodb") else "postgres"


class System:
	"""The system database at one URL. Nothing connects until the first call."""

	def __init__(self, url: str | None = None):
		self.url = normalize(url or default_url())
		self.where = _redact(self.url)

	@property
	def c(self):
		conns = getattr(_LOCAL, "conns", None)
		if conns is None:
			conns = _LOCAL.conns = {}
		c = conns.get(self.url)
		if c is None:
			from . import sql  # noqa: PLC0415 - the same connection class as the SQL connector
			if self.url.startswith("sqlite:///"):
				Path(self.url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
			c = sql._Conn(self.url)
			self._upgrade(c)
			conns[self.url] = c
		return c

	def close(self) -> None:
		c = getattr(_LOCAL, "conns", {}).pop(self.url, None)
		if c is not None:
			c.db.close()

	def _tx(self):
		return _Tx(self.c)

	def _upgrade(self, c) -> None:
		c.begin()
		ok = False
		try:
			c.q("CREATE TABLE IF NOT EXISTS us_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
			row = c.q("SELECT v FROM us_meta WHERE k = 'schema'")
			have = int(row[0][0]) if row else 0
			if have > SCHEMA:
				raise TicketError(f"the system database {self.where} was set up by a newer unblockd (schema {have}, this one knows "
				                  f"{SCHEMA}): upgrade unblockd, or point UNBLOCKD_SYSTEM_DB elsewhere")
			for step in STEPS[have:]:
				sqltext = step.replace("{BLOB}", "BLOB" if c.kind == "sqlite" else "BYTEA").replace("{NOW}", _now())
				for stmt in filter(str.strip, sqltext.split(";")):
					c.q(stmt)
			if have < SCHEMA:
				c.q("INSERT INTO us_meta (k, v) VALUES ('schema', ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (str(SCHEMA),))
			ok = True
		finally:
			c.end(ok)

	# ---------- read ----------
	def info(self) -> dict:
		c = self.c
		return {"url": self.where, "kind": c.kind, "schema": int(c.q("SELECT v FROM us_meta WHERE k = 'schema'")[0][0]),
		        "boards": c.q("SELECT COUNT(*) FROM us_boards")[0][0], "connectors": c.q("SELECT COUNT(*) FROM us_connectors")[0][0]}

	def boards(self, org: str = ORG) -> list[dict]:
		rows = self.c.q("SELECT id, title, connector, location, settings, created FROM us_boards WHERE org = ? ORDER BY created, id", (org,))
		return [{"id": i, "title": t, "connector": cn, "location": loc, "settings": json.loads(s or "{}"), "created": cr}
		        for i, t, cn, loc, s, cr in rows]

	def connectors(self, org: str = ORG) -> list[dict]:
		rows = self.c.q("SELECT name, kind, url, secret, created FROM us_connectors WHERE org = ? ORDER BY name", (org,))
		return [{"name": n, "kind": k, "url": u, "has_password": bool(s), "created": cr} for n, k, u, s, cr in rows]

	def _connector(self, name: str, org: str = ORG) -> tuple:
		row = self.c.q("SELECT url, secret FROM us_connectors WHERE org = ? AND name = ?", (org, name))
		if not row:
			raise NotFound(f"no connector {name!r} in the system database ({', '.join(c['name'] for c in self.connectors(org)) or 'none yet'})")
		return row[0]

	def connector_url(self, name: str, org: str = ORG) -> str:
		"""The connector's address to connect with."""
		url, secret = self._connector(name, org)
		if secret:  # CORE-41
			raise TicketError(f"connector {name} has a stored password, which this unblockd can't read")
		return url

	def locations(self, org: str = ORG) -> list[tuple[str, str]]:
		"""[(board id, location to open)] in registration order. A board whose connector can't be used is listed with a location
		that says why when opened (so it shows as a broken board, never hides the others)."""
		out = []
		for b in self.boards(org):
			if not b["connector"]:
				out.append((b["id"], b["location"]))
				continue
			try:
				out.append((b["id"], f"{self.connector_url(b['connector'], org)}#{b['location']}"))
			except TicketError as e:
				out.append((b["id"], Broken(str(e))))
		return out

	# ---------- write ----------
	def add_connector(self, name: str, url: str, org: str = ORG) -> dict:
		from . import connectors as cn  # noqa: PLC0415
		from .store import NAME_RE  # noqa: PLC0415
		if not NAME_RE.match(name or ""):
			raise TicketError(f"bad connector name {name!r}: lowercase a-z, 0-9, - _ (max 40)")
		url = (url or "").strip()
		if url.startswith("postgresql://"):
			url = "postgres://" + url[len("postgresql://"):]
		if not cn.is_db(url) or "#" in url:
			raise TicketError(f"a connector is a database address without #board: sqlite:///path.db, postgres://user@host/db or mongodb://host/db (got {cn.describe(url)!r})")
		if _password(url) is not None:
			raise TicketError("a password in a connector address needs the secret key (CORE-41): leave it out and let the driver find it "
			                  "(PGPASSWORD / ~/.pgpass for Postgres), or upgrade unblockd")
		with self._tx() as c:
			if c.q("SELECT 1 FROM us_connectors WHERE org = ? AND name = ?", (org, name)):
				raise TicketError(f"connector {name} already exists: remove it first")
			c.q("INSERT INTO us_connectors (org, name, kind, url, secret, created) VALUES (?, ?, ?, ?, NULL, ?)", (org, name, kind_of(url), url, _now()))
		return next(x for x in self.connectors(org) if x["name"] == name)

	def remove_connector(self, name: str, org: str = ORG) -> dict:
		with self._tx() as c:
			self._connector(name, org)
			used = [i for (i,) in c.q("SELECT id FROM us_boards WHERE org = ? AND connector = ? ORDER BY id", (org, name))]
			if used:
				raise TicketError(f"connector {name} holds registered boards ({', '.join(used)}): remove those first")
			c.q("DELETE FROM us_connectors WHERE org = ? AND name = ?", (org, name))
		return {"removed": name}

	def add_board(self, bid: str, location: str = "", connector: str | None = None, title: str = "", org: str = ORG) -> dict:
		"""Register a board. location = a folder or a database URL + #board; with connector = the board's key in that database
		(default: bid). Only the registration: a folder must exist; a database board is created by boards.Boards.register (never later by opening it)."""
		from . import connectors as cn  # noqa: PLC0415
		from .store import NAME_RE  # noqa: PLC0415
		if not NAME_RE.match(bid or ""):
			raise TicketError(f"bad board id {bid!r}: lowercase a-z, 0-9, - _ (max 40)")
		location = (location or "").strip()
		if connector:
			self._connector(connector, org)
			location = location or bid
			if not NAME_RE.match(location):
				raise TicketError(f"bad board key {location!r} in connector {connector}: lowercase a-z, 0-9, - _ (max 40)")
		elif not location:
			raise TicketError("give the board's location (a folder or a database URL + #board) or a connector")
		elif cn.is_db(location):
			cn.split(location)  # says what is missing when there is no #board
			if _password(location) is not None:
				raise TicketError("a password in a board's address is not stored: add the database as a connector, then register the board with it")
		else:
			p = Path(location).expanduser()
			if not p.is_dir():
				raise TicketError(f"no folder {p.absolute().as_posix()}: create it (with an unblockd.json) first")
			location = p.absolute().as_posix()
		with self._tx() as c:
			if c.q("SELECT 1 FROM us_boards WHERE org = ? AND id = ?", (org, bid)):
				raise TicketError(f"board {bid} is already registered: remove it first")
			c.q("INSERT INTO us_boards (org, id, title, connector, location, settings, created) VALUES (?, ?, ?, ?, ?, '{}', ?)",
			    (org, bid, (title or "").strip()[:80], connector or None, location, _now()))
		return next(b for b in self.boards(org) if b["id"] == bid)

	def remove_board(self, bid: str, org: str = ORG) -> dict:
		"""Unregister: the board's tickets stay where they are."""
		with self._tx() as c:
			if not c.q("SELECT 1 FROM us_boards WHERE org = ? AND id = ?", (org, bid)):
				raise NotFound(f"no registered board {bid!r}")
			c.q("DELETE FROM us_boards WHERE org = ? AND id = ?", (org, bid))
		return {"removed": bid, "note": "only the registration: the board's tickets are untouched"}


class Broken(str):
	"""A location that can't be opened; opening it raises its message (boards.Boards.open)."""


def _password(url: str) -> str | None:
	from urllib.parse import urlsplit  # noqa: PLC0415
	try:
		return urlsplit(url.split("#")[0]).password
	except ValueError:
		return None


class _Tx:
	def __init__(self, c):
		self.c = c

	def __enter__(self):
		self.c.begin()
		return self.c

	def __exit__(self, et, ev, tb):
		self.c.end(et is None)
		return False


_WARNED: set[str] = set()


def warn_once(msg: str) -> None:
	if msg not in _WARNED:
		_WARNED.add(msg)
		print(f"unblockd: system database: {msg}", file=sys.stderr, flush=True)


def from_env() -> System:
	return System(env("UNBLOCKD_SYSTEM_DB"))
