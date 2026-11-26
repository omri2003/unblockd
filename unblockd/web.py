"""Board UI + REST API. Stdlib only: `python -m unblockd.web`.

GET    /api/boards                       every board: id, title, ticket counts
POST   /api/boards                       {"id": "my-game", "title": "My game", "root"?}  (needs UNBLOCKD_ROOT)
GET    /api/needs                        every board's tickets that wait on a person (read-only, for the UI's Needs you panel):
                                         [{"id", "title", "items": [{"id", "kind": question|review|proposal|stuck|late_note, "title", "priority", "score", "unblocks",
                                         "since", "waiting_on", "missing_blockers", "stale_days" (only when stale), "done_when", "result", "body" (last 6000 chars), "notes" (newest 10),
                                         "late"? (review / late_note: notes that came after the work was finished, Store.late_notes)}]}], boards with items only,
                                         items: score desc, then oldest first
GET    /api/chat                         {"enabled": bool, "model": ...}
POST   /api/chat                         {"board": id, "messages": [{"role", "content"}]} -> {"reply", "tools", "refs"}
Per board, under /api/b/<board>/ ; the same paths without the prefix use the default (first) board:
GET    tickets?status=&pillar=&owner=&priority=&tag=&agent=&parent=&milestone=&q=   (milestone=- : tickets with none)
GET    next?owner=&pillar=&agent=&tag=&limit=   ready tickets ranked by priority x tickets unblocked
GET    plan?per_agent=                   ready tickets grouped by agent profile (work_plan)
GET    tickets/<id>
POST   tickets                           {"prefix": "BUG", ...fields}  or  {"id": "BUG-13", ...}; "agent"? = the creating worker (strict schema, proposal gate); a human's ticket may come back with "warnings"
POST   tickets/batch                     {"tickets": [{prefix|id, ...fields}, ...], "agent"?}  up to 50, all or nothing; blocked_by / parent may say "#N" (the Nth item) -> {"tickets", "ids": {"#1": id}, "warnings"?}
POST   tickets/<id>/accept               {"by"?}  accept an agent's proposal (400 unless proposed_by is set)
POST   tickets/<id>/approve              {"by"?}  review -> done, recording result.approved_by / approved_at (400 unless status is review; not the completer, not a worker)
PATCH  tickets/<id>                      {...fields to change, "by"?}; If-Match: <rev> (optional) = 409 if the ticket was written since you read it; to done with no result = `warnings` (finish with complete)
POST   tickets/<id>/report               {"by"?, "summary", "files"?, "refs"?}  a person adds the summary a done ticket was closed without (400 unless done with no summary)
DELETE tickets/<id>                      ?agent= (holder rule), ?reason= (a human rejecting a proposal; logged)
GET    meta                              statuses, priorities, fields, config (unblockd.json), version
GET    agents                            who is working what right now + recent activity
GET    activity?ticket=&limit=&since=    the claim / heartbeat / release / expired / complete / created / done log, newest first;
                                         since=<ISO time> keeps only newer events (the "While you were away" digest)
POST   claim                             {"agent", "profile"?, "note"?, "principal"?: a human owner, "branch"?: your worktree's git branch, "wait_s"?: <= 120}
                                         best claimable ticket, or {ticket: null, reason...} (conflict: held_by + retry_after_s; wait_s = wait for a conflicting claim to free up)
POST   tickets/<id>/claim                {"agent", "note"?, "principal"?, "branch"?, "wait_s"?: wait for an overlapping claim to end}
POST   tickets/<id>/heartbeat            {"agent", "note"?, "principal"?, "until"? (ISO: keep the claim alive until then, capped at lease_max_minutes)}
                                         claim / heartbeat / release / complete / split also take "session"? (or header X-Unblockd-Session):
                                         the worker process's id; another session under the claim's name is warned (CORE-88)
POST   tickets/<id>/release              {"agent", "reason"?, "principal"?}
POST   tickets/<id>/ask                  {"agent", "question"}  hand a decision to a human: ends the claim, sets waiting_on
POST   tickets/<id>/notes                {"by"?, "text", "kind"?, "pin"?, "reply_to"?, "supersedes"?}  append a signed note to the ticket's notes (by defaults to "human") -> the ticket
POST   tickets/<id>/answer               {"by"?, "text"}  a human answers an ask: note "Answer to: ...", clears waiting_on (400 unless waiting_on is set)
POST   tickets/<id>/dismiss              {"by"?, "notes": [n, ...], "undo"?}  a person read late notes on a finished ticket: Needs you stops listing them
POST   tickets/<id>/reject               {"by"?, "reason"}  review -> todo with the reason as a note (400 unless status is review)
POST   tickets/<id>/complete             {"agent", "summary", "files"?: [paths], "refs"?: [PR/commit/URL], "handoff"?: text, "principal"?, "followup"?: {title, done_when?, blocked_by?, body?, priority?, touches?}, "closes"?: [ids]}  also stored as the ticket's `result`; followup = the rest as a new ticket; closes = other tickets this work finished (done under it, all or nothing)
POST   tickets/<id>/split                {"titles": [...], "sequential"?, "by"?}  too big: make children, the parent waits on them -> {"parent", "children"}
GET    tickets/<id>/context              docs + attached files to read before working it (?content=0: each doc's `sent` without its text; a sent doc over a third of the budget also gets `outline` + `outline_chars`; when a doc did not fit, `fits_budget` = the smallest budget that sends all)
GET    knowledge                         who reads what: tickets, docs, ticket->doc (with the tag / profile it came from) and doc->doc edges, the rules that send docs, plus no_docs / broken / hubs / orphans
GET    files                             project file paths (no .git / node_modules / tickets/, capped at 3000): what the form suggests for docs and touches
GET    tools                             worker tool sets: {all, default, base, profiles: [{name, tools, tools_off, effective}]}
PUT    tools                             {"base": [...], "profiles": {name: {"tools", "tools_off"}}, "by"?}  save them to unblockd.json -> the same
GET    roadmap                           the milestones in order with progress counts, the next one, and the unscheduled counts
GET    architecture                      the architecture folder: components (+ open tickets, counts), edges with their contracts, ticket_map, problems
POST   architecture/components           {"key", "title", "summary"?, "paths"?: [globs], "by"?}  new note <folder>/<key>.md (the folder is created) -> the architecture payload
PATCH  architecture/components/<key>     {"title"?, "summary"?, "paths"?, "by"?}  only the managed parts of the note change
DELETE architecture/components/<key>?by= 400 while an open ticket lists it in `components`; else deletes the note and the lines in other notes that link to it
PUT    architecture/edges                {"from", "to", "text"?, "by"?}  create or replace the Connections line `- [<to title>](<to>.md) — text`
DELETE architecture/edges?from=&to=&by=  remove that line     (by defaults to human; each write logs one `arch` activity event)
GET    docs?path=&path=[&budget=]        several docs in ONE reply within context_budget {docs, budget, used, note?}
GET    doc?path=                         one text file from the project repo (+ its links)
GET    tickets/<id>/files/<name>         an attached file  {"name", "content"}
POST   tickets/<id>/files                {"name", "content"}  attach a text file
DELETE tickets/<id>/files/<name>

Writes (POST/PATCH/PUT) must send Content-Type: application/json (else 415); an Origin header, when present, must be this server (else 403).
GET    /healthz                          (no /api, never needs auth) {"status": "ok", "version"}: container health / k8s probes
Auth: none by default (binds to 127.0.0.1; a non-loopback HOST without a token refuses to start unless UNBLOCKD_ALLOW_OPEN=1). With UNBLOCKD_TOKEN set, /api/* needs `Authorization: Bearer <token>`
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

from . import __version__
from . import boards as bd
from . import chat
from . import branches
from . import connectors
from . import files
from . import github
from . import knowledge as kn
from . import notify
from . import store as st

STATIC = Path(__file__).resolve().parent / "static"  # an installed package (pyproject.toml) carries the UI here
if not (STATIC / "index.html").is_file():
	STATIC = STATIC.parent.parent / "static"  # a checkout (and the image, where compose mounts ./static)
BOARDS = bd.from_env()
MAX_BODY = 1_000_000


def _num(q: dict, key: str, default: int) -> int:
	v = q.get(key) or str(default)
	if not v.isdigit():
		raise st.TicketError(f"{key} must be a number")
	return int(v)


# CORE-65: an unchanged 4 s poll used to json.dumps the whole board (1.2 MB on a 527-ticket board) and md5 it just to answer 304.
# The body and its ETag are kept per board, keyed by Store.board_sig() (what _board() caches under, so a database board works
# too); what an idle poll still costs is the board read (files: the stat pass; databases: the version query).
_BODY: dict[str, tuple] = {}  # board key -> (board signature, body bytes, ETag)
_NEEDS: dict = {}  # requested board (None = every board) -> (key, body, ETag) of the last /api/needs for it; entries replaced whole
TICKET_FILTERS = ("status", "pillar", "owner", "priority", "q", "tag", "agent", "parent", "milestone", "ids")  # ids=A-1,B-2 (API-23)


def _etag(data: bytes) -> str:
	return f'"{hashlib.md5(data).hexdigest()}"'


def _tickets_body(store) -> tuple[bytes, str]:
	"""GET tickets with no filter = store.list(), serialized once per board change."""
	sig, board = store.board_sig()
	hit = _BODY.get(store._key)
	if sig is not None and hit and hit[0] == sig:
		return hit[1], hit[2]
	data = json.dumps(list(board.values()), ensure_ascii=False).encode()  # what list() returns, without its copies
	etag = _etag(data)
	if sig is not None:
		_BODY[store._key] = (sig, data, etag)  # the ETag stored WITH the data it describes
	return data, etag


def _needs_body(board: str | None = None) -> tuple[bytes, str]:
	"""/api/needs, serialized again only when its inputs changed: per board the board signature, the activity log's cursor
	(failing(); files: activity.jsonl's size, databases: the event counter) and the PR cache (github). Any connector. A board
	whose signature can't be read (a racing writer) means no reuse. `board` = that board only (API-18; unknown = NotFound),
	None = every board; each request kind has its own entry, so one board's body never answers another's request."""
	boards, key, every = [], [], BOARDS.all()
	if board is not None:
		if board not in every:
			raise st.NotFound(f"no board {board!r}. Boards: {', '.join(every)}")
		every = {board: every[board]}
	for bid, path in every.items():
		try:
			s = BOARDS.open(path)
			sig, _ = s.board_sig()
			prs = s.backend.get_doc("prs")
			key.append(None if sig is None else (bid, sig, s.backend.events_after(None)[1], prs))
		except st.TicketError:
			key.append((bid, "broken"))
			continue
		boards.append((bid, s, prs))
	key = None if None in key else tuple(key)
	hit = _NEEDS.get(board)
	if key is not None and hit and hit[0] == key:
		return hit[1], hit[2]
	data = json.dumps(_needs(boards), ensure_ascii=False).encode()
	etag = _etag(data)
	_NEEDS[board] = (key, data, etag)  # key None = never matches; the ETag stored WITH its data
	return data, etag


def _needs(boards: list | None = None) -> list[dict]:
	"""Per board, Store.needs() (the items MCP needs_you returns too, via Boards.needs): [{id, title, items}], boards with items only.
	boards = [(board id, Store, PR cache)] already opened by _needs_body; None = every board. A broken config hides only its own board."""
	out = []
	for bid, s, prs in boards if boards is not None else ((b, p, None) for b, p in BOARDS.all().items()):
		try:
			s = s if isinstance(s, st.Store) else BOARDS.open(s)
			items = s.needs(prs)
		except st.TicketError:
			continue
		if items:
			out.append({"id": bid, "title": s.config["title"], "items": items})
	return out


class Handler(BaseHTTPRequestHandler):
	server_version = "unblockd"

	def _send(self, code: int, payload, ctype="application/json; charset=utf-8", etag: str | None = None):
		"""etag = already computed with the body (a cached one, see _tickets_body); else md5 of the body for a 200 GET."""
		data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
		if etag is None and code == 200 and self.command == "GET":
			etag = _etag(data)
		if etag and etag in [x.strip() for x in (self.headers.get("If-None-Match") or "").split(",")]:
			code, data = 304, b""  # unchanged: the 4 s poll costs headers, not the whole board
		self.send_response(code)
		self.send_header("Content-Type", ctype)
		if etag:
			self.send_header("ETag", etag)
		self.send_header("Content-Length", str(len(data)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.send_header("Content-Security-Policy", "frame-ancestors 'none'")  # no clickjacking; nothing else restricted (the UI is inline)
		self.end_headers()
		self.wfile.write(data)

	def _authed(self, token: str) -> bool:
		bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
		cookie = SimpleCookie(self.headers.get("Cookie") or "").get("unblockd_token")
		return any(c and hmac.compare_digest(c.encode(), token.encode()) for c in (bearer, cookie and cookie.value))

	def _body(self) -> dict:
		try:
			n = int(self.headers.get("Content-Length") or 0)
		except ValueError:
			n = -1
		if n < 0:
			raise st.TicketError("bad Content-Length")
		if n > MAX_BODY:
			raise st.TicketError("request too large")
		try:
			return json.loads(self.rfile.read(n) or b"{}")
		except json.JSONDecodeError as e:
			raise st.TicketError(f"body is not JSON: {e}")

	def _session(self, b: dict):
		"""The worker session id of a claim / heartbeat / release / complete (CORE-88): body key `session`, else header X-Unblockd-Session."""
		return b.get("session") or self.headers.get("X-Unblockd-Session") or None

	def _arch(self, store, method: str, rest: list[str], q: dict):
		"""The architecture writes (docs/reference.md): (status, payload). The store's TicketError becomes a 400 in _route."""
		by = q.get("by") or "human"
		if rest[1] == "edges":
			if method == "PUT":
				b = self._body()
				return 200, kn.set_edge(store, b.get("from"), b.get("to"), b.get("text") or "", by=b.get("by") or "human")
			if method == "DELETE":
				return 200, kn.remove_edge(store, q.get("from"), q.get("to"), by=by)
		elif len(rest) == 2 and method == "POST":
			b = self._body()
			return 201, kn.add_component(store, b.get("key"), b.get("title"), b.get("summary") or "", b.get("paths") or (), by=b.get("by") or "human")
		elif len(rest) == 3 and method == "PATCH":
			b = self._body()
			return 200, kn.update_component(store, rest[2], by=b.get("by") or "human", title=b.get("title"), summary=b.get("summary"), paths=b.get("paths"))
		elif len(rest) == 3 and method == "DELETE":
			return 200, kn.delete_component(store, rest[2], by=by)
		return 405, {"error": "method not allowed"}

	def _scheme(self) -> str:
		return "https" if (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip() == "https" else "http"

	def _write_refused(self, method: str):
		"""(status, message) when a write could be a cross-site request, else None. A page on another site can POST text/plain
		(or form data) to localhost without a preflight, so writes must be JSON from the same origin. DELETE has no simple form,
		so it needs a preflight anyway and may carry no body."""
		if method == "GET":
			return None
		origin = self.headers.get("Origin")
		if origin and origin != f"{self._scheme()}://{self.headers.get('Host')}":
			return 403, f"cross-origin write refused (Origin {origin} is not this server)"
		if method != "DELETE" and not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
			return 415, "writes must send Content-Type: application/json"
		return None

	def _route(self, method: str):
		url = urlparse(self.path)
		parts = [p for p in url.path.split("/") if p]
		q = {k: v[0] for k, v in parse_qs(url.query).items()}
		token = os.environ.get("UNBLOCKD_TOKEN")
		try:
			if token and hmac.compare_digest(q.get("token", "").encode(), token.encode()):
				rest_q = urlencode({k: v for k, v in q.items() if k != "token"})
				self.send_response(302)
				self.send_header("Set-Cookie", f"unblockd_token={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000" + ("; Secure" if self._scheme() == "https" else ""))
				self.send_header("Location", url.path + ("?" + rest_q if rest_q else ""))
				self.send_header("Content-Length", "0")
				return self.end_headers()
			if method == "GET" and parts in ([], ["index.html"]):
				return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
			if method == "GET" and parts == ["healthz"]:  # liveness/readiness probe: no auth, nothing sensitive
				return self._send(200, {"status": "ok", "version": __version__})
			if parts[:1] != ["api"]:
				return self._send(404, {"error": "not found"})
			if bad := self._write_refused(method):
				return self._send(bad[0], {"error": bad[1]})
			if token and not self._authed(token):
				return self._send(401, {"error": "unauthorized: open the board once with ?token=<UNBLOCKD_TOKEN> or send Authorization: Bearer <token>"})
			if parts == ["api", "boards"]:
				if method == "GET":
					return self._send(200, BOARDS.summary())
				if method == "POST":
					b = self._body()
					return self._send(201, BOARDS.create(str(b.get("id", "")), str(b.get("title", "")), b.get("root") or None))
				return self._send(405, {"error": "method not allowed"})
			if parts == ["api", "needs"]:
				if method != "GET":
					return self._send(405, {"error": "method not allowed"})
				data, etag = _needs_body(q.get("board") or None)  # API-18: ?board= = that board; reused while no board, activity log or PR cache changed (CORE-65)
				return self._send(200, data, etag=etag)
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
				                        "config": store.config, "board": board or next(iter(BOARDS.all())), "version": __version__,
				                        # UI-81: a slow Docker Desktop mount (files boards only; a database board has no folder)
				                        "storage": files.storage(store.backend.dir, store.backend.key) if getattr(store.backend, "kind", "") == "files" else None})
			if method == "GET" and rest == ["git"]:  # how the board's checkout compares with origin (branches.py); {} fields when git evidence is off
				d = branches.load(store.rt) if store.rt else {}
				return self._send(200, {k: d.get(k) for k in ("source", "remote", "base", "checkout", "error", "since")})
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
				return self._send(200, store.claim_next(str(b.get("agent", "")), profile=b.get("profile"), note=str(b.get("note", "")), principal=b.get("principal"),
				                                        branch=b.get("branch"), wait_s=b.get("wait_s") or 0, session=self._session(b)))
			if method == "GET" and rest == ["knowledge"]:
				return self._send(200, kn.graph(store))
			if method == "GET" and rest == ["files"]:
				return self._send(200, kn.repo_files(store))
			if method == "GET" and rest == ["tools"]:
				return self._send(200, store.tools_view())
			if method == "PUT" and rest == ["tools"]:
				b = self._body()
				return self._send(200, store.set_tools(b.get("base"), b.get("profiles") or {}, str(b.get("by") or "human")))
			if method == "GET" and rest == ["roadmap"]:
				return self._send(200, store.roadmap())
			if method == "GET" and rest == ["architecture"]:
				return self._send(200, kn.architecture(store))
			if rest[:2] == ["architecture", "components"] and len(rest) <= 3 or rest == ["architecture", "edges"]:
				return self._send(*self._arch(store, method, rest, q))
			if method == "GET" and rest == ["docs"]:  # several docs in one reply (AI-45): docs?path=a&path=b[&budget=]
				return self._send(200, kn.read_docs(store, parse_qs(url.query).get("path", []), budget=int(q["budget"]) if q.get("budget", "").isdigit() else None))
			if method == "GET" and rest == ["doc"]:
				return self._send(200, kn.read_doc(store, q.get("path", "")))
			if rest[:1] == ["tickets"] and len(rest) == 3 and rest[2] == "context" and method == "GET":
				c = kn.context(store, rest[1], inline=q.get("inline") != "0")
				if q.get("content") == "0":  # what would be sent, without the text (the Docs view's preview)
					for d in c["docs"]:
						d.pop("content", None)
					kn.preview_outlines(store, c)  # a sent doc with 2+ headings can be trimmed to sections (UI-72, UI-75)
					if (f := kn.fits_budget(store, rest[1], c)):  # a doc did not fit: the budget at which everything would (UI-64)
						c["fits_budget"] = f
				return self._send(200, c)
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
					return self._send(200, store.claim(agent, tid=tid, note=str(b.get("note", "")), principal=b.get("principal"), branch=b.get("branch"),
					                                   wait_s=b.get("wait_s") or 0, session=self._session(b)))
				if act == "heartbeat":
					box = store.inbox(agent, mark=True)
					t = store.heartbeat(tid, agent, str(b.get("note", "")), b.get("principal"), until=b.get("until") or None, session=self._session(b))
					return self._send(200, {**t, **({"inbox": box} if box else {})})
				if act == "release":
					return self._send(200, store.release(tid, agent, str(b.get("reason", "")), b.get("principal"), missing=str(b.get("missing") or ""), why=str(b.get("why") or ""), request_id=b.get("request_id"), session=self._session(b)))
				if act == "ask":
					return self._send(200, store.ask(tid, agent, str(b.get("question", "")), request_id=b.get("request_id")))
				return self._send(200, store.complete(tid, agent, str(b.get("summary", "")), files=b.get("files"), refs=b.get("refs"), handoff=b.get("handoff"), principal=b.get("principal"), request_id=b.get("request_id"), followup=b.get("followup"), session=self._session(b), closes=b.get("closes")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] == "notes":
				b = self._body()
				return self._send(200, store.add_note(rest[1], str(b.get("by") or "human"), b.get("text"), kind=b.get("kind") or "note", pin=bool(b.get("pin")),
				                                        reply_to=b.get("reply_to"), supersedes=b.get("supersedes"), request_id=b.get("request_id")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] in ("answer", "reject", "accept", "approve"):
				b = self._body()
				by = str(b.get("by") or "human")
				if rest[2] == "accept":
					return self._send(200, store.accept(rest[1], by))
				if rest[2] == "approve":
					return self._send(200, store.approve(rest[1], by))
				return self._send(200, store.answer(rest[1], by, b.get("text")) if rest[2] == "answer" else store.reject(rest[1], by, b.get("reason")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] == "dismiss":
				b = self._body()
				return self._send(200, store.dismiss_notes(rest[1], str(b.get("by") or "human"), b.get("notes"), undo=bool(b.get("undo"))))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] == "report":
				b = self._body()
				return self._send(200, store.report(rest[1], str(b.get("by") or "human"), b.get("summary"), files=b.get("files"), refs=b.get("refs")))
			if method == "POST" and len(rest) == 3 and rest[0] == "tickets" and rest[2] == "split":
				b = self._body()
				return self._send(200, store.split(rest[1], b.get("titles") or [], sequential=bool(b.get("sequential")), by=b.get("by") or b.get("agent"),
				                                    parts=b.get("parts"), claim=b.get("claim"), principal=b.get("principal"), branch=b.get("branch"), session=self._session(b)))
			if method == "POST" and rest == ["tickets", "batch"]:
				b = self._body()
				return self._send(201, store.create_many(b.get("tickets"), agent=b.get("agent")))
			if rest[:1] != ["tickets"] or len(rest) > 2:
				return self._send(404, {"error": "not found"})
			tid = rest[1] if len(rest) == 2 else None
			if method == "GET" and tid is None:
				if not any(q.get(k) for k in (*TICKET_FILTERS, "touches", "component", "fields", "limit", "stale_days")):  # the UI's poll: cached body + ETag while the board is unchanged (CORE-65)
					data, etag = _tickets_body(store)
					return self._send(200, data, etag=etag)
				# fields=a,b: summary rows with those keys (AI-47); touches= / component= add touches + holder (CORE-52); neither = whole tickets
				keep = st.list_fields(q.get("fields"), matched=bool(q.get("touches") or q.get("component")), idle=bool(q.get("stale_days")) and bool(q.get("fields")))
				res = store.list(**{k: q.get(k) for k in (*TICKET_FILTERS, "touches", "component")}, limit=_num(q, "limit", 0) if q.get("limit") else None,
				                 stale_days=_num(q, "stale_days", 0) if q.get("stale_days") else None)  # AI-57: todo tickets idle that long
				return self._send(200, [st.summarize(t, keep) for t in res] if keep else res)
			if method == "GET":
				return self._send(200, store.with_blockers(store.get(tid)))
			if method == "POST" and tid is None:
				b = self._body()
				return self._send(201, store.create(b, tid=b.pop("id", None), prefix=b.pop("prefix", None), agent=b.pop("agent", None), request_id=b.pop("request_id", None)))  # agent: the creating worker (cli.Remote); none = a human
			if method == "PATCH" and tid is None:  # update_many: {"tickets": [{id, changes, expected_rev?}], "agent"?}, all or nothing
				b = self._body()
				return self._send(200, store.update_many(b.get("tickets"), agent=b.get("agent"), by=b.get("by")))
			if method == "PATCH" and tid:
				b = self._body()
				m = (self.headers.get("If-Match") or "").strip().strip('"')  # the rev the client last saw; none = last write wins, as before
				if m and not m.isdigit():
					raise st.TicketError("If-Match must be the ticket's rev (a number)")
				return self._send(200, store.update(tid, b, agent=b.pop("agent", None), expected_rev=int(m) if m else None, by=b.pop("by", None)))  # agent: a remote worker's name (cli.Remote); by: who (logged on done)
			if method == "DELETE" and tid:
				return self._send(200, store.delete(tid, agent=q.get("agent"), reason=q.get("reason", "")))
			return self._send(405, {"error": "method not allowed"})
		except st.NotFound as e:
			return self._send(404, {"error": str(e)})
		except st.Conflict as e:
			return self._send(409, {"error": str(e)})
		except st.TicketError as e:
			return self._send(400, {"error": str(e)})

	def do_GET(self): self._route("GET")
	def do_POST(self): self._route("POST")
	def do_PATCH(self): self._route("PATCH")
	def do_PUT(self): self._route("PUT")
	def do_DELETE(self): self._route("DELETE")

	def log_message(self, fmt, *args):  # quieter: one line per write only
		if self.command != "GET":
			super().log_message(fmt, *args)


def main():
	host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8787"))
	st.refuse_open_bind(host, os.environ.get("UNBLOCKD_TOKEN"))
	files.SCAN_REUSE_S = 1.0  # one page load reads a files board ~4 times: share one stat pass (CORE-60); a write by this server drops it at once
	print(f"unblockd on http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}  boards: "
	      + (", ".join(f"{k} ({connectors.describe(str(v))})" for k, v in BOARDS.all().items()) or "none yet"), flush=True)
	if notify.start(BOARDS):
		print("notifications: webhook on", flush=True)
	if github.start(BOARDS):
		print("github: syncing PRs of tickets in review", flush=True)
	if branches.start(BOARDS):
		print("branches: reading unmerged branches from git (UNBLOCKD_GIT=0 turns it off)", flush=True)
	ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
	main()
