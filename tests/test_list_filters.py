"""Planner lists: list(fields=, limit=) rows with only the asked keys (AI-47), on the Store, REST, CLI and Remote."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd import store as st
from unblockd.boards import Boards
from unblockd.store import Store, TicketError


class ListFieldsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "p" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text("{}", encoding="utf-8")
		self.s = Store(d)
		self.s.create({"title": "epic", "body": "background " * 100}, tid="A-1")
		self.s.create({"title": "api", "parent": "A-1", "touches": ["unblockd/store.py"], "done_when": ["works"]}, tid="A-2")
		self.s.create({"title": "ui", "parent": "A-1", "touches": ["static/"]}, tid="A-3")
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def get(self, path):
		try:
			with urllib.request.urlopen(self.base + path) as r:
				return r.status, json.loads(r.read())
		except urllib.error.HTTPError as e:
			return e.code, json.loads(e.read())

	def cli(self, *a):
		out = io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
			code = cli.main(["--dir", str(self.s.dir), *a])
		return code, json.loads(out.getvalue()) if code == 0 else None

	def test_store_limit_and_field_names(self):
		self.assertEqual([t["id"] for t in self.s.list(limit=2)], ["A-1", "A-2"])
		self.assertEqual(self.s.list(limit=0), [])
		with self.assertRaisesRegex(TicketError, "limit must be"):
			self.s.list(limit="3")
		self.assertEqual(st.list_fields("touches, done_when,touches"), ["touches", "done_when"])
		with self.assertRaisesRegex(TicketError, "unknown field"):
			st.list_fields(["touches", "nope"])
		row = st.summarize(self.s.get("A-2"), ["touches"])
		self.assertEqual(list(row), [*st.SUMMARY_KEYS, "touches"])

	def test_rest_fields_and_limit(self):
		code, rows = self.get("/api/tickets?fields=touches,parent&limit=2")
		self.assertEqual(code, 200)
		self.assertEqual([r["id"] for r in rows], ["A-1", "A-2"])
		self.assertEqual(set(rows[1]), set(st.SUMMARY_KEYS) | {"touches", "parent"})
		self.assertEqual((rows[1]["touches"], rows[1]["parent"]), (["unblockd/store.py"], "A-1"))
		self.assertIn("body", self.get("/api/tickets")[1][0])  # no fields: whole tickets, as the UI reads them
		self.assertEqual(self.get("/api/tickets?fields=colour")[0], 400)
		self.assertEqual(self.get("/api/tickets?limit=x")[0], 400)

	def test_cli_and_remote(self):
		code, rows = self.cli("list", "--fields", "done_when", "--limit", "2")
		self.assertEqual(code, 0)
		self.assertEqual([(r["id"], r["done_when"]) for r in rows], [("A-1", []), ("A-2", ["works"])])
		self.assertNotIn("body", rows[0])
		self.assertEqual(self.cli("list", "--fields", "colour")[0], 1)
		r = cli.Remote(self.base)
		self.assertEqual([t["id"] for t in r.list(limit=1)], ["A-1"])


if __name__ == "__main__":
	unittest.main()
