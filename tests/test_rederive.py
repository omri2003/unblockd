"""CORE-85: _board() re-derives only what a write changed. After every kind of change the patched board must equal the whole
board derived again from scratch (_decorate), key for key and in the same order, on the files connector and on SQLite."""
import json
import random
import shutil
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest import mock

from unblockd import files, store as st
from unblockd.errors import TicketError


def full(s: st.Store) -> dict:
	return {t["id"]: t for t in s._decorate(list(s.backend.tickets().values()))}


class Rederive:
	def open(self) -> st.Store:
		raise NotImplementedError

	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp())
		st._DEC.clear()

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)

	def same(self, s: st.Store, what: str):
		got, want = s._board(), full(s)
		self.assertEqual(list(got), list(want), what)
		for i in want:
			self.assertEqual(json.dumps(got[i]), json.dumps(want[i]), f"{what}: {i}")

	def test_every_change_matches_a_full_derive(self):
		s = self.open()
		rnd = random.Random(85)
		ids = [s.create({"title": f"t{k}", "priority": rnd.choice(["P0", "P1", "P2", None])}, prefix="T")["id"] for k in range(30)]
		self.same(s, "seed")
		for step in range(250):
			s = self.open()  # a fresh Store each step, like the web server: the cache is shared
			live = [t["id"] for t in s.list()]
			x, y = rnd.choice(live), rnd.choice(live)
			op = rnd.randrange(12)
			try:
				if op == 0:
					ids.append(s.create({"title": "new", "blocked_by": rnd.sample(live, rnd.randrange(3)),
					                     "parent": rnd.choice([None, y])}, prefix=rnd.choice(["T", "A", "Z"]))["id"])
				elif op == 1:
					s.update(x, {"status": rnd.choice(["todo", "done", "review", "doing"])})
				elif op == 2:
					s.update(x, {"blocked_by": rnd.sample(live, rnd.randrange(3))})
				elif op == 3:
					s.update(x, {"parent": rnd.choice([None, y])})
				elif op == 4 and len(live) > 10:
					s.delete(x)
				elif op == 5:
					t = s.claim(f"w-{step}")
					if t and rnd.random() < 0.5:
						s.release(t["id"], f"w-{step}", "later")
				elif op == 6:
					claimed = [t for t in s.list() if t.get("claim")]
					if claimed:
						c = rnd.choice(claimed)
						s.heartbeat(c["id"], c["claim"]["agent"], "still on it")
				elif op == 7:
					s.add_note(x, "human", f"note {step}")
				elif op == 8:
					s.backend.put_attachment(x, f"f{step}.txt", b"hello")
				elif op == 9:
					s.update(x, {"waiting_on": rnd.choice(["", "which one?"])})
				elif op == 10:
					claimed = [t for t in s.list() if t.get("claim")]
					if claimed:
						c = rnd.choice(claimed)
						s.complete(c["id"], c["claim"]["agent"], "did it")
				else:
					s.update(x, {"priority": rnd.choice(["P0", "P3", None]), "tags": rnd.choice([[], ["ui"]])})
			except TicketError:
				pass  # refused (a loop, doing with an open blocker...): nothing changed, still compared
			self.same(s, f"step {step} op {op}")

	def test_a_hand_made_loop_falls_back_to_the_whole_board(self):
		s = self.open()
		a, b = s.create({"title": "a"}, prefix="L")["id"], s.create({"title": "b"}, prefix="L")["id"]
		s.update(b, {"blocked_by": [a]})
		self.same(s, "chain")
		t = s.backend.read(a)
		t["blocked_by"] = [b]  # a hand edit or a pull: the store would refuse this loop
		s.backend.write(t)
		self.same(s, "loop")
		self.assertTrue(st._DEC[s._key][2]["loop"])

	def test_a_write_redecorates_only_what_it_reaches(self):
		s = self.open()
		ids = [s.create({"title": f"t{k}"}, prefix="T")["id"] for k in range(200)]
		s.update(ids[1], {"blocked_by": [ids[0]]})
		s.update(ids[2], {"blocked_by": [ids[1]]})
		s._board()
		calls = []
		orig = st.Store._dec1
		st.Store._dec1 = lambda self, t, *a: calls.append(t["id"]) or orig(self, t, *a)
		try:
			s.add_note(ids[50], "human", "x")
			s._board()
			self.assertEqual(set(calls), {ids[50]})
			calls.clear()
			s.update(ids[2], {"status": "done"})  # its blockers' unblocks drop, nothing else changes
			s._board()
			self.assertEqual(set(calls), set(ids[:3]))  # a database board may derive once inside its transaction too
		finally:
			st.Store._dec1 = orig
		self.same(s, "after")


class FilesRederive(Rederive, unittest.TestCase):
	def open(self) -> st.Store:
		return st.Store(self.tmp / "tickets")

	def passes(self, fn) -> int:
		n = []
		orig = files.FileBackend._pass
		with mock.patch.object(files.FileBackend, "_pass", lambda self, *a: n.append(1) or orig(self, *a)):
			fn()
		return len(n)

	def test_one_stat_pass_per_lock_hold(self):
		s = self.open()
		a = s.create({"title": "a"}, prefix="T")["id"]
		s._board()
		self.assertEqual(self.passes(lambda: s._locked(lambda: s.create({"title": "b", "blocked_by": [a]}, prefix="T"))), 1)
		self.assertEqual(self.passes(lambda: s._locked(lambda: (s.update(a, {"priority": "P1"}), s.delete(a)))), 1)
		self.assertEqual([t["id"] for t in s.list()], ["T-2"])  # the next read outside the lock stats again and sees both writes
		t = s.backend.read("T-2")
		t["title"] = "edited by hand"
		s.backend.write(t)
		self.assertEqual(s.get("T-2")["title"], "edited by hand")

	def test_threads_reading_at_once_share_stat_passes(self):
		s = self.open()
		for k in range(50):
			s.create({"title": f"t{k}"}, prefix="T")
		slow = files._stat
		start = threading.Barrier(20)

		def read():
			start.wait()
			st.Store(self.tmp / "tickets").backend.tickets()
		threads = [threading.Thread(target=read) for _ in range(20)]

		def run():
			for th in threads:
				th.start()
			for th in threads:
				th.join()
		with mock.patch.object(files, "_stat", lambda e: (time.sleep(0.001), slow(e))[1]):
			n = self.passes(run)
		self.assertLess(n, 20)  # a thread that asked while a pass ran takes the next one, not its own
		self.assertGreaterEqual(n, 1)


class SqliteRederive(Rederive, unittest.TestCase):
	def open(self) -> st.Store:
		return st.Store(f"sqlite:///{(self.tmp / 'b.db').as_posix()}#rd")

	def tearDown(self):
		from unblockd import sql
		sql.disconnect()
		super().tearDown()


if __name__ == "__main__":
	unittest.main()
