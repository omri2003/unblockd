"""AI-60: complete(followup=) finishes the done part and hands the rest on as its own ticket; release hints at it."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from unblockd import cli
from unblockd.store import Store, TicketError


class FollowupTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "tickets"
		self.s = Store(self.dir)
		self.s.create({"title": "epic"}, tid="UI-1")
		self.s.create({"title": "store side"}, tid="CORE-68")
		self.s.create({"title": "summary first, then the mark", "parent": "UI-1", "pillar": "UI", "tags": ["css"],
		               "touches": ["static/index.html"], "priority": "P1"}, tid="UI-79")
		self.s.claim("w-1", tid="UI-79")

	def tearDown(self):
		self.tmp.cleanup()

	def test_complete_hands_the_rest_on(self):
		got = self.s.complete("UI-79", "w-1", "summary first done", followup={
			"title": "UI-79 part 2: Finished without a summary", "done_when": ["the mark shows"], "blocked_by": ["CORE-68"]})
		self.assertEqual(got["status"], "done")
		f = got["followup"]
		self.assertEqual((f["id"], f["effective_status"], f["open_blockers"]), ("UI-80", "pending", ["CORE-68"]))
		self.assertEqual(got["result"]["followup"], "UI-80")
		t = self.s.get("UI-80")
		self.assertEqual((t["parent"], t["pillar"], t["tags"], t["touches"], t["priority"], t["done_when"]),
		                 ("UI-1", "UI", ["css"], ["static/index.html"], "P1", ["the mark shows"]))
		self.assertTrue(t["body"].startswith("Rest of UI-79."))
		self.assertEqual(self.s.activity("UI-80", limit=1)[0]["event"], "created")
		self.s.claim("w-2", tid="CORE-68")
		self.s.complete("CORE-68", "w-2", "built")
		self.assertEqual(self.s.get("UI-80")["effective_status"], "todo")  # ready by itself, no human step
		self.assertEqual(self.s.claim("w-3")["id"], "UI-80")

	def test_a_bad_followup_completes_nothing(self):
		for f, msg in (({"title": "x", "blocked_by": ["NOPE-1"]}, "NOPE-1"), ({"blocked_by": ["CORE-68"]}, "needs a title"),
		               ({"title": "x", "owner": "someone"}, "not owner"), ("rest", "needs a title")):
			with self.assertRaisesRegex(TicketError, msg):
				self.s.complete("UI-79", "w-1", "half", followup=f)
		t = self.s.get("UI-79")
		self.assertEqual((t["status"], t["claim"]["agent"], t["result"], t["notes"]), ("doing", "w-1", None, []))
		self.assertFalse(self.s._exists("UI-80"))

	def test_body_and_overrides(self):
		got = self.s.complete("UI-79", "w-1", "half", followup={"title": "rest", "body": "Needs the CORE-68 field.", "touches": ["a.py"], "priority": "P3"})
		t = self.s.get(got["followup"]["id"])
		self.assertEqual((t["body"], t["touches"], t["priority"], t["effective_status"]), ("Rest of UI-79.\n\nNeeds the CORE-68 field.", ["a.py"], "P3", "todo"))

	def test_release_naming_a_ticket_hints_at_followup(self):
		got = self.s.release("UI-79", "w-1", "the second half waits on CORE-68")
		self.assertIn("followup=", got["hint"])
		self.assertIn("CORE-68", got["hint"])
		self.s.claim("w-1", tid="UI-79")
		self.assertNotIn("hint", self.s.release("UI-79", "w-1", "out of time (NOPE-9 is not a ticket)"))

	def test_cli_followup(self):
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			code = cli.main(["--dir", str(self.dir), "complete", "UI-79", "--agent", "w-1", "--summary", "half",
			                 "--followup", json.dumps({"title": "rest", "blocked_by": ["CORE-68"]})])
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(out.getvalue())["followup"]["id"], "UI-80")


if __name__ == "__main__":
	unittest.main()
