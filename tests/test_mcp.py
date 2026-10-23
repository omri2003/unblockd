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

try:
	import mcp  # noqa: F401
except ImportError:
	mcp = None


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
		self.assertIn("board_info", m.work_loop_text())
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
		s = m.BOARDS.get(None)
		s.create({"title": "mine", "owner": "Omri", "tags": ["be"]}, tid="B-1")
		s.create({"title": "bots", "owner": "Claude", "tags": ["be"]}, tid="B-2")
		ids = lambda **kw: [t["id"] for t in m.next_ready(**kw)]
		self.assertEqual(ids(), ["B-1", "B-2"])
		self.assertEqual(ids(agent="backend-1"), ["B-2"])
		with self.assertRaisesRegex(ToolError, "matches no agent profile"):
			m.next_ready(agent="nobody")

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
		self.assertEqual(m.complete("B-1", "w-1", "done", principal="Dana")["result"]["principal"], "Dana")
		self.assertIn("principal", m.work_loop_text())

	def test_plan_from_spec_prompt(self):
		from unblockd import mcp_server as m
		self.assertIn("plan_from_spec", [p.name for p in asyncio.run(m.mcp.list_prompts())])
		text = str(asyncio.run(m.mcp.get_prompt("plan_from_spec", {"board": "game", "path": "docs/spec.md"})))
		for word in ("docs/spec.md", "board=", "AT MOST 10", "done_when", "STOP", "before that", "dependency order", "DATA"):
			self.assertIn(word, text)


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
				await c.initialize()
				if name is None:
					return await c.list_tools()
				r = await c.call_tool(name, args)
				assert not r.is_error, r.content[0].text
				return json.loads(r.content[0].text)
		return asyncio.run(go())

	def test_tools_and_architecture_over_http(self):
		url, demo = self.serve()
		tools = {t.name: t for t in self.call(url, None, None).tools}
		for n in ("arch_set_edge", "arch_add_component", "architecture_map", "claim_next", "board_info", "accept_ticket", "approve_ticket"):
			self.assertIn(n, tools)
		self.assertIn("components", tools["create_ticket"].input_schema["properties"])
		self.assertIn("agent", tools["create_ticket"].input_schema["required"])  # the proposal gate's identity, same tool as over stdio
		self.assertEqual(self.call(url, "list_boards", {})["id"], "example")  # one board -> one content block
		info = self.call(url, "board_info", {"board": "example"})
		self.assertTrue(info["components"] and {"key", "title"} <= set(info["components"][0]))
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
