"""The rules of a board: validation, routing, claims, derived fields. Every front door (web, MCP, CLI, chat) calls Store,
so they can never disagree.

Storage is a connector (files.FileBackend by default: one JSON file per ticket in TICKETS_DIR). Store never touches
storage itself: it calls the connector's methods (see files.FileBackend for the list). Stdlib only.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import connectors
from .errors import Conflict, NotFound, TicketError
from .files import (CONFIG_FILE, ID_RE, _LOG, _RAW, FileBackend, _jline, _stamp,  # noqa: F401 (re-exported)
                    linked_worktree, steal_stale)

STATUSES = ("todo", "doing", "pending", "review", "done")
PRIORITIES = ("P0", "P1", "P2", "P3")
PREFIX_RE = re.compile(r"^[A-Z0-9]+$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,39}$")  # lowercase; "agent:<name>" routes a ticket to that agent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")  # agent profile and board ids
STALE_DAYS = 7  # default; a doing ticket untouched this long is flagged (config: stale_days)
DEFAULT_CONFIG = {
	"title": "Tickets",
	"owners": [{"name": "Me", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
	"default_owner": None,  # None = first agent owner
	"pillar_label": "Pillar",
	"pillars": [],  # [{"key": "ART", "name": "Art", "color": "#8A4FB5", "color_dark": "#B58CE6"}]
	"stale_days": STALE_DAYS,
	"done_window_days": 7,  # the UI's Done column shows tickets done within this many days (the API still returns all)
	"wip_limit": None,  # int: the UI warns when more tickets than this are in progress; None = no warning
	"lease_minutes": 15,  # a claim with no heartbeat for this long expires and the ticket goes back to todo (agents[].lease_minutes overrides per profile)
	"lease_max_minutes": 240,  # heartbeat(until=) may keep a claim alive this far ahead, no further (AI-58): a dead worker still frees its ticket
	"legacy_files": [],  # old ticket lists (relative to the tickets folder): new IDs never reuse a number seen there
	"id": None,  # board id when several boards are served; None = the project folder's name
	# Agent profiles: who should work a ticket. A ticket goes to the first profile sharing one of its
	# tags or its pillar; an explicit "agent:<name>" tag wins. Orchestrators read them via work_plan.
	"agents": [],  # [{"name": "backend", "tags": ["gdscript"], "pillars": ["COMBAT"], "model": "sonnet", "instructions": "...", "docs": [...]}]
	# Knowledge every ticket on this board comes with: repo paths, folders or globs (see knowledge.py).
	"docs": [],  # ["AGENTS.md", "docs/HANDOFF.md"]
	"docs_by_tag": {},  # {"map": ["docs/MAP_BRIEF.md"]}: read only by tickets with that tag (trusted like board docs)
	"architecture": "docs/arch",  # repo-relative folder of component notes (one .md per component, see knowledge.components); on when the folder exists, None = off
	# Capabilities the agent harness provides. unblockd only DECLARES them (never starts a server, never stores a secret): the registry is the allow-list,
	# agents[].skills/mcp, use_skills/use_mcp (every ticket on the board) and a ticket's skills/mcp field select from it by name.
	"skills": {},  # {"pytest-runner": {"path": ".claude/skills/pytest-runner/SKILL.md", "description": "<= 200 chars, one line"}}
	"mcp": {},  # {"postgres": {"hint": "claude mcp add ... (<= 300 chars, NEVER a credential)", "doc": "docs/mcp-postgres.md", "required": true}}
	"use_skills": [],  # registry names every ticket on the board gets
	"use_mcp": [],
	"strict_sessions": False,  # True: a heartbeat / complete / release from ANOTHER session of the claim's worker name is refused, not just warned (CORE-88)
	"review": False,  # True: complete() lands the ticket in status "review" (still open, blocks dependents) until a human sets done
	"context_budget": 60_000,  # characters of docs inlined into a ticket's context; the rest is listed
	# What a new ticket must look like. None = anything goes. Agents are refused (every problem listed), humans get `warnings`.
	"schema": None,  # {"prefixes": {"FOG": "fog + explored map"}, "required": ["done_when"], "tags": ["bug"], "body_template": "## Why\n"}
	# "open" (default): agents create ready tickets; "propose": a ticket created by an agent is a proposal (not claimable) until a human accepts it
	"agent_tickets": "open",
	# The roadmap: versions in order (position = order). Tickets point at one with `milestone`; progress is derived, never stored.
	"milestones": [],
	# MCP tools a worker's server exposes (unblockd.mcp_server --tools worker); None = WORKER_TOOLS. agents[].tools adds, agents[].tools_off removes, per profile
	"worker_tools": None,
	"session_tickets": 5,  # claims one worker session takes before claim_next says stop (stdio MCP enforces it; None = no limit)
	"docs_resend": {"claims": 3, "minutes": 30},  # a board / profile doc already sent is skipped only within this many claims AND minutes  # [{"key": "v0.1", "title": "Foundations", "goal": "one line", "exit_criteria": ["..."], "target": "2026-11-15", "closed": false}]
}
MILESTONE_KEYS = {"key", "title", "goal", "exit_criteria", "target", "closed"}
MILESTONE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
SCHEMA_KEYS = ("prefixes", "required", "tags", "body_template")
SCHEMA_REQUIRED = ("done_when", "priority", "touches", "docs", "pillar", "owner", "parent", "body")
BODY_MAX = 50_000  # chars of body + every note's text
NOTES_MAX = 500  # notes on one ticket
NOTE_KINDS = ("note", "decision", "finding", "warning", "question", "answer", "approved", "rejected", "accepted", "released", "expired", "complete")
NOTE_KINDS_ADD = ("note", "decision", "finding", "warning")  # what add_note takes: decision = a choice + why, finding = a cause / error / fact found, warning = don't do X (it breaks Y)
PINNED_KINDS = ("decision", "finding", "warning", "answer", "approved", "rejected", "accepted")  # always pinned (a human's plain note is pinned too)
NOTES_SHORT = 3000  # chars of note text a claim sends whole; past it: every pinned note + the last NOTES_TAIL others
NOTES_TAIL = 5
QUESTION_MAX = 1000  # chars in an ask() question / waiting_on
INBOX_NOTES, INBOX_NOTE_MAX, INBOX_MAX = 10, 2000, 6000  # inbox: notes shown, chars per note, chars in all (AI-44)
WAIT_MAX, WAIT_POLL_S = 120, 2.0  # claim_next(wait_s=): the longest wait, and how often it looks again (AI-46)
LEASE_WARN_S = 180  # a reply to the holder warns when less lease than this is left
REQUESTS_MAX = 500  # request_ids remembered per board (runtime doc `requests`), oldest dropped first (CORE-74)
REQUEST_RE = re.compile(r"^[\w.:-]{1,100}$")
TRANSITIONS = ("claim", "release", "complete", "question", "expired", "dropped", "rejected", "done", "answered", "deleted", "split")
SUMMARY_MAX = 10_000  # chars in a complete() summary (stored in body and result)
# CORE-68: a ticket done on or after this day (done_at, UTC) with no result summary reads `unreported` (UI: Finished without a summary);
# the day it shipped, so the ~100 older hand-closed tickets never flood the board
UNREPORTED_SINCE = "2026-11-21"
ACTIVITY_MAX = 5_000_000  # bytes; past this the activity log rotates (files: activity.jsonl -> activity.1.jsonl, one old file kept)
PRIO_WEIGHT = {"P0": 8, "P1": 4, "P2": 2, "P3": 1, None: 1}
BRANCH_DAYS = 14  # a linked branch with no commit for this long is still listed, but no longer reads as work in progress

# The derived board per connector (its key), shared by every Store on it (the web server builds one per request). Re-derived
# only when the connector's version (tickets, claims, attachments), the date or the config changed. Never written back.
# The connectors keep their own caches of what they read (files: _RAW, _LOG in files.py).
# In-process queue in front of the board lock (CORE-85): threads of one process (the web server, the shared MCP server) wait here,
# blocked, instead of polling the connector's lock (files: a new try every 50 ms, so 200 threads at once starved some past
# the 10 s wait). Re-entrant per thread. Across processes the connector's lock still decides.
_QUEUE: dict[str, threading.RLock] = {}
_DEC: dict[str, tuple] = {}  # board key -> (signature, {id: decorated ticket}, the state _derive patches the next board from)

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
	# the roadmap version this ticket ships in: a key of the board's `milestones` (separate from parent, so a ticket can be in an epic AND a version)
	"milestone": (None, lambda v: v is None or (isinstance(v, str) and MILESTONE_RE.match(v) is not None)),
	"parent": (None, lambda v: v is None or (isinstance(v, str) and ID_RE.match(v) is not None)),
	# the bar for done, apart from the background in `body`: shown first in context(), echoed into complete()'s body entry
	"done_when": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and 0 < len(x.strip()) <= 200 for x in v)),
	"waiting_on": ("", lambda v: isinstance(v, str) and len(v) <= QUESTION_MAX),
	# when a question was parked and which agent asked it (ask() sets both, answer() clears them); read-only. A hand-set waiting_on gets asked_at only
	"asked_at": ("", lambda v: isinstance(v, str) and len(v) <= 40),
	"asked_by": ("", lambda v: isinstance(v, str) and len(v) <= 40),
	# when the ticket last moved to done (UTC ISO); set by create/update/complete, read-only. Old tickets lack it: readers fall back to `updated`
	# bumped by every write of the ticket file (_write); old files read as 0. Read-only: update(expected_rev=) is how a client says "I saw this one"
	"rev": (0, lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0),
	"done_at": ("", lambda v: isinstance(v, str) and len(v) <= 40),
	# the agent that created this ticket while the board's agent_tickets is "propose"; set by create(agent=), cleared by accept(); read-only. While set the ticket is pending
	"proposed_by": ("", lambda v: isinstance(v, str) and len(v) <= 40),
	"body": ("", lambda v: isinstance(v, str) and len(v) <= BODY_MAX),
	# what happened on the ticket, oldest first: [{by, at, kind, text, q? (answer: the question), status? (complete: done|review)}].
	# Append-only, written only by add_note / ask / answer / approve / reject / accept / release / complete / the expiry sweep; read-only for update
	# tools a worker reported missing (release(missing=...)): [{tool, why, profile, by, at}]. A profile listed here skips the ticket until
	# a human clears it (update needs: []) after giving that profile the tool
	"needs": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(n, dict) and isinstance(n.get("tool"), str) for n in v)),
	"notes": ([], lambda v: isinstance(v, list) and all(isinstance(n, dict) and isinstance(n.get("text"), str) for n in v)),
	# paths/globs the work will edit; claim_next never hands out two live tickets that overlap
	"touches": ([], lambda v: isinstance(v, list) and len(v) <= 30 and all(isinstance(x, str) and 0 < len(x) <= 200 for x in v)),
	# repo paths / folders / globs this ticket's worker should read (knowledge.py); files attached
	# to the ticket live in tickets/files/<ID>/ and show up as the derived `files`
	"docs": ([], lambda v: isinstance(v, list) and len(v) <= 30 and all(isinstance(x, str) and 0 < len(x) <= 200 and ".." not in x for x in v)),
	"tags": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and TAG_RE.match(x) for x in v)),
	# keys of the architecture notes (knowledge.components) this ticket changes: the explicit link, checked against the board's notes
	"components": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and 0 < len(x) <= 100 for x in v)),
	# names from the board's `skills` / `mcp` registry (unblockd.json) this ticket needs, on top of the board's and its profile's
	"skills": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and 0 < len(x) <= 100 for x in v)),
	"mcp": ([], lambda v: isinstance(v, list) and len(v) <= 20 and all(isinstance(x, str) and 0 < len(x) <= 100 for x in v)),
	# what complete() produced, for reviewers and downstream tickets (context().upstream): {summary, files, refs, handoff, by, at}
	"result": (None, lambda v: v is None or (isinstance(v, dict) and set(v) <= RESULT_KEYS and isinstance(v.get("summary"), str)
	                                          and all(_short_list(v.get(k, [])) for k in ("files", "refs")) and isinstance(v.get("handoff", ""), str) and len(v.get("handoff", "")) <= 2000)),
}


RESULT_KEYS = {"summary", "files", "refs", "handoff", "by", "at", "principal", "approved_by", "approved_at", "unread", "followup", "closes", "under", "touched", "outside"}  # closes / under: complete(closes=) (AI-57); touched / outside: complete(touched=) (CORE-47)  # unread = note numbers the worker had not seen when it completed (AI-50); followup = id complete(followup=) created (AI-60)
CLOSES_MAX = 20  # tickets one complete(closes=) may close (AI-57)
FOLLOWUP_KEYS = {"title", "done_when", "blocked_by", "body", "priority", "touches"}  # what complete(followup=) takes; the rest is copied (AI-60)
LIST_IDS_MAX = 200  # list(ids=): a pasted plan or review, not a board dump


def list_ids(ids) -> list[str]:
	"""ids= of list(): a list, or a comma / space separated string; stripped, deduplicated, order kept (API-23)."""
	if isinstance(ids, str):
		ids = ids.replace(",", " ").split()
	if not isinstance(ids, (list, tuple)) or not all(isinstance(i, str) for i in ids):
		raise TicketError("ids must be a list of ticket ids, e.g. [\"AI-56\", \"API-23\"]")
	out = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
	if len(out) > LIST_IDS_MAX:
		raise TicketError(f"ids names {len(out)} tickets, at most {LIST_IDS_MAX} per call")
	return out


def q_hint(q: str, filtered: bool = False) -> str:
	"""What a list with q and no hits says: q needs every word in one ticket; a q made of ticket ids wants ids= (API-23: a lead
	pasted a list of ids into q and found nothing). filtered = other filters were given too, so a ticket may match q but not them."""
	words = (q or "").split()
	looks = [w.strip(",;").upper() for w in words if _ID_TOKEN.fullmatch(w.strip(",;").upper())]
	out = (f"no ticket has every word of q ({len(words)} words): q needs ALL its words in one ticket. Search fewer words"
	       if len(words) > 1 else "no ticket contains q")
	if filtered:
		out += " (with your other filters: try q alone)"
	if len(looks) > 1 or (looks and len(looks) == len(words)):
		out += f"; for a list of tickets pass ids={looks[:10]}"
	return out


# a release reason that names a person or a decision: ask_human is what it needed (AI-56)
_HUMAN_WORDS = re.compile(r"\b(human|person|decid\w*|decision|approv\w*|sign[- ]?off|choose|choice|prefer\w*|pick between)\b", re.I)  # not owner / confirm / answer / permission: ordinary error text has them ("permission denied")
_ID_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Z][A-Z0-9]*-\d+(?![A-Za-z0-9_-])")  # a ticket id in free text (release reason)
# a list row (list_tickets, REST GET tickets?fields=, CLI list): these keys, plus what `fields` asks for (AI-47)
SUMMARY_KEYS = ("id", "title", "effective_status", "priority", "owner", "pillar", "milestone", "tags", "agent", "open_blockers")
# what `fields` may add to a row: every stored field and the derived ones. body and notes are the big ones: a row has them only when
# they are named, so a planner gets touches / done_when / parent of the whole board for a fraction of full=true's tokens
LIST_FIELDS = tuple(FIELDS) + ("created", "updated", "children", "progress", "blocks", "unblocks", "missing_blockers", "score",
                               "stale_days", "claim", "files", "branches", "holder", "idle_days")  # holder = the live claim's agent (or null); idle_days: list(stale_days=) rows (AI-57)


def list_fields(fields, matched: bool = False, idle: bool = False) -> list[str]:
	"""`fields` as a list (a comma string is split: REST / CLI), each one checked against LIST_FIELDS. matched = the list was filtered
	by touches / component: rows then also carry touches and holder, what "who works on this file?" asks next (CORE-52).
	idle = filtered by stale_days: rows carry idle_days (AI-57)."""
	if fields is None:
		fields = []
	if isinstance(fields, str):
		fields = fields.split(",")
	if not isinstance(fields, (list, tuple)) or not all(isinstance(f, str) for f in fields):
		raise TicketError("fields must be a list of field names, e.g. [\"touches\", \"done_when\"]")
	out = [f.strip() for f in fields if f.strip()]
	bad = [f for f in out if f not in LIST_FIELDS]
	if bad:
		raise TicketError(f"unknown field(s) {', '.join(bad)} in fields: one of {', '.join(LIST_FIELDS)}")
	return list(dict.fromkeys(out + (["touches", "holder"] if matched else []) + (["idle_days"] if idle else [])))


def summarize(t: dict, fields=()) -> dict:
	"""A list row: SUMMARY_KEYS plus the asked `fields` (already checked by list_fields)."""
	return {k: (t.get("claim") or {}).get("agent") if k == "holder" else t.get(k) for k in (*SUMMARY_KEYS, *fields)}

# The limits of the FIELDS validators above, in numbers: what an error names and what board_info returns as `limits`, so an agent can
# check before it writes. Keep them equal to the validators (tests/test_tool_docs.py checks each one at its edge).
LIMITS = {"title": {"chars": 200}, "section": {"chars": 80}, "owner": {"chars": 40}, "pillar": {"chars": 40},
          "waiting_on": {"chars": QUESTION_MAX}, "body": {"chars": BODY_MAX, "with_notes": True},
          "done_when": {"items": 20, "chars": 200}, "touches": {"items": 30, "chars": 200}, "docs": {"items": 30, "chars": 200},
          "tags": {"items": 20, "chars": 40}, "components": {"items": 20, "chars": 100}, "skills": {"items": 20, "chars": 100},
          "mcp": {"items": 20, "chars": 100}, "blocked_by": {"items": None}, "needs": {"items": 20}}


def _field_problem(k: str, v) -> str:
	"""Why FIELDS[k] refused v, as `invalid <field>[<i>]: <rule>`, without echoing v (a whole echoed input buried the reason)."""
	kind, lim = type(v).__name__, LIMITS.get(k, {})
	if k == "status":
		return f"invalid status: one of {', '.join(x for x in STATUSES if x != 'pending')}"
	if k == "priority":
		return "invalid priority: one of P0, P1, P2, P3 or null"
	if k == "pillar_step":
		return "invalid pillar_step: a whole number > 0, or null"
	if k == "parent":
		return "invalid parent: a ticket id like BUG-12, or null"
	if k == "milestone":
		return "invalid milestone: a key of the board's milestones (letters, digits . _ -, max 40), or null"
	if "items" in lim:
		if not isinstance(v, list):
			return f"invalid {k}: a list" + (" of strings, e.g. [\"...\"]" if isinstance(v, str) else f" (got {kind})")
		if lim["items"] is not None and len(v) > lim["items"]:
			return f"invalid {k}: {len(v)} items, max {lim['items']}"
		out = []
		for i, x in enumerate(v):
			if k == "needs":
				if not (isinstance(x, dict) and isinstance(x.get("tool"), str)):
					out.append(f"invalid needs[{i}]: an object with a `tool` name")
			elif not isinstance(x, str):
				out.append(f"invalid {k}[{i}]: text (got {type(x).__name__})")
			elif not x.strip():
				out.append(f"invalid {k}[{i}]: empty")
			elif lim.get("chars") and len(x) > lim["chars"]:
				out.append(f"invalid {k}[{i}]: {len(x)} chars, max {lim['chars']}")
			elif k == "blocked_by" and not ID_RE.match(x):
				out.append(f"invalid blocked_by[{i}]: a ticket id like BUG-12")
			elif k == "tags" and not TAG_RE.match(x):
				out.append(f"invalid tags[{i}]: lowercase a-z 0-9 : . _ -, starting with a letter or digit")
			elif k == "docs" and ".." in x:
				out.append(f"invalid docs[{i}]: a repo path without ..")
		return "; ".join(out) or f"invalid {k}"
	if "chars" in lim:
		if not isinstance(v, str):
			return f"invalid {k}: text (got {kind})"
		if k == "title" and not v.strip():
			return "invalid title: empty"
		return f"invalid {k}: {len(v)} chars, max {lim['chars']}" + (" (with every note's text)" if lim.get("with_notes") else "")
	return f"invalid {k} ({kind})"


OUTCOMES = {"expired": "expired", "release": "released", "rejected": "rejected"}  # activity events that end an attempt badly


def _short_list(v) -> bool:
	return isinstance(v, list) and len(v) <= 50 and all(isinstance(x, str) and 0 < len(x.strip()) <= 300 for x in v)


def refuse_open_bind(host: str, token: str | None) -> None:
	"""Exit at startup when the server listens beyond loopback with no token: anyone (or any web page, via a rebound name)
	that reaches the port could edit tickets. UNBLOCKD_ALLOW_OPEN=1 opts out (the compose file does: it publishes loopback only)."""
	if token or host in ("127.0.0.1", "localhost", "::1") or os.environ.get("UNBLOCKD_ALLOW_OPEN") == "1":
		return
	raise SystemExit(f"refusing to start: bound to {host} with no auth. Set UNBLOCKD_TOKEN, or bind HOST=127.0.0.1, "
	                 "or set UNBLOCKD_ALLOW_OPEN=1 if the port is only reachable by you (e.g. published on 127.0.0.1 by Docker).")


AGENT_KEYS = {"name", "tags", "pillars", "model", "instructions", "docs", "skills", "mcp", "tools", "tools_off", "lease_minutes"}
# what a `--tools worker` server keeps: every tool WORK_LOOP.md tells a worker to call (tests/test_tools.py checks it, AI-41)
WORKER_TOOLS = ("claim_next", "claim_ticket", "heartbeat", "add_note", "complete", "release", "ask_human", "read_doc", "get_ticket",
                "create_ticket", "create_tickets", "split_ticket", "update_ticket", "arch_set_edge", "arch_remove_edge", "agents_now")
HUMAN_TOOLS = ("answer", "reject", "accept_ticket", "approve_ticket")  # WORK_LOOP names them only to say a worker can't use them
TOOL_RE = re.compile(r"^[a-z][a-z_]{0,39}$")
# every tool the MCP server has (test_mcp checks this matches it): what the Agents panel offers and set_tools accepts
MCP_TOOLS = ("accept_ticket", "add_note", "agents_now", "answer", "approve_ticket", "arch_add_component", "arch_delete_component",
             "arch_remove_edge", "arch_set_edge", "arch_update_component", "architecture_map", "ask_human", "attach_file", "board_info",
             "claim_next", "claim_ticket", "complete", "create_board", "create_ticket", "create_tickets", "delete_ticket", "detach_file", "get_context",
             "get_ticket", "heartbeat", "knowledge_graph", "list_boards", "list_tickets", "needs_you", "next_ready", "read_doc", "reject", "release",
             "report_ticket", "roadmap", "split_ticket", "update_ticket", "update_tickets", "work_plan")
BATCH_MAX = 50  # tickets per create_many
UPDATE_MAX = 200  # tickets per update_many (the UI's bulk bar sends its whole selection in one request)
NEEDS_BODY = 6000  # Store.needs: chars of body (the description) sent per item
NEEDS_NOTES = 10  # Store.needs: the newest notes sent per item (the panel shows the last 3 that are not the question itself)
SKILL_KEYS, MCP_KEYS = {"path", "description"}, {"hint", "doc", "required"}


def load_config(tickets_dir: Path) -> dict:
	"""DEFAULT_CONFIG overlaid with <tickets_dir>/unblockd.json when it exists."""
	return config_from(*FileBackend(tickets_dir).read_config(), Path(tickets_dir))


def config_from(user: dict | None, where: str, tickets_dir: Path | None) -> dict:
	"""DEFAULT_CONFIG overlaid with a board's own settings (`user`, None = none), validated. `where` names them in errors;
	tickets_dir = a files board's folder (skill / mcp doc paths are checked inside its project), None for other connectors."""
	cfg = json.loads(json.dumps(DEFAULT_CONFIG))
	if user is not None:
		if not isinstance(user, dict):
			raise TicketError(f"{where} must be a JSON object")
		unknown = set(user) - set(DEFAULT_CONFIG)
		if unknown:
			raise TicketError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}. Known: {', '.join(DEFAULT_CONFIG)}")
		cfg.update(user)
	if not isinstance(cfg["strict_sessions"], bool):
		raise TicketError(f"{CONFIG_FILE}: strict_sessions must be true or false")
	if not isinstance(cfg["review"], bool):
		raise TicketError(f"{CONFIG_FILE}: review must be true or false")
	if cfg["architecture"] is not None and not isinstance(cfg["architecture"], str):
		raise TicketError(f"{CONFIG_FILE}: architecture must be a repo folder like \"docs/arch\" or null")
	if cfg["wip_limit"] is not None and not (type(cfg["wip_limit"]) is int and cfg["wip_limit"] > 0):
		raise TicketError(f"{CONFIG_FILE}: wip_limit must be a positive number or null")
	if not (type(cfg["done_window_days"]) is int and cfg["done_window_days"] > 0):
		raise TicketError(f"{CONFIG_FILE}: done_window_days must be a positive number of days")
	if not cfg["owners"] or not all(isinstance(o, dict) and o.get("name") for o in cfg["owners"]):
		raise TicketError(f"{CONFIG_FILE}: owners must be a non-empty list of {{\"name\", \"kind\"}}")
	for pl in cfg["pillars"]:
		if not (isinstance(pl, dict) and PREFIX_RE.match(str(pl.get("key", "")))):
			raise TicketError(f"{CONFIG_FILE}: each pillar needs a key like ART (A-Z, 0-9)")
	for ag in cfg["agents"]:
		if not (isinstance(ag, dict) and NAME_RE.match(str(ag.get("name", "")))):
			raise TicketError(f"{CONFIG_FILE}: each agent needs a name like backend (a-z, 0-9, - _)")
		if not all(isinstance(x, str) for x in ag.get("tags", []) + ag.get("pillars", []) + ag.get("docs", []) + ag.get("skills", []) + ag.get("mcp", [])):
			raise TicketError(f"{CONFIG_FILE}: agent {ag['name']}: tags, pillars, docs, skills and mcp are lists of strings")
		if set(ag) - AGENT_KEYS:
			raise TicketError(f"{CONFIG_FILE}: agent {ag['name']}: unknown key(s) {', '.join(sorted(set(ag) - AGENT_KEYS))}. Allowed: {', '.join(sorted(AGENT_KEYS))}")
		if "lease_minutes" in ag and not _minutes(ag["lease_minutes"]):
			raise TicketError(f"{CONFIG_FILE}: agent {ag['name']}: lease_minutes must be a positive number of minutes (e.g. 30), at most 1440")
	for k in ("lease_minutes", "lease_max_minutes"):
		if not _minutes(cfg[k]):
			raise TicketError(f"{CONFIG_FILE}: {k} must be a positive number of minutes, at most 1440")
	for o in cfg["owners"]:
		if o.get("kind") not in ("human", "agent"):
			raise TicketError(f"{CONFIG_FILE}: owner {o['name']}: kind must be \"human\" or \"agent\" (got {o.get('kind')!r})")
	if not (isinstance(cfg["docs"], list) and all(isinstance(x, str) for x in cfg["docs"])):
		raise TicketError(f"{CONFIG_FILE}: docs is a list of repo paths")
	dbt = cfg["docs_by_tag"]
	if not isinstance(dbt, dict):
		raise TicketError(f"{CONFIG_FILE}: docs_by_tag is an object of tag -> list of repo paths, e.g. {{\"map\": [\"docs/MAP_BRIEF.md\"]}}")
	for tag, paths in dbt.items():
		if not TAG_RE.match(tag):
			raise TicketError(f"{CONFIG_FILE}: docs_by_tag key {tag!r} is not a tag (lowercase a-z 0-9 : . _ -, max 40)")
		if not (isinstance(paths, list) and paths and all(isinstance(x, str) and 0 < len(x) <= 200 and ".." not in x for x in paths)):
			raise TicketError(f"{CONFIG_FILE}: docs_by_tag {tag}: a non-empty list of repo paths (no ..)")
	if cfg["agent_tickets"] not in ("propose", "open"):
		raise TicketError(f"{CONFIG_FILE}: agent_tickets must be \"propose\" or \"open\"")
	_check_schema(cfg["schema"])
	_check_milestones(cfg["milestones"])
	if cfg["session_tickets"] is not None and not (type(cfg["session_tickets"]) is int and cfg["session_tickets"] > 0):
		raise TicketError(f"{CONFIG_FILE}: session_tickets must be a positive number or null")
	dr = cfg["docs_resend"]
	if not (isinstance(dr, dict) and set(dr) <= {"claims", "minutes"} and all(type(dr.get(k, 1)) is int and dr.get(k, 1) >= 0 for k in ("claims", "minutes"))):
		raise TicketError(f"{CONFIG_FILE}: docs_resend must be like {{\"claims\": 3, \"minutes\": 30}} (0 = always resend)")
	for what, names in [("worker_tools", cfg["worker_tools"])] + [(f"agent {a['name']} {k}", a.get(k)) for a in cfg["agents"] for k in ("tools", "tools_off")]:
		if names is not None and not (isinstance(names, list) and all(isinstance(x, str) and TOOL_RE.match(x) for x in names)):
			raise TicketError(f"{CONFIG_FILE}: {what} must be a list of MCP tool names like claim_next")
	_check_caps(cfg, tickets_dir)
	if not cfg["default_owner"]:
		agents = [o["name"] for o in cfg["owners"] if o.get("kind") == "agent"]
		cfg["default_owner"] = (agents or [cfg["owners"][0]["name"]])[0]
	return cfg


def _minutes(v) -> bool:
	return type(v) in (int, float) and 0 < v <= 1440


def _check_caps(cfg: dict, tickets_dir: Path | None) -> None:
	"""The skills / mcp registries and every list that selects from them (use_*, agents[].skills / mcp)."""
	from .knowledge import _inside, _is_text  # lazy: knowledge imports this module
	bad = lambda m: TicketError(f"{CONFIG_FILE}: {m}")

	def path(what: str, rel) -> None:  # inside the project, an existing text file, NOT under the tickets folder (tickets/files/<ID>/ is writable by anyone with ticket access)
		if tickets_dir is None:
			raise bad(f"{what}: {rel!r}: this board has no project folder to read files from")
		base, tdir = Path(tickets_dir).resolve().parent, Path(tickets_dir).resolve()
		try:
			p = _inside(base, rel)
		except TicketError as e:
			raise bad(f"{what}: {e}")
		if p == tdir or tdir in p.parents:
			raise bad(f"{what}: {rel!r} is inside the tickets folder, which tickets can write to; keep it in the repo, e.g. .claude/skills/")
		if not (p.is_file() and _is_text(p)):
			raise bad(f"{what}: {rel!r} is not a text file in the project")

	for kind, keys in (("skills", SKILL_KEYS), ("mcp", MCP_KEYS)):
		if not (isinstance(cfg[kind], dict) and all(NAME_RE.match(str(n)) and isinstance(e, dict) for n, e in cfg[kind].items())):
			raise bad(f"{kind} must map a name like pytest-runner (a-z, 0-9, - _) to an object")
		for n, e in cfg[kind].items():
			if set(e) - keys:
				raise bad(f"{kind} {n}: unknown key(s) {', '.join(sorted(set(e) - keys))}. Allowed: {', '.join(sorted(keys))} (unblockd declares, the agent harness loads: no command, url or env)")
	for n, e in cfg["skills"].items():
		path(f"skills {n}", e.get("path"))
		d = e.get("description", "")
		if not (isinstance(d, str) and len(d) <= 200 and len(d.splitlines()) <= 1):
			raise bad(f"skills {n}: description must be one line of up to 200 characters")
	for n, e in cfg["mcp"].items():
		h = e.get("hint")
		if not (isinstance(h, str) and h.strip() and len(h) <= 300 and len(h.splitlines()) <= 1):
			raise bad(f"mcp {n}: hint is required: one line of up to 300 characters saying how to enable it (never a credential: every agent reads this config)")
		if "doc" in e:
			path(f"mcp {n} doc", e["doc"])
		if not isinstance(e.get("required", False), bool):
			raise bad(f"mcp {n}: required must be true or false")
	picks = [("use_skills", cfg["use_skills"], "skills"), ("use_mcp", cfg["use_mcp"], "mcp")]
	picks += [(f"agent {a['name']} {k}", a.get(k, []), k) for a in cfg["agents"] for k in ("skills", "mcp")]
	for what, names, kind in picks:
		if not (isinstance(names, list) and all(isinstance(x, str) for x in names)):
			raise bad(f"{what} must be a list of {kind} names")
		if (miss := [x for x in names if x not in cfg[kind]]):
			raise bad(f"{what}: unknown {kind} {', '.join(miss)}. Registered: {', '.join(cfg[kind]) or 'none'} (the \"{kind}\" registry in {CONFIG_FILE})")


def _check_milestones(ms) -> None:
	bad = lambda m: TicketError(f"{CONFIG_FILE}: milestones {m}")
	if not isinstance(ms, list):
		raise bad('must be a list like [{"key": "v0.1", "title": "Foundations"}], in roadmap order')
	seen = set()
	for m in ms:
		if not (isinstance(m, dict) and isinstance(m.get("key"), str) and MILESTONE_RE.match(m["key"])):
			raise bad('need a key like v0.1 (letters, digits, . _ -, up to 40)')
		k = m["key"]
		if set(m) - MILESTONE_KEYS:
			raise bad(f"{k}: unknown key(s) {', '.join(sorted(set(m) - MILESTONE_KEYS))}. Allowed: {', '.join(sorted(MILESTONE_KEYS))}")
		if k in seen:
			raise bad(f"{k} is listed twice")
		seen.add(k)
		if not (isinstance(m.get("title"), str) and 0 < len(m["title"].strip()) <= 80):
			raise bad(f"{k}: title is required (up to 80 characters)")
		if not (isinstance(m.get("goal", ""), str) and len(m.get("goal", "")) <= 300 and len(m.get("goal", "").splitlines()) <= 1):
			raise bad(f"{k}: goal is one line of up to 300 characters")
		ec = m.get("exit_criteria", [])
		if not (isinstance(ec, list) and len(ec) <= 20 and all(isinstance(x, str) and 0 < len(x.strip()) <= 200 for x in ec)):
			raise bad(f"{k}: exit_criteria is a list of up to 20 short lines")
		if "target" in m:
			try:
				date.fromisoformat(str(m["target"]))
			except ValueError:
				raise bad(f"{k}: target is a date like 2026-11-15")
		if not isinstance(m.get("closed", False), bool):
			raise bad(f"{k}: closed is true or false")


def _check_schema(sc) -> None:
	if sc is None:
		return
	bad = lambda m: TicketError(f"{CONFIG_FILE}: schema {m}")
	if not isinstance(sc, dict) or set(sc) - set(SCHEMA_KEYS):
		raise bad(f"must be an object with only these keys: {', '.join(SCHEMA_KEYS)}")
	pf = sc.get("prefixes", {})
	if not (isinstance(pf, dict) and all(PREFIX_RE.match(str(k)) and isinstance(v, str) and len(v) <= 80 for k, v in pf.items())):
		raise bad("prefixes must map a prefix like FOG (A-Z, 0-9) to a meaning of up to 80 characters")
	if not (isinstance(sc.get("required", []), list) and set(sc.get("required", [])) <= set(SCHEMA_REQUIRED)):
		raise bad(f"required must be a list of: {', '.join(SCHEMA_REQUIRED)}")
	if not (isinstance(sc.get("tags", []), list) and all(isinstance(x, str) and TAG_RE.match(x) for x in sc.get("tags", []))):
		raise bad("tags must be a list of lowercase tags like bug")
	if not isinstance(sc.get("body_template", ""), str):
		raise bad("body_template must be text")


class Store:
	def __init__(self, tickets_dir: str | os.PathLike | None = None, legacy_files: list[str] | None = None, backend=None):
		"""A board on a tickets folder (the files connector), a database location (`sqlite:///x.db#board`, see connectors), or
		on `backend`, any connector (files.FileBackend lists the methods)."""
		if backend is None and connectors.is_db(tickets_dir):
			backend = connectors.open_backend(tickets_dir)  # a database board's row is created on first use
		self.backend = backend if backend is not None else FileBackend(tickets_dir)
		self.backend.parse = _parse  # upgrading what old tickets hold is a rule: the same for every connector
		files = isinstance(self.backend, FileBackend)
		self.dir = self.backend.dir if files else None  # the tickets folder and its runtime folder (.unblockd): files boards only
		self.rt = self.backend.rt if files else None
		self.config = config_from(*self.backend.read_config(), self.dir)
		self.stale_days = int(self.config["stale_days"])
		# Old ticket lists: new IDs never reuse a number seen there.
		self.legacy_files = [Path(p) for p in (legacy_files or []) if p] + ([self.dir / p for p in self.config["legacy_files"]] if files else [])
		self._key = self.backend.key
		self._cfg_sig = json.dumps(self.config, sort_keys=True)

	# ---------- storage (always through the connector) ----------
	@staticmethod
	def _valid(tid: str) -> str:
		if not isinstance(tid, str) or not ID_RE.match(tid):
			raise TicketError(f"bad ticket id {tid!r}: expected like BUG-10 or ART-23")
		return tid

	def _exists(self, tid: str) -> bool:
		return self.backend.exists(self._valid(tid))

	def _fresh(self, tid: str) -> dict:
		"""One ticket as stored, read now (not from a cache): what every read-modify-write under the lock starts from."""
		t = self.backend.read(self._valid(tid))
		if t is None:
			raise NotFound(f"no ticket {tid}")
		return t

	def _path(self, tid: str) -> Path:  # files boards: the ticket's file
		return self.backend.path(self._valid(tid))

	def _read(self, path: Path) -> dict:  # files boards: one ticket file, parsed
		return self.backend.read_file(path)

	def _write(self, t: dict) -> None:
		t["rev"] = t.get("rev", 0) + 1  # every ticket write goes through here (claims and heartbeats never touch the ticket)
		self.backend.write(t)

	def _all_raw(self) -> list[dict]:
		return [_copy(t) for t in self.backend.tickets().values()]

	def _raw(self) -> dict[str, dict]:
		"""{id: ticket} as stored, NOT copied (the connector's cache): for checks that only read. CORE-85: _all_raw() deep-copied all
		20,000 tickets for each check, 0.3 s per create."""
		return self.backend.tickets()

	def _board(self, raw: dict | None = None) -> dict[str, dict]:
		"""Every ticket, decorated, by id (in id order). Re-derived only when a ticket, the claims, an attachment, the config or the date changed,
		and then only for what changed (CORE-85, _derive). raw = what the caller's own backend.tickets() call just returned (claim: it keeps
		that read to patch its write in)."""
		self._live_claims()  # expire stale claims first, so their tickets are read back as todo
		raw = self.backend.tickets() if raw is None else raw
		base = self._base()
		sig = (self.backend.version(), *base)
		hit = _DEC.get(self._key)
		if hit and hit[0] == sig:
			return hit[1]
		dec, state = self._derive(raw, base, hit[2] if hit else None)
		_DEC[self._key] = (sig, dec, state)
		return dec

	def _base(self) -> tuple:
		"""What every decorated ticket depends on besides the tickets, claims and attachments: the date, the config, a files board's branch stamps."""
		return (date.today(), self._cfg_sig, *((_stamp(self.rt / "branches.json"), _stamp(self.rt / "branches_ignored.json")) if self.rt else ()))

	# ---------- validation ----------
	def _clean(self, data: dict, partial: bool) -> dict:
		if not isinstance(data, dict):
			raise TicketError("ticket data must be an object")
		unknown = set(data) - set(FIELDS) - {"id", "created", "updated"}
		if unknown:
			raise TicketError(f"unknown field(s): {', '.join(sorted(unknown))}. Known: {', '.join(FIELDS)}")
		out, problems = {}, []
		for k, v in data.items():
			if k in ("id", "created", "updated"):
				continue
			if k == "result":
				raise TicketError("result is set only by complete(): it can be read, not written")
			if k == "notes":
				raise TicketError("notes can't be written: add one with add_note (they are append-only)")
			if k == "proposed_by":
				raise TicketError("proposed_by is set when an agent creates a ticket and cleared by accept(): it can be read, not written")
			if k in ("asked_at", "asked_by"):
				raise TicketError(f"{k} is set by ask() and cleared by answer(): it can be read, not written")
			if k == "rev":
				raise TicketError("rev is bumped by every write: it can be read, not written (send If-Match / expected_rev to guard an update)")
			if k == "done_at":
				raise TicketError("done_at is set when a ticket moves to done: it can be read, not written")
			if k == "title" and isinstance(v, str):
				v = v.strip()
			if k == "tags" and isinstance(v, list):
				v = list(dict.fromkeys(x.strip().lower() for x in v if isinstance(x, str) and x.strip()))
			if k in ("components", "skills", "mcp") and isinstance(v, list):
				v = list(dict.fromkeys(x.strip() if isinstance(x, str) else x for x in v))
			if k == "status" and v == "pending":
				raise TicketError("status can't be set to pending: it is derived. A todo with an open blocker (blocked_by) or a waiting_on shows as pending; "
				                  "agents use ask_human(id, agent, question), humans set waiting_on or blocked_by")
			if not FIELDS[k][1](v):  # every problem is collected, so one call reports them all
				problems.append(_field_problem(k, v))
				continue
			if k == "tags" and self.config["agents"]:
				names = [a["name"] for a in self.config["agents"]]
				bad = [x for x in v if x.startswith("agent:") and x[6:] not in names]
				if bad:
					problems.append(f"unknown agent in {', '.join(bad)}: agents are {', '.join(names)} (agents in {CONFIG_FILE})")
			if k == "owner" and v not in (names := [o["name"] for o in self.config["owners"]]):
				problems.append(f"invalid owner: one of {', '.join(names)} (owners in {CONFIG_FILE})")
			if k == "pillar" and v is not None and self.config["pillars"] and v not in (keys := [p["key"] for p in self.config["pillars"]]):
				problems.append(f"invalid pillar: one of {', '.join(keys)} or null (pillars in {CONFIG_FILE})")
			out[k] = v
		if not partial and "title" not in out and "title" not in data:
			problems.append("title is required")
		if problems:
			raise TicketError(problems[0] if len(problems) == 1 else f"{len(problems)} problems: " + "; ".join(problems) + " (limits: board_info)")
		return out

	def _check_loop(self, tid: str, blocked_by: list[str], extra: dict = {}) -> None:
		"""Refuse blocked_by that makes a ticket wait on itself, directly or down a chain. extra = more edges (a create_many batch)."""
		if tid in blocked_by:
			raise TicketError(f"{tid} can't be blocked by itself")
		raw = self._raw()

		def edges(x):
			return blocked_by if x == tid else extra[x] if x in extra else raw[x].get("blocked_by", []) if x in raw else []
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
			stack += [(b, x) for b in edges(x)]

	def _check_blockers(self, new: list[str], old: list[str]) -> None:
		"""Refuse blocked_by ids that don't exist, unless already on the ticket (a blocker deleted later keeps it editable)."""
		have = self._raw()
		bad = [b for b in new if b not in have and b not in old]
		if bad:
			raise TicketError("; ".join(f"no ticket {b} on this board" for b in bad) + ": fix the id or create it first")

	def _check_components(self, new: list[str], old: list[str]) -> None:
		"""Refuse component keys that are not notes of the board's architecture, unless already on the ticket (a note renamed later keeps it editable)."""
		bad = [k for k in new if k not in old]
		if not bad:
			return
		from .knowledge import components  # lazy: knowledge imports this module
		keys = [c["key"] for c in components(self)["components"]]
		bad = [k for k in bad if k not in keys]
		if bad and not keys:
			if self.dir is None:
				raise TicketError(f"components {', '.join(bad)}: this board has no architecture notes (it has no repo linked), so leave components empty")
			if not self.config.get("architecture"):
				raise TicketError(f"components {', '.join(bad)}: this board has no architecture. Set \"architecture\" in {CONFIG_FILE} to a repo folder "
				                  "with one .md note per component (docs/reference.md, Architecture), or leave components empty")
			raise TicketError(f"components {', '.join(bad)}: this board has no components yet ({self.config['architecture']} has no notes). "
			                  "Add one on the Map (+ Component) or with arch_add_component, or leave components empty")
		if bad:
			raise TicketError(f"unknown component(s) {', '.join(bad)}: this board's components are {', '.join(keys)} (the file names in its architecture folder)")

	def _check_milestone(self, new: str | None, old: str | None) -> None:
		"""Refuse a milestone key the board doesn't list, unless it is already on the ticket (a milestone removed later keeps it editable)."""
		keys = [m["key"] for m in self.config["milestones"]]
		if new is None or new == old or new in keys:
			return
		if not keys:
			raise TicketError(f"milestone {new}: this board has no milestones. Add them to \"milestones\" in {CONFIG_FILE} "
			                  "(in roadmap order: [{\"key\": \"v0.1\", \"title\": \"...\"}]), or leave milestone empty")
		raise TicketError(f"unknown milestone {new}: this board's milestones are {', '.join(keys)} (milestones in {CONFIG_FILE})")

	def rules(self) -> dict:
		"""The board's ticket rules a worker needs before it writes: schema, agent_tickets, review, the human owners and the
		field / note limits. board_info returns the same, and claim context carries it (`context.rules`), so a worker needs no extra call."""
		c = self.config
		return {"schema": c.get("schema"), "agent_tickets": c.get("agent_tickets", "open"), "review": bool(c.get("review")),
		        "humans": [o["name"] for o in c["owners"] if o.get("kind") == "human"],
		        "limits": {"fields": LIMITS, "note_kinds": list(NOTE_KINDS_ADD), "question_chars": QUESTION_MAX,
		                   "summary_chars": SUMMARY_MAX, "handoff_chars": 2000, "notes_per_ticket": NOTES_MAX}}

	def worker_tools(self, profile: str | None = None) -> list[str]:
		"""The MCP tools a worker of `profile` gets: the board's base set (`worker_tools`, default WORKER_TOOLS), minus the profile's
		`tools_off`, plus its `tools`. Order: base order, then the additions."""
		base = list(self.config.get("worker_tools") or WORKER_TOOLS)
		ag = next((a for a in self.config["agents"] if a["name"] == profile), {})
		off = set(ag.get("tools_off") or [])
		return [t for t in base if t not in off] + [t for t in ag.get("tools") or [] if t not in base]

	def tools_view(self) -> dict:
		"""What the Agents panel shows: every tool, the default and current base set, and per profile its additions, removals and result."""
		base = list(self.config.get("worker_tools") or WORKER_TOOLS)
		return {"all": list(MCP_TOOLS), "default": list(WORKER_TOOLS), "base": base,
		        "profiles": [{"name": a["name"], "tools": a.get("tools") or [], "tools_off": a.get("tools_off") or [],
		                      "effective": self.worker_tools(a["name"])} for a in self.config["agents"]]}

	def set_tools(self, base: list[str], profiles: dict, by: str = "human") -> dict:
		"""Save the worker tool sets to unblockd.json: `base` = every worker's set, `profiles` = {name: {tools: [...], tools_off: [...]}}
		(only the differences from base are stored). Only these keys are rewritten; the rest of the file stays as it is (re-indented)."""
		def check(names, what):
			if not (isinstance(names, list) and all(isinstance(x, str) for x in names)):
				raise TicketError(f"{what} must be a list of tool names")
			bad = [x for x in names if x not in MCP_TOOLS]
			if bad:
				raise TicketError(f"{what}: unknown tool(s) {', '.join(bad)}. Tools: {', '.join(MCP_TOOLS)}")
			return list(dict.fromkeys(names))
		base = check(base, "base")
		if not base:
			raise TicketError("every worker needs at least one tool (claim_next, to take work)")
		if not isinstance(profiles, dict):
			raise TicketError("profiles must map a profile name to {tools, tools_off}")
		names = [a["name"] for a in self.config["agents"]]
		unknown = [p for p in profiles if p not in names]
		if unknown:
			raise TicketError(f"unknown profile(s) {', '.join(unknown)}: profiles are {', '.join(names) or 'none'}")
		def run():
			raw = self.backend.read_config()[0] or {}
			if base == list(WORKER_TOOLS):
				raw.pop("worker_tools", None)
			else:
				raw["worker_tools"] = base
			for ag in raw.get("agents", []):
				if ag.get("name") not in profiles:
					continue
				ch = profiles[ag["name"]] or {}
				add = [t for t in check(ch.get("tools", []), f"{ag['name']} tools") if t not in base]
				off = [t for t in check(ch.get("tools_off", []), f"{ag['name']} tools_off") if t in base]
				for k, v in (("tools", add), ("tools_off", off)):
					if v:
						ag[k] = v
					else:
						ag.pop(k, None)
			self.backend.write_config(raw)
			self.config = config_from(*self.backend.read_config(), self.dir)
			self._cfg_sig = json.dumps(self.config, sort_keys=True)
			self._log("config", None, by, "worker tools")
		self._locked(run)
		return self.tools_view()

	def next_milestone(self) -> str | None:
		"""The current roadmap step: the first milestone not closed."""
		return next((m["key"] for m in self.config["milestones"] if not m.get("closed")), None)

	def roadmap(self) -> dict:
		"""The milestones in order with derived progress: {milestones: [{key, title, goal, exit_criteria, target, closed, next, all_done,
		counts}], next, unscheduled: counts of tickets with no milestone (or one no longer listed)}. counts = total, done, review, doing,
		ready, waiting_on_human (a question or a proposal), blocked: each ticket in exactly one of the last six."""
		ts, nxt = self.list(), self.next_milestone()
		def counts(group: list[dict]) -> dict:
			c = dict.fromkeys(("total", "done", "review", "doing", "ready", "waiting_on_human", "blocked"), 0)
			for t in group:
				c["total"] += 1
				st = t["effective_status"]
				c["done" if st == "done" else "review" if st == "review" else "doing" if st == "doing" else "ready" if st == "todo"
				  else "waiting_on_human" if t.get("waiting_on") or t.get("proposed_by") else "blocked"] += 1
			return c
		keys = {m["key"] for m in self.config["milestones"]}
		out = []
		for m in self.config["milestones"]:
			c = counts([t for t in ts if t["milestone"] == m["key"]])
			out.append({"key": m["key"], "title": m["title"], "goal": m.get("goal", ""), "exit_criteria": m.get("exit_criteria", []),
			            "target": m.get("target"), "closed": bool(m.get("closed")), "next": m["key"] == nxt,
			            "all_done": c["total"] > 0 and c["done"] == c["total"], "counts": c})
		return {"milestones": out, "next": nxt, "unscheduled": counts([t for t in ts if t["milestone"] not in keys])}

	def _check_caps(self, clean: dict, old: dict) -> None:
		"""Refuse skill / mcp names that are not in the board's registry, unless already on the ticket (an entry removed later keeps it editable)."""
		for k in ("skills", "mcp"):
			bad = [x for x in clean.get(k, []) if x not in old.get(k, []) and x not in self.config[k]]
			if bad:
				raise TicketError(f"unknown {k} {', '.join(bad)}: this board's {k} are {', '.join(self.config[k]) or 'none (register them in ' + CONFIG_FILE + ')'}")

	def _check_parent(self, tid: str, parent: str | None, extra: dict = {}) -> None:
		"""Refuse a parent that doesn't exist or that would make a ticket its own ancestor. extra = more {id: parent} (a create_many batch)."""
		if parent is None:
			return
		if parent == tid:
			raise TicketError(f"{tid} can't be its own parent")
		raw = self._raw()

		def up(x):
			return extra[x] if x in extra else raw[x].get("parent") if x in raw else None
		if parent not in raw and parent not in extra:
			raise TicketError(f"parent {parent} doesn't exist: create it first or set parent to null")
		chain, x = [tid, parent], up(parent)
		while x and x not in chain:
			chain.append(x)
			x = up(x)
		if x == tid:
			raise TicketError("parent makes a loop: " + " → ".join(chain + [tid]) + " (a ticket can't be part of its own part)")

	# ---------- derived view ----------
	def _decorate(self, tickets: list[dict]) -> list[dict]:
		"""Add derived fields to every ticket (the whole board, re-derived; _board() re-derives only what changed). A todo with an open
		blocker, a waiting_on (a question for a human) or a proposed_by (not accepted yet) shows as pending (effective_status).
		unblocks = not-done tickets downstream, missing_blockers = blocked_by ids with no file,
		stale_days = days since update for a doing ticket, score = priority weight x (1 + unblocks)."""
		return list(self._derive({t["id"]: t for t in tickets}, None, None)[0].values())

	def _derive(self, raw: dict, base: tuple | None, prev: dict | None) -> tuple[dict, dict]:
		"""(decorated board by id, state for the next call). With prev (the last derive's state on this board, same _base()) only what
		changed is re-derived (CORE-85: at 20,000 tickets every write re-derived the whole board, 0.3-0.5 s, mostly under the lock): the
		tickets whose stored object, claim or attachments changed and, when one appeared, went, turned done / not done or changed its
		blocked_by or parent, its dependents (open_blockers), its blockers (blocks), its parents (children, progress) and everything
		upstream of it (unblocks, score). The whole board (_full) when there is no prev, a blocked_by loop, or a big part changed."""
		live = self._live_claims()
		ctx = {"raw": raw, "today": date.today(), "links": self._branch_links(), "attached": self.backend.attachment_names(),
		       "claims": {tid: {**{k: v for k, v in c.items() if k not in ("age_s", "quiet_s")},  # expires_at: profile lease or heartbeat(until=) (CORE-83)
		                        "expires_at": _iso(self._expires(c))} for tid, c in live.items()}}
		out = self._redecorate(ctx, prev) if prev is not None and base is not None and prev["base"] == base else None
		dec, state = out or self._full(ctx)
		state["base"] = base
		return dec, state

	def _full(self, ctx: dict) -> tuple[dict, dict]:
		raw = ctx["raw"]
		rev: dict[str, list[str]] = {}  # blocker id (may not exist) -> tickets naming it in blocked_by, in id order
		kids: dict[str, list[str]] = {}  # parent id -> its children, in id order
		for i, t in raw.items():
			if t.get("parent") is not None:
				kids.setdefault(t["parent"], []).append(i)
			for b in t.get("blocked_by", []):
				rev.setdefault(b, []).append(i)
		# everything downstream of each ticket as one bitset, built in a single pass (a ticket's set = its dependents' sets + them)
		idx = {x: i for i, x in enumerate(raw)}
		open_mask = 0
		for x, t in raw.items():
			if t.get("status") != "done":
				open_mask |= 1 << idx[x]
		down: dict[str, int] = {}
		loop = False
		for root in raw:
			if root in down:
				continue
			acc, onpath, stack = {root: 0}, {root}, [(root, iter(rev.get(root, ())))]
			while stack:
				x, it = stack[-1]
				for y in it:
					if y in down:
						acc[x] |= down[y] | 1 << idx[y]
					elif y in onpath:  # a blocked_by loop in an old file: count it, don't follow it
						acc[x] |= 1 << idx[y]
						loop = True
					else:
						onpath.add(y)
						acc[y] = 0
						stack.append((y, iter(rev.get(y, ()))))
						break
				else:
					stack.pop()
					onpath.discard(x)
					down[x] = acc.pop(x)
					if stack:
						acc[stack[-1][0]] |= down[x] | 1 << idx[x]
		dec = {i: self._dec1(t, ctx, rev, kids, bin(down[i] & open_mask & ~(1 << idx[i])).count("1")) for i, t in raw.items()}
		return dec, {"raw": raw, "dec": dec, "rev": rev, "kids": kids, "claims": ctx["claims"], "attached": ctx["attached"], "loop": loop}

	def _dec1(self, t: dict, ctx: dict, rev: dict, kids: dict, n: int) -> dict:
		"""One ticket decorated; n = its unblocks."""
		raw, i = ctx["raw"], t["id"]
		open_b = [b for b in t.get("blocked_by", []) if b in raw and raw[b].get("status") != "done"]
		eff = "pending" if t.get("status") == "todo" and (open_b or t.get("waiting_on") or t.get("proposed_by")) else t.get("status")
		br = ctx["links"].get(i, []) if t.get("status") != "done" else []
		if eff == "todo" and any(b["active"] for b in br):
			eff = "doing"  # work on an unmerged branch nobody claimed here (another clone, a cloud session): derived, never written
		stale = None
		if t.get("status") == "doing":
			try:
				stale = max(0, (ctx["today"] - date.fromisoformat(str(t.get("updated"))[:10])).days)
			except ValueError:
				stale = None
		ks = kids.get(i, [])
		return {**t, "parent": t.get("parent"), "milestone": t.get("milestone"), "components": t.get("components", []), "skills": t.get("skills", []), "mcp": t.get("mcp", []), "children": list(ks),
		        "progress": {"done": sum(raw[k].get("status") == "done" for k in ks), "total": len(ks)},
		        "effective_status": eff, "open_blockers": open_b, "blocks": list(rev.get(i, [])),
		        "unblocks": n, "missing_blockers": [b for b in t.get("blocked_by", []) if b not in raw],
		        "stale_days": stale, "score": PRIO_WEIGHT.get(t.get("priority"), 1) * (1 + n),
		        "tags": t.get("tags", []), "touches": t.get("touches", []), "agent": self.route(t),
		        # timestamps only (no ages), so a ticket's JSON doesn't change while nothing happens
		        "docs": t.get("docs", []), "notes": t.get("notes", []), "needs": t.get("needs", []), "files": ctx["attached"].get(i, []), "branches": br,
		        "claim": ctx["claims"].get(i), **({"unreported": True} if _unreported(t) else {})}

	def _redecorate(self, ctx: dict, prev: dict) -> tuple[dict, dict] | None:
		"""_derive's incremental half. None = derive the whole board (a blocked_by loop, or too much changed)."""
		raw, old, claims, attached = ctx["raw"], prev["raw"], ctx["claims"], ctx["attached"]
		if prev["loop"]:
			return None
		changed = {i for i, t in raw.items() if old.get(i) is not t}  # a connector hands out the same object while a ticket is unchanged
		if len(raw) - sum(i not in old for i in changed) != len(old):
			changed |= {i for i in old if i not in raw}
		cap = max(64, len(raw) // 20)
		touched = set(changed)
		touched |= {i for i in claims.keys() | prev["claims"].keys() if claims.get(i) != prev["claims"].get(i)}
		touched |= {i for i in attached.keys() | prev["attached"].keys() if attached.get(i) != prev["attached"].get(i)}
		if len(touched) > cap:
			return None

		def done(t):
			return t.get("status") == "done"
		struct = [i for i in changed if i not in old or i not in raw or done(old[i]) != done(raw[i])
		          or old[i].get("blocked_by", []) != raw[i].get("blocked_by", []) or old[i].get("parent") != raw[i].get("parent")]
		rev, kids = prev["rev"], prev["kids"]
		up: set[str] = set()  # tickets whose unblocks may have changed
		if struct:
			rev, kids = dict(rev), dict(kids)  # copy on write: another thread may be deriving from prev right now
			fix_r, fix_k = set(), set()
			for i in struct:
				o, t = old.get(i) or {}, raw.get(i) or {}
				ob, nb = o.get("blocked_by", []), t.get("blocked_by", [])
				if ob != nb:
					for b in set(ob):
						rev[b] = [x for x in rev.get(b, []) if x != i]
					for b in nb:
						rev[b] = rev.get(b, []) + [i]
					fix_r |= set(ob) | set(nb)
				op, np = o.get("parent"), t.get("parent")
				if op != np:
					if op is not None:
						kids[op] = [x for x in kids.get(op, []) if x != i]
						fix_k.add(op)
					if np is not None:
						kids[np] = kids.get(np, []) + [i]
						fix_k.add(np)
				touched |= set(rev.get(i, [])) | set(prev["rev"].get(i, [])) | set(ob) | set(nb) | ({op, np} - {None})
				for graph, start in ((raw, nb), (old, ob)):  # everything upstream, before and after the change: its downstream changed
					seen, stack = set(), list(start)
					while stack:
						x = stack.pop()
						if x == i and graph is raw:
							return None  # a new blocked_by loop (a hand edit or a pull): the whole board counts it
						if x in seen or x not in graph:
							continue
						seen.add(x)
						stack.extend(graph[x].get("blocked_by", []))
					up |= seen
					if len(up) > cap:
						return None
			if fix_r or fix_k:
				pos = {x: k for k, x in enumerate(raw)}  # id order, as _full builds them
				for d, fix in ((rev, fix_r), (kids, fix_k)):
					for k in fix:
						if d.get(k):
							d[k] = sorted(d[k], key=pos.__getitem__)
						else:
							d.pop(k, None)
		up = {x for x in up if x in raw} | {i for i in changed if i in raw and i not in old}  # a new ticket: count its downstream too
		touched = {x for x in touched | up if x in raw}
		if len(touched) > cap:
			return None
		odec, new, work = prev["dec"], {}, 0
		for x in touched:
			if x in up:  # open tickets downstream of x (not x itself), over the new graph
				seen, stack = {x}, list(rev.get(x, []))
				while stack:
					y = stack.pop()
					if y not in seen:
						seen.add(y)
						stack.extend(rev.get(y, []))
				work += len(seen)
				if work > 20 * cap:
					return None
				n = sum(1 for y in seen if y != x and raw[y].get("status") != "done")
			else:
				n = odec[x]["unblocks"]
			new[x] = self._dec1(raw[x], ctx, rev, kids, n)
		dec = {i: new[i] if i in new else odec[i] for i in raw}
		return dec, {"raw": raw, "dec": dec, "rev": rev, "kids": kids, "claims": claims, "attached": attached, "loop": False}

	def _branch_links(self) -> dict[str, list[dict]]:
		"""{ticket id: [branch]} from branches.json (written by the web server's branches thread; absent = no git evidence).
		active = linked by the branch's name or a ticket file it edits (a commit subject alone is a mention, not a claim on the work),
		with a commit in the last BRANCH_DAYS days, and not dismissed by moving the ticket back to todo (branches_ignored.json)."""
		if self.rt is None:  # a database board: no git checkout, no evidence
			return {}
		try:
			data = json.loads((self.rt / "branches.json").read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return {}
		try:
			ign = json.loads((self.rt / "branches_ignored.json").read_text(encoding="utf-8"))
		except (OSError, ValueError):
			ign = {}
		today, out = date.today(), {}
		for b in data.get("branches") or []:
			try:
				age = (today - date.fromisoformat(str(b.get("at"))[:10])).days
			except ValueError:
				continue
			for tid, via in (b.get("tickets") or {}).items():
				e = {"name": b.get("name"), "sha": b.get("sha"), "at": b.get("at"), "author": b.get("author") or "", "commits": b.get("commits"), "via": via,
				     "active": bool({"name", "file"} & set(via)) and age <= BRANCH_DAYS and b.get("sha") not in ign.get(tid, [])}
				if tid in (b.get("edits") or {}):
					e["edit"] = b["edits"][tid]
				out.setdefault(tid, []).append(e)
		return out

	def _ignore_branches(self, tid: str, shas: list[str]) -> None:
		"""A human moved a ticket that only reads as doing because of a branch back to todo: that branch tip no longer counts
		for it (a new commit on the branch counts again). Runtime state, like claims (caller holds the lock)."""
		if self.rt is None:
			return
		p = self.rt / "branches_ignored.json"
		try:
			ign = json.loads(p.read_text(encoding="utf-8"))
		except (OSError, ValueError):
			ign = {}
		ign[tid] = sorted(set(ign.get(tid, [])) | set(shas))[-20:]
		self._rt()
		p.write_text(json.dumps(ign, indent=1), encoding="utf-8")

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

	def unroutable(self, t: dict) -> list[str]:
		"""CORE-82: a warning when no agent profile will take this open ticket (board with profiles, not owned by a person):
		claim_next never offers it to anyone, so the creator is told what to set. [] otherwise (profile-less boards: nothing)."""
		ags = self.config["agents"]
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		if not ags or t.get("status") == "done" or t.get("owner") in humans or self.route(t):
			return []
		pil = list(dict.fromkeys(p for a in ags for p in a.get("pillars", [])))
		tags = list(dict.fromkeys(x for a in ags for x in a.get("tags", [])))
		how = ", ".join(x for x in (f"set pillar {'/'.join(pil)}" if pil else "", f"a tag {'/'.join(tags)}" if tags else "",
		                                f"tag agent:<{'|'.join(a['name'] for a in ags)}>") if x)
		return [f"no agent profile takes {t.get('id')}: {how}, or owner a person (claim_next never offers it)"]

	def is_agent(self, name: str, tid: str | None = None) -> bool:
		"""Is this author a worker rather than a person? A name that resolves to a profile, an agent owner, the holder of
		tid's live claim, or a service (github). Everyone else (Omri, human, anyone typing in the web board) is a person."""
		agents = {o["name"] for o in self.config["owners"] if o.get("kind") == "agent"}
		held = self._claims().get(tid, {}).get("agent") if tid else None
		return bool(self.profile_of(name)) or name in agents or name == held or name == "github"

	@staticmethod
	def lean_notes(notes: list[dict]) -> tuple[list[dict], int]:
		"""What a claim sends: every note while their text is short (NOTES_SHORT); past it every pinned note that nothing superseded
		(decisions, findings, warnings, human notes, answers, rejections, approvals), the last NOTES_TAIL others, and the note any kept
		note replies to or supersedes, so nothing arrives without what it refers to. In order. Returns (notes, how many were left out)."""
		if sum(len(n.get("text", "")) for n in notes) <= NOTES_SHORT:
			return notes, 0
		keep = {i for i, n in enumerate(notes) if n.get("pinned") and not n.get("superseded_by")}
		keep |= set([i for i, n in enumerate(notes) if i not in keep][-NOTES_TAIL:])
		for i in list(keep):
			for ref in (notes[i].get("reply_to"), notes[i].get("supersedes")):
				if isinstance(ref, int) and 1 <= ref <= len(notes):
					keep.add(ref - 1)
		return [notes[i] for i in sorted(keep)], len(notes) - len(keep)

	def profile_of(self, agent: str) -> str | None:
		"""The agent profile a worker name belongs to: the profile named exactly like it, or the one it starts with (backend-1 -> backend)."""
		return next((a["name"] for a in self.config["agents"] if agent == a["name"] or agent.startswith(a["name"] + "-")), None)

	def work_plan(self, per_agent: int = 3) -> dict:
		"""Ready tickets not owned by a human, grouped by the agent profile that should take them
		(best first), for an orchestrator to fan out: {"agents": [{profile..., "tickets": [...]}], "unrouted": [...]}.
		A ticket with no event and no write for the board's stale_days carries `idle_days` + `stale_hint` (AI-57): its work may have
		landed under another ticket, so check before handing it out. `stale` = how many such tickets the plan holds."""
		ready = self.next_ready(limit=10_000)
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		ready = [t for t in ready if t.get("owner") not in humans]  # a person's ticket is theirs, whatever its tags
		idle = self._idle(ready, self.stale_days)
		for t in ready:
			if t["id"] in idle:
				t["idle_days"], t["stale_hint"] = idle[t["id"]], _STALE_HINT
		out = []
		for ag in self.config["agents"]:
			mine = [t for t in ready if t["agent"] == ag["name"]]
			out.append({**ag, "skills": ag.get("skills", []), "mcp": ag.get("mcp", []), "ready": len(mine), "tickets": mine[:max(0, int(per_agent))]})
		unrouted = [t for t in ready if t["agent"] is None]
		return {"agents": out, "unrouted": unrouted[:max(0, int(per_agent))], "unrouted_count": len(unrouted), "stale": len(idle)}

	def _idle(self, tickets: list[dict], days: int) -> dict:
		"""AI-57: {id: idle days} for the todo tickets among `tickets` with no activity event and no write (`updated`) for `days` days or
		more (idle days = since `updated`, at least `days`). One newest-first pass over the log, stopped at the cutoff."""
		todo = {t["id"]: t for t in tickets if t.get("status") == "todo"}
		if not todo:
			return {}
		cutoff, recent = time.time() - days * 86400, set()
		for e in self.backend.events_newest_first():
			if _ts(e["ts"]) < cutoff:
				break
			recent.add(e.get("ticket"))
		today, out = date.today(), {}
		for tid, t in todo.items():
			if tid in recent:
				continue
			try:
				idle = (today - date.fromisoformat(str(t.get("updated") or t.get("created") or "")[:10])).days
			except ValueError:
				idle = days
			if idle >= days:
				out[tid] = idle
		return out

	# ---------- CRUD ----------
	def list(self, status: str | None = None, pillar: str | None = None, owner: str | None = None,
	         priority: str | None = None, q: str | None = None, tag: str | None = None,
	         agent: str | None = None, parent: str | None = None, milestone: str | None = None, board: dict | None = None,
	         limit: int | None = None, touches: str | None = None, component: str | None = None, ids=None, stale_days: int | None = None) -> list[dict]:
		"""All tickets, decorated. Filters match effective_status (so 'pending' includes blocked todos). milestone "-" = none.
		board = a _board() the caller already read in this locked step (claim), so it is not read again.
		touches = a path or glob: tickets whose `touches` overlap it by the claim rule (_overlap: same path, one inside the other, a glob
		match), so "who works on store.py?" is not a substring search. component = an architecture key: tickets that name it in
		`components` or whose touches fall in its note's paths (the Map's rule). limit = at most that many (after the filters, in id order;
		with ids, in their order).
		ids = only those tickets (a list, or a comma string: REST / CLI), in that order; ids not on the board are left out (API-23).
		q = words that must ALL appear in one ticket (q_hint says so when nothing matched).
		stale_days = N: only todo tickets with no activity event and no write for N days or more, each with `idle_days` (AI-57: its
		work may have landed under another ticket: close it with complete(closes=[...]) if so)."""
		if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
			raise TicketError(f"limit must be a whole number >= 0, not {limit!r}")
		if stale_days is not None and (isinstance(stale_days, bool) or not isinstance(stale_days, int) or stale_days < 1):
			raise TicketError(f"stale_days must be a whole number of days >= 1, not {stale_days!r}")
		cur = self._board() if board is None else board  # cached: filter first, copy what is returned
		if ids is None:
			res = list(cur.values())
		else:  # case-blind match on the stored id (a-12b finds A-12b), deduplicated after matching (a-2 and A-2 are one ticket)
			upper = {k.upper(): k for k in cur}
			keys = (i if i in cur else upper.get(i.upper()) for i in list_ids(ids))
			res = [cur[k] for k in dict.fromkeys(k for k in keys if k)]
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
			agent = self.profile_of(agent) or agent  # a worker name (backend-1) means its profile, as in claim
			res = [t for t in res if t["agent"] == agent]
		if parent:
			res = [t for t in res if t["parent"] == parent]
		if milestone:
			res = [t for t in res if t["milestone"] == (None if milestone == "-" else milestone)]
		if touches is not None:
			if not (isinstance(touches, str) and touches.strip()):
				raise TicketError("touches must be a path or glob, e.g. unblockd/store.py or static/*")
			res = [t for t in res if any(_overlap(touches.strip(), x) for x in t.get("touches", []))]
		if component:
			from .knowledge import components, ticket_components  # lazy: knowledge imports this module
			arch = components(self)
			res = [t for t in res if component in t.get("components", []) or component in ticket_components(t, arch)]
		if q:
			words = q.lower().split()
			res = [t for t in res if all(w in json.dumps(t, ensure_ascii=False).lower() for w in words)]
		idle = None
		if stale_days is not None:
			idle = self._idle(res, stale_days)
			res = [t for t in res if t["id"] in idle]
		out = [_copy(t) for t in (res if limit is None else res[:limit])]
		for t in out if idle else ():
			t["idle_days"] = idle[t["id"]]
		return out

	def next_ready(self, owner: str | None = None, pillar: str | None = None, limit: int = 5,
	               agent: str | None = None, tag: str | None = None, board: dict | None = None) -> list[dict]:
		"""Ready tickets (effective todo: not blocked, started or done), best first:
		score = priority weight x (1 + open tickets it unblocks), then the current milestone's tickets (a tie-break: it never beats a
		higher score), then priority, then id."""
		res = self.list(status="todo", pillar=pillar, owner=owner, agent=agent, tag=tag, board=board)
		nxt = self.next_milestone()
		res.sort(key=lambda t: (-t["score"], not (nxt and t["milestone"] == nxt), PRIORITIES.index(t["priority"]) if t.get("priority") else 9, t["id"]))
		return res[:max(0, int(limit))]

	def get(self, tid: str) -> dict:
		self._valid(tid)
		t = self._board().get(tid)
		if t is None:
			raise NotFound(f"no ticket {tid}")
		return _copy(t)

	def with_blockers(self, t: dict) -> dict:
		"""t plus `blockers`: every blocked_by id with its effective_status and title (missing = no such ticket), for a reader that
		wants to know which blockers are done without fetching each one (get_ticket, REST GET tickets/<id>, CLI get)."""
		board = self._board()
		return {**t, "blockers": [{"id": b, "effective_status": board[b]["effective_status"], "title": board[b].get("title", "")} if b in board
		                          else {"id": b, "effective_status": "missing", "title": ""} for b in t.get("blocked_by", [])]}

	def board_sig(self) -> tuple:
		"""(signature, board): the key _board() caches the decorated board under (backend.version(), the date, the config, a files
		board's branch stamps) and that board, read in one go, so a caller can cache what it derives from the board (web: the GET
		tickets body and its ETag, CORE-65). Works on every connector: a database board's version is its board row's counter.
		signature None = another thread replaced the cache meanwhile, or a transaction is open: don't cache. Read only: never mutate."""
		b = self._board()
		hit = _DEC.get(self._key)
		return (hit[0] if hit and hit[1] is b else None), b

	def next_id(self, prefix: str) -> str:
		if not PREFIX_RE.match(prefix or ""):
			raise TicketError(f"bad prefix {prefix!r}: expected like BUG or ART")
		pat = re.compile(rf"(?<![A-Z0-9]){re.escape(prefix)}-(\d+)")
		nums = self.backend.numbers(prefix)
		for f in self.legacy_files:
			if f.exists():
				nums += [int(m) for m in pat.findall(f.read_text(encoding="utf-8", errors="ignore"))]
		nums.append(self._taken_elsewhere().get(prefix, 0))
		return f"{prefix}-{max(nums, default=0) + 1}"

	def _taken_elsewhere(self) -> dict[str, int]:
		"""{prefix: highest number} on origin's default branch and on unmerged branches, from branches.json (the web server's branch
		scan; absent = {}). Another clone's lock can't stop it picking the same next number, so a checkout that is behind skips past
		what origin and other branches already hold (CORE-76)."""
		if self.rt is None:
			return {}
		try:
			got = json.loads((self.rt / "branches.json").read_text(encoding="utf-8")).get("id_max") or {}
		except (OSError, ValueError, AttributeError):
			return {}
		return {k: v for k, v in got.items() if isinstance(v, int)} if isinstance(got, dict) else {}

	def create(self, data: dict, tid: str | None = None, prefix: str | None = None, agent: str | None = None, request_id: str | None = None) -> dict:
		"""Create from `data`. Give an explicit `tid`, or a `prefix` to get the next free number (a board whose schema lists exactly
		one prefix needs neither). `agent` = the worker creating it (None = a human). The board's schema is enforced here, not in
		_create, so split children and existing tickets are exempt: an agent breaking it is refused with every problem listed,
		a human gets the ticket plus `warnings`. On a board with agent_tickets "propose" an agent's ticket is a proposal until accept().
		An `agent` naming a `kind: human` owner is that person (MCP create_ticket requires a name, so their own Claude sends theirs):
		no proposal (they could never accept their own) and no strict schema; the `created` event still names them."""
		agent, strict = self._creator(agent)
		def run():
			if (seen := self._seen_request(agent, request_id, "create")):
				return seen
			pre = self._pick_prefix(tid, prefix)
			problems = self._schema_problems(data, tid.split("-")[0] if tid else pre, strict)
			if problems and strict:
				raise TicketError(self._schema_refusal(problems))
			t = self._create(data, tid, pre, proposed_by=agent if strict and self.config["agent_tickets"] == "propose" else "", agent=agent)
			self._record_request(agent, request_id, "create", t["id"])
			problems = problems + ([_NO_REPORT.format(id=t["id"])] if t["status"] == "done" else []) + self.unroutable(t)  # a new ticket never has a result
			return {**t, "warnings": problems} if problems else t
		got = self._locked(run)
		return self._already(got) if "already" in got else got

	# ---------- idempotent writes (CORE-74) ----------
	def _seen_request(self, agent: str | None, request_id: str | None, op: str, tid: str | None = None) -> dict | None:
		"""Caller holds the lock. A request_id this agent already used: {already, ticket, at} for the same write, refused for another
		(an id names ONE write). None = new (or no request_id)."""
		if request_id is None or request_id == "":
			return None
		if not (isinstance(request_id, str) and REQUEST_RE.match(request_id)):
			raise TicketError("request_id must be 1-100 letters, digits or . _ : - (one fresh id per write, e.g. a uuid)")
		e = self.backend.get_doc("requests").get(f"{agent or 'human'} {request_id}")
		if e is None:
			return None
		if e["op"] != op or (tid and e["ticket"] != tid):
			raise TicketError(f"request_id {request_id} was already used for {e['op']} on {e['ticket']}: use a new id for each write")
		return {"already": True, "ticket": e["ticket"], "at": e["at"], "request_id": request_id}

	def _record_request(self, agent: str | None, request_id: str | None, op: str, tid: str) -> None:
		"""Caller holds the lock, and has just made the write."""
		if not request_id:
			return
		doc = self.backend.get_doc("requests")
		doc[f"{agent or 'human'} {request_id}"] = {"op": op, "ticket": tid, "at": _now()}
		for k in list(doc)[:max(0, len(doc) - REQUESTS_MAX)]:
			doc.pop(k)
		self.backend.put_doc("requests", doc)

	def _last_transition(self, tid: str) -> dict | None:
		"""The newest claim-changing event of tid (a note or heartbeat is not one)."""
		return next((e for e in self.backend.events_newest_first(ticket=tid) if e.get("ticket") == tid and e.get("event") in TRANSITIONS), None)

	def _repeat(self, tid: str, agent: str, op: str) -> dict | None:
		"""Caller holds the lock. The same agent repeating the transition it just made (a retry after a timeout that landed):
		{already, ticket, at} instead of "isn't claimed". Only when nobody holds tid now and its newest transition is that agent's `op`."""
		if tid in self._claims():
			return None
		e = self._last_transition(tid)
		if not (e and e.get("agent") == agent and e.get("event") == op):
			return None
		raw = self._fresh(tid)
		if op == "complete" and raw.get("status") not in ("review", "done"):
			return None
		if op == "release" and raw.get("status") == "doing":
			return None
		return {"already": True, "ticket": tid, "at": e["ts"]}

	def _already(self, seen: dict) -> dict:
		"""The reply to a repeated write: the ticket as it is, marked already (nothing was changed)."""
		return {**self.get(seen["ticket"]), "already": True, "first_at": seen["at"], **({"request_id": seen["request_id"]} if seen.get("request_id") else {})}

	def _creator(self, agent: str | None) -> tuple[str | None, bool]:
		"""(agent or None, strict): strict = an agent that is not a `kind: human` owner (schema refuses, proposal gate applies)."""
		agent = agent or None
		human = agent in {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		return agent, agent is not None and not human

	def _pick_prefix(self, tid: str | None, prefix: str | None) -> str | None:
		"""No prefix and no id: a schema with exactly one prefix uses it; several = refused listing them."""
		pf = (self.config["schema"] or {}).get("prefixes") or {}
		if tid is None and not prefix and pf:
			if len(pf) != 1:
				raise TicketError(f"give a prefix or an id: this board's prefixes are {', '.join(pf)} (board_info says what each means)")
			return next(iter(pf))
		return prefix

	@staticmethod
	def _schema_refusal(problems: list[str]) -> str:
		msg = "ticket refused by the board schema: " + "; ".join(problems)
		return (msg[:400] + ("…" if len(msg) > 400 else "")
		        + (" (meanings: board_info; if none fits, ask_human, do not guess a near prefix)" if any(x.startswith("prefix") for x in problems) else ""))

	def create_many(self, items: list, agent: str | None = None) -> dict:
		"""Create up to BATCH_MAX tickets in ONE locked, all-or-nothing step. Each item = what create() takes as data, plus optional
		`prefix` / `id`. `blocked_by` and `parent` may name existing ids or "#N" = the Nth item of this batch (1-based), so a plan's
		tickets can wait on each other before they have ids. Schema and proposal gate apply per item exactly as in create().
		Everything is validated before anything is written: any problem = one TicketError naming every one by item ("#2: ...").
		Returns {tickets, ids: {"#1": "ART-60", ...}, warnings?: {"#N": [...]}} (warnings: a human's schema breaks, as in create())."""
		agent, strict = self._creator(agent)
		if not isinstance(items, list) or not items:
			raise TicketError("tickets must be a non-empty list of ticket objects")
		if len(items) > BATCH_MAX:
			raise TicketError(f"{len(items)} tickets in one batch: at most {BATCH_MAX}; create the rest in another call (refer to these by their new ids)")
		def run():
			errs: list[str] = []
			def bad(i, e):
				msg = f"#{i}: {e}"
				if msg not in errs:
					errs.append(msg)
			# 1) ids: explicit ones first, then the next free numbers per prefix, skipping ids taken in this batch
			tids: list[str | None] = [None] * len(items)
			pres: list[str | None] = [None] * len(items)
			for i, it in enumerate(items):
				if not isinstance(it, dict):
					bad(i + 1, "ticket data must be an object")
					continue
				try:
					tid = it.get("id")
					if tid is not None:
						if not isinstance(tid, str):
							raise TicketError(f"invalid id {tid!r}")
						if self._exists(tid):
							raise TicketError(f"{tid} already exists")
						if tid in tids:
							raise TicketError(f"{tid} is given twice in this batch")
						tids[i] = tid
					else:
						pres[i] = self._pick_prefix(None, it.get("prefix"))
						if not pres[i]:
							raise TicketError("give an id or a prefix (e.g. BUG)")
				except TicketError as e:
					bad(i + 1, e)
			nxt: dict[str, int] = {}
			for i, pre in enumerate(pres):
				if pre is None or tids[i]:
					continue
				try:
					n = nxt.get(pre) or int(self.next_id(pre).rsplit("-", 1)[1])
				except TicketError as e:
					bad(i + 1, e)
					continue
				while f"{pre}-{n}" in tids:
					n += 1
				tids[i], nxt[pre] = f"{pre}-{n}", n + 1
			# 2) resolve "#N", clean and check each item against the board plus the rest of the batch
			def ref(x):
				if isinstance(x, str) and x.startswith("#"):
					k = int(x[1:]) if x[1:].isdigit() else 0
					if not 1 <= k <= len(items):
						raise TicketError(f"{x} is not an item of this batch (#1 to #{len(items)})")
					if not tids[k - 1]:
						raise TicketError(f"{x} has no id (see its own problem)")
					return tids[k - 1]
				return x
			datas: list[dict] = []
			for i, it in enumerate(items):
				d = {k: v for k, v in it.items() if k not in ("id", "prefix")} if isinstance(it, dict) else {}
				try:
					if isinstance(d.get("blocked_by"), list):
						d["blocked_by"] = [ref(x) for x in d["blocked_by"]]
					if "parent" in d:
						d["parent"] = ref(d["parent"])
				except TicketError as e:
					bad(i + 1, e)
				datas.append(d)
			known = {x for x in tids if x}
			graph = {tids[i]: d["blocked_by"] for i, d in enumerate(datas) if tids[i] and isinstance(d.get("blocked_by"), list)}
			ups = {tids[i]: d.get("parent") for i, d in enumerate(datas) if tids[i]}
			built, warns = [], {}
			proposed = agent if strict and self.config["agent_tickets"] == "propose" else ""
			for i, d in enumerate(datas):
				if not tids[i]:
					continue  # already reported
				problems = self._schema_problems(d, tids[i].split("-")[0], strict)
				if problems and strict:
					bad(i + 1, self._schema_refusal(problems))
				elif problems:
					warns[f"#{i + 1}"] = problems
				try:
					built.append(self._prepare(d, tids[i], proposed_by=proposed, batch=(known, graph, ups)))
				except TicketError as e:
					bad(i + 1, e)
			if errs:
				raise TicketError(self._batch_refusal(errs))
			# 3) write them all (undone if a write fails), then log
			done = []
			try:
				for t in built:
					self._write(t)
					done.append(t["id"])
			except OSError as e:  # a database connector's write is inside the lock's transaction: it rolls back on its own
				for x in done:
					if self._exists(x):
						self.backend.delete(x)
				raise TicketError(f"could not write the batch ({e}): nothing was created")
			for t in built:
				self._log("created", t["id"], agent, t["title"])
				if proposed:
					self._log("proposed", t["id"], proposed, t["title"])
			out = {"tickets": [self.get(x) for x in done], "ids": {f"#{i + 1}": x for i, x in enumerate(done)}}
			for i, t in enumerate(out["tickets"]):
				if (w := ([_NO_REPORT.format(id=t["id"])] if t["status"] == "done" else []) + self.unroutable(t)):  # done = no summary, as in create() (CORE-83)
					warns.setdefault(f"#{i + 1}", []).extend(w)
			return {**out, "warnings": warns} if warns else out
		return self._locked(run)

	@staticmethod
	def _batch_refusal(errs: list[str], verb: str = "created") -> str:
		msg = f"batch refused, nothing was {verb} ({len(errs)} problem{'s' if len(errs) > 1 else ''}): " + " | ".join(errs)
		return msg[:2000] + ("…" if len(msg) > 2000 else "")

	def _schema_problems(self, data: dict, prefix: str | None, strict: bool) -> list[str]:
		sc = self.config["schema"]
		if not sc:
			return []
		out, pf = [], sc.get("prefixes") or {}
		if pf and prefix not in pf:
			out.append(f"prefix {prefix} is not one of {', '.join(pf)}")
		out += [f"{f} is required" for f in sc.get("required", []) if not data.get(f)]
		if sc.get("tags") is not None:
			bad = [x for x in data.get("tags") or [] if isinstance(x, str) and not x.startswith("agent:") and x.strip().lower() not in sc["tags"]]
			if bad:
				out.append(f"tag {', '.join(bad)} is not one of {', '.join(sc['tags']) or 'none'}")
		if strict and sc.get("body_template") and not (data.get("body") or "").strip():
			out.append("body is required (template: " + " / ".join(x for x in sc["body_template"].splitlines() if x.strip()) + ")")
		return out

	def _create(self, data: dict, tid: str | None = None, prefix: str | None = None, inherited: list[str] = (), inherited_caps: dict = {}, inherited_components: list[str] = (), proposed_by: str = "", agent: str | None = None) -> dict:
		if tid is None:
			if not prefix:
				raise TicketError("give an id or a prefix (e.g. BUG)")
			tid = self.next_id(prefix)
		if self._exists(tid):
			raise TicketError(f"{tid} already exists")
		t = self._prepare(data, tid, inherited, inherited_caps, inherited_components, proposed_by)
		self._write(t)
		self._log("created", tid, agent, t["title"])
		if proposed_by:
			self._log("proposed", tid, proposed_by, t["title"])
		return self.get(tid)

	def _prepare(self, data: dict, tid: str, inherited: list[str] = (), inherited_caps: dict = {}, inherited_components: list[str] = (), proposed_by: str = "", batch: tuple = None) -> dict:
		"""Validate `data` as new ticket `tid` and return the ticket dict, unwritten. batch = (ids, blocked_by graph, parents) of the
		new tickets of a create_many: they count as existing for blocked_by / parent and their edges join the loop checks."""
		clean = self._clean(data, partial=False)
		if clean.get("status") == "review":  # CORE-19: a new ticket never has a result, so it can't be waiting for review (create, create_many)
			raise TicketError(_REVIEW_NEEDS_RESULT.format(id=tid))
		known, graph, ups = batch or ((), {}, {})
		self._check_blockers(clean.get("blocked_by", []), list(inherited) + list(known))
		self._check_loop(tid, clean.get("blocked_by", []), graph)
		self._check_parent(tid, clean.get("parent"), ups)
		self._check_milestone(clean.get("milestone"), None)
		self._check_components(clean.get("components", []), inherited_components)
		self._check_caps(clean, inherited_caps)
		today = date.today().isoformat()
		t = {"id": tid}
		for k, (default, _) in FIELDS.items():
			t[k] = clean.get(k, default if not isinstance(default, list) else list(default))
		if t["owner"] is None:
			t["owner"] = self.config["default_owner"]
		t["created"] = t["updated"] = today
		if t["status"] == "done":
			t["done_at"] = _now()
		if t["waiting_on"]:
			t["asked_at"] = _now()
		t["proposed_by"] = proposed_by
		return t

	def update(self, tid: str, changes: dict, agent: str | None = None, expected_rev: int | None = None, by: str | None = None) -> dict:
		"""Change fields. `agent` = the worker making the change (MCP/CLI agents); None = a human (web, CLI).
		`expected_rev` = the rev the caller last saw: if the ticket was written since, raise Conflict instead of overwriting.
		`by` = who a human change is by (only recorded: the `done` event names agent, else by, else "human"). A move to done with no
		result comes back with `warnings` naming complete() (CORE-68: not refused, so a solo board keeps working)."""
		return self._with_lease(self._locked(lambda: self._update(tid, changes, agent, expected_rev, by)), agent)

	def _update(self, tid: str, changes: dict, agent: str | None = None, expected_rev: int | None = None, by: str | None = None) -> dict:
		t, was, clean = self._prepare_update(tid, changes, agent, expected_rev, keepalive=True)
		return self._commit_update(t, was, clean, agent, by)

	def _prepare_update(self, tid: str, changes: dict, agent: str | None = None, expected_rev: int | None = None,
	                    batch: dict | None = None, keepalive: bool = False) -> tuple[dict, str, dict]:
		"""Every check update() makes, nothing written: (the changed ticket, its old status, the clean changes). batch = the other
		items of an update_many {"blocked_by": {id: ids}, "parent": {id: parent}, "status": {id: status}}: their new values count
		for the loop, parent and open-blocker checks (CORE-51). keepalive = renew the holder's lease (update(); a batch renews after writing)."""
		batch = batch or {"blocked_by": {}, "parent": {}, "status": {}}
		t = self._fresh(tid)
		if expected_rev is not None and expected_rev != t["rev"]:
			raise Conflict(f"{tid} changed since you loaded it (rev {t['rev']}, you had {expected_rev}): reload it and apply your change again")
		clean = self._clean(changes, partial=True)
		self._holder_only(tid, agent)
		if keepalive:
			self._keepalive(tid, agent)
		if "blocked_by" in clean:
			self._check_blockers(clean["blocked_by"], t.get("blocked_by", []))
			self._check_loop(tid, clean["blocked_by"], batch["blocked_by"])
		if "parent" in clean:
			self._check_parent(tid, clean["parent"], batch["parent"])
		if "components" in clean:
			self._check_components(clean["components"], t.get("components", []))
		if "milestone" in clean:
			self._check_milestone(clean["milestone"], t.get("milestone"))
		if agent and self.profile_of(agent) and clean.get("status") in ("doing", "done", "review") and clean["status"] != t.get("status"):
			raise TicketError(f"a worker can't set status {clean['status']}: use claim_ticket / complete / ask_human")
		self._check_caps(clean, t)
		if clean.get("status") == "review" and t.get("status") != "review" and not (t.get("result") or {}).get("summary"):
			raise TicketError(_REVIEW_NEEDS_RESULT.format(id=tid))  # CORE-19: about the data, not the caller (Undo of Approve keeps its result)
		if clean.get("status") == "doing" and t.get("status") != "doing":
			if t.get("proposed_by"):
				raise TicketError(f"{tid} is a proposal by {t['proposed_by']}: accept it first")
			open_b = [b for b in clean.get("blocked_by", t.get("blocked_by", []))
			          if self._exists(b) and batch["status"].get(b, self._fresh(b).get("status")) != "done"]
			if open_b:
				raise TicketError(f"{tid} is blocked by {', '.join(open_b)} (still open): finish or unlink it first")
		was = t.get("status")
		if "waiting_on" in clean:  # by hand: age starts now, no asked_by (only ask() names an agent); cleared with the question
			if not clean["waiting_on"]:
				t["asked_at"] = t["asked_by"] = ""
			elif not t.get("asked_at"):
				t["asked_at"] = _now()
		t.update(clean)
		if t["status"] != was:
			t["done_at"] = _now() if t["status"] == "done" else ""
			if t["status"] != "done" and t.get("result"):  # leaving done (Undo of an approval, a reopen): nobody has approved this any more
				t["result"] = {k: v for k, v in t["result"].items() if k not in ("approved_by", "approved_at")}
		t["updated"] = date.today().isoformat()
		return t, was, clean

	def _commit_update(self, t: dict, was: str, clean: dict, agent: str | None = None, by: str | None = None, write: bool = True) -> dict:
		"""The side effects of an update _prepare_update checked: drop the claim of a ticket moved off doing by hand, forget the
		branches of one moved to todo, write, log done. Returns update()'s reply (with its warnings)."""
		if clean.get("status", "doing") != "doing":  # moved off doing by hand: the claim ends with it
			self._drop_claim(t["id"], f"status set to {clean['status']}")
		if clean.get("status") == "todo" and (act := [b["sha"] for b in self._board().get(t["id"], {}).get("branches", []) if b["active"]]):
			self._ignore_branches(t["id"], act)  # "it isn't being worked on": the branch that made it read as doing stops counting
		if write:
			self._write(t)
		if t["status"] == "done" and was != "done":  # by hand; an agent's complete() logs its own event
			self._log("done", t["id"], agent or (str(by).strip() if by and str(by).strip() else "human"))  # who moved it (CORE-68)
			if not (t.get("result") or {}).get("summary"):
				return {**self.get(t["id"]), "warnings": [_NO_REPORT.format(id=t["id"])]}
		got = self.get(t["id"])
		return {**got, "warnings": w} if (w := self.unroutable(got)) else got

	def update_many(self, items: list, agent: str | None = None, by: str | None = None) -> dict:
		"""Change several tickets in ONE locked, all-or-nothing step (re-prioritise, retag, set milestone / parent / components on a
		selection). items = [{"id", "changes", "expected_rev"?}]. Every item gets update()'s checks (fields, holder rule, worker
		guardrails, blockers, loops, expected_rev) with the batch's other new blocked_by / parent / status counted; any problem refuses
		the whole batch naming each one ("#3 UI-12: ..."), nothing written. A Conflict (expected_rev) anywhere = Conflict (REST 409).
		Returns {"tickets": [the updated tickets, in item order], "warnings"?: ["#N ID: ..."] (update()'s: no summary, unroutable)}
		(with lease info for an agent holding them, as update()). by = who a human change is by, as update()."""
		if not isinstance(items, list) or not items:
			raise TicketError("tickets must be a non-empty list of {id, changes} objects")
		if len(items) > UPDATE_MAX:
			raise TicketError(f"{len(items)} tickets in one batch: at most {UPDATE_MAX}; send the rest in another call")
		keys = {"id", "changes", "expected_rev"}

		def run():
			errs, conflict, seen = [], False, set()
			batch = {"blocked_by": {}, "parent": {}, "status": {}}
			for it in items:  # the batch's own new edges and statuses, for the checks below
				if isinstance(it, dict) and isinstance(it.get("id"), str) and isinstance(it.get("changes"), dict):
					for k in batch:
						if k in it["changes"]:
							batch[k][it["id"]] = it["changes"][k]
			batch["blocked_by"] = {k: v for k, v in batch["blocked_by"].items() if isinstance(v, list)}
			planned = []
			for i, it in enumerate(items, 1):
				tid = it.get("id") if isinstance(it, dict) else None
				try:
					if not isinstance(it, dict) or not isinstance(tid, str):
						raise TicketError("each item must be an object {id, changes}")
					if set(it) - keys:
						raise TicketError(f"unknown key(s) {', '.join(sorted(set(it) - keys))}: an item is {{id, changes, expected_rev?}}")
					if tid in seen:
						raise TicketError(f"{tid} is given twice in this batch: put its changes in one item")
					seen.add(tid)
					rev = it.get("expected_rev")
					if rev is not None and (isinstance(rev, bool) or not isinstance(rev, int)):
						raise TicketError("expected_rev must be the ticket's rev (a number)")
					planned.append(self._prepare_update(tid, it.get("changes"), agent, rev, batch))
				except TicketError as e:
					conflict |= isinstance(e, Conflict)
					errs.append(f"#{i}{' ' + tid if isinstance(tid, str) else ''}: {e}")
			if errs:
				msg = self._batch_refusal(errs, "changed")
				raise Conflict(msg) if conflict else TicketError(msg)
			backup = {t["id"]: self._fresh(t["id"]) for t, _, _ in planned}  # as stored: put back as is on a failed write
			done = []
			try:
				for t, _, _ in planned:
					self._write(t)
					done.append(t["id"])
			except OSError as e:  # a database connector's write is inside the lock's transaction: it rolls back on its own
				for x in done:
					self.backend.write(backup[x])
				raise TicketError(f"could not write the batch ({e}): nothing was changed")
			out, warns = [], []
			for i, (t, was, clean) in enumerate(planned, 1):
				got = self._commit_update(t, was, clean, agent, by, write=False)
				self._keepalive(t["id"], agent)
				warns += [f"#{i} {t['id']}: {w}" for w in got.get("warnings", [])]
				out.append(self._with_lease(got, agent))
			return {"tickets": out, **({"warnings": warns} if warns else {})}
		return self._locked(run)

	def _holder_only(self, tid: str, agent: str | None) -> None:
		"""An agent that is not the live holder of tid may not change, split or delete it. agent None = a human: unchanged."""
		held = self._live_claims().get(tid) if agent else None
		if held and held["agent"] != agent:
			raise TicketError(f"{tid} is claimed by {held['agent']}; only the holder can change it "
			                  "(use add_note to leave a note / ask the holder / wait for the lease to expire)")

	def _human_only(self, action: str, by: str) -> None:
		"""Worker guardrail (accident prevention, not security): a name that resolves to a configured profile (backend-1) may not
		answer / reject / accept. Boards without profiles, and names matching no profile (claude, omri, human), are unrestricted."""
		if self.profile_of(by.strip()):
			raise TicketError(f"{action} is a human action, and {by.strip()!r} is a worker: tell the human (ask_human) or add_note")

	def split(self, tid: str, titles: list[str] | None = None, sequential: bool = False, by: str | None = None,
	          parts: list[dict] | None = None, claim: int | None = None, principal: str | None = None, branch: str | None = None,
	          session: str | None = None) -> dict:
		"""Break a ticket that is too big into parts (same prefix, next free ids, parent = tid; pillar, owner, priority, tags, touches,
		docs, components, skills, mcp, milestone copied unless the part sets its own; every part inherits the parent's blocked_by;
		sequential = each also waits on the one before), and the parent now waits on all of them (a doing parent goes back to todo,
		its claim dropped). titles = the short form, one part per title (body "Split from X."). parts = [{title, done_when, touches,
		body, components, blocked_by, ...}] (CORE-50): each part is what create() takes, minus id / prefix / parent / status; its
		blocked_by may name existing ids or "#N" = the Nth part; validated like create_many items, all or nothing ("#2: ...").
		claim = k: `by` (a worker) claims part #k in the same call (checked before anything is written: it must have no open
		blocker and no human owner; branch as in claim). Returns {"parent", "children", "claimed"?: what claim() returns, "claim_error"?}."""
		if parts is not None and titles:
			raise TicketError("give titles (the short form) or parts, not both")
		if claim is not None:
			if not (isinstance(by, str) and by):
				raise TicketError("claim=k needs by / agent: the worker that takes part #k")
			if isinstance(claim, bool) or not isinstance(claim, int):
				raise TicketError(f"claim must be a part number (1-based), not {claim!r}")
			self.worker_profile(by)  # a name matching no profile is refused before anything is written, as claim() would
			humans = [o["name"] for o in self.config["owners"] if o.get("kind") == "human"]
			if principal and principal not in humans:
				raise TicketError(f"principal {principal!r} is not a human owner on this board: {', '.join(humans) or 'no human owners configured'}")

		def run():
			parent = self.get(tid)
			self._holder_only(tid, by)
			if parent["status"] == "done":
				raise TicketError(f"{tid} is already done: split a ticket that is still open")
			open_kids = [k for k in parent["children"] if self.get(k)["status"] != "done"]
			if open_kids:
				raise TicketError(f"{tid} is already split into {', '.join(open_kids)}: work those, or split one of them")
			if parts is None:
				if not (isinstance(titles, list) and all(isinstance(x, str) for x in titles)):
					raise TicketError("titles must be a list of strings, one per part")
				items = [{"title": x.strip(), "body": f"Split from {tid}."} for x in titles if x.strip()]
			elif not isinstance(parts, list):
				raise TicketError("parts must be a list of objects: [{title, done_when, touches, body, components, blocked_by: [\"#1\"]}]")
			else:
				items = parts
			if len(items) < 2:
				raise TicketError("split needs at least 2 parts: give 2 or more non-empty titles (for one part, just edit the ticket)")
			if len(items) > BATCH_MAX:
				raise TicketError(f"{len(items)} parts: at most {BATCH_MAX}")
			if claim is not None and not 1 <= claim <= len(items):
				raise TicketError(f"claim={claim} is not a part of this split (1 to {len(items)})")
			prefix = tid.split("-")[0]
			first = int(self.next_id(prefix).rsplit("-", 1)[1])
			ids = [f"{prefix}-{first + i}" for i in range(len(items))]
			keep = {k: parent[k] for k in ("pillar", "owner", "priority", "tags", "touches", "docs", "components", "skills", "mcp", "milestone") if k in parent}
			errs: list[str] = []
			def bad(i, e):
				errs.append(f"#{i}: {e}")
			def ref(x):
				if isinstance(x, str) and x.startswith("#"):
					k = int(x[1:]) if x[1:].isdigit() else 0
					if not 1 <= k <= len(items):
						raise TicketError(f"{x} is not a part of this split (#1 to #{len(items)})")
					return ids[k - 1]
				return x
			datas: list[dict | None] = []
			for i, it in enumerate(items):
				try:
					if not isinstance(it, dict):
						raise TicketError("a part must be an object {title, ...}")
					fixed = sorted(set(it) & {"id", "prefix", "parent", "status"})
					if fixed:
						raise TicketError(f"a part can't set {', '.join(fixed)}: ids come next free with {prefix}, the parent is {tid}, a part starts as todo")
					own = it.get("blocked_by", [])
					if not isinstance(own, list):
						raise TicketError("blocked_by must be a list of ids or \"#N\" (the Nth part)")
					own = [ref(x) for x in own]
					d = {**keep, "body": f"Split from {tid}.", **it, "parent": tid,
					     "blocked_by": list(dict.fromkeys(list(parent["blocked_by"]) + own + ([ids[i - 1]] if sequential and i else [])))}
					datas.append(d)
				except TicketError as e:
					bad(i + 1, e)
					datas.append(None)
			graph = {ids[i]: d["blocked_by"] for i, d in enumerate(datas) if d}
			ups = {x: tid for x in ids}
			built = []
			for i, d in enumerate(datas):
				if d is None:
					continue
				try:
					built.append(self._prepare(d, ids[i], inherited=parent["blocked_by"], inherited_caps=parent,
					                           inherited_components=parent.get("components", []), batch=(set(ids), graph, ups)))
				except TicketError as e:
					bad(i + 1, e)
			if claim is not None and not errs:  # the part to claim must be claimable once the split is written: say so now, not after
				t = built[claim - 1]
				humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
				board = self._board()
				waits = [b for b in t["blocked_by"] if b in ids or (b in board and board[b]["status"] != "done")]
				if waits:
					bad(claim, f"claim={claim} refused: that part waits on {', '.join(waits)}; claim a part with no open blocker")
				elif t["owner"] in humans:
					bad(claim, f"claim={claim} refused: that part is owned by {t['owner']} (a human)")
			if errs:
				if parts is None and len(errs) == 1:
					raise TicketError(errs[0].split(": ", 1)[1])  # the short form keeps its old one-problem messages
				errs.sort(key=lambda e: int(e[1:].split(":", 1)[0]))  # by part number
				raise TicketError(self._batch_refusal(errs, "split"))
			done = []
			try:
				for t in built:
					self._write(t)
					done.append(t["id"])
			except OSError as e:
				for x in done:  # a database connector's write is inside the lock's transaction: it rolls back on its own
					if self._exists(x):
						self.backend.delete(x)
				raise TicketError(f"could not write the parts ({e}): nothing was split")
			for t in built:
				self._log("created", t["id"], None, t["title"])
			changes = {"blocked_by": list(parent["blocked_by"]) + ids}
			if parent["status"] == "doing":
				changes["status"] = "todo"  # update() drops the claim; a doing ticket can't have open blockers
			self.update(tid, changes)
			self._log("split", tid, by, ", ".join(ids))
			out = {"parent": self.get(tid), "children": [self.get(i) for i in ids]}
			if claim is not None:
				try:
					out["claimed"] = self.claim(by, tid=ids[claim - 1], principal=principal, branch=branch, session=session)
				except TicketError as e:  # e.g. its touches overlap another live claim: the split stands
					out["claim_error"] = f"split done, but {ids[claim - 1]} could not be claimed: {e}"
			return out
		return self._locked(run)

	def delete(self, tid: str, agent: str | None = None, reason: str = "") -> dict:
		"""Remove a ticket, its claim and attachments. Returns it plus `referenced_by`: tickets still pointing at it. `reason` (a
		human rejecting a proposal) goes into the log. Rejecting a proposal is deleting it."""
		return self._locked(lambda: self._delete(tid, agent, reason))

	def accept(self, tid: str, by: str) -> dict:
		"""A human accepts an agent's proposal: proposed_by is cleared (the ticket is ready as usual), note "Accepted by X", logs `accepted`."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("accept needs `by`: who is accepting")
		self._human_only("accept", by)
		def run():
			raw = self._fresh(tid)
			if not raw.get("proposed_by"):
				raise TicketError(f"{tid} isn't a proposal (nothing to accept)")
			if raw["proposed_by"] == by.strip():
				raise TicketError(f"{by.strip()} proposed {tid} and can't accept it: someone else decides")
			who, raw["proposed_by"], raw["updated"] = raw["proposed_by"], "", date.today().isoformat()
			_append_note(raw, by.strip(), f"Accepted by {by.strip()}", kind="accepted", pinned=True)
			self._write(raw)
			self._log("accepted", tid, by.strip(), f"proposed by {who}")
		self._locked(run)
		return self.get(tid)

	def _delete(self, tid: str, agent: str | None = None, reason: str = "") -> dict:
		t = self._fresh(tid)
		self._holder_only(tid, agent)
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		if agent and self.profile_of(agent) and t.get("owner") in humans:
			raise TicketError(f"{tid} is owned by {t['owner']} (a human): a worker can't delete it; add_note or ask_human")
		refs = [x["id"] for x in self._raw().values() if tid in x.get("blocked_by", []) or x.get("parent") == tid]
		self.backend.delete(tid)  # and its attachments
		self._drop_claim(tid, "ticket deleted")
		self._log("deleted", tid, agent, t["title"] + (f" (rejected: {reason.strip()})" if reason.strip() else ""))
		return {**t, "referenced_by": refs}  # still list it in blocked_by / parent: not edited, now dangling

	# ---------- claims: which agent is working what, right now ----------
	# Runtime state, not ticket content (files: <tickets>/.unblockd/, git-ignored), so heartbeats never show up as changes in the project repo.
	def _rt(self) -> Path:  # files boards: the runtime folder, created on first use
		return self.backend.runtime()

	def _locked(self, fn):
		"""Run fn under the board lock so two agents can't claim the same ticket. Re-entrant: claim()
		holds it while next_ready() may expire stale claims, which needs it too."""
		if getattr(self, "_held", False):
			return fn()
		q = _QUEUE.setdefault(self._key, threading.RLock())
		if not q.acquire(timeout=getattr(self.backend, "busy_s", 10)):  # as long as the connector's own lock waits
			raise TicketError("another claim is in progress; try again")
		try:
			with self.backend.lock(lambda p: type(self)._steal(p)):  # looked up per call, so a test can replace Store._steal
				self._held = True
				try:
					return fn()
				finally:
					self._held = False
		finally:
			q.release()

	_steal = staticmethod(steal_stale)  # files boards: remove a stale lock file (see files.steal_stale)

	def _claims(self) -> dict:
		return self.backend.claims()

	def _save_claims(self, claims: dict) -> None:
		self.backend.save_claims(claims)

	def _log(self, event: str, tid: str | None, agent: str | None, note: str = "", principal: str | None = None) -> None:
		line = {"ts": _now(), "event": event, "ticket": tid, "agent": agent, "note": note[:500]}
		if principal:
			line["principal"] = principal
		self.backend.append_event(line, self._locked, ACTIVITY_MAX)

	def _live_claims(self) -> dict:
		"""Claims with a recent heartbeat. Expired ones are released here (ticket back to todo)."""
		claims = self._claims()
		if not claims:
			return {}
		now = time.time()
		dead = [tid for tid, c in claims.items() if now > self._expires(c)]
		if dead:
			def sweep():
				cur = self._claims()
				for tid in dead:
					c = cur.pop(tid, None)
					if c is None:
						continue
					t = self.backend.read(tid)
					mins = _fmt_min(self.lease_minutes(c.get("profile")))
					if t is not None:
						if t.get("status") == "doing":
							t["status"] = "todo"
							t["updated"] = date.today().isoformat()
							_append_note(t, c["agent"], f"claim by {c['agent']} expired (no heartbeat for {mins} min)", strict=False, kind="expired")
							self._write(t)
					self._log("expired", tid, c["agent"], f"no heartbeat for {mins} min; back to todo")
				self._save_claims(cur)
			self._locked(sweep)
			claims = self._claims()
		return {tid: {**c, "age_s": int(now - _ts(c["claimed_at"])), "quiet_s": int(now - _ts(c["heartbeat_at"]))}
		        for tid, c in claims.items()}

	def lease_minutes(self, profile: str | None) -> float:
		"""The lease of a claim by a worker of `profile`: agents[].lease_minutes, else the board's lease_minutes (AI-58)."""
		ag = next((a for a in self.config["agents"] if a["name"] == profile), None) if profile else None
		return (ag or {}).get("lease_minutes") or self.config["lease_minutes"]

	def _expires(self, c: dict) -> float:
		"""When claim c expires: its last heartbeat + its profile's lease, or the `until` a heartbeat promised, whichever is later."""
		end = _ts(c["heartbeat_at"]) + float(self.lease_minutes(c.get("profile"))) * 60
		return max(end, _ts(c["until"])) if c.get("until") else end

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

	@staticmethod
	def _on_branch(t: dict) -> list[dict]:
		"""The active branches that make a todo ticket read as doing (none when it is doing for another reason)."""
		return [b for b in t.get("branches", []) if b["active"]] if t.get("status") == "todo" and t["effective_status"] == "doing" else []

	def _claimable(self, t: dict, live: dict, by_id: dict, profile: str | None = None, explicit: bool = False, branch: str | None = None) -> bool:
		"""explicit = claimed by id: a ticket that reads as doing only because of a branch may be taken that way (to continue
		that branch's work), and so may an orphaned one (doing, nobody holds it: CORE-79); claim_next never hands either out,
		so two clones don't do the same work and a ticket a person moved to doing by hand is not grabbed.
		branch = where the claimer works (CORE-80): touches overlapping a live claim on ANOTHER known branch do not refuse (they
		only meet at merge: _overlaps reports them as a warning); same branch, or either side unknown, refuses as before."""
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		if (t["effective_status"] != "todo" and not (explicit and (self._on_branch(t) or self._orphaned(t, live)))) or t.get("owner") in humans or t["id"] in live or self._lacks(t, profile):
			return False
		return not any(not o["apart"] for o in self._overlaps(t, live, by_id, branch))

	def _where(self, tid: str, c: dict, by_id: dict) -> str | None:
		"""The branch a live claim works on: the one it named, else the one active branch the board links to its ticket (branch evidence)."""
		if c.get("branch"):
			return c["branch"]
		act = [b["name"] for b in by_id.get(tid, {}).get("branches", []) if b.get("active")]
		return act[0] if len(act) == 1 else None

	def _overlaps(self, t: dict, live: dict, by_id: dict, branch: str | None = None) -> list[dict]:
		"""Live claims whose touches overlap t's: [{id, agent, files: t's touches it meets, branch, apart}]. apart = both sides name a
		branch and they differ (two worktrees: the edits only meet at merge, CORE-80)."""
		mine, out = t.get("touches", []), []
		if not mine:
			return out
		for other, c in live.items():
			theirs = by_id.get(other, {}).get("touches", [])
			files = [a for a in mine if any(_overlap(a, b) for b in theirs)]
			if files:
				where = self._where(other, c, by_id)
				out.append({"id": other, "agent": c["agent"], "files": files, "branch": where, "apart": bool(branch and where and branch != where)})
		return out

	@staticmethod
	def _orphaned(t: dict, live: dict) -> bool:
		"""doing with no live claim (claim expired before the sweep could reset it, claims.json lost, or moved by hand) and nothing else holding it back."""
		return t.get("status") == "doing" and t["id"] not in live and not (t.get("open_blockers") or t.get("waiting_on") or t.get("proposed_by"))

	@staticmethod
	def _lacks(t: dict, profile: str | None) -> list[str]:
		"""Tools this ticket needs that a worker of `profile` reported missing (none = it may take it)."""
		return [n["tool"] for n in t.get("needs", []) if n.get("profile") == profile]

	def _unclaimable(self, t: dict, live: dict, by_id: dict, profile: str | None = None, branch: str | None = None) -> str:
		"""Why _claimable said no, in words."""
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		if (lack := self._lacks(t, profile)):
			return f"a worker of this profile has no {', '.join(lack)} (see needs): a human gives the profile the tool, then clears needs"
		if t.get("proposed_by"):
			return f"it's a proposal by {t['proposed_by']} that a human has not accepted"
		if t.get("waiting_on"):
			return f"it's waiting on a human's answer ({t['waiting_on'][:80]})"
		if t["open_blockers"]:
			return "it's blocked by " + ", ".join(b["id"] if isinstance(b, dict) else b for b in t["open_blockers"])
		if (br := self._on_branch(t)):
			return f"branch {br[0]['name']} already has work for it (last commit {br[0]['at']}): claim it by id to continue that branch"
		if t["effective_status"] != "todo":
			return f"it's {t['effective_status']}"
		if t.get("owner") in humans:
			return f"it's owned by {t['owner']} (a human)"
		for o in self._overlaps(t, live, by_id, branch):
			if not o["apart"]:
				return (f"its touches overlap {o['id']}, claimed by {o['agent']}" + (f" on branch {o['branch']}" if o["branch"] else "")
				        + f" ({', '.join(o['files'])}; that claim runs until {_iso(self._expires(live[o['id']]))} unless it heartbeats: wait_s=<seconds> waits for it)"
				        + ("" if branch else ": if you work in your own git worktree, claim with branch=<your branch> (an overlap on another branch is only a warning)"))
		return "it isn't ready"

	def worker_profile(self, agent: str) -> str | None:
		"""profile_of, but on a board with profiles a name matching none is refused (what claim and the MCP next_ready both want)."""
		profile = self.profile_of(agent)
		profiles = [a["name"] for a in self.config["agents"]]
		if profiles and profile is None:
			raise TicketError(f"worker {agent!r} matches no agent profile, so its tickets can't be routed: name workers <profile>-N "
			                  f"(profiles: {', '.join(profiles)}; e.g. {profiles[0]}-1)")  # no "pass profile=": claim_ticket / next_ready have none (API-21)
		return profile

	def claim(self, agent: str, tid: str | None = None, profile: str | None = None, note: str = "", principal: str | None = None,
	          branch: str | None = None, wait_s: float = 0, session: str | None = None) -> dict | None:
		"""See _claim. wait_s (<= WAIT_MAX, with tid only) = when tid is refused only because its touches overlap another live claim,
		look again every WAIT_POLL_S until that claim ends or the time is up (then the refusal, which names the holder and its expiry)."""
		wait_s, start = _wait_s(wait_s) if tid else 0, time.time()
		while True:
			try:
				return self._claim(agent, tid, profile, note, principal, branch, session)[0]
			except Busy:
				left = start + wait_s - time.time()
				if left <= 0:
					raise
				time.sleep(min(WAIT_POLL_S, left))

	def _claim(self, agent: str, tid: str | None = None, profile: str | None = None, note: str = "", principal: str | None = None,
	           branch: str | None = None, session: str | None = None) -> tuple:
		"""(claim()'s reply, the decorated board it read): claim_next reports why nothing was claimable from that same read.
		The step reads the board ONCE (CORE-61: it used to read every ticket 3 times, twice under the lock), on every connector:
		one backend.tickets() under the lock serves _claimable's touches, next_ready and, with the written ticket patched in,
		the reply (decorated after the lock is let go).
		Take a ticket for `agent` (a unique worker name, e.g. backend-1): the given one, or the best
		ready ticket for `profile` (default: the profile named like the agent, else any) that no live
		claim overlaps. Sets it to doing. `principal` = the human owner (kind human) the agent works for; stored in the claim.
		`branch` = the git branch the agent works on (its own worktree; CORE-80): stored in the claim; an overlap with a claim on another
		branch is allowed with a warning, and claim_next takes tickets with no overlap first. `session` = an id of the running worker process
		(stdio MCP server, UNBLOCKD_SESSION; CORE-88): stored in the claim, so a second live worker under the same name is noticed (_holder).
		Returns the ticket, or None when nothing is claimable."""
		if not (isinstance(agent, str) and NAME_RE.match(agent)):
			raise TicketError(f"bad agent name {agent!r}: lowercase a-z, 0-9, - _ (e.g. backend-1)")
		humans = [o["name"] for o in self.config["owners"] if o.get("kind") == "human"]
		principal = (principal or "").strip() or None
		if principal and principal not in humans:
			raise TicketError(f"principal {principal!r} is not a human owner on this board: {', '.join(humans) or 'no human owners configured'}")
		profiles = [a["name"] for a in self.config["agents"]]
		if profile is None:
			profile = self.worker_profile(agent)
		elif profile not in profiles:
			raise TicketError(f"unknown profile {profile!r}: {', '.join(profiles) or 'none configured'}")

		branch = (branch or "").strip() or None
		if branch and (len(branch) > 200 or any(ch.isspace() for ch in branch)):
			raise TicketError("branch is a git branch name (no spaces, <= 200 chars), e.g. core-80")
		session = _session(session)
		orphan: list[bool] = []
		meets: list[dict] = []

		def take():
			live = self._live_claims()
			raw = self.backend.tickets()  # THE read of this step (shared with the cache: never changed here)
			board = by_id = self._board(raw)  # _claimable looks up live claims' touches in it
			if tid:
				self._valid(tid)
				if tid not in board:
					raise NotFound(f"no ticket {tid}")
				t = _copy(board[tid])
				if tid in live:
					raise TicketError(f"{tid} is already claimed by {live[tid]['agent']}")
				if not self._claimable(t, live, by_id, profile, explicit=True, branch=branch):
					why = self._unclaimable(t, live, by_id, profile, branch)
					raise (Busy if why.startswith("its touches overlap") else TicketError)(f"{tid} can't be claimed: {why}")
				pick = t
				orphan.append(self._orphaned(t, live))
			else:
				ready = self.next_ready(limit=10_000, agent=profile, board=board)
				ok = (t for t in ready if self._claimable(t, live, by_id, profile, branch=branch))
				first = None
				for t in ok:  # best ticket that meets no live claim at all; else the best one that only meets claims on other branches
					if not self._overlaps(t, live, by_id):
						pick = t
						break
					first = first or t
				else:
					pick = first
				if pick is None:
					return None, board
			meets[:] = self._overlaps(pick, live, by_id, branch)
			claims = self._claims()
			now = _now()
			claims[pick["id"]] = {"agent": agent, "profile": profile, "claimed_at": now, "heartbeat_at": now, "note": note[:300],
			                      "seen": len(pick.get("notes", [])),  # inbox(): notes before the claim came with it, they are not news
				                      **({"principal": principal} if principal else {}), **({"branch": branch} if branch else {}),
			                      **({"session": session} if session else {})}
			self._save_claims(claims)
			fresh = self._fresh(pick["id"])  # one ticket, read now: the read-modify-write starts from what is stored
			fresh["status"], fresh["updated"] = "doing", date.today().isoformat()
			self._write(fresh)
			self._log("claim", pick["id"], agent, note, principal)
			return pick["id"], {**raw, pick["id"]: fresh}  # the step's read with the write in it: no second read

		got, read = self._locked(take)
		if not got:
			return None, read
		hit = _DEC.get(self._key)  # outside the lock; the claim is in claims already. Patched from the cached board (CORE-85), not stored
		board = self._derive(read, self._base(), hit[2] if hit else None)[0]
		t, c = _copy(board[got]), self._claims()[got]
		lease, exp = self.lease_minutes(c.get("profile")), self._expires(c)
		t["notes"], left = self.lean_notes(t["notes"])
		if left:
			t["notes_omitted"] = f"{left} older notes not sent (none of them pinned): get_ticket shows every note"
		warn = [f"branch {b['name']} already has {b['commits']} commit(s) for {got} (last {b['at']}{', by ' + b['author'] if b['author'] else ''}): "
		        "fetch it and build on it instead of starting over, unless it is abandoned" for b in t.get("branches", []) if b["active"]]
		for o in meets:  # only ones on another branch get this far (CORE-80)
			warn.append(f"expect a merge conflict in {', '.join(o['files'])} with {o['id']} ({o['agent']} on branch {o['branch']}): "
			            f"both edit it; merge or rebase on {o['id']}'s branch once it lands, and keep your change there small")
		if orphan and orphan[-1]:
			warn.append(f"{got} was doing with nobody holding it (a claim that expired or was lost, or a hand move): you took it over. "
			            "Read previous_attempts and the latest notes first: someone may have left half-done work")
		if not principal and len(humans) > 1:
			warn.append("no principal: this board has several human owners; pass principal=<the human you work for> so the result says who answers for it")
		near = self._doing_overlaps(t, board)
		return {**t, "lease_minutes": lease, "lease_left_s": int(lease * 60), "expires_at": _iso(exp),
		        "previous_attempts": self.attempts(got, 5), **({"overlapping": near} if near else {}), **({"warnings": warn} if warn else {})}, board

	def _doing_overlaps(self, t: dict, board: dict) -> list[dict]:
		"""CORE-47: the OTHER doing tickets (claimed, orphaned, or work on a branch) whose touches meet t's:
		[{id, title, agent (live claim's, or null), files: t's touches it meets, branch}] from a decorated board."""
		mine, out = t.get("touches", []), []
		for o in board.values() if mine else ():
			if o["id"] == t["id"] or o.get("effective_status") != "doing" or not o.get("touches"):
				continue
			files = [a for a in mine if any(_overlap(a, b) for b in o["touches"])]
			if files:
				c = o.get("claim") or {}
				act = next((b["name"] for b in o.get("branches", []) if b.get("active")), None)
				out.append({"id": o["id"], "title": o.get("title", ""), "agent": c.get("agent"), "files": files, "branch": c.get("branch") or act})
		return out

	def claim_next(self, agent: str, profile: str | None = None, note: str = "", principal: str | None = None, branch: str | None = None,
	               wait_s: float = 0, session: str | None = None) -> dict:
		"""claim() for a worker that takes whatever is next: the claimed ticket, or, when there is none, WHY, so the worker
		knows whether to retry: {"ticket": None, "reason": "dry"|"conflict"|"waiting", "counts": {...}, "ids": {same keys: [ticket ids]}, "orphaned": [doing ids with no live claim]}.
		conflict = ready tickets exist but their touches overlap a live claim (retry later; the reply adds `held_by` [{id, agent, files,
		blocks, expires_at}] = the live claims in the way and `retry_after_s` = a hint, at most 60 s and never past the first of their
		leases to end); waiting = open tickets exist but none is ready (in flight, in review, waiting on a human, blocked); dry = nothing
		open for this profile. wait_s (<= WAIT_MAX) = on conflict, look again every WAIT_POLL_S and return as soon as a ticket frees up
		(complete, release, an expired lease) or the time is up; the reply then carries `waited_s`. Other reasons return at once."""
		wait_s, start = _wait_s(wait_s), time.time()
		while True:
			got, board = self._claim(agent, profile=profile, note=note, principal=principal, branch=branch, session=session)
			res = got or self._why_empty(agent, profile, board, branch)
			left = start + wait_s - time.time()
			if got or res["reason"] != "conflict" or left <= 0:
				return {**res, "waited_s": round(time.time() - start, 1)} if wait_s else res
			time.sleep(min(WAIT_POLL_S, left))

	def _why_empty(self, agent: str, profile: str | None, board: dict, branch: str | None) -> dict:
		"""claim_next's answer when _claim found nothing, from the board that step read (see claim_next)."""
		if profile is None:
			profile = self.profile_of(agent)
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		ids: dict[str, list[str]] = {k: [] for k in ("in_flight", "ready_conflicting", "in_review", "waiting_on_human", "blocked")}
		orphaned = []
		for t in board.values():  # the read claim() made under the lock (read only, so no copies)
			if t["status"] == "done" or (profile and t["agent"] != profile):
				continue
			if t["effective_status"] == "doing":  # claimed, or work on an unmerged branch (Store._on_branch)
				k = "in_flight"
				if not t.get("claim") and t["status"] == "doing":
					orphaned.append(t["id"])  # doing with no live claim: moved by hand, or the claim was lost (claims.json is not in git)
			elif t["status"] == "review":
				k = "in_review"
			elif t.get("waiting_on") or t.get("proposed_by") or t.get("owner") in humans or self._lacks(t, profile):
				k = "waiting_on_human"  # a missing tool waits on a human too
			elif t["effective_status"] == "todo":
				k = "ready_conflicting"
			else:
				k = "blocked"
			ids[k].append(t["id"])
		counts = {k: len(v) for k, v in ids.items()}
		reason = "conflict" if counts["ready_conflicting"] else "waiting" if any(counts.values()) else "dry"
		out = {"ticket": None, "reason": reason, "counts": counts, "ids": ids, "orphaned": orphaned}
		return {**out, **self._held_by([board[i] for i in ids["ready_conflicting"]], board, branch)} if reason == "conflict" else out

	def _held_by(self, ready: list[dict], board: dict, branch: str | None = None) -> dict:
		"""For a conflict: the live claims whose touches refuse the ready tickets ({held_by: [{id, agent, files, blocks, expires_at}]}, the
		same overlap rule as _claimable, so a claim on another branch is not in the way), and retry_after_s = when to look again: the
		first of their leases to end, capped at 60 s, at least 5 (a holder that completes frees it sooner). AI-46."""
		live, held = self._live_claims(), {}
		for t in ready:
			for o in self._overlaps(t, live, board, branch):
				if not o["apart"]:
					h = held.setdefault(o["id"], {"id": o["id"], "agent": o["agent"], "files": [], "blocks": [],
					                              "expires_at": _iso(self._expires(live[o["id"]]))})
					h["blocks"].append(t["id"])
					h["files"] += [f for f in o["files"] if f not in h["files"]]
		soonest = min((self._expires(live[i]) - time.time() for i in held), default=60)
		return {"held_by": [held[i] for i in sorted(held)], "retry_after_s": int(max(5, min(60, soonest)))}

	def inbox(self, agent: str, mark: bool = False) -> dict | None:
		"""Notes others left on the tickets `agent` holds that it has not been shown yet: {count, notes: [{ticket, n, by, kind, at,
		text, reply_to?, truncated?}], truncated?} (oldest first, the last INBOX_NOTES), or None. A note on a ticket you hold used to wait
		until you re-read the ticket, so a "please add these 3 lines to your file" request took a human 3 hops. Each note's whole text
		comes along up to INBOX_NOTE_MAX chars, all of them up to INBOX_MAX (the newest get the room first); a note cut short has
		`truncated: true` (and so has the inbox): only then is get_ticket needed. Seen = the claim's `seen` (note count when last shown;
		a claim older than that field: notes newer than claimed_at). mark=True (the heartbeat front doors) records what was shown, so each note comes
		once. Its own mark, not heartbeat_at: the worker's own add_note renews the lease but has not read anyone's note."""
		mine = {tid: c for tid, c in self._live_claims().items() if c["agent"] == agent}
		out, seen = [], {}
		for tid, c in mine.items():
			notes = self.get(tid).get("notes", [])
			seen[tid] = len(notes)
			out += self._unseen(tid, notes, c, agent)
		if mark and mine:
			def save():
				claims = self._claims()
				for tid, k in seen.items():
					if tid in claims and claims[tid]["agent"] == agent:
						claims[tid]["seen"] = k
				self._save_claims(claims)
			self._locked(save)
		return self._pack_inbox(out)

	@staticmethod
	def _pack_inbox(items: list[dict]) -> dict | None:
		"""{count, notes, truncated?} from inbox items: the last INBOX_NOTES, each text cut at INBOX_NOTE_MAX, all of them at INBOX_MAX."""
		if not items:
			return None
		keep, room, cut = items[-INBOX_NOTES:], INBOX_MAX, False
		for it in reversed(keep):  # newest first: the latest request is the one to act on
			cap = min(INBOX_NOTE_MAX, room)
			if len(it["text"]) > cap:
				it["text"], it["truncated"], cut = it["text"][:cap], True, True
			room -= len(it["text"])
		return {"count": len(items), "notes": keep, **({"truncated": True, "hint": "a note was cut short: get_ticket(id) shows it whole"} if cut else {})}

	@staticmethod
	def _unseen(tid: str, notes: list[dict], c: dict, agent: str) -> list[dict]:
		"""Notes by others on tid that claim c has not been shown (its `seen` count; an older claim: notes after claimed_at), whole text
		(_pack_inbox caps it)."""
		first, out = c.get("seen"), []
		for i, n in enumerate(notes):
			new = i >= first if isinstance(first, int) else _ts(n.get("at") or "1970-01-01T00:00:00+00:00") > _ts(c["claimed_at"])
			if new and n.get("by") != agent:
				out.append({"ticket": tid, "n": n.get("n", i + 1), "by": n.get("by"), "kind": n.get("kind"), "at": n.get("at"),
				            "text": (n.get("text") or "").strip(), **({"reply_to": n["reply_to"]} if n.get("reply_to") else {})})
		return out

	def _with_unread(self, t: dict, late: list[dict], what: str) -> dict:
		"""complete / release / ask reply plus `inbox` with the notes others left that the worker had not been shown when its claim
		ended (a note between its last heartbeat and this call was otherwise never read), flagged unread_before_<what> (AI-50)."""
		box = self._pack_inbox(late)
		return {**t, "inbox": {**box, f"unread_before_{what}": True}} if box else t

	def late_notes(self, t: dict) -> list[dict]:
		"""Notes that arrived after the work was finished, for the human who looks next (AI-50). A ticket in review: every note
		after its last `complete` note not by the completer, plus the ones the worker had not read when it completed (`unread: true`,
		from result.unread). A done ticket: a PERSON's note (add_note kinds; is_agent false) written after done_at (old tickets
		without it: after the last complete / approved note) that nobody dismissed. Anything else: []."""
		notes, res = t.get("notes", []), t.get("result") or {}
		kinds = NOTE_KINDS_ADD
		if t.get("status") == "review":
			unread = set(res.get("unread") or [])
			end = max((i for i, n in enumerate(notes) if n.get("kind") == "complete"), default=None)
			out = []
			for i, n in enumerate(notes):
				if n.get("kind") not in kinds or n.get("by") == res.get("by"):
					continue
				if n.get("n") in unread:
					out.append({**n, "unread": True})
				elif end is not None and i > end:
					out.append(dict(n))
			return out
		if t.get("status") != "done":
			return []
		done_at = _ts(t["done_at"]) if t.get("done_at") else None
		end = max((i for i, n in enumerate(notes) if n.get("kind") in ("complete", "approved")), default=None)
		def after(i, n):  # done_at has whole seconds: a note in the same second counts when it comes after the finishing note
			if done_at is None:
				return end is not None and i > end
			at = _ts(n.get("at") or "1970-01-01T00:00:00+00:00")
			return at > done_at or (at == done_at and (end is None or i > end))
		return [dict(n) for i, n in enumerate(notes)
		        if n.get("kind") in kinds and not n.get("dismissed_by") and after(i, n)
		        and not self.is_agent(n.get("by") or "", t["id"])]  # last: it reads claims.json

	def dismiss_notes(self, tid: str, by: str, ns: list[int], undo: bool = False) -> dict:
		"""A person read the late notes `ns` (note numbers) on a finished ticket and nothing needs doing: each gets `dismissed_by` /
		`dismissed_at`, so Needs you stops listing it (undo=True takes that back). The notes themselves are kept as written."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("dismiss needs `by`: who read the notes")
		self._human_only("dismiss", by)
		if not (isinstance(ns, list) and ns and all(isinstance(x, int) and not isinstance(x, bool) for x in ns)):
			raise TicketError("dismiss needs `notes`: a list of note numbers, e.g. [4, 5]")
		def run():
			raw = self._fresh(tid)
			notes = raw.get("notes", [])
			bad = [x for x in ns if not 1 <= x <= len(notes)]
			if bad:
				raise TicketError(f"{tid} has notes 1-{len(notes)}, not {', '.join(map(str, bad))}")
			for x in ns:
				if undo:
					notes[x - 1].pop("dismissed_by", None)
					notes[x - 1].pop("dismissed_at", None)
				else:
					notes[x - 1].update(dismissed_by=by.strip(), dismissed_at=_now())
			self._write(raw)
			self._log("dismissed", tid, by.strip(), ("undo " if undo else "") + "notes " + ", ".join(map(str, ns)))
		self._locked(run)
		return self.get(tid)

	def attempts(self, tid: str, limit: int | None = None) -> list[dict]:
		"""Failed claims on a ticket, newest first, from the activity log: [{agent, outcome: expired|released|rejected, at, note}]."""
		out = [{"agent": e["agent"], "outcome": OUTCOMES[e["event"]], "at": e["ts"], "note": e["note"]}
		       for e in self.activity(tid, limit=1_000_000) if e["event"] in OUTCOMES]
		return out[:limit]

	def failing(self, n: int = 3) -> dict[str, list[dict]]:
		"""Tickets with >= n failed attempts (expired / released / rejected) since the last human touch: {id: attempts, newest first}.
		Human touch = an `answered` event, or a `note` by someone who is not an agent. Agent = a name that resolves to a configured
		profile (profile_of) or that has claimed that ticket; so a board with no profiles still tells a person from a worker."""
		events = self.activity(limit=1_000_000)  # newest first
		claimers = {(e["ticket"], e["agent"]) for e in events if e["event"] == "claim"}
		out, closed = {}, set()
		for e in events:
			tid = e["ticket"]
			if tid in closed:
				continue
			if e["event"] == "answered" or (e["event"] == "note" and not self.profile_of(e["agent"] or "") and (tid, e["agent"]) not in claimers):
				closed.add(tid)
			elif e["event"] in OUTCOMES:
				out.setdefault(tid, []).append({"agent": e["agent"], "outcome": OUTCOMES[e["event"]], "at": e["ts"], "note": e["note"]})
		return {t: a for t, a in out.items() if len(a) >= n}

	def needs(self, prs: dict | None = None) -> list[dict]:
		"""The open tickets that wait on a person, the Needs you panel's items (REST GET /api/needs via Boards.needs, MCP needs_you):
		an agent's question, work in review, a proposal, work stuck (a doing ticket with an open blocker, untouched for stale_days, a
		blocker that no longer exists, a missing tool, or >= 3 failed attempts) and a person's note on finished work (late_note, AI-50;
		review items carry `late` too). One kind each, in that precedence: late_note (done tickets only), review, proposal, question, stuck. Sorted by score, then oldest first. list() may sweep expired claims, like the notifier does.
		prs = the PR cache already read (web's /api/needs keys its cache on it); None = read it."""
		from collections import Counter
		ts = self.list()
		items, by_id, failing = [], {t["id"]: t for t in ts}, self.failing()
		prs = self.backend.get_doc("prs") if prs is None else prs  # github.py's PR cache (any connector); web passes the one it keyed on
		for t in ts:
			tries = []
			late = self.late_notes(t) if t["status"] in ("review", "done") else []
			if t["status"] == "done":
				if not late:
					continue
				kind, at = "late_note", late[-1].get("at")  # a person wrote on finished work: reopen it or dismiss the note
			elif t["status"] == "review":
				kind, at = "review", (t.get("result") or {}).get("at")
			elif t.get("proposed_by"):
				kind, at = "proposal", None
			elif t.get("waiting_on") and t.get("asked_by"):  # a hand-set waiting_on (no asked_by) is parked, not a question
				kind, at = "question", t.get("asked_at")
			elif t.get("needs") or t["missing_blockers"] or (t["status"] == "doing" and t["open_blockers"]) or (t["stale_days"] is not None and t["stale_days"] >= self.stale_days) or (not t.get("claim") and t["id"] in failing):
				kind, at = "stuck", None
				tries = [] if t.get("claim") else failing.get(t["id"], [])
			else:
				continue
			stuck = kind == "stuck"
			outcomes = Counter(a["outcome"] for a in tries) if stuck else {}
			items.append({"id": t["id"], "kind": kind, "title": t["title"], "priority": t.get("priority"), "score": t["score"],
			              "unblocks": t["unblocks"], "since": at or str(t.get("updated"))[:10] + "T00:00:00",
			              "waiting_on": t.get("waiting_on", ""), "missing_blockers": t["missing_blockers"],
			              "stale_days": t["stale_days"] if t["stale_days"] is not None and t["stale_days"] >= self.stale_days else None, "done_when": t.get("done_when", []), "result": t.get("result"),
			              "body": (t.get("body") or "")[-NEEDS_BODY:], "notes": t.get("notes", [])[-NEEDS_NOTES:],
			              **({"proposed_by": t["proposed_by"]} if kind == "proposal" else {}),
			              **({"late": late} if late else {}),
			              **({"needs": t["needs"]} if stuck and t.get("needs") else {}),
			              **({"pr": {k: v for k, v in prs[t["id"]].items() if k != "noted"}} if kind == "review" and t["id"] in prs else {}),
			              **({"open_blockers": [{"id": b, "effective_status": by_id[b]["effective_status"], "title": by_id[b]["title"]} for b in t["open_blockers"]],
			                  "claim_note": ({"by": c["agent"], "at": c["heartbeat_at"], "text": c["note"]} if (c := t.get("claim")) and c.get("note") else None),
			                  "attempts": tries, "failed": f"Failed {len(tries)} times ({', '.join(f'{k} ×{v}' for k, v in outcomes.items())})" if tries else ""} if stuck else {})})
		items.sort(key=lambda i: (-i["score"], i["since"]))
		return items

	def _keepalive(self, tid: str, agent: str | None) -> None:
		"""Any write that names the live holder of tid counts as a heartbeat (agents can't ping during a long test run). Caller holds the lock."""
		if agent and (c := self._live_claims().get(tid)) and c["agent"] == agent:
			claims = self._claims()
			claims[tid]["heartbeat_at"] = _now()
			self._save_claims(claims)

	def _lapsed(self, tid: str, agent: str) -> dict | None:
		"""The `claim` event of `agent`'s last claim on tid when that claim EXPIRED and nothing ended or re-took it since
		(no other claim, release, complete, question, drop); else None. Read from the activity log, newest first."""
		expired = False
		for e in self.activity(tid, limit=1_000_000):
			ev = e["event"]
			if ev == "expired":
				if e["agent"] != agent or expired:
					return None
				expired = True
			elif ev == "claim":
				return e if expired and e["agent"] == agent else None
			elif ev in ("release", "complete", "question", "dropped", "rejected", "approved", "done", "deleted", "split"):
				return None
		return None

	def lease_status(self, tid: str, agent: str | None) -> dict:
		"""What a reply to `agent` about tid adds: {lease_left_s} while it holds the live claim (plus a warning under LEASE_WARN_S),
		a warning when its claim lapsed or someone else holds it, nothing for a caller with no claim history (a human)."""
		if not agent:
			return {}
		c = self._live_claims().get(tid)
		if c and c["agent"] == agent:
			exp = self._expires(c)
			left = max(0, int(exp - time.time()))
			warn = [f"your lease on {tid} ends in {left} s: call heartbeat now, or the ticket goes back to todo"] if left < LEASE_WARN_S else []
			return {"lease_left_s": left, "expires_at": _iso(exp), **({"warnings": warn} if warn else {})}
		if c:
			return {"warnings": [f"you don't hold {tid}: {c['agent']} does"]}
		if self._lapsed(tid, agent):
			return {"warnings": [f"you no longer hold {tid}: your claim expired (no heartbeat for {_fmt_min(self.lease_minutes(self.profile_of(agent)))} min). "
			                     "heartbeat or complete takes it back while nobody else has claimed it"]}
		return {}

	def _retake(self, tid: str, agent: str, principal: str | None, session: str | None = None) -> dict | None:
		"""Give `agent` back the claim it lost to the lease when nobody claimed tid since and it is still claimable. Caller holds the lock.
		The old claim's session does not count (CORE-88: a restarted worker has a new one and must recover its own ticket); the new claim
		gets the caller's. Returns the new claim, or None."""
		ev = self._lapsed(tid, agent)
		if not ev:
			return None
		live = self._live_claims()
		profile = self.profile_of(agent)
		t = self.get(tid)
		if not self._claimable(t, live, self._raw(), profile):
			return None
		principal = principal or ev.get("principal")
		claims, now = self._claims(), _now()
		claims[tid] = {"agent": agent, "profile": profile, "claimed_at": now, "heartbeat_at": now, "note": "taken back after the lease lapsed",
		               **({"principal": principal} if principal else {}), **({"session": session} if session else {})}
		self._save_claims(claims)
		raw = self._fresh(tid)
		raw["status"], raw["updated"] = "doing", date.today().isoformat()
		self._write(raw)
		self._log("claim", tid, agent, "taken back after the lease lapsed (nobody claimed it since)", principal)
		return {**claims[tid], "retaken": True}

	def _holder(self, tid: str, agent: str, principal: str | None = None, retake: bool = False, session: str | None = None) -> dict:
		"""The live claim on tid when `agent` (for `principal`) holds it, else a TicketError saying why. Caller holds the lock.
		A claim made by another session of the same name (CORE-88) comes back with `clash` = the warning (or, strict_sessions, is refused)."""
		session = _session(session)
		c = self._live_claims().get(tid)
		if not c and retake:
			c = self._retake(tid, agent, principal, session)
		if not c:
			if self._lapsed(tid, agent):
				t = self.get(tid)
				why = self._unclaimable(t, self._live_claims(), self._raw(), self.profile_of(agent))
				raise TicketError(f"you no longer hold {tid}: your claim expired (no heartbeat for {_fmt_min(self.lease_minutes(self.profile_of(agent)))} min) "
				                  f"and it can't be taken back: {why}")
			raise TicketError(f"{tid} isn't claimed (or the claim expired): claim it again")
		if c["agent"] != agent:
			raise TicketError(f"{tid} is claimed by {c['agent']}, not {agent}")
		if principal and c.get("principal") and c["principal"] != principal:  # same worker name, another human's worker
			raise TicketError(f"{tid} is claimed by {agent} for {c['principal']}, not for {principal}")
		if session and c.get("session") and c["session"] != session:  # both sides named one, and they differ: two live workers, one name
			c = {**c, "clash": self._clash(tid, agent, c, session)}
		return c

	def _clash(self, tid: str, agent: str, c: dict, session: str) -> str:
		"""CORE-88: `agent` calls about tid from another session than the one that claimed it. strict_sessions: refused. Else logs
		`session_clash`, marks the claim (`clash_at`, shown by agents_now) and returns the warning; the call itself goes through.
		Caller holds the lock."""
		name = f"{c.get('profile') or agent}-<n>"
		if self.config["strict_sessions"]:
			raise TicketError(f"{tid} is held by another session of {agent} (two workers running under one name?): use your own worker name "
			                  f"({name}) and claim your own ticket. If you ARE the worker that claimed it, restarted: wait until its lease lapses "
			                  f"({_fmt_min(self.lease_minutes(c.get('profile')))} min with no heartbeat), then heartbeat or complete takes it back")
		self._log("session_clash", tid, agent, f"session {session[:40]} called; the claim is session {str(c['session'])[:40]}")
		claims = self._claims()
		if tid in claims:
			claims[tid]["clash_at"] = _now()
			self._save_claims(claims)
		return (f"{agent} is another session here: {tid} was claimed by a different running worker named {agent}, so each of you keeps the "
		        f"other's claim alive and either can finish it. Stop and pick another name ({name}) unless you are that worker, restarted")

	def heartbeat(self, tid: str, agent: str, note: str = "", principal: str | None = None, until: str | None = None,
	              session: str | None = None) -> dict:
		"""Keep a claim alive and say what you're doing now (shown live on the board). until = an ISO time: the claim stays
		alive until then even without another heartbeat (a long build or render), capped at lease_max_minutes from now (AI-58)."""
		cap_warn = []
		end = None
		if until:
			try:
				end = _ts(until)
			except (TypeError, ValueError):
				raise TicketError(f"until must be an ISO time like 2026-11-21T18:30:00+00:00 (got {str(until)[:40]!r})")
			now, mx = time.time(), float(self.config["lease_max_minutes"])
			if end <= now:
				raise TicketError(f"until {until} is in the past: give the time your run ends (UTC ISO), or leave it out")
			if end > now + mx * 60:
				end = now + mx * 60
				cap_warn = [f"until capped to {_iso(end)}: this board allows at most {_fmt_min(mx)} min ahead (lease_max_minutes); heartbeat again before then"]

		def beat():
			c = self._holder(tid, agent, principal, retake=True, session=session)
			if c.get("clash"):
				cap_warn.insert(0, c["clash"])
			claims = self._claims()
			claims[tid]["heartbeat_at"] = _now()
			if end is not None:
				claims[tid]["until"] = _iso(end)
			if note:
				claims[tid]["note"] = note[:300]
			self._save_claims(claims)
			self._log("heartbeat", tid, agent, note)
			return c.get("retaken")
		retaken = self._locked(beat)
		out = self._with_lease(self.get(tid), agent, retaken)
		return {**out, "warnings": out.get("warnings", []) + cap_warn} if cap_warn else out

	def _with_lease(self, t: dict, agent: str | None, retaken: bool = False) -> dict:
		"""t plus lease_status(t, agent), and a warning when the claim was just taken back."""
		info = self.lease_status(t["id"], agent)
		warn = ([f"your claim on {t['id']} had expired; nobody claimed it since, so it is yours again. Heartbeat more often"] if retaken else []) + info.pop("warnings", [])
		return {**t, **info, **({"warnings": t.get("warnings", []) + warn} if warn else {})}

	def release(self, tid: str, agent: str, reason: str = "", principal: str | None = None, missing: str = "", why: str = "",
	            request_id: str | None = None, session: str | None = None) -> dict:
		"""Give a claimed ticket back: claim ends, ticket returns to todo. missing = a tool the worker needs and does not have (why = one
		line on what it needed it for): recorded in `needs` with the worker's profile, as a pinned warning note, shown in Needs you; workers
		of that profile skip the ticket until a human clears `needs`."""
		missing, why = (missing or "").strip(), (why or "").strip()
		if missing and not why:
			raise TicketError("release(missing=...) needs `why`: one line on what you needed the tool for")
		if len(missing) > 100 or len(why) > 300:
			raise TicketError("missing is a tool name (<= 100 chars), why one line (<= 300)")
		def rel():
			if (seen := self._seen_request(agent, request_id, "release", tid) or self._repeat(tid, agent, "release")):
				return seen
			c = self._holder(tid, agent, principal, session=session)
			clash[:] = [c["clash"]] if c.get("clash") else []
			claims = self._claims()
			claims.pop(tid, None)
			self._save_claims(claims)
			raw = self._fresh(tid)
			late[:] = self._unseen(tid, raw.get("notes", []), c, agent)
			if raw.get("status") == "doing":
				raw["status"], raw["updated"] = "todo", date.today().isoformat()
			if reason.strip():
				_append_note(raw, agent, reason.strip(), strict=False, kind="released")
			if missing:
				prof = c.get("profile") or self.profile_of(agent)
				raw["needs"] = [n for n in raw.get("needs", []) if not (n["tool"] == missing and n.get("profile") == prof)] + \
				               [{"tool": missing, "why": why, "profile": prof, "by": agent, "at": _now()}]
				_append_note(raw, agent, f"Needs tool `{missing}`: {why}", strict=False, kind="warning", pinned=True)
			self._write(raw)
			self._log("release", tid, agent, reason + (f" (missing tool {missing})" if missing else ""), c.get("principal"))
			self._record_request(agent, request_id, "release", tid)
		late: list[dict] = []
		clash: list[str] = []
		seen = self._locked(rel)
		if seen:
			return self._already(seen)
		out = self._with_unread(self.get(tid), late, "release")
		if clash:
			out["warnings"] = clash + out.get("warnings", [])
		waits = [x for x in dict.fromkeys(_ID_TOKEN.findall(reason or "")) if x != tid and self._exists(x)]
		if waits:  # AI-60: "waiting on CORE-68" is usually half done, half waiting; release records nothing of the done half
			out["hint"] = (f"if part of {tid} is done and the rest waits on {', '.join(waits)}: claim it again and "
			               f"complete({tid}, summary, followup={{title, done_when, blocked_by: {waits}}}) records the done part "
			               f"and makes the rest its own ticket, ready by itself once {', '.join(waits)} is done")
		if self._names_human(reason):  # AI-56: a release counts as a failed attempt, and the next worker hits the same question
			ask = (f"if {tid} waits on a decision only a human can make: ask_human({tid}, agent, question) parks it with the question "
			       f"until it is answered (no claim needed on a todo ticket); a release counts as a failed attempt and the next worker hits the same wall")
			out["hint"] = f"{out['hint']}. {ask}" if out.get("hint") else ask
		return out

	def _names_human(self, text: str) -> bool:
		"""text (a release reason) names a person or a decision: a word of _HUMAN_WORDS or a kind-human owner's name."""
		if not (text or "").strip():
			return False
		if _HUMAN_WORDS.search(text):
			return True
		names = [o["name"] for o in self.config["owners"] if o.get("kind") == "human"]
		return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.I) for n in names if n.strip())

	def ask(self, tid: str, agent: str, question: str, request_id: str | None = None) -> dict:
		"""Hand a decision to a human: the claim ends, waiting_on = question, status todo. A todo with a waiting_on is
		pending (not ready, not claimable) until someone clears it. Works on a ticket you hold, or on an unclaimed todo with no
		question yet (no claim needed, AI-56); a ticket someone else holds is refused."""
		q = question.strip() if isinstance(question, str) else ""
		if not q:
			raise TicketError("ask needs a question: say what the human must decide")
		if len(q) > QUESTION_MAX:
			raise TicketError(f"question is {len(q)} chars, the limit is {QUESTION_MAX}: shorten it, or put the background in add_note and ask the decision")
		if not (agent or "").strip():
			raise TicketError("ask needs who is asking: pass agent (your worker name), so the human sees who the question is from")
		def run():
			if (seen := self._seen_request(agent, request_id, "ask", tid)):
				return seen
			if tid not in self._claims() and (cur := self._fresh(tid)) and cur.get("waiting_on") == q and cur.get("asked_by") == agent:
				return {"already": True, "ticket": tid, "at": cur.get("asked_at", "")}  # the same question again: it is already asked
			raw = self._fresh(tid)
			if tid in self._live_claims() or raw.get("status") != "todo":
				c = self._holder(tid, agent)  # claimed by someone else, or doing / review / done: refused as before
			elif raw.get("waiting_on"):
				raise TicketError(f"{tid} already waits on a human: {raw['waiting_on'][:200]!r}. add_note to add to it, or wait for the answer")
			else:  # AI-56: an unclaimed todo can be asked about without a claim (a lead used to claim it, 24k chars of context, only to ask)
				c = {}
			late[:] = self._unseen(tid, raw.get("notes", []), c, agent) if c else []
			raw.update(status="todo", waiting_on=q, asked_at=_now(), asked_by=agent, updated=date.today().isoformat())
			_append_note(raw, agent, q, kind="question")  # refuses before the claim is touched
			claims = self._claims()
			claims.pop(tid, None)
			self._save_claims(claims)
			self._write(raw)
			self._log("question", tid, agent, q)
			self._record_request(agent, request_id, "ask", tid)
		late: list[dict] = []
		seen = self._locked(run)
		return self._already(seen) if seen else self._with_unread(self.get(tid), late, "ask")

	def add_note(self, tid: str, by: str, text: str, kind: str = "note", pin: bool = False, reply_to: int | None = None,
	             supersedes: int | None = None, request_id: str | None = None) -> dict:
		"""Append a signed, timestamped note under the lock: it can't clobber a concurrent complete or answer
		(update(body=...) replaces the whole body; use this to report progress). Notes are numbered (`n`, from 1).
		kind: note (progress) | decision (a choice and why) | finding (a cause, an error, a fact found) | warning (don't do X: it breaks Y).
		Pinned = always sent to the next worker: decision / finding / warning, a note by a human, or pin=True.
		reply_to = the `n` this note answers (it is then always sent along with it); supersedes = the `n` of a pinned note this one
		replaces (a reversed decision): that one stays in the history but is no longer pinned."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("add_note needs `by`: who is writing (your worker name, or a person's name)")
		if not (isinstance(text, str) and text.strip()):
			raise TicketError("add_note needs text: what to record")
		if kind == "progress":  # the protocol says "report progress with add_note": take the word agents reach for
			kind = "note"
		if kind not in NOTE_KINDS_ADD:
			raise TicketError(f"note kind must be one of {', '.join(NOTE_KINDS_ADD)} (progress = note)")
		def run():
			if (seen := self._seen_request(by.strip(), request_id, "note", tid)):
				return seen
			raw = self._fresh(tid)
			notes, who = raw.get("notes", []), by.strip()
			for ref, what in ((reply_to, "reply_to"), (supersedes, "supersedes")):
				if ref is not None and not (isinstance(ref, int) and 1 <= ref <= len(notes)):
					raise TicketError(f"{what} must be a note number of {tid} (1-{len(notes)}), got {ref!r}")
			extra = {k: v for k, v in (("reply_to", reply_to), ("supersedes", supersedes)) if v is not None}
			if kind in PINNED_KINDS or pin or not self.is_agent(who, tid):
				extra["pinned"] = True
			_append_note(raw, who, text.strip(), kind=kind, **extra)
			if supersedes is not None:
				notes[supersedes - 1]["superseded_by"] = len(notes)
			raw["updated"] = date.today().isoformat()
			self._write(raw)
			self._keepalive(tid, by.strip())
			self._log("note", tid, by.strip(), text.strip())
			self._record_request(by.strip(), request_id, "note", tid)
		seen = self._locked(run)
		return self._with_lease(self._already(seen) if seen else self.get(tid), by.strip())

	def answer(self, tid: str, by: str, text: str) -> dict:
		"""A human answers an ask(): appends "Answer to: <question> — <text>" as a note, clears waiting_on (the ticket is ready again), logs `answered`."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("answer needs `by`: who is answering")
		self._human_only("answer", by)
		if not (isinstance(text, str) and text.strip()):
			raise TicketError("answer needs text: what you decide")
		def run():
			raw = self._fresh(tid)
			if not raw.get("waiting_on"):
				raise TicketError(f"{tid} isn't waiting on anyone (no question to answer): use add_note to comment")
			if raw.get("asked_by") == by.strip():
				raise TicketError(f"{by.strip()} asked this question and can't answer it: someone else decides")
			_append_note(raw, by.strip(), text.strip(), kind="answer", q=raw["waiting_on"], pinned=True)
			raw["waiting_on"], raw["asked_at"], raw["asked_by"], raw["updated"] = "", "", "", date.today().isoformat()
			self._write(raw)
			self._log("answered", tid, by, text.strip())
		self._locked(run)
		return self.get(tid)

	def approve(self, tid: str, by: str) -> dict:
		"""A human approves work in review: status done, `result.approved_by` / `approved_at` (a plain update to done leaves them empty:
		nobody approved), note "Approved by X", logs `approved`. Refused unless the status is review, for a worker, and for the completer."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("approve needs `by`: who is approving")
		self._human_only("approve", by)
		by = by.strip()
		def run():
			raw = self._fresh(tid)
			if raw.get("status") != "review":
				raise TicketError(f"{tid} is {raw.get('status')}, not review: only a ticket waiting for review can be approved")
			res = raw.get("result")  # none = moved to review by hand: the note and the log record the approver, no result is invented
			if res and res.get("by") == by:
				raise TicketError(f"{by} completed {tid} and can't approve it: someone else decides")
			_append_note(raw, by, f"Approved by {by}", kind="approved", pinned=True)
			if res:
				raw["result"] = {**res, "approved_by": by, "approved_at": _now()}
			res = res or {}
			raw["status"], raw["done_at"], raw["updated"] = "done", _now(), date.today().isoformat()
			self._write(raw)
			self._log("approved", tid, by, res.get("summary", ""), res.get("principal"))
		self._locked(run)
		return self.get(tid)

	def reject(self, tid: str, by: str, reason: str) -> dict:
		"""A human sends a ticket in review back: status todo, "Rejected: <reason>" appended as a note, logs `rejected`."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("reject needs `by`: who is rejecting")
		self._human_only("reject", by)
		if not (isinstance(reason, str) and reason.strip()):
			raise TicketError("reject needs a reason: say what is wrong, the worker reads it before redoing the ticket")
		def run():
			raw = self._fresh(tid)
			if raw.get("status") != "review":
				raise TicketError(f"{tid} is {raw.get('status')}, not review: only a ticket waiting for review can be rejected")
			if (raw.get("result") or {}).get("by") == by.strip():
				raise TicketError(f"{by.strip()} completed {tid} and can't reject it: someone else decides")
			_append_note(raw, by.strip(), reason.strip(), kind="rejected", pinned=True)
			raw["status"], raw["updated"] = "todo", date.today().isoformat()
			raw.pop("result", None)  # rejected work must not show as upstream
			self._write(raw)
			self._log("rejected", tid, by, reason.strip())
		self._locked(run)
		return self.get(tid)

	def complete(self, tid: str, agent: str, summary: str, files: list[str] | None = None,
	             refs: list[str] | None = None, handoff: str | None = None, principal: str | None = None, request_id: str | None = None,
	             followup: dict | None = None, session: str | None = None, closes: list[str] | None = None,
	             touched: list[str] | None = None) -> dict:
		"""Finish a claimed ticket: status done (or review when the board's `review` is on), summary appended to the body (dated, signed).
		Also stored as `result` {summary, files, refs, handoff, by, at}: files = paths changed, refs = PR / commit / URL, handoff = what a
		downstream ticket must know. `result.principal` = the human the claim was made for (copied from the claim). Tickets that wait on this one get it in their context() as `upstream`.
		followup = {title, done_when?, blocked_by?, body?, priority?, touches?} (AI-60): the part that could not be done yet becomes its own
		ticket in the same step (same prefix, parent, pillar, owner, tags, touches, docs, components; body "Rest of <id>."), waiting on
		blocked_by, so it turns ready by itself when they are done. All or nothing: a bad followup refuses the complete too.
		closes = ids of other open tickets whose work landed in this one (AI-57): each goes to the same status (done, or review on a review
		board) with a note and result "Done under <id>: <summary>"; all or nothing (a closed, missing, claimed-by-another ticket refuses the
		complete too). result.closes lists them, each one's result.under = this id.
		touched = the paths the work really edited (CORE-47): stored as result.touched; the ones outside the ticket's touches as
		result.outside, and the reply warns about them and about any other doing ticket whose touches meet one of them."""
		_check_result_input("complete", summary, files, refs, handoff)
		closes = _closes_input(tid, closes)
		if touched is not None:
			_check_result_input("complete", summary, touched, None, None)
			touched = list(dict.fromkeys(x.strip() for x in touched if x.strip()))
		result = {"summary": summary.strip(), "files": [x.strip() for x in files or []], "refs": [x.strip() for x in refs or []],
		          "handoff": (handoff or "").strip(), "by": agent, "at": _now()}
		def done():
			if (seen := self._seen_request(agent, request_id, "complete", tid) or self._repeat(tid, agent, "complete")):
				return seen
			c = self._holder(tid, agent, principal, retake=True, session=session)
			clash[:] = [c["clash"]] if c.get("clash") else []
			if c.get("principal"):
				result["principal"] = c["principal"]
			raw = self._fresh(tid)
			fu = self._followup(raw, followup, agent) if followup is not None else None  # validated before anything changes
			if fu:
				result["followup"] = fu["id"]
			shut = self._closes(tid, closes, agent) if closes else []  # validated before anything changes too
			if shut:
				result["closes"] = [x["id"] for x in shut]
			if touched:
				result["touched"] = touched
				if (extra := [x for x in touched if not any(_overlap(x, b) for b in raw.get("touches", []))]):
					result["outside"] = extra
			late[:] = self._unseen(tid, raw.get("notes", []), c, agent)
			if late:  # the reviewer is shown them next to the result: the worker finished without reading them
				result["unread"] = [n["n"] for n in late if isinstance(n.get("n"), int)]
			status = "review" if self.config["review"] else "done"
			bar = {"done_when": raw["done_when"]} if raw.get("done_when") else {}  # the bar it was finished against, next to the summary
			_append_note(raw, agent, summary.strip(), kind="complete", status=status, **bar)  # refuses before the claim is touched
			claims = self._claims()
			claims.pop(tid, None)
			self._save_claims(claims)
			raw["status"], raw["updated"], raw["result"] = status, date.today().isoformat(), result
			raw["done_at"] = _now() if status == "done" else ""
			if fu:
				self._write(fu)
			try:
				self._write(raw)
			except OSError:  # files: take the follow-up back (a database rolls the lock's transaction back on its own)
				if fu and self._exists(fu["id"]):
					self.backend.delete(fu["id"])
				raise
			self._log("complete", tid, agent, summary, c.get("principal"))
			if shut:
				self._shut(tid, shut, status, summary.strip(), result, agent)
			if fu:
				self._log("created", fu["id"], agent, f"{fu['title']} (rest of {tid})")
				if fu["proposed_by"]:
					self._log("proposed", fu["id"], agent, fu["title"])
			self._record_request(agent, request_id, "complete", tid)
			return c.get("retaken")
		late: list[dict] = []
		clash: list[str] = []
		retaken = self._locked(done)
		if isinstance(retaken, dict):  # a repeat: nothing changed
			return self._already(retaken)
		out = self._with_unread(self._with_lease(self.get(tid), None, retaken), late, "complete")
		if clash:
			out["warnings"] = clash + out.get("warnings", [])
		if touched and (tw := self._touched_warnings(out)):
			out["warnings"] = out.get("warnings", []) + tw
		if closes and (ids := (out.get("result") or {}).get("closes")):
			out["closed"] = [{"id": x, "status": (b := self.get(x))["status"], "title": b["title"]} for x in ids]
		if followup is not None and (fid := (out.get("result") or {}).get("followup")):
			f = self.get(fid)
			out["followup"] = {k: f.get(k) for k in ("id", "title", "effective_status", "blocked_by", "open_blockers", "proposed_by")}
		return out

	def _touched_warnings(self, t: dict) -> list[str]:
		"""CORE-47: complete(touched=)'s warnings: paths edited outside the ticket's touches, and other doing tickets whose touches meet them."""
		res, warn = t.get("result") or {}, []
		if res.get("outside"):
			warn.append(f"you edited {', '.join(res['outside'])} outside {t['id']}'s touches: claims are kept apart by touches, so another worker "
			            "may have been editing them too. Check the merge, and list such paths in touches next time (update_ticket before you edit)")
		for o in self._doing_overlaps({"id": t["id"], "touches": res.get("touched", [])}, self._board()):
			warn.append(f"{', '.join(o['files'])} also in {o['id']}'s touches ({o['agent'] or 'no live claim'} doing it"
			            f"{', branch ' + o['branch'] if o['branch'] else ''}): tell them (add_note on {o['id']}); expect a merge conflict")
		return warn

	def _closes(self, tid: str, ids: list[str], agent: str) -> list[dict]:
		"""Caller holds the lock. complete(closes=)'s tickets, read and checked (AI-57); every problem refuses the complete, listed at once."""
		live, out, bad = self._live_claims(), [], []
		humans = {o["name"] for o in self.config["owners"] if o.get("kind") == "human"}
		board = self._board()  # decorated: open blockers
		for x in ids:
			raw = self.backend.read(x)
			if raw is None:
				bad.append(f"no ticket {x}")
			elif raw.get("status") in ("done", "review"):
				bad.append(f"{x} is already {raw['status']}")
			elif x in live and live[x]["agent"] != agent:
				bad.append(f"{x} is claimed by {live[x]['agent']}: they finish it, or ask them to release it")
			# guardrails (owner, 2026-11-26): closing is never a way around a person's decision or a blocker
			elif raw.get("waiting_on"):
				bad.append(f"{x} waits on a human ({raw['waiting_on'][:80]!r}): the answer comes first")
			elif raw.get("proposed_by"):
				bad.append(f"{x} is a proposal nobody accepted yet: a human accepts or deletes it")
			elif raw.get("owner") in humans and self.profile_of(agent):
				bad.append(f"{x} is owned by {raw['owner']}: tell them it looks done (add_note), they close it")
			elif (ob := (board.get(x) or {}).get("open_blockers")):
				bad.append(f"{x} still waits on {', '.join(ob)}: close those first, or leave it")
			else:
				out.append(raw)
		if bad:
			raise TicketError(f"closes refused, {tid} not completed: " + "; ".join(bad))
		return out

	def _shut(self, tid: str, shut: list[dict], status: str, summary: str, result: dict, agent: str) -> None:
		"""Caller holds the lock. Write complete(closes=)'s tickets: same status as tid, a note and result "Done under tid" (AI-57)."""
		claims = self._claims()
		if any(claims.pop(x["id"], None) for x in shut):  # the completing agent's own claim on one of them
			self._save_claims(claims)
		line = f"Done under {tid}: {summary.splitlines()[0][:300]}"
		for raw in shut:
			_append_note(raw, agent, line, strict=False, kind="complete", status=status)
			raw["status"], raw["updated"] = status, date.today().isoformat()
			raw["done_at"] = _now() if status == "done" else ""
			raw["result"] = {"summary": line, "files": result["files"], "refs": result["refs"], "handoff": "", "by": agent, "at": result["at"], "under": tid}
			self._write(raw)
			self._log("done" if status == "done" else "complete", raw["id"], agent, f"done under {tid}")

	def _followup(self, raw: dict, f, agent: str) -> dict:
		"""Caller holds the lock. complete(followup=) as an unwritten, validated ticket (AI-60): placed where the ticket it continues is;
		schema and proposal gate as for create_ticket by this agent."""
		if not (isinstance(f, dict) and isinstance(f.get("title"), str) and f["title"].strip()):
			raise TicketError("followup needs a title: {title, done_when?, blocked_by?: [ids it waits on], body?, priority?, touches?} = the part left to do")
		if (bad := set(f) - FOLLOWUP_KEYS):
			raise TicketError(f"followup takes {', '.join(sorted(FOLLOWUP_KEYS))}, not {', '.join(sorted(bad))} (the rest is copied from {raw['id']})")
		who, strict = self._creator(agent)
		keep = ("pillar", "owner", "priority", "tags", "touches", "docs", "components", "skills", "mcp")
		data = {k: raw[k] for k in keep if raw.get(k) not in (None, "", [])}
		if raw.get("parent") and self._exists(raw["parent"]):
			data["parent"] = raw["parent"]
		if raw.get("milestone") in {m["key"] for m in self.config.get("milestones") or []}:
			data["milestone"] = raw["milestone"]
		data.update({k: v for k, v in f.items() if k != "body"})
		body = f.get("body")
		data["body"] = f"Rest of {raw['id']}." + (f"\n\n{body.strip()}" if isinstance(body, str) and body.strip() else "")
		pre = raw["id"].rsplit("-", 1)[0]
		nid = self.next_id(pre)
		problems = self._schema_problems(data, pre, strict)
		if problems and strict:
			raise TicketError("followup " + self._schema_refusal(problems))
		try:
			return self._prepare(data, nid, inherited_caps=raw, inherited_components=raw.get("components", []),
			                     proposed_by=who if strict and self.config["agent_tickets"] == "propose" else "")
		except TicketError as e:
			raise TicketError(f"followup refused, {raw['id']} not completed: {e}")

	def report(self, tid: str, by: str, summary: str, files: list[str] | None = None, refs: list[str] | None = None) -> dict:
		"""CORE-68: a person writes the summary a done ticket was closed without (moved to done by hand, not finished with complete()).
		Sets `result` {summary, files, refs, handoff "", by, at}, adds a `complete` note, logs `reported`. Refused unless the ticket is
		done, when it already has a summary, and for a worker (a worker finishes its own tickets with complete)."""
		if not (isinstance(by, str) and by.strip()):
			raise TicketError("report needs `by`: who is writing the summary")
		self._human_only("report", by)
		by = by.strip()
		_check_result_input("report", summary, files, refs, None)
		def run():
			raw = self._fresh(tid)
			if raw.get("status") != "done":
				raise TicketError(f"{tid} is {raw.get('status')}, not done: open work is finished with complete(), which records the summary")
			if (raw.get("result") or {}).get("summary"):
				raise TicketError(f"{tid} already has a summary by {raw['result'].get('by')}: add a note instead")
			_append_note(raw, by, summary.strip(), kind="complete", status="done")
			raw["result"] = {"summary": summary.strip(), "files": [x.strip() for x in files or []], "refs": [x.strip() for x in refs or []],
			                 "handoff": "", "by": by, "at": _now()}
			raw["updated"] = date.today().isoformat()
			self._write(raw)
			self._log("reported", tid, by, summary.strip())
		self._locked(run)
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
		for e in self.backend.events_newest_first(ticket=tid):  # stop as soon as the limit or the floor is reached
			if len(out) >= limit:
				return out
			if floor is not None and _ts(e["ts"]) < floor:
				return out
			if tid is None or e.get("ticket") == tid:
				out.append(dict(e))
		return out

	def agents_now(self) -> dict:
		"""The live view: one row per working agent, plus recent activity."""
		live = self._live_claims()
		titles = {i: t.get("title", "") for i, t in self._raw().items()}
		working = [{"agent": c["agent"], "profile": c.get("profile"), "ticket": tid, "title": titles.get(tid, ""),
		            "note": c.get("note", ""), "principal": c.get("principal"), "age_s": c["age_s"], "quiet_s": c["quiet_s"],
		            "session": c.get("session"), "clash": bool(c.get("clash_at"))}  # clash = another session of this name called (CORE-88)
		           for tid, c in sorted(live.items(), key=lambda kv: kv[1]["claimed_at"])]
		return {"working": working, "lease_minutes": self.config["lease_minutes"], "recent": self.activity(limit=20)}


_NO_REPORT = ("finish with complete(): {id} is done with no summary of what changed (cause -> fix -> tests, files, PR); it shows as "
              "Finished without a summary until someone adds one (report {id}: REST POST tickets/{id}/report, CLI report)")


_REVIEW_NEEDS_RESULT = ("{id} has no result, so a reviewer has nothing to check: finish it with complete() (it records the summary, files "
                        "and PR link, and lands in review on a review board), or move it to todo / done")


def _unreported(t: dict) -> bool:
	"""Done after UNREPORTED_SINCE with no result summary (moved to done by hand, not finished with complete())."""
	return t.get("status") == "done" and not (t.get("result") or {}).get("summary") and str(t.get("done_at") or "")[:10] >= UNREPORTED_SINCE


def _closes_input(tid: str, closes) -> list[str]:
	"""complete(closes=) as a clean list of ids (AI-57): refused before anything is read when it is not one."""
	if closes is None or closes == []:
		return []
	if isinstance(closes, str):
		closes = [x for x in closes.replace(",", " ").split() if x]
	if not (isinstance(closes, (list, tuple)) and all(isinstance(x, str) and _ID_TOKEN.fullmatch(x.strip()) for x in closes)):
		raise TicketError("closes is a list of ticket ids, e.g. [\"CORE-12\", \"CORE-14\"]: open tickets whose work landed in this one")
	ids = list(dict.fromkeys(x.strip() for x in closes))
	if tid in ids:
		raise TicketError(f"closes names {tid} itself: list the OTHER tickets this work also finished")
	if len(ids) > CLOSES_MAX:
		raise TicketError(f"closes names {len(ids)} tickets, at most {CLOSES_MAX}")
	return ids


_STALE_HINT = ("no event and no edit for a while: its work may have landed under another ticket. Check (git log, the ticket's touches) "
               "before working it; if it is done, close it with complete(<the ticket that did it>, closes=[this id]) or ask a human")


def _session(s) -> str | None:
	"""A worker session id (CORE-88): any short string, stripped; empty = none (old clients, plain CLI: no check)."""
	s = s.strip() if isinstance(s, str) else ""
	if len(s) > 100 or any(ch.isspace() for ch in s):
		raise TicketError("session is an id of the running worker (no spaces, <= 100 chars), e.g. a uuid")
	return s or None


def _check_result_input(op: str, summary, files, refs, handoff) -> None:
	"""What complete() and report() take, checked before anything changes."""
	if not (isinstance(summary, str) and summary.strip()):
		raise TicketError(f"{op} needs a summary: cause -> fix -> tests, and what was NOT verified")
	for name, v in (("files", files), ("refs", refs)):
		if v is not None and not _short_list(v):
			eg = '["unblockd/store.py"]' if name == "files" else '["https://github.com/o/r/pull/7"]'  # outside the f-string: 3.10 allows no backslash in {} (CORE-89)
			raise TicketError(f"{name} must be a list of short strings (max 50, each up to 300 chars), e.g. {eg}")
	if len(summary.strip()) > SUMMARY_MAX:
		raise TicketError(f"summary is {len(summary.strip())} chars, the limit is {SUMMARY_MAX}: keep cause -> fix -> tests, move detail into an attached file")
	if handoff is not None and not (isinstance(handoff, str) and len(handoff) <= 2000):
		raise TicketError("handoff must be text up to 2000 chars: what the tickets that wait on this one need to know")


def _append_note(raw: dict, by: str, text: str, strict: bool = True, kind: str = "note", **extra) -> bool:
	"""The one way a note enters a ticket (caller holds the lock and writes): {by, at, kind, text, **extra} at the end of `notes`.
	Over BODY_MAX (body + notes) or NOTES_MAX: strict raises (explicit writes), otherwise the note is skipped and False comes back
	(automatic ones: expiry, release reason; the activity log keeps the event)."""
	notes = raw.setdefault("notes", [])
	size = len(raw.get("body", "")) + sum(len(n.get("text", "")) for n in notes) + len(text)
	if size > BODY_MAX or len(notes) >= NOTES_MAX:
		if strict:
			raise TicketError(f"{raw['id']} would pass {BODY_MAX} chars or {NOTES_MAX} notes: move old detail into an attached file (attach_file) first")
		return False
	notes.append({"n": len(notes) + 1, "by": by, "at": _now(), "kind": kind, "text": text, **extra})
	return True


_NOTE_RE = re.compile(r"— ([^,\n]+), (\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d|Z)?): ([\s\S]*)")
_DONE_RE = re.compile(r"(\d{4}-\d\d-\d\d) \(([^,\n]+), (done|review)\): ([\s\S]*)")
_LEGACY = (("question: ", "question"), ("Rejected: ", "rejected"), ("released: ", "released"))


def split_notes(body: str) -> tuple[str, list[dict]]:
	"""An old body (notes appended as `— by, ISO: text` and complete()'s `YYYY-MM-DD (agent, done): summary`) -> (description, notes).
	A chunk starts at a blank line followed by one of those two shapes; anything else stays with the chunk before it."""
	chunks = re.split(r"\n\n(?=— [^,\n]+, \d{4}-\d\d-\d\dT|\d{4}-\d\d-\d\d \([^,\n]+, (?:done|review)\): )", body or "")
	desc, notes = [], []
	for c in chunks:
		if (m := _NOTE_RE.fullmatch(c)):
			by, at, text = m.groups()
			n = {"by": by, "at": at, "kind": "note", "text": text}
			if text.startswith("Answer to: ") and " — " in text:
				n["q"], n["text"] = text[11:].split(" — ", 1)
				n["kind"] = "answer"
			elif text.startswith(("Approved by ", "Accepted by ")):
				n["kind"] = "approved" if text.startswith("Approved") else "accepted"
			elif text.startswith("claim by ") and " expired" in text:
				n["kind"] = "expired"
			else:
				for pre, kind in _LEGACY:
					if text.startswith(pre):
						n["kind"], n["text"] = kind, text[len(pre):]
			notes.append(n)
		elif (m := _DONE_RE.fullmatch(c)):
			day, by, status, text = m.groups()
			notes.append({"by": by, "at": day, "kind": "complete", "text": text.split("\nDone when:")[0], "status": status})
		elif notes:  # text after a note that is not a note: keep it with that note
			notes[-1]["text"] += "\n\n" + c
		else:
			desc.append(c)
	for i, n in enumerate(notes, 1):
		n["n"] = i
		if n["kind"] in PINNED_KINDS:
			n["pinned"] = True
	return "\n\n".join(desc), notes


def _parse(t: dict) -> dict:
	"""A ticket as stored, from any connector, upgraded from what old files hold (a rule, so it lives here, not in a connector)."""
	if t.get("status") == "pending":  # derived only (effective_status); old files stored it, read as todo
		t["status"] = "todo"
	t.setdefault("rev", 0)
	if "notes" not in t:  # old files: notes were appended to the body; split them out (written back on the ticket's next write)
		t["body"], t["notes"] = split_notes(t.get("body", ""))
	for i, n in enumerate(t["notes"], 1):  # notes written before they were numbered
		n.setdefault("n", i)
	return t


def _copy(t: dict) -> dict:
	"""A copy of a cached ticket that a caller may change freely (lists and dicts copied two levels deep: result.files, claim)."""
	return {k: v[:] if isinstance(v, list) else {a: b[:] if isinstance(b, list) else b for a, b in v.items()} if isinstance(v, dict) else v
	        for k, v in t.items()}


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ts(iso: str) -> float:
	d = datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith("Z") else iso)
	return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()  # a time without offset is UTC, like every stored one


def _wait_s(v) -> float:
	"""claim / claim_next wait_s: seconds, clamped to 0..WAIT_MAX (AI-46)."""
	try:
		return min(max(float(v or 0), 0.0), WAIT_MAX)
	except (TypeError, ValueError):
		raise TicketError(f"wait_s must be a number of seconds (0-{WAIT_MAX})")


class Busy(TicketError):
	"""claim(tid) refused only because another live claim holds overlapping touches: waiting (wait_s) can help (AI-46)."""


def _iso(ts: float) -> str:
	return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _fmt_min(m: float) -> str:
	return str(int(m)) if float(m).is_integer() else f"{m:g}"


def _overlap(a: str, b: str) -> bool:
	"""Do two paths/globs touch the same files? (same path, one inside the other, or a glob match)"""
	a, b = a.replace("\\", "/").strip("/"), b.replace("\\", "/").strip("/")
	return a == b or a.startswith(b + "/") or b.startswith(a + "/") or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)
