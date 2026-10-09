"""PR evidence from GitHub. Off unless GITHUB_TOKEN (or UNBLOCKD_GITHUB_TOKEN) is set.

One daemon thread in the web server (agents run in their own processes, so the store can't do this). Every INTERVAL s,
for each ticket in `review` whose result.refs hold a github.com PR URL: read the PR + its CI, cache the summary in
tickets/.unblockd/prs.json (runtime state, never the ticket JSON), and move the ticket to done when the PR is merged.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.request

from .store import _now

INTERVAL = 60
PR_URL = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?$")
FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "error"}


def load(tdir) -> dict:
	try:
		return json.loads((tdir / ".unblockd" / "prs.json").read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}


class Syncer:
	def __init__(self, boards, token: str, api: str = "https://api.github.com"):
		self.boards, self.token, self.api = boards, token, api.rstrip("/")
		self.failing = self.failed = False  # failing = last poll had errors; printed once per streak

	def _get(self, path: str):
		req = urllib.request.Request(self.api + path, headers={
			"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "User-Agent": "unblockd"})
		with urllib.request.urlopen(req, timeout=10) as r:
			return json.load(r)

	def poll(self) -> None:
		self.failed = False
		for bid, tdir in self.boards.all().items():
			try:
				self._board(bid, tdir)
			except Exception as e:  # one broken board must not stop the others
				print(f"github: {bid}: {e}", flush=True)
		self.failing = self.failed

	def _board(self, bid: str, tdir) -> None:
		store, old, new = self.boards.get(bid), load(tdir), {}
		for t in store.list():
			if t["status"] != "review":
				continue
			m = next((m for r in (t.get("result") or {}).get("refs") or [] if (m := PR_URL.match(r))), None)
			if not m:
				continue
			try:
				e = self._check(m, old.get(t["id"]) or {})
			except Exception as ex:
				if not self.failing:  # once per failure streak; the old cache entry stays
					print(f"github: {t['id']}: {ex}", flush=True)
				self.failed = True  # ponytail: no retry queue, the next tick tries again
				if t["id"] in old:
					new[t["id"]] = old[t["id"]]
				continue
			if e["merged"]:
				store.update(t["id"], {"status": "done"})
				store.add_note(t["id"], "github", f"PR #{e['number']} merged — moved to done")
				continue  # done now: no cache entry
			if e["state"] == "closed" and not e["noted"]:
				store.add_note(t["id"], "github", f"PR #{e['number']} was closed without merging; still in review")
				e["noted"] = True
			new[t["id"]] = e
		if new != old:
			d = tdir / ".unblockd"
			d.mkdir(exist_ok=True)
			(d / "prs.json").write_text(json.dumps(new, indent=1), encoding="utf-8")

	def _check(self, m, prev: dict) -> dict:
		o, r, n = m.groups()
		pr = self._get(f"/repos/{o}/{r}/pulls/{n}")
		sha = pr["head"]["sha"]
		runs = self._get(f"/repos/{o}/{r}/commits/{sha}/check-runs")["check_runs"]
		sts = self._get(f"/repos/{o}/{r}/commits/{sha}/status")["statuses"]
		c = {"passed": 0, "failed": 0, "pending": 0}
		for x in runs:
			c["pending" if x["status"] != "completed" else "failed" if x["conclusion"] in FAILED else "passed"] += 1
		for x in sts:
			c["passed" if x["state"] == "success" else "failed" if x["state"] in FAILED else "pending"] += 1
		merged = bool(pr.get("merged"))
		return {"url": m.group(0), "number": int(n), "title": pr.get("title", ""), "state": "merged" if merged else pr["state"],
		        "merged": merged, "checks": c, "at": _now(), "noted": prev.get("noted", False)}


def start(boards) -> Syncer | None:
	token = os.environ.get("GITHUB_TOKEN") or os.environ.get("UNBLOCKD_GITHUB_TOKEN")
	if not token:
		return None
	s = Syncer(boards, token, os.environ.get("UNBLOCKD_GITHUB_API", "https://api.github.com"))

	def loop():
		s.poll()
		while not threading.Event().wait(INTERVAL):
			s.poll()
	threading.Thread(target=loop, daemon=True, name="github").start()
	return s
