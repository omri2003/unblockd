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

	def test_human_notes_answers_and_decisions_are_pinned_agent_chatter_is_not(self):
		cfg = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}], "agents": [{"name": "backend"}]}
		(self.d / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
		s = Store(self.d)
		s.create({"title": "x"}, tid="A-1")
		s.claim("backend-1", tid="A-1")
		s.add_note("A-1", "backend-1", "looking at store.py")
		s.add_note("A-1", "backend-1", "chose a bitset because the DFS was O(n^2)", kind="decision")
		s.add_note("A-1", "backend-1", "tests fail on Windows: CRLF in fixtures", kind="finding")
		s.add_note("A-1", "Omri", "keep the old API working")
		s.add_note("A-1", "human", "from the web board")
		s.ask("A-1", "backend-1", "which db?")
		s.answer("A-1", "Omri", "postgres")
		pins = [(n["by"], n["kind"]) for n in s.get("A-1")["notes"] if n.get("pinned")]
		self.assertEqual(pins, [("backend-1", "decision"), ("backend-1", "finding"), ("Omri", "note"), ("human", "note"), ("Omri", "answer")])

	def test_a_claim_sends_every_pinned_note_and_the_last_five_others(self):
		s = self.s
		s.create({"title": "x"}, tid="A-1")
		s.add_note("A-1", "human", "DECISION-EARLY: never touch the payments table")  # a person: pinned, and old
		s.claim("w", tid="A-1")
		for i in range(40):  # the worker holding the claim: not pinned
			s.add_note("A-1", "w", f"progress {i} " + "x" * 120)
		s.release("A-1", "w")
		t = s.claim("w2", tid="A-1")
		texts = [n["text"] for n in t["notes"]]
		self.assertIn("DECISION-EARLY: never touch the payments table", texts)
		self.assertEqual(sum(x.startswith("progress") for x in texts), 5)
		self.assertTrue(texts[-1].startswith("progress 39"))
		self.assertIn("35 older notes", t["notes_omitted"])  # 41 - 1 pinned - 5 latest
		self.assertEqual(len(s.get("A-1")["notes"]), 41)  # get_ticket still has everything

	def test_a_short_history_is_sent_whole(self):
		self.s.create({"title": "x"}, tid="A-1")
		self.s.claim("w", tid="A-1")
		for i in range(8):
			self.s.add_note("A-1", "w", f"n{i}")
		self.s.release("A-1", "w")
		t = self.s.claim("w", tid="A-1")
		self.assertEqual((len(t["notes"]), "notes_omitted" in t), (8, False))

	def test_old_notes_keep_human_decisions_pinned(self):
		(self.d / "A-1.json").write_text(json.dumps({"id": "A-1", "title": "parry", "status": "todo", "body": OLD}), encoding="utf-8")
		self.assertEqual([n["kind"] for n in self.s.get("A-1")["notes"] if n.get("pinned")], ["answer", "approved"])

	def test_supersedes_unpins_and_replies_travel_with_their_target(self):
		s = self.s
		s.create({"title": "x"}, tid="A-1")
		s.claim("w", tid="A-1")
		s.add_note("A-1", "w", "use sqlite for the cache", kind="decision")  # n1
		s.add_note("A-1", "w", "should the cache survive a restart? " + "x" * 50)  # n2: plain, would fall out of the tail
		for i in range(30):
			s.add_note("A-1", "w", f"progress {i} " + "y" * 120)
		s.add_note("A-1", "w", "in-memory cache instead: sqlite was a second copy", kind="decision", supersedes=1)
		s.add_note("A-1", "w", "no: it rebuilds from the files", reply_to=2)
		with self.assertRaisesRegex(TicketError, "note number"):
			s.add_note("A-1", "w", "x", reply_to=99)
		with self.assertRaisesRegex(TicketError, "kind"):
			s.add_note("A-1", "w", "x", kind="urgent")
		s.release("A-1", "w")
		sent = {n["n"]: n for n in s.claim("w", tid="A-1")["notes"]}
		self.assertIn(33, sent)  # the new decision
		self.assertIn(1, sent)  # the old one rides along as what 33 replaced, marked superseded
		self.assertEqual(sent[1]["superseded_by"], 33)
		self.assertIn(2, sent)  # the question that 34 answers
		self.assertNotIn(3, sent)  # old progress is left out


if __name__ == "__main__":
	unittest.main()
