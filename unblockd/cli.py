"""Command line over the same Store (for agents without the MCP server).

    python -m unblockd.cli [--dir TICKETS] list [--status S] [--pillar P] [--owner O] [--priority P] [--tag T] [--agent A] [-q WORDS]
    python -m unblockd.cli next [--owner O] [--pillar P] [--agent A] [--tag T] [--limit N]   # ready, best first
    python -m unblockd.cli plan [--per-agent N]   # ready tickets grouped by agent profile
    python -m unblockd.cli claim --agent godot-dev-1 [--id ID] [--profile P]   # take the next (or one) ticket
    python -m unblockd.cli heartbeat ID --agent A [--note "now: writing tests"]
    python -m unblockd.cli release ID --agent A [--reason "..."]
    python -m unblockd.cli complete ID --agent A --summary "cause -> fix -> tests; not verified: ..."
    python -m unblockd.cli agents                  # who is working what right now
    python -m unblockd.cli get ID
    python -m unblockd.cli create --prefix BUG --title "..." [--json '{"priority": "P1", ...}']
    python -m unblockd.cli update ID --json '{"status": "done"}'
    python -m unblockd.cli note ID "text"        # appends a dated paragraph to the body

--dir defaults to $TICKETS_DIR, then ./tickets. Settings come from
<dir>/unblockd.json (see README); extra legacy numbering files from $UNBLOCKD_LEGACY.
Prints JSON. Exit code 1 + message on bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from . import store as st


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(prog="unblockd.cli")
	ap.add_argument("--dir", default=os.environ.get("TICKETS_DIR", "tickets"))
	sub = ap.add_subparsers(dest="cmd", required=True)
	ls = sub.add_parser("list")
	for f in ("status", "pillar", "owner", "priority", "tag", "agent"):
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
	cl = sub.add_parser("claim")
	cl.add_argument("--agent", required=True)
	cl.add_argument("--id")
	cl.add_argument("--profile")
	cl.add_argument("--note", default="")
	for name, extra in (("heartbeat", "--note"), ("release", "--reason"), ("complete", "--summary")):
		x = sub.add_parser(name)
		x.add_argument("id")
		x.add_argument("--agent", required=True)
		x.add_argument(extra, required=name == "complete", default="")
	sub.add_parser("agents")
	no = sub.add_parser("note")
	no.add_argument("id")
	no.add_argument("text")
	a = ap.parse_args(argv)

	try:
		s = st.Store(a.dir, [p for p in os.environ.get("UNBLOCKD_LEGACY", "").split(os.pathsep) if p])
		if a.cmd == "list":
			res = s.list(status=a.status, pillar=a.pillar, owner=a.owner, priority=a.priority, q=a.q, tag=a.tag, agent=a.agent)
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
			out = s.claim(a.agent, tid=a.id, profile=a.profile, note=a.note)
		elif a.cmd == "heartbeat":
			out = s.heartbeat(a.id, a.agent, a.note)
		elif a.cmd == "release":
			out = s.release(a.id, a.agent, a.reason)
		elif a.cmd == "complete":
			out = s.complete(a.id, a.agent, a.summary)
		elif a.cmd == "agents":
			out = s.agents_now()
		elif a.cmd == "get":
			out = s.get(a.id)
		elif a.cmd == "create":
			out = s.create({**json.loads(a.json), "title": a.title}, tid=a.id, prefix=a.prefix)
		elif a.cmd == "update":
			out = s.update(a.id, json.loads(a.json))
		else:
			body = s.get(a.id).get("body", "")
			out = s.update(a.id, {"body": (body + "\n\n" if body else "") + f"{date.today().isoformat()}: {a.text}"})
	except (st.TicketError, json.JSONDecodeError) as e:
		print(f"error: {e}", file=sys.stderr)
		return 1
	print(json.dumps(out, indent=2, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	sys.exit(main())
