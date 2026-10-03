"""Agent routing, work plans, several boards, and the multi-board HTTP API."""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

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

	def call(self, method, path, body=None):
		req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body else None,
		                             headers={"Content-Type": "application/json"})
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

	def test_plan_and_agent_filter(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		plan = self.call("GET", "/api/b/a/plan")[1]
		self.assertEqual(plan["agents"][0]["tickets"][0]["id"], "CMB-1")
		self.assertEqual(len(self.call("GET", "/api/b/a/next?agent=godot-dev")[1]), 1)
		self.assertEqual(self.call("GET", "/api/b/a/next?limit=x")[0], 400)
		self.assertEqual(self.call("POST", "/api/boards", {"id": "c"})[0], 201)


if __name__ == "__main__":
	unittest.main()
