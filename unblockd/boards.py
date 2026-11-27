"""Several boards behind one server. A board = one tickets folder (+ its unblockd.json).

Where boards come from (both may be used, both are rescanned on every call):
- UNBLOCKD_ROOT: folder(s) of projects, os.pathsep separated. Every <root>/tickets/ and
  <root>/<project>/tickets/ that has an unblockd.json is a board. Opt-in on purpose: a random
  "tickets" folder in some repo is not a board until you put the config there.
- TICKETS_DIR / UNBLOCKD_BOARDS: explicit tickets folders (os.pathsep separated), config optional, or database locations
  (`sqlite:///x.db#payments`, `postgres://u:p@host:5432/db#payments`, `mongodb://...#payments`; see connectors). A
  database board is listed even before it has a row: its row is created on first use, like a folder's config is optional.

A folder board's id is the config's "id", else its project folder's name; a database board's id is its #board. The first
board is the default.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import connectors as cn
from . import store as st
from . import system as sy


MOUNTINFO = Path("/proc/self/mountinfo")  # module attributes so tests can point them at fakes
DOCKERENV = Path("/.dockerenv")


def _mounts() -> list[tuple[str, str]] | None:
	"""[(mount point, root inside its source)] of this process, or None when not in a container (or not Linux).
	Only inside a container does "/" mean a filesystem that vanishes when the container is recreated."""
	if not (DOCKERENV.exists() or os.environ.get("container")):
		return None
	try:
		lines = MOUNTINFO.read_text(encoding="utf-8").splitlines()
	except OSError:
		return None
	esc = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)  # mountinfo writes a space as \040 
	return [(esc(f[4]), esc(f[3])) for f in (line.split(" - ")[0].split() for line in lines) if len(f) >= 5]


def volume_of(p: Path, mounts: list[tuple[str, str]]) -> tuple[str, str] | None:
	"""The mount (point, source root) that holds p, i.e. the longest mount point above it; None = only the container's own "/"."""
	s = p.as_posix()
	best = max((m for m in mounts if m[0] != "/" and (s == m[0] or s.startswith(m[0].rstrip("/") + "/"))), key=lambda m: len(m[0]), default=None)
	return best


def _slug(s: str) -> str:
	s = re.sub(r"[^a-z0-9_-]+", "-", s.lower()).strip("-")
	return s[:40] or "board"


class Boards:
	def __init__(self, roots: list[str] | None = None, dirs: list[str] | None = None, legacy: list[str] | None = None,
	             system: sy.System | None = None):
		self.roots = [Path(r) for r in (roots or []) if r]
		self.dirs = [d if cn.is_db(d) else Path(d) for d in (dirs or []) if d]  # a Path, or a database location (str)
		self.legacy = legacy or []
		self.system = system  # the system database (CORE-34): its registered boards are listed after the scanned ones
		self._from_system: set[str] = set()  # ids all() took from it (summary marks them registered)

	def registered(self) -> list[tuple[str, object]]:
		"""[(id, location)] from the system database; [] when there is none, or it can't be read (said once on stderr: the
		scanned boards must keep working)."""
		if self.system is None:
			return []
		try:
			return [(bid, loc if cn.is_db(loc) or isinstance(loc, sy.Broken) else Path(loc)) for bid, loc in self.system.locations()]
		except Exception as e:  # noqa: BLE001 - a broken system database hides only the registered boards
			sy.warn_once(f"{self.system.where}: {e}; registered boards are not listed")
			return []

	def _scan(self) -> list:
		found = [d for d in self.dirs if isinstance(d, str) or d.is_dir()]
		for r in self.roots:
			if not r.is_dir():
				continue
			cands = [r / "tickets"] + sorted(p / "tickets" for p in r.iterdir() if p.is_dir() and not p.name.startswith("."))
			found += [c for c in cands if (c / st.CONFIG_FILE).is_file()]
		uniq, seen = [], set()
		for p in found:
			k = p if isinstance(p, str) else p.resolve()
			if k not in seen:
				seen.add(k)
				uniq.append(p)
		return uniq

	def all(self) -> dict:
		"""id -> tickets folder (Path) or database location (str), in discovery order. Never connects to a database."""
		out: dict = {}
		for p in self._scan():
			bid = None
			if isinstance(p, str):
				try:
					bid = cn.split(p)[1]
				except st.TicketError:  # no #board: listed under a placeholder id, opening it says what is missing
					bid = "board"
			cfg = None if isinstance(p, str) else p / st.CONFIG_FILE
			if cfg is not None and cfg.is_file():
				try:
					bid = json.loads(cfg.read_text(encoding="utf-8")).get("id")
				except (json.JSONDecodeError, AttributeError):
					bid = None
			bid = _slug(bid or p.resolve().parent.name)  # bid is always set for a database location
			base, n = bid, 2
			while bid in out:
				bid, n = f"{base}-{n}", n + 1
			out[bid] = p
		seen = {p if isinstance(p, str) else p.resolve() for p in out.values()}
		self._from_system = set()
		for bid, p in self.registered():  # registered boards keep their id (a clash with a scanned board gets -2)
			if not isinstance(p, sy.Broken) and (p if isinstance(p, str) else p.resolve()) in seen:
				continue  # already listed by scanning
			base, n = bid, 2
			while bid in out:
				bid, n = f"{base}-{n}", n + 1
			out[bid] = p
			self._from_system.add(bid)
			if not isinstance(p, sy.Broken):
				seen.add(p if isinstance(p, str) else p.resolve())
		return out

	def get(self, board: str | None = None) -> st.Store:
		boards = self.all()
		if not boards:
			raise st.TicketError("no boards: set UNBLOCKD_ROOT to a folder of projects or TICKETS_DIR to a tickets folder")
		if board is None:
			bid = next(iter(boards))
			return self._open(bid, boards[bid])
		if board not in boards:
			raise st.NotFound(f"no board {board!r}. Boards: {', '.join(boards)}")
		return self._open(board, boards[board])

	def open(self, where, create: bool = True, bid: str | None = None) -> st.Store:
		"""The Store for one entry of all(): a folder or a database location. create=False: a database board must already exist
		(a registered board: a typo in its #board must say so, not quietly open a new empty board)."""
		if isinstance(where, sy.Broken):
			raise st.TicketError(str(where))
		if create or not cn.is_db(where):
			return st.Store(where, self.legacy)
		try:
			backend = cn.open_backend(where, create=False)
		except st.NotFound as e:
			raise st.TicketError(f"{e}: registered board {bid} points at a board that does not exist; fix its location or "
			                     f"`system remove-board {bid}`") from None
		return st.Store(backend=backend, legacy_files=self.legacy)

	def _open(self, bid: str, where) -> st.Store:
		"""open() for the board listed as bid: a registered one is never created by opening it."""
		return self.open(where, create=bid not in self._from_system, bid=bid)

	def summary(self) -> list[dict]:
		out = []
		every = self.all()
		reg = set(self._from_system)  # the entries that came from the system database
		for bid, p in every.items():
			extra = {"registered": True} if bid in reg else {}
			try:
				s = self._open(bid, p)
				ts = s.list()
				out.append({"id": bid, "title": s.config["title"], "path": cn.describe(str(p)), "tickets": len(ts),
				            "open": sum(t["status"] != "done" for t in ts),
				            "ready": sum(t["effective_status"] == "todo" for t in ts), **extra})
			except st.TicketError as e:  # a broken config shouldn't hide the other boards
				out.append({"id": bid, "title": bid, "path": "" if isinstance(p, sy.Broken) else cn.describe(str(p)), "error": str(e), **extra})
		return out

	def needs(self, board: str | None = None) -> list[dict]:
		"""What waits on a person, per board: [{id, title, items}] (Store.needs), boards with items only. board = just that one
		(unknown -> NotFound); None = every board, and a broken config hides only its own board. GET /api/needs and MCP needs_you."""
		boards = self.all()
		if board is not None and board not in boards:
			raise st.NotFound(f"no board {board!r}. Boards: {', '.join(boards)}")
		out = []
		for bid, path in boards.items():
			if board is not None and bid != board:
				continue
			try:
				s = self._open(bid, path)
				items = s.needs()
			except st.TicketError:
				if board is not None:
					raise
				continue
			if items:
				out.append({"id": bid, "title": s.config["title"], "items": items})
		return out

	def register(self, board_id: str, location: str = "", connector: str | None = None, title: str = "") -> dict:
		"""Add a board to the system database (CORE-34): refused when the id is already in the board list."""
		if self.system is None:
			raise st.TicketError("no system database")
		if board_id in self.all() and board_id not in self._from_system:
			raise st.TicketError(f"board {board_id} is already listed (found by scanning): pick another id")
		got = self.system.add_board(board_id, location, connector, title)
		where = dict(self.registered()).get(board_id)  # a database board is created HERE, on purpose, never later by opening it
		try:
			s = self.open(where)
			got["tickets"] = len(s.list())
			if getattr(s.backend, "created", False):
				got["new"] = True
				got["hint"] = "a new empty board was created there; if you meant an existing one, check the #board name"
		except st.TicketError as e:
			got["warnings"] = [f"registered, but it can't be opened yet: {e}"]
		return got

	def create(self, board_id: str, title: str = "", root: str | None = None) -> dict:
		"""New board under a root (default the first): <root>/<id>/tickets/unblockd.json. In a container, a root that is not on a mounted
		volume is refused: the board would live in the container's own filesystem and vanish when it is recreated. The reply adds
		`path` and, in a container, `host_path` (the folder on the mounted source, best effort)."""
		if not self.roots:
			raise st.TicketError("creating boards needs UNBLOCKD_ROOT (a folder the new board goes into)")
		if not st.NAME_RE.match(board_id or ""):
			raise st.TicketError(f"bad board id {board_id!r}: lowercase a-z, 0-9, - _ (max 40)")
		if root:
			hits = [r for r in self.roots if r.as_posix().rstrip("/") == str(root).replace("\\", "/").rstrip("/")]
			if not hits:
				raise st.TicketError(f"root {root!r} is not one of the server's UNBLOCKD_ROOT folders: {', '.join(r.as_posix() for r in self.roots)}")
			base = hits[0]
		else:
			base = self.roots[0]
		mounts = _mounts()
		vol = None
		if mounts is not None:
			vol = volume_of(base.absolute(), mounts)
			if vol is None:
				ok = [r.as_posix() for r in self.roots if volume_of(r.absolute(), mounts)]
				raise st.TicketError(f"{base.as_posix()} is inside the container, not on a mounted volume: a board created there vanishes when "
				                     "the container is recreated. " + (f"Pass root= one of the mounted roots: {', '.join(ok)}" if ok else
				                     "Mount a host folder (compose `volumes: - <host folder>:/projects`) and set UNBLOCKD_ROOT to it"))
		if board_id in self.all():
			raise st.TicketError(f"board {board_id} already exists")
		d = base / board_id / "tickets"
		if (d / st.CONFIG_FILE).exists():
			raise st.TicketError(f"{d / st.CONFIG_FILE} already exists")
		d.mkdir(parents=True, exist_ok=True)
		cfg = {"id": board_id, "title": (title or board_id).strip()[:80]}
		(d / st.CONFIG_FILE).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8", newline="\n")
		out = {**next(b for b in self.summary() if b["id"] == board_id), "path": d.as_posix(),
		       "agents_md": agents_md(board_id, d.as_posix()),
		       "agents_md_hint": "paste agents_md into the project's CLAUDE.md / AGENTS.md (or run `python -m unblockd.cli --dir <path> agents-md --write CLAUDE.md`) so every agent session claims, heartbeats and completes"}
		if vol:
			rel = d.absolute().as_posix()[len(vol[0].rstrip("/")):]
			out["host_path"] = vol[1].rstrip("/") + rel  # where the mount's source has it (on Docker Desktop a /run/desktop/... style path)
		return out


AGENTS_MD_START = "<!-- unblockd:agents-md start -->"
AGENTS_MD_END = "<!-- unblockd:agents-md end -->"
# The tools named here are WORK_LOOP.md's own steps (tests/test_agents_md.py fails when one is not in WORK_LOOP.md or the MCP tool set):
# this block is the short pointer every session reads (CLAUDE.md / AGENTS.md), the protocol stays in unblockd://work-loop.
NL, LF, CRLF = chr(10), bytes([10]), bytes([13, 10])
AGENTS_MD_TOOLS = ("claim_next", "claim_ticket", "heartbeat", "complete", "ask_human", "release")


def agents_md(board: str, location: str = "tickets") -> str:
	"""The <= 15-line block for a project's CLAUDE.md / AGENTS.md (AI-55): which board, claim -> heartbeat -> complete / ask_human,
	the CLI form for agents without MCP. `location` = the --dir the CLI takes (folder or database URL + #board, password hidden)."""
	loc = cn.describe(location) if cn.is_db(location) else location
	cli = f"python -m unblockd.cli --dir {loc}"
	return NL.join((
		AGENTS_MD_START,
		f"## Tickets: unblockd board `{board}`",
		"Work on this repo is tracked as tickets. Follow the worker protocol (MCP prompt `work_loop`, resource unblockd://work-loop); in short:",
		"1. Claim before you start: `claim_next(agent=<profile>-<n>)` or `claim_ticket(id, agent)`. Never build on a ticket you did not claim.",
		"2. Read the ticket, ALL its notes and its `context` first; `done_when` is the bar for done.",
		"3. `heartbeat(id, agent, note)` at least every 5 minutes: a silent claim expires and the ticket goes back to todo.",
		"4. Finish with `complete(id, agent, summary, files, refs)`: cause -> fix -> tests; NOT verified: ... Never move a ticket to done with update.",
		"5. A decision only a human can make: `ask_human(id, agent, question)`. Can't finish: `release(id, agent, reason)`.",
		f"Without MCP: `{cli} claim --agent NAME [--id ID]`, `heartbeat ID --agent NAME --note ...`,",
		"`complete ID --agent NAME --summary ... --file PATH --ref PR`, `ask ID --agent NAME --question ...` (other machine: set UNBLOCKD_URL).",
		AGENTS_MD_END,
	)) + NL


def write_agents_md(path: str | Path, block: str) -> dict:
	"""Append `block` to a CLAUDE.md / AGENTS.md. Refused when the start marker is already there; every existing byte stays as it was
	(only bytes are appended, matching the file's CRLF line endings when it uses them). A missing file is created."""
	p = Path(path)
	old = p.read_bytes() if p.exists() else b""
	if AGENTS_MD_START.encode() in old:
		raise st.TicketError(f"{p.as_posix()} already has the unblockd block (between {AGENTS_MD_START} and {AGENTS_MD_END}): edit or delete it there, then run again")
	nl = CRLF if CRLF in old else LF
	add = block.encode("utf-8").replace(LF, nl)
	if old:
		add = (b"" if old.endswith(LF) else nl) + nl + add
	with open(p, "ab") as f:
		f.write(add)
	return {"written": p.as_posix(), "lines": block.count(NL)}


def entries(value: str, sep: str = os.pathsep) -> list[str]:
	"""Split a list of board locations. On Linux/macOS the separator is ":", which database URLs contain
	(postgres://u:p@host:5432/db#b): a piece that starts a URL is joined with the next ones up to the one holding its #board."""
	parts, out, i = value.split(sep), [], 0
	while i < len(parts):
		cur = parts[i]
		if sep == ":" and i + 1 < len(parts) and cur + ":" in cn.DB_SCHEMES_NAMES and parts[i + 1].startswith("//"):
			while "#" not in cur and i + 1 < len(parts):
				i += 1
				cur += ":" + parts[i]
		if cur:
			out.append(cur)
		i += 1
	return out


def from_env() -> Boards:
	sep = os.pathsep
	env = os.environ
	dirs = entries(env.get("UNBLOCKD_BOARDS", ""))
	if env.get("TICKETS_DIR"):
		dirs.insert(0, env["TICKETS_DIR"])
	roots = [r for r in env.get("UNBLOCKD_ROOT", "").split(sep) if r]
	if not dirs and not roots:
		dirs = ["tickets"]
	return Boards(roots, dirs, [p for p in env.get("UNBLOCKD_LEGACY", "").split(sep) if p], system=sy.from_env())
