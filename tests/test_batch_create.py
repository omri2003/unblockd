"""create_many: several tickets in one locked, all-or-nothing step, with "#N" refs between them (Store, REST, CLI, Remote)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import BATCH_MAX, Store, TicketError

SCHEMA = {"prefixes": {"API": "endpoints", "UI": "the web board"}, "required": ["done_when"]}


def store(tmp, **cfg):
	d = Path(tmp) / "p" / "tickets"
	d.mkdir(parents=True, exist_ok=True)
	(d / "unblockd.json").write_text(json.dumps(cfg))
	return Store(d)


class CreateManyTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.s = store(self.tmp.name)

	def tearDown(self):
		self.tmp.cleanup()

	def ids(self):
		return [t["id"] for t in self.s.list()]

	def test_temp_refs_resolve_to_the_new_ids(self):
		self.s.create({"title": "old"}, tid="API-3")
		out = self.s.create_many([
			{"prefix": "API", "title": "epic"},
			{"prefix": "API", "title": "endpoint", "parent": "#1", "blocked_by": ["API-3"]},
			{"prefix": "UI", "title": "use it", "parent": "#1", "blocked_by": ["#2", "#4"]},  # a forward ref is fine
			{"id": "UI-9", "title": "explicit"},
		])
		self.assertEqual(out["ids"], {"#1": "API-4", "#2": "API-5", "#3": "UI-1", "#4": "UI-9"})
		self.assertNotIn("warnings", out)
		t = {x["id"]: x for x in out["tickets"]}
		self.assertEqual(t["UI-1"]["blocked_by"], ["API-5", "UI-9"])
		self.assertEqual((t["UI-1"]["parent"], t["API-5"]["parent"]), ("API-4", "API-4"))
		self.assertEqual(self.s.get("API-4")["children"], ["API-5", "UI-1"])
		self.assertEqual(self.s.get("UI-1")["effective_status"], "pending")
		created = [e["ticket"] for e in self.s.activity(limit=10) if e["event"] == "created"]
		self.assertEqual(sorted(created), ["API-3", "API-4", "API-5", "UI-1", "UI-9"])

	def test_one_bad_item_creates_nothing_and_every_problem_is_listed(self):
		with self.assertRaises(TicketError) as e:
			self.s.create_many([{"prefix": "A", "title": "fine"}, {"prefix": "A"}, {"prefix": "A", "title": "x", "blocked_by": ["#7"]},
			                    {"prefix": "A", "title": "y", "blocked_by": ["NOPE-1"]}, "not a dict", {"title": "no prefix"}])
		msg = str(e.exception)
		for part in ("nothing was created", "#2: title is required", "#3: #7 is not an item of this batch", "#4: no ticket NOPE-1", "#5: ticket data must be an object", "#6: give an id or a prefix"):
			self.assertIn(part, msg)
		self.assertNotIn("#1:", msg)
		self.assertEqual(self.ids(), [])
		self.assertEqual(self.s.activity(limit=10), [])
		with self.assertRaisesRegex(TicketError, "given twice"):
			self.s.create_many([{"id": "A-1", "title": "a"}, {"id": "A-1", "title": "b"}])
		self.s.create({"title": "x"}, tid="A-1")
		with self.assertRaisesRegex(TicketError, "#1: A-1 already exists"):
			self.s.create_many([{"id": "A-1", "title": "a"}])
		with self.assertRaisesRegex(TicketError, "unknown field"):
			self.s.create_many([{"prefix": "A", "title": "a", "colour": "red"}])
		self.assertEqual(self.ids(), ["A-1"])

	def test_loops_are_refused_and_spelled_out(self):
		with self.assertRaisesRegex(TicketError, "blocked_by makes a loop: .*A-1.*A-2.*A-3"):
			self.s.create_many([{"prefix": "A", "title": "a", "blocked_by": ["#3"]}, {"prefix": "A", "title": "b", "blocked_by": ["#1"]},
			                    {"prefix": "A", "title": "c", "blocked_by": ["#2"]}])
		with self.assertRaisesRegex(TicketError, "#1: A-1 can't be blocked by itself"):
			self.s.create_many([{"prefix": "A", "title": "a", "blocked_by": ["#1"]}])
		with self.assertRaisesRegex(TicketError, "parent makes a loop"):
			self.s.create_many([{"prefix": "A", "title": "a", "parent": "#2"}, {"prefix": "A", "title": "b", "parent": "#1"}])
		with self.assertRaisesRegex(TicketError, "can't be its own parent"):
			self.s.create_many([{"prefix": "A", "title": "a", "parent": "#1"}])
		self.assertEqual(self.ids(), [])
		# a blocker that is also the parent's part is not a loop (parent is not a blocker)
		out = self.s.create_many([{"prefix": "A", "title": "epic", "blocked_by": ["#2"]}, {"prefix": "A", "title": "part", "parent": "#1"}])
		self.assertEqual(out["ids"], {"#1": "A-1", "#2": "A-2"})

	def test_cap_and_empty(self):
		with self.assertRaisesRegex(TicketError, f"at most {BATCH_MAX}"):
			self.s.create_many([{"prefix": "A", "title": str(i)} for i in range(BATCH_MAX + 1)])
		with self.assertRaisesRegex(TicketError, "non-empty list"):
			self.s.create_many([])
		with self.assertRaisesRegex(TicketError, "non-empty list"):
			self.s.create_many({"title": "x"})
		self.assertEqual(len(self.s.create_many([{"prefix": "A", "title": str(i)} for i in range(BATCH_MAX)])["tickets"]), BATCH_MAX)

	def test_schema_strict_for_agents_warnings_for_humans(self):
		s = store(self.tmp.name, schema=SCHEMA, owners=[{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}])
		items = [{"prefix": "API", "title": "a", "done_when": ["ok"]}, {"prefix": "FOG", "title": "b"}]
		with self.assertRaises(TicketError) as e:
			s.create_many(items, agent="bot-1")
		self.assertIn("#2: ticket refused by the board schema: prefix FOG is not one of API, UI; done_when is required", str(e.exception))
		self.assertNotIn("#1:", str(e.exception))
		self.assertEqual(s.list(), [])
		out = s.create_many(items)  # a human
		self.assertEqual(out["warnings"], {"#2": ["prefix FOG is not one of API, UI", "done_when is required"]})
		self.assertEqual(out["ids"], {"#1": "API-1", "#2": "FOG-1"})
		self.assertIn("warnings", s.create_many([{"prefix": "UI", "title": "c"}], agent="Omri"))  # a human owner's own Claude
		with self.assertRaisesRegex(TicketError, "#1: give a prefix or an id: this board's prefixes are API, UI"):
			s.create_many([{"title": "no prefix"}])
		one = store(tempfile.mkdtemp(dir=self.tmp.name), schema={"prefixes": {"ONE": "only"}})
		self.assertEqual(one.create_many([{"title": "a"}, {"title": "b", "blocked_by": ["#1"]}])["ids"], {"#1": "ONE-1", "#2": "ONE-2"})

	def test_proposal_gate(self):
		s = store(self.tmp.name, agent_tickets="propose", owners=[{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}])
		out = s.create_many([{"prefix": "A", "title": "a"}, {"prefix": "A", "title": "b", "blocked_by": ["#1"]}], agent="bot-1")
		self.assertEqual([(t["proposed_by"], t["effective_status"]) for t in out["tickets"]], [("bot-1", "pending")] * 2)
		self.assertEqual(s.next_ready(), [])
		ev = [(e["event"], e["ticket"]) for e in reversed(s.activity(limit=10))]
		self.assertEqual(ev, [("created", "A-1"), ("proposed", "A-1"), ("created", "A-2"), ("proposed", "A-2")])
		self.assertEqual(s.create_many([{"prefix": "A", "title": "c"}], agent="Omri")["tickets"][0]["proposed_by"], "")
		self.assertEqual(s.create_many([{"prefix": "A", "title": "d"}])["tickets"][0]["proposed_by"], "")

	def test_two_threads_get_distinct_ids(self):
		outs, errs = [], []
		def go(n):
			try:
				outs.append(Store(self.s.dir).create_many([{"prefix": "A", "title": f"{n}-{i}", "blocked_by": [f"#{i}"] if i else []} for i in range(10)]))
			except Exception as e:  # noqa: BLE001
				errs.append(e)
		ths = [threading.Thread(target=go, args=(n,)) for n in range(2)]
		for t in ths:
			t.start()
		for t in ths:
			t.join()
		self.assertEqual(errs, [])
		self.assertEqual(len(self.ids()), 20)
		for o in outs:  # each batch's chain points inside itself
			ids = list(o["ids"].values())
			self.assertEqual([t["blocked_by"] for t in o["tickets"]], [[]] + [[x] for x in ids[:-1]])


class FrontDoorTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.s = store(self.tmp.name, schema=SCHEMA)
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def call(self, body):
		req = urllib.request.Request(self.base + "/api/tickets/batch", method="POST", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
		try:
			with urllib.request.urlopen(req) as r:
				return r.status, json.loads(r.read())
		except urllib.error.HTTPError as e:
			return e.code, json.loads(e.read())

	def test_rest_round_trip(self):
		code, out = self.call({"tickets": [{"prefix": "API", "title": "a", "done_when": ["ok"]}, {"prefix": "UI", "title": "b", "done_when": ["ok"], "blocked_by": ["#1"]}], "agent": "bot-1"})
		self.assertEqual((code, out["ids"]), (201, {"#1": "API-1", "#2": "UI-1"}))
		self.assertEqual(self.s.get("UI-1")["blocked_by"], ["API-1"])
		code, out = self.call({"tickets": [{"prefix": "UI", "title": "c"}], "agent": "bot-1"})
		self.assertEqual(code, 400)
		self.assertIn("#1: ticket refused by the board schema", out["error"])
		self.assertEqual(self.call({})[0], 400)

	def test_remote_create_many(self):
		r = cli.Remote(self.base)
		out = r.create_many([{"prefix": "API", "title": "a"}], agent=None)
		self.assertEqual((out["ids"], out["warnings"]), ({"#1": "API-1"}, {"#1": ["done_when is required"]}))
		with self.assertRaisesRegex(TicketError, "done_when is required"):
			r.create_many([{"prefix": "API", "title": "a"}], agent="bot-1")

	def test_cli_file_and_stdin(self):
		def run(*a, stdin=""):
			out, old = io.StringIO(), sys.stdin
			sys.stdin = io.StringIO(stdin)
			try:
				with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
					code = cli.main(["--dir", str(self.s.dir), *a])
			finally:
				sys.stdin = old
			return code, json.loads(out.getvalue()) if code == 0 else None
		plan = Path(self.tmp.name) / "plan.json"
		plan.write_text(json.dumps([{"prefix": "API", "title": "a", "done_when": ["ok"]}, {"prefix": "API", "title": "b", "done_when": ["ok"], "parent": "#1"}]))
		code, out = run("create-many", "--file", str(plan), "--agent", "bot-1")
		self.assertEqual((code, out["ids"]), (0, {"#1": "API-1", "#2": "API-2"}))
		code, out = run("create-many", "--file", "-", stdin=json.dumps([{"prefix": "UI", "title": "c", "done_when": ["ok"], "blocked_by": ["API-2"]}]))
		self.assertEqual((code, out["ids"]), (0, {"#1": "UI-1"}))
		self.assertEqual(run("create-many", "--file", "-", "--agent", "bot-1", stdin=json.dumps([{"prefix": "UI", "title": "d"}]))[0], 1)
		self.assertEqual(run("create-many", "--file", "-", stdin="not json")[0], 1)


if __name__ == "__main__":
	unittest.main()
