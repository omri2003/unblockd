"""Claims: who is working what, leases, parallel safety, and the live view."""
import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest import mock

from unblockd.store import Store, TicketError, _ts

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
       "pillars": [{"key": "ART"}, {"key": "COMBAT"}],
       "agents": [{"name": "backend", "pillars": ["COMBAT"]}, {"name": "blender", "pillars": ["ART"]}]}


def _claim_all(args):
	d, n = args
	s = Store(Path(d))
	got = []
	while (t := s.claim(f"backend-{n}")) is not None:
		got.append(t["id"])
	return got


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
		a = self.s.claim("backend-1")  # profile from the name
		self.assertEqual((a["id"], a["status"], a["claim"]["agent"]), ("CMB-1", "doing", "backend-1"))
		b = self.s.claim("backend-2")  # CMB-2's glob overlaps player_3d.gd: skipped
		self.assertEqual(b["id"], "CMB-3")
		self.assertIsNone(self.s.claim("backend-3"))  # CMB-2 still overlaps CMB-1
		self.assertIsNone(self.s.claim("blender-1"))  # ART-1 belongs to a human
		self.assertTrue((self.dir / ".unblockd" / ".gitignore").is_file())

	def test_only_the_holder_can_beat_release_complete(self):
		self.s.claim("backend-1", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "claimed by backend-1"):
			self.s.heartbeat("CMB-1", "backend-2", "hi")
		self.s.heartbeat("CMB-1", "backend-1", "writing tests")
		self.assertEqual(self.s.get("CMB-1")["claim"]["note"], "writing tests")
		with self.assertRaisesRegex(TicketError, "needs a summary"):
			self.s.complete("CMB-1", "backend-1", " ")
		t = self.s.complete("CMB-1", "backend-1", "fixed; tests pass")
		self.assertEqual((t["status"], t["claim"]), ("done", None))
		self.assertIn("(backend-1, done): fixed; tests pass", t["body"])
		self.assertEqual([e["event"] for e in self.s.activity("CMB-1")], ["complete", "heartbeat", "claim", "created"])

	def test_update_by_another_agent_cannot_touch_a_claimed_ticket(self):
		self.s.claim("backend-1", tid="CMB-1")
		for ch in ({"status": "done"}, {"status": "doing"}, {"waiting_on": ""}):
			with self.assertRaisesRegex(TicketError, "claimed by backend-1; only the holder"):
				self.s.update("CMB-1", ch, agent="backend-2")
		with self.assertRaisesRegex(TicketError, "only the holder"):  # any field, not just status/waiting_on
			self.s.update("CMB-1", {"body": "note"}, agent="backend-2")
		self.assertEqual(self.s.update("CMB-1", {"body": "human note"})["body"], "human note")  # no agent identity = a human
		for call in (lambda: self.s.split("CMB-1", ["a", "b"], by="backend-2"), lambda: self.s.delete("CMB-1", agent="backend-2")):
			with self.assertRaisesRegex(TicketError, "only the holder"):
				call()
		self.assertEqual(len(self.s.list()), 4)  # nothing split or deleted
		self.assertIn("hello", self.s.add_note("CMB-1", "backend-2", "hello")["body"])  # notes from anyone, even on a held ticket
		self.assertEqual(self.s.update("CMB-1", {"priority": "P0"}, agent="backend-1")["priority"], "P0")
		with self.assertRaisesRegex(TicketError, "claim_ticket / complete / ask_human"):  # a profile-named worker must claim
			self.s.update("CMB-2", {"status": "doing"}, agent="backend-2")
		self.assertEqual(self.s.update("CMB-2", {"status": "doing"}, agent="claude")["status"], "doing")  # a non-profile actor: fine
		self.assertEqual(self.s.update("CMB-1", {"status": "todo"})["claim"], None)  # no agent = a human: as before

	def test_release_and_manual_status_change_end_the_claim(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.assertEqual(self.s.release("CMB-1", "backend-1", "blocked on a decision")["status"], "todo")
		self.s.claim("backend-1", tid="CMB-1")
		self.s.update("CMB-1", {"status": "todo", "waiting_on": "Omri: pick a parry window"})
		self.assertIsNone(self.s.get("CMB-1")["claim"])
		self.assertEqual(self.s.activity("CMB-1")[0]["event"], "dropped")

	def test_ask_parks_the_ticket_until_a_human_answers(self):
		self.s.claim("backend-1", tid="CMB-2")
		with self.assertRaises(TicketError):
			self.s.ask("CMB-2", "backend-2", "who?")  # not the holder
		with self.assertRaises(TicketError):
			self.s.ask("CMB-2", "backend-1", "  ")
		t = self.s.ask("CMB-2", "backend-1", "Omri: keep the dodge i-frames?")
		self.assertEqual((t["status"], t["effective_status"], t["claim"], t["waiting_on"]), ("todo", "pending", None, "Omri: keep the dodge i-frames?"))
		self.assertNotIn("CMB-2", [x["id"] for x in self.s.next_ready(limit=50)])
		with self.assertRaises(TicketError):
			self.s.claim("backend-1", tid="CMB-2")
		ev = self.s.activity("CMB-2")[0]
		self.assertEqual((ev["event"], ev["agent"]), ("question", "backend-1"))
		self.s.update("CMB-2", {"waiting_on": ""})  # answered: back in the pool
		self.assertEqual(self.s.get("CMB-2")["effective_status"], "todo")
		self.assertEqual(self.s.claim("backend-1", tid="CMB-2")["id"], "CMB-2")

	def test_ask_records_asked_at_and_by_answer_clears_them(self):
		self.s.claim("backend-1", tid="CMB-2")
		t = self.s.ask("CMB-2", "backend-1", "keep i-frames?")
		self.assertEqual(t["asked_by"], "backend-1")
		self.assertRegex(t["asked_at"], r"^\d{4}-\d\d-\d\dT")
		for k in ("asked_at", "asked_by"):
			with self.assertRaises(TicketError):
				self.s.update("CMB-2", {k: "x"})
		t = self.s.answer("CMB-2", "omri", "yes")
		self.assertEqual((t["waiting_on"], t["asked_at"], t["asked_by"]), ("", "", ""))

	def test_hand_set_waiting_on_has_age_but_no_asker(self):
		t = self.s.update("CMB-1", {"waiting_on": "Omri: pick"})
		self.assertEqual(t["asked_by"], "")
		self.assertTrue(t["asked_at"])
		first = t["asked_at"]
		self.assertEqual(self.s.update("CMB-1", {"waiting_on": "Omri: pick again"})["asked_at"], first)  # editing keeps the age
		t = self.s.update("CMB-1", {"waiting_on": ""})
		self.assertEqual((t["asked_at"], t["asked_by"]), ("", ""))
		with self.assertRaises(TicketError):
			self.s.create({"title": "x", "asked_by": "a"}, prefix="CMB")

	def test_add_note_appends_signed_lines_and_survives_a_concurrent_complete(self):
		self.s.claim("backend-1", tid="CMB-1")
		t = self.s.add_note("CMB-1", "backend-1", "found the cause")
		self.assertRegex(t["body"], r"^— backend-1, \d{4}-\d\d-\d\dT[\d:]+\+00:00: found the cause$")
		self.assertEqual(self.s.activity("CMB-1")[0]["event"], "note")
		for bad in (("", "x"), ("a", "  ")):
			with self.assertRaises(TicketError):
				self.s.add_note("CMB-1", *bad)
		with self.assertRaises(TicketError):
			self.s.add_note("NOPE-1", "a", "x")
		ts = [threading.Thread(target=lambda i=i: Store(self.dir).add_note("CMB-1", "human", f"n{i}")) for i in range(8)]
		ts.append(threading.Thread(target=lambda: Store(self.dir).complete("CMB-1", "backend-1", "all done")))
		[x.start() for x in ts]
		[x.join() for x in ts]
		body = self.s.get("CMB-1")["body"]
		self.assertEqual([f"n{i}" in body for i in range(8)], [True] * 8)  # nothing lost, whatever the order
		self.assertIn("all done", body)
		self.assertIn("found the cause", body)

	def test_ask_release_and_expiry_leave_notes(self):
		with self.assertRaisesRegex(TicketError, "limit is 1000"):
			self.s.claim("backend-1", tid="CMB-1")
			self.s.ask("CMB-1", "backend-1", "x" * 1001)
		long_q = "q" * 900
		self.assertEqual(self.s.ask("CMB-1", "backend-1", long_q)["waiting_on"], long_q)
		self.assertIn("question: " + long_q, self.s.get("CMB-1")["body"])  # kept after waiting_on is cleared
		self.s.update("CMB-1", {"waiting_on": ""})
		self.assertIn("question: ", self.s.get("CMB-1")["body"])
		self.s.claim("backend-1", tid="CMB-2")
		self.assertIn("released: too big", self.s.release("CMB-2", "backend-1", "too big")["body"])
		self.s.claim("backend-1", tid="CMB-3")
		with mock.patch("time.time", return_value=time.time() + 3600):
			self.assertIn("claim by backend-1 expired", self.s.get("CMB-3")["body"])

	def test_answer_clears_the_question_and_leaves_a_note(self):
		with self.assertRaisesRegex(TicketError, "isn't waiting"):
			self.s.answer("CMB-2", "Omri", "yes")
		self.s.claim("backend-1", tid="CMB-2")
		self.s.ask("CMB-2", "backend-1", "keep i-frames?")
		with self.assertRaises(TicketError):
			self.s.answer("CMB-2", "Omri", " ")
		t = self.s.answer("CMB-2", "Omri", "keep them")
		self.assertEqual((t["waiting_on"], t["effective_status"]), ("", "todo"))
		self.assertRegex(t["body"], r"— Omri, [^\n]*: Answer to: keep i-frames\? — keep them$")
		self.assertEqual(self.s.activity("CMB-2")[0]["event"], "answered")
		with self.assertRaises(TicketError):
			self.s.answer("CMB-2", "Omri", "again")  # already answered

	def test_long_waiting_on_survives_an_edit_and_notes_respect_body_max(self):
		from unblockd import store as st
		self.s.claim("backend-1", tid="CMB-1")
		q = "q" * 1000
		self.s.ask("CMB-1", "backend-1", q)
		self.assertEqual(self.s.update("CMB-1", {"title": "parry2", "waiting_on": q})["waiting_on"], q)  # the edit form's save
		with self.assertRaises(TicketError):
			self.s.update("CMB-1", {"waiting_on": "q" * 1001})
		# complete: summary and handoff caps, refused before the claim is touched
		self.s.claim("backend-1", tid="CMB-2")
		with self.assertRaisesRegex(TicketError, "limit is 10000"):
			self.s.complete("CMB-2", "backend-1", "s" * 10001)
		with self.assertRaisesRegex(TicketError, "2000"):
			self.s.complete("CMB-2", "backend-1", "ok", handoff="h" * 2001)
		self.assertEqual(self.s.get("CMB-2")["claim"]["agent"], "backend-1")
		# a nearly full body: explicit writes are refused (claim kept), automatic ones skip the note
		raw = self.s._read(self.s._path("CMB-2"))
		raw["body"] = "b" * (st.BODY_MAX - 20)
		self.s._write(raw)
		for call in (lambda: self.s.complete("CMB-2", "backend-1", "done it"),
		             lambda: self.s.ask("CMB-2", "backend-1", "what now?"),
		             lambda: self.s.add_note("CMB-2", "Omri", "hello there")):
			with self.assertRaisesRegex(TicketError, "body would pass"):
				call()
		t = self.s.get("CMB-2")
		self.assertEqual((t["status"], t["claim"]["agent"], t["waiting_on"], t["result"]), ("doing", "backend-1", "", None))
		self.s.release("CMB-2", "backend-1", "reason that does not fit")
		t = self.s.get("CMB-2")
		self.assertEqual((t["status"], len(t["body"])), ("todo", st.BODY_MAX - 20))
		self.assertEqual(self.s.activity("CMB-2")[0]["event"], "release")

	def test_answer_and_reject_need_by(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.s.ask("CMB-1", "backend-1", "ok?")
		with self.assertRaisesRegex(TicketError, "needs `by`"):
			self.s.answer("CMB-1", "", "yes")
		with self.assertRaisesRegex(TicketError, "needs `by`"):
			self.s.reject("CMB-1", " ", "no")

	def test_reject_sends_review_back_with_the_reason(self):
		d = Path(self.tmp.name) / "r" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({**CFG, "review": True}), encoding="utf-8")
		s = Store(d)
		s.create({"title": "a", "pillar": "COMBAT"}, tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "not review"):
			s.reject("CMB-1", "Omri", "no")
		s.claim("backend-1", tid="CMB-1")
		s.complete("CMB-1", "backend-1", "did it")
		with self.assertRaises(TicketError):
			s.reject("CMB-1", "Omri", "")
		t = s.reject("CMB-1", "Omri", "window is 0.3s, not 0.2s")
		self.assertEqual((t["status"], t["effective_status"]), ("todo", "todo"))
		self.assertRegex(t["body"], r"— Omri, [^\n]*: Rejected: window is 0.3s, not 0.2s$")
		self.assertEqual(s.activity("CMB-1")[0]["event"], "rejected")
		self.assertIsNone(t.get("result"))  # rejected work is not upstream
		self.assertEqual(s.claim("backend-1", tid="CMB-1")["id"], "CMB-1")  # claimable again

	def test_review_gate_lands_complete_in_review_and_does_not_unblock(self):
		d = Path(self.tmp.name) / "r" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({**CFG, "review": True}), encoding="utf-8")
		s = Store(d)
		s.create({"title": "a", "pillar": "COMBAT"}, tid="CMB-1")
		s.create({"title": "b", "pillar": "COMBAT", "blocked_by": ["CMB-1"]}, tid="CMB-2")
		s.claim("backend-1", tid="CMB-1")
		t = s.complete("CMB-1", "backend-1", "did it")
		self.assertEqual((t["status"], t["claim"]), ("review", None))
		self.assertIn("(backend-1, review): did it", t["body"])
		b = s.get("CMB-2")
		self.assertEqual((b["effective_status"], b["open_blockers"]), ("pending", ["CMB-1"]))  # review is not done
		self.assertIsNone(s.claim("backend-2"))  # nothing claimable, review is for a human
		self.assertEqual(s.update("CMB-1", {"status": "done"})["status"], "done")  # the human's decision
		self.assertEqual(s.get("CMB-2")["effective_status"], "todo")
		bad = Path(self.tmp.name) / "bad" / "tickets"
		bad.mkdir(parents=True)
		(bad / "unblockd.json").write_text(json.dumps({"review": "yes"}), encoding="utf-8")
		with self.assertRaises(TicketError):
			Store(bad)

	def test_complete_without_review_is_done(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.assertEqual(self.s.complete("CMB-1", "backend-1", "ok")["status"], "done")

	def test_claim_reads_the_tickets_once_not_per_candidate(self):
		for i in range(10, 40):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT", "priority": "P0", "touches": ["x/*" if i == 10 else "x/y.gd"]}, tid=f"CMB-{i}")
		self.s.claim("backend-9", tid="CMB-10")  # a live claim: every other touches-ticket overlaps it
		calls = []
		real = Store._read
		with mock.patch.object(Store, "_read", lambda self, p: (calls.append(p), real(self, p))[1]):
			got = self.s.claim("backend-1")
		self.assertEqual(got["id"], "CMB-1")  # after 29 P0 tickets that overlap x/*
		self.assertLess(len(calls), 4 * 35)  # per-candidate rescans were ~30 x 35

	def test_split_wires_children_and_drops_the_claim(self):
		self.s.create({"title": "gate", "pillar": "COMBAT"}, tid="CMB-9")
		self.s.update("CMB-1", {"blocked_by": ["CMB-9"], "tags": ["agent:backend"], "docs": ["AGENTS.md"]})
		self.s.update("CMB-9", {"status": "done"})
		self.s.claim("backend-1", tid="CMB-1")
		r = self.s.split("CMB-1", ["window", " ", "vfx", "sound"], by="backend-1")
		kids = r["children"]
		self.assertEqual([k["id"] for k in kids], ["CMB-10", "CMB-11", "CMB-12"])  # next free numbers, parent's prefix
		for k in kids:
			self.assertEqual((k["pillar"], k["owner"], k["priority"], k["tags"], k["touches"], k["docs"], k["body"], k["blocked_by"]),
			                 ("COMBAT", "Claude", "P1", ["agent:backend"], ["scripts/player_3d.gd"], ["AGENTS.md"], "Split from CMB-1.", ["CMB-9"]))
		p = r["parent"]
		self.assertEqual((p["blocked_by"], p["status"], p["effective_status"], p["claim"]), (["CMB-9", "CMB-10", "CMB-11", "CMB-12"], "todo", "pending", None))
		self.assertEqual(p["unblocks"], 0)
		self.assertEqual(self.s.get("CMB-10")["unblocks"], 1)
		ev = self.s.activity("CMB-1")[0]
		self.assertEqual((ev["event"], ev["agent"], ev["note"]), ("split", "backend-1", "CMB-10, CMB-11, CMB-12"))
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
		self.s.claim("backend-1", tid="CMB-1")
		p = self.dir / ".unblockd" / "claims.json"
		c = json.loads(p.read_text())
		c["CMB-1"]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		p.write_text(json.dumps(c))
		t = self.s.get("CMB-1")
		self.assertEqual((t["status"], t["claim"]), ("todo", None))
		self.assertEqual(self.s.activity("CMB-1")[0]["event"], "expired")

	def test_claim_result_has_lease_and_previous_attempts(self):
		t = self.s.claim("backend-1", tid="CMB-1")
		self.assertEqual((t["lease_minutes"], t["previous_attempts"]), (self.s.config["lease_minutes"], []))
		self.assertEqual(t["expires_at"], datetime.fromtimestamp(_ts(t["claim"]["heartbeat_at"]) + t["lease_minutes"] * 60, timezone.utc).isoformat(timespec="seconds"))
		self.s.release("CMB-1", "backend-1", "tests need the rig")
		self.s.claim("backend-2", tid="CMB-1")
		p = self.dir / ".unblockd" / "claims.json"
		c = json.loads(p.read_text())
		c["CMB-1"]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		p.write_text(json.dumps(c))
		got = self.s.claim("backend-3", tid="CMB-1")
		self.assertEqual([(a["agent"], a["outcome"]) for a in got["previous_attempts"]], [("backend-2", "expired"), ("backend-1", "released")])
		self.assertEqual(got["previous_attempts"][1]["note"], "tests need the rig")
		for i in range(6):
			self.s.release("CMB-1", "backend-3"); self.s.claim("backend-3", tid="CMB-1")
		self.assertEqual(len(self.s.attempts("CMB-1", 5)), 5)
		self.assertEqual(self.s.claim_next("backend-4")["previous_attempts"], [])  # CMB-3, another ticket

	def test_calls_naming_the_holder_refresh_the_lease(self):
		self.s.claim("backend-1", tid="CMB-1")
		p = self.dir / ".unblockd" / "claims.json"
		def age():
			c = json.loads(p.read_text())
			c["CMB-1"]["heartbeat_at"] = "2026-01-01T00:00:00+00:00"
			p.write_text(json.dumps(c))
		for call in (lambda: self.s.add_note("CMB-1", "backend-1", "running tests"),
		             lambda: self.s.update("CMB-1", {"body": "x"}, agent="backend-1")):
			age()
			with mock.patch("unblockd.store.time.time", return_value=_ts("2026-01-01T00:10:00+00:00")):
				call()  # 10 min quiet: still live, and the call renews it
			self.assertGreater(json.loads(p.read_text())["CMB-1"]["heartbeat_at"], "2026-02")
		age()
		self.s.add_note("CMB-1", "omri", "a human's note")  # not the holder: no refresh
		self.assertEqual(self.s.get("CMB-1")["status"], "todo")  # lease long gone, swept

	def test_failing_after_three_attempts_until_a_human_touch(self):
		for who, how in (("backend-1", "release"), ("backend-2", "release"), ("backend-1", "release")):
			self.s.claim(who, tid="CMB-1"); self.s.release("CMB-1", who, "no luck")
		self.assertEqual(len(self.s.failing()["CMB-1"]), 3)
		self.s.add_note("CMB-1", "backend-9", "agent note does not count")
		self.assertIn("CMB-1", self.s.failing())
		self.s.add_note("CMB-1", "omri", "try the other rig")
		self.assertEqual(self.s.failing(), {})
		for _ in range(2):
			self.s.claim("backend-1", tid="CMB-1"); self.s.release("CMB-1", "backend-1")
		self.assertEqual(self.s.failing(), {})  # 2 since the human note
		self.s.claim("backend-1", tid="CMB-1"); self.s.release("CMB-1", "backend-1")
		self.assertEqual(len(self.s.failing()["CMB-1"]), 3)

	def test_activity_since_and_digest_events(self):
		self.s.claim("backend-1", tid="CMB-1")
		self.s.release("CMB-1", "backend-1", "stuck on rig")
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
			self.s.claim("backend-1", tid="ART-1")
		self.s.claim("backend-9", tid="CMB-1")
		with self.assertRaisesRegex(TicketError, "already claimed by backend-9"):
			self.s.claim("backend-8", tid="CMB-1")

	def test_claim_next_says_why_it_is_empty(self):
		why = lambda agent="backend-2": self.s.claim_next(agent)
		self.assertEqual(self.s.claim_next("backend-1")["id"], "CMB-1")
		got = why()  # CMB-3 is free; take it so only the overlapping CMB-2 is left
		self.assertEqual(got["id"], "CMB-3")
		r = why("backend-3")  # CMB-2 is ready but overlaps CMB-1's touches: retry later
		self.assertEqual((r["ticket"], r["reason"]), (None, "conflict"))
		self.assertEqual(r["counts"], {"in_flight": 2, "ready_conflicting": 1, "in_review": 0, "waiting_on_human": 0, "blocked": 0})
		self.s.release("CMB-1", "backend-1")
		self.s.update("CMB-2", {"status": "done"})
		self.s.update("CMB-1", {"waiting_on": "Omri: ok?"})
		self.assertEqual(why("backend-3")["reason"], "waiting")  # CMB-3 in flight, CMB-1 waits on a human
		self.s.update("CMB-1", {"status": "done"})
		self.s.complete("CMB-3", "backend-2", "ok")
		r = why("backend-3")
		self.assertEqual((r["reason"], sum(r["counts"].values())), ("dry", 0))  # ART-1 is blender's: not counted for backend
		self.assertEqual(why("blender-1")["counts"]["waiting_on_human"], 1)

	def test_claim_next_empty_answer_lists_ids_and_orphans(self):
		self.s.claim_next("backend-1")  # CMB-1
		self.s.claim_next("backend-2")  # CMB-3
		self.s.create({"title": "hand", "pillar": "COMBAT", "touches": ["x"]}, tid="CMB-4")
		self.s.update("CMB-4", {"status": "doing"})  # moved by hand: no claim
		r = self.s.claim_next("backend-3")
		self.assertEqual(r["ids"], {"in_flight": ["CMB-1", "CMB-3", "CMB-4"], "ready_conflicting": ["CMB-2"], "in_review": [], "waiting_on_human": [], "blocked": []})
		self.assertEqual(r["counts"]["in_flight"], 3)
		self.assertEqual(r["orphaned"], ["CMB-4"])

	def test_claim_refusal_names_the_cause(self):
		self.s.create({"title": "b", "pillar": "COMBAT", "blocked_by": ["CMB-1"]}, tid="CMB-5")
		self.s.create({"title": "q", "pillar": "COMBAT", "waiting_on": "which?"}, tid="CMB-6")
		self.s.claim("backend-1", tid="CMB-1")
		for tid, pat in (("CMB-5", "blocked by CMB-1"), ("CMB-6", "waiting on a human.*which"), ("ART-1", "owned by Omri"),
		                 ("CMB-2", "touches overlap CMB-1, claimed by backend-1")):
			with self.assertRaisesRegex(TicketError, pat):
				self.s.claim("backend-2", tid=tid)
		with self.assertRaisesRegex(TicketError, r"or pass profile=<name>"):
			self.s.claim("claude-1")

	def test_unknown_worker_name_is_refused_on_a_board_with_profiles(self):
		with self.assertRaisesRegex(TicketError, "matches no agent profile.*backend, blender.*backend-1"):
			self.s.claim("claude-1")
		with self.assertRaisesRegex(TicketError, "matches no agent profile"):
			self.s.claim("claude-1", tid="CMB-1")
		self.assertEqual(self.s.claim("claude-1", profile="backend")["id"], "CMB-1")  # an explicit profile is a choice
		with tempfile.TemporaryDirectory() as d:  # no profiles: any name works
			s = Store(d)
			s.create({"title": "x"}, tid="E-1")
			self.assertEqual(s.claim("whoever")["id"], "E-1")

	def test_processes_never_claim_the_same_ticket_even_over_a_stale_lock(self):
		for i in range(4, 34):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT"}, tid=f"CMB-{i}")
		lock = self.s._rt() / "lock"
		lock.write_text("")
		os.utime(lock, (time.time() - 120, time.time() - 120))  # a crashed holder's lock: every process tries to steal it at once
		with multiprocessing.Pool(4) as pool:
			lists = pool.map(_claim_all, [(str(self.dir), n) for n in range(1, 5)])
		got = [x for g in lists for x in g]
		self.assertEqual(len(got), len(set(got)))  # nobody got a ticket twice
		self.assertEqual(len(self.s.agents_now()["working"]), len(got))
		self.assertFalse(lock.exists())

	def test_steal_rechecks_age_so_a_fresh_lock_survives(self):
		lock = self.s._rt() / "lock"
		lock.write_text("")
		Store._steal(lock)  # a process that read an old, stale mtime arrives after another already replaced the lock
		self.assertTrue(lock.exists())
		os.utime(lock, (time.time() - 120,) * 2)
		Store._steal(lock)
		self.assertFalse(lock.exists())
		self.assertFalse(lock.with_name("lock.steal").exists())  # the guard is always removed
		guard = lock.with_name("lock.steal")
		guard.write_text("")
		os.utime(guard, (time.time() - 120,) * 2)  # a guard left by a crash is cleared on the next try
		Store._steal(lock)
		self.assertFalse(guard.exists())

	def test_stuck_steal_guard_times_out_instead_of_spinning(self):
		lock = self.s._rt() / "lock"
		for f in (lock, lock.with_name("lock.steal")):
			f.write_text("")
		os.utime(lock, (time.time() - 100,) * 2)  # crashed holder's lock, and a fresh guard held by someone alive
		os.utime(lock.with_name("lock.steal"), None)
		calls, ticks = [], iter([0, 11, 11, 11])  # deadline set at 0+10, then the clock jumps past it
		with mock.patch("unblockd.store.time.monotonic", lambda: next(ticks)), \
		     mock.patch.object(Store, "_steal", side_effect=lambda l: calls.append(1)):
			with self.assertRaisesRegex(TicketError, "try again"):
				self.s.update("CMB-1", {"body": "x"})
		self.assertLess(len(calls), 3)  # one look, then the deadline; no busy loop

	def test_corrupt_claims_file_is_an_error_not_an_empty_dict(self):
		self.s.claim("backend-1", tid="CMB-1")
		(self.dir / ".unblockd" / "claims.json").write_text("{not json", encoding="utf-8")
		with self.assertRaisesRegex(TicketError, "claims.json is not valid JSON.*delete .*claims.json: claims are runtime state"):
			self.s.list()
		with self.assertRaisesRegex(TicketError, "claims.json"):
			self.s.claim("backend-2")

	def test_parallel_claims_never_collide(self):
		for i in range(4, 14):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT"}, tid=f"CMB-{i}")
		got, lock = [], threading.Lock()

		def worker(n):
			s = Store(self.dir)  # its own Store, like a separate process
			while (t := s.claim(f"backend-{n}")) is not None:
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
			while (t := s.claim("backend-w1")) is not None:
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


class PrincipalTest(unittest.TestCase):
	"""principal = the human an agent works for: validated, stored in the claim, copied to result, part of the holder check."""
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		self.cfg({**CFG, "owners": [{"name": "Omri", "kind": "human"}, {"name": "Dana", "kind": "human"}, {"name": "Claude", "kind": "agent"}]})
		for i in (1, 2, 3):
			self.s.create({"title": f"t{i}", "pillar": "COMBAT"}, tid=f"CMB-{i}")

	def cfg(self, c):
		(self.dir / "unblockd.json").write_text(json.dumps(c), encoding="utf-8")
		self.s = Store(self.dir)

	def test_stored_shown_and_copied_to_result(self):
		t = self.s.claim("backend-1", tid="CMB-1", principal="Omri")
		self.assertEqual(t["claim"]["principal"], "Omri")
		self.assertNotIn("warnings", t)
		self.assertEqual(self.s.agents_now()["working"][0]["principal"], "Omri")
		self.s.heartbeat("CMB-1", "backend-1", "x", "Omri")
		done = self.s.complete("CMB-1", "backend-1", "fixed", principal="Omri")
		self.assertEqual((done["result"]["by"], done["result"]["principal"]), ("backend-1", "Omri"))
		ev = {e["event"]: e for e in self.s.activity("CMB-1")}
		self.assertEqual((ev["claim"]["principal"], ev["complete"]["principal"]), ("Omri", "Omri"))

	def test_review_reject_clears_principal(self):
		self.cfg({**CFG, "review": True, "owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}]})
		self.s.claim("backend-1", tid="CMB-1", principal="Omri")
		self.assertEqual(self.s.complete("CMB-1", "backend-1", "s")["result"]["principal"], "Omri")  # complete needs none: it comes from the claim
		self.assertIsNone(self.s.reject("CMB-1", "Omri", "no").get("result"))

	def test_unknown_or_agent_principal_refused(self):
		for bad in ("Nobody", "Claude"):
			with self.assertRaisesRegex(TicketError, "not a human owner.*Omri, Dana"):
				self.s.claim("backend-1", tid="CMB-1", principal=bad)
		self.assertIsNone(self.s.get("CMB-1")["claim"])

	def test_same_worker_name_two_principals(self):
		self.s.claim("backend-1", tid="CMB-1", principal="Omri")
		for call in (lambda: self.s.heartbeat("CMB-1", "backend-1", "", "Dana"), lambda: self.s.complete("CMB-1", "backend-1", "s", principal="Dana"),
		             lambda: self.s.release("CMB-1", "backend-1", "", "Dana")):
			with self.assertRaisesRegex(TicketError, "for Omri, not for Dana"):
				call()
		self.assertEqual(self.s.get("CMB-1")["claim"]["principal"], "Omri")
		self.s.heartbeat("CMB-1", "backend-1")  # no principal given: as before

	def test_no_principal_works_and_warns_only_with_several_humans(self):
		t = self.s.claim("backend-1", tid="CMB-1")
		self.assertIn("no principal", t["warnings"][0])
		self.assertNotIn("principal", t["claim"])
		self.assertNotIn("principal", self.s.complete("CMB-1", "backend-1", "s")["result"])
		self.assertNotIn("warnings", self.s.claim_next("backend-1", principal="Dana"))
		self.assertIn("warnings", self.s.claim_next("backend-2"))
		self.cfg(CFG)  # one human: nothing new
		self.s.release("CMB-3", "backend-2")
		self.assertNotIn("warnings", self.s.claim("backend-1", tid="CMB-3"))


if __name__ == "__main__":
	unittest.main()
