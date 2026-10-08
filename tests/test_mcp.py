"""MCP prompt + resource carrying the worker protocol (skipped when the `mcp` package is missing)."""
import asyncio
import os
import tempfile
import unittest

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
		text = m.work_loop_text("game", "godot-dev")
		for word in ("claim_next(agent=<your name>, profile=godot-dev, board=game)", "godot-dev-1", "heartbeat", "ask_human", "release", "complete", "done_when", "conflict", "waiting", "dry", "Answer to", "Rejected"):
			self.assertIn(word, text)
		self.assertIn("board_info", m.work_loop_text())
		self.assertEqual(m.work_loop_resource(), m.work_loop_text())
		prompt = asyncio.run(m.mcp.get_prompt("work_loop", {"board": "game", "profile": "godot-dev"}))
		self.assertIn("claim_next", str(prompt))
		self.assertIn("work_loop", [p.name for p in asyncio.run(m.mcp.list_prompts())])
		self.assertIn("unblockd://work-loop", [str(r.uri) for r in asyncio.run(m.mcp.list_resources())])


if __name__ == "__main__":
	unittest.main()
