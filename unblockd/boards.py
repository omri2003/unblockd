"""Several boards behind one server. A board = one tickets folder (+ its unblockd.json).

Where boards come from (both may be used, both are rescanned on every call):
- UNBLOCKD_ROOT: folder(s) of projects, os.pathsep separated. Every <root>/tickets/ and
  <root>/<project>/tickets/ that has an unblockd.json is a board. Opt-in on purpose: a random
  "tickets" folder in some repo is not a board until you put the config there.
- TICKETS_DIR / UNBLOCKD_BOARDS: explicit tickets folders (os.pathsep separated), config optional.

A board's id is the config's "id", else its project folder's name. The first board is the default.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import store as st


def _slug(s: str) -> str:
	s = re.sub(r"[^a-z0-9_-]+", "-", s.lower()).strip("-")
	return s[:40] or "board"


class Boards:
	def __init__(self, roots: list[str] | None = None, dirs: list[str] | None = None, legacy: list[str] | None = None):
		self.roots = [Path(r) for r in (roots or []) if r]
		self.dirs = [Path(d) for d in (dirs or []) if d]
		self.legacy = legacy or []

	def _scan(self) -> list[Path]:
		found = [d for d in self.dirs if d.is_dir()]
		for r in self.roots:
			if not r.is_dir():
				continue
			cands = [r / "tickets"] + sorted(p / "tickets" for p in r.iterdir() if p.is_dir() and not p.name.startswith("."))
			found += [c for c in cands if (c / st.CONFIG_FILE).is_file()]
		uniq, seen = [], set()
		for p in found:
			k = p.resolve()
			if k not in seen:
				seen.add(k)
				uniq.append(p)
		return uniq

	def all(self) -> dict[str, Path]:
		"""id -> tickets folder, in discovery order."""
		out: dict[str, Path] = {}
		for p in self._scan():
			bid = None
			cfg = p / st.CONFIG_FILE
			if cfg.is_file():
				try:
					bid = json.loads(cfg.read_text(encoding="utf-8")).get("id")
				except (json.JSONDecodeError, AttributeError):
					bid = None
			bid = _slug(bid or p.resolve().parent.name)
			base, n = bid, 2
			while bid in out:
				bid, n = f"{base}-{n}", n + 1
			out[bid] = p
		return out

	def get(self, board: str | None = None) -> st.Store:
		boards = self.all()
		if not boards:
			raise st.TicketError("no boards: set UNBLOCKD_ROOT to a folder of projects or TICKETS_DIR to a tickets folder")
		if board is None:
			return st.Store(next(iter(boards.values())), self.legacy)
		if board not in boards:
			raise st.NotFound(f"no board {board!r}. Boards: {', '.join(boards)}")
		return st.Store(boards[board], self.legacy)

	def summary(self) -> list[dict]:
		out = []
		for bid, p in self.all().items():
			try:
				s = st.Store(p, self.legacy)
				ts = s.list()
				out.append({"id": bid, "title": s.config["title"], "path": str(p), "tickets": len(ts),
				            "open": sum(t["status"] != "done" for t in ts),
				            "ready": sum(t["effective_status"] == "todo" for t in ts)})
			except st.TicketError as e:  # a broken config shouldn't hide the other boards
				out.append({"id": bid, "title": bid, "path": str(p), "error": str(e)})
		return out

	def create(self, board_id: str, title: str = "") -> dict:
		"""New board under the first root: <root>/<id>/tickets/unblockd.json."""
		if not self.roots:
			raise st.TicketError("creating boards needs UNBLOCKD_ROOT (a folder the new board goes into)")
		if not st.NAME_RE.match(board_id or ""):
			raise st.TicketError(f"bad board id {board_id!r}: lowercase a-z, 0-9, - _ (max 40)")
		if board_id in self.all():
			raise st.TicketError(f"board {board_id} already exists")
		d = self.roots[0] / board_id / "tickets"
		if (d / st.CONFIG_FILE).exists():
			raise st.TicketError(f"{d / st.CONFIG_FILE} already exists")
		d.mkdir(parents=True, exist_ok=True)
		cfg = {"id": board_id, "title": (title or board_id).strip()[:80]}
		(d / st.CONFIG_FILE).write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8", newline="\n")
		return next(b for b in self.summary() if b["id"] == board_id)


def from_env() -> Boards:
	sep = os.pathsep
	env = os.environ
	dirs = [d for d in env.get("UNBLOCKD_BOARDS", "").split(sep) if d]
	if env.get("TICKETS_DIR"):
		dirs.insert(0, env["TICKETS_DIR"])
	roots = [r for r in env.get("UNBLOCKD_ROOT", "").split(sep) if r]
	if not dirs and not roots:
		dirs = ["tickets"]
	return Boards(roots, dirs, [p for p in env.get("UNBLOCKD_LEGACY", "").split(sep) if p])
