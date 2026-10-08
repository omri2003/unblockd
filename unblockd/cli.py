"""Command line over the same Store (for agents without the MCP server).

    python -m unblockd.cli [--dir TICKETS] list [--status S] [--pillar P] [--owner O] [--priority P] [--tag T] [--agent A] [--parent ID] [-q WORDS]
    python -m unblockd.cli next [--owner O] [--pillar P] [--agent A] [--tag T] [--limit N]   # ready, best first
    python -m unblockd.cli plan [--per-agent N]   # ready tickets grouped by agent profile
    python -m unblockd.cli claim --agent godot-dev-1 [--id ID] [--profile P]   # take the next (or one) ticket
    python -m unblockd.cli heartbeat ID --agent A [--note "now: writing tests"]
    python -m unblockd.cli release ID --agent A [--reason "..."]
    python -m unblockd.cli ask ID --agent A --question "what only a human can decide"   # releases the claim, waits on a human
    python -m unblockd.cli complete ID --agent A --summary "cause -> fix -> tests; not verified: ..." [--file PATH]... [--ref PR_OR_URL]... [--handoff "what dependents must know"]
    python -m unblockd.cli agents                  # who is working what right now
    python -m unblockd.cli context ID              # docs + attached files to read before working ID
    python -m unblockd.cli doc PATH                # one file from the project repo
    python -m unblockd.cli attach ID FILE [--name N]   # attach a local text file to a ticket
    python -m unblockd.cli get ID
    python -m unblockd.cli create --prefix BUG --title "..." [--json '{"priority": "P1", ...}']
    python -m unblockd.cli update ID --json '{"status": "done"}' [--agent A]'
    python -m unblockd.cli split ID --title "part one" --title "part two" [--sequential] [--by AGENT]   # too big: make children
    python -m unblockd.cli note ID "text" [--by NAME]   # appends a signed, timestamped line to the body (by defaults to "human")

--dir defaults to $TICKETS_DIR, then ./tickets. With $UNBLOCKD_URL set (plus $UNBLOCKD_TOKEN, and $UNBLOCKD_BOARD for a non-default board) every
command goes to that server's REST API instead of a local folder, so agents on other machines share one board and one lock. Settings come from
<dir>/unblockd.json (see README); extra legacy numbering files from $UNBLOCKD_LEGACY.
Prints JSON. Exit code 1 + message on bad input.
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

from . import knowledge as kn
from . import store as st


class Remote:
	"""The Store calls this CLI makes, sent to a running unblockd.web instead (UNBLOCKD_URL)."""
	def __init__(self, url: str, token: str | None = None, board: str | None = None):
		self.base = url.rstrip("/") + "/api" + (f"/b/{urllib.parse.quote(board)}" if board else "")
		self.token = token

	def _call(self, method: str, path: str, body=None, **q):
		q = {k: v for k, v in q.items() if v is not None}
		req = urllib.request.Request(self.base + path + ("?" + urllib.parse.urlencode(q) if q else ""), method=method,
		                             data=None if body is None else json.dumps(body).encode(),
		                             headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
		try:
			with urllib.request.urlopen(req, timeout=30) as r:
				return json.loads(r.read())
		except urllib.error.HTTPError as e:
			try:
				msg = json.loads(e.read()).get("error")
			except (ValueError, AttributeError):
				msg = None
			raise st.TicketError(msg or f"{self.base}: HTTP {e.code}")
		except urllib.error.URLError as e:
			raise st.TicketError(f"can't reach {self.base}: {e.reason} (UNBLOCKD_URL)")

	def list(self, **f): return self._call("GET", "/tickets", **f)
	def next_ready(self, **f): return self._call("GET", "/next", **f)
	def work_plan(self, per_agent=3): return self._call("GET", "/plan", per_agent=per_agent)
	def agents_now(self): return self._call("GET", "/agents")
	def get(self, tid): return self._call("GET", f"/tickets/{tid}")
	def create(self, data, tid=None, prefix=None): return self._call("POST", "/tickets", {**data, **({"id": tid} if tid else {}), **({"prefix": prefix} if prefix else {})})
	def update(self, tid, changes, agent=None): return self._call("PATCH", f"/tickets/{tid}", {**changes, **({"agent": agent} if agent else {})})
	def split(self, tid, titles, sequential=False, by=None): return self._call("POST", f"/tickets/{tid}/split", {"titles": titles, "sequential": sequential, "by": by})
	def claim(self, agent, tid=None, profile=None, note=""):
		return self._call("POST", f"/tickets/{tid}/claim" if tid else "/claim", {"agent": agent, "profile": profile, "note": note})
	def claim_next(self, agent, profile=None, note=""): return self.claim(agent, profile=profile, note=note)  # the server answers with the reason when empty
	def heartbeat(self, tid, agent, note=""): return self._call("POST", f"/tickets/{tid}/heartbeat", {"agent": agent, "note": note})
	def release(self, tid, agent, reason=""): return self._call("POST", f"/tickets/{tid}/release", {"agent": agent, "reason": reason})
	def ask(self, tid, agent, question): return self._call("POST", f"/tickets/{tid}/ask", {"agent": agent, "question": question})
	def add_note(self, tid, by, text): return self._call("POST", f"/tickets/{tid}/notes", {"by": by, "text": text})
	def complete(self, tid, agent, summary, files=None, refs=None, handoff=None):
		return self._call("POST", f"/tickets/{tid}/complete", {"agent": agent, "summary": summary, "files": files, "refs": refs, "handoff": handoff})
	def context(self, tid): return self._call("GET", f"/tickets/{tid}/context")
	def read_doc(self, path): return self._call("GET", "/doc", path=path)
	def attach(self, tid, name, content): return self._call("POST", f"/tickets/{tid}/files", {"name": name, "content": content})


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(prog="unblockd.cli")
	ap.add_argument("--dir", default=os.environ.get("TICKETS_DIR", "tickets"))
	sub = ap.add_subparsers(dest="cmd", required=True)
	ls = sub.add_parser("list")
	for f in ("status", "pillar", "owner", "priority", "tag", "agent", "parent"):
		ls.add_argument(f"--{f}")
	ls.add_argument("-q")
	nx = sub.add_parser("next")
	nx.add_argument("--owner")
	nx.add_argument("--pillar")
	nx.add_argument("--agent")
	nx.add_argument("--tag")
	nx.add_argument("--limit", type=int, default=5)
	sub.add_parser("plan").add_argument("--per-agent", type=int, default=3)
	sub.add_parser("get").add_argument("id")
	cr = sub.add_parser("create")
	cr.add_argument("--prefix")
	cr.add_argument("--id")
	cr.add_argument("--title", required=True)
	cr.add_argument("--json", default="{}")
	up = sub.add_parser("update")
	up.add_argument("id")
	up.add_argument("--json", required=True)
	up.add_argument("--agent", help="the worker making the change: it can't change status/waiting_on of a ticket another agent holds")
	cl = sub.add_parser("claim")
	cl.add_argument("--agent", required=True)
	cl.add_argument("--id")
	cl.add_argument("--profile")
	cl.add_argument("--note", default="")
	for name, extra in (("heartbeat", "--note"), ("release", "--reason"), ("ask", "--question"), ("complete", "--summary")):
		x = sub.add_parser(name)
		x.add_argument("id")
		x.add_argument("--agent", required=True)
		x.add_argument(extra, required=name in ("ask", "complete"), default="")
	cp = sub.choices["complete"]
	cp.add_argument("--file", action="append", help="a path you changed (repeatable)")
	cp.add_argument("--ref", action="append", help="a PR / commit / URL (repeatable)")
	cp.add_argument("--handoff", help="what tickets waiting on this one must know")
	sub.add_parser("agents")
	sub.add_parser("context").add_argument("id")
	sub.add_parser("doc").add_argument("path")
	at = sub.add_parser("attach")
	at.add_argument("id")
	at.add_argument("file")
	at.add_argument("--name")
	sp = sub.add_parser("split")
	sp.add_argument("id")
	sp.add_argument("--title", action="append", default=[])
	sp.add_argument("--sequential", action="store_true")
	sp.add_argument("--by")
	no = sub.add_parser("note")
	no.add_argument("id")
	no.add_argument("text")
	no.add_argument("--by", default="human")
	a = ap.parse_args(argv)

	try:
		url = os.environ.get("UNBLOCKD_URL")
		s = Remote(url, os.environ.get("UNBLOCKD_TOKEN"), os.environ.get("UNBLOCKD_BOARD")) if url else \
			st.Store(a.dir, [p for p in os.environ.get("UNBLOCKD_LEGACY", "").split(os.pathsep) if p])
		ctx = s.context if url else lambda tid: kn.context(s, tid)
		if a.cmd == "list":
			res = s.list(status=a.status, pillar=a.pillar, owner=a.owner, priority=a.priority, q=a.q, tag=a.tag, agent=a.agent, parent=a.parent)
			out = [{k: t.get(k) for k in ("id", "title", "effective_status", "priority", "owner", "pillar", "agent", "open_blockers")} for t in res]
		elif a.cmd == "next":
			out = [{**{k: t.get(k) for k in ("id", "title", "priority", "owner", "pillar", "agent")}, "score": t["score"], "unblocks": t["unblocks"]}
			       for t in s.next_ready(owner=a.owner, pillar=a.pillar, agent=a.agent, tag=a.tag, limit=a.limit)]
		elif a.cmd == "plan":
			out = s.work_plan(per_agent=a.per_agent)
			short = lambda t: {k: t.get(k) for k in ("id", "title", "priority", "score")}
			for g in out["agents"]:
				g["tickets"] = [short(t) for t in g["tickets"]]
			out["unrouted"] = [short(t) for t in out["unrouted"]]
		elif a.cmd == "claim":
			got = s.claim(a.agent, tid=a.id, profile=a.profile, note=a.note) if a.id else s.claim_next(a.agent, profile=a.profile, note=a.note)
			out = {**got, "context": ctx(got["id"])} if "id" in got else got  # no id = {ticket: null, reason, counts}
		elif a.cmd == "heartbeat":
			out = s.heartbeat(a.id, a.agent, a.note)
		elif a.cmd == "release":
			out = s.release(a.id, a.agent, a.reason)
		elif a.cmd == "ask":
			out = s.ask(a.id, a.agent, a.question)
		elif a.cmd == "complete":
			out = s.complete(a.id, a.agent, a.summary, files=a.file, refs=a.ref, handoff=a.handoff)
		elif a.cmd == "agents":
			out = s.agents_now()
		elif a.cmd == "context":
			out = ctx(a.id)
		elif a.cmd == "doc":
			out = s.read_doc(a.path) if url else kn.read_doc(s, a.path)
		elif a.cmd == "attach":
			f = Path(a.file)
			out = (s.attach if url else lambda *x: kn.attach(s, *x))(a.id, a.name or f.name, f.read_text(encoding="utf-8"))
		elif a.cmd == "split":
			out = s.split(a.id, a.title, sequential=a.sequential, by=a.by)
		elif a.cmd == "get":
			out = s.get(a.id)
		elif a.cmd == "create":
			out = s.create({**json.loads(a.json), "title": a.title}, tid=a.id, prefix=a.prefix)
		elif a.cmd == "update":
			out = s.update(a.id, json.loads(a.json), agent=a.agent)
		else:
			out = s.add_note(a.id, a.by, a.text)
	except (st.TicketError, json.JSONDecodeError) as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	print(json.dumps(out, indent=2, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())
