"""Architecture notes: parsing, ticket -> component mapping, context(), the budget order, problems, REST."""
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import knowledge as kn
from unblockd.boards import Boards
from unblockd.store import Store, TicketError

S3 = """---
paths: src/storage/**, infra/s3.tf
---
# S3 bucket
Stores uploaded photos.
Second line of the same paragraph.

## Connections
- [database](database.md) — every upload writes a row in `photos`; deleting an object must delete the row
- [[api]] — api issues presigned PUT URLs; never proxy file bytes
- [missing](nope.md) — a note that does not exist
- [outside](../elsewhere.md) — outside the folder
- [site](https://example.com) — external, ignored

## Notes
- [frontend](frontend.md) — not under Connections: no edge
"""


class ArchTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.repo = Path(self.tmp.name) / "app"
		a = self.repo / "docs" / "arch"
		a.mkdir(parents=True)
		(self.repo / "src" / "storage").mkdir(parents=True)
		(self.repo / "src" / "storage" / "put.py").write_text("x", encoding="utf-8")
		(self.repo / "src" / "api").mkdir()
		(self.repo / "src" / "api" / "app.py").write_text("x", encoding="utf-8")
		(self.repo / "infra").mkdir()
		(self.repo / "infra" / "s3.tf").write_text("x", encoding="utf-8")
		(self.repo / "BOARD.md").write_text("B" * 200, encoding="utf-8")
		(a / "s3.md").write_text(S3, encoding="utf-8")
		(a / "database.md").write_text("# Database\nPostgres.\n\n## connections\n- [api](api.md)\n", encoding="utf-8")
		(a / "api.md").write_text("---\npaths: src/api/**\n---\n\n# API\n\nHTTP layer.\n\n### Connections\n- [[s3]]: bytes go direct\n  more text\n- [[database|db]] — reads and writes `photos`\n\n## Other\n", encoding="utf-8")
		(a / "frontend.md").write_text("Plain note, no heading.\n", encoding="utf-8")
		self.tdir = self.repo / "tickets"
		self.tdir.mkdir()
		self.cfg({"architecture": "docs/arch", "docs": ["BOARD.md"], "pillars": [{"key": "T"}]})

	def cfg(self, c):
		(self.tdir / "unblockd.json").write_text(json.dumps(c), encoding="utf-8")
		self.s = Store(self.tdir)

	def tearDown(self):
		self.tmp.cleanup()

	def test_parsing(self):
		a = kn.components(self.s)
		by = {c["key"]: c for c in a["components"]}
		self.assertEqual(sorted(by), ["api", "database", "frontend", "s3"])
		self.assertEqual((by["s3"]["title"], by["s3"]["paths"], by["s3"]["path"]), ("S3 bucket", ["src/storage/**", "infra/s3.tf"], "docs/arch/s3.md"))
		self.assertEqual(by["s3"]["summary"], "Stores uploaded photos. Second line of the same paragraph.")
		self.assertEqual((by["frontend"]["title"], by["frontend"]["summary"], by["frontend"]["paths"]), ("frontend", "Plain note, no heading.", []))
		self.assertEqual(by["api"]["summary"], "HTTP layer.")
		edges = {(e["from"], e["to"]): e["text"] for e in a["edges"]}
		self.assertEqual(edges[("s3", "database")], "every upload writes a row in `photos`; deleting an object must delete the row")
		self.assertEqual(edges[("s3", "api")], "api issues presigned PUT URLs; never proxy file bytes")
		self.assertEqual(edges[("api", "s3")], "bytes go direct")  # wikilink, ":" stripped, case-insensitive level-3 heading
		self.assertEqual(edges[("api", "database")], "reads and writes `photos`")  # [[x|label]]
		self.assertEqual(edges[("database", "api")], "")  # lower-case heading, no contract
		self.assertNotIn(("s3", "frontend"), edges)  # link outside Connections
		self.assertEqual(len(a["edges"]), 5)
		self.assertEqual(a["broken_links"], [{"from": "s3", "target": "nope.md"}, {"from": "s3", "target": "../elsewhere.md"}])

	def test_off_and_missing_folder(self):
		self.cfg({"architecture": None})  # null = off (the default is on: "docs/arch", used when that folder exists)
		self.assertEqual(kn.components(self.s), {"folder": None, "components": [], "edges": [], "broken_links": []})
		self.assertNotIn("architecture", kn.context(self.s, self.s.create({"title": "x", "touches": ["src/api/app.py"]}, prefix="T")["id"]))
		self.cfg({"architecture": "docs/none"})  # on, but the folder does not exist yet: folder reported, no notes, no error
		a = kn.architecture(self.s)
		self.assertEqual((a["folder"], a["components"], a["edges"], "error" in a), ("docs/none", [], [], False))
		self.cfg({"architecture": "../out"})
		self.assertIn("outside", kn.components(self.s)["error"])
		t = self.s.create({"title": "x"}, prefix="T")
		self.assertNotIn("architecture", kn.context(self.s, t["id"]))  # missing folder never breaks context()
		with self.assertRaisesRegex(TicketError, "architecture must be"):
			self.cfg({"architecture": 3})

	def test_mapping_explicit_and_derived(self):
		a = kn.components(self.s)
		t = {"touches": ["src/storage/put.py"], "components": ["api", "gone"], "docs": ["./docs/arch/frontend.md"]}
		self.assertEqual(kn.ticket_components(t, a), ["api", "s3"])  # explicit first (a removed note is ignored), then derived; a docs entry does not link
		self.assertEqual(kn.ticket_components({"touches": ["src/storage"]}, a), ["s3"])  # folder overlaps glob
		self.assertEqual(kn.ticket_components({"touches": ["src/storage/put.py"], "components": ["s3"]}, a), ["s3"])  # explicit + derived: once
		self.assertEqual(kn.ticket_components({"touches": ["README.md"], "docs": ["docs/arch/s3.md"]}, a), [])
		self.assertEqual(kn.ticket_components({}, a), [])

	def test_components_field(self):
		s = self.s
		t = s.create({"title": "x", "components": ["api", " s3 ", "api"]}, prefix="T")
		self.assertEqual(t["components"], ["api", "s3"])  # deduped, trimmed, order kept
		self.assertEqual(s.create({"title": "y"}, prefix="T")["components"], [])
		with self.assertRaisesRegex(TicketError, r"unknown component\(s\) nope: this board's components are api, database, frontend, s3"):
			s.create({"title": "z", "components": ["api", "nope"]}, prefix="T")
		with self.assertRaisesRegex(TicketError, "unknown component"):
			s.update(t["id"], {"components": ["nope"]})
		self.assertEqual(s.update(t["id"], {"components": []})["components"], [])
		with self.assertRaisesRegex(TicketError, "invalid components"):
			s.update(t["id"], {"components": "api"})
		c = s.update(t["id"], {"components": ["api", "s3"]})
		kids = s.split(c["id"], ["a", "b"])["children"]
		self.assertEqual([k["components"] for k in kids], [["api", "s3"], ["api", "s3"]])  # split copies
		(self.repo / "docs" / "arch" / "s3.md").unlink()  # a note removed later: the ticket stays editable and splittable
		self.assertEqual(s.update(t["id"], {"components": ["api", "s3"], "title": "renamed"})["title"], "renamed")
		self.assertEqual(len(s.split(kids[0]["id"], ["c", "d"])["children"]), 2)
		with self.assertRaisesRegex(TicketError, "unknown component"):
			s.update(kids[1]["id"], {"components": ["s3", "database", "x"]})

	def test_cli_component(self):
		import contextlib, io
		from unblockd import cli

		def call(*a):
			out, err = io.StringIO(), io.StringIO()
			with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
				code = cli.main(["--dir", str(self.tdir), *a])
			return code, (json.loads(out.getvalue()) if code == 0 else err.getvalue())
		code, t = call("create", "--prefix", "T", "--title", "cli", "--component", "api", "--component", "s3")
		self.assertEqual((code, t["components"]), (0, ["api", "s3"]))
		self.assertEqual(call("update", t["id"], "--component", "database")[1]["components"], ["database"])  # sets, does not add
		code, msg = call("update", t["id"], "--component", "zzz")
		self.assertEqual(code, 1)
		self.assertIn("unknown component", msg)
		self.assertEqual(call("update", t["id"], "--json", '{"title": "t2"}')[1]["components"], ["database"])  # no flag: untouched

	def test_components_need_an_architecture(self):
		self.cfg({"architecture": None})
		with self.assertRaisesRegex(TicketError, 'no architecture.*Set "architecture" in unblockd.json'):
			self.s.create({"title": "x", "components": ["api"]}, prefix="T")
		self.assertEqual(self.s.create({"title": "x", "components": []}, prefix="T")["components"], [])
		self.cfg({"architecture": "docs/none"})  # folder missing = no components either
		with self.assertRaisesRegex(TicketError, "no components yet.*Add one on the Map"):
			self.s.create({"title": "x", "components": ["api"]}, prefix="T")

	def test_context_neighbours_and_others(self):
		t = self.s.create({"title": "presign", "touches": ["infra/s3.tf"]}, prefix="T")
		ctx = kn.context(self.s, t["id"])
		self.assertIn("architecture", ctx["note"])
		self.assertEqual(("docs/arch/s3.md", "component:s3"), next((d["path"], d["source"]) for d in ctx["docs"] if d["source"].startswith("component")))
		arch = ctx["architecture"]
		self.assertEqual(arch["components"], ["s3"])
		self.assertTrue(all(n["trust"] == "untrusted" and n["text"].startswith("<untrusted ") for n in arch["neighbours"]))  # the notes are UI-editable
		self.assertTrue(all(d["trust"] == "untrusted" and d["content"].startswith("<untrusted ") for d in ctx["docs"] if d["source"].startswith("component")))
		got = {(n["key"], n["dir"]): re.sub(r"</?untrusted[^>]*>", "", n["text"]) for n in arch["neighbours"]}
		self.assertEqual(got, {("database", "out"): "every upload writes a row in `photos`; deleting an object must delete the row",
		                       ("api", "out"): "api issues presigned PUT URLs; never proxy file bytes",
		                       ("api", "in"): "bytes go direct"})
		db = next(n for n in arch["neighbours"] if n["key"] == "database")
		self.assertEqual((db["title"], db["summary"], db["path"]), ("Database", '<untrusted source="component database note">Postgres.</untrusted>', "docs/arch/database.md"))
		self.assertEqual(len(arch["neighbours"]), 3)
		self.assertEqual(arch["others"], ["frontend"])
		u = self.s.create({"title": "plain", "touches": ["docs/x.md"]}, prefix="T")
		self.assertNotIn("architecture", kn.context(self.s, u["id"]))  # unmapped

	def test_tight_budget_spends_on_ticket_docs_first(self):
		(self.repo / "notes.md").write_text("N" * 100, encoding="utf-8")
		t = self.s.create({"title": "x", "docs": ["notes.md"], "touches": ["src/api/app.py"]}, prefix="T")
		budget = len((self.repo / "docs" / "arch" / "api.md").read_text(encoding="utf-8")) + 150
		ctx = kn.context(self.s, t["id"], budget=budget)  # api note + notes fit, the 200-char board doc does not
		self.assertEqual([d["path"] for d in ctx["docs"]], ["BOARD.md", "docs/arch/api.md", "notes.md"])  # output order unchanged
		self.assertEqual({d["path"]: "content" in d for d in ctx["docs"]}, {"BOARD.md": False, "docs/arch/api.md": True, "notes.md": True})
		self.assertLessEqual(ctx["inlined_chars"], 330)

	def test_architecture_view_and_problems(self):
		(self.repo / "docs" / "arch" / "dead.md").write_text("---\npaths: nothing/here/**, src/storage\n---\n# Dead\nx\n", encoding="utf-8")
		a = self.s.create({"title": "one", "components": ["api"]}, prefix="T")  # explicit
		b = self.s.create({"title": "two", "touches": ["src/api/app.py", "infra/s3.tf"], "blocked_by": [a["id"]]}, prefix="T")  # derived
		d = self.s.create({"title": "done", "touches": ["src/api/app.py"], "status": "done"}, prefix="T")
		v = kn.architecture(self.s)
		by = {c["key"]: c for c in v["components"]}
		self.assertEqual(by["api"]["tickets"], [a["id"], b["id"]])
		self.assertEqual(by["api"]["counts"], {"todo": 1, "pending": 1, "doing": 0, "review": 0})
		self.assertEqual(by["s3"]["counts"]["pending"], 1)
		self.assertEqual(v["ticket_map"], {a["id"]: ["api"], b["id"]: ["api", "s3"]})
		p = v["problems"]
		self.assertEqual(p["cross"], [{"ticket": b["id"], "components": ["api", "s3"]}])
		self.assertEqual(p["no_contract"], [{"from": "database", "to": "api"}])
		self.assertEqual(p["dead_paths"], [{"key": "dead", "glob": "nothing/here/**"}])  
		self.assertEqual(len(p["broken_links"]), 2)
		self.assertNotIn(d["id"], v["ticket_map"])

	def test_rest_endpoint(self):
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		self.addCleanup(srv.server_close)
		self.addCleanup(srv.shutdown)
		bid = "app"
		for path in ("/api/architecture", f"/api/b/{bid}/architecture"):
			with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}{path}") as r:
				body = json.loads(r.read())
			self.assertEqual((r.status, body["folder"], len(body["components"])), (200, "docs/arch", 4))
			self.assertIn("problems", body)


if __name__ == "__main__":
	unittest.main()
