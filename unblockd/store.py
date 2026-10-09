"""Tickets as JSON files: one file per ticket in TICKETS_DIR.

This is the only module that touches the files. The web API (web.py) and
the MCP server (mcp_server.py) both call it, so they can never disagree.
Stdlib only.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

STATUSES = ("todo", "doing", "pending", "review", "done")
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
	"wip_limit": None,  # int: the UI warns when more tickets than this are in progress; None = no warning
	"lease_minutes": 15,  # a claim with no heartbeat for this long expires and the ticket goes back to todo
	"legacy_files": [],  # old ticket lists (relative to the tickets folder): new IDs never reuse a number seen there
	"id": None,  # board id when several boards are served; None = the project folder's name
	# Agent profiles: who should work a ticket. A ticket goes to the first profile sharing one of its
	# tags or its pillar; an explicit "agent:<name>" tag wins. Orchestrators read them via work_plan.
	"agents": [],  # [{"name": "godot-dev", "tags": ["gdscript"], "pillars": ["COMBAT"], "model": "sonnet", "instructions": "...", "docs": [...]}]
	# Knowledge every ticket on this board comes with: repo paths, folders or globs (see knowledge.py).
	"docs": [],  # ["AGENTS.md", "docs/HANDOFF.md"]
	"review": False,  # True: complete() lands the ticket in status "review" (still open, blocks dependents) until a human sets done
	"context_budget": 60_000,  # characters of docs inlined into a ticket's context; the rest is listed
}
BODY_MAX = 50_000
QUESTION_MAX = 1000  # chars in an ask() question / waiting_on
SUMMARY_MAX = 10_000  # chars in a complete() summary (stored in body and result)
ACTIVITY_MAX = 5_000_000  # bytes; past this activity.jsonl becomes activity.1.jsonl (one old file kept)
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
	# a group (epic), not a blocker: this ticket is a part of that one. Loop-checked, the parent must exist
	"parent": (None, lambda v: v is None or (isinstance(v, str) and ID_RE.match(v) is not None)),
	# the bar for done, apart from the background in `body`: shown first in context(), echoed into complete()'s body entry
	"done_when": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and 0 < len(x.strip()) <= 200 for x in v)),
	"waiting_on": ("", lambda v: isinstance(v, str) and len(v) <= QUESTION_MAX),
	# when a question was parked and which agent asked it (ask() sets both, answer() clears them); read-only. A hand-set waiting_on gets asked_at only
	"asked_at": ("", lambda v: isinstance(v, str) and len(v) <= 40),
	"asked_by": ("", lambda v: isinstance(v, str) and len(v) <= 40),
	"body": ("", lambda v: isinstance(v, str) and len(v) <= BODY_MAX),
	# paths/globs the work will edit; claim_next never hands out two live tickets that overlap
	"touches": ([], lambda v: isinstance(v, list) and len(v) <= 30 and all(isinstance(x, str) and 0 < len(x) <= 200 for x in v)),
	# repo paths / folders / globs this ticket's worker should read (knowledge.py); files attached
	# to the ticket live in tickets/files/<ID>/ and show up as the derived `files`
	"docs": ([], lambda v: isinstance(v, list) and len(v) <= 30 and all(isinstance(x, str) and 0 < len(x) <= 200 and ".." not in x for x in v)),
	"tags": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and TAG_RE.match(x) for x in v)),
	# what complete() produced, for reviewers and downstream tickets (context().upstream): {summary, files, refs, handoff, by, at}
	"result": (None, lambda v: v is None or (isinstance(v, dict) and set(v) <= RESULT_KEYS and isinstance(v.get("summary"), str)
	                                          and all(_short_list(v.get(k, [])) for k in ("files", "refs")) and isinstance(v.get("handoff", ""), str) and len(v.get("handoff", "")) <= 2000)),
}


RESULT_KEYS = {"summary", "files", "refs", "handoff", "by", "at"}


def _short_list(v) -> bool:
	return isinstance(v, list) and len(v) <= 50 and all(isinstance(x, str) and 0 < len(x.strip()) <= 300 for x in v)


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
	if not isinstance(cfg["review"], bool):
		raise TicketError(f"{CONFIG_FILE}: review must be true or false")
	if cfg["wip_limit"] is not None and not (type(cfg["wip_limit"]) is int and cfg["wip_limit"] > 0):
		raise TicketError(f"{CONFIG_FILE}: wip_limit must be a positive number or null")
	if not cfg["owners"] or not all(isinstance(o, dict) and o.get("name") for o in cfg["owners"]):
		raise TicketError(f"{CONFIG_FILE}: owners must be a non-empty list of {{\"name\", \"kind\"}}")
	for pl in cfg["pillars"]:
		if not (isinstance(pl, dict) and PREFIX_RE.match(str(pl.get("key", "")))):
			raise TicketError(f"{CONFIG_FILE}: each pillar needs a key like ART (A-Z, 0-9)")
	for ag in cfg["agents"]:
		if not (isinstance(ag, dict) and NAME_RE.match(str(ag.get("name", "")))):
			raise TicketError(f"{CONFIG_FILE}: each agent needs a name like godot-dev (a-z, 0-9, - _)")
		if not all(isinstance(x, str) for x in ag.get("tags", []) + ag.get("pillars", []) + ag.get("docs", [])):
			raise TicketError(f"{CONFIG_FILE}: agent {ag['name']}: tags, pillars and docs are lists of strings")
	if not (isinstance(cfg["docs"], list) and all(isinstance(x, str) for x in cfg["docs"])):
		raise TicketError(f"{CONFIG_FILE}: docs is a list of repo paths")
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
			t = json.load(f)
		if t.get("status") == "pending":  # derived only (effective_status); old files stored it, read as todo
			t["status"] = "todo"
		return t

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
			if k == "result":
				raise TicketError("result is set only by complete(): it can be read, not written")
			if k in ("asked_at", "asked_by"):
				raise TicketError(f"{k} is set by ask() and cleared by answer(): it can be read, not written")
			if k == "title" and isinstance(v, str):
				v = v.strip()
			if k == "tags" and isinstance(v, list):
				v = list(dict.fromkeys(x.strip().lower() for x in v if isinstance(x, str) and x.strip()))
			if k == "status" and v == "pending":
				raise TicketError("status can't be set to pending: it is derived. A todo with an open blocker (blocked_by) or a waiting_on shows as pending; "
				                  "agents use ask_human(id, agent, question), humans set waiting_on or blocked_by")
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

	def _check_blockers(self, new: list[str], old: list[str]) -> None:
		"""Refuse blocked_by ids that don't exist, unless already on the ticket (a blocker deleted later keeps it editable)."""
		have = {t["id"] for t in self._all_raw()}
		bad = [b for b in new if b not in have and b not in old]
		if bad:
			raise TicketError("; ".join(f"no ticket {b} on this board" for b in bad) + ": fix the id or create it first")

	def _check_parent(self, tid: str, parent: str | None) -> None:
		"""Refuse a parent that doesn't exist or that would make a ticket its own ancestor."""
		if parent is None:
			return
		if parent == tid:
			raise TicketError(f"{tid} can't be its own parent")
		up = {t["id"]: t.get("parent") for t in self._all_raw()}
		if parent not in up:
			raise TicketError(f"parent {parent} doesn't exist: create it first or set parent to null")
		chain, x = [tid, parent], up[parent]
		while x and x not in chain:
			chain.append(x)
			x = up.get(x)
		if x == tid:
			raise TicketError("parent makes a loop: " + " → ".join(chain + [tid]) + " (a ticket can't be part of its own part)")

	# ---------- derived view ----------
	def _decorate(self, tickets: list[dict]) -> list[dict]:
		"""Add derived fields. A todo with an open blocker or a waiting_on (a question for a human) shows as pending (effective_status).
		unblocks = not-done tickets downstream, missing_blockers = blocked_by ids with no file,
		stale_days = days since update for a doing ticket, score = priority weight x (1 + unblocks)."""
		live = self._live_claims()
		fd = self.dir / "files"
		attached = {d.name: sorted(f.name for f in d.iterdir() if f.is_file()) for d in fd.iterdir() if d.is_dir()} if fd.is_dir() else {}
		by_id = {t["id"]: t for t in tickets}
		blocks: dict[str, list[str]] = {t["id"]: [] for t in tickets}
		kids: dict[str, list[dict]] = {}
		for t in tickets:
			if t.get("parent") in by_id:
				kids.setdefault(t["parent"], []).append(t)
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
			eff = "pending" if t.get("status") == "todo" and (open_b or t.get("waiting_on")) else t.get("status")
			n = unblocks(t["id"])
			stale = None
			if t.get("status") == "doing":
				try:
					stale = max(0, (today - date.fromisoformat(str(t.get("updated"))[:10])).days)
				except ValueError:
					stale = None
			ks = kids.get(t["id"], [])
			out.append({**t, "parent": t.get("parent"), "children": [k["id"] for k in ks],
			            "progress": {"done": sum(k.get("status") == "done" for k in ks), "total": len(ks)},
			            "effective_status": eff, "open_blockers": open_b, "blocks": blocks[t["id"]],
			            "unblocks": n, "missing_blockers": [b for b in t.get("blocked_by", []) if b not in by_id],
			            "stale_days": stale, "score": PRIO_WEIGHT.get(t.get("priority"), 1) * (1 + n),
			            "tags": t.get("tags", []), "touches": t.get("touches", []), "agent": self.route(t),
			            # timestamps only (no ages), so a ticket's JSON doesn't change while nothing happens
			            "docs": t.get("docs", []), "files": attached.get(t["id"], []),
			            "claim": {k: v for k, v in live[t["id"]].items() if k not in ("age_s", "quiet_s")} if t["id"] in live else None})
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

	def profile_of(self, agent: str) -> str | None:
		"""The agent profile a worker name belongs to: the profile named exactly like it, or the one it starts with (godot-dev-1 -> godot-dev)."""
		return next((a["name"] for a in self.config["agents"] if agent == a["name"] or agent.startswith(a["name"] + "-")), None)

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
	         agent: str | None = None, parent: str | None = None) -> list[dict]:
		"""All tickets, decorated. Filters match effective_status (so 'pending' includes blocked todos)."""
		self._live_claims()  # expire stale claims first, so their tickets are read back as todo
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
			agent = self.profile_of(agent) or agent  # a worker name (godot-dev-1) means its profile, as in claim
			res = [t for t in res if t["agent"] == agent]
		if parent:
			res = [t for t in res if t["parent"] == parent]
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
		self._live_claims()
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
		return self._locked(lambda: self._create(data, tid, prefix))

	def _create(self, data: dict, tid: str | None = None, prefix: str | None = None, inherited: list[str] = ()) -> dict:
		clean = self._clean(data, partial=False)
		if tid is None:
			if not prefix:
				raise TicketError("give an id or a prefix (e.g. BUG)")
			tid = self.next_id(prefix)
		if self._path(tid).exists():
			raise TicketError(f"{tid} already exists")
		self._check_blockers(clean.get("blocked_by", []), inherited)
		self._check_loop(tid, clean.get("blocked_by", []))
		self._check_parent(tid, clean.get("parent"))
		today = date.today().isoformat()
		t = {"id": tid}
		for k, (default, _) in FIELDS.items():
			t[k] = clean.get(k, default if not isinstance(default, list) else list(default))
		if t["owner"] is None:
			t["owner"] = self.config["default_owner"]
		t["created"] = t["updated"] = today
		if t["waiting_on"]:
			t["asked_at"] = _now()
		self._write(t)
		self._log("created", tid, None, t["title"])
		return self.get(tid)

	def update(self, tid: str, changes: dict, agent: str | None = None) -> dict:
		"""Change fields. `agent` = the worker making the change (MCP/CLI agents); None = a human (web, CLI)."""
		return self._locked(lambda: self._update(tid, changes, agent))

	def _update(self, tid: str, changes: dict, agent: str | None = None) -> dict:
		path = self._path(tid)
		if not path.exists():
			raise NotFound(f"no ticket {tid}")
		t = self._read(path)
		clean = self._clean(changes, partial=True)
		self._holder_only(tid, agent)
		if "blocked_by" in clean:
			self._check_blockers(clean["blocked_by"], t.get("blocked_by", []))
			self._check_loop(tid, clean["blocked_by"])
		if "parent" in clean:
			self._check_parent(tid, clean["parent"])
		if clean.get("status") == "doing" and t.get("status") != "doing":
			open_b = [b for b in clean.get("blocked_by", t.get("blocked_by", []))
			          if self._path(b).exists() and self._read(self._path(b)).get("status") != "done"]
			if open_b:
				raise TicketError(f"{tid} is blocked by {', '.join(open_b)} (still open): finish or unlink it first")
		if clean.get("status", "doing") != "doing":  # moved off doing by hand: the claim ends with it
			self._drop_claim(tid, f"status set to {clean['status']}")
		was = t.get("status")
		if "waiting_on" in clean:  # by hand: age starts now, no asked_by (only ask() names an agent); cleared with the question
			if not clean["waiting_on"]:
				t["asked_at"] = t["asked_by"] = ""
			elif not t.get("asked_at"):
				t["asked_at"] = _now()
		t.update(clean)
		t["updated"] = date.today().isoformat()
		self._write(t)
		if t["status"] == "done" and was != "done":  # by hand; an agent's complete() logs its own event
			self._log("done", tid, None)
		return self.get(tid)

	def _holder_only(self, tid: str, agent: str | None) -> None:
		"""An agent that is not the live holder of tid may not change, split or delete it. agent None = a human: unchanged."""
		held = self._live_claims().get(tid) if agent else None
		if held and held["agent"] != agent:
			raise TicketError(f"{tid} is claimed by {held['agent']}; only the holder can change it "
			                  "(use add_note to leave a note / ask the holder / wait for the lease to expire)")

	def split(self, tid: str, titles: list[str], sequential: bool = False, by: str | None = None) -> dict:
		"""Break a ticket that is too big into parts. One child per title (same prefix; pillar, owner, priority,
		tags, touches, docs copied; inherits the parent's blocked_by; sequential = each also waits on the one before),
		and the parent now waits on all of them (a doing parent goes back to todo, its claim dropped).
		Returns {"parent": ..., "children": [...]}."""
		def run():
			parent = self.get(tid)
			self._holder_only(tid, by)
			if parent["status"] == "done":
				raise TicketError(f"{tid} is already done: split a ticket that is still open")
			if not (isinstance(titles, list) and all(isinstance(x, str) for x in titles)):
				raise TicketError("titles must be a list of strings, one per part")
			titles_ = [x.strip() for x in titles if x.strip()]
			if len(titles_) < 2:
				raise TicketError("split needs at least 2 parts: give 2 or more non-empty titles (for one part, just edit the ticket)")
			for x in titles_:  # validate everything before creating anything
				self._clean({"title": x}, partial=True)
			prefix = tid.split("-")[0]
			keep = {k: parent.get(k) for k in ("pillar", "owner", "priority", "tags", "touches", "docs")}
			kids: list[dict] = []
			for x in titles_:
				blocked = list(parent["blocked_by"]) + ([kids[-1]["id"]] if sequential and kids else [])
				kids.append(self._create({**keep, "title": x, "body": f"Split from {tid}.", "blocked_by": blocked, "parent": tid}, prefix=prefix, inherited=parent["blocked_by"]))
			ids = [k["id"] for k in kids]
			changes = {"blocked_by": list(parent["blocked_by"]) + ids}
			if parent["status"] == "doing":
				changes["status"] = "todo"  # update() drops the claim; a doing ticket can't have open blockers
			self.update(tid, changes)
			self._log("split", tid, by, ", ".join(ids))
			return {"parent": self.get(tid), "children": [self.get(i) for i in ids]}
		return self._locked(run)

	def delete(self, tid: str, agent: str | None = None) -> dict:
		"""Remove a ticket, its claim and attachments. Returns it plus `referenced_by`: tickets still pointing at it."""
		return self._locked(lambda: self._delete(tid, agent))

	def _delete(self, tid: str, agent: str | None = None) -> dict:
		path = self._path(tid)
		if not path.exists():
			raise NotFound(f"no ticket {tid}")
		self._holder_only(tid, agent)
		t = self._read(path)
		refs = [x["id"] for x in self._all_raw() if tid in x.get("blocked_by", []) or x.get("parent") == tid]
		path.unlink()
		self._drop_claim(tid, "ticket deleted")
		shutil.rmtree(self.dir / "files" / tid, ignore_errors=True)  # attachments
		self._log("deleted", tid, None, t["title"])
		return {**t, "referenced_by": refs}  # still list it in blocked_by / parent: not edited, now dangling

	# ---------- claims: which agent is working what, right now ----------
	# Runtime state, not ticket content: kept in <tickets>/.unblockd/ (git-ignored by a .gitignore
	# inside it), so heartbeats never show up as changes in the project repo.
	def _rt(self) -> Path:
		d = self.dir / ".unblockd"
		if not d.is_dir():
			d.mkdir(exist_ok=True)  # parallel first calls race here
			(d / ".gitignore").write_text("*\n", encoding="utf-8")
		return d

	def _locked(self, fn):
		"""Run fn under a lock file so two agents can't claim the same ticket. Re-entrant: claim()
		holds it while next_ready() may expire stale claims, which needs it too."""
		if getattr(self, "_held", False):
			return fn()
		lock = self._rt() / "lock"
		deadline = time.monotonic() + 10
		while True:
			try:
				fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
				break
			except FileExistsError:
				try:
					if time.time() - lock.stat().st_mtime > 30:  # left behind by a crash
						self._steal(lock)  # then fall through: deadline + sleep, so a stuck guard can't spin us
				except FileNotFoundError:
					continue
				if time.monotonic() > deadline:
					raise TicketError("another claim is in progress; try again")
				time.sleep(0.05)
		self._held = True
		try:
			return fn()
		finally:
			self._held = False
			os.close(fd)
			lock.unlink(missing_ok=True)

	@staticmethod
	def _steal(lock: Path) -> None:
		"""Remove a stale lock. Only the holder of lock.steal may, and it re-checks the age under it: two processes that both
		read the same stale mtime can't then delete the fresh lock the first one just created."""
		guard = lock.with_name("lock.steal")
		try:
			fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
		except FileExistsError:  # someone else is stealing; the caller retries. A guard left by a crash goes after 30 s too
			try:
				if time.time() - guard.stat().st_mtime > 30:
					guard.unlink()
			except FileNotFoundError:
				pass
			return
		try:
			if time.time() - lock.stat().st_mtime > 30:
				lock.unlink()
		except FileNotFoundError:
			pass
		finally:
			os.close(fd)
			guard.unlink(missing_ok=True)

	def _claims(self) -> dict:
		p = self.dir / ".unblockd" / "claims.json"
		try:
			return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
		except json.JSONDecodeError as e:
			raise TicketError(f"{p} is not valid JSON ({e}), so no claim can be read: fix it by hand, or delete {p}: claims are runtime state "
			                  "and live workers will re-claim (tickets they held stay 'doing' until then)")

	def _save_claims(self, claims: dict) -> None:
		p = self._rt() / "claims.json"
		tmp = p.with_suffix(".tmp")
		tmp.write_text(json.dumps(claims, indent=2), encoding="utf-8")
		os.replace(tmp, p)

	def _log(self, event: str, tid: str | None, agent: str | None, note: str = "") -> None:
		line = {"ts": _now(), "event": event, "ticket": tid, "agent": agent, "note": note[:500]}
		p = self._rt() / "activity.jsonl"
		if p.exists() and p.stat().st_size > ACTIVITY_MAX:
			self._locked(lambda: p.stat().st_size > ACTIVITY_MAX and os.replace(p, p.with_name("activity.1.jsonl")))
		with open(p, "a", encoding="utf-8") as f:
			f.write(json.dumps(line, ensure_ascii=False) + "\n")

	def _live_claims(self) -> dict:
		"""Claims with a recent heartbeat. Expired ones are released here (ticket back to todo)."""
		claims = self._claims()
		if not claims:
			return {}
		lease = float(self.config["lease_minutes"]) * 60
		now = time.time()
		dead = [tid for tid, c in claims.items() if now - _ts(c["heartbeat_at"]) > lease]
		if dead:
			def sweep():
				cur = self._claims()
				for tid in dead:
					c = cur.pop(tid, None)
					if c is None:
						continue
					path = self._path(tid)
					if path.exists():
						t = self._read(path)
						if t.get("status") == "doing":
							t["status"] = "todo"
							t["updated"] = date.today().isoformat()
							_append_note(t, c["agent"], f"claim by {c['agent']} expired (no heartbeat for {int(lease // 60)} min)", strict=False)
							self._write(t)
					self._log("expired", tid, c["agent"], f"no heartbeat for {int(lease // 60)} min; back to todo")
				self._save_claims(cur)
			self._locked(sweep)
			claims = self._claims()
		return {tid: {**c, "age_s": int(now - _ts(c["claimed_at"])), "quiet_s": int(now - _ts(c["heartbeat_at"]))}
		        for tid, c in claims.items()}

	def _drop_claim(self, tid: str, why: str) -> None:
		claims = self._claims()
		if tid in claims:
			def drop():
				cur = self._claims()
				c = cur.pop(tid, None)
				if c:
					self._save_claims(cur)
					self._log("dropped", tid, c["agent"], why)
			self._locked(drop)

	def _claimable(self, t: dict, live: dict, by_id: dict) -> bool:
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		if t["effective_status"] != "todo" or t.get("owner") in humans or t["id"] in live:
			return False
		mine = t.get("touches", [])
		if mine:
			for other in live:
				if any(_overlap(a, b) for a in mine for b in by_id.get(other, {}).get("touches", [])):
					return False
		return True

	def claim(self, agent: str, tid: str | None = None, profile: str | None = None, note: str = "") -> dict | None:
		"""Take a ticket for `agent` (a unique worker name, e.g. godot-dev-1): the given one, or the best
		ready ticket for `profile` (default: the profile named like the agent, else any) that no live
		claim overlaps. Sets it to doing. Returns the ticket, or None when nothing is claimable."""
		if not (isinstance(agent, str) and NAME_RE.match(agent)):
			raise TicketError(f"bad agent name {agent!r}: lowercase a-z, 0-9, - _ (e.g. godot-dev-1)")
		profiles = [a["name"] for a in self.config["agents"]]
		if profile is None:
			profile = self.profile_of(agent)
			if profiles and profile is None:
				raise TicketError(f"worker {agent!r} matches no agent profile, so its tickets can't be routed: name workers <profile>-N "
				                  f"(profiles: {', '.join(profiles)}; e.g. {profiles[0]}-1) or pass profile=")
		elif profile not in profiles:
			raise TicketError(f"unknown profile {profile!r}: {', '.join(profiles) or 'none configured'}")

		def take():
			live = self._live_claims()
			by_id = {x["id"]: x for x in self._all_raw()}  # read once: _claimable looks up live claims' touches in it
			if tid:
				t = self.get(tid)
				if tid in live:
					raise TicketError(f"{tid} is already claimed by {live[tid]['agent']}")
				if not self._claimable(t, live, by_id):
					raise TicketError(f"{tid} can't be claimed: it's {t['effective_status']}"
					                  + (f", owned by {t['owner']}" if t["effective_status"] == "todo" else "")
					                  + " (or its touches overlap a live claim)")
				pick = t
			else:
				ready = self.next_ready(limit=10_000, agent=profile)
				pick = next((t for t in ready if self._claimable(t, live, by_id)), None)
				if pick is None:
					return None
			claims = self._claims()
			now = _now()
			claims[pick["id"]] = {"agent": agent, "profile": profile, "claimed_at": now, "heartbeat_at": now, "note": note[:300]}
			self._save_claims(claims)
			raw = self._read(self._path(pick["id"]))
			raw["status"], raw["updated"] = "doing", date.today().isoformat()
			self._write(raw)
			self._log("claim", pick["id"], agent, note)
			return pick["id"]

		got = self._locked(take)
		return self.get(got) if got else None

	def claim_next(self, agent: str, profile: str | None = None, note: str = "") -> dict:
		"""claim() for a worker that takes whatever is next: the claimed ticket, or, when there is none, WHY, so the worker
		knows whether to retry: {"ticket": None, "reason": "dry"|"conflict"|"waiting", "counts": {...}}.
		conflict = ready tickets exist but their touches overlap a live claim (retry later); waiting = open tickets exist
		but none is ready (in flight, in review, waiting on a human, blocked); dry = nothing open for this profile."""
		got = self.claim(agent, profile=profile, note=note)
		if got:
			return got
		if profile is None:
			profile = self.profile_of(agent)
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		counts = dict.fromkeys(("in_flight", "ready_conflicting", "in_review", "waiting_on_human", "blocked"), 0)
		for t in self.list():
			if t["status"] == "done" or (profile and t["agent"] != profile):
				continue
			if t["status"] == "doing":
				counts["in_flight"] += 1
			elif t["status"] == "review":
				counts["in_review"] += 1
			elif t.get("waiting_on") or t.get("owner") in humans:
				counts["waiting_on_human"] += 1
			elif t["effective_status"] == "todo":
				counts["ready_conflicting"] += 1
			else:
				counts["blocked"] += 1
		reason = "conflict" if counts["ready_conflicting"] else "waiting" if any(counts.values()) else "dry"
		return {"ticket": None, "reason": reason, "counts": counts}

	def _holder(self, tid: str, agent: str) -> None:
		c = self._live_claims().get(tid)
		if not c:
			raise TicketError(f"{tid} isn't claimed (or the claim expired): claim it again")
		if c["agent"] != agent:
			raise TicketError(f"{tid} is claimed by {c['agent']}, not {agent}")

	def heartbeat(self, tid: str, agent: str, note: str = "") -> dict:
		"""Keep a claim alive and say what you're doing now (shown live on the board)."""
		def beat():
			self._holder(tid, agent)
			claims = self._claims()
			claims[tid]["heartbeat_at"] = _now()
			if note:
				claims[tid]["note"] = note[:300]
			self._save_claims(claims)
			self._log("heartbeat", tid, agent, note)
		self._locked(beat)
		return self.get(tid)

	def release(self, tid: str, agent: str, reason: str = "") -> dict:
		"""Give a claimed ticket back: claim ends, ticket returns to todo."""
		def rel():
			self._holder(tid, agent)
			claims = self._claims()
			claims.pop(tid, None)
			self._save_claims(claims)
			raw = self._read(self._path(tid))
			if raw.get("status") == "doing":
				raw["status"], raw["updated"] = "todo", date.today().isoformat()
			if reason.strip():
				_append_note(raw, agent, f"released: {reason.strip()}", strict=False)
			self._write(raw)
			self._log("release", tid, agent, reason)
		self._locked(rel)
		return self.get(tid)

	def ask(self, tid: str, agent: str, question: str) -> dict:
		"""Hand a decision to a human: the claim ends, waiting_on = question, status todo. A todo with a waiting_on is
		pending (not ready, not claimable) until someone clears it."""
		q = question.strip() if isinstance(question, str) else ""
		if not q:
			raise TicketError("ask needs a question: say what the human must decide")
		if len(q) > QUESTION_MAX:
			raise TicketError(f"question is {len(q)} chars, the limit is {QUESTION_MAX}: shorten it, or put the background in add_note and ask the decision")
		def run():
			self._holder(tid, agent)
			raw = self._read(self._path(tid))
			raw.update(status="todo", waiting_on=q, asked_at=_now(), asked_by=agent, updated=date.today().isoformat())
			_append_note(raw, agent, f"question: {q}")  # refuses before the claim is touched
			claims = self._claims()
			claims.pop(tid, None)
			self._save_claims(claims)
			self._write(raw)
			self._log("question", tid, agent, q)
		self._locked(run)
		return self.get(tid)

	def add_note(self, tid: str, by: str, text: str) -> dict:
		"""Append a signed, timestamped line to the body, under the lock: it can't clobber a concurrent complete or answer
		(update(body=...) replaces the whole body; use this to report progress)."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("add_note needs `by`: who is writing (your worker name, or a person's name)")
		if not (isinstance(text, str) and text.strip()):
			raise TicketError("add_note needs text: what to record")
		def run():
			path = self._path(tid)
			if not path.exists():
				raise NotFound(f"no ticket {tid}")
			raw = self._read(path)
			_append_note(raw, by.strip(), text.strip())
			raw["updated"] = date.today().isoformat()
			self._write(raw)
			self._log("note", tid, by.strip(), text.strip())
		self._locked(run)
		return self.get(tid)

	def answer(self, tid: str, by: str, text: str) -> dict:
		"""A human answers an ask(): appends "Answer to: <question> — <text>" as a note, clears waiting_on (the ticket is ready again), logs `answered`."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("answer needs `by`: who is answering")
		if not (isinstance(text, str) and text.strip()):
			raise TicketError("answer needs text: what you decide")
		def run():
			path = self._path(tid)
			if not path.exists():
				raise NotFound(f"no ticket {tid}")
			raw = self._read(path)
			if not raw.get("waiting_on"):
				raise TicketError(f"{tid} isn't waiting on anyone (no question to answer): use add_note to comment")
			_append_note(raw, by.strip(), f"Answer to: {raw['waiting_on']} — {text.strip()}")
			raw["waiting_on"], raw["asked_at"], raw["asked_by"], raw["updated"] = "", "", "", date.today().isoformat()
			self._write(raw)
			self._log("answered", tid, by, text.strip())
		self._locked(run)
		return self.get(tid)

	def reject(self, tid: str, by: str, reason: str) -> dict:
		"""A human sends a ticket in review back: status todo, "Rejected: <reason>" appended as a note, logs `rejected`."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("reject needs `by`: who is rejecting")
		if not (isinstance(reason, str) and reason.strip()):
			raise TicketError("reject needs a reason: say what is wrong, the worker reads it before redoing the ticket")
		def run():
			path = self._path(tid)
			if not path.exists():
				raise NotFound(f"no ticket {tid}")
			raw = self._read(path)
			if raw.get("status") != "review":
				raise TicketError(f"{tid} is {raw.get('status')}, not review: only a ticket waiting for review can be rejected")
			_append_note(raw, by.strip(), f"Rejected: {reason.strip()}")
			raw["status"], raw["updated"] = "todo", date.today().isoformat()
			raw.pop("result", None)  # rejected work must not show as upstream
			self._write(raw)
			self._log("rejected", tid, by, reason.strip())
		self._locked(run)
		return self.get(tid)

	def complete(self, tid: str, agent: str, summary: str, files: list[str] | None = None,
	             refs: list[str] | None = None, handoff: str | None = None) -> dict:
		"""Finish a claimed ticket: status done (or review when the board's `review` is on), summary appended to the body (dated, signed).
		Also stored as `result` {summary, files, refs, handoff, by, at}: files = paths changed, refs = PR / commit / URL, handoff = what a
		downstream ticket must know. Tickets that wait on this one get it in their context() as `upstream`."""
		if not (isinstance(summary, str) and summary.strip()):
			raise TicketError("complete needs a summary: cause -> fix -> tests, and what was NOT verified")
		for name, v in (("files", files), ("refs", refs)):
			if v is not None and not _short_list(v):
				raise TicketError(f"{name} must be a list of short strings (max 50, each up to 300 chars), e.g. {'[\"unblockd/store.py\"]' if name == 'files' else '[\"https://github.com/o/r/pull/7\"]'}")
		if len(summary.strip()) > SUMMARY_MAX:
			raise TicketError(f"summary is {len(summary.strip())} chars, the limit is {SUMMARY_MAX}: keep cause -> fix -> tests, move detail into an attached file")
		if handoff is not None and not (isinstance(handoff, str) and len(handoff) <= 2000):
			raise TicketError("handoff must be text up to 2000 chars: what the tickets that wait on this one need to know")
		result = {"summary": summary.strip(), "files": [x.strip() for x in files or []], "refs": [x.strip() for x in refs or []],
		          "handoff": (handoff or "").strip(), "by": agent, "at": _now()}
		def done():
			self._holder(tid, agent)
			raw = self._read(self._path(tid))
			status = "review" if self.config["review"] else "done"
			bar = "".join(f"\n- {x.strip()}" for x in raw.get("done_when", []))
			_append(raw, f"{date.today().isoformat()} ({agent}, {status}): {summary.strip()}" + (f"\nDone when:{bar}" if bar else ""))  # refuses before the claim is touched
			claims = self._claims()
			claims.pop(tid, None)
			self._save_claims(claims)
			raw["status"], raw["updated"], raw["result"] = status, date.today().isoformat(), result
			self._write(raw)
			self._log("complete", tid, agent, summary)
		self._locked(done)
		return self.get(tid)

	def activity(self, tid: str | None = None, limit: int = 50, since: str | None = None) -> list[dict]:
		"""Newest first. `since` (ISO timestamp, naive = UTC) keeps only events at or after it."""
		floor = None
		if since:
			try:
				d = datetime.fromisoformat(since.replace("Z", "+00:00"))
			except ValueError:
				raise TicketError(f"since must be an ISO timestamp like 2026-10-08T18:40:00Z, got {since!r}")
			floor = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
		limit = max(0, int(limit))
		out = []
		for name in ("activity.jsonl", "activity.1.jsonl"):  # newest first, so stop as soon as the limit or the floor is reached
			p = self.dir / ".unblockd" / name
			for line in (reversed(p.read_text(encoding="utf-8").splitlines()) if p.exists() else ()):
				if len(out) >= limit:
					return out
				try:
					e = json.loads(line)
				except json.JSONDecodeError:
					continue
				if floor is not None and _ts(e["ts"]) < floor:
					return out
				if tid is None or e.get("ticket") == tid:
					out.append(e)
		return out

	def agents_now(self) -> dict:
		"""The live view: one row per working agent, plus recent activity."""
		live = self._live_claims()
		titles = {t["id"]: t.get("title", "") for t in self._all_raw()}
		working = [{"agent": c["agent"], "profile": c.get("profile"), "ticket": tid, "title": titles.get(tid, ""),
		            "note": c.get("note", ""), "age_s": c["age_s"], "quiet_s": c["quiet_s"]}
		           for tid, c in sorted(live.items(), key=lambda kv: kv[1]["claimed_at"])]
		return {"working": working, "lease_minutes": self.config["lease_minutes"], "recent": self.activity(limit=20)}


def _append(raw: dict, line: str, strict: bool = True) -> bool:
	"""The one way a note enters a body (caller holds the lock and writes). Over BODY_MAX: strict raises (explicit writes),
	otherwise the note is skipped and False comes back (automatic ones: expiry, release reason; the activity log keeps the event)."""
	body = raw.get("body", "")
	new = (body + "\n\n" if body else "") + line
	if len(new) > BODY_MAX:
		if strict:
			raise TicketError(f"{raw['id']}'s body would pass {BODY_MAX} chars: move old notes into an attached file (attach_file) first")
		return False
	raw["body"] = new
	return True


def _append_note(raw: dict, by: str, text: str, strict: bool = True) -> bool:
	"""Add `— by, time: text` to raw's body after a blank line."""
	return _append(raw, f"— {by}, {_now()}: {text}", strict)


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts(iso: str) -> float:
	return datetime.fromisoformat(iso).timestamp()


def _overlap(a: str, b: str) -> bool:
	"""Do two paths/globs touch the same files? (same path, one inside the other, or a glob match)"""
	a, b = a.replace("\\", "/").strip("/"), b.replace("\\", "/").strip("/")
	return a == b or a.startswith(b + "/") or b.startswith(a + "/") or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)
