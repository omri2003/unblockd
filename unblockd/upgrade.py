"""Built-in database upgrades (CORE-38; owner decision 2026-11-08: no migration library, no hand-run scripts).

Every database unblockd keeps data in stores its schema version: the SQL connector in ub_meta, the MongoDB connector in
ub_meta {_id: "schema"}, the system database in us_meta. Each has a list of numbered STEPS (step n takes schema n to n + 1;
schema = len(STEPS); a released step is never edited, a change is a new step). On first connection unblockd applies the
steps a database is missing:
- SQL (SQLite, Postgres): all missing steps in ONE transaction, all or nothing (Postgres: an advisory lock, so two servers
  starting at once upgrade it once; SQLite: BEGIN IMMEDIATE holds the file).
- MongoDB (no transactions for index builds): one step at a time, the version raised after each, so a crash resumes at
  the step that did not finish; steps are idempotent (create_index is).
A database NEWER than this unblockd is always refused (with what to do). `UNBLOCKD_AUTO_UPGRADE=0` turns the automatic
upgrade off: an older database is then refused until an admin runs `python -m unblockd.cli db upgrade`. A brand-new
(empty) database is set up either way: there is nothing to upgrade.

mode: "auto" (on connect: upgrade unless turned off), "upgrade" (db upgrade: always), "status" (db status: report only).
"""
from __future__ import annotations

import os
import sys
import zlib
from datetime import datetime, timezone

from .errors import TicketError

ENV = "UNBLOCKD_AUTO_UPGRADE"


def automatic() -> bool:
	return os.environ.get(ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def _decide(where: str, have: int, target: int, mode: str) -> tuple[bool, str | None]:
	"""(apply the missing steps?, error for status mode); raises when this mode must refuse."""
	if have > target:
		msg = (f"{where} was set up by a newer unblockd (schema {have}, this one knows {target}): upgrade unblockd "
		       "(or point it at another database)")
		if mode == "status":
			return False, msg
		raise TicketError(msg)
	if have == target or mode == "status":
		return False, None
	if mode == "auto" and have > 0 and not automatic():
		raise TicketError(f"{where} has schema {have} and this unblockd needs {target}; automatic upgrades are off ({ENV}=0): "
		                  "run python -m unblockd.cli db upgrade")
	return True, None


def _report(where, kind, have, target, applied, err) -> dict:
	out = {"database": where, "kind": kind, "schema": target if applied else have, "latest": target}
	if applied:
		out["upgraded_from"] = have
	if applied and have:  # a new database being set up is not news
		print(f"unblockd: upgraded {where} from schema {have} to {target}", file=sys.stderr, flush=True)
	if err:
		out["error"] = err
	return out


def sql(c, meta: str, steps: list[str], where: str, mode: str = "auto") -> dict:
	"""Bring one SQL database (an open sql._Conn) to len(steps). Statements are split on ';'; {BLOB} = BLOB / BYTEA, {NOW} = now."""
	target = len(steps)
	c.begin()
	ok = False
	try:
		if c.kind == "postgres":  # one upgrader per database and meta table at a time; freed with the transaction
			c.q("SELECT pg_advisory_xact_lock(?)", (zlib.crc32(meta.encode()),))
		c.q(f"CREATE TABLE IF NOT EXISTS {meta} (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
		row = c.q(f"SELECT v FROM {meta} WHERE k = 'schema'")
		have = int(row[0][0]) if row else 0
		apply, err = _decide(where, have, target, mode)
		if apply:
			now = datetime.now(timezone.utc).isoformat(timespec="seconds")
			for step in steps[have:]:
				text = step.replace("{BLOB}", "BLOB" if c.kind == "sqlite" else "BYTEA").replace("{NOW}", now)
				for stmt in filter(str.strip, text.split(";")):
					c.q(stmt)
			c.q(f"INSERT INTO {meta} (k, v) VALUES ('schema', ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v", (str(target),))
		ok = mode != "status"  # status changes nothing (not even the meta table it may have made)
		return _report(where, c.kind, have, target, apply, err)
	finally:
		c.end(ok)


def mongo(db, steps: list, where: str, mode: str = "auto") -> dict:
	"""Bring one MongoDB database to len(steps); each step is a function(db), idempotent."""
	target = len(steps)
	doc = db.ub_meta.find_one({"_id": "schema"})
	have = int(doc["v"]) if doc else 0
	apply, err = _decide(where, have, target, mode)
	if apply:
		for n in range(have, target):
			steps[n](db)
			db.ub_meta.update_one({"_id": "schema"}, {"$max": {"v": n + 1}}, upsert=True)  # never lowers a newer server's mark
	return _report(where, "mongo", have, target, apply, err)
