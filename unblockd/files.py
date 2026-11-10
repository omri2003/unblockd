"""The files connector: one JSON file per ticket in a tickets folder (the default, and what a git repo holds).

Storage only. Every rule (validation, routing, claims logic, derived fields) lives in store.Store, which calls a
connector through the methods below; a database connector implements the same ones (see docs/reference.md, Connectors).
Runtime state (lock, claims.json, activity.jsonl, prs.json) lives in <tickets>/.unblockd, which ignores itself in git.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import TicketError

CONFIG_FILE = "unblockd.json"  # optional, next to the tickets
ID_RE = re.compile(r"^[A-Z0-9]+-\d+[a-z]?$")  # also the filename: blocks path traversal
ACTIVITY_MAX = 5_000_000  # bytes; past this activity.jsonl becomes activity.1.jsonl (one old file kept)
LOCK_STALE = 30  # seconds: a lock file older than this was left by a crash

# Per-process caches keyed by folder / file path, so every Store on the same folder (the web server builds one per request)
# shares them. Files stay the only truth: every read re-stats the folder and re-parses only files whose (inode, mtime, size)
# changed, so another process's write, a git pull or a hand edit is seen on the next call. Never written back.
_RAW: dict[tuple, dict] = {}  # (tickets folder, parse) -> {stem: (stamp, parsed ticket)}: a raw reader never shares a parsed cache
_LOG: dict[str, tuple] = {}  # activity file -> (inode, bytes parsed, [events oldest first])


class FileBackend:
	"""A board stored as files. `parse` turns a ticket file's JSON into the stored form (the rules' upgrade of old files)."""
	kind = "files"

	def __init__(self, tickets_dir: str | os.PathLike, parse=lambda t: t):
		self.dir = Path(tickets_dir)
		self.dir.mkdir(parents=True, exist_ok=True)
		self.parse = parse
		self.key = str(self.dir.resolve())
		# runtime state: <tickets>/.unblockd, except in a linked git worktree, where it is the MAIN checkout's copy of the same
		# folder: every worktree then shares one lock and one claims.json (ticket files stay per branch)
		wt = linked_worktree(self.dir.resolve())
		self.rt = (wt[1] / self.dir.resolve().relative_to(wt[0]) if wt else self.dir) / ".unblockd"

	def project_root(self) -> Path:
		"""The repo the board belongs to: repo docs, architecture notes and file suggestions are read from here."""
		return self.dir.resolve().parent

	# ---------- config ----------
	def read_config(self) -> tuple[dict | None, str]:
		"""(the board's own settings, or None when it has none; where they live, for error messages)."""
		path = self.dir / CONFIG_FILE
		if not path.exists():
			return None, str(path)
		try:
			return json.loads(path.read_text(encoding="utf-8")), str(path)
		except json.JSONDecodeError as e:
			raise TicketError(f"{path} is not valid JSON: {e}")

	def write_config(self, raw: dict) -> None:
		p = self.dir / CONFIG_FILE
		tmp = p.with_suffix(".tmp")
		tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
		os.replace(tmp, p)

	# ---------- tickets ----------
	def path(self, tid: str) -> Path:
		return self.dir / f"{tid}.json"

	def read_file(self, path: Path) -> dict:
		with open(path, encoding="utf-8") as f:
			return self.parse(json.load(f))

	def tickets(self) -> dict[str, dict]:
		"""{id: ticket} for every ticket, in id order, re-parsing only the files that changed since the last call (any Store,
		this process). The dicts are shared with the cache: callers copy before changing them."""
		return {k: t for k, (_, t) in self._scan().items()}

	def _scan(self) -> dict[str, tuple]:
		ck = (self.key, self.parse)
		old, new = _RAW.get(ck, {}), {}
		with os.scandir(self.dir) as it:
			for e in it:
				if not e.name.endswith(".json") or not ID_RE.match(stem := e.name[:-5]):
					continue
				st = e.stat()
				stamp = (st.st_ino, st.st_mtime_ns, st.st_size)  # os.replace makes a new file, so a rewrite changes the stamp even within one clock tick
				hit = old.get(stem)
				new[stem] = hit if hit and hit[0] == stamp else (stamp, self.read_file(Path(e.path)))
		_RAW[ck] = dict(sorted(new.items()))
		return _RAW[ck]

	def version(self):
		"""Changes whenever a ticket, a claim or an attachment changed (read after tickets()): the derived board is rebuilt only then."""
		return (tuple((k, v[0]) for k, v in _RAW.get((self.key, self.parse), {}).items()), _stamp(self.rt / "claims.json"), self._files_sig())

	def _files_sig(self) -> tuple:
		fd = self.dir / "files"
		if not fd.is_dir():
			return ()
		with os.scandir(fd) as it:  # the names themselves: a directory's mtime is not reliably bumped on every OS
			return tuple(sorted((d.name, tuple(sorted(os.listdir(d.path)))) for d in it if d.is_dir()))

	def exists(self, tid: str) -> bool:
		return self.path(tid).exists()

	def read(self, tid: str) -> dict | None:
		"""A fresh read of one ticket (not the cache), or None."""
		p = self.path(tid)
		try:
			return self.read_file(p)
		except FileNotFoundError:
			return None

	def write(self, t: dict) -> None:
		fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			json.dump(t, f, indent=2, ensure_ascii=False)
			f.write("\n")
		os.replace(tmp, self.path(t["id"]))  # atomic: a reader never sees half a file

	def delete(self, tid: str) -> None:
		"""The ticket and its attachments."""
		self.path(tid).unlink()
		shutil.rmtree(self.dir / "files" / tid, ignore_errors=True)

	def numbers(self, prefix: str) -> list[int]:
		"""Numbers already used by ids with this prefix (ART-23a counts as 23)."""
		pat = re.compile(rf"(?<![A-Z0-9]){re.escape(prefix)}-(\d+)")
		return [int(m) for p in self.dir.glob(f"{prefix}-*.json") for m in pat.findall(p.stem)]

	# ---------- the board lock ----------
	def runtime(self) -> Path:
		d = self.rt
		if not d.is_dir():
			d.mkdir(parents=True, exist_ok=True)  # parallel first calls race here
			(d / ".gitignore").write_text("*\n", encoding="utf-8")
		return d

	@contextmanager
	def lock(self, steal=None):
		"""Hold the board lock: a file created exclusively, so two processes can't both hold it. `steal(lock_path)` removes a
		stale one (store.Store._steal: re-checks the age under a guard). Not re-entrant: store.Store makes it so."""
		steal = steal or steal_stale
		lock = self.runtime() / "lock"
		deadline = time.monotonic() + 10
		while True:
			try:
				fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
				break
			except PermissionError:  # Windows: the holder just deleted it and the delete is still pending; wait like for a held lock
				if time.monotonic() > deadline:
					raise TicketError(f"can't create {lock} (permission denied for 10 s): check the folder is writable")
				time.sleep(0.01)
				continue
			except FileExistsError:
				try:
					if time.time() - lock.stat().st_mtime > LOCK_STALE:  # left behind by a crash
						steal(lock)  # then fall through: deadline + sleep, so a stuck guard can't spin us
				except FileNotFoundError:
					continue
				if time.monotonic() > deadline:
					raise TicketError("another claim is in progress; try again")
				time.sleep(0.05)
		try:
			yield
		finally:
			os.close(fd)
			lock.unlink(missing_ok=True)

	# ---------- claims ----------
	# Runtime state, not ticket content, so heartbeats never show up as changes in the project repo.
	def claims(self) -> dict:
		p = self.rt / "claims.json"
		try:
			return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
		except json.JSONDecodeError as e:
			raise TicketError(f"{p} is not valid JSON ({e}), so no claim can be read: fix it by hand, or delete {p}: claims are runtime state "
			                  "and live workers will re-claim (tickets they held stay 'doing' until then)")

	def save_claims(self, claims: dict) -> None:
		p = self.runtime() / "claims.json"
		tmp = p.with_suffix(".tmp")
		tmp.write_text(json.dumps(claims, indent=2), encoding="utf-8")
		os.replace(tmp, p)

	# ---------- activity ----------
	def append_event(self, line: dict, locked, cap: int = ACTIVITY_MAX) -> None:
		"""Append one event. `locked(fn)` runs fn under the board lock (re-entrant), for the rotation past `cap` bytes."""
		p = self.runtime() / "activity.jsonl"
		if p.exists() and p.stat().st_size > cap:
			locked(lambda: p.stat().st_size > cap and os.replace(p, p.with_name("activity.1.jsonl")))
		with open(p, "a", encoding="utf-8") as f:
			f.write(json.dumps(line, ensure_ascii=False) + "\n")

	def events_newest_first(self, ticket: str | None = None):
		"""Every event (only one ticket's: ticket=), newest first (stop iterating early to read less)."""
		for name in ("activity.jsonl", "activity.1.jsonl"):
			for e in reversed(_events(self.rt / name)):
				if ticket is None or e.get("ticket") == ticket:
					yield e

	def events_after(self, cursor):
		"""(events appended since `cursor`, new cursor). cursor None = start at the end now (history is never replayed).
		A rotation is followed: the rest of activity.1.jsonl first, then the new file from 0."""
		p = self.rt / "activity.jsonl"
		size = p.stat().st_size if p.exists() else 0
		off = size if cursor is None else cursor
		events = []
		if size < off:  # rotated (or truncated): first finish the old file, which append_event renamed to activity.1.jsonl
			old = p.with_name("activity.1.jsonl")
			if old.exists() and old.stat().st_size >= off:  # smaller = not our file (plain truncation)
				events += _read_from(old, off, old.stat().st_size)[0]
			off = 0
		if size > off:
			new, off = _read_from(p, off, size)
			events += new
		return events, off

	# ---------- attachments ----------
	def attachment_names(self) -> dict[str, list[str]]:
		fd = self.dir / "files"
		return {d.name: sorted(f.name for f in d.iterdir() if f.is_file()) for d in fd.iterdir() if d.is_dir()} if fd.is_dir() else {}

	def list_attachments(self, tid: str) -> list[dict]:
		d = self.dir / "files" / tid
		if not d.is_dir():
			return []
		return [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(d.iterdir()) if p.is_file()]

	def put_attachment(self, tid: str, name: str, data: bytes) -> None:
		d = self.dir / "files" / tid
		d.mkdir(parents=True, exist_ok=True)
		(d / name).write_bytes(data)

	def get_attachment(self, tid: str, name: str) -> bytes | None:
		p = self.dir / "files" / tid / name
		return p.read_bytes() if p.is_file() else None

	def delete_attachment(self, tid: str, name: str) -> bool:
		p = self.dir / "files" / tid / name
		if not p.is_file():
			return False
		p.unlink()
		return True

	# ---------- small runtime documents (e.g. the GitHub PR cache) ----------
	def get_doc(self, name: str) -> dict:
		try:
			return json.loads((self.rt / f"{name}.json").read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return {}

	def put_doc(self, name: str, doc: dict) -> None:
		d = self.rt
		d.mkdir(parents=True, exist_ok=True)
		(d / f"{name}.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")


def steal_stale(lock: Path) -> None:
	"""Remove a stale lock. Only the holder of lock.steal may, and it re-checks the age under it: two processes that both
	read the same stale mtime can't then delete the fresh lock the first one just created."""
	guard = lock.with_name("lock.steal")
	try:
		fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
	except FileExistsError:  # someone else is stealing; the caller retries. A guard left by a crash goes after LOCK_STALE too
		try:
			if time.time() - guard.stat().st_mtime > LOCK_STALE:
				guard.unlink()
		except FileNotFoundError:
			pass
		return
	try:
		if time.time() - lock.stat().st_mtime > LOCK_STALE:
			lock.unlink()
	except FileNotFoundError:
		pass
	finally:
		os.close(fd)
		guard.unlink(missing_ok=True)


def _events(p: Path) -> list[dict]:
	"""Parsed lines of one activity file, oldest first. The log only grows (or is renamed away by rotation), so a repeat call
	parses just the bytes appended since; a different inode or a shorter file = read it again. A half-written last line waits."""
	try:
		st = p.stat()
	except FileNotFoundError:
		return []
	ino, done, events = _LOG.get(str(p), (None, 0, []))
	if ino != st.st_ino or st.st_size < done:
		done, events = 0, []
	if st.st_size > done:
		with open(p, "rb") as f:
			f.seek(done)
			chunk = f.read(st.st_size - done)
		end = chunk.rfind(b"\n") + 1
		events = events + [e for e in (_jline(x) for x in chunk[:end].decode("utf-8", "replace").splitlines()) if e is not None]
		done += end
	_LOG[str(p)] = (st.st_ino, done, events)
	return events


def _read_from(p: Path, off: int, size: int) -> tuple[list, int]:
	with open(p, "rb") as f:
		f.seek(off)
		chunk = f.read(size - off)
	chunk = chunk[:chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next call
	return [e for e in (_jline(x) for x in chunk.decode("utf-8", "replace").splitlines()) if e is not None], off + len(chunk)


def _jline(line: str) -> dict | None:
	try:
		e = json.loads(line)
	except json.JSONDecodeError:
		return None
	return e if isinstance(e, dict) else None


def _stamp(p: Path) -> tuple | None:
	try:
		st = p.stat()
	except FileNotFoundError:
		return None
	return (st.st_ino, st.st_mtime_ns, st.st_size)


def linked_worktree(path: Path) -> tuple[Path, Path] | None:
	"""(worktree root, main checkout root) when `path` is inside a linked git worktree (`git worktree add`: its .git is a FILE
	pointing at <main>/.git/worktrees/<name>, which has a `commondir`), else None (a normal checkout, a submodule, no git)."""
	for d in (path, *path.parents):
		g = d / ".git"
		if g.is_dir():
			return None
		if g.is_file():
			m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8", errors="replace").strip())
			if not m:
				return None
			gd = Path(m[1].strip())
			gd = gd if gd.is_absolute() else d / gd
			cf = gd / "commondir"
			if not cf.is_file():
				return None  # a submodule: its own repo, nothing to share
			common = (gd / cf.read_text(encoding="utf-8").strip()).resolve()
			return (d, common.parent) if common.name == ".git" else None  # a bare repo has no main checkout
	return None
