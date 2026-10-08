"""Webhook notifications for a human who leaves agents running. Off unless UNBLOCKD_WEBHOOK is set.

One daemon thread in the web server polls each board's tickets/.unblockd/activity.jsonl (agents write it from their own
CLI / MCP processes, so hooking the store's logger would miss them) and posts a batched message per board.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request

INTERVAL = 30  # seconds between polls
MAX_LINES = 10


def _line(board: str, e: dict, titles: dict) -> str | None:
	ev, tid, who, note = e.get("event"), e.get("ticket"), e.get("agent"), (e.get("note") or "").strip()
	if ev == "complete":
		first = note.splitlines()[0] if note else ""
		return f"✅ {board}: {tid} {titles.get(tid, '')} finished by {who}" + (f" — {first}" if first else "")
	if ev in ("expired", "release"):
		return f"⚠ {board}: {tid} stuck — {who} {note}".rstrip()
	if ev == "question":
		return f"❓ {board}: {tid} {titles.get(tid, '')} needs a human — {who} asks: {note}"
	if ev == "answered":
		return f"💬 {board}: {tid} {titles.get(tid, '')} answered by {who} — {note}"
	if ev == "rejected":
		return f"↩ {board}: {tid} {titles.get(tid, '')} rejected by {who} — {note}"
	if ev == "ran_dry":
		return f"🏁 {board} ran dry: no ready tickets and no agent working"
	return None


class Notifier:
	def __init__(self, boards, url: str, fmt: str = "json", public_url: str = ""):
		self.boards, self.url, self.fmt, self.public = boards, url, fmt, public_url.rstrip("/")
		self.offset: dict[str, int] = {}  # activity.jsonl path -> bytes already handled
		self.armed: dict[str, bool] = {}  # board -> may announce "ran dry" the next time it is dry
		self.failing = False

	def poll(self) -> None:
		for bid, tdir in self.boards.all().items():
			try:
				self._board(bid, tdir)
			except Exception as e:  # one broken board must not stop the others
				print(f"notify: {bid}: {e}", flush=True)

	@staticmethod
	def _read(p, off: int, size: int) -> tuple[list, int]:
		with open(p, "rb") as f:
			f.seek(off)
			chunk = f.read(size - off)
		chunk = chunk[:chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next tick
		events = []
		for raw in chunk.decode("utf-8", "replace").splitlines():
			try:
				events.append(json.loads(raw))
			except json.JSONDecodeError:
				pass
		return events, off + len(chunk)

	def _board(self, bid: str, tdir) -> None:
		store = self.boards.get(bid)
		ts = store.list()  # also sweeps expired claims, which logs `expired` even when nobody has the UI open
		p = tdir / ".unblockd" / "activity.jsonl"
		size = p.stat().st_size if p.exists() else 0
		off = self.offset.setdefault(str(p), size)  # first sight: start at the end, never replay history
		events = []
		if size < off:  # rotated (or truncated): first finish the old file, which `_log` renamed to activity.1.jsonl
			old = p.with_name("activity.1.jsonl")
			if old.exists() and old.stat().st_size >= off:  # smaller = not our file (plain truncation)
				events += self._read(old, off, old.stat().st_size)[0]
			off = 0
		if size > off:
			new, off = self._read(p, off, size)
			events += new
		self.offset[str(p)] = off
		humans = {o["name"] for o in store.config["owners"] if o.get("kind") == "human"}
		busy = any(t.get("claim") for t in ts) or any(
			t["effective_status"] == "todo" and t.get("owner") not in humans for t in ts)
		if busy or events:
			self.armed[bid] = True
		if not busy and self.armed.get(bid):
			self.armed[bid] = False
			events.append({"event": "ran_dry"})
		titles = {t["id"]: t.get("title", "") for t in ts}
		sent = [(e, _line(bid, e, titles)) for e in events]
		sent = [(e, l) for e, l in sent if l]
		if sent:
			self._send(bid, [l for _, l in sent], [e for e, _ in sent])

	def _send(self, bid: str, lines: list[str], events: list[dict]) -> None:
		if len(lines) > MAX_LINES:
			lines = lines[:MAX_LINES] + [f"+{len(lines) - MAX_LINES} more"]
		msg = "\n".join(lines) + (f"\n{self.public}/?board={bid}" if self.public else "")
		if self.fmt == "text":
			data, ctype = msg.encode(), "text/plain; charset=utf-8"
		else:  # Slack reads `text`, Discord `content`
			data = json.dumps({"text": msg, "content": msg, "board": bid, "events": events}, ensure_ascii=False).encode()
			ctype = "application/json"
		try:
			req = urllib.request.Request(self.url, data=data, headers={"Content-Type": ctype}, method="POST")
			urllib.request.urlopen(req, timeout=5).close()
			self.failing = False
		except Exception as e:
			if not self.failing:  # once per failure streak
				print(f"notify: webhook failed ({e}); dropping messages until it recovers", flush=True)
			self.failing = True  # ponytail: failed batches are dropped, no retry queue


def start(boards) -> Notifier | None:
	url = os.environ.get("UNBLOCKD_WEBHOOK")
	if not url:
		return None
	n = Notifier(boards, url, os.environ.get("UNBLOCKD_WEBHOOK_FORMAT", "json"), os.environ.get("UNBLOCKD_PUBLIC_URL", ""))

	def loop():
		n.poll()  # primes the offsets
		while not threading.Event().wait(INTERVAL):
			n.poll()
	threading.Thread(target=loop, daemon=True, name="notify").start()
	return n
