"""AI-59: a lead's claim stays alive while its child agents work. A heartbeat (or add_note, update(agent=), complete) on a
child ticket renews the live claim on its parent chain when both claims name the same principal; never with no principal,
never a lapsed claim, never a status change, one claims write per heartbeat."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from unblockd.store import Store

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Dana", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "default_owner": "Claude", "pillars": [{"key": "GAME"}], "agents": [{"name": "lead", "pillars": ["GAME"]}, {"name": "art", "pillars": ["GAME"]}]}


class LeadLeaseTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows: SQLite keeps the db file open
		self.addCleanup(self.tmp.cleanup)
		self.s = self.open()
		self.s.create({"title": "ship the level", "pillar": "GAME"}, tid="SK-1")
		self.s.create({"title": "sprites", "pillar": "GAME", "parent": "SK-1"}, tid="SK-2")
		self.s.create({"title": "one sprite", "pillar": "GAME", "parent": "SK-2"}, tid="SK-3")

	def open(self):
		d = Path(self.tmp.name) / "tickets"
		d.mkdir(exist_ok=True)
		(d / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		return Store(d)

	def quiet(self, tid, minutes):
		"""Pretend tid's holder has been silent for `minutes`."""
		c = self.s.backend.claims()
		c[tid]["heartbeat_at"] = datetime.fromtimestamp(time.time() - minutes * 60, timezone.utc).isoformat(timespec="seconds")
		self.s.backend.save_claims(c)

	def beat_at(self, tid):
		return self.s.backend.claims()[tid]["heartbeat_at"]

	def lead_and_child(self, lead_for="Omri", child_for="Omri"):
		self.s.claim("lead-1", tid="SK-1", principal=lead_for)
		self.s.claim("art-1", tid="SK-2", principal=child_for)
		self.quiet("SK-1", 10)  # lease is 15 min: alive, but would lapse soon
		return self.beat_at("SK-1")

	def test_child_heartbeat_renews_the_leads_claim_for_the_same_principal(self):
		old = self.lead_and_child()
		self.s.heartbeat("SK-2", "art-1", "drawing")
		self.assertGreater(self.beat_at("SK-1"), old)
		self.quiet("SK-1", 10)
		self.s.add_note("SK-2", "art-1", "half done")  # implicit keepalives count too
		self.assertGreater(self.beat_at("SK-1"), old)
		self.quiet("SK-1", 10)
		self.s.update("SK-2", {"priority": "P2"}, agent="art-1")
		self.assertGreater(self.beat_at("SK-1"), old)
		self.quiet("SK-1", 10)
		self.s.complete("SK-2", "art-1", "sprites drawn")
		self.assertGreater(self.beat_at("SK-1"), old)
		t = self.s.get("SK-1")
		self.assertEqual((t["status"], t["claim"]["agent"]), ("doing", "lead-1"))  # status untouched, still the lead's

	def test_grandchild_renews_up_the_chain(self):
		self.s.claim("lead-1", tid="SK-1", principal="Omri")
		self.s.claim("art-1", tid="SK-3", principal="Omri")  # SK-2 between them is not claimed
		self.quiet("SK-1", 10)
		old = self.beat_at("SK-1")
		self.s.heartbeat("SK-3", "art-1")
		self.assertGreater(self.beat_at("SK-1"), old)

	def test_another_principal_does_not_renew(self):
		old = self.lead_and_child("Omri", "Dana")
		self.s.heartbeat("SK-2", "art-1")
		self.assertEqual(self.beat_at("SK-1"), old)

	def test_no_principal_does_not_renew(self):
		for lead_for, child_for in ((None, None), ("Omri", None), (None, "Omri")):
			with self.subTest(lead=lead_for, child=child_for):
				for tid in ("SK-1", "SK-2"):
					if self.s.get(tid)["claim"]:
						self.s.release(tid, self.s.get(tid)["claim"]["agent"])
				old = self.lead_and_child(lead_for, child_for)
				self.s.heartbeat("SK-2", "art-1")
				self.assertEqual(self.beat_at("SK-1"), old)

	def test_a_lapsed_lead_claim_is_not_revived(self):
		self.lead_and_child()
		self.quiet("SK-1", 20)  # past the 15 min lease
		self.s.heartbeat("SK-2", "art-1")
		t = self.s.get("SK-1")
		self.assertIsNone(t["claim"])
		self.assertEqual(t["status"], "todo")
		self.assertNotIn("SK-1", self.s.backend.claims())

	def test_one_claims_write_per_heartbeat(self):
		self.lead_and_child()
		saves = []
		real = self.s.backend.save_claims
		self.s.backend.save_claims = lambda c: (saves.append(1), real(c))
		self.s.heartbeat("SK-2", "art-1")
		self.assertEqual(len(saves), 1)

	def test_no_reads_up_the_chain_without_a_same_principal_claim(self):
		self.s.claim("art-1", tid="SK-3", principal="Omri")
		reads = []
		real = self.s.backend.read
		self.s.backend.read = lambda tid: (reads.append(tid), real(tid))[1]
		self.s.heartbeat("SK-3", "art-1")
		self.assertEqual(reads, [])


class SqliteLeadLeaseTest(LeadLeaseTest):
	def open(self):
		from unblockd import sql
		self.addCleanup(sql.disconnect)
		url = f"sqlite:///{Path(self.tmp.name) / 'b.db'}"
		sql.SqlBackend(url, "game").write_config(CFG)
		return Store(f"{url}#game")


if __name__ == "__main__":
	unittest.main()
