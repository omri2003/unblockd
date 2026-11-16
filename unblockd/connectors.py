"""Where a board lives, and moving a board from one place to another.

A board's LOCATION is one string:
- a folder path: the files connector (`tickets/`, `/data/tickets`);
- a database URL + `#board`: `sqlite:///path/unblockd.db#payments`, `postgres://user:pass@host:5432/db#payments`,
  `mongodb://user:pass@host:27017/db#payments` (`mongodb+srv://` too). `#board` is the board's id in that database.

`describe(location)` is the same string with any password hidden: use it in every message and log.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .errors import TicketError

DB_SCHEMES = ("sqlite:///", "postgres://", "postgresql://", "mongodb://", "mongodb+srv://")
DB_SCHEMES_NAMES = tuple(s.split("//")[0] for s in DB_SCHEMES)  # "sqlite:", "postgres:", ...


RETRY_S = 30  # a database that could not be reached is not tried again for this long
_DOWN: dict[str, tuple[float, str]] = {}  # url -> (monotonic time of the next try, the error)
_DOWN_LOCK = threading.Lock()


def reach(url: str, connect):
	"""connect() unless url failed less than RETRY_S ago: then its last error at once. Every board list and the 15 s
	needs poll open every board, so without this one unreachable server would stall them all for the full connect timeout."""
	with _DOWN_LOCK:
		hit = _DOWN.get(url)
	if hit and hit[0] > time.monotonic():
		raise TicketError(f"{hit[1]} (next try within {RETRY_S} s)")
	try:
		out = connect()
	except TicketError as e:
		with _DOWN_LOCK:
			_DOWN[url] = (time.monotonic() + RETRY_S, str(e))
		raise
	with _DOWN_LOCK:
		_DOWN.pop(url, None)
	return out


def is_db(location) -> bool:
	return isinstance(location, str) and location.startswith(DB_SCHEMES)


def split(location: str) -> tuple[str, str]:
	"""(database URL, board id) of a database location."""
	url, sep, board = location.rpartition("#")
	if not sep or not url or not board:
		raise TicketError(f"{describe(location)}: name the board after a #, e.g. {describe(location)}#payments")
	return url, board


def describe(location: str) -> str:
	if not is_db(location):
		return str(location)
	from .sql import redact  # noqa: PLC0415 - same rule for every URL
	return redact(location)


def open_backend(location: str, create: bool = True):
	"""The connector for a location. create=False: a database board must already exist (a folder must exist either way)."""
	if not is_db(location):
		from .files import FileBackend  # noqa: PLC0415
		if not Path(location).is_dir():
			raise TicketError(f"no board folder at {Path(location).resolve()}" + ("" if not create else ": create the folder first"))
		return FileBackend(location)
	url, board = split(location)
	if url.startswith("mongodb"):
		from .mongo import MongoBackend  # noqa: PLC0415
		return MongoBackend(url, board, create=create)
	from .sql import SqlBackend  # noqa: PLC0415
	return SqlBackend(url, board, create=create)


def _empty(b) -> bool:
	return not b.tickets() and next(iter(b.events_newest_first()), None) is None and b.read_config()[0] is None


def _clear(b) -> None:
	"""Remove everything a board has (for migrate --force)."""
	if b.kind == "files":
		for p in b.dir.glob("*.json"):
			p.unlink()
		import shutil  # noqa: PLC0415
		shutil.rmtree(b.dir / "files", ignore_errors=True)
		for name in ("activity.jsonl", "activity.1.jsonl", "claims.json"):
			(b.rt / name).unlink(missing_ok=True)
		return
	b.drop()
	b.__init__(b.url, b.board, b.parse)  # the board row again, empty


def migrate(src: str, dst: str, force: bool = False) -> dict:
	"""Copy a board from one location to another: settings, tickets exactly as stored (ids, rev, notes, results), attachments
	and the activity log. Claims are not copied (runtime state: workers claim again). Refuses a target that already has a
	board unless force (which first removes what the target has). Checks the copy before answering."""
	from .store import ACTIVITY_MAX, config_from  # noqa: PLC0415 - store imports this module's users, not this module
	a = open_backend(src, create=False)
	if a.read_config()[0] is None and not a.tickets():
		raise TicketError(f"{describe(src)} has no board to copy (no settings, no tickets)")
	cfg = a.read_config()[0]
	if cfg is not None:
		try:  # before the target is even opened: settings that can't work there (paths need a repo) stop the move here
			config_from(cfg, f"{describe(src)} settings", None if is_db(dst) else Path(dst))
		except TicketError as e:
			raise TicketError(f"can't move these settings to {describe(dst)}: {e}") from None
	if not is_db(dst):
		Path(dst).mkdir(parents=True, exist_ok=True)  # a new folder for the board (checks above passed)
	b = open_backend(dst, create=True)
	if a.key == b.key:
		raise TicketError("--from and --to are the same board")
	if not _empty(b):
		if not force:
			raise TicketError(f"{describe(dst)} already has a board (tickets, history or settings): pick an empty place, "
			                  "or pass --force to REPLACE everything there")
		_clear(b)
	tickets = a.tickets()  # exactly as stored: this connector's parse is the identity (its cache is its own, see files._RAW)
	ids = sorted(tickets)
	files = {i: [(f["name"], a.get_attachment(i, f["name"])) for f in a.list_attachments(i)] for i in ids}
	events = list(a.events_newest_first())[::-1]

	def copy():
		if cfg is not None:
			b.write_config(cfg)
		for i in ids:
			b.write(tickets[i])  # write() stores the ticket as given: rev is NOT bumped (Store does that), so it is kept
			for name, data in files[i]:
				b.put_attachment(i, name, data)
		for e in events:
			b.append_event(e, lambda fn: fn(), ACTIVITY_MAX)
	if hasattr(b, "bulk_load"):  # databases: batched inserts (SQL: one transaction, all or nothing)
		b.bulk_load(cfg, [tickets[i] for i in ids], [(i, n, d) for i in ids for n, d in files[i]], events)
	else:  # files: one file per ticket, written as they come
		copy()
	# check: read everything back from the target
	back = b.tickets()  # this connector's parse is the identity: the documents as stored
	bad = [i for i in ids if back.get(i) != tickets[i]]
	if sorted(back) != ids:
		bad.append("(ticket list differs)")
	bad += [f"{i}/{n}" for i in ids for n, data in files[i] if b.get_attachment(i, n) != data]
	got = sum(1 for _ in b.events_newest_first())
	warn = []
	if got != len(events):
		if b.kind == "files" and got < len(events):
			warn.append(f"history: {got} of {len(events)} events kept (a files board keeps about the last {2 * ACTIVITY_MAX // 1_000_000} MB)")
		else:
			bad.append(f"(history: {got} events, expected {len(events)})")
	if bad:
		raise TicketError(f"copied to {describe(dst)} but the check failed for {', '.join(bad[:10])}: the source is untouched; "
		                  "fix the problem and run again with --force")
	docs = [d for d in (cfg or {}).get("docs", [])] if b.dir is None else []
	if docs:
		warn.append(f"the board's docs ({', '.join(docs[:5])}) are repo paths, and {describe(dst)} has no repo: agents get a 'no repo linked' note for them")
	return {"from": describe(src), "to": describe(dst), "tickets": len(ids), "attachments": sum(len(v) for v in files.values()),
	        "events": got, "claims_copied": 0, "warnings": warn}

