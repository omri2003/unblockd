"""The connector contract: what every storage connector must guarantee when several agents act at the same moment.

A connector's test class mixes in BoardContract and implements open_board(cfg) -> a function that returns a NEW Store on
one fresh board each time it is called (its own connection / handle, like a separate agent process). Every case runs its
workers in threads that each open their own Store and start together on a barrier. Not a test module itself (no
test_ prefix): tests/test_contract.py binds it to each connector.
"""
import threading

from unblockd.store import Conflict, TicketError

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}]}  # no profiles: any worker name may claim any ticket
ROUNDS = 5  # races are probabilistic: repeat the ones that need two writers to meet


def together(n: int, fn) -> list:
	"""Run fn(i) in n threads released at once; returns each result, or the exception it raised."""
	gate, out = threading.Barrier(n), [None] * n

	def run(i):
		gate.wait(10)
		try:
			out[i] = fn(i)
		except Exception as e:  # noqa: BLE001 - the case decides what counts as a failure
			out[i] = e
	ths = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
	[t.start() for t in ths]
	[t.join(60) for t in ths]
	if any(t.is_alive() for t in ths):
		raise AssertionError("a worker is still running after 60 s (a lock that never frees, or a loop)")
	return out


def unexpected(res) -> list:
	"""Exceptions that are not the store's own refusals."""
	return [r for r in res if isinstance(r, Exception) and not isinstance(r, TicketError)]


class BoardContract:
	def open_board(self, cfg: dict):  # pragma: no cover - each connector implements it
		raise NotImplementedError

	def backdate_claim(self, tid: str) -> None:  # pragma: no cover - each connector implements it
		"""Set the live claim's heartbeat_at to 2020, as if its worker went silent long ago (no sleeping in tests)."""
		raise NotImplementedError

	def setUp(self):
		super().setUp()
		self.new = self.open_board(CFG)
		self.s = self.new()

	def test_one_ticket_claimed_by_many_at_once_has_one_holder(self):
		for r in range(ROUNDS):
			tid = self.s.create({"title": f"t{r}"}, prefix="T")["id"]
			res = together(8, lambda i: self.new().claim(f"w-{i}", tid=tid))
			self.assertEqual(unexpected(res), [])
			won = [x for x in res if isinstance(x, dict)]
			self.assertEqual(len(won), 1, f"round {r}: {res}")
			self.assertEqual(self.s.get(tid)["claim"]["agent"], won[0]["claim"]["agent"])

	def test_claim_next_by_many_workers_hands_each_ticket_out_once(self):
		ids = {self.s.create({"title": f"t{i}"}, prefix="T")["id"] for i in range(12)}

		def worker(i):
			s, got = self.new(), []
			for _ in range(20):  # capped: a broken connector may hand the same ticket out forever
				t = s.claim_next(f"w-{i}")
				if not t.get("id"):
					break
				got.append(t["id"])
			return got
		res = together(8, worker)
		self.assertEqual(unexpected(res), [])
		got = [tid for r in res for tid in r]
		self.assertEqual(sorted(got), sorted(ids))  # every ticket once, none twice
		self.assertEqual(sorted(self.s.agents_now()["working"][i]["ticket"] for i in range(12)), sorted(ids))

	def test_tickets_touching_the_same_files_are_never_held_together(self):
		for r in range(ROUNDS):
			a = self.s.create({"title": "a", "touches": [f"src/r{r}/a.py"]}, prefix="T")["id"]
			b = self.s.create({"title": "b", "touches": [f"src/r{r}/*"]}, prefix="T")["id"]
			res = together(6, lambda i: self.new().claim(f"w-{r}{i}", tid=(a, b)[i % 2]))
			self.assertEqual(unexpected(res), [])
			held = [t["id"] for t in self.s.list(status="doing") if t["id"] in (a, b)]
			self.assertEqual(len(held), 1, f"round {r}: both {a} and {b} claimed")

	def test_parallel_creates_never_share_an_id(self):
		res = together(8, lambda i: [self.new().create({"title": f"w{i} t{k}"}, prefix="T")["id"] for k in range(8)])
		self.assertEqual(unexpected(res), [])
		ids = [tid for r in res for tid in r]
		self.assertEqual(len(ids), 64)
		self.assertEqual(len(set(ids)), 64)
		self.assertEqual(len(self.s.list()), 64)  # and none was overwritten

	def test_a_blocker_loop_made_from_two_sides_at_once_is_refused(self):
		for r in range(ROUNDS):
			x = self.s.create({"title": "x"}, prefix="T")["id"]
			y = self.s.create({"title": "y"}, prefix="T")["id"]
			pairs = ((x, y), (y, x))
			res = together(2, lambda i: self.new().update(pairs[i][0], {"blocked_by": [pairs[i][1]]}))
			self.assertEqual(unexpected(res), [])
			self.assertEqual(sum(isinstance(v, dict) for v in res), 1, f"round {r}: {res}")
			self.assertFalse(x in self.s.get(y)["blocked_by"] and y in self.s.get(x)["blocked_by"])

	def test_split_is_all_or_nothing_and_only_once(self):
		p = self.s.create({"title": "big"}, prefix="T")["id"]
		before = len(self.s.list())
		with self.assertRaises(TicketError):
			self.s.split(p, ["part one", "x" * 10000])  # one bad title: nothing may be created
		self.assertEqual(len(self.s.list()), before)
		self.assertEqual(self.s.get(p)["blocked_by"], [])
		res = together(4, lambda i: self.new().split(p, [f"a{i}", f"b{i}"]))
		self.assertEqual(unexpected(res), [])
		self.assertEqual(sum(isinstance(v, dict) for v in res), 1, res)  # the others: "already split"
		self.assertEqual(len(self.s.list()), before + 2)
		self.assertEqual(len(self.s.get(p)["children"]), 2)

	def test_an_expired_claim_is_taken_over_by_exactly_one_worker(self):
		s = self.s
		tid = s.create({"title": "t"}, prefix="T")["id"]
		s.claim("w-old", tid=tid)
		self.backdate_claim(tid)  # its heartbeat is now years old: expired, while new claims get the normal 15 min lease
		res = together(6, lambda i: self.new().claim(f"w-{i}", tid=tid))
		self.assertEqual(unexpected(res), [])
		won = [x for x in res if isinstance(x, dict)]
		self.assertEqual(len(won), 1, res)
		events = [e["event"] for e in s.activity(tid=tid, limit=50)]
		self.assertEqual(events.count("expired"), 1)  # swept once, not once per worker

	def test_two_edits_from_the_same_rev_one_wins_one_conflicts(self):
		for r in range(ROUNDS):
			tid = self.s.create({"title": "t"}, prefix="T")["id"]
			rev = self.s.get(tid)["rev"]
			res = together(2, lambda i: self.new().update(tid, {"body": f"from {i}"}, expected_rev=rev))
			self.assertEqual(sorted(type(v).__name__ for v in res), ["Conflict", "dict"], f"round {r}: {res}")
			won = next(i for i, v in enumerate(res) if isinstance(v, dict))
			t = self.s.get(tid)
			self.assertEqual((t["body"], t["rev"]), (f"from {won}", rev + 1))
		self.assertTrue(issubclass(Conflict, TicketError))

	def test_notes_added_in_parallel_are_all_kept_and_numbered_once(self):
		tid = self.s.create({"title": "t"}, prefix="T")["id"]
		res = together(8, lambda i: [self.new().add_note(tid, f"w-{i}", f"note {i}.{k}") for k in range(6)])
		self.assertEqual(unexpected(res), [])
		notes = self.s.get(tid)["notes"]
		self.assertEqual(len(notes), 48)
		self.assertEqual(sorted(n["n"] for n in notes), list(range(1, 49)))
		self.assertEqual(len({n["text"] for n in notes}), 48)

	def test_a_claim_survives_edits_made_at_the_same_time(self):
		ids = [self.s.create({"title": f"t{i}"}, prefix="T")["id"] for i in range(10)]

		def act(i):
			s = self.new()
			if i == 0:
				return [t["id"] for t in (s.claim("w-1") for _ in range(20)) if t]  # capped like above
			for tid in ids * 2:
				s.update(tid, {"body": f"edit {i}"})
			return []
		res = together(3, act)
		self.assertEqual(unexpected(res), [])
		for tid in res[0]:  # an edit that read the ticket before the claim must not write the old status back
			self.assertEqual(self.s.get(tid)["status"], "doing", tid)
