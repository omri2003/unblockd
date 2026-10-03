"""Tickets as JSON files: one file per ticket in TICKETS_DIR.

This is the only module that touches the files. The web API (web.py) and
the MCP server (mcp_server.py) both call it, so they can never disagree.
Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

STATUSES = ("todo", "doing", "pending", "done")
PRIORITIES = ("P0", "P1", "P2", "P3")
ID_RE = re.compile(r"^[A-Z0-9]+-\d+[a-z]?$")  # also the filename: blocks path traversal
PREFIX_RE = re.compile(r"^[A-Z0-9]+$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,39}$")  # lowercase; "agent:<name>" routes a ticket to that agent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")  # agent profile and board ids
STALE_DAYS = 7  # default; a doing ticket untouched this long is flagged (config: stale_days)
CONFIG_FILE = "unblockd.json"  # optional, next to the tickets
DEFAULT_CONFIG = {
	"title": "Tickets",
	"owners": [{"name": "Me", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
	"default_owner": None,  # None = first agent owner
	"pillar_label": "Pillar",
	"pillars": [],  # [{"key": "ART", "name": "Art", "color": "#8A4FB5", "color_dark": "#B58CE6"}]
	"stale_days": STALE_DAYS,
	"legacy_files": [],  # old ticket lists (relative to the tickets folder): new IDs never reuse a number seen there
	"id": None,  # board id when several boards are served; None = the project folder's name
	# Agent profiles: who should work a ticket. A ticket goes to the first profile sharing one of its
	# tags or its pillar; an explicit "agent:<name>" tag wins. Orchestrators read them via work_plan.
	"agents": [],  # [{"name": "godot-dev", "tags": ["gdscript"], "pillars": ["COMBAT"], "model": "sonnet", "instructions": "..."}]
}
PRIO_WEIGHT = {"P0": 8, "P1": 4, "P2": 2, "P3": 1, None: 1}

# Field -> (default, validator). Add a field here and the API + MCP accept it.
FIELDS = {
	"title": ("", lambda v: isinstance(v, str) and 0 < len(v.strip()) <= 200),
	"status": ("todo", lambda v: v in STATUSES),
	"priority": (None, lambda v: v is None or v in PRIORITIES),
	"owner": (None, lambda v: isinstance(v, str) and 0 < len(v) <= 40),
	"pillar": (None, lambda v: v is None or (isinstance(v, str) and len(v) <= 40)),
	"pillar_step": (None, lambda v: v is None or (isinstance(v, int) and v > 0)),
	"section": ("", lambda v: isinstance(v, str) and len(v) <= 80),
	"blocked_by": ([], lambda v: isinstance(v, list) and all(isinstance(x, str) and ID_RE.match(x) for x in v)),
	"waiting_on": ("", lambda v: isinstance(v, str) and len(v) <= 200),
	"body": ("", lambda v: isinstance(v, str) and len(v) <= 50_000),
	"tags": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and TAG_RE.match(x) for x in v)),
}


class TicketError(ValueError):
	"""Bad input from a caller (maps to HTTP 400 / an MCP tool error)."""


class NotFound(TicketError):
	pass


def load_config(tickets_dir: Path) -> dict:
	"""DEFAULT_CONFIG overlaid with <tickets_dir>/unblockd.json when it exists."""
	cfg = json.loads(json.dumps(DEFAULT_CONFIG))
	path = Path(tickets_dir) / CONFIG_FILE
	if path.exists():
		try:
			user = json.loads(path.read_text(encoding="utf-8"))
		except json.JSONDecodeError as e:
			raise TicketError(f"{path} is not valid JSON: {e}")
		if not isinstance(user, dict):
			raise TicketError(f"{path} must be a JSON object")
		unknown = set(user) - set(DEFAULT_CONFIG)
		if unknown:
			raise TicketError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}. Known: {', '.join(DEFAULT_CONFIG)}")
		cfg.update(user)
	if not cfg["owners"] or not all(isinstance(o, dict) and o.get("name") for o in cfg["owners"]):
		raise TicketError(f"{CONFIG_FILE}: owners must be a non-empty list of {{\"name\", \"kind\"}}")
	for pl in cfg["pillars"]:
		if not (isinstance(pl, dict) and PREFIX_RE.match(str(pl.get("key", "")))):
			raise TicketError(f"{CONFIG_FILE}: each pillar needs a key like ART (A-Z, 0-9)")
	for ag in cfg["agents"]:
		if not (isinstance(ag, dict) and NAME_RE.match(str(ag.get("name", "")))):
			raise TicketError(f"{CONFIG_FILE}: each agent needs a name like godot-dev (a-z, 0-9, - _)")
		if not all(isinstance(x, str) for x in ag.get("tags", []) + ag.get("pillars", [])):
			raise TicketError(f"{CONFIG_FILE}: agent {ag['name']}: tags and pillars are lists of strings")
	if not cfg["default_owner"]:
		agents = [o["name"] for o in cfg["owners"] if o.get("kind") == "agent"]
		cfg["default_owner"] = (agents or [cfg["owners"][0]["name"]])[0]
	return cfg


class Store:
	def __init__(self, tickets_dir: str | os.PathLike, legacy_files: list[str] | None = None):
		self.dir = Path(tickets_dir)
		self.dir.mkdir(parents=True, exist_ok=True)
		self.config = load_config(self.dir)
		self.stale_days = int(self.config["stale_days"])
		# Old ticket lists: new IDs never reuse a number seen there.
		self.legacy_files = [Path(p) for p in (legacy_files or []) if p] + [self.dir / p for p in self.config["legacy_files"]]

	# ---------- files ----------
	def _path(self, tid: str) -> Path:
		if not isinstance(tid, str) or not ID_RE.match(tid):
			raise TicketError(f"bad ticket id {tid!r}: expected like BUG-10 or ART-23")
		return self.dir / f"{tid}.json"

	def _read(self, path: Path) -> dict:
		with open(path, encoding="utf-8") as f:
			return json.load(f)

	def _write(self, t: dict) -> None:
		path = self._path(t["id"])
		fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			json.dump(t, f, indent=2, ensure_ascii=False)
			f.write("\n")
		os.replace(tmp, path)  # atomic: a reader never sees half a file

	def _all_raw(self) -> list[dict]:
		out = []
		for p in sorted(self.dir.glob("*.json")):
			if ID_RE.match(p.stem):
				out.append(self._read(p))
		return out

	# ---------- validation ----------
	def _clean(self, data: dict, partial: bool) -> dict:
		if not isinstance(data, dict):
			raise TicketError("ticket data must be an object")
		unknown = set(data) - set(FIELDS) - {"id", "created", "updated"}
		if unknown:
			raise TicketError(f"unknown field(s): {', '.join(sorted(unknown))}. Known: {', '.join(FIELDS)}")
		out = {}
		for k, v in data.items():
			if k in ("id", "created", "updated"):
				continue
			if k == "title" and isinstance(v, str):
				v = v.strip()
			if k == "tags" and isinstance(v, list):
				v = list(dict.fromkeys(x.strip().lower() for x in v if isinstance(x, str) and x.strip()))
			if not FIELDS[k][1](v):
				raise TicketError(f"invalid {k}: {v!r}" + (" (tags: lowercase a-z 0-9 : . _ -, max 40 chars, max 20)" if k == "tags" else ""))
			if k == "tags" and self.config["agents"]:
				names = [a["name"] for a in self.config["agents"]]
				bad = [x for x in v if x.startswith("agent:") and x[6:] not in names]
				if bad:
					raise TicketError(f"unknown agent in {', '.join(bad)}: agents are {', '.join(names)} (agents in {CONFIG_FILE})")
			if k == "owner" and v not in (names := [o["name"] for o in self.config["owners"]]):
				raise TicketError(f"invalid owner {v!r}: one of {', '.join(names)} (owners in {CONFIG_FILE})")
			if k == "pillar" and v is not None and self.config["pillars"] and v not in (keys := [p["key"] for p in self.config["pillars"]]):
				raise TicketError(f"invalid pillar {v!r}: one of {', '.join(keys)} or null (pillars in {CONFIG_FILE})")
			out[k] = v
		if not partial and "title" not in out:
			raise TicketError("title is required")
		return out

	def _check_loop(self, tid: str, blocked_by: list[str]) -> None:
		"""Refuse blocked_by that makes a ticket wait on itself, directly or down a chain."""
		if tid in blocked_by:
			raise TicketError(f"{tid} can't be blocked by itself")
		graph = {t["id"]: t.get("blocked_by", []) for t in self._all_raw()}
		graph[tid] = blocked_by
		parent: dict[str, str] = {}
		stack = [(b, tid) for b in blocked_by]
		while stack:
			x, via = stack.pop()
			if x in parent:
				continue
			parent[x] = via
			if x == tid:
				path = [tid]
				while path[-1] != tid or len(path) == 1:
					path.append(parent[path[-1]])
				raise TicketError("blocked_by makes a loop: " + " → ".join(reversed(path)) + " (each would wait on the other forever)")
			stack += [(b, x) for b in graph.get(x, [])]

	# ---------- derived view ----------
	def _decorate(self, tickets: list[dict]) -> list[dict]:
		"""Add derived fields. A todo with an open blocker shows as pending (effective_status).
		unblocks = not-done tickets downstream, missing_blockers = blocked_by ids with no file,
		stale_days = days since update for a doing ticket, score = priority weight x (1 + unblocks)."""
		by_id = {t["id"]: t for t in tickets}
		blocks: dict[str, list[str]] = {t["id"]: [] for t in tickets}
		for t in tickets:
			for b in t.get("blocked_by", []):
				if b in blocks:
					blocks[b].append(t["id"])
		def unblocks(tid: str) -> int:
			seen: set[str] = set()
			stack = list(blocks[tid])
			while stack:
				x = stack.pop()
				if x not in seen:
					seen.add(x)
					stack += blocks.get(x, [])
			seen.discard(tid)
			return sum(1 for x in seen if by_id[x].get("status") != "done")

		today = date.today()
		out = []
		for t in tickets:
			open_b = [b for b in t.get("blocked_by", []) if b in by_id and by_id[b].get("status") != "done"]
			eff = "pending" if t.get("status") == "todo" and open_b else t.get("status")
			n = unblocks(t["id"])
			stale = None
			if t.get("status") == "doing":
				try:
					stale = max(0, (today - date.fromisoformat(str(t.get("updated"))[:10])).days)
				except ValueError:
					stale = None
			out.append({**t, "effective_status": eff, "open_blockers": open_b, "blocks": blocks[t["id"]],
			            "unblocks": n, "missing_blockers": [b for b in t.get("blocked_by", []) if b not in by_id],
			            "stale_days": stale, "score": PRIO_WEIGHT.get(t.get("priority"), 1) * (1 + n),
			            "tags": t.get("tags", []), "agent": self.route(t)})
		return out

	def route(self, t: dict) -> str | None:
		"""The agent profile that should work this ticket, or None."""
		tags = t.get("tags", [])
		for x in tags:
			if x.startswith("agent:"):
				return x[6:]
		for ag in self.config["agents"]:
			if set(tags) & set(ag.get("tags", [])) or (t.get("pillar") and t["pillar"] in ag.get("pillars", [])):
				return ag["name"]
		return None

	def work_plan(self, per_agent: int = 3) -> dict:
		"""Ready tickets not owned by a human, grouped by the agent profile that should take them
		(best first), for an orchestrator to fan out: {"agents": [{profile..., "tickets": [...]}], "unrouted": [...]}."""
		ready = self.next_ready(limit=10_000)
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		ready = [t for t in ready if t.get("owner") not in humans]  # a person's ticket is theirs, whatever its tags
		out = []
		for ag in self.config["agents"]:
			mine = [t for t in ready if t["agent"] == ag["name"]]
			out.append({**ag, "ready": len(mine), "tickets": mine[:max(0, int(per_agent))]})
		unrouted = [t for t in ready if t["agent"] is None]
		return {"agents": out, "unrouted": unrouted[:max(0, int(per_agent))], "unrouted_count": len(unrouted)}

	# ---------- CRUD ----------
	def list(self, status: str | None = None, pillar: str | None = None, owner: str | None = None,
	         priority: str | None = None, q: str | None = None, tag: str | None = None,
	         agent: str | None = None) -> list[dict]:
		"""All tickets, decorated. Filters match effective_status (so 'pending' includes blocked todos)."""
		res = self._decorate(self._all_raw())
		if status:
			res = [t for t in res if t["effective_status"] == status]
		if pillar:
			res = [t for t in res if (t.get("pillar") or "").upper() == pillar.upper()]
		if owner:
			res = [t for t in res if t.get("owner", "").lower() == owner.lower()]
		if priority:
			res = [t for t in res if t.get("priority") == priority]
		if tag:
			res = [t for t in res if tag.lower() in t["tags"]]
		if agent:
			res = [t for t in res if t["agent"] == agent]
		if q:
			words = q.lower().split()
			res = [t for t in res if all(w in json.dumps(t, ensure_ascii=False).lower() for w in words)]
		return res

	def next_ready(self, owner: str | None = None, pillar: str | None = None, limit: int = 5,
	               agent: str | None = None, tag: str | None = None) -> list[dict]:
		"""Ready tickets (effective todo: not blocked, started or done), best first:
		score = priority weight x (1 + open tickets it unblocks), then priority, then id."""
		res = self.list(status="todo", pillar=pillar, owner=owner, agent=agent, tag=tag)
		res.sort(key=lambda t: (-t["score"], PRIORITIES.index(t["priority"]) if t.get("priority") else 9, t["id"]))
		return res[:max(0, int(limit))]

	def get(self, tid: str) -> dict:
		path = self._path(tid)
		if not path.exists():
			raise NotFound(f"no ticket {tid}")
		target = self._read(path)
		for t in self._decorate(self._all_raw()):
			if t["id"] == target["id"]:
				return t
		return target

	def next_id(self, prefix: str) -> str:
		if not PREFIX_RE.match(prefix or ""):
			raise TicketError(f"bad prefix {prefix!r}: expected like BUG or ART")
		pat = re.compile(rf"(?<![A-Z0-9]){re.escape(prefix)}-(\d+)")
		nums = [int(m) for p in self.dir.glob(f"{prefix}-*.json") for m in pat.findall(p.stem)]
		for f in self.legacy_files:
			if f.exists():
				nums += [int(m) for m in pat.findall(f.read_text(encoding="utf-8", errors="ignore"))]
		return f"{prefix}-{max(nums, default=0) + 1}"

	def create(self, data: dict, tid: str | None = None, prefix: str | None = None) -> dict:
		"""Create from `data`. Give an explicit `tid`, or a `prefix` to get the next free number."""
		clean = self._clean(data, partial=False)
		if tid is None:
			if not prefix:
				raise TicketError("give an id or a prefix (e.g. BUG)")
			tid = self.next_id(prefix)
		if self._path(tid).exists():
			raise TicketError(f"{tid} already exists")
		self._check_loop(tid, clean.get("blocked_by", []))
		today = date.today().isoformat()
		t = {"id": tid}
		for k, (default, _) in FIELDS.items():
			t[k] = clean.get(k, default if not isinstance(default, list) else list(default))
		if t["owner"] is None:
			t["owner"] = self.config["default_owner"]
		t["created"] = t["updated"] = today
		self._write(t)
		return self.get(tid)

	def update(self, tid: str, changes: dict) -> dict:
		path = self._path(tid)
		if not path.exists():
			raise NotFound(f"no ticket {tid}")
		t = self._read(path)
		clean = self._clean(changes, partial=True)
		if "blocked_by" in clean:
			self._check_loop(tid, clean["blocked_by"])
		t.update(clean)
		t["updated"] = date.today().isoformat()
		self._write(t)
		return self.get(tid)

	def delete(self, tid: str) -> dict:
		path = self._path(tid)
		if not path.exists():
			raise NotFound(f"no ticket {tid}")
		t = self._read(path)
		path.unlink()
		return t


def from_env() -> Store:
	return Store(
		os.environ.get("TICKETS_DIR", "tickets"),
		[p for p in os.environ.get("UNBLOCKD_LEGACY", "").split(os.pathsep) if p],
	)
