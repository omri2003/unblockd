"""CORE-74: a write that timed out may have landed. A retry with the same request_id changes nothing and answers the first
write's ticket with already: true; the same agent repeating its own complete / release / question gets already: true, not an error."""
import json
import tempfile
import unittest
from pathlib import Path

from unblockd.store import Store, TicketError


class IdempotentTest(unittest.TestCase):
	backend = None  # files; the SQLite subclass below runs the same cases on a database board

	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows: SQLite keeps the db file open
		self.addCleanup(self.tmp.cleanup)
		self.s = self.open()
		self.s.create({"title": "parry"}, tid="CMB-1")

	def open(self):
		d = Path(self.tmp.name) / "tickets"
		return Store(d)

	def events(self, event):
		return [e for e in self.s.activity(limit=1000) if e["event"] == event]

	def test_complete_with_the_same_request_id_twice(self):
		self.s.claim("w-1", tid="CMB-1")
		first = self.s.complete("CMB-1", "w-1", "fixed", request_id="r-1")
		again = self.s.complete("CMB-1", "w-1", "fixed", request_id="r-1")
		self.assertNotIn("already", first)
		self.assertTrue(again["already"])
		self.assertEqual((again["id"], again["status"], again["request_id"]), ("CMB-1", "done", "r-1"))
		self.assertEqual(len(self.events("complete")), 1)
		self.assertEqual([n["kind"] for n in self.s.get("CMB-1")["notes"]].count("complete"), 1)

	def test_repeating_your_own_complete_or_release_without_an_id(self):
		self.s.claim("w-1", tid="CMB-1")
		self.s.release("CMB-1", "w-1", "blocked by the build")
		r = self.s.release("CMB-1", "w-1", "blocked by the build")
		self.assertTrue(r["already"])
		self.assertEqual(len(self.events("release")), 1)
		self.s.claim("w-1", tid="CMB-1")
		self.s.complete("CMB-1", "w-1", "done it")
		self.assertTrue(self.s.complete("CMB-1", "w-1", "done it")["already"])
		self.assertEqual(len(self.events("complete")), 1)
		with self.assertRaisesRegex(TicketError, "isn't claimed"):  # someone else's repeat is still refused
			self.s.complete("CMB-1", "w-2", "me too")

	def test_a_release_after_a_new_claim_is_not_a_repeat(self):
		self.s.claim("w-1", tid="CMB-1")
		self.s.release("CMB-1", "w-1")
		self.s.claim("w-2", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "claimed by w-2"):
			self.s.release("CMB-1", "w-1")

	def test_ask_twice_and_note_with_an_id(self):
		self.s.claim("w-1", tid="CMB-1")
		self.s.ask("CMB-1", "w-1", "which engine?")
		r = self.s.ask("CMB-1", "w-1", "which engine?")
		self.assertTrue(r["already"])
		self.assertEqual(len(self.events("question")), 1)
		self.s.add_note("CMB-1", "w-1", "tried A", request_id="n-1")
		self.assertTrue(self.s.add_note("CMB-1", "w-1", "tried A", request_id="n-1")["already"])
		self.s.add_note("CMB-1", "w-1", "tried A")  # no id: a note is a note
		self.assertEqual([n["text"] for n in self.s.get("CMB-1")["notes"]].count("tried A"), 2)

	def test_create_with_the_same_request_id(self):
		a = self.s.create({"title": "new"}, prefix="CMB", agent="w-1", request_id="c-1")
		b = self.s.create({"title": "new"}, prefix="CMB", agent="w-1", request_id="c-1")
		self.assertEqual((b["id"], b["already"]), (a["id"], True))
		self.assertEqual(len([t for t in self.s.list() if t["title"] == "new"]), 1)
		c = self.s.create({"title": "new"}, prefix="CMB", agent="w-2", request_id="c-1")  # ids are per agent
		self.assertNotEqual(c["id"], a["id"])

	def test_one_id_names_one_write(self):
		self.s.claim("w-1", tid="CMB-1")
		self.s.add_note("CMB-1", "w-1", "x", request_id="r-9")
		with self.assertRaisesRegex(TicketError, "already used for note on CMB-1"):
			self.s.complete("CMB-1", "w-1", "done", request_id="r-9")
		with self.assertRaisesRegex(TicketError, "request_id must be"):
			self.s.add_note("CMB-1", "w-1", "x", request_id="has spaces")
		self.assertEqual(self.s.get("CMB-1")["status"], "doing")

	def test_mcp_and_cli_pass_it(self):
		try:
			from unblockd import mcp_server as m
		except ImportError:
			self.skipTest("mcp not installed")
		import inspect
		for name in ("complete", "release", "add_note", "ask_human", "create_ticket"):
			self.assertIn("request_id", inspect.signature(getattr(m, name)).parameters, name)
		self.assertIn("get_ticket before retrying", m.complete.__doc__)
		self.s.claim("w-1", tid="CMB-1")
		self.s.complete("CMB-1", "w-1", "x")
		self.assertTrue(m._ack(self.s.complete("CMB-1", "w-1", "x"))["already"])


class IdempotentSqliteTest(IdempotentTest):
	def open(self):
		from unblockd import sql
		self.addCleanup(sql.disconnect)
		return Store(f"sqlite:///{Path(self.tmp.name) / 'b.db'}#game")


if __name__ == "__main__":
	unittest.main()
