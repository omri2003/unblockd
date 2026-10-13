"""Webhook notifications against a fake receiver (no network)."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import notify
from unblockd.boards import Boards


class Receiver(BaseHTTPRequestHandler):
	got: list = []

	def do_POST(self):
		Receiver.got.append((self.headers["Content-Type"], self.rfile.read(int(self.headers["Content-Length"])).decode()))
		self.send_response(200)
		self.send_header("Content-Length", "0")
		self.end_headers()

	def log_message(self, *a):
		pass


class NotifyTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "game" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({"title": "game"}), encoding="utf-8")
		self.boards = Boards([self.tmp.name])
		self.s = self.boards.get("game")
		self.s.create({"title": "pre-existing", "owner": "Claude"}, tid="BUG-1")
		self.s.claim("w-1")
		self.s.complete("BUG-1", "w-1", "old news")  # history: must never be sent
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.url = f"http://127.0.0.1:{self.srv.server_port}/hook"
		Receiver.got = []

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def notifier(self, **kw):
		n = notify.Notifier(self.boards, self.url, **kw)
		n.poll()  # prime: start at end of file
		self.assertEqual(Receiver.got, [])
		return n

	def work(self, tid, agent="w-1", summary="did it"):
		self.s.create({"title": f"job {tid}", "owner": "Claude"}, tid=tid)
		self.s.claim(agent, tid=tid)
		self.s.complete(tid, agent, summary + "\nsecond line")

	def msgs(self):
		return [json.loads(b)["text"] for _, b in Receiver.got]

	def test_new_lines_only_and_complete_message(self):
		n = self.notifier()
		self.s.create({"title": "other", "owner": "Claude"}, tid="BUG-9")  # keeps the board busy: no ran-dry line
		self.work("BUG-2")
		n.poll()
		self.assertEqual(self.msgs(), ["✅ game: BUG-2 job BUG-2 finished by w-1 — did it"])
		n.poll()
		self.assertEqual(len(Receiver.got), 1)  # nothing new, nothing sent

	def test_stuck_messages_and_json_payload(self):
		n = self.notifier()
		self.s.create({"title": "a", "owner": "Claude"}, tid="BUG-2")
		self.s.claim("w-2", tid="BUG-2")
		self.s.release("BUG-2", "w-2", "tests flaky")
		self.s._log("expired", "BUG-2", "w-3", "no heartbeat for 10 min; back to todo")
		n.poll()
		ctype, body = Receiver.got[0]
		self.assertEqual(ctype, "application/json")
		p = json.loads(body)
		self.assertEqual(p["text"], p["content"])
		self.assertEqual(p["board"], "game")
		self.assertEqual(p["text"].splitlines(), ["⚠ game: BUG-2 stuck — w-2 tests flaky",
		                                          "⚠ game: BUG-2 stuck — w-3 no heartbeat for 10 min; back to todo"])
		self.assertEqual([e["event"] for e in p["events"]], ["release", "expired"])

	def test_question_line(self):
		n = self.notifier()
		self.s.create({"title": "keep busy", "owner": "Claude"}, tid="BUG-99")
		self.s.create({"title": "a", "owner": "Claude"}, tid="BUG-2")
		self.s.claim("w-2", tid="BUG-2")
		self.s.ask("BUG-2", "w-2", "Omri: ship or wait?")
		n.poll()
		self.assertEqual(self.msgs(), ["❓ game: BUG-2 a needs a human — w-2 asks: Omri: ship or wait?"])

	def test_answered_and_rejected_lines(self):
		n = self.notifier()
		self.s.create({"title": "keep busy", "owner": "Claude"}, tid="BUG-99")
		self.s.create({"title": "a", "owner": "Claude"}, tid="BUG-2")
		self.s.claim("w-2", tid="BUG-2")
		self.s.ask("BUG-2", "w-2", "ship?")
		n.poll()
		Receiver.got = []
		self.s.answer("BUG-2", "Omri", "ship it")
		self.s._log("rejected", "BUG-2", "Omri", "tests missing")
		n.poll()
		self.assertEqual(self.msgs()[0].splitlines(), ["💬 game: BUG-2 a answered by Omri — ship it", "↩ game: BUG-2 a rejected by Omri — tests missing"])

	def test_approved_line(self):
		n = self.notifier()
		self.s.create({"title": "keep busy", "owner": "Claude"}, tid="BUG-99")
		self.s.create({"title": "a", "owner": "Claude"}, tid="BUG-2")
		self.s.update("BUG-2", {"status": "review"})
		n.poll()
		Receiver.got = []
		self.s.approve("BUG-2", "Omri")
		n.poll()
		self.assertEqual(self.msgs(), ["✅ game: BUG-2 a approved by Omri"])

	def test_batching(self):
		n = self.notifier()
		self.s.create({"title": "keep busy", "owner": "Claude"}, tid="BUG-99")
		for i in range(12):
			self.s._log("release", f"BUG-{i}", "w", f"r{i}")
		n.poll()
		self.assertEqual(len(Receiver.got), 1)
		lines = self.msgs()[0].splitlines()
		self.assertEqual(len(lines), 11)
		self.assertEqual(lines[-1], "+2 more")

	def test_ran_dry_once_and_rearmed(self):
		n = self.notifier()
		self.work("BUG-2")
		n.poll()
		self.assertEqual(self.msgs()[-1].splitlines()[-1], "🏁 game ran dry: no ready tickets and no agent working")
		n.poll()
		n.poll()
		self.assertEqual(len(Receiver.got), 1)
		self.s.create({"title": "more", "owner": "Claude"}, tid="BUG-3")
		n.poll()  # work appears: re-armed, nothing sent
		self.assertEqual(len(Receiver.got), 1)
		self.s.claim("w-1", tid="BUG-3")
		self.s.complete("BUG-3", "w-1", "ok")
		n.poll()
		self.assertEqual(len(Receiver.got), 2)
		self.assertIn("ran dry", self.msgs()[-1])

	def test_dry_board_without_activity_is_quiet(self):
		n = self.notifier()
		n.poll()
		self.assertEqual(Receiver.got, [])

	def test_text_format_and_public_url(self):
		n = self.notifier(fmt="text", public_url="https://b.example/")
		self.s.create({"title": "other", "owner": "Claude"}, tid="BUG-9")
		self.work("BUG-2")
		n.poll()
		ctype, body = Receiver.got[0]
		self.assertTrue(ctype.startswith("text/plain"))
		self.assertEqual(body, "✅ game: BUG-2 job BUG-2 finished by w-1 — did it\nhttps://b.example/?board=game")

	def test_truncated_log_resets_offset(self):
		n = self.notifier()
		self.s.create({"title": "other", "owner": "Claude"}, tid="BUG-9")
		self.s._log("release", "BUG-2", "w", "before")
		n.poll()
		p = Path(self.tmp.name) / "game" / "tickets" / ".unblockd" / "activity.jsonl"
		p.write_text("", encoding="utf-8")
		n.poll()
		self.s._log("release", "BUG-2", "w", "after")
		n.poll()
		self.assertTrue(self.msgs()[-1].endswith("after"))

	def test_rotated_log_is_read_from_the_start(self):
		n = self.notifier()
		self.s.create({"title": "other", "owner": "Claude"}, tid="BUG-9")
		for i in range(3):
			self.s._log("release", "BUG-2", "w", f"old{i}")
		n.poll()
		self.assertEqual(len(self.msgs()), 1)
		with mock.patch("unblockd.store.ACTIVITY_MAX", 100):
			self.s._log("release", "BUG-2", "w", "new")  # rotates: activity.jsonl is now just this line
		self.assertTrue((Path(self.tmp.name) / "game" / "tickets" / ".unblockd" / "activity.1.jsonl").exists())
		n.poll()
		self.assertEqual(len(Receiver.got), 2)
		self.assertTrue(self.msgs()[-1].endswith("new") and "old" not in self.msgs()[-1])

	def test_events_before_rotation_are_not_lost(self):
		n = self.notifier()
		self.s.create({"title": "other", "owner": "Claude"}, tid="BUG-9")
		self.s._log("release", "BUG-2", "w", "seen")
		n.poll()
		self.s._log("release", "BUG-2", "w", "unpolled")  # written after the last poll, then the file rotates
		with mock.patch("unblockd.store.ACTIVITY_MAX", 100):
			self.s._log("release", "BUG-2", "w", "new")
		n.poll()
		self.assertEqual(self.msgs()[-1].splitlines()[-2:], ["⚠ game: BUG-2 stuck — w unpolled", "⚠ game: BUG-2 stuck — w new"])
		self.assertEqual(len(Receiver.got), 2)
		self.assertNotIn("seen", self.msgs()[-1])

	def test_receiver_down_does_not_raise(self):
		n = notify.Notifier(self.boards, "http://127.0.0.1:1/hook")
		n.poll()
		self.s._log("release", "BUG-2", "w", "x")
		with mock.patch("builtins.print") as pr:
			n.poll()
			self.s._log("release", "BUG-2", "w", "y")
			n.poll()
		self.assertTrue(n.failing)
		self.assertEqual(pr.call_count, 1)  # printed once per failure streak

	def test_unset_env_starts_nothing(self):
		with mock.patch.dict("os.environ", {}, clear=False) as env, mock.patch("threading.Thread") as th:
			env.pop("UNBLOCKD_WEBHOOK", None)
			self.assertIsNone(notify.start(self.boards))
			th.assert_not_called()


if __name__ == "__main__":
	unittest.main()
