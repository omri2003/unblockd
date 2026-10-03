"""MCP server (stdio) over the same Store: `python -m unblockd.mcp_server`.

Tools: next_ready, list_tickets, get_ticket, create_ticket, update_ticket, delete_ticket.
Errors come back as tool errors with a plain message (bad id, bad field...).
"""
from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import store as st

STORE = st.from_env()
_CFG = STORE.config
mcp = MCPServer(
	"unblockd",
	instructions=(
		f"Ticket tracker: {_CFG['title']}. One ticket = one JSON file. "
		"status is todo|doing|pending|done; a todo with an open blocker reads as pending "
		"(effective_status). Priorities P0 (blocking) to P3 (later). "
		f"owner is one of: {', '.join(o['name'] for o in _CFG['owners'])}. "
		+ (f"{_CFG['pillar_label']} is one of: {', '.join(p['key'] for p in _CFG['pillars'])}, or null. " if _CFG["pillars"] else "")
		+ "Start with next_ready to pick work. Create with a prefix (BUG, UI...) and the next free "
		"number is picked. blocked_by lists tickets that must be done first; loops are refused. "
		"Put the reasoning in body, not just the change."
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
	keys = ("id", "title", "effective_status", "priority", "owner", "pillar", "open_blockers")
	return {k: t.get(k) for k in keys}


@tool
def next_ready(owner: str | None = None, pillar: str | None = None, limit: int = 5) -> list[dict]:
	"""What to work on next: ready tickets ranked by priority x how many open tickets they unblock."""
	return [{**_summary(t), "score": t["score"], "unblocks": t["unblocks"]}
	        for t in STORE.next_ready(owner=owner, pillar=pillar, limit=limit)]


@tool
def list_tickets(status: str | None = None, pillar: str | None = None, owner: str | None = None,
                 priority: str | None = None, q: str | None = None, full: bool = False) -> list[dict]:
	"""List tickets. Filters: status (todo|doing|pending|done, matches effective status),
	pillar and owner (values listed in the server instructions), priority (P0-P3), q (words to search).
	Returns short summaries unless full=true."""
	res = STORE.list(status=status, pillar=pillar, owner=owner, priority=priority, q=q)
	return res if full else [_summary(t) for t in res]


@tool
def get_ticket(id: str) -> dict:
	"""One ticket with everything: body, blocked_by, open_blockers, blocks, dates."""
	return STORE.get(id)


@tool
def create_ticket(title: str, prefix: str | None = None, id: str | None = None, status: str = "todo",
                  priority: str | None = None, owner: str | None = None, pillar: str | None = None,
                  pillar_step: int | None = None, section: str = "", blocked_by: list[str] | None = None,
                  waiting_on: str = "", body: str = "") -> dict:
	"""Create a ticket (owner defaults to the configured agent owner). Pass prefix (e.g. "BUG") to get the next free number, or an explicit id."""
	data = {"title": title, "status": status, "priority": priority, "owner": owner or _CFG["default_owner"], "pillar": pillar,
	        "pillar_step": pillar_step, "section": section, "blocked_by": blocked_by or [],
	        "waiting_on": waiting_on, "body": body}
	return STORE.create(data, tid=id, prefix=prefix)


@tool
def update_ticket(id: str, changes: dict) -> dict:
	"""Change fields of a ticket. changes is an object of field -> new value, e.g.
	{"status": "done", "body": "..."}. Fields: title, status, priority, owner, pillar,
	pillar_step, section, blocked_by, waiting_on, body."""
	return STORE.update(id, changes)


@tool
def delete_ticket(id: str) -> dict:
	"""Delete a ticket file. Prefer status=done for finished work; delete is for mistakes and duplicates."""
	return STORE.delete(id)


def main():
	mcp.run()


if __name__ == "__main__":
	main()
