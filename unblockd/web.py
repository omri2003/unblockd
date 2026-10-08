"""Board UI + REST API. Stdlib only: `python -m unblockd.web`.

GET    /api/boards                       every board: id, title, ticket counts
POST   /api/boards                       {"id": "my-game", "title": "My game"}  (needs UNBLOCKD_ROOT)
GET    /api/chat                         {"enabled": bool, "model": ...}
POST   /api/chat                         {"board": id, "messages": [{"role", "content"}]} -> {"reply", "tools", "refs"}
Per board, under /api/b/<board>/ ; the same paths without the prefix use the default (first) board:
GET    tickets?status=&pillar=&owner=&priority=&tag=&agent=&parent=&q=
GET    next?owner=&pillar=&agent=&tag=&limit=   ready tickets ranked by priority x tickets unblocked
GET    plan?per_agent=                   ready tickets grouped by agent profile (work_plan)
GET    tickets/<id>
POST   tickets                           {"prefix": "BUG", ...fields}  or  {"id": "BUG-13", ...}
PATCH  tickets/<id>                      {...fields to change}
DELETE tickets/<id>
GET    meta                              statuses, priorities, fields, config (unblockd.json)
GET    agents                            who is working what right now + recent activity
GET    activity?ticket=&limit=&since=    the claim / heartbeat / release / expired / complete / created / done log, newest first;
                                         since=<ISO time> keeps only newer events (the "While you were away" digest)
POST   claim                             {"agent", "profile"?, "note"?}  best claimable ticket, or null
POST   tickets/<id>/claim                {"agent", "note"?}
POST   tickets/<id>/heartbeat            {"agent", "note"?}
POST   tickets/<id>/release              {"agent", "reason"?}
POST   tickets/<id>/ask                  {"agent", "question"}  hand a decision to a human: ends the claim, sets waiting_on
POST   tickets/<id>/notes                {"by"?, "text"}  append a signed line to the body (by defaults to "human") -> the ticket
POST   tickets/<id>/answer               {"by"?, "text"}  a human answers an ask: note "Answer to: ...", clears waiting_on (400 unless waiting_on is set)
POST   tickets/<id>/reject               {"by"?, "reason"}  review -> todo with the reason as a note (400 unless status is review)
POST   tickets/<id>/complete             {"agent", "summary", "files"?: [paths], "refs"?: [PR/commit/URL], "handoff"?: text}  also stored as the ticket's `result`
POST   tickets/<id>/split                {"titles": [...], "sequential"?, "by"?}  too big: make children, the parent waits on them -> {"parent", "children"}
GET    tickets/<id>/context              docs + attached files to read before working it
GET    knowledge                         who reads what: tickets, docs, ticket->doc and doc->doc edges, plus no_docs / broken / hubs / orphans
GET    doc?path=                         one text file from the project repo (+ its links)
GET    tickets/<id>/files/<name>         an attached file  {"name", "content"}
POST   tickets/<id>/files                {"name", "content"}  attach a text file
DELETE tickets/<id>/files/<name>

Auth: none by default (binds to 127.0.0.1). With UNBLOCKD_TOKEN set, /api/* needs `Authorization: Bearer <token>`
or the `unblockd_token` cookie, which any page visited with `?token=<token>` sets. A shared secret, not per-user identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from . import boards as bd
from . import chat
from . import knowledge as kn
from . import notify
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
		etag = f'"{hashlib.md5(data).hexdigest()}"' if code == 200 and self.command == "GET" else None
		if etag and etag in [x.strip() for x in (self.headers.get("If-None-Match") or "").split(",")]:
			code, data = 304, b""  # unchanged: the 4 s poll costs headers, not the whole board
		self.send_response(code)
		self.send_header("Content-Type", ctype)
		if etag:
			self.send_header("ETag", etag)
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(data)

	def _authed(self, token: str) -> bool:
		bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
		cookie = SimpleCookie(self.headers.get("Cookie") or "").get("unblockd_token")
		return any(c and hmac.compare_digest(c.encode(), token.encode()) for c in (bearer, cookie and cookie.value))

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
		token = os.environ.get("UNBLOCKD_TOKEN")
		try:
			if token and hmac.compare_digest(q.get("token", "").encode(), token.encode()):
				rest_q = urlencode({k: v for k, v in q.items() if k != "token"})
				self.send_response(302)
				self.send_header("Set-Cookie", f"unblockd_token={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
				self.send_header("Location", url.path + ("?" + rest_q if rest_q else ""))
				self.send_header("Content-Length", "0")
				return self.end_headers()
			if method == "GET" and parts in ([], ["index.html"]):
				return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
			if parts[:1] != ["api"]:
				return self._send(404, {"error": "not found"})
			if token and not self._authed(token):
				return self._send(401, {"error": "unauthorized: open the board once with ?token=<UNBLOCKD_TOKEN> or send Authorization: Bearer <token>"})
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
			if method == "GET" and rest == ["agents"]:
				return self._send(200, store.agents_now())
			if method == "GET" and rest == ["activity"]:
				return self._send(200, store.activity(q.get("ticket"), _num(q, "limit", 50), q.get("since")))
			if method == "POST" and rest == ["claim"]:
				b = self._body()
				return self._send(200, store.claim_next(str(b.get("agent", "")), profile=b.get("profile"), note=str(b.get("note", ""))))
			if method == "GET" and rest == ["knowledge"]:
				return self._send(200, kn.graph(store))
			if method == "GET" and rest == ["doc"]:
				return self._send(200, kn.read_doc(store, q.get("path", "")))
			if rest[:1] == ["tickets"] and len(rest) == 3 and rest[2] == "context" and method == "GET":
				return self._send(200, kn.context(store, rest[1], inline=q.get("inline") != "0"))
			if rest[:1] == ["tickets"] and len(rest) in (3, 4) and rest[2] == "files":
				tid = rest[1]
				if method == "POST" and len(rest) == 3:
					b = self._body()
					return self._send(201, kn.attach(store, tid, str(b.get("name", "")), b.get("content")))
				if method == "GET" and len(rest) == 4:
					return self._send(200, {"name": rest[3], "content": kn.read_file(store, tid, rest[3])})
				if method == "DELETE" and len(rest) == 4:
					return self._send(200, kn.detach(store, tid, rest[3]))
				return self._send(405, {"error": "method not allowed"})
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] in ("claim", "heartbeat", "release", "ask", "complete"):
				b, tid, act = self._body(), rest[1], rest[2]
				agent = str(b.get("agent", ""))
				if act == "claim":
					return self._send(200, store.claim(agent, tid=tid, note=str(b.get("note", ""))))
				if act == "heartbeat":
					return self._send(200, store.heartbeat(tid, agent, str(b.get("note", ""))))
				if act == "release":
					return self._send(200, store.release(tid, agent, str(b.get("reason", ""))))
				if act == "ask":
					return self._send(200, store.ask(tid, agent, str(b.get("question", ""))))
				return self._send(200, store.complete(tid, agent, str(b.get("summary", "")), files=b.get("files"), refs=b.get("refs"), handoff=b.get("handoff")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] == "notes":
				b = self._body()
				return self._send(200, store.add_note(rest[1], str(b.get("by") or "human"), b.get("text")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] in ("answer", "reject"):
				b = self._body()
				by = str(b.get("by") or "human")
				return self._send(200, store.answer(rest[1], by, b.get("text")) if rest[2] == "answer" else store.reject(rest[1], by, b.get("reason")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] == "split":
				b = self._body()
				return self._send(200, store.split(rest[1], b.get("titles") or [], sequential=bool(b.get("sequential")), by=b.get("by")))
			if rest[:1] != ["tickets"] or len(rest) > 2:
				return self._send(404, {"error": "not found"})
			tid = rest[1] if len(rest) == 2 else None
			if method == "GET" and tid is None:
				return self._send(200, store.list(**{k: q.get(k) for k in ("status", "pillar", "owner", "priority", "q", "tag", "agent", "parent")}))
			if method == "GET":
				return self._send(200, store.get(tid))
			if method == "POST" and tid is None:
				b = self._body()
				return self._send(201, store.create(b, tid=b.pop("id", None), prefix=b.pop("prefix", None)))
			if method == "PATCH" and tid:
				b = self._body()
				return self._send(200, store.update(tid, b, agent=b.pop("agent", None)))  # agent: a remote worker's name (cli.Remote)
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
	if not os.environ.get("UNBLOCKD_TOKEN") and host not in ("127.0.0.1", "localhost", "::1"):
		print(f"WARNING: bound to {host} with no auth: set UNBLOCKD_TOKEN, or anyone who can reach the port can edit tickets", flush=True)
	print(f"unblockd on http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}  boards: "
	      + (", ".join(f"{k} ({v})" for k, v in BOARDS.all().items()) or "none yet"), flush=True)
	if notify.start(BOARDS):
		print("notifications: webhook on", flush=True)
	ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
	main()
