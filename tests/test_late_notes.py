"""AI-50: a note that arrives after the worker's last heartbeat is never lost: complete / release / ask_human return it, a review
row shows it next to the result, and a person's note on a done ticket is listed in Needs you until it is reopened or dismissed."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import Store, TicketError

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}], "default_owner": "Claude",
       "agents": [{"name": "backend", "tags": ["be"]}]}


class LateNotesTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		self.cfg(CFG)
		for i in (1, 2, 3):
			self.s.create({"title": f"t{i}", "tags": ["be"]}, tid=f"B-{i}")

	def cfg(self, c):
		(self.dir / "unblockd.json").write_text(json.dumps(c), encoding="utf-8")
		self.s = Store(self.dir)

	def tearDown(self):
		self.tmp.cleanup()

	def needs(self):
		from unblockd import web
		old, web.BOARDS = web.BOARDS, Boards(dirs=[str(self.dir)])
		try:
			return {i["id"]: i for g in web._needs() for i in g["items"]}
		finally:
			web.BOARDS = old

	def test_complete_release_and_ask_return_notes_the_worker_never_read(self):
		s = self.s
		s.claim("backend-1", tid="B-1")
		s.add_note("B-1", "backend-1", "my own progress")  # own notes are never news
		s.add_note("B-1", "Omri", "Don't use the old font loader." + chr(10) + "Use FontCache.get().")
		t = s.complete("B-1", "backend-1", "done")  # no heartbeat since the note: it would have been lost
		box = t["inbox"]
		self.assertTrue(box["unread_before_complete"])
		self.assertEqual([(n["n"], n["by"], n["text"].splitlines()[1]) for n in box["notes"]], [(2, "Omri", "Use FontCache.get().")])
		self.assertEqual(t["result"]["unread"], [2])
		s.claim("backend-1", tid="B-2")
		self.assertNotIn("inbox", s.complete("B-2", "backend-1", "done"))  # nothing unread: no key, and no result.unread
		self.assertNotIn("unread", s.get("B-2")["result"])
		s.claim("backend-1", tid="B-3")
		s.add_note("B-3", "Omri", "wait")
		self.assertTrue(s.release("B-3", "backend-1", "stuck")["inbox"]["unread_before_release"])
		s.claim("backend-1", tid="B-3")
		s.inbox("backend-1", mark=True)
		self.assertNotIn("inbox", s.release("B-3", "backend-1"))  # shown by a heartbeat already
		s.claim("backend-1", tid="B-3")
		s.add_note("B-3", "Omri", "and this")
		self.assertTrue(s.ask("B-3", "backend-1", "which?")["inbox"]["unread_before_ask"])

	def test_mcp_and_cli_replies_carry_them(self):
		s = self.s
		s.claim("backend-1", tid="B-1")
		s.add_note("B-1", "Omri", "for the CLI")
		out = io.StringIO()
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(out):
			self.assertEqual(cli.main(["--dir", str(self.dir), "complete", "B-1", "--agent", "backend-1", "--summary", "done"]), 0)
		self.assertEqual(json.loads(out.getvalue())["inbox"]["notes"][0]["text"], "for the CLI")
		try:
			from unblockd import mcp_server
		except ImportError:
			self.skipTest("mcp not installed")
		old, mcp_server.BOARDS = mcp_server.BOARDS, Boards(dirs=[str(self.dir)])
		try:
			mcp_server.claim_ticket("B-2", "backend-1")
			s.add_note("B-2", "Omri", "for the MCP")
			r = mcp_server.complete("B-2", "backend-1", "done")
		finally:
			mcp_server.BOARDS = old
		self.assertEqual((r["inbox"]["notes"][0]["text"], r["inbox"]["unread_before_complete"]), ("for the MCP", True))

	def test_review_row_shows_notes_after_complete(self):
		self.cfg({**CFG, "review": True})
		s = self.s
		s.claim("backend-1", tid="B-1")
		s.add_note("B-1", "Omri", "unread when it finished")
		s.complete("B-1", "backend-1", "done")
		s.add_note("B-1", "backend-1", "the completer's own note")  # not news to the reviewer
		s.add_note("B-1", "Dana", "after it finished")
		row = self.needs()["B-1"]
		self.assertEqual(row["kind"], "review")
		self.assertEqual([(n["text"], n.get("unread", False)) for n in row["late"]],
		                 [("unread when it finished", True), ("after it finished", False)])
		s.claim("backend-1", tid="B-2")
		s.complete("B-2", "backend-1", "done")
		self.assertNotIn("late", self.needs()["B-2"])

	def test_a_note_in_the_same_second_as_done_counts_only_after_the_finishing_note(self):
		s = self.s
		s.add_note("B-1", "Omri", "before it finished")
		s.claim("backend-1", tid="B-1")
		s.complete("B-1", "backend-1", "done")
		s.add_note("B-1", "Omri", "right after it finished")
		p = self.dir / "B-1.json"  # done_at has whole seconds: put every note in that same second
		t = json.loads(p.read_text(encoding="utf-8"))
		for n in t["notes"]:
			n["at"] = t["done_at"]
		p.write_text(json.dumps(t), encoding="utf-8")
		self.assertEqual([n["text"] for n in self.needs()["B-1"]["late"]], ["right after it finished"])

	def test_a_persons_note_on_a_done_ticket_until_reopen_or_dismiss(self):
		s = self.s
		for tid in ("B-1", "B-2"):
			s.claim("backend-1", tid=tid)
			s.complete(tid, "backend-1", "done")
		for tid in ("B-1", "B-2"):  # back-date done_at: a note in the same second must still count as after it
			p = self.dir / f"{tid}.json"
			t = json.loads(p.read_text(encoding="utf-8"))
			t["done_at"] = "2020-01-01T00:00:00+00:00"
			p.write_text(json.dumps(t), encoding="utf-8")
		s.add_note("B-1", "backend-2", "an agent's note stays a plain note")
		s.add_note("B-1", "github", "PR #3 merged")
		self.assertNotIn("B-1", self.needs())
		s.add_note("B-1", "Omri", "this broke the login page")
		row = self.needs()["B-1"]
		self.assertEqual((row["kind"], [n["n"] for n in row["late"]]), ("late_note", [4]))
		with self.assertRaisesRegex(TicketError, "human action"):
			s.dismiss_notes("B-1", "backend-1", [4])
		with self.assertRaisesRegex(TicketError, "notes 1-4"):
			s.dismiss_notes("B-1", "Omri", [9])
		s.dismiss_notes("B-1", "Omri", [4])
		self.assertNotIn("B-1", self.needs())
		self.assertEqual(s.get("B-1")["notes"][3]["dismissed_by"], "Omri")
		s.dismiss_notes("B-1", "Omri", [4], undo=True)  # the Undo
		self.assertIn("B-1", self.needs())
		self.assertTrue(s.get("B-1")["notes"][3]["pinned"])  # a person's note: pinned for the next worker
		s.update("B-1", {"status": "todo"})  # Reopen
		self.assertNotIn("B-1", self.needs())
		self.assertEqual(s.claim("backend-1", tid="B-1")["notes"][-1]["text"], "this broke the login page")
		s.add_note("B-2", "Omri", "nice")  # CLI, then REST through cli.Remote
		out = io.StringIO()
		with mock.patch.dict(os.environ, {"UNBLOCKD_URL": ""}), contextlib.redirect_stdout(out):
			self.assertEqual(cli.main(["--dir", str(self.dir), "dismiss", "B-2", "2", "--by", "Omri"]), 0)
		self.assertNotIn("B-2", self.needs())
		import threading
		from http.server import ThreadingHTTPServer
		from unblockd import web
		old, web.BOARDS = web.BOARDS, Boards(dirs=[str(self.dir)])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		try:
			r = cli.Remote(f"http://127.0.0.1:{srv.server_port}")
			self.assertNotIn("dismissed_by", r.dismiss_notes("B-2", "Omri", [2], undo=True)["notes"][1])
			self.assertIn("B-2", self.needs())
			self.assertEqual(r.dismiss_notes("B-2", "Omri", [2])["notes"][1]["dismissed_by"], "Omri")
			with self.assertRaisesRegex(TicketError, "list of note numbers"):
				r.dismiss_notes("B-2", "Omri", [])
		finally:
			srv.shutdown()
			srv.server_close()
			web.BOARDS = old


if __name__ == "__main__":
	unittest.main()
