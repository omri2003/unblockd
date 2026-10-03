"""Command line over the same Store (for agents without the MCP server).

    python -m unblockd.cli [--dir TICKETS] list [--status S] [--pillar P] [--owner O] [--priority P] [-q WORDS]
    python -m unblockd.cli next [--owner O] [--pillar P] [--limit N]   # ready tickets, best first
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
	for f in ("status", "pillar", "owner", "priority"):
		ls.add_argument(f"--{f}")
	ls.add_argument("-q")
	nx = sub.add_parser("next")
	nx.add_argument("--owner")
	nx.add_argument("--pillar")
	nx.add_argument("--limit", type=int, default=5)
	sub.add_parser("get").add_argument("id")
	cr = sub.add_parser("create")
	cr.add_argument("--prefix")
	cr.add_argument("--id")
	cr.add_argument("--title", required=True)
	cr.add_argument("--json", default="{}")
	up = sub.add_parser("update")
	up.add_argument("id")
	up.add_argument("--json", required=True)
	no = sub.add_parser("note")
	no.add_argument("id")
	no.add_argument("text")
	a = ap.parse_args(argv)

	try:
		s = st.Store(a.dir, [p for p in os.environ.get("UNBLOCKD_LEGACY", "").split(os.pathsep) if p])
		if a.cmd == "list":
			res = s.list(status=a.status, pillar=a.pillar, owner=a.owner, priority=a.priority, q=a.q)
			out = [{k: t.get(k) for k in ("id", "title", "effective_status", "priority", "owner", "pillar", "open_blockers")} for t in res]
		elif a.cmd == "next":
			out = [{**{k: t.get(k) for k in ("id", "title", "priority", "owner", "pillar")}, "score": t["score"], "unblocks": t["unblocks"]}
			       for t in s.next_ready(owner=a.owner, pillar=a.pillar, limit=a.limit)]
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
