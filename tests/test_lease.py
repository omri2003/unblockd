"""AI-35: lease safety for long runs. Every reply to the holder says how much lease is left, a lapsed claim is said out loud,
and the same agent takes its claim back on heartbeat / complete when nobody claimed the ticket since."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from unblockd.store import Store, TicketError, LEASE_WARN_S

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}], "default_owner": "Claude", "pillars": [{"key": "COMBAT"}],
       "agents": [{"name": "backend", "pillars": ["COMBAT"]}]}


class LeaseTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(self.dir)
		self.s.create({"title": "parry", "pillar": "COMBAT", "priority": "P1"}, tid="CMB-1")

	def tearDown(self):
		self.tmp.cleanup()

	def quiet(self, minutes: float, tid: str = "CMB-1") -> None:
		"""Pretend the holder has been silent for `minutes`."""
		p = self.dir / ".unblockd" / "claims.json"
		c = json.loads(p.read_text())
		c[tid]["heartbeat_at"] = datetime.fromtimestamp(time.time() - minutes * 60, timezone.utc).isoformat(timespec="seconds")
		p.write_text(json.dumps(c))

	def test_claim_and_writes_carry_lease_left(self):
		lease = self.s.config["lease_minutes"] * 60
		self.assertEqual(self.s.claim("backend-1", tid="CMB-1")["lease_left_s"], lease)
		self.quiet(5)
		self.assertGreaterEqual(self.s.heartbeat("CMB-1", "backend-1")["lease_left_s"], lease - 2)  # renewed
		self.assertNotIn("warnings", self.s.add_note("CMB-1", "backend-1", "running the tests"))
		self.assertIn("lease_left_s", self.s.update("CMB-1", {"priority": "P2"}, agent="backend-1"))
		self.assertNotIn("lease_left_s", self.s.update("CMB-1", {"priority": "P1"}))  # a human gets nothing extra

	def test_low_lease_warns(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.quiet(self.s.config["lease_minutes"] - 1)
		info = self.s.lease_status("CMB-1", "backend-1")
		self.assertLess(info["lease_left_s"], LEASE_WARN_S)
		self.assertIn("heartbeat now", info["warnings"][0])

	def test_note_after_a_lapse_says_you_no_longer_hold_it(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.quiet(20)
		t = self.s.add_note("CMB-1", "backend-1", "still testing")
		self.assertEqual(t["status"], "todo")  # a note is kept but does not re-take the claim
		self.assertIn("you no longer hold CMB-1", t["warnings"][0])
		self.assertNotIn("lease_left_s", t)

	def test_quiet_for_20_min_then_complete_is_done(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.quiet(20)
		self.assertEqual(self.s.get("CMB-1")["status"], "todo")  # swept
		t = self.s.complete("CMB-1", "backend-1", "parry works; tests pass")
		self.assertEqual(t["status"], "done")
		self.assertIn("yours again", t["warnings"][0])
		self.assertEqual(t["result"]["by"], "backend-1")
		events = [e["event"] for e in self.s.activity("CMB-1")]
		self.assertEqual(events[:3], ["complete", "claim", "expired"])

	def test_heartbeat_after_a_lapse_takes_it_back(self):
		self.s.claim("backend-1", tid="CMB-1", principal="Omri")
		self.quiet(20)
		t = self.s.heartbeat("CMB-1", "backend-1", "tests at 80%")
		self.assertEqual((t["status"], t["claim"]["agent"], t["claim"]["principal"]), ("doing", "backend-1", "Omri"))
		self.assertIn("yours again", t["warnings"][0])
		self.assertIn("lease_left_s", t)

	def test_no_retake_once_someone_else_claimed(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.quiet(20)
		self.s.claim("backend-2", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "claimed by backend-2"):
			self.s.complete("CMB-1", "backend-1", "done")
		self.s.release("CMB-1", "backend-2", "not mine")
		with self.assertRaisesRegex(TicketError, "isn't claimed"):  # backend-2's claim ended it, not a lapse of backend-1
			self.s.heartbeat("CMB-1", "backend-1")

	def test_no_retake_when_the_ticket_is_no_longer_claimable(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.quiet(20)
		self.s.update("CMB-1", {"waiting_on": "which parry window?"})  # a human parked it meanwhile
		with self.assertRaisesRegex(TicketError, "you no longer hold CMB-1.*waiting on a human"):
			self.s.complete("CMB-1", "backend-1", "done")
		self.assertEqual(self.s.get("CMB-1")["status"], "todo")

	def test_someone_elses_ticket_warns(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.assertIn("backend-1 does", self.s.lease_status("CMB-1", "backend-2")["warnings"][0])

	def test_mcp_short_reply_keeps_lease_and_warnings(self):
		try:
			from unblockd.mcp_server import _ack
		except ImportError:
			self.skipTest("mcp not installed")
		self.s.claim("backend-1", tid="CMB-1")
		self.assertIn("lease_left_s", _ack(self.s.heartbeat("CMB-1", "backend-1")))
		self.quiet(20)
		self.assertIn("you no longer hold", _ack(self.s.add_note("CMB-1", "backend-1", "x"))["warnings"][0])


if __name__ == "__main__":
	unittest.main()
