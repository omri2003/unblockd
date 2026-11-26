"""Behaviour every database connector shares beyond the race contract (tests/contract.py): a whole ticket life, all-or-nothing
under the lock, caches that stay true, many boards in one database, events after a cursor, settings in the database, lock
waits and the no-repo messages. Not a test module itself: tests/test_sql.py and tests/test_mongo.py bind it.

A binding sets `mod` (the connector module: SqlBackend / MongoBackend, boards(), disconnect(), BUSY_S), `atomic` (does a
failure under the lock roll everything back?) and implements database() and backend(board, create).
"""
import io
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from unblockd import knowledge as kn
from unblockd import store as store_mod
from unblockd.store import Store, TicketError

CFG = {"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}]}


def in_thread(fn):
	"""fn() in a new thread (its own connection or session, like another process); returns its result or raises its error."""
	out = {}
	th = threading.Thread(target=lambda: out.update(r=_try(fn)))
	th.start()
	th.join(30)
	r = out["r"]
	if isinstance(r, BaseException):
		raise r
	return r


def _try(fn):
	try:
		return fn()
	except BaseException as e:  # noqa: BLE001 - handed back to the caller
		return e


class ConnectorCases:
	mod = None
	atomic = True

	def database(self) -> str:  # pragma: no cover
		raise NotImplementedError

	def backend(self, board: str, create: bool = True):  # pragma: no cover
		raise NotImplementedError

	def setUp(self):
		self.url = self.database()
		self.boards = []
		self.addCleanup(lambda: [self.backend(b).drop() for b in self.boards] and None)
		self.addCleanup(self.mod.disconnect)
		self.s = self.store()

	def store(self, board: str | None = None, create: bool = True) -> Store:
		if board is None:
			board = "b" + uuid.uuid4().hex[:12]
		if board not in self.boards:
			self.boards.append(board)
			b = self.backend(board)
			if b.read_config()[0] is None:
				b.write_config(CFG)
		return Store(backend=self.backend(board, create=create))

	def test_a_ticket_lives_its_whole_life_in_the_database(self):
		s = self.s
		a = s.create({"title": "parent", "body": "why"}, prefix="T")
		s.update(a["id"], {"priority": "P1"})
		s.add_note(a["id"], "Omri", "a human note")
		kids = s.split(a["id"], ["one", "two"], sequential=True)
		self.assertEqual([k["id"] for k in kids["children"]], ["T-2", "T-3"])
		t = s.claim("w-1")
		self.assertEqual(t["id"], "T-2")
		s.heartbeat("T-2", "w-1", "working")
		kn.attach(s, "T-2", "log.txt", "line one")
		self.assertEqual(kn.read_file(s, "T-2", "log.txt"), "line one")
		self.assertEqual(s.get("T-2")["files"], ["log.txt"])
		s.complete("T-2", "w-1", "cause -> fix -> tests", files=["a.py"], handoff="read a.py")
		self.assertEqual(s.get("T-2")["status"], "done")
		self.assertEqual(s.get("T-3")["effective_status"], "todo")  # unblocked by T-2
		ctx = kn.context(s, "T-3")
		self.assertEqual([u["id"] for u in ctx["upstream"]], ["T-2"])
		events = [e["event"] for e in s.activity(limit=100)]
		for ev in ("created", "split", "claim", "complete", "note", "attach"):
			self.assertIn(ev, events)
		gone = s.delete("T-2")
		self.assertIn("T-3", gone["referenced_by"])
		self.assertIsNone(s.backend.get_attachment("T-2", "log.txt"))  # attachments go with the ticket
		self.assertEqual(sorted(t["id"] for t in s.list()), ["T-1", "T-3"])

	def test_a_batch_is_created_all_or_nothing(self):
		# create_many (CORE-46) came after the connector seam: on a database board too, "#N" refs resolve and a refused batch writes nothing
		s = self.s
		out = s.create_many([{"title": "one", "prefix": "B"}, {"title": "two", "prefix": "B", "blocked_by": ["#1"]}])
		self.assertEqual(out["ids"], {"#1": "B-1", "#2": "B-2"})
		self.assertEqual(s.get("B-2")["blocked_by"], ["B-1"])
		with self.assertRaises(TicketError):
			s.create_many([{"title": "three", "prefix": "B"}, {"title": "", "prefix": "B"}])
		self.assertEqual(sorted(t["id"] for t in s.list()), ["B-1", "B-2"])
		with self.assertRaises(TicketError):
			s.create_many([{"title": "again", "id": "B-1"}])  # an id that exists, asked through the connector

	def test_a_lapsed_claim_is_taken_back_by_the_same_worker(self):
		# the lease re-take (AI-35) reads the ticket through the connector, not a file
		s = self.s
		a = s.create({"title": "long run"}, prefix="L")
		s.claim("w-1")
		c = s.backend.claims()
		c[a["id"]]["heartbeat_at"] = "2020-01-01T00:00:00+00:00"
		s.backend.save_claims(c)
		self.assertEqual(s.get(a["id"])["status"], "todo")  # swept on read
		r = s.heartbeat(a["id"], "w-1", "still going")
		self.assertTrue(any("yours again" in w for w in r.get("warnings", [])), r)
		self.assertEqual(s.get(a["id"])["status"], "doing")
		self.assertEqual(s.get(a["id"])["claim"]["agent"], "w-1")

	def test_a_claim_reads_the_board_once(self):
		# CORE-61: the claim step reads every ticket once through the connector, under the lock; the reply patches the write in
		s = self.s
		for title in ("one", "two"):
			s.create({"title": title}, prefix="C")
		calls, real = [], s.backend.tickets
		with mock.patch.object(s.backend, "tickets", lambda: calls.append(1) or real()):
			got = s.claim_next("w-1")
			self.assertEqual((got["id"], got["status"], got["effective_status"], got["claim"]["agent"], len(calls)), ("C-1", "doing", "doing", "w-1", 1))
			calls.clear()
			s.claim_next("w-2")
			calls.clear()
			none = s.claim_next("w-3")
			self.assertEqual((none["ticket"], none["reason"], none["counts"]["in_flight"], len(calls)), (None, "waiting", 2, 1))
		self.assertEqual(self.store(self.boards[0]).get("C-1")["claim"]["agent"], "w-1")  # another Store reads the same

	def test_an_attachment_that_is_not_utf8_is_inlined_and_named(self):
		# CORE-48: bad bytes become U+FFFD with a warning; the bytes come from the connector, not a folder
		s = self.s
		a = s.create({"title": "log"}, prefix="U")
		s.backend.put_attachment(a["id"], "log.txt", "dash — here".encode("cp1252"))
		item = next(d for d in kn.context(s, a["id"])["docs"] if d.get("file") == "log.txt")
		self.assertIn("�", item["content"])
		self.assertIn("0x97", item["warning"])

	def test_everything_under_the_lock_commits_together_or_not_at_all(self):
		s = self.s
		t = s.create({"title": "t"}, prefix="T")

		def half():
			raw = s._fresh(t["id"])
			raw["title"] = "changed"
			s._write(raw)
			s._log("note", t["id"], "x", "half done")
			s.list()  # a read inside the transaction must not leave the uncommitted state in a cache
			raise RuntimeError("crash after the first write")
		with self.assertRaises(RuntimeError):
			s._locked(half)
		if not self.atomic:  # no transactions: the writes made before the crash stay, in order (safe write order)
			self.assertEqual(s.get(t["id"])["title"], "changed")
			self.assertIn("half done", [e["note"] for e in s.activity(limit=20)])
			return
		self.assertEqual(s.get(t["id"])["title"], "t")
		self.assertEqual(s.get(t["id"])["rev"], 1)
		self.assertNotIn("half done", [e["note"] for e in s.activity(limit=20)])
		self.assertEqual(in_thread(lambda: self.store(self.boards[0], create=False).get(t["id"])["title"]), "t")

	def test_a_write_from_another_connection_is_seen_on_the_next_read(self):
		s = self.s
		s.create({"title": "mine"}, prefix="T")
		self.assertEqual(len(s.list()), 1)  # fills this process's cache
		board = self.boards[0]
		in_thread(lambda: self.store(board, create=False).create({"title": "theirs"}, prefix="T"))
		in_thread(lambda: self.store(board, create=False).update("T-1", {"title": "mine, edited"}))
		self.assertEqual({t["id"]: t["title"] for t in s.list()}, {"T-1": "mine, edited", "T-2": "theirs"})
		in_thread(lambda: self.store(board, create=False).delete("T-2"))
		self.assertEqual([t["id"] for t in s.list()], ["T-1"])
		in_thread(lambda: self.store(board, create=False).claim("w-1"))
		self.assertEqual(s.get("T-1")["claim"]["agent"], "w-1")  # a claim alone (no ticket write) changes the board version too

	def test_many_boards_share_a_database_and_stay_apart(self):
		a, b = self.s, self.store()
		a.create({"title": "in a"}, prefix="T")
		b.create({"title": "in b"}, prefix="T")
		self.assertEqual([t["title"] for t in a.list()], ["in a"])
		self.assertEqual([t["title"] for t in b.list()], ["in b"])  # same id T-1, two boards
		a.claim("w-1")
		self.assertEqual(b.agents_now()["working"], [])
		self.assertTrue(set(self.boards) <= set(self.mod.boards(self.url)))
		self.backend(self.boards[1]).drop()
		self.assertNotIn(self.boards[1], self.mod.boards(self.url))
		self.assertEqual([t["title"] for t in a.list()], ["in a"])
		with self.assertRaises(TicketError):
			self.backend(self.boards[1], create=False)

	def test_events_are_read_after_a_cursor_in_order(self):
		s = self.s
		events, cur = s.backend.events_after(None)
		self.assertEqual(events, [])  # first sight: never replay history
		s.create({"title": "a"}, prefix="T")
		s.add_note("T-1", "Omri", "hi")
		events, cur2 = s.backend.events_after(cur)
		self.assertEqual([e["event"] for e in events], ["created", "note"])
		self.assertEqual(s.backend.events_after(cur2), ([], cur2))
		self.assertEqual([e["event"] for e in s.backend.events_newest_first()][:2], ["note", "created"])

	def _old(self, s, days, event, tid="T-1", agent="w-1"):
		"""An event stamped `days` ago, written straight to the connector (as if logged back then)."""
		ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
		s.backend.append_event({"ts": ts, "event": event, "ticket": tid, "agent": agent, "note": f"{days}d"}, None)

	def test_history_older_than_history_days_is_deleted_once_a_day(self):
		"""CORE-37: events past history_days go on the first write of the day; newer ones, the counts built on them
		(failing, attempts, activity since) and notify's cursor stay right."""
		s = self.s
		s.backend.write_config({**CFG, "history_days": 30})
		s = Store(backend=self.backend(self.boards[0]))
		s.create({"title": "a"}, prefix="T")
		for days, ev in ((400, "claim"), (400, "expired"), (90, "release"), (40, "rejected"),  # older than 30 days: go
		                 (5, "claim"), (5, "expired"), (4, "release"), (3, "rejected")):     # newer: stay
			self._old(s, days, ev)
		self.assertEqual(len(s.failing()["T-1"]), 6)
		_, cur = s.backend.events_after(None)
		store_mod._PRUNED.clear()
		s.backend.put_doc("history", {})
		s.create({"title": "trigger"}, prefix="U")  # the day's first write cleans, after its lock
		self.assertEqual([e["note"] for e in s.backend.events_newest_first()], ["trigger", "3d", "4d", "5d", "5d", "a"])
		self.assertEqual(s.backend.get_doc("history")["deleted"], 4)
		self.assertEqual([a["outcome"] for a in s.attempts("T-1")], ["rejected", "released", "expired"])
		self.assertEqual([a["note"] for a in s.failing()["T-1"]], ["3d", "4d", "5d"])  # only the kept attempts count
		events, cur2 = s.backend.events_after(cur)  # a cursor taken before the cleanup still reads exactly what came after it
		self.assertEqual([e["note"] for e in events], ["trigger"])
		since = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat(timespec="seconds")
		self.assertEqual(len(s.activity(limit=100, since=since)), 6)  # what the Agents stats read: every kept event
		self._old(s, 100, "expired")
		s.add_note("T-1", "Omri", "again")  # same day, same process: not again
		store_mod._PRUNED.clear()
		s.add_note("T-1", "Omri", "new process")  # same day, another process: the runtime doc says it already ran
		self.assertIn("100d", [e["note"] for e in s.backend.events_newest_first()])
		_, cur3 = s.backend.events_after(cur2)
		self.assertGreater(cur3, cur2)  # event numbers keep growing after a cleanup
		s.backend.append_event({"ts": "", "event": "note", "ticket": "T-1", "agent": "x", "note": "no ts"}, None)
		store_mod._PRUNED.clear()
		s.backend.put_doc("history", {})
		s.add_note("T-1", "Omri", "next day")
		got = [e["note"] for e in s.backend.events_newest_first()]
		self.assertNotIn("100d", got)
		self.assertIn("no ts", got)  # unknown age: kept
		s.backend.write_config({**CFG, "history_days": None})  # null = keep forever
		s = Store(backend=self.backend(self.boards[0]))
		self._old(s, 100, "expired")
		store_mod._PRUNED.clear()
		s.backend.put_doc("history", {})
		s.add_note("T-1", "Omri", "kept")
		self.assertIn("100d", [e["note"] for e in s.backend.events_newest_first()])

	def test_a_failed_cleanup_never_fails_the_write(self):
		s = self.s
		store_mod._PRUNED.clear()
		s.backend.put_doc("history", {})
		with mock.patch.object(type(s.backend), "prune_events", side_effect=RuntimeError("disk full")), 		     mock.patch("sys.stderr", new_callable=io.StringIO) as err:
			t = s.create({"title": "a"}, prefix="T")
		self.assertEqual(s.get(t["id"])["title"], "a")
		self.assertIn("history cleanup", err.getvalue())

	def test_settings_live_in_the_database(self):
		s = self.s
		self.assertEqual(s.config["owners"][0]["name"], "Omri")
		s.backend.write_config({**CFG, "title": "Payments"})
		self.assertEqual(Store(backend=self.backend(self.boards[0])).config["title"], "Payments")
		s.backend.write_config({"nope": 1})
		with self.assertRaises(TicketError) as e:
			Store(backend=self.backend(self.boards[0]))
		self.assertIn(f"board {self.boards[0]} settings", str(e.exception))

	def test_a_waiting_writer_gives_up_with_try_again(self):
		s, board = self.s, self.boards[0]
		s.create({"title": "t"}, prefix="T")
		held, done = threading.Event(), threading.Event()

		def hold():
			def inside():
				held.set()
				done.wait(10)
			self.store(board, create=False)._locked(inside)
		th = threading.Thread(target=hold)
		th.start()
		held.wait(10)
		try:
			with mock.patch.object(self.mod, "BUSY_S", 1):
				t0 = time.monotonic()
				with self.assertRaises(TicketError) as e:
					in_thread(lambda: self.store(board, create=False).update("T-1", {"title": "late"}))
				self.assertIn("try again", str(e.exception))
				self.assertLess(time.monotonic() - t0, 8)
		finally:
			done.set()
			th.join(10)
		self.assertEqual(s.update("T-1", {"title": "now"})["title"], "now")  # the lock is free again

	def test_repo_features_say_there_is_no_repo(self):
		s = self.s
		t = s.create({"title": "t", "docs": ["README.md"]}, prefix="T")
		ctx = kn.context(s, t["id"])
		self.assertIn("no repo", ctx["docs"][0]["error"])
		self.assertEqual(kn.repo_files(s), {"files": [], "truncated": False})
		self.assertIsNone(kn.architecture(s)["folder"])
		with self.assertRaises(TicketError):
			s.update(t["id"], {"components": ["web"]})
		self.assertTrue(kn.graph(s)["broken"])
		u = s.create({"title": "u", "touches": ["unblockd/web.py"]}, prefix="T")  # UI-66: no repo, so no doc to suggest, and no error
		self.assertEqual(kn.graph(s)["suggest"], {})
		self.assertEqual(kn.suggest(s, [u["id"]], [u], [], kn.BUDGET), {})
