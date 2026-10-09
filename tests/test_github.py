"""PR sync against a fake GitHub API (no network)."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import github
from unblockd.boards import Boards

PRS: dict = {}  # number -> {"pr": {...}, "runs": [...], "statuses": [...]}
SEEN: list = []


class Fake(BaseHTTPRequestHandler):
	def do_GET(self):
		SEEN.append((self.path, self.headers.get("Authorization")))
		parts = self.path.split("/")  # '', repos, o, r, pulls, N | commits, sha, check-runs|status
		try:
			if parts[4] == "pulls":
				body = PRS[int(parts[5])]["pr"]
			else:
				d = PRS[int(parts[5].removeprefix("sha"))]
				body = {"check_runs": d["runs"]} if parts[6] == "check-runs" else {"statuses": d["statuses"]}
			code = 200
		except KeyError:
			code, body = 404, {"message": "Not Found"}
		data = json.dumps(body).encode()
		self.send_response(code)
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)

	def log_message(self, *a):
		pass


def pr(n, state="open", merged=False):
	return {"pr": {"state": state, "merged": merged, "title": f"PR {n}", "head": {"sha": f"sha{n}"}}, "runs": [], "statuses": []}


class GithubTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.tdir = Path(self.tmp.name) / "game" / "tickets"
		self.tdir.mkdir(parents=True)
		(self.tdir / "unblockd.json").write_text(json.dumps({"title": "game"}), encoding="utf-8")
		self.boards = Boards([self.tmp.name])
		self.s = self.boards.get("game")
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.sync = github.Syncer(self.boards, "tok", f"http://127.0.0.1:{self.srv.server_port}")
		PRS.clear()
		SEEN.clear()

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def review(self, tid, ref):
		self.s.create({"title": tid, "owner": "Claude"}, tid=tid)
		self.s.claim("w-1", tid=tid)
		self.s.complete(tid, "w-1", "done", refs=[ref] if ref else None)
		self.s.update(tid, {"status": "review"})

	def url(self, n):
		return f"https://github.com/o/r/pull/{n}"

	def cache(self):
		return github.load(self.tdir)

	def test_open_pr_caches_check_summary(self):
		PRS[1] = pr(1)
		PRS[1]["runs"] = [{"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "failure"},
		                  {"status": "in_progress", "conclusion": None}, {"status": "completed", "conclusion": "skipped"}]
		PRS[1]["statuses"] = [{"state": "success"}, {"state": "pending"}]
		self.review("BUG-1", self.url(1))
		self.sync.poll()
		c = self.cache()["BUG-1"]
		self.assertEqual((c["number"], c["state"], c["checks"]), (1, "open", {"passed": 3, "failed": 1, "pending": 2}))
		self.assertEqual(self.s.get("BUG-1")["status"], "review")
		self.assertNotIn("PR", self.s.get("BUG-1")["body"].split("done")[-1])  # ticket untouched
		self.assertEqual(SEEN[0][1], "Bearer tok")

	def test_needs_exposes_pr_without_noted(self):
		from unblockd import web
		PRS[1] = pr(1)
		self.review("BUG-1", self.url(1))
		self.sync.poll()
		with mock.patch.object(web, "BOARDS", self.boards):
			item = web._needs()[0]["items"][0]
		self.assertEqual(item["pr"]["number"], 1)
		self.assertNotIn("noted", item["pr"])

	def test_merged_pr_moves_ticket_to_done_with_note(self):
		PRS[2] = pr(2, "closed", True)
		self.review("BUG-2", self.url(2))
		self.sync.poll()
		t = self.s.get("BUG-2")
		self.assertEqual(t["status"], "done")
		self.assertIn("— github,", t["body"])
		self.assertIn("PR #2 merged — moved to done", t["body"])
		self.assertEqual(self.cache(), {})
		self.assertIn("done", [e["event"] for e in self.s.activity("BUG-2", 5)])

	def test_closed_pr_notes_once(self):
		PRS[3] = pr(3, "closed")
		self.review("BUG-3", self.url(3))
		self.sync.poll()
		self.sync.poll()
		t = self.s.get("BUG-3")
		self.assertEqual(t["status"], "review")
		self.assertEqual(t["body"].count("closed without merging"), 1)
		self.assertEqual(self.cache()["BUG-3"]["state"], "closed")

	def test_ignored_tickets_and_bad_urls(self):
		PRS[4] = pr(4, "closed", True)
		self.s.create({"title": "todo", "owner": "Claude"}, tid="BUG-4")
		self.s.update("BUG-4", {"status": "doing"})
		self.review("BUG-5", "https://example.com/o/r/pull/4")
		self.review("BUG-6", "https://github.com/o/r/issues/4")
		self.review("BUG-7", None)
		self.sync.poll()
		self.assertEqual(SEEN, [])
		self.assertEqual(self.cache(), {})

	def test_errors_print_once_and_keep_cache(self):
		PRS[1] = pr(1)
		self.review("BUG-1", self.url(1))
		self.review("BUG-8", self.url(99))  # 404
		self.sync.poll()
		self.assertIn("BUG-1", self.cache())
		with mock.patch("builtins.print") as pr_:
			self.sync.poll()
			self.sync.poll()
		self.assertEqual(pr_.call_count, 0)  # the first poll (above) already printed this streak
		self.assertEqual(list(self.cache()), ["BUG-1"])

	def test_start_needs_token(self):
		with mock.patch.dict("os.environ", {}, clear=True):
			self.assertIsNone(github.start(self.boards))


if __name__ == "__main__":
	unittest.main()
