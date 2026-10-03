"""MCP server (stdio) over the same boards and Store: `python -m unblockd.mcp_server`.

Tools: list_boards, create_board, board_info, work_plan, next_ready, list_tickets, get_ticket,
create_ticket, update_ticket, delete_ticket, for workers claim_next, claim_ticket, heartbeat,
release, complete, agents_now, and for knowledge get_context, read_doc, attach_file, detach_file. Every ticket tool takes board= (default: the first board).
Errors come back as tool errors with a plain message (bad id, bad field, unknown board...).
"""
from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import boards as bd
from . import knowledge as kn
from . import store as st

BOARDS = bd.from_env()
mcp = MCPServer(
	"unblockd",
	instructions=(
		"unblockd: dependency-aware ticket boards. One ticket = one JSON file; several boards may be "
		"served (list_boards; every tool takes board=, default = the first). status is "
		"todo|doing|pending|done; a todo with an open blocker reads as pending (effective_status). "
		"Priorities P0 (blocking) to P3 (later). Each board's owners, pillars and agent profiles: "
		"board_info. A ticket's `agent` says which agent profile should work it (from its tags or "
		"pillar; an `agent:<name>` tag overrides). To work a board: work_plan groups ready tickets "
		"per agent profile with that profile's instructions and model, ready to fan out; a single "
		"agent uses next_ready(agent=its name). Create with a prefix (BUG, UI...) and the next free "
		"number is picked. blocked_by lists tickets that must be done first; loops are refused. "
		"Put the reasoning in body, not just the change.\n"
		"WORKER PROTOCOL (when you are one of several agents): pick a unique agent name like "
		"godot-dev-1 (profile name + number). Loop: claim_next(agent) -> it sets the ticket to doing "
		"and returns it, or null when nothing is left for you; work it; call heartbeat(id, agent, "
		"note) at least every few minutes with what you're doing (a claim with no heartbeat for "
		"lease_minutes expires and goes back to todo); finish with complete(id, agent, summary: "
		"cause -> fix -> tests, and what was NOT verified), or release(id, agent, reason) if you "
		"can't finish, or update_ticket to pending with waiting_on when a human must decide. Never "
		"work a ticket you didn't claim. agents_now shows who is on what.\n"
		"KNOWLEDGE: claim_next / claim_ticket return the ticket's `context`: the docs linked on the "
		"board, the agent profile and the ticket, plus files attached to it, inlined up to a budget. "
		"Read them before working; they are the instructions you would otherwise be told. Items without "
		"content: read_doc(path). Link more with update_ticket(docs=[...]) or attach_file."
	),
)


def tool(fn):
	"""Register fn as a tool; a TicketError reaches the agent as its message (MCP hides other errors)."""
	@functools.wraps(fn)
	def wrapper(*a, **kw):
		try:
			return fn(*a, **kw)
		except st.TicketError as e:
			raise ToolError(str(e)) from e
	return mcp.tool()(wrapper)


def _summary(t: dict) -> dict:
	keys = ("id", "title", "effective_status", "priority", "owner", "pillar", "tags", "agent", "open_blockers")
	return {k: t.get(k) for k in keys}


@tool
def list_boards() -> list[dict]:
	"""Every board this server knows: id, title, ticket / open / ready counts."""
	return BOARDS.summary()


@tool
def create_board(id: str, title: str = "") -> dict:
	"""Make a new empty board (a tickets folder with an unblockd.json) under the server's project root."""
	return BOARDS.create(id, title)


@tool
def board_info(board: str | None = None) -> dict:
	"""A board's settings: title, owners, pillars, agent profiles (name, tags, pillars, model,
	instructions), stale_days."""
	return BOARDS.get(board).config


@tool
def work_plan(board: str | None = None, per_agent: int = 3) -> dict:
	"""Ready tickets (not owned by a human) grouped by the agent profile that should take them, best first, each group with
	the profile's instructions and model; plus tickets no profile claims (unrouted). For an
	orchestrator: spawn one agent per group, each works its tickets one at a time."""
	plan = BOARDS.get(board).work_plan(per_agent=per_agent)
	for g in plan["agents"]:
		g["tickets"] = [{**_summary(t), "score": t["score"]} for t in g["tickets"]]
	plan["unrouted"] = [{**_summary(t), "score": t["score"]} for t in plan["unrouted"]]
	return plan


@tool
def next_ready(board: str | None = None, owner: str | None = None, pillar: str | None = None,
               agent: str | None = None, tag: str | None = None, limit: int = 5) -> list[dict]:
	"""What to work on next: ready tickets ranked by priority x how many open tickets they unblock.
	An agent with a profile passes agent=<its name> to get only its own work."""
	return [{**_summary(t), "score": t["score"], "unblocks": t["unblocks"]}
	        for t in BOARDS.get(board).next_ready(owner=owner, pillar=pillar, agent=agent, tag=tag, limit=limit)]


@tool
def list_tickets(board: str | None = None, status: str | None = None, pillar: str | None = None,
                 owner: str | None = None, priority: str | None = None, tag: str | None = None,
                 agent: str | None = None, q: str | None = None, full: bool = False) -> list[dict]:
	"""List tickets. Filters: status (todo|doing|pending|done, matches effective status), pillar,
	owner, priority (P0-P3), tag, agent (profile name), q (words to search).
	Returns short summaries unless full=true."""
	res = BOARDS.get(board).list(status=status, pillar=pillar, owner=owner, priority=priority, q=q, tag=tag, agent=agent)
	return res if full else [_summary(t) for t in res]


@tool
def get_ticket(id: str, board: str | None = None) -> dict:
	"""One ticket with everything: body, tags, agent, blocked_by, open_blockers, blocks, dates."""
	return BOARDS.get(board).get(id)


@tool
def create_ticket(title: str, board: str | None = None, prefix: str | None = None, id: str | None = None,
                  status: str = "todo", priority: str | None = None, owner: str | None = None,
                  pillar: str | None = None, pillar_step: int | None = None, section: str = "",
                  blocked_by: list[str] | None = None, tags: list[str] | None = None,
                  waiting_on: str = "", body: str = "") -> dict:
	"""Create a ticket (owner defaults to the board's agent owner). Pass prefix (e.g. "BUG") to get
	the next free number, or an explicit id. tags route it to an agent profile (see board_info)."""
	store = BOARDS.get(board)
	data = {"title": title, "status": status, "priority": priority, "owner": owner or store.config["default_owner"],
	        "pillar": pillar, "pillar_step": pillar_step, "section": section, "blocked_by": blocked_by or [],
	        "tags": tags or [], "waiting_on": waiting_on, "body": body}
	return store.create(data, tid=id, prefix=prefix)


@tool
def update_ticket(id: str, changes: dict, board: str | None = None) -> dict:
	"""Change fields of a ticket. changes is an object of field -> new value, e.g.
	{"status": "done", "body": "..."}. Fields: title, status, priority, owner, pillar,
	pillar_step, section, blocked_by, tags, touches, docs, waiting_on, body."""
	return BOARDS.get(board).update(id, changes)


@tool
def delete_ticket(id: str, board: str | None = None) -> dict:
	"""Delete a ticket file. Prefer status=done for finished work; delete is for mistakes and duplicates."""
	return BOARDS.get(board).delete(id)


@tool
def claim_next(agent: str, profile: str | None = None, note: str = "", board: str | None = None) -> dict | None:
	"""Take the best ready ticket for this worker and set it to doing. agent = your unique worker
	name (e.g. godot-dev-1); profile defaults to the profile your name starts with. Skips tickets
	owned by humans, already claimed, or whose `touches` overlap a live claim. Returns null when
	there's nothing left for you."""
	store = BOARDS.get(board)
	t = store.claim(agent, profile=profile, note=note)
	return {**t, "context": kn.context(store, t["id"])} if t else None


@tool
def claim_ticket(id: str, agent: str, note: str = "", board: str | None = None) -> dict:
	"""Claim one specific ticket (it must be ready, not human-owned, not claimed)."""
	store = BOARDS.get(board)
	t = store.claim(agent, tid=id, note=note)
	return {**t, "context": kn.context(store, t["id"])}


@tool
def heartbeat(id: str, agent: str, note: str = "", board: str | None = None) -> dict:
	"""Keep your claim alive and say what you're doing now; shown live on the board. Call it at
	least every few minutes."""
	return BOARDS.get(board).heartbeat(id, agent, note)


@tool
def release(id: str, agent: str, reason: str = "", board: str | None = None) -> dict:
	"""Give your claimed ticket back (it returns to todo). Say why."""
	return BOARDS.get(board).release(id, agent, reason)


@tool
def complete(id: str, agent: str, summary: str, board: str | None = None) -> dict:
	"""Finish your claimed ticket: status done, summary appended to its body. summary = cause ->
	fix -> tests, and what was NOT verified."""
	return BOARDS.get(board).complete(id, agent, summary)


@tool
def agents_now(board: str | None = None) -> dict:
	"""Who is working what right now (agent, ticket, last note, seconds since last heartbeat),
	plus recent activity."""
	return BOARDS.get(board).agents_now()


@tool
def get_context(id: str, board: str | None = None, budget: int = 60000) -> dict:
	"""What to read before working a ticket: docs linked on the board, its agent profile and the
	ticket itself (paths, folders, globs in the project repo), then files attached to it. Inlined up
	to `budget` characters; the rest is listed for read_doc. Each doc lists its `links` (the
	knowledge graph's edges) and any error (missing, outside the project)."""
	return kn.context(BOARDS.get(board), id, budget=budget)


@tool
def read_doc(path: str, board: str | None = None) -> dict:
	"""One text file from the board's project repo (path relative to the repo root), with its links."""
	return kn.read_doc(BOARDS.get(board), path)


@tool
def attach_file(id: str, name: str, content: str, board: str | None = None) -> dict:
	"""Attach a text file to a ticket (stored in tickets/files/<ID>/<name>, committed with it). Use
	for notes, specs or findings that belong to this ticket; link repo docs with update_ticket docs."""
	return kn.attach(BOARDS.get(board), id, name, content)


@tool
def detach_file(id: str, name: str, board: str | None = None) -> dict:
	"""Remove a file attached to a ticket."""
	return kn.detach(BOARDS.get(board), id, name)


def main():
	mcp.run()


if __name__ == "__main__":
	main()
