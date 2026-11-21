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

class OrphanTest(unittest.TestCase):
	"""CORE-79: a ticket left doing with nobody holding it (claim expired or lost, health board FB-1) could not be claimed by anyone."""
	setUp, tearDown = LeaseTest.setUp, LeaseTest.tearDown

	def orphan(self, tid: str = "CMB-1") -> None:
		self.s.claim("backend-1", tid=tid)
		(self.dir / ".unblockd" / "claims.json").write_text("{}")  # the claim is gone, the file still says doing

	def test_explicit_claim_takes_over_with_a_warning(self):
		self.orphan()
		self.assertEqual(self.s.get("CMB-1")["status"], "doing")
		got = self.s.claim("backend-2", tid="CMB-1")
		self.assertEqual((got["status"], got["claim"]["agent"]), ("doing", "backend-2"))
		self.assertTrue(any("nobody holding it" in w and "previous_attempts" in w for w in got["warnings"]))
		self.assertIn("previous_attempts", got)
		self.s.complete("CMB-1", "backend-2", "done")  # and the new holder can finish it
		self.assertEqual(self.s.get("CMB-1")["status"], "done")

	def test_claim_next_still_skips_orphans(self):
		self.orphan()
		r = self.s.claim_next("backend-2")
		self.assertIsNone(r["ticket"])
		self.assertEqual(r["orphaned"], ["CMB-1"])
		self.assertEqual(self.s.get("CMB-1")["claim"], None)

	def test_live_claim_and_hand_set_doing_with_blockers_still_refused(self):
		self.s.claim("backend-1", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "already claimed by backend-1"):
			self.s.claim("backend-2", tid="CMB-1")
		self.s.create({"title": "b", "pillar": "COMBAT"}, tid="CMB-2")
		self.s.create({"title": "q", "pillar": "COMBAT"}, tid="CMB-3")
		self.s.update("CMB-3", {"status": "doing"})
		self.s.update("CMB-3", {"waiting_on": "which one?"})
		with self.assertRaisesRegex(TicketError, "waiting on a human"):
			self.s.claim("backend-2", tid="CMB-3")

	def test_a_normal_claim_has_no_orphan_warning(self):
		self.assertNotIn("warnings", self.s.claim("backend-1", tid="CMB-1"))


class LeasePerProfileTest(unittest.TestCase):
	"""AI-58: agents[].lease_minutes per profile, and heartbeat(until=) for a run longer than the lease, capped by lease_max_minutes."""
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		cfg = {**CFG, "agents": [{"name": "backend", "pillars": ["COMBAT"]}, {"name": "artist", "pillars": ["ART"], "lease_minutes": 30}],
		       "pillars": [{"key": "COMBAT"}, {"key": "ART"}], "lease_max_minutes": 60}
		(self.dir / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
		self.s = Store(self.dir)
		self.s.create({"title": "parry", "pillar": "COMBAT"}, tid="CMB-1")
		self.s.create({"title": "sprite", "pillar": "ART"}, tid="ART-1")

	def tearDown(self):
		self.tmp.cleanup()

	quiet = LeaseTest.quiet

	def at(self, minutes: float) -> str:
		return datetime.fromtimestamp(time.time() + minutes * 60, timezone.utc).isoformat(timespec="seconds")

	def test_profile_lease_used_by_claim_and_expiry(self):
		got = self.s.claim("artist-1", tid="ART-1")
		self.assertEqual((got["lease_minutes"], got["lease_left_s"]), (30, 1800))
		self.assertGreater(datetime.fromisoformat(got["expires_at"]).timestamp(), time.time() + 29 * 60)
		self.assertEqual(self.s.claim("backend-1", tid="CMB-1")["lease_minutes"], 15)  # the board's default
		self.quiet(20, "ART-1")
		self.quiet(20, "CMB-1")
		self.assertEqual(self.s.get("ART-1")["status"], "doing")  # 20 min < the artist's 30
		self.assertEqual(self.s.get("CMB-1")["status"], "todo")  # 20 min > the board's 15
		self.quiet(31, "ART-1")
		t = self.s.get("ART-1")
		self.assertEqual(t["status"], "todo")
		self.assertIn("no heartbeat for 30 min", t["notes"][-1]["text"])

	def test_heartbeat_until_extends_the_lease_and_replies_say_so(self):
		self.s.claim("backend-1", tid="CMB-1")
		r = self.s.heartbeat("CMB-1", "backend-1", "rendering", until=self.at(40))
		self.assertGreater(r["lease_left_s"], 39 * 60)
		self.assertAlmostEqual(datetime.fromisoformat(r["expires_at"]).timestamp(), time.time() + 40 * 60, delta=5)
		self.quiet(25)  # silent past the 15 min lease, inside the promised 40
		self.assertEqual(self.s.get("CMB-1")["status"], "doing")
		self.assertGreater(self.s.add_note("CMB-1", "backend-1", "still rendering")["lease_left_s"], 14 * 60)

	def test_until_is_capped_and_a_dead_worker_still_frees_the_ticket(self):
		self.s.claim("backend-1", tid="CMB-1")
		r = self.s.heartbeat("CMB-1", "backend-1", until=self.at(600))
		self.assertTrue(any("capped" in w and "60 min" in w for w in r["warnings"]))
		self.assertLessEqual(r["lease_left_s"], 60 * 60)
		p = self.dir / ".unblockd" / "claims.json"
		c = json.loads(p.read_text())
		c["CMB-1"]["until"] = datetime.fromtimestamp(time.time() - 60, timezone.utc).isoformat(timespec="seconds")  # the promise ran out
		c["CMB-1"]["heartbeat_at"] = datetime.fromtimestamp(time.time() - 16 * 60, timezone.utc).isoformat(timespec="seconds")  # and no heartbeat since
		p.write_text(json.dumps(c))
		self.assertEqual(self.s.get("CMB-1")["status"], "todo")

	def test_bad_until_and_bad_config_refused(self):
		self.s.claim("backend-1", tid="CMB-1")
		for bad, pat in (("tomorrow", "ISO time"), (self.at(-5), "in the past")):
			with self.assertRaisesRegex(TicketError, pat):
				self.s.heartbeat("CMB-1", "backend-1", until=bad)
		self.assertGreater(self.s.heartbeat("CMB-1", "backend-1", until=self.at(30).replace("+00:00", "Z"))["lease_left_s"], 29 * 60)
		for cfg, pat in (({"agents": [{"name": "a", "lease_minutes": 0}]}, "agent a: lease_minutes"), ({"agents": [{"name": "a", "lease_minutes": "30"}]}, "agent a: lease_minutes"),
		                 ({"lease_max_minutes": -1}, "lease_max_minutes")):
			(self.dir / "unblockd.json").write_text(json.dumps(cfg), encoding="utf-8")
			with self.assertRaisesRegex(TicketError, pat):
				Store(self.dir).list()
