"""The chat's tool loop and reference resolution, against a fake Messages API (no key, no network)."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from unblockd import chat
from unblockd.boards import Boards


class FakeClaude(BaseHTTPRequestHandler):
	"""Round 1: ask for get_ticket BUG-1. Round 2: answer with what the tool returned."""
	seen: list = []

	def do_POST(self):
		req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
		FakeClaude.seen.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": req})
		last = req["messages"][-1]["content"]
		if isinstance(last, list) and last[0].get("type") == "tool_result":
			t = json.loads(last[0]["content"])
			out = {"stop_reason": "end_turn", "content": [{"type": "text", "text": f"#{t['id']} is {t['effective_status']}."}]}
		else:
			out = {"stop_reason": "tool_use", "content": [
				{"type": "text", "text": "Checking."},
				{"type": "tool_use", "id": "toolu_1", "name": "get_ticket", "input": {"id": "BUG-1"}}]}
		data = json.dumps(out).encode()
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)

	def log_message(self, *a):
		pass


class ChatTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		root = Path(self.tmp.name)
		for name in ("game", "site"):
			d = root / name / "tickets"
			d.mkdir(parents=True)
			(d / "unblockd.json").write_text(json.dumps({"title": name}), encoding="utf-8")
		self.b = Boards([str(root)])
		self.b.get("game").create({"title": "walls", "status": "doing"}, tid="BUG-1")
		self.b.get("site").create({"title": "readme"}, tid="DOC-1")
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeClaude)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.env = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
		os.environ["ANTHROPIC_API_KEY"] = "test-key"
		os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{self.srv.server_port}"
		FakeClaude.seen = []

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		for k, v in self.env.items():
			os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
		self.tmp.cleanup()

	def test_tool_loop_answers_from_real_data(self):
		out = chat.ask(self.b, "game", [{"role": "user", "content": "how is the wall bug?"}])
		self.assertEqual(out["reply"], "#BUG-1 is doing.")
		self.assertEqual(out["tools"], ["get_ticket"])
		first = FakeClaude.seen[0]
		self.assertEqual(first["headers"]["x-api-key"], "test-key")
		self.assertEqual(first["headers"]["anthropic-version"], "2023-06-01")
		self.assertIn("get_ticket", [t["name"] for t in first["body"]["tools"]])

	def test_references_are_resolved_into_the_message(self):
		out = chat.ask(self.b, "game", [{"role": "user", "content": "compare #BUG-1 with #site/DOC-1 and @site, #NOPE-9"}])
		self.assertEqual([(r["kind"], r["board"], r["id"]) for r in out["refs"]],
		                 [("board", "site", "site"), ("ticket", "game", "BUG-1"), ("ticket", "site", "DOC-1"), ("ticket", "game", "NOPE-9")])
		sent = FakeClaude.seen[0]["body"]["messages"][-1]["content"]
		self.assertIn('"title": "readme"', sent[0]["text"])  # the other board's ticket, inlined
		self.assertIn("no ticket NOPE-9", sent[0]["text"])

	def test_needs_a_key(self):
		os.environ.pop("ANTHROPIC_API_KEY")
		self.assertFalse(chat.enabled()["enabled"])
		with self.assertRaisesRegex(chat.ChatError, "ANTHROPIC_API_KEY"):
			chat.ask(self.b, None, [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
	unittest.main()
