"""Behaviour every database connector shares beyond the race contract (tests/contract.py): a whole ticket life, all-or-nothing
under the lock, caches that stay true, many boards in one database, events after a cursor, settings in the database, lock
waits and the no-repo messages. Not a test module itself: tests/test_sql.py and tests/test_mongo.py bind it.

A binding sets `mod` (the connector module: SqlBackend / MongoBackend, boards(), disconnect(), BUSY_S), `atomic` (does a
failure under the lock roll everything back?) and implements database() and backend(board, create).
"""
import threading
import time
import uuid
from unittest import mock

from unblockd import knowledge as kn
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
