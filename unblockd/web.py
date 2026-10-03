"""Board UI + REST API. Stdlib only: `python -m unblockd.web`.

GET    /api/tickets?status=&pillar=&owner=&priority=&q=
GET    /api/next?owner=&pillar=&limit=   ready tickets ranked by priority x tickets unblocked
GET    /api/tickets/<id>
POST   /api/tickets            {"prefix": "BUG", ...fields}  or  {"id": "BUG-13", ...}
PATCH  /api/tickets/<id>       {...fields to change}
DELETE /api/tickets/<id>
GET    /api/meta               statuses, priorities, fields, config (unblockd.json)

No auth: it binds to 127.0.0.1 by default. Don't expose it beyond your machine.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store as st

STATIC = Path(__file__).resolve().parent.parent / "static"
STORE = st.from_env()
MAX_BODY = 1_000_000


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
		try:
			if method == "GET" and parts in ([], ["index.html"]):
				return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
			if method == "GET" and parts == ["api", "next"]:
				q = {k: v[0] for k, v in parse_qs(url.query).items()}
				try:
					limit = int(q.get("limit") or 5)
				except ValueError:
					raise st.TicketError("limit must be a number")
				return self._send(200, STORE.next_ready(owner=q.get("owner"), pillar=q.get("pillar"), limit=limit))
			if parts[:2] != ["api", "tickets"] and parts != ["api", "meta"]:
				return self._send(404, {"error": "not found"})
			if parts == ["api", "meta"]:
				return self._send(200, {"statuses": st.STATUSES, "priorities": st.PRIORITIES, "fields": list(st.FIELDS), "config": STORE.config})
			tid = parts[2] if len(parts) == 3 else None
			if len(parts) > 3:
				return self._send(404, {"error": "not found"})
			if method == "GET" and tid is None:
				q = {k: v[0] for k, v in parse_qs(url.query).items()}
				return self._send(200, STORE.list(**{k: q.get(k) for k in ("status", "pillar", "owner", "priority", "q")}))
			if method == "GET":
				return self._send(200, STORE.get(tid))
			if method == "POST" and tid is None:
				b = self._body()
				return self._send(201, STORE.create(b, tid=b.pop("id", None), prefix=b.pop("prefix", None)))
			if method == "PATCH" and tid:
				return self._send(200, STORE.update(tid, self._body()))
			if method == "DELETE" and tid:
				return self._send(200, STORE.delete(tid))
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
	print(f"unblockd on http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}  tickets: {STORE.dir.resolve()}", flush=True)
	ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
	main()
