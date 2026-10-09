"""Knowledge linked to tickets: board / profile / ticket docs, attached files, links, path safety."""
import json
import tempfile
import unittest
from pathlib import Path

from unblockd import knowledge as kn
from unblockd.store import Store, TicketError


class KnowledgeTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.repo = Path(self.tmp.name) / "game"
		(self.repo / "docs" / "notes").mkdir(parents=True)
		(self.repo / "AGENTS.md").write_text("Start here. See [handoff](docs/HANDOFF.md).\n@docs/MECHANICS.md\n", encoding="utf-8")
		(self.repo / "docs" / "HANDOFF.md").write_text("Next: BUG-10.", encoding="utf-8")
		(self.repo / "docs" / "MECHANICS.md").write_text("Combat numbers. [web](https://x.y)", encoding="utf-8")
		(self.repo / "docs" / "notes" / "a.md").write_text("note a", encoding="utf-8")
		(self.repo / "docs" / "notes" / "b.txt").write_text("note b", encoding="utf-8")
		(self.repo / "docs" / "notes" / "pic.png").write_bytes(b"\x89PNG")
		(Path(self.tmp.name) / "secret.md").write_text("outside", encoding="utf-8")
		t = self.repo / "tickets"
		t.mkdir()
		(t / "unblockd.json").write_text(json.dumps({
			"pillars": [{"key": "COMBAT"}], "docs": ["AGENTS.md"],
			"agents": [{"name": "godot-dev", "pillars": ["COMBAT"], "docs": ["docs/MECHANICS.md", "docs/HANDOFF.md"]}]}), encoding="utf-8")
		self.s = Store(t)
		self.s.create({"title": "parry", "pillar": "COMBAT", "docs": ["docs/notes/", "docs/HANDOFF.md", "nope.md"]}, tid="CMB-1")

	def tearDown(self):
		self.tmp.cleanup()

	def test_context_order_sources_dedupe_links_errors(self):
		ctx = kn.context(self.s, "CMB-1")
		self.assertEqual(ctx["protocol"], "unblockd://work-loop")
		self.assertNotIn("Never go silent", json.dumps(ctx))  # a pointer, not inlined
		got = [(d["path"], d["source"], d.get("error")) for d in ctx["docs"]]
		self.assertEqual(got, [
			("AGENTS.md", "board", None),
			("docs/MECHANICS.md", "agent:godot-dev", None),
			("docs/HANDOFF.md", "agent:godot-dev", None),  # listed again by the ticket: deduped
			("docs/notes/a.md", "ticket", None),
			("docs/notes/b.txt", "ticket", None),  # the .png is skipped
			("nope.md", "ticket", "missing"),
		])
		agents = ctx["docs"][0]
		self.assertEqual(agents["links"], ["docs/HANDOFF.md", "docs/MECHANICS.md"])  # md link + @import; web link ignored
		self.assertIn("Start here", agents["content"])

	def test_complete_stores_a_result_and_dependents_get_it_as_upstream(self):
		self.s.create({"title": "riposte", "pillar": "COMBAT", "blocked_by": ["CMB-1"]}, tid="CMB-2")
		self.s.create({"title": "unrelated", "pillar": "COMBAT", "blocked_by": ["CMB-1"]}, tid="CMB-3")
		self.assertEqual(kn.context(self.s, "CMB-2")["upstream"], [])  # CMB-1 has no result yet
		self.s.claim("godot-dev-1", tid="CMB-1")
		for bad in ({"files": "a.gd"}, {"refs": [""]}, {"files": ["x" * 301]}, {"handoff": "h" * 2001}):
			with self.assertRaises(TicketError):
				self.s.complete("CMB-1", "godot-dev-1", "did it", **bad)
		self.assertEqual(self.s.get("CMB-1")["status"], "doing")  # a refused complete changes nothing
		r = self.s.complete("CMB-1", "godot-dev-1", "did it", files=["scripts/parry.gd"], refs=["https://x/pull/1"], handoff="use Parry.window()")["result"]
		self.assertEqual({k: r[k] for k in ("summary", "files", "refs", "handoff", "by")},
		                 {"summary": "did it", "files": ["scripts/parry.gd"], "refs": ["https://x/pull/1"], "handoff": "use Parry.window()", "by": "godot-dev-1"})
		self.assertTrue(r["at"])
		up = kn.context(self.s, "CMB-2")["upstream"]
		self.assertEqual(up, [{"id": "CMB-1", "title": "parry", "summary": "did it", "files": ["scripts/parry.gd"], "refs": ["https://x/pull/1"], "handoff": "use Parry.window()"}])
		self.assertEqual(kn.context(self.s, "CMB-1")["upstream"], [])
		self.s.claim("godot-dev-1", tid="CMB-2")
		self.assertEqual(self.s.complete("CMB-2", "godot-dev-1", "ok")["result"]["files"], [])  # all optional
		with self.assertRaisesRegex(TicketError, "only by complete"):  # can't be forged
			self.s.update("CMB-3", {"result": {"summary": "by hand"}})
		with self.assertRaisesRegex(TicketError, "only by complete"):
			self.s.create({"title": "x", "result": {"summary": "forged"}}, tid="CMB-9")

	def test_done_when_leads_the_context_and_is_echoed_by_complete(self):
		self.assertEqual(kn.context(self.s, "CMB-1")["done_when"], [])
		self.s.update("CMB-1", {"done_when": ["parry window is 0.2s", "tests pass"]})
		ctx = kn.context(self.s, "CMB-1")
		self.assertEqual(list(ctx)[:3], ["ticket", "done_when", "docs"])
		self.assertEqual(ctx["done_when"], ["parry window is 0.2s", "tests pass"])
		self.assertIn("done_when", ctx["note"])
		self.s.claim("godot-dev-1", tid="CMB-1")
		body = self.s.complete("CMB-1", "godot-dev-1", "window 0.2s; tests pass")["body"]
		self.assertTrue(body.endswith("window 0.2s; tests pass\nDone when:\n- parry window is 0.2s\n- tests pass"))
		for bad in ("x", [""], ["a"] * 21, [1]):
			with self.assertRaises(TicketError):
				self.s.update("CMB-1", {"done_when": bad})

	def test_budget_lists_instead_of_inlining(self):
		ctx = kn.context(self.s, "CMB-1", budget=40)
		inlined = [d["path"] for d in ctx["docs"] if "content" in d]
		self.assertLessEqual(ctx["inlined_chars"], 40)
		self.assertNotIn("AGENTS.md", inlined)  # too big for the budget: listed, read with read_doc
		self.assertEqual(kn.read_doc(self.s, "AGENTS.md")["links"], ["docs/HANDOFF.md", "docs/MECHANICS.md"])

	def test_paths_cannot_leave_the_project(self):
		for bad in ("../secret.md", "/../secret.md", "docs/../../secret.md"):
			with self.assertRaisesRegex(TicketError, "outside the project"):
				kn.read_doc(self.s, bad)
		with self.assertRaises(TicketError):
			self.s.update("CMB-1", {"docs": ["../secret.md"]})  # refused at write time too
		self.assertIn("outside the project", kn.expand(self.s, "../*.md")[0][2])  # reported, not raised

	def test_attach_list_read_detach(self):
		kn.attach(self.s, "CMB-1", "spec.md", "# Parry\nwindow 0.2 s")
		self.assertEqual(self.s.get("CMB-1")["files"], ["spec.md"])
		ctx = kn.context(self.s, "CMB-1")
		self.assertEqual(ctx["docs"][-1]["source"], "attached")
		self.assertIn("window 0.2 s", ctx["docs"][-1]["content"])
		for name, content, err in (("../x.md", "x", "bad file name"), ("a.exe", "x", "only text"), ("big.md", "x" * 400_000, "limit")):
			with self.assertRaisesRegex(TicketError, err):
				kn.attach(self.s, "CMB-1", name, content)
		kn.detach(self.s, "CMB-1", "spec.md")
		self.assertEqual(self.s.get("CMB-1")["files"], [])
		self.assertEqual([e["event"] for e in self.s.activity("CMB-1")][:2], ["detach", "attach"])

	def test_graph(self):
		self.s.create({"title": "plain", "pillar": "COMBAT"}, tid="CMB-2")  # board + profile docs
		self.s.create({"title": "bare"}, tid="CMB-3")                        # no profile: board doc only
		self.s.create({"title": "att"}, tid="CMB-4")
		self.s.create({"title": "done one", "docs": ["nope2.md"]}, tid="CMB-5")
		self.s.update("CMB-5", {"status": "done"})
		kn.attach(self.s, "CMB-4", "spec.md", "see [m](../../../docs/MECHANICS.md)")
		(self.repo / "docs" / "extra.md").write_text("only reachable by link", encoding="utf-8")
		(self.repo / "docs" / "HANDOFF.md").write_text("Next. [x](extra.md) [n](notes/a.md)", encoding="utf-8")
		g = kn.graph(self.s)
		self.assertEqual([t["id"] for t in g["tickets"]], ["CMB-1", "CMB-2", "CMB-3", "CMB-4"])  # done tickets are skipped
		docs = {d["path"]: d for d in g["docs"]}
		self.assertEqual([docs[p]["kind"] for p in ("AGENTS.md", "docs/MECHANICS.md", "docs/notes/a.md", "tickets/files/CMB-4/spec.md")],
		                 ["board", "profile", "ticket", "file"])
		reads = {(r["ticket"], r["doc"]): r["via"] for r in g["reads"]}
		self.assertTrue(docs["unblockd://work-loop"]["builtin"])
		self.assertEqual(docs["unblockd://work-loop"]["readers"], 4)
		self.assertIn("Never go silent", kn.read_doc(self.s, "unblockd://work-loop")["content"])
		self.assertNotIn("unblockd://work-loop", [h["path"] for h in g["hubs"]])
		self.assertEqual(reads[("CMB-1", "docs/HANDOFF.md")], "profile")  # also on the ticket: first source wins, like context()
		self.assertEqual(reads[("CMB-3", "AGENTS.md")], "board")
		self.assertNotIn(("CMB-3", "docs/MECHANICS.md"), reads)
		self.assertNotIn("nope2.md", docs)
		self.assertIn({"from": "AGENTS.md", "to": "docs/HANDOFF.md"}, g["links"])  # doc -> doc
		self.assertIn({"from": "tickets/files/CMB-4/spec.md", "to": "docs/MECHANICS.md"}, g["links"])
		self.assertEqual(g["orphans"], ["docs/extra.md"])  # linked by a doc, read by no ticket
		self.assertTrue(docs["docs/extra.md"]["linked_only"])
		self.assertEqual(g["broken"], [{"path": "nope.md", "error": "missing", "via": "ticket", "tickets": ["CMB-1"]}])
		self.assertEqual(g["no_docs"], ["CMB-3"])  # CMB-1/2 have profile docs, CMB-4 an attachment
		self.assertEqual(g["hubs"][0], {"path": "docs/HANDOFF.md", "readers": 2})  # AGENTS.md (board) is not a hub

	def test_graph_flags_paths_outside_the_project(self):
		self.s.config["docs"].append("../secret.md")  # can't be written through the API, but a hand-edited config can
		g = kn.graph(self.s)
		self.assertEqual([(b["path"], b["via"], b["tickets"]) for b in g["broken"] if "outside" in b["error"]], [("../secret.md", "board", ["CMB-1"])])
		self.assertNotIn("outside", " ".join(d["path"] for d in g["docs"] if "error" not in d))


if __name__ == "__main__":
	unittest.main()
