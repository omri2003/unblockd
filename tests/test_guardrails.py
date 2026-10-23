"""Worker guardrails: accident prevention for profile-named actors; a solo owner's own Claude (any other name) is unrestricted."""
import json
import tempfile
import unittest
from pathlib import Path

from unblockd.store import Store, TicketError

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "agents": [{"name": "backend", "tags": ["be"]}], "review": True, "agent_tickets": "propose"}


def make(cfg):
	tmp = tempfile.TemporaryDirectory()
	d = Path(tmp.name) / "tickets"
	d.mkdir()
	(d / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
	return tmp, Store(d)


class GuardrailTest(unittest.TestCase):
	def setUp(self):
		self.tmp, self.s = make(CFG)
		self.addCleanup(self.tmp.cleanup)

	def test_worker_cannot_answer_reject_accept(self):
		s = self.s
		s.create({"title": "a"}, tid="T-1")
		s.claim("backend-1", tid="T-1")
		s.ask("T-1", "backend-1", "which?")
		s.create({"title": "p"}, tid="T-2", agent="backend-2")
		for call in (lambda: s.answer("T-1", "backend-2", "this"), lambda: s.reject("T-1", "backend-2", "no"),
		             lambda: s.accept("T-2", "backend-3")):
			with self.assertRaisesRegex(TicketError, "human action.*ask_human"):
				call()
		self.assertTrue(s.get("T-1")["waiting_on"])
		self.assertTrue(s.get("T-2")["proposed_by"])

	def test_self_approval_refused_for_everyone(self):
		s = self.s
		s.create({"title": "q"}, tid="T-1")
		s.claim("claude", tid="T-1", profile="backend")  # a non-profile name, explicit profile
		s.ask("T-1", "claude", "which?")
		with self.assertRaisesRegex(TicketError, "can't answer"):
			s.answer("T-1", "claude", "my own answer")
		s.answer("T-1", "omri", "ok")  # someone else can
		s.create({"title": "p"}, tid="T-2", agent="claude")
		with self.assertRaisesRegex(TicketError, "can't accept"):
			s.accept("T-2", "claude")
		s.claim("claude", tid="T-1", profile="backend")
		s.complete("T-1", "claude", "done")
		with self.assertRaisesRegex(TicketError, "can't reject"):
			s.reject("T-1", "claude", "meh")

	def test_approve_rules(self):
		s = self.s
		s.create({"title": "a"}, tid="T-1")
		with self.assertRaisesRegex(TicketError, "not review"):
			s.approve("T-1", "omri")
		s.claim("claude", tid="T-1", profile="backend")
		s.complete("T-1", "claude", "done")  # review: true
		for who, msg in (("backend-2", "human action"), ("claude", "can't approve"), (" ", "needs `by`")):
			with self.assertRaisesRegex(TicketError, msg):
				s.approve("T-1", who)
		self.assertEqual(s.get("T-1")["status"], "review")
		t = s.approve("T-1", "omri")
		self.assertEqual((t["status"], t["result"]["approved_by"], t["result"]["by"]), ("done", "omri", "claude"))
		self.assertTrue(t["result"]["approved_at"] and t["done_at"])
		self.assertEqual((t["notes"][-1]["kind"], t["notes"][-1]["text"]), ("approved", "Approved by omri"))
		self.assertEqual(s.activity("T-1")[0]["event"], "approved")
		t = s.update("T-1", {"status": "review"})  # Undo: nobody approves it any more
		self.assertNotIn("approved_by", t["result"])
		self.assertNotIn("approved_at", t["result"])
		self.assertEqual(t["result"]["by"], "claude")

	def test_plain_patch_to_done_records_no_approver(self):
		s = self.s
		s.create({"title": "a"}, tid="T-1")
		s.claim("claude", tid="T-1", profile="backend")
		s.complete("T-1", "claude", "done")
		t = s.update("T-1", {"status": "done"})
		self.assertEqual(t["status"], "done")
		self.assertNotIn("approved_by", t["result"])

	def test_worker_status_and_delete(self):
		s = self.s
		s.create({"title": "a"}, tid="T-1")
		s.create({"title": "mine", "owner": "Omri"}, tid="T-2")
		for st in ("doing", "done", "review"):
			with self.assertRaisesRegex(TicketError, "claim_ticket / complete / ask_human"):
				s.update("T-1", {"status": st}, agent="backend-1")
		self.assertEqual(s.update("T-1", {"priority": "P1"}, agent="backend-1")["priority"], "P1")  # other fields fine
		s.claim("backend-1", tid="T-1")
		self.assertEqual(s.update("T-1", {"status": "todo"}, agent="backend-1")["status"], "todo")  # back to todo fine
		with self.assertRaisesRegex(TicketError, "human"):
			s.delete("T-2", agent="backend-1")
		self.assertTrue(s.delete("T-1", agent="backend-1"))  # an unowned ticket: fine

	def test_solo_flow_unchanged(self):
		s = self.s
		s.create({"title": "a", "owner": "Omri"}, tid="T-1")
		s.create({"title": "b"}, tid="T-2")
		s.create({"title": "p"}, tid="T-3", agent="backend-1")
		s.claim("backend-1", tid="T-2")
		s.ask("T-2", "backend-1", "which?")
		s.answer("T-2", "claude", "this one")
		s.accept("T-3", "claude")
		s.claim("backend-1", tid="T-2")
		s.complete("T-2", "backend-1", "done")  # review gate
		s.reject("T-2", "omri", "redo")
		for st in ("doing", "review", "done"):
			self.assertEqual(s.update("T-2", {"status": st}, agent="claude")["status"], st)
		s.delete("T-1", agent="claude")

	def test_board_without_profiles_is_unrestricted(self):
		tmp, s = make({"owners": CFG["owners"], "review": True})
		self.addCleanup(tmp.cleanup)
		s.create({"title": "a", "owner": "Claude"}, tid="T-1")  # default owner is the human, who can't be claimed from
		s.claim("backend-1", tid="T-1")
		s.ask("T-1", "backend-1", "which?")
		s.answer("T-1", "backend-2", "x")
		self.assertEqual(s.update("T-1", {"status": "done"}, agent="backend-2")["status"], "done")
		s.delete("T-1", agent="backend-2")


if __name__ == "__main__":
	unittest.main()
