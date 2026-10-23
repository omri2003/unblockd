"""Notes: a list on the ticket ({by, at, kind, text}), separate from the body; old bodies with appended notes are split on read."""
import json
import tempfile
import unittest
from pathlib import Path

from unblockd.store import Store, TicketError, split_notes

OLD = ("Fix the parry window.\n\nIt is 0.3s.\n\n— backend-1, 2026-10-01T10:00:00+00:00: found it\n\n"
       "— backend-1, 2026-10-01T10:05:00+00:00: question: 0.2 or 0.25?\n\n"
       "— Omri, 2026-10-01T11:00:00+00:00: Answer to: 0.2 or 0.25? — 0.2\nbecause feel\n\nreally\n\n"
       "2026-10-02 (backend-1, done): window 0.2s; tests pass\nDone when:\n- 0.2s\n\n"
       "— Omri, 2026-10-02T12:00:00+00:00: Approved by Omri")


class NotesTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.d = Path(self.tmp.name) / "tickets"
		self.s = Store(self.d)

	def tearDown(self):
		self.tmp.cleanup()

	def test_old_bodies_are_split_on_read_and_written_back_on_the_next_write(self):
		(self.d / "A-1.json").write_text(json.dumps({"id": "A-1", "title": "parry", "status": "todo", "body": OLD}), encoding="utf-8")
		t = self.s.get("A-1")
		self.assertEqual(t["body"], "Fix the parry window.\n\nIt is 0.3s.")
		self.assertEqual([(n["by"], n["kind"]) for n in t["notes"]],
		                 [("backend-1", "note"), ("backend-1", "question"), ("Omri", "answer"), ("backend-1", "complete"), ("Omri", "approved")])
		self.assertEqual((t["notes"][1]["text"], t["notes"][2]["q"], t["notes"][2]["text"]), ("0.2 or 0.25?", "0.2 or 0.25?", "0.2\nbecause feel\n\nreally"))
		self.assertEqual((t["notes"][3]["text"], t["notes"][3]["status"]), ("window 0.2s; tests pass", "done"))
		self.assertNotIn("notes", json.loads((self.d / "A-1.json").read_text(encoding="utf-8")))  # reading never writes
		self.s.add_note("A-1", "omri", "new")
		disk = json.loads((self.d / "A-1.json").read_text(encoding="utf-8"))
		self.assertEqual((disk["body"], len(disk["notes"]), disk["notes"][-1]["text"]), ("Fix the parry window.\n\nIt is 0.3s.", 6, "new"))

	def test_a_body_with_no_notes_is_unchanged(self):
		for b in ("", "plain text\n\nwith — a dash, 2026: not a note", "Split from A-1."):
			self.assertEqual(split_notes(b), (b, []))

	def test_notes_are_append_only_and_the_body_is_only_the_description(self):
		self.s.create({"title": "x", "body": "the why"}, tid="A-1")
		self.s.add_note("A-1", "omri", "first")
		with self.assertRaisesRegex(TicketError, "add_note"):
			self.s.update("A-1", {"notes": []})
		with self.assertRaisesRegex(TicketError, "add_note"):
			self.s.create({"title": "y", "notes": [{"text": "x"}]}, tid="A-2")
		t = self.s.update("A-1", {"body": "a better why"})  # the edit form saves the body: notes untouched
		self.assertEqual((t["body"], [n["text"] for n in t["notes"]]), ("a better why", ["first"]))


if __name__ == "__main__":
	unittest.main()
