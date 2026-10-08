"""Claims: who is working what, leases, parallel safety, and the live view."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from unblockd.store import Store, TicketError

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "pillars": [{"key": "ART"}, {"key": "COMBAT"}],
       "agents": [{"name": "godot-dev", "pillars": ["COMBAT"]}, {"name": "blender", "pillars": ["ART"]}]}


class ClaimTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(self.dir)
		self.s.create({"title": "parry", "pillar": "COMBAT", "priority": "P1", "touches": ["scripts/player_3d.gd"]}, tid="CMB-1")
		self.s.create({"title": "dodge", "pillar": "COMBAT", "priority": "P2", "touches": ["scripts/*.gd"]}, tid="CMB-2")
		self.s.create({"title": "hud", "pillar": "COMBAT", "priority": "P3", "touches": ["scripts/hud_3d.gd"]}, tid="CMB-3")
		self.s.create({"title": "rig", "pillar": "ART", "owner": "Omri"}, tid="ART-1")  # a human's

	def tearDown(self):
		self.tmp.cleanup()

	def test_claim_next_by_profile_and_touches(self):
		a = self.s.claim("godot-dev-1")  # profile from the name
		self.assertEqual((a["id"], a["status"], a["claim"]["agent"]), ("CMB-1", "doing", "godot-dev-1"))
		b = self.s.claim("godot-dev-2")  # CMB-2's glob overlaps player_3d.gd: skipped
		self.assertEqual(b["id"], "CMB-3")
		self.assertIsNone(self.s.claim("godot-dev-3"))  # CMB-2 still overlaps CMB-1
		self.assertIsNone(self.s.claim("blender-1"))  # ART-1 belongs to a human
		self.assertTrue((self.dir / ".unblockd" / ".gitignore").is_file())

	def test_only_the_holder_can_beat_release_complete(self):
		self.s.claim("godot-dev-1", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "claimed by godot-dev-1"):
			self.s.heartbeat("CMB-1", "godot-dev-2", "hi")
		self.s.heartbeat("CMB-1", "godot-dev-1", "writing tests")
		self.assertEqual(self.s.get("CMB-1")["claim"]["note"], "writing tests")
		with self.assertRaisesRegex(TicketError, "needs a summary"):
			self.s.complete("CMB-1", "godot-dev-1", " ")
		t = self.s.complete("CMB-1", "godot-dev-1", "fixed; tests pass")
		self.assertEqual((t["status"], t["claim"]), ("done", None))
		self.assertIn("(godot-dev-1, done): fixed; tests pass", t["body"])
		self.assertEqual([e["event"] for e in self.s.activity("CMB-1")], ["complete", "heartbeat", "claim", "created"])

	def test_update_by_another_agent_cannot_touch_a_claimed_ticket(self):
		self.s.claim("godot-dev-1", tid="CMB-1")
		for ch in ({"status": "done"}, {"status": "doing"}, {"waiting_on": ""}):
			with self.assertRaisesRegex(TicketError, "claimed by godot-dev-1.*complete.*release.*ask_human"):
				self.s.update("CMB-1", ch, agent="godot-dev-2")
		self.assertEqual(self.s.update("CMB-1", {"body": "note"}, agent="godot-dev-2")["body"], "note")  # other fields are fine
		self.assertEqual(self.s.update("CMB-1", {"priority": "P0"}, agent="godot-dev-1")["priority"], "P0")
		self.assertEqual(self.s.update("CMB-2", {"status": "doing"}, agent="godot-dev-2")["status"], "doing")  # unclaimed: fine
		self.assertEqual(self.s.update("CMB-1", {"status": "todo"})["claim"], None)  # no agent = a human: as before

	def test_release_and_manual_status_change_end_the_claim(self):
		self.s.claim("godot-dev-1", tid="CMB-1")
		self.assertEqual(self.s.release("CMB-1", "godot-dev-1", "blocked on a decision")["status"], "todo")
		self.s.claim("godot-dev-1", tid="CMB-1")
		self.s.update("CMB-1", {"status": "todo", "waiting_on": "Omri: pick a parry window"})
		self.assertIsNone(self.s.get("CMB-1")["claim"])
		self.assertEqual(self.s.activity("CMB-1")[0]["event"], "dropped")

	def test_ask_parks_the_ticket_until_a_human_answers(self):
		self.s.claim("godot-dev-1", tid="CMB-2")
		with self.assertRaises(TicketError):
			self.s.ask("CMB-2", "godot-dev-2", "who?")  # not the holder
		with self.assertRaises(TicketError):
			self.s.ask("CMB-2", "godot-dev-1", "  ")
		t = self.s.ask("CMB-2", "godot-dev-1", "Omri: keep the dodge i-frames?")
		self.assertEqual((t["status"], t["effective_status"], t["claim"], t["waiting_on"]), ("todo", "pending", None, "Omri: keep the dodge i-frames?"))
		self.assertNotIn("CMB-2", [x["id"] for x in self.s.next_ready(limit=50)])
		with self.assertRaises(TicketError):
			self.s.claim("godot-dev-1", tid="CMB-2")
		ev = self.s.activity("CMB-2")[0]
		self.assertEqual((ev["event"], ev["agent"]), ("question", "godot-dev-1"))
		self.s.update("CMB-2", {"waiting_on": ""})  # answered: back in the pool
		self.assertEqual(self.s.get("CMB-2")["effective_status"], "todo")
		self.assertEqual(self.s.claim("godot-dev-1", tid="CMB-2")["id"], "CMB-2")

	def test_review_gate_lands_complete_in_review_and_does_not_unblock(self):
		d = Path(self.tmp.name) / "r" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({**CFG, "review": True}), encoding="utf-8")
		s = Store(d)
		s.create({"title": "a", "pillar": "COMBAT"}, tid="CMB-1")
		s.create({"title": "b", "pillar": "COMBAT", "blocked_by": ["CMB-1"]}, tid="CMB-2")
		s.claim("godot-dev-1", tid="CMB-1")
		t = s.complete("CMB-1", "godot-dev-1", "did it")
		self.assertEqual((t["status"], t["claim"]), ("review", None))
		self.assertIn("(godot-dev-1, review): did it", t["body"])
		b = s.get("CMB-2")
		self.assertEqual((b["effective_status"], b["open_blockers"]), ("pending", ["CMB-1"]))  # review is not done
		self.assertIsNone(s.claim("godot-dev-2"))  # nothing claimable, review is for a human
		self.assertEqual(s.update("CMB-1", {"status": "done"})["status"], "done")  # the human's decision
		self.assertEqual(s.get("CMB-2")["effective_status"], "todo")
		bad = Path(self.tmp.name) / "bad" / "tickets"
		bad.mkdir(parents=True)
		(bad / "unblockd.json").write_text(json.dumps({"review": "yes"}), encoding="utf-8")
		with self.assertRaises(TicketError):
			Store(bad)

	def test_complete_without_review_is_done(self):
		self.s.claim("godot-dev-1", tid="CMB-1")
		self.assertEqual(self.s.complete("CMB-1", "godot-dev-1", "ok")["status"], "done")

	def test_claim_reads_the_tickets_once_not_per_candidate(self):
		for i in range(10, 40):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT", "priority": "P0", "touches": ["x/*" if i == 10 else "x/y.gd"]}, tid=f"CMB-{i}")
		self.s.claim("godot-dev-9", tid="CMB-10")  # a live claim: every other touches-ticket overlaps it
		calls = []
		real = Store._read
		with mock.patch.object(Store, "_read", lambda self, p: (calls.append(p), real(self, p))[1]):
			got = self.s.claim("godot-dev-1")
		self.assertEqual(got["id"], "CMB-1")  # after 29 P0 tickets that overlap x/*
		self.assertLess(len(calls), 4 * 35)  # per-candidate rescans were ~30 x 35

	def test_split_wires_children_and_drops_the_claim(self):
		self.s.create({"title": "gate", "pillar": "COMBAT"}, tid="CMB-9")
		self.s.update("CMB-1", {"blocked_by": ["CMB-9"], "tags": ["agent:godot-dev"], "docs": ["AGENTS.md"]})
		self.s.update("CMB-9", {"status": "done"})
		self.s.claim("godot-dev-1", tid="CMB-1")
		r = self.s.split("CMB-1", ["window", " ", "vfx", "sound"], by="godot-dev-1")
		kids = r["children"]
		self.assertEqual([k["id"] for k in kids], ["CMB-10", "CMB-11", "CMB-12"])  # next free numbers, parent's prefix
		for k in kids:
			self.assertEqual((k["pillar"], k["owner"], k["priority"], k["tags"], k["touches"], k["docs"], k["body"], k["blocked_by"]),
			                 ("COMBAT", "Claude", "P1", ["agent:godot-dev"], ["scripts/player_3d.gd"], ["AGENTS.md"], "Split from CMB-1.", ["CMB-9"]))
		p = r["parent"]
		self.assertEqual((p["blocked_by"], p["status"], p["effective_status"], p["claim"]), (["CMB-9", "CMB-10", "CMB-11", "CMB-12"], "todo", "pending", None))
		self.assertEqual(p["unblocks"], 0)
		self.assertEqual(self.s.get("CMB-10")["unblocks"], 1)
		ev = self.s.activity("CMB-1")[0]
		self.assertEqual((ev["event"], ev["agent"], ev["note"]), ("split", "godot-dev-1", "CMB-10, CMB-11, CMB-12"))
		self.assertEqual(self.s.activity("CMB-1")[1]["event"], "dropped")

	def test_split_sequential_chains_the_parts(self):
		kids = self.s.split("CMB-2", ["a", "b", "c"], sequential=True)["children"]
		self.assertEqual([k["blocked_by"] for k in kids], [[], ["CMB-4"], ["CMB-5"]])
		self.assertEqual([k["effective_status"] for k in kids], ["todo", "pending", "pending"])

	def test_split_refusals(self):
		for args, msg in ((("NOPE-1", ["a", "b"]), "no ticket"), (("CMB-1", ["only"]), "at least 2 parts"),
		                  (("CMB-1", ["a", "  "]), "at least 2 parts"), (("CMB-1", ["a", "x" * 201]), "invalid title")):
			with self.assertRaisesRegex(TicketError, msg):
				self.s.split(*args)
		self.s.update("CMB-3", {"status": "done"})
		with self.assertRaisesRegex(TicketError, "already done"):
			self.s.split("CMB-3", ["a", "b"])
		self.assertEqual(len(self.s.list()), 4)  # nothing was created by the refused calls

	def test_lease_expires_back_to_todo(self):
		self.s.claim("godot-dev-1", tid="CMB-1")
		p = self.dir / ".unblockd" / "claims.json"
		c = json.loads(p.read_text())
		c["CMB-1"]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		p.write_text(json.dumps(c))
		t = self.s.get("CMB-1")
		self.assertEqual((t["status"], t["claim"]), ("todo", None))
		self.assertEqual(self.s.activity("CMB-1")[0]["event"], "expired")

	def test_activity_since_and_digest_events(self):
		self.s.claim("godot-dev-1", tid="CMB-1")
		self.s.release("CMB-1", "godot-dev-1", "stuck on rig")
		self.s.create({"title": "new one"}, tid="CMB-9")
		self.s.update("CMB-3", {"status": "done"})  # by hand
		self.s.update("CMB-3", {"title": "hud2"})  # not a transition: no second event
		ev = [e["event"] for e in self.s.activity(since="2020-01-01T00:00:00Z", limit=100)]
		self.assertEqual(ev.count("done"), 1)
		self.assertEqual(ev.count("created"), 5)
		self.assertEqual([e["event"] for e in self.s.activity(since="2020-01-01T00:00:00")][:2], ["done", "created"])
		self.assertEqual(self.s.activity(since="2999-01-01T00:00:00Z"), [])
		with self.assertRaisesRegex(TicketError, "since must be an ISO timestamp"):
			self.s.activity(since="yesterday")

	def test_refusals(self):
		with self.assertRaisesRegex(TicketError, "bad agent name"):
			self.s.claim("Bad Name")
		with self.assertRaisesRegex(TicketError, "can't be claimed"):
			self.s.claim("godot-dev-1", tid="ART-1")
		self.s.claim("godot-dev-9", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "already claimed by godot-dev-9"):
			self.s.claim("godot-dev-8", tid="CMB-1")

	def test_claim_next_says_why_it_is_empty(self):
		why = lambda agent="godot-dev-2": self.s.claim_next(agent)
		self.assertEqual(self.s.claim_next("godot-dev-1")["id"], "CMB-1")
		got = why()  # CMB-3 is free; take it so only the overlapping CMB-2 is left
		self.assertEqual(got["id"], "CMB-3")
		r = why("godot-dev-3")  # CMB-2 is ready but overlaps CMB-1's touches: retry later
		self.assertEqual((r["ticket"], r["reason"]), (None, "conflict"))
		self.assertEqual(r["counts"], {"in_flight": 2, "ready_conflicting": 1, "in_review": 0, "waiting_on_human": 0, "blocked": 0})
		self.s.release("CMB-1", "godot-dev-1")
		self.s.update("CMB-2", {"status": "done"})
		self.s.update("CMB-1", {"waiting_on": "Omri: ok?"})
		self.assertEqual(why("godot-dev-3")["reason"], "waiting")  # CMB-3 in flight, CMB-1 waits on a human
		self.s.update("CMB-1", {"status": "done"})
		self.s.complete("CMB-3", "godot-dev-2", "ok")
		r = why("godot-dev-3")
		self.assertEqual((r["reason"], sum(r["counts"].values())), ("dry", 0))  # ART-1 is blender's: not counted for godot-dev
		self.assertEqual(why("blender-1")["counts"]["waiting_on_human"], 1)

	def test_unknown_worker_name_is_refused_on_a_board_with_profiles(self):
		with self.assertRaisesRegex(TicketError, "matches no agent profile.*godot-dev, blender.*godot-dev-1"):
			self.s.claim("claude-1")
		with self.assertRaisesRegex(TicketError, "matches no agent profile"):
			self.s.claim("claude-1", tid="CMB-1")
		self.assertEqual(self.s.claim("claude-1", profile="godot-dev")["id"], "CMB-1")  # an explicit profile is a choice
		with tempfile.TemporaryDirectory() as d:  # no profiles: any name works
			s = Store(d)
			s.create({"title": "x"}, tid="E-1")
			self.assertEqual(s.claim("whoever")["id"], "E-1")

	def test_parallel_claims_never_collide(self):
		for i in range(4, 14):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT"}, tid=f"CMB-{i}")
		got, lock = [], threading.Lock()

		def worker(n):
			s = Store(self.dir)  # its own Store, like a separate process
			while (t := s.claim(f"godot-dev-{n}")) is not None:
				with lock:
					got.append(t["id"])
		ths = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
		[t.start() for t in ths]
		[t.join() for t in ths]
		self.assertEqual(len(got), len(set(got)))  # nobody got the same ticket twice
		view = self.s.agents_now()
		self.assertEqual(len(view["working"]), len(got))

	def test_update_does_not_lose_a_concurrent_claim(self):
		for i in range(4, 24):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT"}, tid=f"CMB-{i}")
		got = []

		def claimer():
			s = Store(self.dir)
			while (t := s.claim("godot-dev-w1")) is not None:
				got.append(t["id"])

		def editor():
			s = Store(self.dir)
			for _ in range(3):
				for i in range(1, 24):
					s.update(f"CMB-{i}", {"body": "edit"})
		ths = [threading.Thread(target=claimer), threading.Thread(target=editor), threading.Thread(target=editor)]
		[t.start() for t in ths]
		[t.join() for t in ths]
		for tid in got:  # an unlocked update re-writes the old status over the claim
			self.assertEqual(self.s.get(tid)["status"], "doing", tid)


if __name__ == "__main__":
	unittest.main()
