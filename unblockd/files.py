"""The files connector: one JSON file per ticket in a tickets folder (the default, and what a git repo holds).

Storage only. Every rule (validation, routing, claims logic, derived fields) lives in store.Store, which calls a
connector through the methods below; a database connector implements the same ones (see docs/reference.md, Connectors).
Runtime state (lock, claims.json, activity.jsonl, prs.json) lives in <tickets>/.unblockd, which ignores itself in git.
Stdlib only.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import TicketError

CONFIG_FILE = "unblockd.json"  # optional, next to the tickets
ID_RE = re.compile(r"^[A-Z0-9]+-\d+[a-z]?$")  # also the filename: blocks path traversal
ACTIVITY_MAX = 5_000_000  # bytes; past this activity.jsonl becomes activity.1.jsonl (one old file kept)
LOCK_STALE = 30  # seconds: a lock file older than this was left by a crash
BUSY_S = 10  # how long a write waits for the board lock before "try again"
_UMASK = os.umask(0)  # read once at import (os.umask is process-wide: setting it per write would race the web server's threads)
os.umask(_UMASK)
NEW_MODE = 0o666 & ~_UMASK  # what open() gives a new file: 0644 under the usual umask 022 (CORE-75)
WINDOWS = os.name == "nt"
REPLACE_WAIT = 5  # seconds replace() retries a Windows PermissionError (a reader has the target open) before giving up

# Per-process caches keyed by folder / file path, so every Store on the same folder (the web server builds one per request)
# shares them. Files stay the only truth: every read re-stats the folder and re-parses only files whose (inode, mtime, size)
# changed, so another process's write, a git pull or a hand edit is seen on the next call. Never written back.
_RAW: dict[tuple, dict] = {}  # (tickets folder, parse) -> {stem: (stamp, parsed ticket)}: a raw reader never shares a parsed cache
_LOG: dict[str, tuple] = {}  # activity file -> (inode, bytes parsed, [events oldest first])
_TIX: dict[tuple, tuple] = {}  # _RAW key -> (the _RAW dict, tickets() built from it)
_VER: dict[tuple, tuple] = {}  # _RAW key -> (that _RAW dict, its number from _GEN): version()'s ticket part
_GEN = itertools.count(1)
# Single-flight stat passes (CORE-85): one pass per folder at a time in this process; a thread that asks while a pass runs waits for
# it and reuses the NEXT pass that started after it asked (as fresh as its own would be), so 200 threads reading the board make a
# few passes, not 200. _PASS[key] = (passes started, last pass done)
_SCANQ: dict[tuple, threading.Lock] = {}
_PASS: dict[tuple, tuple] = {}
# Reuse of a fresh stat pass (CORE-60): one page load reads the same board ~4 times. A read OUTSIDE the lock may reuse the
# folder's last stat pass while it is younger than SCAN_REUSE_S and this process wrote no ticket since; under the lock every
# read stats again (claims and read-modify-writes must see other processes' writes). The reuse also needs the folder's own
# mtime_ns unchanged (ONE stat): os.replace, which every store write and git checkout uses, changes it, so another process's
# write is seen on the very next read; only an in-place editor save can wait out the window. 0 = always stat (CLI, MCP,
# tests); web.main sets 1 s.
SCAN_REUSE_S = 0.0
_SCANNED: dict[tuple, tuple] = {}  # _RAW key -> (time.monotonic() when its last pass started, _WROTE count then, folder mtime_ns then)
_WROTE: dict[str, int] = {}  # tickets folder -> ticket writes / locked steps by this process (any one drops the reuse)

# Slow storage (UI-81): on Docker Desktop (Windows / macOS) a bind-mounted project costs ~0.6 ms per file stat, ~75x a native
# disk (docs/reference.md "Docker Desktop"). _scan times the stats of every pass it really makes (no extra filesystem call:
# a reused pass is not timed) and keeps the last SLOW_PASSES per-file times of boards with >= SLOW_MIN tickets; storage() =
# their median. slow = inside a container AND median over SLOW_STAT_MS: a native board (Windows, macOS, Linux) is never
# flagged, and a Linux host's bind mount is native speed. Env UNBLOCKD_SLOW_STORAGE=1 / 0 forces it (demos, tests).
SLOW_STAT_MS = 0.1
SLOW_MIN = 10  # fewer ticket files than this: too few to judge, never measured
SLOW_PASSES = 3
_STAT_MS: dict[str, list] = {}  # tickets folder -> ms per file stat of its last SLOW_PASSES scans
_stat = lambda e: e.stat()  # the stat _scan times; tests swap in a slow one
_CONTAINER: list = []  # in_container(), asked once per process


def in_container() -> bool:
	if not _CONTAINER:
		_CONTAINER.append(Path("/.dockerenv").exists() or bool(os.environ.get("container")))
	return _CONTAINER[0]


def storage(tickets_dir: str | os.PathLike, key: str | None = None) -> dict:
	"""{per_file_ms, passes, slow} for a files board, from the stat passes its reads already made (none yet: per_file_ms None)."""
	runs = _STAT_MS.get(key or str(Path(tickets_dir).resolve()), [])
	per = round(sorted(runs)[len(runs) // 2], 4) if runs else None
	force = os.environ.get("UNBLOCKD_SLOW_STORAGE", "")
	slow = force == "1" or (force != "0" and per is not None and per > SLOW_STAT_MS and in_container())
	return {"per_file_ms": per, "passes": len(runs), "slow": slow}


class FileBackend:
	"""A board stored as files. `parse` turns a ticket file's JSON into the stored form (the rules' upgrade of old files)."""
	kind = "files"
	busy_s = property(lambda self: BUSY_S)  # the lock wait (store.Store._locked's in-process queue waits as long)

	def __init__(self, tickets_dir: str | os.PathLike, parse=lambda t: t):
		self.dir = Path(tickets_dir)
		self.dir.mkdir(parents=True, exist_ok=True)
		self.parse = parse
		self.key = str(self.dir.resolve())
		self._held = False  # inside lock(): the first _scan of the hold stats (CORE-60), later ones reuse it (_lraw, CORE-85)
		self._lraw = None  # the stat pass of the current lock hold, with this backend's own writes patched in
		self._v = None  # the number of the stat pass tickets() last returned (version())
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
			return json.loads(read_text(path)), str(path)
		except json.JSONDecodeError as e:
			raise TicketError(f"{path} is not valid JSON: {e}")

	def write_config(self, raw: dict) -> None:
		p = self.dir / CONFIG_FILE
		tmp = p.with_suffix(".tmp")
		tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")  # LF on every OS: a committed config must not turn CRLF (CORE-87)
		replace(tmp, p)

	# ---------- tickets ----------
	def path(self, tid: str) -> Path:
		return self.dir / f"{tid}.json"

	def read_file(self, path: Path) -> dict:
		return self.parse(json.loads(read_text(path)))

	def tickets(self) -> dict[str, dict]:
		"""{id: ticket} for every ticket, in id order, re-parsing only the files that changed since the last call (any Store,
		this process). The dicts (and the {id: ticket} one itself, kept while nothing changed: CORE-85) are shared with the cache:
		callers copy before changing them."""
		raw, ck = self._scan(), (self.key, self.parse)
		hit = _TIX.get(ck)
		if hit is None or hit[0] is not raw:
			hit = _TIX[ck] = (raw, {k: t for k, (_, t) in raw.items()})
		return hit[1]

	def _scan(self) -> dict[str, tuple]:
		ck = (self.key, self.parse)
		if self._held and self._lraw is not None:  # CORE-85: one stat pass per lock hold (no other process writes meanwhile;
			return self._lraw  # this one's own writes patch it in, _patch)
		gen, last = _WROTE.get(self.key, 0), _SCANNED.get(ck)
		mt = os.stat(self.dir).st_mtime_ns if SCAN_REUSE_S else 0  # read before the pass: a write during it changes it
		if (last and last[1] == gen and last[2] == mt and not self._held and time.monotonic() - last[0] < SCAN_REUSE_S
				and ck in _RAW):
			return self._seen(ck, _RAW[ck])  # fresh, nothing written by this process since, no file added / replaced / removed (outside the lock only)
		asked = _PASS.get(ck, (0, 0))[0]  # passes started before this call: none of them is fresh enough
		with _SCANQ.setdefault(ck, threading.Lock()):
			began, done = _PASS.get(ck, (0, 0))
			if done > asked and ck in _RAW:  # a pass that started after this call asked has finished meanwhile
				raw = self._seen(ck, _RAW[ck])
				if self._held:
					self._lraw = raw
				return raw
			_PASS[ck] = (began + 1, done)
			raw = self._pass(ck, gen, mt)
			_PASS[ck] = (began + 1, began + 1)
			return raw

	def _pass(self, ck: tuple, gen: int, mt: int) -> dict[str, tuple]:
		started = time.monotonic()  # taken before the pass: a file changed during it is not older than the stamp says
		old, new, spent, timed, same = _RAW.get(ck, {}), {}, 0.0, 0, True
		with os.scandir(self.dir) as it:
			for k, e in enumerate(it):
				name = e.name
				if not name.endswith(".json"):
					continue
				stem = name[:-5]
				hit = old.get(stem)
				if hit is None and not ID_RE.match(stem):  # a name seen before already matched
					continue
				if k & 7:
					st = _stat(e)
				else:  # UI-81: what a stat costs on this storage (storage()), timed on every 8th file: timing each one cost
					t0 = time.perf_counter()  # a quarter of a 20,000-ticket pass (CORE-85)
					st = _stat(e)
					spent += time.perf_counter() - t0
					timed += 1
				stamp = (st.st_ino, st.st_mtime_ns, st.st_size)  # os.replace makes a new file, so a rewrite changes the stamp even within one clock tick
				if hit and hit[0] == stamp:
					new[stem] = hit
				else:
					new[stem], same = (stamp, self.read_file(Path(e.path))), False
		if len(new) >= SLOW_MIN and timed:
			_STAT_MS[self.key] = (_STAT_MS.get(self.key, []) + [spent * 1000 / timed])[-SLOW_PASSES:]
		_SCANNED[ck] = (started, gen, mt)  # gen and mt read before the pass: a write during it drops the reuse
		if same and len(new) == len(old) and ck in _RAW:
			raw = self._seen(ck, _RAW[ck])  # nothing changed: the cached dict (and so the version) stays, no sort
		else:
			raw = self._publish(ck, dict(sorted(new.items())))
		if self._held:
			self._lraw = raw
		return raw

	def _publish(self, ck: tuple, raw: dict) -> dict:
		_RAW[ck] = raw
		self._v = next(_GEN)
		_VER[ck] = (raw, self._v)
		return raw

	def _seen(self, ck: tuple, raw: dict) -> dict:
		v = _VER.get(ck)
		if v is None or v[0] is not raw:  # the cache was emptied or replaced from outside: a new version
			return self._publish(ck, raw)
		self._v = v[1]
		return raw

	def _patch(self, tid: str, deleted: bool = False) -> None:
		"""Inside the lock, after this backend wrote or deleted tid: the hold's stat pass with that one file changed, so the next read
		in the same hold needs no new pass (CORE-85: a create read the 20,000-file folder 4 times under the lock)."""
		if not (self._held and self._lraw is not None):
			return
		raw = dict(self._lraw)
		if deleted:
			raw.pop(tid, None)
		else:
			p = self.path(tid)
			st = os.stat(p)
			new = tid not in raw
			raw[tid] = ((st.st_ino, st.st_mtime_ns, st.st_size), self.read_file(p))
			if new:
				raw = dict(sorted(raw.items()))
		self._lraw = self._publish((self.key, self.parse), raw)

	def _wrote(self) -> None:
		_WROTE[self.key] = _WROTE.get(self.key, 0) + 1  # the next read stats again (CORE-60)

	def version(self):
		"""Changes whenever a ticket, a claim or an attachment changed (read after tickets()): the derived board is rebuilt only then.
		The ticket part = the number of the stat pass tickets() returned (CORE-85: was a tuple of 20,000 stamps per call)."""
		return (self._v if self._v is not None else object(), _stamp(self.rt / "claims.json"), self._files_sig())

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
		p = self.path(t["id"])
		try:
			mode = stat.S_IMODE(os.stat(p).st_mode)  # a rewrite keeps the file's mode
		except FileNotFoundError:
			mode = NEW_MODE
		fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			json.dump(t, f, indent=2, ensure_ascii=False)
			f.write("\n")
		os.chmod(tmp, mode)  # mkstemp makes it 0600 and os.replace keeps that: a root server left tickets other users couldn't read (CORE-75)
		replace(tmp, p)  # atomic: a reader never sees half a file
		self._wrote()
		self._patch(t["id"])

	def delete(self, tid: str) -> None:
		"""The ticket and its attachments."""
		self.path(tid).unlink()
		self._wrote()
		self._patch(tid, deleted=True)
		shutil.rmtree(self.dir / "files" / tid, ignore_errors=True)

	def numbers(self, prefix: str) -> list[int]:
		"""Numbers already used by ids with this prefix (ART-23a counts as 23)."""
		pat = re.compile(rf"{re.escape(prefix)}-(\d+)")  # from the stat pass (CORE-85: a glob of the folder was a third of a create at 20,000)
		return [int(m.group(1)) for tid in self.tickets() if (m := pat.match(tid))]

	# ---------- the board lock ----------
	def runtime(self) -> Path:
		d = self.rt
		if not (d / ".gitignore").is_file():  # also when something else made the folder first: it holds a git mirror too, which must never show in git status
			d.mkdir(parents=True, exist_ok=True)  # parallel first calls race here
			(d / ".gitignore").write_text("*\n", encoding="utf-8")
		return d

	@contextmanager
	def lock(self, steal=None):
		"""Hold the board lock: a file created exclusively, so two processes can't both hold it. `steal(lock_path)` removes a
		stale one (store.Store._steal: re-checks the age under a guard). Not re-entrant: store.Store makes it so."""
		steal = steal or steal_stale
		lock = self.runtime() / "lock"
		deadline = time.monotonic() + BUSY_S
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
		self._held, self._lraw = True, None
		try:
			yield
		finally:
			self._held, self._lraw = False, None
			self._wrote()  # a locked step may have written or deleted a ticket: drop the reuse (CORE-60)
			os.close(fd)
			lock.unlink(missing_ok=True)

	# ---------- claims ----------
	# Runtime state, not ticket content, so heartbeats never show up as changes in the project repo.
	def claims(self) -> dict:
		p = self.rt / "claims.json"
		try:
			return json.loads(read_text(p)) if p.exists() else {}
		except json.JSONDecodeError as e:
			raise TicketError(f"{p} is not valid JSON ({e}), so no claim can be read: fix it by hand, or delete {p}: claims are runtime state "
			                  "and live workers will re-claim (tickets they held stay 'doing' until then)")

	def save_claims(self, claims: dict) -> None:
		p = self.runtime() / "claims.json"
		tmp = p.with_suffix(".tmp")
		tmp.write_text(json.dumps(claims, indent=2), encoding="utf-8")
		replace(tmp, p)

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


def _windows_retry(fn, path):
	"""fn(), retried on Windows while another thread or program has `path` open or is replacing it (CORE-86): there
	os.replace raises PermissionError over an open file, and an open() racing a replace does too; claims.json and ticket
	files are read without the board lock. Bounded by REPLACE_WAIT, then a TicketError saying what holds it. Elsewhere a
	PermissionError is real and raised at once."""
	deadline = time.monotonic() + REPLACE_WAIT
	while True:
		try:
			return fn()
		except PermissionError:
			if not WINDOWS:
				raise
			if time.monotonic() > deadline:
				raise TicketError(f"can't use {path}: another program kept it open for {REPLACE_WAIT} s (an editor, a virus "
				                  "scanner or a sync tool?): close it and try again") from None
			time.sleep(0.01)


def replace(tmp, dst) -> None:
	"""os.replace(tmp, dst) through _windows_retry; the temp file is removed when it fails."""
	try:
		_windows_retry(lambda: os.replace(tmp, dst), dst)
	except (OSError, TicketError):
		try:
			os.unlink(tmp)
		except OSError:
			pass
		raise


def read_text(path) -> str:
	"""Path(path).read_text(utf-8) through _windows_retry (a reader racing a replace)."""
	return _windows_retry(lambda: Path(path).read_text(encoding="utf-8"), path)


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
