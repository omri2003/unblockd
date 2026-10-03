"""Board UI + REST API. Stdlib only: `python -m unblockd.web`.

GET    /api/boards                       every board: id, title, ticket counts
POST   /api/boards                       {"id": "my-game", "title": "My game"}  (needs UNBLOCKD_ROOT)
GET    /api/chat                         {"enabled": bool, "model": ...}
POST   /api/chat                         {"board": id, "messages": [{"role", "content"}]} -> {"reply", "tools", "refs"}
Per board, under /api/b/<board>/ ; the same paths without the prefix use the default (first) board:
GET    tickets?status=&pillar=&owner=&priority=&tag=&agent=&q=
GET    next?owner=&pillar=&agent=&tag=&limit=   ready tickets ranked by priority x tickets unblocked
GET    plan?per_agent=                   ready tickets grouped by agent profile (work_plan)
GET    tickets/<id>
POST   tickets                           {"prefix": "BUG", ...fields}  or  {"id": "BUG-13", ...}
PATCH  tickets/<id>                      {...fields to change}
DELETE tickets/<id>
GET    meta                              statuses, priorities, fields, config (unblockd.json)

No auth: it binds to 127.0.0.1 by default. Don't expose it beyond your machine.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import boards as bd
from . import chat
from . import store as st

STATIC = Path(__file__).resolve().parent.parent / "static"
BOARDS = bd.from_env()
MAX_BODY = 1_000_000


def _num(q: dict, key: str, default: int) -> int:
	v = q.get(key) or str(default)
	if not v.isdigit():
		raise st.TicketError(f"{key} must be a number")
	return int(v)


class Handler(BaseHTTPRequestHandler):
	server_version = "unblockd"

	def _send(self, code: int, payload, ctype="application/json; charset=utf-8"):
		data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
		self.send_response(code)
		self.send_header("Content-Type", ctype)
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(data)

	def _body(self) -> dict:
		n = int(self.headers.get("Content-Length") or 0)
		if n > MAX_BODY:
			raise st.TicketError("request too large")
		try:
			return json.loads(self.rfile.read(n) or b"{}")
		except json.JSONDecodeError as e:
			raise st.TicketError(f"body is not JSON: {e}")

	def _route(self, method: str):
		url = urlparse(self.path)
		parts = [p for p in url.path.split("/") if p]
		q = {k: v[0] for k, v in parse_qs(url.query).items()}
		try:
			if method == "GET" and parts in ([], ["index.html"]):
				return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
			if parts[:1] != ["api"]:
				return self._send(404, {"error": "not found"})
			if parts == ["api", "boards"]:
				if method == "GET":
					return self._send(200, BOARDS.summary())
				if method == "POST":
					b = self._body()
					return self._send(201, BOARDS.create(str(b.get("id", "")), str(b.get("title", ""))))
				return self._send(405, {"error": "method not allowed"})
			if parts == ["api", "chat"]:
				if method == "GET":
					return self._send(200, chat.enabled())
				if method == "POST":
					b = self._body()
					try:
						return self._send(200, chat.ask(BOARDS, b.get("board") or None, b.get("messages") or []))
					except chat.ChatError as e:
						return self._send(503, {"error": str(e)})
				return self._send(405, {"error": "method not allowed"})
			board, rest = (parts[2], parts[3:]) if parts[1:2] == ["b"] and len(parts) > 2 else (None, parts[1:])
			store = BOARDS.get(board)
			if method == "GET" and rest == ["meta"]:
				return self._send(200, {"statuses": st.STATUSES, "priorities": st.PRIORITIES, "fields": list(st.FIELDS),
				                        "config": store.config, "board": board or next(iter(BOARDS.all()))})
			if method == "GET" and rest == ["next"]:
				return self._send(200, store.next_ready(owner=q.get("owner"), pillar=q.get("pillar"), agent=q.get("agent"),
				                                        tag=q.get("tag"), limit=_num(q, "limit", 5)))
			if method == "GET" and rest == ["plan"]:
				return self._send(200, store.work_plan(per_agent=_num(q, "per_agent", 3)))
			if rest[:1] != ["tickets"] or len(rest) > 2:
				return self._send(404, {"error": "not found"})
			tid = rest[1] if len(rest) == 2 else None
			if method == "GET" and tid is None:
				return self._send(200, store.list(**{k: q.get(k) for k in ("status", "pillar", "owner", "priority", "q", "tag", "agent")}))
			if method == "GET":
				return self._send(200, store.get(tid))
			if method == "POST" and tid is None:
				b = self._body()
				return self._send(201, store.create(b, tid=b.pop("id", None), prefix=b.pop("prefix", None)))
			if method == "PATCH" and tid:
				return self._send(200, store.update(tid, self._body()))
			if method == "DELETE" and tid:
				return self._send(200, store.delete(tid))
			return self._send(405, {"error": "method not allowed"})
		except st.NotFound as e:
			return self._send(404, {"error": str(e)})
		except st.TicketError as e:
			return self._send(400, {"error": str(e)})

	def do_GET(self): self._route("GET")
	def do_POST(self): self._route("POST")
	def do_PATCH(self): self._route("PATCH")
	def do_DELETE(self): self._route("DELETE")

	def log_message(self, fmt, *args):  # quieter: one line per write only
		if self.command != "GET":
			super().log_message(fmt, *args)


def main():
	host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8787"))
	print(f"unblockd on http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}  boards: "
	      + (", ".join(f"{k} ({v})" for k, v in BOARDS.all().items()) or "none yet"), flush=True)
	ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
	main()
