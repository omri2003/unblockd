"""MCP server (stdio) over the same boards and Store: `python -m unblockd.mcp_server`.

Prompt `work_loop(board, profile)` and resource `unblockd://work-loop`: the worker protocol, for any MCP client.
Tools: list_boards, create_board, board_info, work_plan, next_ready, list_tickets, get_ticket,
create_ticket, update_ticket, split_ticket, delete_ticket, for workers claim_next, claim_ticket, heartbeat,
release, ask_human, add_note, complete, agents_now, and for knowledge get_context, read_doc, attach_file, detach_file. Every ticket tool takes board= (default: the first board).
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
		"todo|doing|pending|review|done; a todo with an open blocker reads as pending (effective_status). review (boards with review=true) = finished by an agent, waiting for a human to set done; it does not unblock dependents. "
		"Priorities P0 (blocking) to P3 (later). Each board's owners, pillars and agent profiles: "
		"board_info. A ticket's `agent` says which agent profile should work it (from its tags or "
		"pillar; an `agent:<name>` tag overrides). To work a board: work_plan groups ready tickets "
		"per agent profile with that profile's instructions and model, ready to fan out; a single "
		"agent uses next_ready(agent=its name). Create with a prefix (BUG, UI...) and the next free "
		"number is picked. blocked_by lists tickets that must be done first; loops are refused. "
		"Put the reasoning in body, not just the change; report progress with add_note (append-only, signed), not update_ticket(body=...), which replaces the whole body.\n"
		"WORKER PROTOCOL (when you are one of several agents): pick a unique agent name like "
		"godot-dev-1 (profile name + number). Loop: claim_next(agent) -> it sets the ticket to doing "
		"and returns it, or {ticket: null, reason, counts} when nothing can be taken (see the work loop for what each reason means); work it; call heartbeat(id, agent, "
		"note) at least every few minutes with what you're doing (a claim with no heartbeat for "
		"lease_minutes expires and goes back to todo); finish with complete(id, agent, summary: "
		"cause -> fix -> tests, and what was NOT verified), or release(id, agent, reason) if you "
		"can't finish, or ask_human(id, agent, question) when only a human can decide (it releases your claim and parks the ticket until the answer is written back). Never "
		"work a ticket you didn't claim. agents_now shows who is on what.\n"
		"KNOWLEDGE: claim_next / claim_ticket return the ticket's `context`: the docs linked on the "
		"board, the agent profile and the ticket, plus files attached to it, inlined up to a budget. "
		"Read them before working; they are the instructions you would otherwise be told. Items without "
		"content: read_doc(path). Link more with update_ticket(id, {docs: [...]}, agent) or attach_file."
	),
)


WORK_LOOP = """You are a worker on the unblockd board {board}{profile_line}. Loop until step 2 says to stop:
1. Your worker name is unique: {agent_hint}. Use it as `agent` in every call below.
2. claim_next(agent=<your name>{claim_args}) -> a ticket plus its `context`, or {{ticket: null, reason, counts}}: reason "conflict" = ready tickets exist but overlap other workers' live claims (touches): wait about a minute and call again, up to 5 times, then stop and report; reason "waiting" (everything open is in flight, in review, waiting on a human or blocked) or "dry" (nothing open): stop and report what you did and the reason and counts.
3. Read every doc in `context` in order before touching anything (read_doc(path) for items without content; report entries with an `error`). `done_when`, if present, is the bar for done.
4. Work it. Call heartbeat(id, agent, note) with what you are doing at least every 5 minutes: 15 minutes of silence expires the claim and the ticket goes back to todo.
5. Needs a decision only a human can make: ask_human(id, agent, question), then go on to the next ticket. Cannot finish for another reason: release(id, agent, reason).
6. Done: complete(id, agent, summary) with "cause -> fix -> tests; NOT verified: ..." and an answer for each `done_when` item. On boards with review=true it lands in review for a human instead of done.
Rules: never work a ticket you did not claim; never edit outside what the ticket needs; a new problem becomes its own ticket (create_ticket with blocked_by and touches), not a wider change here. agents_now shows who is on what."""


def work_loop_text(board: str | None = None, profile: str | None = None) -> str:
	return WORK_LOOP.format(
		board=board or "(the default board; list_boards shows the others)", profile_line=f" with the agent profile {profile}" if profile else "",
		agent_hint=f"{profile}-1, {profile}-2, ... (profile name + number)" if profile else "profile name + number, e.g. godot-dev-1 (board_info lists the profiles)",
		claim_args=(f", profile={profile}" if profile else "") + (f", board={board}" if board else ""))


@mcp.prompt()
def work_loop(board: str | None = None, profile: str | None = None) -> str:
	"""The worker protocol: claim -> read context -> heartbeat -> complete / release / ask_human, in a loop."""
	return work_loop_text(board, profile)


@mcp.resource("unblockd://work-loop", mime_type="text/plain")
def work_loop_resource() -> str:
	"""The same worker protocol as the work_loop prompt, for clients that read resources."""
	return work_loop_text()


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
	agent = a profile name or your worker name (godot-dev-1 means profile godot-dev, the same rule claim_next uses): only that profile's work."""
	return [{**_summary(t), "score": t["score"], "unblocks": t["unblocks"]}
	        for t in BOARDS.get(board).next_ready(owner=owner, pillar=pillar, agent=agent, tag=tag, limit=limit)]


@tool
def list_tickets(board: str | None = None, status: str | None = None, pillar: str | None = None,
                 owner: str | None = None, priority: str | None = None, tag: str | None = None,
                 agent: str | None = None, parent: str | None = None, q: str | None = None, full: bool = False) -> list[dict]:
	"""List tickets. Filters: status (todo|doing|pending|review|done, matches effective status), pillar,
	owner, priority (P0-P3), tag, agent (profile name or worker name, resolved to its profile), parent (id: that ticket's parts), q (words to search).
	Returns short summaries unless full=true."""
	res = BOARDS.get(board).list(status=status, pillar=pillar, owner=owner, priority=priority, q=q, tag=tag, agent=agent, parent=parent)
	return res if full else [_summary(t) for t in res]


@tool
def get_ticket(id: str, board: str | None = None) -> dict:
	"""One ticket with everything: body, tags, agent, blocked_by, open_blockers, blocks, dates."""
	return BOARDS.get(board).get(id)


@tool
def create_ticket(title: str, board: str | None = None, prefix: str | None = None, id: str | None = None,
                  status: str = "todo", priority: str | None = None, owner: str | None = None,
                  pillar: str | None = None, pillar_step: int | None = None, section: str = "",
                  blocked_by: list[str] | None = None, tags: list[str] | None = None, parent: str | None = None,
                  waiting_on: str = "", body: str = "", touches: list[str] | None = None, docs: list[str] | None = None,
                  done_when: list[str] | None = None) -> dict:
	"""Create a ticket (owner defaults to the board's agent owner). Pass prefix (e.g. "BUG") to get
	the next free number, or an explicit id. tags route it to an agent profile (see board_info). touches = paths/globs the work edits (claims
	never overlap), docs = repo paths to read, done_when = the acceptance criteria (one short line each)."""
	store = BOARDS.get(board)
	data = {"title": title, "status": status, "priority": priority, "owner": owner or store.config["default_owner"],
	        "pillar": pillar, "pillar_step": pillar_step, "section": section, "blocked_by": blocked_by or [], "parent": parent,
	        "tags": tags or [], "waiting_on": waiting_on, "body": body, "touches": touches or [], "docs": docs or [], "done_when": done_when or []}
	return store.create(data, tid=id, prefix=prefix)


@tool
def update_ticket(id: str, changes: dict, agent: str, board: str | None = None) -> dict:
	"""Change fields of a ticket. agent = your worker name: you can't change status or waiting_on of a
	ticket another agent has claimed (finish with complete, give back with release, ask with ask_human).
	changes is an object of field -> new value, e.g. {"body": "..."}. Fields: title, status, priority, owner, pillar,
	pillar_step, section, blocked_by, done_when (list of acceptance criteria), parent (id of the group it belongs to; children/progress are derived), tags, touches, docs, waiting_on, body."""
	return BOARDS.get(board).update(id, changes, agent=agent)


@tool
def split_ticket(id: str, titles: list[str], sequential: bool = False, board: str | None = None) -> dict:
	"""Use this when a ticket is too big to finish in one go: split it instead of half-doing it, then
	claim_next. One child per title (pillar, owner, priority, tags, touches, docs copied); the parent
	then waits on all of them. sequential=true makes each part wait on the one before. If you hold the
	claim, the parent goes back to todo and your claim ends. Returns {parent, children}."""
	return BOARDS.get(board).split(id, titles, sequential=sequential)


@tool
def delete_ticket(id: str, board: str | None = None) -> dict:
	"""Delete a ticket file. Prefer status=done for finished work; delete is for mistakes and duplicates."""
	return BOARDS.get(board).delete(id)


@tool
def claim_next(agent: str, profile: str | None = None, note: str = "", board: str | None = None) -> dict:
	"""Take the best ready ticket for this worker and set it to doing. agent = your unique worker
	name (<profile>-N, e.g. godot-dev-1; on a board with agent profiles a name matching none is refused); profile defaults to the profile your name starts with. Skips tickets
	owned by humans, already claimed, or whose `touches` overlap a live claim. When there is nothing to
	take it returns {ticket: null, reason, counts}: reason "conflict" = ready tickets are blocked by
	live claims' touches, retry in a minute; "waiting" = open tickets exist but none is ready (in
	flight, in review, waiting on a human, blocked) and "dry" = nothing open: on those stop and report."""
	store = BOARDS.get(board)
	return kn.with_context(store, store.claim_next(agent, profile=profile, note=note))


@tool
def claim_ticket(id: str, agent: str, note: str = "", board: str | None = None) -> dict:
	"""Claim one specific ticket (it must be ready, not human-owned, not claimed)."""
	store = BOARDS.get(board)
	return kn.with_context(store, store.claim(agent, tid=id, note=note))


@tool
def heartbeat(id: str, agent: str, note: str = "", board: str | None = None) -> dict:
	"""Keep your claim alive and say what you're doing now; shown live on the board. Call it at
	least every few minutes."""
	return BOARDS.get(board).heartbeat(id, agent, note)


@tool
def ask_human(id: str, agent: str, question: str, board: str | None = None) -> dict:
	"""Ask a human a question you can't decide yourself: ends your claim, sets waiting_on to the question (the ticket
	is not claimable until a human clears waiting_on), and notifies. Move on to the next ticket."""
	return BOARDS.get(board).ask(id, agent, question)


@tool
def add_note(id: str, agent: str, text: str, board: str | None = None) -> dict:
	"""Record progress or a finding on a ticket: appends a signed, timestamped line to its body, safely (never replaces the body, so it can't
	clobber a concurrent complete or a human's answer). Prefer it to update_ticket(body=...). Works on any ticket, claimed or not."""
	return BOARDS.get(board).add_note(id, agent, text)


@tool
def release(id: str, agent: str, reason: str = "", board: str | None = None) -> dict:
	"""Give your claimed ticket back (it returns to todo). Say why."""
	return BOARDS.get(board).release(id, agent, reason)


@tool
def complete(id: str, agent: str, summary: str, files: list[str] | None = None, refs: list[str] | None = None,
             handoff: str | None = None, board: str | None = None) -> dict:
	"""Finish your claimed ticket: status done (review on boards that require a human look), summary appended to its body. summary = cause ->
	fix -> tests, and what was NOT verified. Also stored as the ticket's `result`: files = paths you changed, refs = PR / commit / URL,
	handoff = what tickets waiting on this one must know; they get it in their context as `upstream`."""
	return BOARDS.get(board).complete(id, agent, summary, files=files, refs=refs, handoff=handoff)


@tool
def agents_now(board: str | None = None) -> dict:
	"""Who is working what right now (agent, ticket, last note, seconds since last heartbeat),
	plus recent activity."""
	return BOARDS.get(board).agents_now()


@tool
def get_context(id: str, board: str | None = None, budget: int | None = None) -> dict:
	"""What to read before working a ticket: docs linked on the board, its agent profile and the
	ticket itself (paths, folders, globs in the project repo), then files attached to it. Inlined up
	to `budget` characters (default: the board's context_budget); the rest is listed for read_doc. Each doc lists its `links` (the
	knowledge graph's edges) and any error (missing, outside the project)."""
	return kn.context(BOARDS.get(board), id, budget=budget)


@tool
def knowledge_graph(board: str | None = None) -> dict:
	"""Who reads what: open tickets, the docs their context would hold, ticket->doc and doc->doc edges, and the problems:
	`no_docs` (tickets with no task-specific doc), `broken` (missing / outside / non-text refs + their tickets), `hubs`, `orphans`."""
	return kn.graph(BOARDS.get(board))


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
