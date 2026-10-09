"""Agent routing, work plans, several boards, and the multi-board HTTP API."""
import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from unittest import mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import NotFound, Store, TicketError

AGENTS = [{"name": "godot-dev", "tags": ["gdscript"], "pillars": ["COMBAT"], "model": "sonnet", "instructions": "Run the tests."},
          {"name": "artist", "pillars": ["ART"]}]


def board(root: Path, name: str, cfg: dict) -> Path:
	d = root / name / "tickets"
	d.mkdir(parents=True)
	(d / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
	return d


class RoutingTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = board(Path(self.tmp.name), "game", {"pillars": [{"key": "ART"}, {"key": "COMBAT"}], "agents": AGENTS})
		self.s = Store(d)

	def tearDown(self):
		self.tmp.cleanup()

	def test_tags_route_by_pillar_tag_and_explicit_override(self):
		a = self.s.create({"title": "parry", "pillar": "COMBAT"}, prefix="CMB")
		b = self.s.create({"title": "shader", "tags": ["GDScript", " gdscript "]}, prefix="ART")
		c = self.s.create({"title": "rig", "pillar": "COMBAT", "tags": ["agent:artist"]}, prefix="CMB")
		d = self.s.create({"title": "readme"}, prefix="DOC")
		self.assertEqual(b["tags"], ["gdscript"])  # lowercased, trimmed, deduped
		self.assertEqual([t["agent"] for t in (a, b, c, d)], ["godot-dev", "godot-dev", "artist", None])
		self.assertEqual([t["id"] for t in self.s.list(agent="artist")], [c["id"]])
		self.assertEqual([t["id"] for t in self.s.next_ready(tag="gdscript")], [b["id"]])
		self.assertEqual([t["id"] for t in self.s.next_ready(agent="artist-1")], [c["id"]])  # a worker name means its profile
		self.assertEqual([t["id"] for t in self.s.list(agent="godot-dev-2")], sorted([a["id"], b["id"]]))
		self.assertEqual(self.s.list(agent="nobody-1"), [])

	def test_bad_tags_and_unknown_agents_are_refused(self):
		with self.assertRaisesRegex(TicketError, "unknown agent"):
			self.s.create({"title": "x", "tags": ["agent:nobody"]}, prefix="BUG")
		with self.assertRaisesRegex(TicketError, "invalid tags"):
			self.s.create({"title": "x", "tags": ["has space"]}, prefix="BUG")

	def test_work_plan_groups_ready_tickets_per_agent(self):
		self.s.create({"title": "parry", "pillar": "COMBAT", "priority": "P1"}, tid="CMB-1")
		self.s.create({"title": "riposte", "pillar": "COMBAT", "blocked_by": ["CMB-1"]}, tid="CMB-2")  # not ready
		self.s.create({"title": "skeleton", "pillar": "ART"}, tid="ART-1")
		self.s.create({"title": "docs"}, tid="DOC-1")
		self.s.create({"title": "mixamo", "pillar": "ART", "owner": "Me"}, tid="ART-2")  # a human's: never planned
		plan = self.s.work_plan()
		got = {g["name"]: [t["id"] for t in g["tickets"]] for g in plan["agents"]}
		self.assertEqual(got, {"godot-dev": ["CMB-1"], "artist": ["ART-1"]})
		self.assertEqual(plan["agents"][0]["model"], "sonnet")
		self.assertEqual([t["id"] for t in plan["unrouted"]], ["DOC-1"])


class BoardsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name)
		board(self.root, "example-project", {"id": "dc", "title": "Example Project"})
		board(self.root, "website", {"title": "Site"})
		(self.root / "no-config" / "tickets").mkdir(parents=True)  # not opted in: not a board
		self.b = Boards([str(self.root)])

	def tearDown(self):
		self.tmp.cleanup()

	def test_discovery_ids_and_lookup(self):
		self.assertEqual(list(self.b.all()), ["dc", "website"])
		self.assertEqual(self.b.get().config["title"], "Example Project")  # first = default
		self.b.get("website").create({"title": "x"}, prefix="WEB")
		self.assertEqual([x["tickets"] for x in self.b.summary()], [0, 1])
		with self.assertRaisesRegex(NotFound, "Boards: dc, website"):
			self.b.get("nope")

	def test_create_board(self):
		made = self.b.create("tools", "Tools")
		self.assertEqual((made["id"], made["title"]), ("tools", "Tools"))
		self.assertTrue((self.root / "tools" / "tickets" / "unblockd.json").is_file())
		with self.assertRaisesRegex(TicketError, "already exists"):
			self.b.create("tools")
		with self.assertRaisesRegex(TicketError, "bad board id"):
			self.b.create("Bad Name")


class ApiTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		root = Path(self.tmp.name)
		board(root, "a", {"title": "A", "agents": AGENTS, "pillars": [{"key": "COMBAT"}]})
		board(root, "b", {"title": "B"})
		from unblockd import web
		web.BOARDS = Boards([str(root)])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def call(self, method, path, body=None, headers=None):
		req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body else None,
		                             headers={"Content-Type": "application/json", **(headers or {})})
		try:
			with urllib.request.urlopen(req) as r:
				return r.status, json.loads(r.read())
		except urllib.error.HTTPError as e:
			return e.code, json.loads(e.read())

	def test_boards_are_separate_and_default_is_first(self):
		self.assertEqual(self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "x"})[0], 201)
		self.assertEqual(len(self.call("GET", "/api/b/b/tickets")[1]), 1)
		self.assertEqual(self.call("GET", "/api/tickets")[1], [])  # default board = a
		self.assertEqual(self.call("GET", "/api/meta")[1]["board"], "a")
		self.assertEqual([x["id"] for x in self.call("GET", "/api/boards")[1]], ["a", "b"])
		self.assertEqual(self.call("GET", "/api/b/zzz/tickets")[0], 404)

	def test_etag_304_when_the_board_is_unchanged(self):
		self.call("POST", "/api/tickets", {"prefix": "BUG", "title": "x"})

		def get(etag=None):
			c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
			c.request("GET", "/api/tickets", headers={"If-None-Match": etag} if etag else {})
			r = c.getresponse()
			return r.status, r.getheader("ETag"), r.read()
		code, etag, body = get()
		self.assertEqual(code, 200)
		self.assertTrue(etag and body)
		self.assertEqual(get(etag), (304, etag, b""))
		self.call("PATCH", "/api/tickets/BUG-1", {"title": "y"})
		code2, etag2, _ = get(etag)  # stale tag after an update: full 200 with a new tag
		self.assertEqual(code2, 200)
		self.assertNotEqual(etag2, etag)

	def cli(self, *args, **env):
		"""Run the CLI as a remote worker (UNBLOCKD_URL) -> (exit code, parsed stdout)."""
		out, err = io.StringIO(), io.StringIO()
		e = {"UNBLOCKD_URL": self.base, "UNBLOCKD_BOARD": "a", **env}
		with mock.patch.dict(os.environ, e), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = cli.main(list(args))
		return code, (json.loads(out.getvalue()) if out.getvalue() else err.getvalue().strip())

	def test_remote_cli_round_trip(self):
		self.assertEqual(self.cli("create", "--prefix", "CMB", "--title", "parry", "--json", '{"pillar": "COMBAT"}')[1]["id"], "CMB-1")
		self.assertEqual(self.cli("create", "--prefix", "CMB", "--title", "dodge", "--json", '{"pillar": "COMBAT"}')[1]["id"], "CMB-2")
		self.assertEqual([t["id"] for t in self.cli("list", "--status", "todo")[1]], ["CMB-1", "CMB-2"])
		code, got = self.cli("claim", "--agent", "godot-dev-1")
		self.assertEqual((code, got["status"], got["claim"]["agent"], got["context"]["ticket"]), (0, "doing", "godot-dev-1", got["id"]))
		self.assertEqual(self.cli("heartbeat", got["id"], "--agent", "godot-dev-1", "--note", "working")[1]["claim"]["note"], "working")
		code, msg = self.cli("heartbeat", got["id"], "--agent", "someone-else")  # server's message, exit 1
		self.assertEqual(code, 1)
		self.assertIn("claimed by godot-dev-1", msg)
		done = self.cli("complete", got["id"], "--agent", "godot-dev-1", "--summary", "did it", "--file", "a.py", "--file", "b.py", "--ref", "PR-1", "--handoff", "h")[1]
		self.assertEqual((done["status"], done["result"]["files"], done["result"]["refs"], done["result"]["handoff"]), ("done", ["a.py", "b.py"], ["PR-1"], "h"))
		other = self.cli("claim", "--agent", "godot-dev-1")[1]
		asked = self.cli("ask", other["id"], "--agent", "godot-dev-1", "--question", "Omri: ok?")[1]
		self.assertEqual((asked["effective_status"], asked["claim"]), ("pending", None))
		empty = self.cli("claim", "--agent", "godot-dev-1")[1]  # nothing left, and why
		self.assertEqual((empty["ticket"], empty["reason"], empty["counts"]["waiting_on_human"]), (None, "waiting", 1))
		self.assertEqual(self.cli("update", other["id"], "--json", '{"waiting_on": ""}')[1]["effective_status"], "todo")
		self.cli("claim", "--agent", "godot-dev-1", "--id", other["id"])
		code, msg = self.cli("update", other["id"], "--json", '{"status": "done"}', "--agent", "godot-dev-2")  # --agent travels to the server
		self.assertEqual(code, 1)
		self.assertIn("claimed by godot-dev-1", msg)
		code, msg = self.cli("delete", other["id"], "--agent", "godot-dev-2")  # delete carries the agent over REST too
		self.assertEqual((code, "only the holder" in msg), (1, True))
		self.cli("release", other["id"], "--agent", "godot-dev-1")
		self.assertEqual(self.cli("claim", "--agent", "godot-dev-1", "--id", other["id"])[1]["id"], other["id"])
		self.assertEqual(self.cli("release", other["id"], "--agent", "godot-dev-1", "--reason", "x")[1]["status"], "todo")
		self.assertEqual(self.cli("get", "CMB-2")[1]["id"], "CMB-2")
		self.assertIn("— ana, ", self.cli("note", "CMB-2", "hi", "--by", "ana")[1]["body"])
		self.assertEqual(self.cli("answer", "CMB-2", "x")[0], 1)  # Remote.answer / reject reach the server's refusals
		self.assertIn("not review", self.cli("reject", "CMB-2", "x")[1])  # Remote.add_note -> POST notes
		self.assertEqual(self.cli("plan")[1]["agents"][0]["ready"], 1)
		self.assertEqual(self.cli("agents")[1]["working"], [])
		kids = self.cli("split", "CMB-2", "--title", "a", "--title", "b")[1]["children"]
		self.assertEqual([k["parent"] for k in kids], ["CMB-2", "CMB-2"])
		self.assertEqual(len(self.cli("list", "--parent", "CMB-2")[1]), 2)
		self.assertEqual(len(self.call("GET", "/api/b/a/tickets")[1]), 4)  # it all landed on the server's board

	def test_remote_cli_token_and_errors(self):
		with mock.patch.dict(os.environ, {"UNBLOCKD_TOKEN": "s3cret"}):
			self.assertEqual(self.cli("list", UNBLOCKD_TOKEN="s3cret")[0], 0)
			with self.assertRaisesRegex(TicketError, "unauthorized"):  # (env is shared with the in-process server, so use Remote directly)
				cli.Remote(self.base, "nope", "a").list()
		code, msg = self.cli("get", "NOPE-1")
		self.assertEqual(code, 1)
		self.assertIn("no ticket NOPE-1", msg)
		code, msg = self.cli("list", UNBLOCKD_URL="http://127.0.0.1:1")
		self.assertEqual(code, 1)
		self.assertIn("can't reach", msg)

	def test_token_auth(self):
		self.assertEqual(self.call("GET", "/api/tickets")[0], 200)  # unset: open
		with mock.patch.dict(os.environ, {"UNBLOCKD_TOKEN": "s3cret"}):
			code, body = self.call("GET", "/api/tickets")
			self.assertEqual(code, 401)
			self.assertIn("?token=", body["error"])
			self.assertEqual(self.call("GET", "/api/tickets", headers={"Authorization": "Bearer nope"})[0], 401)
			self.assertEqual(self.call("GET", "/api/tickets", headers={"Authorization": "Bearer s3cret"})[0], 200)
			self.assertEqual(self.call("GET", "/api/tickets", headers={"Cookie": "unblockd_token=s3cret"})[0], 200)
			self.assertEqual(self.call("GET", "/api/tickets?token=nope")[0], 401)
			conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)  # urllib would follow the redirect
			conn.request("GET", "/?board=b&token=s3cret")
			r = conn.getresponse()
			self.assertEqual((r.status, r.getheader("Location")), (302, "/?board=b"))
			self.assertRegex(r.getheader("Set-Cookie"), "unblockd_token=s3cret; HttpOnly; SameSite=Strict; Path=/")
			conn.request("GET", "/")
			self.assertEqual(conn.getresponse().status, 200)  # the page itself needs no token

	def test_plan_and_agent_filter(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		plan = self.call("GET", "/api/b/a/plan")[1]
		self.assertEqual(plan["agents"][0]["tickets"][0]["id"], "CMB-1")
		self.assertEqual(len(self.call("GET", "/api/b/a/next?agent=godot-dev")[1]), 1)
		self.assertEqual(self.call("GET", "/api/b/a/next?limit=x")[0], 400)
		self.assertEqual(self.call("POST", "/api/boards", {"id": "c"})[0], 201)

	def test_knowledge_routes(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/files", {"name": "spec.md", "content": "window 0.2"})[0], 201)
		self.assertEqual(self.call("GET", "/api/b/a/tickets/CMB-1/files/spec.md")[1]["content"], "window 0.2")
		ctx = self.call("GET", "/api/b/a/tickets/CMB-1/context")[1]
		self.assertEqual(ctx["docs"][0]["content"], "window 0.2")
		self.assertEqual(self.call("GET", "/api/b/a/doc?path=tickets/unblockd.json")[0], 200)
		self.assertEqual(self.call("GET", "/api/b/a/doc?path=../b/tickets/unblockd.json")[0], 400)
		g = self.call("GET", "/api/b/a/knowledge")[1]
		self.assertEqual((g["reads"][0]["doc"], g["reads"][0]["via"]), ("tickets/files/CMB-1/spec.md", "file"))
		self.assertEqual(self.call("DELETE", "/api/b/a/tickets/CMB-1/files/spec.md")[0], 200)
		self.assertEqual(self.call("GET", "/api/b/a/tickets/CMB-1/files/spec.md")[0], 400)

	def test_split_route(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/split", {"titles": ["one"]})[0], 400)
		code, r = self.call("POST", "/api/b/a/tickets/CMB-1/split", {"titles": ["one", "two"], "sequential": True})
		self.assertEqual((code, r["parent"]["blocked_by"], r["children"][1]["blocked_by"]), (200, ["CMB-2", "CMB-3"], ["CMB-2"]))
		self.assertEqual(self.call("POST", "/api/b/a/tickets/NOPE-1/split", {"titles": ["a", "b"]})[0], 404)
		with mock.patch.dict(os.environ, {"UNBLOCKD_TOKEN": "s3cret"}):
			self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-2/split", {"titles": ["a", "b"]})[0], 401)

	def test_notes_route_defaults_by_to_human(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		code, t = self.call("POST", "/api/b/a/tickets/CMB-1/notes", {"text": "looks right"})
		self.assertEqual(code, 200)
		self.assertRegex(t["body"], r"^— human, .*: looks right$")
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/notes", {"by": "ana"})[0], 400)
		self.assertEqual(self.call("POST", "/api/b/a/tickets/NOPE-1/notes", {"text": "x"})[0], 404)

	def test_answer_and_reject_routes(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/answer", {"text": "x"})[0], 400)  # nothing asked
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/reject", {"reason": "x"})[0], 400)  # not in review
		self.call("POST", "/api/b/a/tickets/CMB-1/claim", {"agent": "godot-dev-1"})
		self.call("POST", "/api/b/a/tickets/CMB-1/ask", {"agent": "godot-dev-1", "question": "which?"})
		code, t = self.call("POST", "/api/b/a/tickets/CMB-1/answer", {"text": "this one"})
		self.assertEqual((code, t["waiting_on"]), (200, ""))
		self.assertRegex(t["body"], r"— human, .*Answer to: which\? — this one$")
		self.call("PATCH", "/api/b/a/tickets/CMB-1", {"status": "review"})
		code, t = self.call("POST", "/api/b/a/tickets/CMB-1/reject", {"by": "ana", "reason": "redo"})
		self.assertEqual((code, t["status"]), (200, "todo"))
		self.assertIn("— ana, ", t["body"])
		self.assertEqual(self.call("POST", "/api/b/a/tickets/NOPE-1/answer", {"text": "x"})[0], 404)

	def test_needs_lists_questions_and_reviews_on_every_board(self):
		self.assertEqual(self.call("GET", "/api/needs"), (200, []))
		for pre, title, extra in (("CMB", "parry", {"pillar": "COMBAT"}), ("CMB", "dodge", {"pillar": "COMBAT", "priority": "P1"}),
		                          ("CMB", "parked", {"waiting_on": "Omri: later"}), ("CMB", "plain", {})):
			self.call("POST", "/api/b/a/tickets", {"prefix": pre, "title": title, **extra})
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "page"})
		self.call("POST", "/api/b/a/tickets/CMB-1/claim", {"agent": "godot-dev-1"})
		self.call("POST", "/api/b/a/tickets/CMB-1/ask", {"agent": "godot-dev-1", "question": "which?"})
		self.call("PATCH", "/api/b/a/tickets/CMB-2", {"status": "review"})
		self.call("POST", "/api/b/b/tickets/WEB-1/claim", {"agent": "w-1"})
		self.call("POST", "/api/b/b/tickets/WEB-1/ask", {"agent": "w-1", "question": "colour?"})
		code, n = self.call("GET", "/api/needs")
		self.assertEqual(code, 200)
		self.assertEqual([(g["id"], g["title"]) for g in n], [("a", "A"), ("b", "B")])
		# score order: the P1 review before the unprioritised question; the hand-parked and plain tickets are not listed
		self.assertEqual([(i["id"], i["kind"]) for i in n[0]["items"]], [("CMB-2", "review"), ("CMB-1", "question")])
		q = n[0]["items"][1]
		self.assertEqual((q["waiting_on"], q["since"][:4].isdigit(), "question: which?" in q["body"]), ("which?", True, True))
		self.assertEqual({"id", "kind", "title", "priority", "score", "unblocks", "since", "waiting_on", "missing_blockers", "stale_days", "done_when", "result", "body"}, set(q))
		self.assertEqual([i["id"] for i in n[1]["items"]], ["WEB-1"])
		self.call("POST", "/api/b/a/tickets/CMB-1/answer", {"text": "this one"})
		self.assertEqual([i["id"] for i in self.call("GET", "/api/needs")[1][0]["items"]], ["CMB-2"])
		# stuck: a doing ticket untouched for stale_days, or one whose blocker file is gone; fresh work and parked tickets stay out
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "old"})
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "orphan"})
		self.call("POST", "/api/b/b/tickets/WEB-2/claim", {"agent": "w-2"})
		old = Path(self.tmp.name) / "b" / "tickets" / "WEB-2.json"
		raw = json.loads(old.read_text())
		old.write_text(json.dumps({**raw, "updated": (date.today() - timedelta(days=30)).isoformat()}))
		self.call("PATCH", "/api/b/b/tickets/WEB-3", {"blocked_by": ["WEB-1"]})
		orphan = Path(self.tmp.name) / "b" / "tickets" / "WEB-3.json"
		orphan.write_text(json.dumps({**json.loads(orphan.read_text()), "blocked_by": ["WEB-99"]}))
		items = {i["id"]: i for i in self.call("GET", "/api/needs")[1][1]["items"]}
		self.assertEqual({k: v["kind"] for k, v in items.items()}, {"WEB-1": "question", "WEB-2": "stuck", "WEB-3": "stuck"})
		self.assertEqual((items["WEB-2"]["stale_days"], items["WEB-2"]["missing_blockers"]), (30, []))
		self.assertEqual((items["WEB-3"]["stale_days"], items["WEB-3"]["missing_blockers"]), (None, ["WEB-99"]))
		self.assertEqual(self.call("POST", "/api/needs", {})[0], 405)

	def test_claim_routes(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		code, t = self.call("POST", "/api/b/a/claim", {"agent": "godot-dev-1", "note": "starting"})
		self.assertEqual((code, t["id"], t["claim"]["agent"]), (200, "CMB-1", "godot-dev-1"))
		empty = self.call("POST", "/api/b/a/claim", {"agent": "godot-dev-2"})[1]
		self.assertEqual((empty["ticket"], empty["reason"], empty["counts"]["in_flight"]), (None, "waiting", 1))
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/heartbeat", {"agent": "godot-dev-1", "note": "tests"})[0], 200)
		self.assertEqual(self.call("GET", "/api/b/a/agents")[1]["working"][0]["note"], "tests")
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/complete", {"agent": "nope", "summary": "x"})[0], 400)
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/complete", {"agent": "godot-dev-1", "summary": "done"})[1]["status"], "done")
		self.assertEqual([e["event"] for e in self.call("GET", "/api/b/a/activity?ticket=CMB-1")[1]], ["complete", "heartbeat", "claim", "created"])
		self.assertEqual(len(self.call("GET", "/api/b/a/activity?since=2020-01-01T00:00:00Z&limit=99")[1]), 4)
		self.assertEqual(self.call("GET", "/api/b/a/activity?since=2999-01-01T00:00:00Z")[1], [])
		self.assertEqual(self.call("GET", "/api/b/a/activity?since=nope")[0], 400)


if __name__ == "__main__":
	unittest.main()
