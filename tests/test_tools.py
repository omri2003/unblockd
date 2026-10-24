"""Worker tool sets: base minus a profile's tools_off plus its tools; saved to unblockd.json from the Agents panel."""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import store as st
from unblockd.boards import Boards
from unblockd.store import Store, TicketError

try:
	import mcp
except ImportError:
	mcp = None


class ToolsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.d = Path(self.tmp.name) / "p" / "tickets"
		self.d.mkdir(parents=True)
		(self.d / "unblockd.json").write_text(json.dumps({"title": "T", "agents": [{"name": "godot", "tags": ["gd"]}, {"name": "design"}]}), encoding="utf-8")
		self.s = Store(self.d)

	def tearDown(self):
		self.tmp.cleanup()

	def test_off_for_everyone_on_for_one_profile_and_the_reverse(self):
		base = [t for t in st.WORKER_TOOLS if t != "read_doc"]
		v = self.s.set_tools(base, {"godot": {"tools": ["read_doc"]}, "design": {"tools_off": ["get_ticket"]}})
		eff = {p["name"]: p["effective"] for p in v["profiles"]}
		self.assertIn("read_doc", eff["godot"])
		self.assertNotIn("read_doc", eff["design"])
		self.assertNotIn("get_ticket", eff["design"])
		self.assertIn("get_ticket", eff["godot"])
		self.assertEqual(Store(self.d).worker_tools("godot"), eff["godot"])  # read back from the file
		raw = json.loads((self.d / "unblockd.json").read_text(encoding="utf-8"))
		self.assertEqual((raw["title"], raw["agents"][0]["tools"], raw["agents"][1]["tools_off"]), ("T", ["read_doc"], ["get_ticket"]))

	def test_only_differences_are_stored_and_the_default_base_is_not_written(self):
		self.s.set_tools(list(st.WORKER_TOOLS), {"godot": {"tools": ["claim_next", "attach_file"], "tools_off": ["delete_ticket"]}})
		raw = json.loads((self.d / "unblockd.json").read_text(encoding="utf-8"))
		self.assertNotIn("worker_tools", raw)
		self.assertEqual(raw["agents"][0].get("tools"), ["attach_file"])  # claim_next is already in base
		self.assertNotIn("tools_off", raw["agents"][0])  # delete_ticket is not in base: nothing to remove

	def test_refusals(self):
		for args, word in (((["nope"], {}), "unknown tool"), (([], {}), "at least one"), ((["claim_next"], {"ghost": {}}), "unknown profile")):
			with self.assertRaisesRegex(TicketError, word):
				self.s.set_tools(*args)

	def test_rest(self):
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		try:
			url = f"http://127.0.0.1:{srv.server_port}/api/tools"
			self.assertEqual(json.loads(urllib.request.urlopen(url).read())["base"], list(st.WORKER_TOOLS))
			req = urllib.request.Request(url, method="PUT", data=json.dumps({"base": ["claim_next", "complete"], "profiles": {"godot": {"tools": ["read_doc"]}}}).encode(), headers={"Content-Type": "application/json"})
			v = json.loads(urllib.request.urlopen(req).read())
			self.assertEqual(v["profiles"][0]["effective"], ["claim_next", "complete", "read_doc"])
		finally:
			srv.shutdown()
			srv.server_close()

	@unittest.skipIf(mcp is None, "mcp not installed")
	def test_catalogue_matches_the_mcp_server(self):
		import asyncio
		from unblockd import mcp_server as m
		self.assertEqual(sorted(t.name for t in asyncio.run(m.mcp.list_tools())), sorted(st.MCP_TOOLS))


if __name__ == "__main__":
	unittest.main()
