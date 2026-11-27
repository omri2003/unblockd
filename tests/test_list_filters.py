"""Planner lists: list(fields=, limit=) rows with only the asked keys (AI-47) and touches= / component= by the claim overlap rule
(CORE-52), on the Store, REST, CLI and Remote."""
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

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

	def test_cli_list_is_open_tickets_unless_asked(self):
		"""API-22: a planner's `list` printed every done ticket's row too (84 KB on a real board). Now: open (not done) by default,
		--all for every ticket, --status still picks one (done included); the limit counts after the filter. Same through Remote."""
		self.s.create({"title": "old", "status": "done"}, tid="A-4")
		self.s.create({"title": "new"}, tid="A-5")
		ids = lambda *a: [r["id"] for r in self.cli("list", *a)[1]]
		self.assertEqual(ids(), ["A-1", "A-2", "A-3", "A-5"])
		self.assertEqual(ids("--limit", "4"), ["A-1", "A-2", "A-3", "A-5"])
		self.assertEqual(ids("--all"), ["A-1", "A-2", "A-3", "A-4", "A-5"])
		self.assertEqual(ids("--status", "done"), ["A-4"])
		self.assertEqual(self.cli("list", "--all", "--status", "done")[0], 1)  # one or the other
		self.assertEqual([t["id"] for t in self.s.list(status="open")], ["A-1", "A-2", "A-3", "A-5"])
		r = cli.Remote(self.base)
		self.assertEqual([t["id"] for t in r.list(status="open", limit=2)], ["A-1", "A-2"])
		self.assertEqual(len(r.list()), 5)
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": self.base}):
			self.assertEqual(ids(), ["A-1", "A-2", "A-3", "A-5"])
			self.assertEqual(ids("--all", "--fields", "touches")[3], "A-4")

	def test_cli_knowledge_lists_blind_tickets_and_broken_docs(self):
		"""API-22: `cli knowledge` = the lint part of GET knowledge: open tickets with no doc of their own (with suggested docs) and
		broken doc refs, locally and through Remote, without the whole graph."""
		root = self.s.dir.parent
		(root / "docs").mkdir()
		(root / "docs" / "store.md").write_text("unblockd/store.py keeps the rules.", encoding="utf-8")
		self.s.update("A-3", {"docs": ["docs/nope.md"]})
		self.s.create({"title": "shipped", "status": "done"}, tid="A-4")
		code, got = self.cli("knowledge")
		self.assertEqual(code, 0)
		self.assertEqual(set(got), {"no_docs", "broken"})
		self.assertEqual([(t["id"], t["title"], t["suggested"]) for t in got["no_docs"]],
		                 [("A-1", "epic", []), ("A-2", "api", ["docs/store.md"]), ("A-3", "ui", [])])  # done A-4 is not listed
		self.assertEqual([(b["path"], b["tickets"]) for b in got["broken"]], [("docs/nope.md", ["A-3"])])
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": self.base}):
			self.assertEqual(self.cli("knowledge"), (0, got))

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


class TouchesFilterTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		root = Path(self.tmp.name) / "p"
		d = root / "tickets"
		d.mkdir(parents=True)
		(root / "docs" / "arch").mkdir(parents=True)
		(root / "docs" / "arch" / "store.md").write_text("---\npaths: unblockd/store.py\n---\n# Store\n", encoding="utf-8")
		(root / "docs" / "arch" / "web.md").write_text("---\npaths: static/**\n---\n# Web\n", encoding="utf-8")
		(d / "unblockd.json").write_text("{}", encoding="utf-8")
		self.s = Store(d)
		self.s.create({"title": "exact", "touches": ["unblockd/store.py"]}, tid="A-1")
		self.s.create({"title": "folder", "touches": ["unblockd/"]}, tid="A-2")
		self.s.create({"title": "glob", "touches": ["unblockd/*.py"]}, tid="A-3")
		self.s.create({"title": "mentions unblockd/store.py in its body only", "body": "see unblockd/store.py", "touches": ["static/index.html"]}, tid="A-4")
		self.s.create({"title": "named component", "components": ["web"]}, tid="A-5")
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def ids(self, **kw):
		return [t["id"] for t in self.s.list(**kw)]

	def test_overlap_rule_not_substring(self):
		self.assertEqual(self.ids(touches="unblockd/store.py"), ["A-1", "A-2", "A-3"])
		self.assertEqual(self.ids(q="unblockd/store.py"), ["A-1", "A-4"])  # what q= used to give a planner
		self.assertEqual(self.ids(touches="unblockd/web.py"), ["A-2", "A-3"])
		self.assertEqual(self.ids(touches="static/*"), ["A-4"])
		self.assertEqual(self.ids(touches="nothing/here.py"), [])
		with self.assertRaisesRegex(TicketError, "touches must be"):
			self.s.list(touches=" ")

	def test_component_explicit_or_by_paths(self):
		self.assertEqual(self.ids(component="web"), ["A-4", "A-5"])
		self.assertEqual(self.ids(component="store"), ["A-1", "A-2", "A-3"])  # the folder unblockd/ holds the note's path: it overlaps
		self.assertEqual(self.ids(component="ghost"), [])

	def test_rows_carry_touches_and_holder_on_every_front_door(self):
		self.s.claim("w-1", tid="A-3")
		rows = {r["id"]: r for r in [st.summarize(t, st.list_fields(None, matched=True)) for t in self.s.list(touches="unblockd/store.py")]}
		self.assertEqual((rows["A-3"]["holder"], rows["A-1"]["holder"], rows["A-3"]["touches"]), ("w-1", None, ["unblockd/*.py"]))
		with urllib.request.urlopen(self.base + "/api/tickets?touches=unblockd/store.py") as r:
			rest = json.loads(r.read())
		self.assertEqual([(x["id"], x["holder"]) for x in rest], [("A-1", None), ("A-2", None), ("A-3", "w-1")])
		self.assertNotIn("body", rest[0])
		with urllib.request.urlopen(self.base + "/api/tickets?component=web") as r:
			self.assertEqual([x["id"] for x in json.loads(r.read())], ["A-4", "A-5"])
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			self.assertEqual(cli.main(["--dir", str(self.s.dir), "list", "--touches", "unblockd/*"]), 0)
		self.assertEqual([(x["id"], x["holder"], x["touches"]) for x in json.loads(out.getvalue())],
		                 [("A-1", None, ["unblockd/store.py"]), ("A-2", None, ["unblockd/"]), ("A-3", "w-1", ["unblockd/*.py"])])
		r = cli.Remote(self.base)
		self.assertEqual([t["id"] for t in r.list(touches="static", component="web")], ["A-4"])


if __name__ == "__main__":
	unittest.main()
