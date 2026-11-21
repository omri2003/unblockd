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

AGENTS = [{"name": "backend", "tags": ["gdscript"], "pillars": ["COMBAT"], "model": "sonnet", "instructions": "Run the tests."},
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
		self.assertEqual([t["agent"] for t in (a, b, c, d)], ["backend", "backend", "artist", None])
		self.assertEqual([t["id"] for t in self.s.list(agent="artist")], [c["id"]])
		self.assertEqual([t["id"] for t in self.s.next_ready(tag="gdscript")], [b["id"]])
		self.assertEqual([t["id"] for t in self.s.next_ready(agent="artist-1")], [c["id"]])  # a worker name means its profile
		self.assertEqual([t["id"] for t in self.s.list(agent="backend-2")], sorted([a["id"], b["id"]]))
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
		self.assertEqual(got, {"backend": ["CMB-1"], "artist": ["ART-1"]})
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

	def test_create_board_in_a_container_needs_a_mounted_root(self):
		from unblockd import boards as bd
		other = tempfile.TemporaryDirectory()
		self.addCleanup(other.cleanup)
		ephemeral = Path(other.name)  # not under self.root, which is the mounted one
		b = Boards([str(ephemeral), str(self.root)])
		mi = ephemeral / "mountinfo"
		vol = self.root.absolute().as_posix()
		mi.write_text(f"20 1 0:40 / / rw,relatime - overlay overlay rw\n"
		              f"30 20 8:1 /home/omri/my\\040projects {vol} rw,relatime - ext4 /dev/sda1 rw\n", encoding="utf-8")
		with mock.patch.object(bd, "MOUNTINFO", mi), mock.patch.dict(os.environ, {"container": "docker"}):
			with self.assertRaisesRegex(TicketError, f"vanishes when the container is recreated. Pass root= one of the mounted roots: {vol}"):
				b.create("lost")
			self.assertFalse((ephemeral / "lost").exists())
			made = b.create("kept", root=str(self.root))
			self.assertEqual(made["path"], (self.root / "kept" / "tickets").as_posix())
			self.assertEqual(made["host_path"], "/home/omri/my projects/kept/tickets")
			with self.assertRaisesRegex(TicketError, "not one of the server's UNBLOCKD_ROOT"):
				b.create("x", root="/elsewhere")
		self.assertNotIn("host_path", Boards([str(ephemeral)]).create("local"))  # not in a container: no check, no host path


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

	def to_review(self, tid, board="a"):  # review needs a result (CORE-19): finish it, then move it back to review as a person would
		self.call("POST", f"/api/b/{board}/tickets/{tid}/claim", {"agent": "backend-1"})
		self.call("POST", f"/api/b/{board}/tickets/{tid}/complete", {"agent": "backend-1", "summary": "did it"})
		self.assertEqual(self.call("PATCH", f"/api/b/{board}/tickets/{tid}", {"status": "review"})[1]["status"], "review")

	def call(self, method, path, body=None, headers=None):
		req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body else None,
		                             headers={"Content-Type": "application/json", **(headers or {})})
		try:
			with urllib.request.urlopen(req) as r:
				return r.status, json.loads(r.read())
		except urllib.error.HTTPError as e:
			return e.code, json.loads(e.read())

	def raw(self, method, path, body=b"", headers=None):
		c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
		c.request(method, path, body=body, headers=headers or {})
		r = c.getresponse()
		return r.status, r.read(), r

	def test_writes_need_json_and_the_same_origin(self):  # CSRF: text/plain POSTs need no preflight
		body = json.dumps({"prefix": "BUG", "title": "x"}).encode()
		js = {"Content-Type": "application/json"}
		code, out, _ = self.raw("POST", "/api/tickets", body, {"Content-Type": "text/plain"})
		self.assertEqual(code, 415)
		self.assertIn("application/json", json.loads(out)["error"])
		self.assertEqual(self.raw("POST", "/api/tickets", body)[0], 415)  # no Content-Type at all
		self.assertEqual(self.call("GET", "/api/tickets")[1], [])  # nothing was created
		host = f"127.0.0.1:{self.srv.server_port}"
		self.assertEqual(self.raw("POST", "/api/tickets", body, {**js, "Origin": "http://evil.example"})[0], 403)
		self.assertEqual(self.raw("POST", "/api/tickets", body, {**js, "Origin": "https://" + host})[0], 403)  # scheme counts
		self.assertEqual(self.raw("DELETE", "/api/tickets/BUG-1", headers={"Origin": "http://evil.example"})[0], 403)
		self.assertEqual(self.raw("POST", "/api/tickets", body, {**js, "Origin": "http://" + host})[0], 201)  # same origin
		self.assertEqual(self.raw("POST", "/api/tickets", body, {**js, "Origin": "https://" + host, "X-Forwarded-Proto": "https"})[0], 201)
		self.assertEqual(self.raw("POST", "/api/tickets", body, js)[0], 201)  # no Origin: curl, CLI
		self.assertEqual(self.raw("DELETE", "/api/tickets/BUG-1")[0], 200)  # DELETE needs no body or Content-Type

	def test_if_match_guards_patch_with_409(self):
		t = self.call("POST", "/api/tickets", {"prefix": "BUG", "title": "x"})[1]
		self.assertEqual(t["rev"], 1)
		code, out, _ = self.raw("PATCH", "/api/tickets/BUG-1", json.dumps({"title": "mine"}).encode(), {"Content-Type": "application/json", "If-Match": "1"})
		self.assertEqual((code, json.loads(out)["rev"]), (200, 2))
		code, out, _ = self.raw("PATCH", "/api/tickets/BUG-1", json.dumps({"title": "stale"}).encode(), {"Content-Type": "application/json", "If-Match": '"1"'})
		self.assertEqual(code, 409)
		self.assertIn("reload", json.loads(out)["error"])
		self.assertEqual(self.call("GET", "/api/tickets/BUG-1")[1]["title"], "mine")
		self.assertEqual(self.call("PATCH", "/api/tickets/BUG-1", {"title": "no header"})[0], 200)  # no If-Match: last write wins, as before
		self.assertEqual(self.raw("PATCH", "/api/tickets/BUG-1", b"{}", {"Content-Type": "application/json", "If-Match": "abc"})[0], 400)
		self.assertEqual(self.call("PATCH", "/api/tickets/BUG-1", {"rev": 1})[0], 400)

	def test_bad_content_length_is_400(self):
		code, out, _ = self.raw("POST", "/api/tickets", b"{}", {"Content-Type": "application/json", "Content-Length": "abc"})
		self.assertEqual(code, 400)
		self.assertIn("Content-Length", json.loads(out)["error"])

	def test_security_headers(self):
		_, _, r = self.raw("GET", "/api/tickets")
		self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
		self.assertEqual(r.getheader("Content-Security-Policy"), "frame-ancestors 'none'")

	def test_token_cookie_is_secure_over_https(self):
		with mock.patch.dict(os.environ, {"UNBLOCKD_TOKEN": "s3cret"}):
			for proto, secure in (("https", True), ("http", False)):
				_, _, r = self.raw("GET", "/?token=s3cret", headers={"X-Forwarded-Proto": proto})
				self.assertEqual("; Secure" in r.getheader("Set-Cookie"), secure)


	def test_boards_are_separate_and_default_is_first(self):
		self.assertEqual(self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "x"})[0], 201)
		self.assertEqual(len(self.call("GET", "/api/b/b/tickets")[1]), 1)
		self.assertEqual(self.call("GET", "/api/tickets")[1], [])  # default board = a
		self.assertEqual(self.call("GET", "/api/meta")[1]["board"], "a")
		self.assertEqual([x["id"] for x in self.call("GET", "/api/boards")[1]], ["a", "b"])
		self.assertEqual(self.call("GET", "/api/b/zzz/tickets")[0], 404)

	def test_files_lists_the_project_only(self):  # form suggestions for docs / touches
		root = Path(self.tmp.name)
		outside = root / "outside"
		outside.mkdir()
		(outside / "secret.md").write_text("x")
		proj = root / "a"
		(proj / "docs").mkdir()
		(proj / "docs" / "guide.md").write_text("x")
		(proj / "src").mkdir()
		(proj / "src" / "app.py").write_text("x")
		(proj / "node_modules").mkdir()
		(proj / "node_modules" / "junk.js").write_text("x")
		(proj / ".git").mkdir()
		(proj / ".git" / "config").write_text("x")
		os.symlink(outside, proj / "link")
		os.symlink(outside / "secret.md", proj / "docs" / "leak.md")
		code, d = self.call("GET", "/api/files")
		self.assertEqual(code, 200)
		self.assertEqual(d["files"], ["docs/guide.md", "src/app.py"])  # no tickets/, .git, node_modules, symlinks
		self.assertFalse(d["truncated"])
		self.assertEqual(self.call("GET", "/api/b/b/files")[1]["files"], [])  # per board
		from unblockd import knowledge
		old, knowledge.LIST_MAX = knowledge.LIST_MAX, 1
		try:
			self.assertEqual(self.call("GET", "/api/files")[1], {"files": ["docs/guide.md"], "truncated": True})
		finally:
			knowledge.LIST_MAX = old

	def test_old_done_tickets_are_still_listed(self):  # the Done column's window is UI only
		old = {"id": "OLD-1", "title": "ancient", "status": "done", "owner": "Me", "created": "2020-01-01", "updated": "2020-01-01"}
		(Path(self.tmp.name) / "a" / "tickets" / "OLD-1.json").write_text(json.dumps(old))
		ids = [t["id"] for t in self.call("GET", "/api/tickets")[1]]
		self.assertIn("OLD-1", ids)
		self.assertEqual(self.call("GET", "/api/meta")[1]["config"]["done_window_days"], 7)

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

	def test_an_unchanged_poll_does_no_json_dumps_or_md5(self):
		"""CORE-65: the body + ETag of GET tickets and /needs are kept while their inputs are unchanged; any write re-serializes.
		On a files board and a database board (the key is the connector's version, what _board() caches under)."""
		from unblockd import sql, web
		db = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(db.cleanup)
		self.addCleanup(sql.disconnect)
		web.BOARDS = Boards([self.tmp.name], ["sqlite:///" + (Path(db.name) / "x.db").as_posix() + "#s"])

		def get(path, etag=None):
			c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
			c.request("GET", path, headers={"If-None-Match": etag} if etag else {})
			r = c.getresponse()
			return r.status, r.getheader("ETag"), r.read()
		for b in ("a", "s"):
			with self.subTest(board=b):
				self.assertEqual(self.call("POST", f"/api/b/{b}/tickets", {"prefix": "BUG", "title": "x"})[0], 201)
				self.to_review("BUG-1", b)  # a new ticket can't start in review (CORE-19)
				n = {"dumps": 0, "md5": 0}
				fake_json = mock.Mock(wraps=json, dumps=lambda *a, **k: n.__setitem__("dumps", n["dumps"] + 1) or json.dumps(*a, **k))
				real_md5 = web.hashlib.md5
				fake_hash = mock.Mock(md5=lambda *a: n.__setitem__("md5", n["md5"] + 1) or real_md5(*a))
				_, et, body = get(f"/api/b/{b}/tickets")
				_, ne, nbody = get("/api/needs")
				with mock.patch.object(web, "json", fake_json), mock.patch.object(web, "hashlib", fake_hash):
					self.assertEqual(get(f"/api/b/{b}/tickets", et), (304, et, b""))
					self.assertEqual(get(f"/api/b/{b}/tickets"), (200, et, body))  # no tag: the same bytes, from the cache
					self.assertEqual(get("/api/needs", ne), (304, ne, b""))
					self.assertEqual(n, {"dumps": 0, "md5": 0})
					self.assertEqual(json.loads(get(f"/api/b/{b}/tickets?status=review")[2])[0]["id"], "BUG-1")  # a filter is not cached
					self.call("POST", f"/api/b/{b}/tickets/BUG-1/notes", {"text": "hi"})
					code, et2, body2 = get(f"/api/b/{b}/tickets", et)
					self.assertEqual((code, json.loads(body2)[0]["notes"][-1]["text"]), (200, "hi"))
					self.assertNotEqual(et2, et)
					code, ne2, _ = get("/api/needs", ne)
					self.assertEqual(code, 200)
					self.assertNotEqual(ne2, ne)
					self.assertEqual(get("/api/needs", ne2)[0], 304)

	def cli(self, *args, **env):
		"""Run the CLI as a remote worker (UNBLOCKD_URL) -> (exit code, parsed stdout)."""
		out, err = io.StringIO(), io.StringIO()
		e = {"UNBLOCKD_URL": self.base, "UNBLOCKD_BOARD": "a", **env}
		with mock.patch.dict(os.environ, e), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = cli.main(list(args))
		got = json.loads(out.getvalue())  # stdout is not a terminal here: errors come as {"error", "code"} too (API-24)
		return code, (f"{got['code']}: {got['error']}" if code == 1 else got)

	def test_principal_over_rest_and_cli_env(self):
		self.call("POST", "/api/tickets", {"prefix": "CMB", "title": "a", "pillar": "COMBAT"})
		self.call("POST", "/api/tickets", {"prefix": "CMB", "title": "b", "pillar": "COMBAT"})
		code, d = self.call("POST", "/api/claim", {"agent": "backend-1", "principal": "Nobody"})
		self.assertEqual((code, "not a human owner" in d["error"]), (400, True))
		code, d = self.call("POST", "/api/tickets/CMB-1/claim", {"agent": "backend-1", "principal": "Me"})
		self.assertEqual(d["claim"]["principal"], "Me")
		code, d = self.call("POST", "/api/tickets/CMB-1/heartbeat", {"agent": "backend-1", "principal": "Dana"})
		self.assertEqual(code, 400)
		self.assertEqual(self.call("POST", "/api/tickets/CMB-1/complete", {"agent": "backend-1", "summary": "s", "principal": "Me"})[1]["result"]["principal"], "Me")
		got = self.cli("claim", "--agent", "backend-1", UNBLOCKD_PRINCIPAL="Me")[1]  # env default
		self.assertEqual(got["claim"]["principal"], "Me")
		self.assertEqual(self.cli("complete", got["id"], "--agent", "backend-1", "--summary", "s", UNBLOCKD_PRINCIPAL="Me")[1]["result"]["principal"], "Me")

	def test_remote_cli_round_trip(self):
		self.assertEqual(self.cli("create", "--prefix", "CMB", "--title", "parry", "--json", '{"pillar": "COMBAT"}')[1]["id"], "CMB-1")
		self.assertEqual(self.cli("create", "--prefix", "CMB", "--title", "dodge", "--json", '{"pillar": "COMBAT"}')[1]["id"], "CMB-2")
		self.assertEqual([t["id"] for t in self.cli("list", "--status", "todo")[1]], ["CMB-1", "CMB-2"])
		code, got = self.cli("claim", "--agent", "backend-1")
		self.assertEqual((code, got["status"], got["claim"]["agent"], got["context"]["ticket"]), (0, "doing", "backend-1", got["id"]))
		self.assertEqual(self.cli("heartbeat", got["id"], "--agent", "backend-1", "--note", "working")[1]["claim"]["note"], "working")
		code, msg = self.cli("heartbeat", got["id"], "--agent", "someone-else")  # server's message, exit 1
		self.assertEqual(code, 1)
		self.assertIn("claimed by backend-1", msg)
		done = self.cli("complete", got["id"], "--agent", "backend-1", "--summary", "did it", "--file", "a.py", "--file", "b.py", "--ref", "PR-1", "--handoff", "h")[1]
		self.assertEqual((done["status"], done["result"]["files"], done["result"]["refs"], done["result"]["handoff"]), ("done", ["a.py", "b.py"], ["PR-1"], "h"))
		other = self.cli("claim", "--agent", "backend-1")[1]
		asked = self.cli("ask", other["id"], "--agent", "backend-1", "--question", "Omri: ok?")[1]
		self.assertEqual((asked["effective_status"], asked["claim"]), ("pending", None))
		empty = self.cli("claim", "--agent", "backend-1")[1]  # nothing left, and why
		self.assertEqual((empty["ticket"], empty["reason"], empty["counts"]["waiting_on_human"]), (None, "waiting", 1))
		self.assertEqual(self.cli("update", other["id"], "--json", '{"waiting_on": ""}')[1]["effective_status"], "todo")
		self.cli("claim", "--agent", "backend-1", "--id", other["id"])
		code, msg = self.cli("update", other["id"], "--json", '{"status": "done"}', "--agent", "backend-2")  # --agent travels to the server
		self.assertEqual(code, 1)
		self.assertIn("claimed by backend-1", msg)
		code, msg = self.cli("delete", other["id"], "--agent", "backend-2")  # delete carries the agent over REST too
		self.assertEqual((code, "only the holder" in msg), (1, True))
		self.cli("release", other["id"], "--agent", "backend-1")
		self.assertEqual(self.cli("claim", "--agent", "backend-1", "--id", other["id"])[1]["id"], other["id"])
		self.assertEqual(self.cli("release", other["id"], "--agent", "backend-1", "--reason", "x")[1]["status"], "todo")
		self.assertEqual(self.cli("get", "CMB-2")[1]["id"], "CMB-2")
		self.assertEqual(self.cli("note", "CMB-2", "hi", "--by", "ana")[1]["notes"][-1]["by"], "ana")
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
		self.assertIn("not_found: no ticket NOPE-1", msg)  # the server's 404 keeps its code through Remote
		code, msg = self.cli("list", UNBLOCKD_URL="http://127.0.0.1:1")
		self.assertEqual(code, 1)
		self.assertIn("down: unblockd server down: nothing answers at http://127.0.0.1:1", msg)
		self.assertIn("docker compose up -d", msg)

	def test_remote_cli_error_codes(self):
		"""API-24 over REST: 409 and 404 keep their code through Remote, a refusal is `refused`."""
		self.assertEqual(self.cli("create", "--prefix", "CMB", "--title", "a", "--json", '{"pillar": "COMBAT"}')[0], 0)
		self.assertIn("conflict: CMB-1 changed since you loaded it", self.cli("update", "CMB-1", "--rev", "7", "--json", '{"title": "b"}')[1])
		self.assertEqual(self.cli("update", "CMB-1", "--rev", "1", "--json", '{"title": "b"}')[1]["title"], "b")  # If-Match sent
		self.assertEqual(self.cli("claim", "--id", "CMB-1", "--agent", "backend-1")[0], 0)
		self.assertTrue(self.cli("claim", "--id", "CMB-1", "--agent", "backend-2")[1].startswith("refused: "))

	def test_remote_cli_until_request_id_takeover_followup(self):
		"""CORE-83: the REST / Remote / CLI paths of AI-58 (heartbeat until), CORE-74 (request_id), CORE-79 (explicit takeover) and AI-60 (followup)."""
		from datetime import datetime, timedelta, timezone
		for x in ("a", "b", "c"):
			self.assertEqual(self.cli("create", "--prefix", "CMB", "--title", x, "--json", '{"pillar": "COMBAT"}')[0], 0)
		self.assertEqual(self.cli("claim", "--id", "CMB-1", "--agent", "backend-1")[0], 0)
		until = (datetime.now(timezone.utc) + timedelta(minutes=40)).replace(microsecond=0)
		code, hb = self.cli("heartbeat", "CMB-1", "--agent", "backend-1", "--until", until.isoformat())
		self.assertEqual(code, 0)
		self.assertGreater(datetime.fromisoformat(hb["expires_at"]), until - timedelta(minutes=1))  # the claim lives past the normal lease
		self.assertEqual(self.cli("get", "CMB-1")[1]["claim"]["expires_at"], hb["expires_at"])  # what the board's lease bar reads (CORE-83)
		args = ("complete", "CMB-1", "--agent", "backend-1", "--summary", "done", "--request-id", "r-1")
		first, again = self.cli(*args)[1], self.cli(*args)[1]
		self.assertEqual((first["status"], first.get("already"), again["already"], again["request_id"]), ("done", None, True, "r-1"))
		self.call("PATCH", "/api/b/a/tickets/CMB-2", {"status": "doing"})  # doing by hand, nobody holds it: orphaned
		code, got = self.cli("claim", "--id", "CMB-2", "--agent", "backend-2")
		self.assertEqual(code, 0)
		self.assertTrue(any("you took it over" in w for w in got["warnings"]))
		code, got = self.cli("complete", "CMB-2", "--agent", "backend-2", "--summary", "half", "--followup", json.dumps({"title": "rest", "blocked_by": ["CMB-3"]}))
		self.assertEqual((code, got["followup"]["id"], got["followup"]["effective_status"]), (0, "CMB-4", "pending"))

	def test_cli_health(self):
		code, d = self.cli("health")
		self.assertEqual((code, d["status"], d["url"]), (0, "ok", self.base))
		code, msg = self.cli("health", UNBLOCKD_URL="http://127.0.0.1:1")
		self.assertEqual(code, 1)
		self.assertIn("server down", msg)

	def test_healthz_needs_no_auth(self):
		with mock.patch.dict(os.environ, {"UNBLOCKD_TOKEN": "s3cret"}):
			self.assertEqual(self.call("GET", "/healthz"), (200, {"status": "ok"}))
			self.assertEqual(self.call("GET", "/api/tickets")[0], 401)

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
		self.assertEqual(len(self.call("GET", "/api/b/a/next?agent=backend")[1]), 1)
		self.assertEqual(self.call("GET", "/api/b/a/next?limit=x")[0], 400)
		self.assertEqual(self.call("POST", "/api/boards", {"id": "c"})[0], 201)

	def test_knowledge_routes(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/files", {"name": "spec.md", "content": "window 0.2"})[0], 201)
		self.assertEqual(self.call("GET", "/api/b/a/tickets/CMB-1/files/spec.md")[1]["content"], "window 0.2")
		ctx = self.call("GET", "/api/b/a/tickets/CMB-1/context")[1]
		self.assertIn("window 0.2", ctx["docs"][0]["content"])
		lean = self.call("GET", "/api/b/a/tickets/CMB-1/context?content=0")[1]  # the Docs view's preview: what is sent, without the text
		self.assertEqual((lean["docs"][0]["sent"], "content" in lean["docs"][0]), ("full", False))
		self.assertNotIn("fits_budget", lean)  # everything is sent: no "fits at" line (UI-64)
		self.call("POST", "/api/b/a/tickets/CMB-1/files", {"name": "big.md", "content": "y" * 70000})  # over the 60k default budget
		self.assertEqual(self.call("GET", "/api/b/a/tickets/CMB-1/context?content=0")[1]["fits_budget"], 70010)
		self.call("DELETE", "/api/b/a/tickets/CMB-1/files/big.md")
		self.assertIn("rules", self.call("GET", "/api/b/a/knowledge")[1])
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
		self.assertEqual([(n["by"], n["text"]) for n in t["notes"]], [("human", "looks right")])
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/notes", {"by": "ana"})[0], 400)
		self.assertEqual(self.call("POST", "/api/b/a/tickets/NOPE-1/notes", {"text": "x"})[0], 404)

	def test_answer_and_reject_routes(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/answer", {"text": "x"})[0], 400)  # nothing asked
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/reject", {"reason": "x"})[0], 400)  # not in review
		self.call("POST", "/api/b/a/tickets/CMB-1/claim", {"agent": "backend-1"})
		self.call("POST", "/api/b/a/tickets/CMB-1/ask", {"agent": "backend-1", "question": "which?"})
		code, t = self.call("POST", "/api/b/a/tickets/CMB-1/answer", {"text": "this one"})
		self.assertEqual((code, t["waiting_on"]), (200, ""))
		self.assertEqual({k: t["notes"][-1][k] for k in ("by", "kind", "q", "text")}, {"by": "human", "kind": "answer", "q": "which?", "text": "this one"})
		self.to_review("CMB-1")
		code, t = self.call("POST", "/api/b/a/tickets/CMB-1/reject", {"by": "ana", "reason": "redo"})
		self.assertEqual((code, t["status"]), (200, "todo"))
		self.assertEqual((t["notes"][-1]["by"], t["notes"][-1]["kind"]), ("ana", "rejected"))
		self.assertEqual(self.call("POST", "/api/b/a/tickets/NOPE-1/answer", {"text": "x"})[0], 404)

	def test_approve_route_and_cli(self):
		for t in ("one", "two"):
			self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": t, "pillar": "COMBAT"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/approve", {"by": "ana"})[0], 400)  # not in review
		f = Path(self.tmp.name) / "a" / "tickets" / "CMB-1.json"  # an old file moved to review by hand before CORE-19: no result
		f.write_text(json.dumps({**json.loads(f.read_text(encoding="utf-8")), "status": "review"}), encoding="utf-8")
		code, t = self.call("POST", "/api/b/a/tickets/CMB-1/approve", {"by": "ana"})
		self.assertEqual((code, t["status"], t["result"]), (200, "done", None))  # moved to review by hand: no result is invented
		self.assertEqual(t["notes"][-1]["kind"], "approved")
		self.to_review("CMB-2")
		code, t = self.cli("approve", "CMB-2", "--by", "ana")
		self.assertEqual((code, t["status"]), (0, "done"))
		self.assertEqual(t["notes"][-1]["text"], "Approved by ana")
		self.assertEqual(self.call("POST", "/api/b/a/tickets/NOPE-1/approve", {})[0], 404)

	def test_needs_lists_questions_and_reviews_on_every_board(self):
		self.assertEqual(self.call("GET", "/api/needs"), (200, []))
		for pre, title, extra in (("CMB", "parry", {"pillar": "COMBAT"}), ("CMB", "dodge", {"pillar": "COMBAT", "priority": "P1"}),
		                          ("CMB", "parked", {"waiting_on": "Omri: later"}), ("CMB", "plain", {})):
			self.call("POST", "/api/b/a/tickets", {"prefix": pre, "title": title, **extra})
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "page"})
		self.call("POST", "/api/b/a/tickets/CMB-1/claim", {"agent": "backend-1"})
		self.call("POST", "/api/b/a/tickets/CMB-1/ask", {"agent": "backend-1", "question": "which?"})
		self.to_review("CMB-2")
		self.call("POST", "/api/b/b/tickets/WEB-1/claim", {"agent": "w-1"})
		self.call("POST", "/api/b/b/tickets/WEB-1/ask", {"agent": "w-1", "question": "colour?"})
		code, n = self.call("GET", "/api/needs")
		self.assertEqual(code, 200)
		self.assertEqual([(g["id"], g["title"]) for g in n], [("a", "A"), ("b", "B")])
		# score order: the P1 review before the unprioritised question; the hand-parked and plain tickets are not listed
		self.assertEqual([(i["id"], i["kind"]) for i in n[0]["items"]], [("CMB-2", "review"), ("CMB-1", "question")])
		q = n[0]["items"][1]
		self.assertEqual((q["waiting_on"], q["since"][:4].isdigit(), q["notes"][-1]["text"]), ("which?", True, "which?"))
		self.assertEqual({"id", "kind", "title", "priority", "score", "unblocks", "since", "waiting_on", "missing_blockers", "stale_days", "done_when", "result", "body", "notes"}, set(q))  # open_blockers/claim_note: stuck only
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
		# doing + open blocker is stuck on day 1; payload names the blocker; claim note rides along
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "blocked doer"})
		self.call("POST", "/api/b/b/tickets/WEB-4/claim", {"agent": "w-3"})
		self.call("POST", "/api/b/b/tickets/WEB-4/heartbeat", {"agent": "w-3", "note": "waiting on assets"})
		raw4 = Path(self.tmp.name) / "b" / "tickets" / "WEB-4.json"
		raw4.write_text(json.dumps({**json.loads(raw4.read_text()), "blocked_by": ["WEB-1"]}))
		w4 = {i["id"]: i for i in self.call("GET", "/api/needs")[1][1]["items"]}["WEB-4"]
		self.assertEqual((w4["kind"], w4["stale_days"]), ("stuck", None))
		self.assertEqual(w4["open_blockers"], [{"id": "WEB-1", "effective_status": "pending", "title": "page"}])
		self.assertEqual((w4["claim_note"]["by"], w4["claim_note"]["text"]), ("w-3", "waiting on assets"))
		self.assertEqual(items["WEB-2"].get("open_blockers"), [])
		self.call("PATCH", "/api/b/b/tickets/WEB-4", {"status": "todo"})  # release: back to todo, shows as Pending, leaves the panel
		self.assertNotIn("WEB-4", {i["id"] for i in self.call("GET", "/api/needs")[1][1]["items"]})
		self.assertEqual(self.call("POST", "/api/needs", {})[0], 405)

	def test_needs_for_one_board_lists_only_that_board(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "page"})
		for b, tid, agent in (("a", "CMB-1", "backend-1"), ("b", "WEB-1", "w-1")):
			self.call("POST", f"/api/b/{b}/tickets/{tid}/claim", {"agent": agent})
			self.call("POST", f"/api/b/{b}/tickets/{tid}/ask", {"agent": agent, "question": "which?"})
		listed = []
		real = Store.list
		def counting(s, *a, **k):
			listed.append(s.dir.parent.name)
			return real(s, *a, **k)
		with mock.patch.object(Store, "list", counting):
			code, n = self.call("GET", "/api/needs?board=b")
		self.assertEqual((code, [g["id"] for g in n], [i["id"] for i in n[0]["items"]]), (200, ["b"], ["WEB-1"]))
		self.assertEqual(listed, ["b"])  # board a is not even read
		self.assertEqual([g["id"] for g in self.call("GET", "/api/needs")[1]], ["a", "b"])  # no param: every board, as before
		self.assertEqual(self.call("GET", "/api/needs?board=a")[1][0]["id"], "a")
		code, err = self.call("GET", "/api/needs?board=nope")
		self.assertEqual(code, 404)
		self.assertIn("no board 'nope'", err["error"])

	def test_needs_cache_is_per_requested_board(self):
		"""CORE-65 + API-18: /needs?board=a and ?board=b are cached apart (one board's body never answers the other's request),
		and an unchanged ?board=a poll does no json.dumps / md5."""
		from unblockd import web
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		self.call("POST", "/api/b/b/tickets", {"prefix": "WEB", "title": "page"})
		self.to_review("CMB-1", "a")  # a new ticket can't start in review (CORE-19)
		self.to_review("WEB-1", "b")

		def get(path, etag=None):
			c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
			c.request("GET", path, headers={"If-None-Match": etag} if etag else {})
			r = c.getresponse()
			return r.status, r.getheader("ETag"), r.read()
		_, ea, a = get("/api/needs?board=a")
		_, eb, b = get("/api/needs?board=b")
		self.assertNotEqual((ea, a), (eb, b))
		self.assertEqual(([g["id"] for g in json.loads(a)], [g["id"] for g in json.loads(b)]), (["a"], ["b"]))
		self.assertEqual([g["id"] for g in json.loads(get("/api/needs")[2])], ["a", "b"])  # no param: every board, its own entry
		n = {"dumps": 0, "md5": 0}
		real_md5 = web.hashlib.md5
		fake_json = mock.Mock(wraps=json, dumps=lambda *x, **k: n.__setitem__("dumps", n["dumps"] + 1) or json.dumps(*x, **k))
		fake_hash = mock.Mock(md5=lambda *x: n.__setitem__("md5", n["md5"] + 1) or real_md5(*x))
		with mock.patch.object(web, "json", fake_json), mock.patch.object(web, "hashlib", fake_hash):
			self.assertEqual(get("/api/needs?board=a", ea), (304, ea, b""))
			self.assertEqual(get("/api/needs?board=a"), (200, ea, a))  # the same bytes, from the cache
			self.assertEqual(get("/api/needs?board=b"), (200, eb, b))  # b's own entry, not a's
			self.assertEqual(n, {"dumps": 0, "md5": 0})
		self.assertEqual(get("/api/needs?board=nope")[0], 404)  # unknown board: still 404 with the cache

	def test_needs_stuck_after_three_failed_attempts(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "flaky", "pillar": "COMBAT"})
		def fail(agent):
			self.call("POST", "/api/b/a/tickets/CMB-1/claim", {"agent": agent})
			self.call("POST", "/api/b/a/tickets/CMB-1/release", {"agent": agent, "reason": "rig broke"})
		def items():
			return {i["id"]: i for g in self.call("GET", "/api/needs")[1] for i in g["items"]}
		fail("backend-1"); fail("backend-2")
		self.assertEqual(items(), {})
		fail("backend-1")
		i = items()["CMB-1"]
		self.assertEqual((i["kind"], i["failed"]), ("stuck", "Failed 3 times (released ×3)"))
		self.assertEqual((len(i["attempts"]), i["attempts"][0]["note"]), (3, "rig broke"))
		self.call("POST", "/api/b/a/tickets/CMB-1/claim", {"agent": "backend-1"})  # being worked again: not listed
		self.assertEqual(items(), {})
		self.call("POST", "/api/b/a/tickets/CMB-1/release", {"agent": "backend-1"})
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/notes", {"text": "use the other rig"})[0], 200)  # by=human
		self.assertEqual(items(), {})

	def test_claim_routes(self):
		self.call("POST", "/api/b/a/tickets", {"prefix": "CMB", "title": "parry", "pillar": "COMBAT"})
		code, t = self.call("POST", "/api/b/a/claim", {"agent": "backend-1", "note": "starting"})
		self.assertEqual((code, t["id"], t["claim"]["agent"]), (200, "CMB-1", "backend-1"))
		empty = self.call("POST", "/api/b/a/claim", {"agent": "backend-2"})[1]
		self.assertEqual((empty["ticket"], empty["reason"], empty["counts"]["in_flight"]), (None, "waiting", 1))
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/heartbeat", {"agent": "backend-1", "note": "tests"})[0], 200)
		self.assertEqual(self.call("GET", "/api/b/a/agents")[1]["working"][0]["note"], "tests")
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/complete", {"agent": "nope", "summary": "x"})[0], 400)
		self.assertEqual(self.call("POST", "/api/b/a/tickets/CMB-1/complete", {"agent": "backend-1", "summary": "done"})[1]["status"], "done")
		self.assertEqual([e["event"] for e in self.call("GET", "/api/b/a/activity?ticket=CMB-1")[1]], ["complete", "heartbeat", "claim", "created"])
		self.assertEqual(len(self.call("GET", "/api/b/a/activity?since=2020-01-01T00:00:00Z&limit=99")[1]), 4)
		self.assertEqual(self.call("GET", "/api/b/a/activity?since=2999-01-01T00:00:00Z")[1], [])
		self.assertEqual(self.call("GET", "/api/b/a/activity?since=nope")[0], 400)


if __name__ == "__main__":
	unittest.main()


class OpenBindTest(unittest.TestCase):
	def test_refuses_beyond_loopback_without_a_token(self):
		from unblockd.store import refuse_open_bind
		with mock.patch.dict(os.environ, {}, clear=False):
			os.environ.pop("UNBLOCKD_ALLOW_OPEN", None)
			refuse_open_bind("127.0.0.1", None)
			refuse_open_bind("::1", "")
			refuse_open_bind("0.0.0.0", "tok")  # a token is enough
			with self.assertRaises(SystemExit) as cm:
				refuse_open_bind("0.0.0.0", None)
			for hint in ("UNBLOCKD_TOKEN", "HOST=127.0.0.1", "UNBLOCKD_ALLOW_OPEN=1"):
				self.assertIn(hint, str(cm.exception))
			os.environ["UNBLOCKD_ALLOW_OPEN"] = "1"
			refuse_open_bind("0.0.0.0", None)  # explicit opt-out
