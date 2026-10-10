"""Claims, the lock and rev across real OS processes (test_claims covers threads in one process)."""
import json
import multiprocessing as mp
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path

from unblockd.store import Conflict, Store

CTX = mp.get_context("fork")  # children inherit the barrier; no pickling of it
CFG = {"owners": [{"name": "Claude", "kind": "agent"}], "lease_minutes": 1}


def _claimer(d, n, barrier, out):
	s = Store(Path(d))
	barrier.wait(10)  # all workers start together
	try:
		r = s.claim_next(f"w-{n}")
		out.put((n, r.get("id"), r.get("reason"), None))  # success = the ticket itself, empty = {ticket: None, reason}
	except Exception as e:  # reported to the parent, which fails the test
		out.put((n, None, None, repr(e)))


def _updater(d, n, barrier, out):
	s = Store(Path(d))
	rev = s.get("T-1")["rev"]  # both read the same rev before either writes
	barrier.wait(10)
	try:
		s.update("T-1", {"body": f"from {n}"}, expected_rev=rev)
		out.put((n, "won"))
	except Conflict:
		out.put((n, "conflict"))
	except Exception as e:
		out.put((n, repr(e)))


def _die_holding_lock(d):
	Store(Path(d))._locked(lambda: os.kill(os.getpid(), signal.SIGKILL))


def _claim_then_die(d):
	Store(Path(d)).claim("w-dead", tid="T-1")
	os.kill(os.getpid(), signal.SIGKILL)


def _run(target, n, d):
	"""n processes of target(d, i, barrier, out), released together; returns what each put on `out`."""
	barrier, out = CTX.Barrier(n), CTX.Queue()
	ps = [CTX.Process(target=target, args=(d, i, barrier, out)) for i in range(n)]
	[p.start() for p in ps]
	res = [out.get(timeout=30) for _ in ps]
	[p.join(10) for p in ps]
	return res


def _kill(target, d):
	p = CTX.Process(target=target, args=(d,))
	p.start()
	p.join(10)
	return p.exitcode


class MultiProcessTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps(CFG), encoding="utf-8")
		self.s = Store(self.dir)

	def test_eight_processes_one_ticket_each_the_rest_get_nothing_ready(self):
		for i in range(1, 4):
			self.s.create({"title": f"t{i}"}, tid=f"T-{i}")
		res = _run(_claimer, 8, str(self.dir))
		self.assertEqual([r[3] for r in res if r[3]], [])  # no exceptions
		self.assertEqual(sorted(r[1] for r in res if r[1]), ["T-1", "T-2", "T-3"])  # each ticket exactly once
		self.assertEqual([r[2] for r in res if not r[1]], ["waiting"] * 5)  # the other 5: a clean answer, everything in flight
		claims = json.loads((self.dir / ".unblockd" / "claims.json").read_text())
		self.assertEqual(sorted(claims), ["T-1", "T-2", "T-3"])
		for t in self.s.list():
			self.assertEqual(t["status"], "doing")
			self.assertEqual(t["claim"]["agent"], claims[t["id"]]["agent"])
		for f in self.dir.glob("*.json"):
			json.loads(f.read_text())  # no half-written file

	def test_killed_lock_holder_is_stolen_after_the_stale_age(self):
		self.s.create({"title": "a"}, tid="T-1")
		self.assertEqual(_kill(_die_holding_lock, str(self.dir)), -signal.SIGKILL)
		lock = self.dir / ".unblockd" / "lock"
		self.assertTrue(lock.exists())  # the crash left it behind
		os.utime(lock, (time.time() - 120,) * 2)  # the 30 s stale age has passed (the constant is not configurable)
		self.assertEqual(Store(self.dir).claim("w-1")["id"], "T-1")
		self.assertFalse(lock.exists())

	def test_claim_of_a_killed_worker_expires_after_the_lease(self):
		self.s.create({"title": "a"}, tid="T-1")
		self.assertEqual(_kill(_claim_then_die, str(self.dir)), -signal.SIGKILL)
		self.assertEqual(Store(self.dir).get("T-1")["status"], "doing")  # lease (1 min, from config) still running
		cp = self.dir / ".unblockd" / "claims.json"
		c = json.loads(cp.read_text())
		c["T-1"]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"  # long silent; no sleeping
		cp.write_text(json.dumps(c))
		t = Store(self.dir).get("T-1")
		self.assertEqual((t["status"], t["claim"]), ("todo", None))
		self.assertEqual(Store(self.dir).claim("w-2")["id"], "T-1")  # and it can be taken again

	def test_same_expected_rev_in_two_processes_one_wins(self):
		self.s.create({"title": "a"}, tid="T-1")
		rev = self.s.get("T-1")["rev"]
		res = _run(_updater, 2, str(self.dir))
		self.assertEqual(sorted(r[1] for r in res), ["conflict", "won"])
		winner = next(r[0] for r in res if r[1] == "won")
		t = self.s.get("T-1")
		self.assertEqual((t["body"], t["rev"]), (f"from {winner}", rev + 1))


if __name__ == "__main__":
	unittest.main()
