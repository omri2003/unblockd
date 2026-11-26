"""Command line over the same Store (for agents without the MCP server).

    python -m unblockd.cli [--dir TICKETS] list [--status S | --all] [--pillar P] [--owner O] [--priority P] [--tag T] [--agent A] [--parent ID] [-q WORDS] [--ids A,B] [--touches PATH] [--component KEY] [--fields touches,done_when] [--limit N] [--stale-days N]
    python -m unblockd.cli next [--owner O] [--pillar P] [--agent A] [--tag T] [--limit N]   # ready, best first
    python -m unblockd.cli plan [--per-agent N]   # ready tickets grouped by agent profile
    python -m unblockd.cli claim --agent backend-1 [--id ID] [--profile P] [--principal HUMAN] [--branch B] [--wait S]   # take the next (or one) ticket; --principal (env UNBLOCKD_PRINCIPAL) = the human owner you work for; --wait (<= 120) = on a conflict, wait for it to free up
    python -m unblockd.cli heartbeat ID --agent A [--note "now: writing tests"] [--until 2026-11-21T18:30:00+00:00]   # --until: a long run ends then (capped at lease_max_minutes)
    python -m unblockd.cli release ID --agent A [--reason "..."] [--next]
    python -m unblockd.cli ask ID --agent A --question "what only a human can decide" [--next]   # releases the claim, waits on a human
    python -m unblockd.cli complete ID --agent A --summary "cause -> fix -> tests; not verified: ..." [--file PATH]... [--ref PR_OR_URL]... [--handoff "what dependents must know"] [--followup '{"title": "rest", "blocked_by": ["CORE-68"]}'] [--closes ID]... [--touched PATH]... [--next]
        # --next (complete / release / ask): also claim the next ticket (with --principal / --branch): prints {"done": ..., "next": <what claim prints>}
        # env UNBLOCKD_SESSION (claim / heartbeat / release / complete): an id of this worker's session; another session under the same name is warned (CORE-88)
    python -m unblockd.cli answer ID "text" [--by NAME]   # a human answers an ask: note + clears waiting_on
    python -m unblockd.cli reject ID "reason" [--by NAME] # a ticket in review goes back to todo with the reason as a note
    python -m unblockd.cli agents-md [--board ID] [--write CLAUDE.md]   # the short block for CLAUDE.md / AGENTS.md: claim, heartbeat, complete
    python -m unblockd.cli agents                  # who is working what right now
    python -m unblockd.cli context ID              # docs + attached files to read before working ID
    python -m unblockd.cli knowledge               # open tickets with no doc of their own (+ suggested docs) and broken doc refs
    python -m unblockd.cli doc PATH                # one file from the project repo
    python -m unblockd.cli attach ID FILE [--name N]   # attach a local text file to a ticket
    python -m unblockd.cli get ID
    python -m unblockd.cli create --prefix BUG --title "..." [--component KEY]... [--skill NAME]... [--mcp NAME]... [--json '{"priority": "P1", ...}'] [--agent A]   # --agent: strict schema, may park as a proposal
    python -m unblockd.cli create-many --file plan.json|- [--agent A]   # a JSON array, all or nothing; blocked_by / parent may say "#N" = the Nth item
    python -m unblockd.cli accept ID [--by NAME]   # a human accepts an agent's proposal
    python -m unblockd.cli approve ID [--by NAME]  # a human approves work in review: done, with the approver recorded
    python -m unblockd.cli report ID "summary" [--by NAME] [--file PATH]... [--ref URL]...   # a person adds the summary a done ticket was closed without
    python -m unblockd.cli update ID [--rev N] [--json '{"status": "done"}'] [--component KEY]... [--skill NAME]... [--mcp NAME]... [--agent A]   # --component SETS the ticket's components (the board's architecture notes)
    python -m unblockd.cli update-many --file changes.json|- [--agent A]   # [{"id": "UI-12", "changes": {...}}, ...], all or nothing
    python -m unblockd.cli delete ID [--agent A]
    python -m unblockd.cli split ID --title "part one" --title "part two" [--sequential] [--by AGENT]   # too big: make children
    python -m unblockd.cli split ID --parts parts.json|- [--by AGENT --claim K]   # parts with their own done_when / touches / body; "#N" refs; claim part K
    python -m unblockd.cli note ID "text" [--by NAME]   # appends a signed, timestamped note to the ticket's notes (by defaults to "human")

--dir defaults to $TICKETS_DIR, then ./tickets; it may be a database location instead (`sqlite:///x.db#board`, see docs/reference.md). With $UNBLOCKD_URL set (plus $UNBLOCKD_TOKEN, and $UNBLOCKD_BOARD for a non-default board) every
command goes to that server's REST API instead of a local folder, so agents on other machines share one board and one lock. Settings come from
<dir>/unblockd.json (see README); extra legacy numbering files from $UNBLOCKD_LEGACY.
Prints JSON. Exit code 1 + message on bad input. With --json before the command (`cli --json claim ...`), or whenever stdout is
not a terminal (an agent or a script reading it), an error is ONE JSON object on stdout too:
{"error": "...", "code": "refused|not_found|conflict|down|usage"}, exit 1. In a terminal: `error: ...` on stderr, as before.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__
from . import boards as bd
from . import connectors
from . import knowledge as kn
from . import store as st


class Down(st.TicketError):
	"""The server did not answer (Remote): code "down" in --json mode."""


class _Usage(st.TicketError):
	"""A command line argparse refused: code "usage" in --json mode."""


class _Parser(argparse.ArgumentParser):
	JSON = False  # set by main before parsing: argparse's own errors become a JSON object too (API-24)

	def error(self, message):
		if not _Parser.JSON:
			super().error(message)
		raise _Usage(f"{message} (usage: {self.format_usage().strip().removeprefix('usage: ')})")


def _fail(e: Exception, as_json: bool) -> int:
	"""API-24: every error the same shape for a reader that parses stdout; a person in a terminal keeps the plain line."""
	if as_json:
		code = ("not_found" if isinstance(e, st.NotFound) else "conflict" if isinstance(e, st.Conflict) else
		        "down" if isinstance(e, Down) else "usage" if isinstance(e, _Usage) else "refused")
		print(json.dumps({"error": str(e), "code": code}, ensure_ascii=False))
	else:
		print(f"error: {e}", file=sys.stderr)
	return 1


def _json_mode(argv: list[str]) -> bool:
	"""--json among the options before the command (not create's / update's own --json data), or stdout is not a terminal."""
	i = 0
	while i < len(argv) and argv[i].startswith("-"):
		if argv[i] == "--json":
			return True
		i += 2 if argv[i] == "--dir" else 1
	try:
		return not sys.stdout.isatty()
	except (AttributeError, ValueError):
		return True


def _ses(session) -> dict:
	"""The session key of a Remote write body: only when there is one (CORE-88), so an old server never sees it."""
	return {"session": session} if session else {}


class Remote:
	"""The Store calls this CLI makes, sent to a running unblockd.web instead (UNBLOCKD_URL)."""
	def __init__(self, url: str, token: str | None = None, board: str | None = None):
		self.origin = url.rstrip("/")
		self.base = self.origin + "/api" + (f"/b/{urllib.parse.quote(board)}" if board else "")
		self.token = token

	def down(self, why) -> Down:
		"""The one "server down" message: say it plainly and what to do, so an agent stops and tells its human instead of retrying blind."""
		return Down(f"unblockd server down: nothing answers at {self.origin} ({why}). The host may be asleep or the server stopped: "
		                      "ask your human to start it (docker compose up -d). Nothing you sent was saved; run `health` until it says ok, then retry")

	def health(self) -> dict:
		"""GET /healthz (no auth): {"status": "ok", "url"} or the server-down error."""
		try:
			with urllib.request.urlopen(self.origin + "/healthz", timeout=10) as r:
				return {**json.loads(r.read()), "url": self.origin}
		except urllib.error.HTTPError as e:
			raise st.TicketError(f"{self.origin}/healthz answered HTTP {e.code}: is UNBLOCKD_URL the unblockd web server (not the MCP port)?")
		except urllib.error.URLError as e:
			raise self.down(e.reason)
		except (TimeoutError, OSError) as e:
			raise self.down(e)

	def _call(self, method: str, path: str, body=None, headers=None, timeout: float = 30, **q):
		q = {k: v for k, v in q.items() if v is not None}
		req = urllib.request.Request(self.base + path + ("?" + urllib.parse.urlencode(q) if q else ""), method=method,
		                             data=None if body is None else json.dumps(body).encode(),
		                             headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {}), **(headers or {})})
		try:
			with urllib.request.urlopen(req, timeout=timeout) as r:
				return json.loads(r.read())
		except urllib.error.HTTPError as e:
			try:
				msg = json.loads(e.read()).get("error")
			except (ValueError, AttributeError):
				msg = None
			raise {404: st.NotFound, 409: st.Conflict}.get(e.code, st.TicketError)(msg or f"{self.base}: HTTP {e.code}")
		except urllib.error.URLError as e:
			raise self.down(e.reason)
		except (TimeoutError, ConnectionError) as e:  # the server went away mid-reply, or hangs
			raise self.down(e)

	def list(self, **f): return self._call("GET", "/tickets", **{**f, **({"ids": ",".join(f["ids"])} if isinstance(f.get("ids"), (list, tuple)) else {})})
	def next_ready(self, **f): return self._call("GET", "/next", **f)
	def roadmap(self): return self._call("GET", "/roadmap")
	def work_plan(self, per_agent=3): return self._call("GET", "/plan", per_agent=per_agent)
	def agents_now(self): return self._call("GET", "/agents")
	def get(self, tid): return self._call("GET", f"/tickets/{tid}")
	def create(self, data, tid=None, prefix=None, agent=None, request_id=None): return self._call("POST", "/tickets", {**data, **({"id": tid} if tid else {}), **({"prefix": prefix} if prefix else {}), **({"agent": agent} if agent else {}), **({"request_id": request_id} if request_id else {})})
	def create_many(self, items, agent=None): return self._call("POST", "/tickets/batch", {"tickets": items, **({"agent": agent} if agent else {})})
	def accept(self, tid, by): return self._call("POST", f"/tickets/{tid}/accept", {"by": by})
	def approve(self, tid, by): return self._call("POST", f"/tickets/{tid}/approve", {"by": by})
	def update(self, tid, changes, agent=None, by=None, expected_rev=None):
		return self._call("PATCH", f"/tickets/{tid}", {**changes, **({"agent": agent} if agent else {}), **({"by": by} if by else {})},
		                  headers={"If-Match": str(expected_rev)} if expected_rev is not None else None)
	def update_many(self, items, agent=None, by=None):
		return self._call("PATCH", "/tickets", {"tickets": items, **({"agent": agent} if agent else {}), **({"by": by} if by else {})})
	def report(self, tid, by, summary, files=None, refs=None): return self._call("POST", f"/tickets/{tid}/report", {"by": by, "summary": summary, "files": files, "refs": refs})
	def split(self, tid, titles=None, sequential=False, by=None, parts=None, claim=None, principal=None, branch=None, session=None):
		return self._call("POST", f"/tickets/{tid}/split", {"titles": titles or [], "sequential": sequential, "by": by, "parts": parts, "claim": claim, "principal": principal, **({"branch": branch} if branch else {}), **_ses(session)})
	def delete(self, tid, agent=None): return self._call("DELETE", f"/tickets/{tid}", agent=agent)
	def claim(self, agent, tid=None, profile=None, note="", principal=None, branch=None, wait_s=0, session=None):
		return self._call("POST", f"/tickets/{tid}/claim" if tid else "/claim", {"agent": agent, "profile": profile, "note": note, "principal": principal,
		                  **({"branch": branch} if branch else {}), **({"wait_s": wait_s} if wait_s else {}), **_ses(session)}, timeout=30 + min(float(wait_s or 0), st.WAIT_MAX))
	def claim_next(self, agent, profile=None, note="", principal=None, branch=None, wait_s=0, session=None):  # the server answers with the reason when empty
		return self.claim(agent, profile=profile, note=note, principal=principal, branch=branch, wait_s=wait_s, session=session)
	def heartbeat(self, tid, agent, note="", principal=None, until=None, session=None): return self._call("POST", f"/tickets/{tid}/heartbeat", {"agent": agent, "note": note, "principal": principal, "until": until, **_ses(session)})
	def release(self, tid, agent, reason="", principal=None, missing="", why="", request_id=None, session=None):
		return self._call("POST", f"/tickets/{tid}/release", {"agent": agent, "reason": reason, "principal": principal, "missing": missing, "why": why, "request_id": request_id, **_ses(session)})
	def ask(self, tid, agent, question, request_id=None): return self._call("POST", f"/tickets/{tid}/ask", {"agent": agent, "question": question, "request_id": request_id})
	def answer(self, tid, by, text): return self._call("POST", f"/tickets/{tid}/answer", {"by": by, "text": text})
	def reject(self, tid, by, reason): return self._call("POST", f"/tickets/{tid}/reject", {"by": by, "reason": reason})
	def dismiss_notes(self, tid, by, ns, undo=False): return self._call("POST", f"/tickets/{tid}/dismiss", {"by": by, "notes": ns, "undo": undo})
	def add_note(self, tid, by, text, kind="note", pin=False, reply_to=None, supersedes=None, request_id=None):
		return self._call("POST", f"/tickets/{tid}/notes", {"by": by, "text": text, "kind": kind, "pin": pin, "reply_to": reply_to, "supersedes": supersedes, "request_id": request_id})
	def complete(self, tid, agent, summary, files=None, refs=None, handoff=None, principal=None, request_id=None, followup=None, session=None, closes=None, touched=None):
		return self._call("POST", f"/tickets/{tid}/complete", {"agent": agent, "summary": summary, "files": files, "refs": refs, "handoff": handoff, "principal": principal, "request_id": request_id,
		                                                       **({"followup": followup} if followup is not None else {}), **_ses(session), **({"closes": closes} if closes else {}), **({"touched": touched} if touched is not None else {})})
	def context(self, tid): return self._call("GET", f"/tickets/{tid}/context")
	def knowledge(self): return self._call("GET", "/knowledge")
	def read_doc(self, path): return self._call("GET", "/doc", path=path)
	def read_docs(self, paths): return self._call("GET", "/docs?" + urllib.parse.urlencode([("path", p) for p in paths]))
	def attach(self, tid, name, content): return self._call("POST", f"/tickets/{tid}/files", {"name": name, "content": content})


def blind(g: dict) -> dict:
	"""The lint part of GET knowledge (API-22): open tickets with no doc of their own (`no_docs`, each with the docs suggest() offers:
	add one with `update ID --json '{"docs": [...]}'`) and broken doc references, without the graph."""
	title = {t["id"]: t["title"] for t in g["tickets"]}
	sug = g.get("suggest") or {}
	return {"no_docs": [{"id": i, "title": title.get(i), "suggested": [x["path"] for x in sug.get(i, []) if not x.get("weak")]} for i in g["no_docs"]],
	        "broken": g["broken"]}


def _utf8_out() -> None:
	# Piped output on Windows defaults to the ANSI code page (cp1252), which cannot encode "→" and friends:
	# the command had already run (a claim was taken) and then printing it crashed. JSON readers expect UTF-8 anyway.
	for f in (sys.stdout, sys.stderr):
		if (getattr(f, "encoding", None) or "").lower().replace("-", "") != "utf8" and hasattr(f, "reconfigure"):
			f.reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
	_utf8_out()
	argv = sys.argv[1:] if argv is None else list(argv)
	as_json = _Parser.JSON = _json_mode(argv)
	ap = _Parser(prog="unblockd.cli")
	ap.add_argument("--version", action="version", version=f"unblockd {__version__}")
	ap.add_argument("--dir", default=os.environ.get("TICKETS_DIR", "tickets"))
	ap.add_argument("--json", dest="json_out", action="store_true", help="errors as one JSON object on stdout (default when stdout is not a terminal)")
	sub = ap.add_subparsers(dest="cmd", required=True)
	ls = sub.add_parser("list", help="open tickets (not done) unless --status or --all")
	which = ls.add_mutually_exclusive_group()
	which.add_argument("--status", help="todo|doing|pending|review|done, or open (the default: every one but done)")
	which.add_argument("--all", action="store_true", help="done tickets too")
	for f in ("pillar", "owner", "priority", "tag", "agent", "parent", "milestone"):
		ls.add_argument(f"--{f}")
	ls.add_argument("-q", help="words that must ALL appear in one ticket")
	ls.add_argument("--ids", help="exactly these tickets, comma or space separated (API-23)")
	ls.add_argument("--touches", help="a path or glob: tickets whose touches overlap it (the claim rule); rows add touches + holder")
	ls.add_argument("--component", help="an architecture component key: tickets that name it or touch its paths")
	ls.add_argument("--fields", help="extra keys per row, comma list (e.g. touches,parent,done_when); body and notes only when named")
	ls.add_argument("--limit", type=int, help="at most N rows")
	ls.add_argument("--stale-days", type=int, help="only todo tickets with no event and no edit for N days (rows add idle_days): work that may have landed under another ticket")
	nx = sub.add_parser("next")
	nx.add_argument("--owner")
	nx.add_argument("--pillar")
	nx.add_argument("--agent")
	nx.add_argument("--tag")
	nx.add_argument("--limit", type=int, default=5)
	sub.add_parser("plan").add_argument("--per-agent", type=int, default=3)
	sub.add_parser("roadmap")
	sub.add_parser("health", help="is the board reachable? local: the folder exists; remote (UNBLOCKD_URL): GET /healthz. Exit 1 when down")
	mm = sub.add_parser("migrate-milestones", help="move tickets from a version epic (parent) to a milestone: REL-1=v0.1 REL-2=v0.2 ...")
	mm.add_argument("pairs", nargs="+", metavar="PARENT=KEY")
	sub.add_parser("get").add_argument("id")
	cr = sub.add_parser("create")
	cr.add_argument("--prefix")
	cr.add_argument("--id")
	cr.add_argument("--title", required=True)
	cr.add_argument("--json", default="{}")
	cr.add_argument("--agent", help="the worker creating it: the board's schema is strict for agents, and its ticket may park as a proposal until a human accepts it")
	cr.add_argument("--component", action="append", help="a component key of the board's architecture (repeatable)")
	cr.add_argument("--milestone", help="the roadmap milestone key (milestones in unblockd.json)")
	cr.add_argument("--skill", action="append", help="a skill name from the board's registry (repeatable)")
	cr.add_argument("--mcp", action="append", help="an MCP server name from the board's registry (repeatable)")
	cr.add_argument("--request-id", help="a fresh id for this create: a retry with the same id returns the ticket it made (already: true)")
	cm = sub.add_parser("create-many", help='several tickets in one all-or-nothing step: a JSON array of ticket objects (prefix or id + fields); blocked_by / parent may say "#N" = the Nth item')
	cm.add_argument("--file", required=True, help="path to the JSON array, or - for stdin")
	cm.add_argument("--agent", help="the worker creating them (strict schema, proposal gate), as for create")
	up = sub.add_parser("update")
	up.add_argument("id")
	up.add_argument("--json", default="{}")
	up.add_argument("--component", action="append", help="set the ticket's components to these keys (repeatable)")
	up.add_argument("--milestone", help="set the roadmap milestone key; - clears it")
	up.add_argument("--skill", action="append", help="set the ticket's skills to these registry names (repeatable)")
	up.add_argument("--mcp", action="append", help="set the ticket's MCP servers to these registry names (repeatable)")
	up.add_argument("--agent", help="the worker making the change: it can't change, split or delete a ticket another agent holds")
	up.add_argument("--by", help="who a human change is by (recorded when it moves the ticket to done)")
	up.add_argument("--rev", type=int, help="the rev you last read: refused (code conflict) if the ticket was written since")
	rp = sub.add_parser("report", help="a person adds the summary a done ticket was closed without (moved to done by hand, not with complete)")
	rp.add_argument("id")
	rp.add_argument("summary")
	rp.add_argument("--by", default="human")
	rp.add_argument("--file", action="append", help="a path that changed (repeatable)")
	rp.add_argument("--ref", action="append", help="a PR / commit / URL (repeatable)")
	um = sub.add_parser("update-many", help='several tickets in one all-or-nothing step: a JSON array of {"id", "changes", "expected_rev"?}')
	um.add_argument("--file", required=True, help="path to the JSON array, or - for stdin")
	um.add_argument("--agent", help="the worker making the changes (holder rule and worker limits, as for update)")
	um.add_argument("--by", help="who a human change is by (as update)")
	cl = sub.add_parser("claim")
	cl.add_argument("--agent", required=True)
	cl.add_argument("--id")
	cl.add_argument("--profile")
	cl.add_argument("--note", default="")
	cl.add_argument("--wait", type=float, default=0, help=f"seconds (<= {st.WAIT_MAX}): on a conflict, wait for a claim in the way to free up instead of returning at once")
	pr = os.environ.get("UNBLOCKD_PRINCIPAL")
	cl.add_argument("--principal", default=pr, help="the human owner you work for (default: env UNBLOCKD_PRINCIPAL)")
	cl.add_argument("--branch", default=os.environ.get("UNBLOCKD_BRANCH"), help="the git branch you work on in your own worktree (default: env UNBLOCKD_BRANCH): "
	                "an overlap with a claim on another branch is then a warning, not a refusal")
	for name, extra in (("heartbeat", "--note"), ("release", "--reason"), ("ask", "--question"), ("complete", "--summary")):
		x = sub.add_parser(name)
		x.add_argument("id")
		x.add_argument("--agent", required=True)
		x.add_argument(extra, required=name in ("ask", "complete"), default="")
		if name == "heartbeat":
			x.add_argument("--until", help="UTC ISO time a long run ends: the claim stays alive until then (capped at lease_max_minutes)")
		if name == "release":
			x.add_argument("--missing", default="", help="a tool you need and do not have")
			x.add_argument("--why", default="", help="with --missing: one line on what you needed it for")
		x.add_argument("--principal", default=pr, help="same as claim (default: env UNBLOCKD_PRINCIPAL)" + ("; only used by --next" if name == "ask" else ""))
		if name != "heartbeat":
			x.add_argument("--request-id", help="a fresh id for this write: a retry with the same id changes nothing and answers already: true")
			x.add_argument("--next", action="store_true", help="then claim the next ticket in the same run: prints {done, next}")
			x.add_argument("--branch", default=os.environ.get("UNBLOCKD_BRANCH"), help="with --next: same as claim")
	cp = sub.choices["complete"]
	cp.add_argument("--file", action="append", help="a path you changed (repeatable)")
	cp.add_argument("--ref", action="append", help="a PR / commit / URL (repeatable)")
	cp.add_argument("--handoff", help="what tickets waiting on this one must know")
	cp.add_argument("--touched", action="append", help="a path you really edited (repeatable): ones outside the ticket's touches, or in another doing ticket's, are warned about")
	cp.add_argument("--closes", action="append", help="another open ticket this work also finished: it goes to done under this one (repeatable; all or nothing)")
	cp.add_argument("--followup", help="JSON {title, done_when?, blocked_by?, body?, priority?, touches?}: the part left to do becomes its own ticket, ready once blocked_by is done")
	sub.add_parser("agents")
	sub.add_parser("context").add_argument("id")
	sub.add_parser("knowledge", help="tickets that would start blind (no doc of their own) and broken doc references")
	sub.add_parser("doc", help="print a repo doc; several paths = one reply {docs} within the board's context_budget").add_argument("path", nargs="+")
	at = sub.add_parser("attach")
	at.add_argument("id")
	at.add_argument("file")
	at.add_argument("--name")
	sp = sub.add_parser("split")
	sp.add_argument("id")
	sp.add_argument("--title", action="append", default=[])
	sp.add_argument("--sequential", action="store_true")
	sp.add_argument("--by")
	sp.add_argument("--parts", help='instead of --title: a JSON array file (or - for stdin) of parts [{"title", "done_when", "touches", "body", "blocked_by": ["#1"]}]')
	sp.add_argument("--claim", type=int, help="with --by: claim part K (1-based) in the same step")
	sp.add_argument("--principal", default=pr, help="with --claim: the human owner you work for (default: env UNBLOCKD_PRINCIPAL)")
	sp.add_argument("--branch", default=os.environ.get("UNBLOCKD_BRANCH"), help="with --claim: same as claim")
	de = sub.add_parser("delete")
	de.add_argument("id")
	de.add_argument("--agent", help="the worker deleting: it can't delete a ticket another agent holds")
	no = sub.add_parser("note")
	no.add_argument("id")
	no.add_argument("text")
	no.add_argument("--by", default="human")
	no.add_argument("--kind", default="note", help="note | decision (a choice and why) | finding (cause, error, fact) | warning (don't do X); the last three are pinned")
	no.add_argument("--pin", action="store_true", help="pin it: always sent to the next worker")
	no.add_argument("--reply-to", type=int, help="the note number this answers")
	no.add_argument("--supersedes", type=int, help="the pinned note number this one replaces")
	no.add_argument("--request-id", help="a fresh id for this note: a retry with the same id adds nothing (already: true)")
	dm = sub.add_parser("dismiss", help="a person read late notes on a finished ticket (Needs you lists a person's note on a done ticket): stop listing them")
	dm.add_argument("id")
	dm.add_argument("notes", nargs="+", type=int, metavar="N")
	dm.add_argument("--by", default="human")
	dm.add_argument("--undo", action="store_true")
	for name in ("answer", "reject", "accept", "approve"):
		x = sub.add_parser(name)
		x.add_argument("id")
		if name not in ("accept", "approve"):
			x.add_argument("text")
		x.add_argument("--by", default="human")
	am = sub.add_parser("agents-md", help="print the short block for a project's CLAUDE.md / AGENTS.md that tells every agent session to claim, heartbeat and complete")
	am.add_argument("--board", help="board id to name (default: the board's own id)")
	am.add_argument("--write", metavar="FILE", help="append the block to FILE between markers (refused when they are already there; the rest of FILE is untouched)")
	mg = sub.add_parser("migrate", help="copy a board to another place: a folder, sqlite:///x.db#board, postgres://...#board, mongodb://...#board")
	mg.add_argument("--from", dest="src", required=True, help="where the board is now (a folder or a database URL + #board)")
	mg.add_argument("--to", dest="dst", required=True, help="where it goes; must be empty unless --force")
	mg.add_argument("--force", action="store_true", help="REPLACE whatever board is at --to")
	try:
		a = ap.parse_args(argv)
	except _Usage as e:
		return _fail(e, True)

	if a.cmd == "migrate":  # needs no board of its own (--dir is not used)
		try:
			out = connectors.migrate(a.src, a.dst, force=a.force)
		except st.TicketError as e:
			return _fail(e, as_json)
		print(json.dumps(out, indent=2, ensure_ascii=False))
		return 0
	try:
		url = os.environ.get("UNBLOCKD_URL")
		if not url and not connectors.is_db(a.dir) and not Path(a.dir).is_dir():  # creating it would hand a worker an empty board (e.g. in a git worktree where tickets/ is not committed)
			raise st.TicketError(f"no board at {Path(a.dir).resolve()}: run from the project root, or pass --dir / set TICKETS_DIR to the tickets folder "
			                     "(in a git worktree, the main checkout's). To start a new board, create the folder first.")
		s = Remote(url, os.environ.get("UNBLOCKD_TOKEN"), os.environ.get("UNBLOCKD_BOARD")) if url else \
			st.Store(a.dir, [p for p in os.environ.get("UNBLOCKD_LEGACY", "").split(os.pathsep) if p])
		ctx = s.context if url else lambda tid: kn.context(s, tid)
		ses = os.environ.get("UNBLOCKD_SESSION") or None  # CORE-88: the worker session's id; unset = no session check (one command per process)
		with_ctx = lambda got: {**got, "context": ctx(got["id"])} if "id" in got else got  # no id = {ticket: null, reason, counts}
		if a.cmd == "list":
			keep = st.list_fields(a.fields, matched=bool(a.touches or a.component), idle=a.stale_days is not None)
			res = s.list(status=None if (a.all or (a.ids and not a.status)) else (a.status or "open"), pillar=a.pillar, owner=a.owner, priority=a.priority, q=a.q, tag=a.tag, agent=a.agent, parent=a.parent, milestone=a.milestone,
			             limit=a.limit, touches=a.touches, component=a.component, ids=a.ids, **({"stale_days": a.stale_days} if a.stale_days is not None else {}))  # --ids: exactly those, done ones too
			if a.q and not res:
				others = any(x not in (None, "") for x in (a.status, a.pillar, a.owner, a.priority, a.tag, a.agent, a.parent, a.milestone, a.touches, a.component, a.ids))
				print(f"hint: {st.q_hint(a.q, others)}", file=sys.stderr)  # stdout stays the JSON list
			out = [st.summarize(t, keep) for t in res]
		elif a.cmd == "next":
			out = [{**{k: t.get(k) for k in ("id", "title", "priority", "owner", "pillar", "agent")}, "score": t["score"], "unblocks": t["unblocks"]}
			       for t in s.next_ready(owner=a.owner, pillar=a.pillar, agent=a.agent, tag=a.tag, limit=a.limit)]
		elif a.cmd == "roadmap":
			out = s.roadmap()
		elif a.cmd == "health":
			out = s.health() if url else {"status": "ok", "version": __version__, "dir": connectors.describe(a.dir) if connectors.is_db(a.dir) else Path(a.dir).resolve().as_posix(), "tickets": len(s.list())}
		elif a.cmd == "migrate-milestones":
			pairs = [p.split("=", 1) for p in a.pairs]
			if not all(len(p) == 2 and p[0] and p[1] for p in pairs):
				raise st.TicketError("give pairs like REL-1=v0.1 (the version epic's id = the milestone key in unblockd.json)")
			out, items = {}, []
			for parent, key in pairs:  # every ticket under that epic: milestone set, parent cleared (it was the version, not a feature epic)
				out[parent] = [t["id"] for t in s.list(parent=parent)]
				items += [{"id": x, "changes": {"milestone": key, "parent": None}} for x in out[parent]]
			for i in range(0, len(items), st.UPDATE_MAX):  # update_many: all or nothing per call (one call up to UPDATE_MAX tickets)
				s.update_many(items[i:i + st.UPDATE_MAX])
		elif a.cmd == "plan":
			out = s.work_plan(per_agent=a.per_agent)
			short = lambda t: {k: t.get(k) for k in ("id", "title", "priority", "score", "idle_days") if k in t}
			for g in out["agents"]:
				g["tickets"] = [short(t) for t in g["tickets"]]
			out["unrouted"] = [short(t) for t in out["unrouted"]]
		elif a.cmd == "claim":
			got = s.claim(a.agent, tid=a.id, profile=a.profile, note=a.note, principal=a.principal, branch=a.branch, wait_s=a.wait, session=ses) if a.id else \
				s.claim_next(a.agent, profile=a.profile, note=a.note, principal=a.principal, branch=a.branch, wait_s=a.wait, session=ses)
			out = with_ctx(got)
		elif a.cmd == "heartbeat":
			box = None if url else s.inbox(a.agent, mark=True)  # remote: the server adds it
			out = s.heartbeat(a.id, a.agent, a.note, a.principal, until=a.until, session=ses)
			if box:
				out = {**out, "inbox": box}
		elif a.cmd == "release":
			out = s.release(a.id, a.agent, a.reason, a.principal, missing=a.missing, why=a.why, request_id=a.request_id, session=ses)
		elif a.cmd == "ask":
			out = s.ask(a.id, a.agent, a.question, request_id=a.request_id)
		elif a.cmd == "complete":
			out = s.complete(a.id, a.agent, a.summary, files=a.file, refs=a.ref, handoff=a.handoff, principal=a.principal, request_id=a.request_id, session=ses,
			                 **({"closes": a.closes} if a.closes else {}), **({"touched": a.touched} if a.touched else {}), **({"followup": json.loads(a.followup)} if a.followup else {}))
		elif a.cmd == "agents-md":
			board = a.board or (os.environ.get("UNBLOCKD_BOARD") or "default" if url else s.config["id"])
			text = bd.agents_md(board, a.dir)
			if not a.write:
				print(text, end="")
				return 0
			out = bd.write_agents_md(a.write, text)
		elif a.cmd == "agents":
			out = s.agents_now()
		elif a.cmd == "context":
			out = ctx(a.id)
		elif a.cmd == "knowledge":
			out = blind(s.knowledge() if url else kn.graph(s))
		elif a.cmd == "doc":
			if len(a.path) > 1:
				out = s.read_docs(a.path) if url else kn.read_docs(s, a.path)
			else:
				out = s.read_doc(a.path[0]) if url else kn.read_doc(s, a.path[0])
		elif a.cmd == "attach":
			f = Path(a.file)
			out = (s.attach if url else lambda *x: kn.attach(s, *x))(a.id, a.name or f.name, f.read_text(encoding="utf-8"))
		elif a.cmd == "split":
			parts = json.loads(sys.stdin.read() if a.parts == "-" else Path(a.parts).read_text(encoding="utf-8")) if a.parts else None
			out = s.split(a.id, a.title, sequential=a.sequential, by=a.by, parts=parts, claim=a.claim, principal=a.principal if a.claim else None, branch=a.branch if a.claim else None,
			              session=ses if a.claim else None)
			if out.get("claimed"):  # like claim: the part comes with what to read
				out["claimed"] = {**out["claimed"], "context": ctx(out["claimed"]["id"])}
		elif a.cmd == "answer":
			out = s.answer(a.id, a.by, a.text)
		elif a.cmd == "reject":
			out = s.reject(a.id, a.by, a.text)
		elif a.cmd == "accept":
			out = s.accept(a.id, a.by)
		elif a.cmd == "approve":
			out = s.approve(a.id, a.by)
		elif a.cmd == "dismiss":
			out = s.dismiss_notes(a.id, a.by, a.notes, undo=a.undo)
		elif a.cmd == "delete":
			out = s.delete(a.id, agent=a.agent)
		elif a.cmd == "get":
			out = s.get(a.id) if url else s.with_blockers(s.get(a.id))  # remote: GET tickets/<id> already adds them
		elif a.cmd == "create":
			out = s.create({**json.loads(a.json), "title": a.title, **{k: v for k, v in (("components", a.component), ("skills", a.skill), ("mcp", a.mcp), ("milestone", a.milestone)) if v}}, tid=a.id, prefix=a.prefix, agent=a.agent, request_id=a.request_id)
		elif a.cmd == "create-many":
			out = s.create_many(json.loads(sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")), agent=a.agent)
		elif a.cmd == "update-many":
			out = s.update_many(json.loads(sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")), agent=a.agent, by=a.by)
		elif a.cmd == "update":
			out = s.update(a.id, {**json.loads(a.json), **{k: v for k, v in (("components", a.component), ("skills", a.skill), ("mcp", a.mcp)) if v},
			                     **({"milestone": None if a.milestone == "-" else a.milestone} if a.milestone else {})}, agent=a.agent, by=a.by,
			               **({"expected_rev": a.rev} if a.rev is not None else {}))
		elif a.cmd == "report":
			out = s.report(a.id, a.by, a.summary, files=a.file, refs=a.ref)
		else:
			out = s.add_note(a.id, a.by, a.text, kind=a.kind, pin=a.pin, reply_to=a.reply_to, supersedes=a.supersedes, request_id=a.request_id)
		if a.cmd in ("release", "ask", "complete") and a.next:  # AI-42: one run finishes this ticket and takes the next
			try:
				out = {"done": out, "next": with_ctx(s.claim_next(a.agent, principal=a.principal, branch=a.branch, session=ses))}
			except st.TicketError as e:  # the write above landed: say so, with why no next ticket came
				out = {"done": out, "next": {"ticket": None, "error": str(e)}}
	except (st.TicketError, json.JSONDecodeError, OSError) as e:  # OSError: a --file / attach path that can't be read
		return _fail(e, as_json)
	print(json.dumps(out, indent=2, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())
