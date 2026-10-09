"""Architecture writes: the server edits the notes (add / update / delete component, set / remove edge) and touches only the managed parts."""
import json
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

# a hand-written note with everything the writer must leave alone: other front matter, a second section, prose after Connections,
# a wikilink line, a line that is not an edge, an external link
STORE = """---
owner: platform team
paths: src/store/**
reviewed: 2026-09-01
---
# Store
Keeps tickets as JSON files.
It is the only code that writes them.

## Design notes
Free prose, with a [link](https://example.com) and `code`.
- bullet that links [api](api.md) outside Connections: not an edge

## Connections
- [[api]] — the api calls every method; never reads files
Remember: keep this section short.
- [site](https://example.com) — external, ignored
- [database](database.md) — rows only

## Later
Closing words, no newline at the end"""
API = "# API\nHTTP layer.\n\n## Connections\n- [Store](store.md) — one call per route\n"
DATABASE = "# Database\r\nPostgres.\r\n\r\n## Connections\r\n- [Store](store.md)\r\n"
PLAIN = "Plain note, no heading.\n"


class ArchWriteTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.repo = Path(self.tmp.name) / "app"
		self.arch = self.repo / "docs" / "arch"
		self.arch.mkdir(parents=True)
		for k, v in {"store": STORE, "api": API, "database": DATABASE, "plain": PLAIN}.items():
			(self.arch / f"{k}.md").write_bytes(v.encode())
		self.tdir = self.repo / "tickets"
		self.tdir.mkdir()
		(self.tdir / "unblockd.json").write_text("{}", encoding="utf-8")  # a board; architecture is on by default and docs/arch exists
		self.s = Store(self.tdir)

	def tearDown(self):
		self.tmp.cleanup()

	def note(self, k):
		return (self.arch / f"{k}.md").read_bytes().decode()

	def keys(self):
		return [c["key"] for c in kn.components(self.s)["components"]]

	def edges(self):
		return {(e["from"], e["to"]): e["text"] for e in kn.components(self.s)["edges"]}

	def test_default_on_and_missing_folder_behaves_as_off(self):
		self.assertEqual(self.s.config["architecture"], "docs/arch")
		other = Path(self.tmp.name) / "empty"
		(other / "tickets").mkdir(parents=True)
		s = Store(other / "tickets")
		self.assertEqual(kn.components(s), {"folder": "docs/arch", "components": [], "edges": [], "broken_links": []})  # no error, nothing drawn
		a = kn.architecture(s)
		self.assertEqual((a["components"], a["problems"]["cross"], "error" in a), ([], [], False))
		t = s.create({"title": "x", "touches": ["src/a.py"]}, prefix="T")
		self.assertNotIn("architecture", kn.context(s, t["id"]))
		self.assertEqual(s.create({"title": "y", "components": []}, prefix="T")["components"], [])
		with self.assertRaisesRegex(TicketError, "no components yet"):
			s.create({"title": "z", "components": ["web"]}, prefix="T")
		kn.add_component(s, "web", "Web", by="me")  # the first component creates the folder
		self.assertEqual([c["key"] for c in kn.components(s)["components"]], ["web"])
		self.assertEqual(s.create({"title": "z", "components": ["web"]}, prefix="T")["components"], ["web"])

	def test_off_refuses_writes(self):
		(self.tdir / "unblockd.json").write_text(json.dumps({"architecture": None}), encoding="utf-8")
		with self.assertRaisesRegex(TicketError, "architecture is off"):
			kn.add_component(Store(self.tdir), "web", "Web", by="me")

	def test_folder_that_is_a_file_is_a_clear_error(self):
		(self.repo / "docs" / "blocked").write_text("x", encoding="utf-8")
		(self.tdir / "unblockd.json").write_text(json.dumps({"architecture": "docs/blocked"}), encoding="utf-8")
		s = Store(self.tdir)
		self.assertEqual(kn.components(s)["components"], [])
		with self.assertRaisesRegex(TicketError, "cannot write the architecture notes"):
			kn.add_component(s, "web", "Web", by="me")

	def test_round_trip_is_byte_identical(self):
		for key in self.keys():
			text = self.note(key)
			sc = kn._scan(text, key, lambda raw, wiki: None)
			again = kn._edit_note(text, key, lambda raw, wiki: None, sc["title"], sc["summary"], sc["paths"])
			self.assertEqual(again, text, key)
			before = {k: self.note(k) for k in self.keys()}
			kn.update_component(self.s, key, by="me", title=sc["title"], summary=sc["summary"], paths=sc["paths"])  # no change: no write
			self.assertEqual({k: self.note(k) for k in self.keys()}, before)
		# a canonical edge written again is the same bytes
		kn.set_edge(self.s, "api", "store", "one call per route", by="me")
		self.assertEqual(self.note("api"), API.replace("[Store](store.md)", "[Store](store.md)"))

	def test_edge_write_touches_only_its_line(self):
		before = self.note("store")
		kn.set_edge(self.s, "store", "database", "rows in `tickets`; deleting a ticket deletes the row", by="me")
		after = self.note("store")
		self.assertEqual(after, before.replace("- [database](database.md) — rows only", "- [Database](database.md) — rows in `tickets`; deleting a ticket deletes the row"))
		kn.set_edge(self.s, "store", "plain", "a new edge", by="me")  # new line goes last in Connections, before the next section
		new = self.note("store")
		self.assertIn("rows in `tickets`; deleting a ticket deletes the row\n- [plain](plain.md) — a new edge\n\n## Later", new)
		self.assertIn("Remember: keep this section short.\n", new)  # unmanaged line in Connections
		self.assertIn("- [site](https://example.com) — external, ignored\n", new)
		self.assertIn("- [[api]] — the api calls every method; never reads files\n", new)  # wikilink line we did not touch
		self.assertTrue(new.startswith(before.split("# Store")[0]))  # other front matter
		self.assertTrue(new.endswith("Closing words, no newline at the end"))  # the last line keeps having no newline
		self.assertEqual(self.edges()[("store", "api")], "the api calls every method; never reads files")
		kn.set_edge(self.s, "store", "api", "now canonical", by="me")  # replaced in place, canonical form
		self.assertIn("- [API](api.md) — now canonical\nRemember", self.note("store"))
		kn.remove_edge(self.s, "store", "plain", by="me")
		self.assertNotIn("plain.md", self.note("store"))

	def test_set_edge_without_a_connections_section_appends_one(self):
		kn.set_edge(self.s, "plain", "api", "calls it", by="me")
		self.assertEqual(self.note("plain"), PLAIN + "\n## Connections\n- [API](api.md) — calls it\n")
		kn.set_edge(self.s, "plain", "database", "", by="me")  # empty text: an edge with no contract
		self.assertEqual(self.note("plain").splitlines()[-1], "- [Database](database.md)")
		self.assertIn({"from": "plain", "to": "database"}, kn.architecture(self.s)["problems"]["no_contract"])
		(self.arch / "nonl.md").write_bytes(b"# No newline")  # a file that does not end with one
		kn.set_edge(self.s, "nonl", "api", "x", by="me")
		self.assertEqual(self.note("nonl"), "# No newline\n\n## Connections\n- [API](api.md) — x\n")

	def test_crlf_is_kept(self):
		kn.set_edge(self.s, "database", "api", "reads", by="me")
		self.assertEqual(self.note("database"), DATABASE + "- [API](api.md) — reads\r\n")
		kn.update_component(self.s, "database", title="DB", summary="Relational.", by="me")
		self.assertEqual(self.note("database").splitlines()[:2], ["# DB", "Relational."])
		self.assertNotIn("\n", self.note("database").replace("\r\n", ""))

	def test_update_component_managed_parts_only(self):
		before = self.note("store")
		kn.update_component(self.s, "store", by="me", title="Ticket store", summary="One JSON file per ticket.", paths=["src/store/**", "tests/test_store.py"])
		after = self.note("store")
		self.assertEqual(after, before.replace("# Store", "# Ticket store").replace("Keeps tickets as JSON files.\nIt is the only code that writes them.", "One JSON file per ticket.")
		                 .replace("paths: src/store/**", "paths: src/store/**, tests/test_store.py"))
		c = next(c for c in kn.components(self.s)["components"] if c["key"] == "store")
		self.assertEqual((c["title"], c["summary"], c["paths"]), ("Ticket store", "One JSON file per ticket.", ["src/store/**", "tests/test_store.py"]))
		kn.update_component(self.s, "store", by="me", paths=[])  # other front matter lines stay, so does the block
		self.assertIn("---\nowner: platform team\nreviewed: 2026-09-01\n---\n# Ticket store", self.note("store"))
		kn.update_component(self.s, "api", by="me", paths=["src/api/**"])  # no front matter: created
		self.assertTrue(self.note("api").startswith("---\npaths: src/api/**\n---\n# API\n"))
		kn.update_component(self.s, "api", by="me", paths=[])  # and removed again
		self.assertEqual(self.note("api"), API)
		kn.update_component(self.s, "plain", by="me", title="Plain", summary="Now it has both.")  # no title, no summary heading
		self.assertEqual(self.note("plain"), "# Plain\n\nNow it has both.\n")  # the stray paragraph was the summary; the title went above it
		kn.update_component(self.s, "plain", by="me", summary="")
		self.assertEqual(self.note("plain"), "# Plain\n")

	def test_summary_inserted_after_the_title(self):
		(self.arch / "bare.md").write_text("# Bare\n\n## Connections\n- [API](api.md)\n", encoding="utf-8")
		kn.update_component(self.s, "bare", by="me", summary="Hello  there\nworld")
		self.assertEqual(self.note("bare"), "# Bare\n\nHello there world\n\n## Connections\n- [API](api.md)\n")
		(self.arch / "tight.md").write_text("# Tight\n## Connections\n", encoding="utf-8")
		kn.update_component(self.s, "tight", by="me", summary="S")
		self.assertEqual(self.note("tight"), "# Tight\n\nS\n\n## Connections\n")
		# a Connections line is never mistaken for the summary
		self.assertEqual(kn.components(self.s)["components"][self.keys().index("tight")]["summary"], "S")

	def test_add_component(self):
		a = kn.add_component(self.s, "web", "Web UI", "The board page.", ["static/**"], by="human")
		self.assertEqual(self.note("web"), "---\npaths: static/**\n---\n# Web UI\n\nThe board page.\n")
		self.assertIn("web", [c["key"] for c in a["components"]])  # returns the new architecture payload
		kn.add_component(self.s, "min", "Min", by="human")
		self.assertEqual(self.note("min"), "# Min\n")
		self.assertEqual(self.s.activity(limit=1)[0]["event"], "arch")

	def test_delete_component(self):
		kn.set_edge(self.s, "plain", "store", "x", by="me")
		kn.set_edge(self.s, "api", "database", "y", by="me")
		self.s.create({"title": "open", "components": ["store"]}, prefix="T")
		done = self.s.create({"title": "closed", "components": ["database"], "status": "done"}, prefix="T")
		with self.assertRaisesRegex(TicketError, r"store is still a component of open ticket\(s\) T-1: reassign first"):
			kn.delete_component(self.s, "store", by="me")
		self.assertIn("store", self.keys())
		a = kn.delete_component(self.s, "database", by="me")  # only a done ticket names it: allowed; it keeps the key
		self.assertNotIn("database", [c["key"] for c in a["components"]])
		self.assertEqual(self.s.get(done["id"])["components"], ["database"])
		self.assertNotIn("database.md", self.note("api"))  # the incoming line went
		self.assertNotIn("database.md", self.note("store"))
		self.assertIn("- [[api]] — the api calls", self.note("store"))  # the others stayed
		self.assertFalse((self.arch / "database.md").exists())
		self.assertEqual(kn.components(self.s)["broken_links"], [])
		self.s.update("T-1", {"status": "done"})
		kn.delete_component(self.s, "store", by="me")
		self.assertNotIn("store.md", self.note("plain") + self.note("api"))

	def test_validation(self):
		def bad(rx, fn, *a, **kw):
			with self.assertRaisesRegex(TicketError, rx):
				fn(self.s, *a, **kw)
		bad("bad key", kn.add_component, "Web", "Web")
		bad("bad key", kn.add_component, "-web", "Web")
		bad("bad key", kn.add_component, "a" * 41, "Web")
		bad("bad key", kn.add_component, "../x", "Web")
		bad("already exists", kn.add_component, "store", "Again")
		bad("title is required", kn.add_component, "web", "  ")
		bad("limit is 80", kn.add_component, "web", "t" * 81)
		bad("single line", kn.add_component, "web", "a\nb")
		bad("can't end with '#'", kn.add_component, "web", "C#")
		bad("limit is 1000", kn.add_component, "web", "Web", "s" * 1001)
		bad("outside the project", kn.add_component, "web", "Web", "", ["../secret/**"])
		bad("outside the project", kn.add_component, "web", "Web", "", ["/etc/passwd"])
		bad("comma", kn.add_component, "web", "Web", "", ["a,b"])
		bad("list of globs", kn.add_component, "web", "Web", "", "static/**")
		bad("no component 'nope'", kn.update_component, "nope", title="x")
		bad("no component 'nope'", kn.set_edge, "store", "nope", "x")
		bad("no component 'nope'", kn.set_edge, "nope", "store", "x")
		bad("link to itself", kn.set_edge, "store", "store", "x")
		bad("single line", kn.set_edge, "store", "api", "a\nb")
		bad("limit is 500", kn.set_edge, "store", "api", "x" * 501)
		bad("markdown link", kn.set_edge, "store", "api", "see [x](y.md)")
		bad("no connection to", kn.remove_edge, "api", "database")
		self.assertEqual(sorted(self.keys()), ["api", "database", "plain", "store"])  # nothing was written
		(self.arch / "multi.md").write_text("# Multi\n\n## Connections\n- [a](api.md) and [s](store.md)\n", encoding="utf-8")
		bad("edit that line by hand", kn.set_edge, "multi", "api", "x")
		self.assertEqual(self.s.activity(limit=1), [])  # failures log nothing

	def test_activity_event(self):
		kn.set_edge(self.s, "store", "api", "c", by="api-1")
		e = self.s.activity(limit=1)[0]
		self.assertEqual((e["event"], e["agent"], e["ticket"], e["note"]), ("arch", "api-1", None, "edge store -> api"))
		kn.remove_edge(self.s, "store", "api", by="api-1")
		self.assertEqual(self.s.activity(limit=1)[0]["note"], "remove edge store -> api")

	def test_concurrent_set_edge_keeps_both_lines(self):
		errs = []

		def go(to):
			try:
				kn.set_edge(Store(self.tdir), "plain", to, f"to {to}", by=to)
			except Exception as e:  # pragma: no cover
				errs.append(e)
		ts = [threading.Thread(target=go, args=(t,)) for t in ("api", "database", "store")]
		for t in ts:
			t.start()
		for t in ts:
			t.join()
		self.assertEqual(errs, [])
		self.assertEqual({k: v for k, v in self.edges().items() if k[0] == "plain"}, {("plain", "api"): "to api", ("plain", "database"): "to database", ("plain", "store"): "to store"})

	def test_rest(self):
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		self.addCleanup(srv.server_close)
		self.addCleanup(srv.shutdown)
		url = f"http://127.0.0.1:{srv.server_port}/api/b/app/architecture"

		def call(method, path, body=None):
			req = urllib.request.Request(url + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
			try:
				with urllib.request.urlopen(req) as r:
					return r.status, json.loads(r.read())
			except urllib.error.HTTPError as e:
				return e.code, json.loads(e.read())
		code, a = call("POST", "/components", {"key": "web", "title": "Web", "summary": "UI", "paths": ["static/**"]})
		self.assertEqual((code, "web" in [c["key"] for c in a["components"]]), (201, True))
		self.assertEqual(call("POST", "/components", {"key": "web", "title": "Web"})[0], 400)
		code, a = call("PATCH", "/components/web", {"title": "Web UI"})
		self.assertEqual((code, next(c["title"] for c in a["components"] if c["key"] == "web")), (200, "Web UI"))
		code, a = call("PUT", "/edges", {"from": "web", "to": "api", "text": "polls", "by": "ui"})
		self.assertEqual((code, {(e["from"], e["to"]): e["text"] for e in a["edges"]}[("web", "api")]), (200, "polls"))
		self.assertEqual(self.s.activity(limit=1)[0]["agent"], "ui")
		self.assertEqual(call("PUT", "/edges", {"from": "web", "to": "web", "text": "x"})[0], 400)
		self.assertEqual(call("PUT", "/edges", {"from": "web", "to": "api", "text": "bad\nline"})[0], 400)
		self.assertEqual(call("DELETE", "/edges?from=web&to=api&by=ui")[0], 200)
		self.assertEqual(call("DELETE", "/edges?from=web&to=api")[0], 400)  # no such edge any more
		self.s.create({"title": "open", "components": ["web"]}, prefix="T")
		code, e = call("DELETE", "/components/web")
		self.assertEqual(code, 400)
		self.assertIn("T-1", e["error"])
		self.s.update("T-1", {"status": "done"})
		code, a = call("DELETE", "/components/web?by=ui")
		self.assertEqual((code, "web" in [c["key"] for c in a["components"]]), (200, False))
		self.assertEqual(call("GET", "")[0], 200)
		self.assertEqual(call("PATCH", "/edges", {})[0], 405)


if __name__ == "__main__":
	unittest.main()
