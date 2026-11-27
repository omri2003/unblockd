"""MCP prompt + resource carrying the worker protocol (skipped when the `mcp` package is missing)."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import unblockd

try:
	import mcp  # noqa: F401
except ImportError:
	mcp = None


class CliNextTest(unittest.TestCase):  # AI-42: no mcp needed
	def test_complete_release_ask_next(self):
		import contextlib
		import io
		from unittest import mock
		from unblockd import cli
		from unblockd.store import Store
		with tempfile.TemporaryDirectory() as tmp:
			s = Store(tmp)
			for i in (1, 2, 3):
				s.create({"title": str(i)}, tid=f"B-{i}")
			s.claim("w-1", tid="B-1")

			def run(*args):
				out = io.StringIO()
				with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(out):
					self.assertEqual(cli.main(["--dir", tmp, *args]), 0)
				return json.loads(out.getvalue())
			r = run("complete", "B-1", "--agent", "w-1", "--summary", "done", "--next")
			self.assertEqual((r["done"]["status"], r["next"]["id"], r["next"]["status"]), ("done", "B-2", "doing"))
			self.assertIn("context", r["next"])
			r = run("release", "B-2", "--agent", "w-1", "--reason", "stuck", "--next")
			self.assertEqual((r["done"]["status"], r["next"]["id"]), ("todo", "B-2"))  # released, best ticket again
			r = run("ask", "B-2", "--agent", "w-1", "--question", "which?", "--next")
			self.assertEqual(r["next"]["id"], "B-3")
			r = run("complete", "B-3", "--agent", "w-1", "--summary", "done", "--next")
			self.assertEqual(r["next"]["reason"], "waiting")  # B-2 waits on a human


@unittest.skipIf(mcp is None, "mcp not installed")
class WorkLoopTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		os.environ["TICKETS_DIR"] = self.tmp.name

	def tearDown(self):
		del os.environ["TICKETS_DIR"]
		self.tmp.cleanup()

	def test_prompt_and_resource_carry_the_protocol(self):
		from unblockd import mcp_server as m
		text = m.work_loop_text("game", "backend")
		for word in ("claim_next(agent=<your name>, profile=backend, board=game)", "backend-1", "heartbeat", "ask_human", "release", "complete", "done_when", "conflict", "waiting", "dry", "`answer`", "`rejected`", "worktree", "Fresh context"):
			self.assertIn(word, text)
		self.assertNotIn("board_info", m.work_loop_text())  # the rules come with the claim (context.rules, AI-41)
		self.assertIn("context.rules", text)
		self.assertIn("Never go silent", text)
		self.assertIn("Your board's policy", text)
		self.assertIn("Never go silent", m.WORK_LOOP.read_text(encoding="utf-8"))  # the text comes from the file
		self.assertEqual(m.work_loop_resource(), m.work_loop_text())
		prompt = asyncio.run(m.mcp.get_prompt("work_loop", {"board": "game", "profile": "backend"}))
		self.assertIn("claim_next", str(prompt))
		self.assertIn("work_loop", [p.name for p in asyncio.run(m.mcp.list_prompts())])
		self.assertIn("unblockd://work-loop", [str(r.uri) for r in asyncio.run(m.mcp.list_resources())])

	def test_next_ready_for_a_worker_skips_humans_and_refuses_unknown_names(self):
		from unblockd import mcp_server as m
		from mcp.server.mcpserver.exceptions import ToolError
		(Path(self.tmp.name) / "unblockd.json").write_text(json.dumps({"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
			"agents": [{"name": "backend", "tags": ["be"]}]}), encoding="utf-8")
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()  # this test's folder, whatever test imported the module first
		s = m.BOARDS.get(None)
		s.create({"title": "mine", "owner": "Omri", "tags": ["be"]}, tid="B-1")
		s.create({"title": "bots", "owner": "Claude", "tags": ["be"]}, tid="B-2")
		ids = lambda **kw: [t["id"] for t in m.next_ready(**kw)]
		self.assertEqual(ids(), ["B-1", "B-2"])
		self.assertEqual(ids(agent="backend-1"), ["B-2"])
		with self.assertRaisesRegex(ToolError, "matches no agent profile"):
			m.next_ready(agent="nobody")

	def test_claim_refusal_names_only_real_arguments(self):
		"""API-21: the refusal said 'or pass profile=<name>', but claim_ticket has no profile argument."""
		import inspect
		from unblockd import mcp_server as m
		from mcp.server.mcpserver.exceptions import ToolError
		(Path(self.tmp.name) / "unblockd.json").write_text(json.dumps({"agents": [{"name": "builder", "tags": ["be"]}]}), encoding="utf-8")
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		m.BOARDS.get(None).create({"title": "x", "tags": ["be"]}, tid="B-1")
		for call in (lambda: m.claim_ticket(id="B-1", agent="nobody"), lambda: m.next_ready(agent="nobody")):
			with self.assertRaises(ToolError) as e:
				call()
			self.assertIn("builder-1", str(e.exception))
			self.assertNotIn("profile=", str(e.exception))
		self.assertNotIn("profile", inspect.signature(m.claim_ticket).parameters)  # if it gains one, the hint may name it again
		self.assertEqual(m.claim_ticket(id="B-1", agent="builder-1")["id"], "B-1")

	def test_server_instructions_point_workers_at_claim_next(self):
		from unblockd import mcp_server as m
		self.assertNotIn("next_ready(agent", m.mcp.instructions)
		self.assertIn("work_loop", m.mcp.instructions)

	def test_principal_on_claim_next(self):
		from unblockd import mcp_server as m
		from mcp.server.mcpserver.exceptions import ToolError
		(Path(self.tmp.name) / "unblockd.json").write_text(json.dumps({"owners": [{"name": "Omri", "kind": "human"}, {"name": "Dana", "kind": "human"}, {"name": "Claude", "kind": "agent"}]}), encoding="utf-8")
		m.BOARDS = m.bd.from_env()  # BOARDS is built at import: point it at this test's folder
		m.BOARDS.get(None).create({"title": "a"}, tid="B-1")
		with self.assertRaisesRegex(ToolError, "not a human owner"):
			m.claim_next("w-1", principal="Claude")
		got = m.claim_next("w-1", principal="Dana")
		self.assertEqual(got["claim"]["principal"], "Dana")
		self.assertEqual(m.complete("B-1", "w-1", "done", principal="Dana")["status"], "done")
		self.assertEqual(m.get_ticket("B-1")["result"]["principal"], "Dana")
		self.assertIn("principal", m.work_loop_text())

	def test_worker_writes_answer_short(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		m.BOARDS.get(None).create({"title": "a", "body": "x" * 5000}, tid="B-1")
		m.claim_ticket("B-1", "w-1")
		for r in (m.heartbeat("B-1", "w-1", "testing"), m.add_note("B-1", "w-1", "found it", kind="finding"),
		          m.update_ticket("B-1", {"priority": "P1"}, "w-1"), m.complete("B-1", "w-1", "done")):
			self.assertEqual(set(r) - {"note", "lease_left_s", "expires_at", "warnings"}, {"ok", "id", "status", "effective_status", "rev"})
			self.assertLess(len(json.dumps(r)), 200)

	def test_next_true_finishes_and_claims_in_one_reply(self):  # AI-42
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		(Path(self.tmp.name) / "unblockd.json").write_text(json.dumps({"session_tickets": 3}), encoding="utf-8")
		m.BOARDS = m.bd.from_env()
		for i in (1, 2, 3, 4):
			m.BOARDS.get(None).create({"title": str(i)}, tid=f"B-{i}")
		m.SENT.clear()
		m.CLAIMS.clear()
		m.DEDUPE = True
		try:
			first = m.claim_next("w-1")
			r = m.complete(first["id"], "w-1", "done", next=True)
			self.assertEqual((r["done"]["id"], r["done"]["status"]), (first["id"], "done"))
			self.assertEqual(r["next"]["status"], "doing")
			self.assertIn("context", r["next"])  # the same reply claim_next gives
			r2 = m.release(r["next"]["id"], "w-1", "stuck", next=True)
			self.assertEqual(r2["done"]["status"], "todo")
			self.assertEqual(r2["next"]["claim"]["agent"], "w-1")
			r3 = m.ask_human(r2["next"]["id"], "w-1", "which way?", next=True)
			self.assertEqual(r3["done"]["effective_status"], "pending")
			self.assertEqual(r3["next"]["reason"], "session_done")  # 3 claims in this session: the cap still ends it
		finally:
			m.DEDUPE = False
		left = m.claim_next("w-2")
		self.assertNotIn("next", m.complete(left["id"], "w-2", "done"))  # next=false: the plain ack

	def test_next_true_when_nothing_is_left_or_the_claim_fails(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		(Path(self.tmp.name) / "unblockd.json").write_text(json.dumps({"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}]}), encoding="utf-8")
		m.BOARDS = m.bd.from_env()
		for i in (1, 2):
			m.BOARDS.get(None).create({"title": str(i)}, tid=f"B-{i}")
		m.claim_ticket("B-1", "w-1")
		r = m.complete("B-1", "w-1", "done", principal="Claude", next=True)  # Claude is no human owner: complete ignores it, the claim refuses it
		self.assertEqual(r["done"]["status"], "done")
		self.assertIn("not a human owner", r["next"]["error"])
		m.claim_ticket("B-2", "w-1")
		r = m.complete("B-2", "w-1", "done", next=True)
		self.assertEqual(r["next"]["reason"], "dry")

	def test_claim_next_wait_is_capped_by_the_server(self):  # AI-46
		from unblockd import mcp_server as m
		from unblockd import store
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		self.addCleanup(setattr, m, "WAIT_MAX", m.WAIT_MAX)
		self.addCleanup(setattr, store, "WAIT_POLL_S", store.WAIT_POLL_S)
		m.BOARDS = m.bd.from_env()
		m.BOARDS.get(None).create({"title": "a", "touches": ["x.py"]}, tid="B-1")
		m.BOARDS.get(None).create({"title": "b", "touches": ["x.py"]}, tid="B-2")
		m.claim_next("w-1")
		store.WAIT_POLL_S, m.WAIT_MAX = 0.05, 0.2  # what MCP_WAIT_MAX does on the --http server
		r = m.claim_next("w-2", wait_s=120)
		self.assertEqual((r["reason"], r["held_by"][0]["id"]), ("conflict", "B-1"))
		self.assertLess(r["waited_s"], 5)

	def test_claim_next_wait_over_rest(self):  # AI-46: POST claim {wait_s} and cli.Remote
		import threading
		from http.server import ThreadingHTTPServer
		from unblockd import cli, store, web
		s = web.bd.Boards(dirs=[self.tmp.name])
		s.get(None).create({"title": "a", "touches": ["x.py"]}, tid="B-1")
		s.get(None).create({"title": "b", "touches": ["x.py"]}, tid="B-2")
		s.get(None).claim("w-1", tid="B-1")
		self.addCleanup(setattr, web, "BOARDS", web.BOARDS)
		self.addCleanup(setattr, store, "WAIT_POLL_S", store.WAIT_POLL_S)
		web.BOARDS, store.WAIT_POLL_S = s, 0.05
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		try:
			threading.Timer(0.3, lambda: s.get(None).release("B-1", "w-1")).start()
			got = cli.Remote(f"http://127.0.0.1:{srv.server_port}").claim_next("w-2", wait_s=20)
		finally:
			srv.shutdown()
			srv.server_close()
		self.assertIn(got["id"], ("B-1", "B-2"))
		self.assertLess(got["waited_s"], 10)

	def test_board_docs_are_sent_once_per_session(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		d = Path(self.tmp.name)
		(d.parent / "POLICY.md").write_text("how we work " * 50, encoding="utf-8")
		(d.parent / "SPEC.md").write_text("the spec", encoding="utf-8")
		(d / "unblockd.json").write_text(json.dumps({"docs": ["POLICY.md"]}), encoding="utf-8")
		m.BOARDS = m.bd.from_env()
		for i in (1, 2, 3):
			m.BOARDS.get(None).create({"title": str(i), "docs": ["SPEC.md"]}, tid=f"B-{i}")
		m.SENT.clear()
		m.DEDUPE = True
		try:
			docs = lambda r: {x["path"]: x for x in r["context"]["docs"]}
			first = docs(m.claim_ticket("B-1", "w-1"))
			self.assertTrue(first["POLICY.md"]["content"].startswith("how we work"))
			m.complete("B-1", "w-1", "done")
			second = docs(m.claim_ticket("B-2", "w-1"))
			self.assertIsNone(second["POLICY.md"]["content"])  # board doc: already sent, unchanged
			self.assertIn("read_doc", second["POLICY.md"]["unchanged"])
			self.assertIn("the spec", second["SPEC.md"]["content"])  # the ticket's own doc: always sent
			m.complete("B-2", "w-1", "done")
			(d.parent / "POLICY.md").write_text("new rules", encoding="utf-8")
			self.assertEqual(docs(m.claim_ticket("B-3", "w-1"))["POLICY.md"]["content"], "new rules")  # changed: sent again
		finally:
			m.DEDUPE = False

	def test_worker_tool_set(self):
		import asyncio
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		from mcp.server.mcpserver import MCPServer
		m.BOARDS = m.bd.from_env()
		real = m.mcp
		names = lambda: {t.name for t in asyncio.run(m.mcp.list_tools())}
		try:
			m.mcp = MCPServer("t")  # a throwaway server with the same tools
			for t in asyncio.run(real.list_tools()):
				m.mcp.add_tool(real._tool_manager.get_tool(t.name).fn, name=t.name)
			self.assertEqual(m.limit_tools("all"), [])
			kept = m.limit_tools("worker")
			self.assertEqual(names(), set(kept))
			self.assertIn("claim_next", kept)
			self.assertNotIn("delete_ticket", names())
			with self.assertRaises(SystemExit):
				m.limit_tools("claim_next,nope")
		finally:
			m.mcp = real

	def test_plan_from_spec_prompt(self):
		from unblockd import mcp_server as m
		self.assertIn("plan_from_spec", [p.name for p in asyncio.run(m.mcp.list_prompts())])
		text = str(asyncio.run(m.mcp.get_prompt("plan_from_spec", {"board": "game", "path": "docs/spec.md"})))
		for word in ("docs/spec.md", "board=", "AT MOST 10", "done_when", "STOP", "before that", "ONE `create_tickets", "#N", "all or nothing", "DATA"):
			self.assertIn(word, text)

	def test_plan_from_spec_challenge_step(self):
		# AI-61: a separate challenge pass before the plan is shown; its verdicts are a column of step 4's table
		from unblockd import mcp_server as m
		text = str(asyncio.run(m.mcp.get_prompt("plan_from_spec", {"board": "g", "path": "s.md"})))
		for word in ("3b. Challenge the draft (required", "separate agent", "No subagents", "steelman", "premortem", "overlap", "keep | edit", "drop", "Challenge ="):
			self.assertIn(word, text)
		self.assertLess(text.index("3b. Challenge"), text.index("4. Show the plan"))
		self.assertLess(text.index("4. Show the plan"), text.index("ONE `create_tickets"))
		self.assertIn("STOP. Wait for approval", text)

	def test_create_tickets_one_call_with_temp_refs(self):
		from unblockd import mcp_server as m
		from mcp.server.mcpserver.exceptions import ToolError
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		tool = {t.name: t for t in asyncio.run(m.mcp.list_tools())}["create_tickets"]
		self.assertEqual(set(tool.input_schema["required"]), {"tickets"})  # the name: agent or by, checked by _who (API-17)
		out = m.create_tickets([{"prefix": "A", "title": "a"}, {"prefix": "A", "title": "b", "blocked_by": ["#1"]}], agent="planner")
		self.assertEqual(out["ids"], {"#1": "A-1", "#2": "A-2"})
		self.assertEqual(m.BOARDS.get(None).get("A-2")["blocked_by"], ["A-1"])
		with self.assertRaisesRegex(ToolError, "#2: title is required"):
			m.create_tickets([{"prefix": "A", "title": "c"}, {"prefix": "A"}], agent="planner")
		self.assertEqual(len(m.BOARDS.get(None).list()), 2)

	def test_list_tickets_fields_and_limit(self):
		from unblockd import mcp_server as m
		from mcp.server.mcpserver.exceptions import ToolError
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		s = m.BOARDS.get(None)
		s.create({"title": "epic", "body": "long " * 400}, tid="B-1")
		s.create({"title": "part", "parent": "B-1", "touches": ["ui/x.js"], "done_when": ["it works"]}, tid="B-2")
		s.add_note("B-2", "omri", "a note")
		plain = m.list_tickets()
		self.assertEqual(set(plain[0]), set(st_keys := ("id", "title", "effective_status", "priority", "owner", "pillar", "milestone", "tags", "agent", "open_blockers")))
		rows = m.list_tickets(fields=["touches", "parent", "done_when"])
		self.assertEqual(set(rows[1]), set(st_keys) | {"touches", "parent", "done_when"})  # only the asked keys
		self.assertEqual((rows[1]["touches"], rows[1]["parent"], rows[1]["done_when"]), (["ui/x.js"], "B-1", ["it works"]))
		self.assertNotIn("body", rows[0])
		self.assertNotIn("notes", rows[1])
		self.assertLess(len(json.dumps(rows)), len(json.dumps(m.list_tickets(full=True))) / 3)
		self.assertEqual(m.list_tickets(fields=["notes"])[1]["notes"][0]["text"], "a note")  # notes / body: when named
		self.assertTrue(m.list_tickets(fields=["body"])[0]["body"].startswith("long"))
		self.assertEqual([t["id"] for t in m.list_tickets(limit=1)], ["B-1"])
		with self.assertRaisesRegex(ToolError, "unknown field.*colour.*one of"):
			m.list_tickets(fields=["colour"])
		with self.assertRaisesRegex(ToolError, "limit must be"):
			m.list_tickets(limit=-1)
		self.assertIn('fields=["touches", "parent", "done_when"]', str(asyncio.run(m.mcp.get_prompt("plan_from_spec", {"board": "g", "path": "s.md"}))))

	def test_update_tickets_all_or_nothing_with_short_acks(self):
		from unblockd import mcp_server as m
		from mcp.server.mcpserver.exceptions import ToolError
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		s = m.BOARDS.get(None)
		for i in (1, 2):
			s.create({"title": str(i), "body": "x" * 3000}, tid=f"B-{i}")
		acks = m.update_tickets([{"id": "B-1", "changes": {"priority": "P1"}}, {"id": "B-2", "changes": {"tags": ["ui"]}}], agent="planner")
		self.assertEqual([(a["ok"], a["id"]) for a in acks], [(True, "B-1"), (True, "B-2")])
		self.assertLess(len(json.dumps(acks)), 300)
		with self.assertRaisesRegex(ToolError, "#2 B-2: invalid priority"):
			m.update_tickets([{"id": "B-1", "changes": {"priority": "P3"}}, {"id": "B-2", "changes": {"priority": "P7"}}], agent="planner")
		self.assertEqual(s.get("B-1")["priority"], "P1")

	def test_split_ticket_parts_and_claim(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		s = m.BOARDS.get(None)
		s.create({"title": "big", "touches": ["ui/"]}, tid="B-1")
		m.claim_ticket("B-1", "w-1")
		out = m.split_ticket("B-1", "w-1", parts=[{"title": "a", "touches": ["ui/a.js"], "done_when": ["a works"]},
		                                         {"title": "b", "touches": ["ui/b.js"], "blocked_by": ["#1"]}], claim=1)
		self.assertEqual([(k["id"], k["touches"], k["blocked_by"]) for k in out["children"]], [("B-2", ["ui/a.js"], []), ("B-3", ["ui/b.js"], ["B-2"])])
		self.assertEqual((out["claimed"]["id"], out["claimed"]["claim"]["agent"]), ("B-2", "w-1"))
		self.assertIn("context", out["claimed"])
		self.assertEqual(len(m.split_ticket("B-3", "w-2", titles=["x", "y"])["children"]), 2)  # the short form

	def test_list_tickets_by_touches_shows_the_holder(self):
		from unblockd import mcp_server as m
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.from_env()
		s = m.BOARDS.get(None)
		s.create({"title": "a", "touches": ["src/"]}, tid="B-1")
		s.create({"title": "b src/x.py", "touches": ["docs/"]}, tid="B-2")
		s.claim("w-1", tid="B-1")
		rows = m.list_tickets(touches="src/x.py")
		self.assertEqual([(r["id"], r["holder"], r["touches"]) for r in rows], [("B-1", "w-1", ["src/"])])
		self.assertNotIn("holder", m.list_tickets()[0])
		self.assertIn('touches="<path or glob>"', str(asyncio.run(m.mcp.get_prompt("plan_from_spec", {"board": "g", "path": "s.md"}))))


@unittest.skipIf(mcp is None, "mcp not installed")
class NeedsYouTest(unittest.TestCase):
	"""AI-43: needs_you = GET /api/needs (one shared Boards.needs), and the human-side writes answer short."""

	def setUp(self):
		from unblockd import mcp_server as m, web
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		root = Path(self.tmp.name)
		for bid, extra in (("a", {"review": True, "agent_tickets": "propose"}), ("b", {})):
			(root / bid / "tickets").mkdir(parents=True)
			(root / bid / "tickets" / "unblockd.json").write_text(json.dumps({"id": bid, "title": bid.upper(), **extra,
				"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}], "default_owner": "Claude"}), encoding="utf-8")
		boards = m.bd.Boards([str(root)])
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		self.addCleanup(setattr, web, "BOARDS", web.BOARDS)
		m.BOARDS = web.BOARDS = boards
		self.m, self.a, self.b = m, boards.get("a"), boards.get("b")
		from http.server import ThreadingHTTPServer
		import threading
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.addCleanup(self.srv.server_close)
		self.addCleanup(self.srv.shutdown)

	def api_needs(self):
		import urllib.request
		with urllib.request.urlopen(f"http://127.0.0.1:{self.srv.server_port}/api/needs") as r:
			return json.loads(r.read())

	def test_same_items_as_the_panel_and_short_acks(self):
		m, a, b = self.m, self.a, self.b
		self.assertEqual(m.needs_you(), [])
		a.create({"title": "to review", "priority": "P1"}, tid="A-1")
		a.claim("w-1", tid="A-1")
		a.complete("A-1", "w-1", "did it", refs=["https://example.com/pr/1"])
		a.create({"title": "a question"}, tid="A-2")
		a.claim("w-1", tid="A-2")
		a.ask("A-2", "w-1", "left or right?")
		a.create({"title": "proposed"}, tid="A-3", agent="w-1")
		a.create({"title": "to reject"}, tid="A-4")
		a.claim("w-2", tid="A-4")
		a.complete("A-4", "w-2", "meh")
		b.create({"title": "parked", "waiting_on": "later"}, tid="B-1")  # hand-parked: never listed
		b.create({"title": "orphan"}, tid="B-2")
		raw = b.dir / "B-2.json"
		raw.write_text(json.dumps({**json.loads(raw.read_text(encoding="utf-8")), "blocked_by": ["B-99"]}), encoding="utf-8")
		mine = m.needs_you()
		self.assertEqual(json.loads(json.dumps(mine, ensure_ascii=False)), self.api_needs())  # the very same reply as the web panel's
		self.assertEqual([(g["id"], {i["id"]: i["kind"] for i in g["items"]}) for g in mine],  # order: score, then since (a date or a UTC time)
		                 [("a", {"A-1": "review", "A-2": "question", "A-3": "proposal", "A-4": "review"}), ("b", {"B-2": "stuck"})])
		self.assertEqual(mine[0]["items"][0]["id"], "A-1")  # the P1 first
		self.assertEqual(mine[0]["items"][0]["result"]["refs"], ["https://example.com/pr/1"])
		self.assertEqual(m.needs_you(board="b"), [g for g in mine if g["id"] == "b"])
		with self.assertRaisesRegex(Exception, "no board 'nope'"):
			m.needs_you(board="nope")
		# the four human-side writes answer short, like the worker writes
		replies = [m.approve_ticket("A-1", by="Omri"), m.answer("A-2", by="Omri", text="left"), m.accept_ticket("A-3", by="Omri"),
		           m.reject("A-4", by="Omri", reason="redo")]
		for r in replies:
			self.assertEqual(set(r) - {"note", "lease_left_s", "warnings"}, {"ok", "id", "status", "effective_status", "rev"})
			self.assertLess(len(json.dumps(r)), 200)
		self.assertEqual([r["status"] for r in replies], ["done", "todo", "todo", "todo"])
		self.assertEqual(self.api_needs(), [{"id": "b", "title": "B", "items": self.api_needs()[0]["items"]}])  # board a is clear

	def test_human_tool_not_a_worker_tool(self):
		from unblockd import store as st
		self.assertIn("needs_you", st.MCP_TOOLS)
		self.assertNotIn("needs_you", st.WORKER_TOOLS)


@unittest.skipIf(mcp is None, "mcp not installed")
class ActorNameTest(unittest.TestCase):
	"""API-17: every tool that names who is acting takes `agent` and `by`; agent wins; neither is refused."""
	ACTORS = {"create_ticket", "create_tickets", "accept_ticket", "update_ticket", "update_tickets", "split_ticket", "delete_ticket", "claim_next", "claim_ticket",
	          "heartbeat", "ask_human", "add_note", "answer", "approve_ticket", "reject", "report_ticket", "release", "complete", "arch_add_component",
	          "arch_update_component", "arch_delete_component", "arch_set_edge", "arch_remove_edge"}

	def setUp(self):
		from unblockd import mcp_server as m
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		d = Path(self.tmp.name) / "tickets"
		d.mkdir()
		(d / "unblockd.json").write_text(json.dumps({"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
			"default_owner": "Claude", "review": True, "agent_tickets": "propose"}), encoding="utf-8")
		self.addCleanup(setattr, m, "BOARDS", m.BOARDS)
		m.BOARDS = m.bd.Boards(dirs=[str(d)])
		self.m = m

	def test_every_actor_tool_takes_both_names(self):
		tools = {t.name: t.input_schema for t in asyncio.run(self.m.mcp.list_tools())}
		self.assertEqual({n for n, sc in tools.items() if "by" in sc["properties"]}, self.ACTORS)
		for n in self.ACTORS:
			sc = tools[n]
			self.assertIn("agent", sc["properties"], n)
			self.assertFalse({"agent", "by"} & set(sc.get("required", [])), n)  # one of the two, checked by _who
		for n, arg in (("add_note", "text"), ("answer", "text"), ("reject", "reason"), ("complete", "summary"), ("report_ticket", "summary"), ("ask_human", "question")):
			self.assertIn(arg, tools[n]["required"], n)  # defaulted in Python only because it follows the optional name

	def test_each_tool_accepts_the_other_name(self):
		from mcp.server.mcpserver.exceptions import ToolError
		m, s = self.m, self.m.BOARDS.get(None)
		# planner side: worker tools called with by=, human tools with agent=
		a = m.create_ticket("a", by="planner", prefix="B")["id"]
		self.assertEqual(s.get(a)["proposed_by"], "planner")
		m.accept_ticket(a, agent="Omri")
		b, c = m.create_tickets([{"prefix": "B", "title": "b"}, {"prefix": "B", "title": "c"}], by="planner")["ids"].values()
		for x in (b, c):
			m.accept_ticket(x, by="Omri")
		self.assertEqual(m.claim_ticket(a, by="w-1")["claim"]["agent"], "w-1")
		self.assertTrue(m.heartbeat(a, by="w-1", note="on it")["ok"])
		m.add_note(a, by="w-1", text="found it")
		self.assertEqual(s.get(a)["notes"][-1]["by"], "w-1")
		m.add_note(a, agent="w-1", by="someone-else", text="agent wins")
		self.assertEqual(s.get(a)["notes"][-1]["by"], "w-1")
		m.update_ticket(a, {"priority": "P1"}, by="w-1")
		m.update_tickets([{"id": a, "changes": {"priority": "P2"}}], by="w-1")
		self.assertEqual(s.get(a)["priority"], "P2")
		self.assertEqual(m.complete(a, by="w-1", summary="did it")["status"], "review")
		m.reject(a, agent="Omri", reason="redo")
		self.assertEqual(s.get(a)["notes"][-1]["by"], "Omri")
		got = m.claim_next(by="w-1")
		self.assertEqual(got["claim"]["agent"], "w-1")
		m.ask_human(got["id"], by="w-1", question="which way?")
		m.answer(got["id"], agent="Omri", text="left")
		self.assertEqual(s.get(got["id"])["waiting_on"], "")
		m.claim_ticket(got["id"], by="w-1")
		m.complete(got["id"], by="w-1", summary="done")
		m.approve_ticket(got["id"], agent="Omri")
		self.assertEqual(s.get(got["id"])["result"]["approved_by"], "Omri")
		other = next(x for x in (a, b, c) if s.get(x)["status"] == "todo" and x != got["id"])
		m.claim_ticket(other, by="w-2")
		m.release(other, by="w-2", reason="no time")
		self.assertIsNone(s.get(other)["claim"])
		kids = m.split_ticket(other, titles=["x", "y"], by="planner")["children"]
		m.delete_ticket(kids[-1]["id"], by="planner")
		self.assertEqual(len(s.get(other)["blocked_by"]), 2)  # the parent still names the deleted child: only the file went
		m.arch_add_component("web", "Web", by="planner")
		m.arch_add_component("store", "Store", by="planner")
		m.arch_update_component("web", by="planner", summary="the board")
		self.assertEqual(m.arch_set_edge("web", "store", "tickets as JSON", by="planner")["edges"][0]["text"], "tickets as JSON")
		self.assertEqual(m.arch_remove_edge("web", "store", by="planner")["edges"], [])
		m.arch_delete_component("web", by="planner")
		self.assertEqual([e["agent"] for e in s.activity(limit=6) if e["event"] == "arch"][:1], ["planner"])
		with self.assertRaisesRegex(ToolError, "say who is acting"):
			m.add_note(a, text="nobody")
		with self.assertRaisesRegex(ToolError, "say who is acting"):
			m.approve_ticket(a)


@unittest.skipIf(mcp is None, "mcp not installed")
class HttpServerTest(unittest.TestCase):
	"""`python -m unblockd.mcp_server --http` in a subprocess against a temp copy of example/, called with the SDK's HTTP client."""

	def serve(self, token=""):
		root = Path(self.tmp.name) / "projects"
		if not root.exists():
			shutil.copytree(Path(__file__).resolve().parent.parent / "example", root / "example")
		with socket.socket() as sk:
			sk.bind(("127.0.0.1", 0))
			port = sk.getsockname()[1]
		env = {**os.environ, "UNBLOCKD_ROOT": str(root), "MCP_PORT": str(port), "UNBLOCKD_TOKEN": token, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
		env.pop("TICKETS_DIR", None)
		proc = subprocess.Popen([sys.executable, "-m", "unblockd.mcp_server", "--http"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
		self.addCleanup(lambda: (proc.kill(), proc.wait()))
		for _ in range(100):
			try:
				with socket.create_connection(("127.0.0.1", port), 0.2):
					break
			except OSError:
				time.sleep(0.1)
		return f"http://127.0.0.1:{port}/mcp", root / "example"

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)

	def call(self, url, name, args, token=""):
		import httpx2
		from mcp import ClientSession
		from mcp.client.streamable_http import streamable_http_client

		async def go():
			http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"} if token else {})
			async with streamable_http_client(url, http_client=http) as (r, w), ClientSession(r, w) as c:
				init = await c.initialize()
				if name == "initialize":
					return init
				if name is None:
					return await c.list_tools()
				r = await c.call_tool(name, args)
				assert not r.is_error, r.content[0].text
				return json.loads(r.content[0].text)
		return asyncio.run(go())

	def test_tools_and_architecture_over_http(self):
		url, demo = self.serve()
		tools = {t.name: t for t in self.call(url, None, None).tools}
		for n in ("arch_set_edge", "arch_add_component", "architecture_map", "claim_next", "board_info", "accept_ticket", "approve_ticket", "create_tickets"):
			self.assertIn(n, tools)
		self.assertIn("components", tools["create_ticket"].input_schema["properties"])
		self.assertLessEqual({"agent", "by"}, set(tools["create_ticket"].input_schema["properties"]))  # the proposal gate's identity (either name, API-17), same tool as over stdio
		self.assertEqual(self.call(url, "list_boards", {})["id"], "example")  # one board -> one content block
		info = self.call(url, "board_info", {"board": "example"})
		self.assertTrue(info["components"] and {"key", "title"} <= set(info["components"][0]))
		self.assertEqual(info["version"], unblockd.__version__)  # CORE-84: board_info and serverInfo carry it
		self.assertEqual(self.call(url, "initialize", None).server_info.version, unblockd.__version__)
		arch = self.call(url, "architecture_map", {"board": "example"})
		a, b = arch["edges"][0]["from"], arch["edges"][0]["to"]
		new = self.call(url, "arch_set_edge", {"frm": a, "to": b, "text": "over http", "agent": "t-1", "board": "example"})
		self.assertIn("over http", [e["text"] for e in new["edges"]])
		self.assertIn("over http", (demo / "docs" / "arch" / f"{a}.md").read_text(encoding="utf-8"))  # the note on disk changed

	def test_token(self):
		url, _ = self.serve("s3cret")
		body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
		for hdr in ({}, {"Authorization": "Bearer nope"}):
			req = urllib.request.Request(url, body, {"Content-Type": "application/json", **hdr})
			with self.assertRaises(urllib.error.HTTPError) as cm:
				urllib.request.urlopen(req)
			self.assertEqual(cm.exception.code, 401)
			self.assertIn("UNBLOCKD_TOKEN", json.loads(cm.exception.read())["error"])
		self.assertIn("board_info", [t.name for t in self.call(url, None, None, "s3cret").tools])

	def test_host_header_checked(self):
		url, _ = self.serve()
		req = urllib.request.Request(url, b"{}", {"Content-Type": "application/json", "Host": "evil.example:80"})
		with self.assertRaises(urllib.error.HTTPError) as cm:
			urllib.request.urlopen(req)
		self.assertEqual(cm.exception.code, 421)


if __name__ == "__main__":
	unittest.main()
