"""Milestones: the roadmap as an ordered list in unblockd.json, tickets point at one with `milestone`, progress is derived."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import Store, TicketError

MS = [{"key": "v0.1", "title": "Foundations", "goal": "it runs", "exit_criteria": ["boots"], "target": "2026-11-15"},
      {"key": "v0.2", "title": "Sync"}, {"key": "v0.3", "title": "Later"}]


def board(tmp, **cfg):
	d = Path(tmp) / "p" / "tickets"
	d.mkdir(parents=True, exist_ok=True)
	(d / "unblockd.json").write_text(json.dumps({"milestones": MS, **cfg}))
	return Store(d)


class MilestoneTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.s = board(self.tmp.name)

	def tearDown(self):
		self.tmp.cleanup()

	def test_config_is_validated(self):
		for bad, word in ([{"key": "v 1", "title": "x"}], "key"), ([{"key": "a", "title": "x"}, {"key": "a", "title": "y"}], "twice"), \
		                 ([{"key": "a"}], "title"), ([{"key": "a", "title": "x", "due": "z"}], "unknown"), \
		                 ([{"key": "a", "title": "x", "target": "soon"}], "date"), ({"a": 1}, "list"):
			with self.assertRaises(TicketError) as e:
				board(self.tmp.name, milestones=bad)
			self.assertIn(word, str(e.exception))

	def test_field_checked_against_the_config(self):
		t = self.s.create({"title": "a", "milestone": "v0.1", "parent": None}, tid="A-1")
		self.assertEqual(t["milestone"], "v0.1")
		with self.assertRaises(TicketError) as e:
			self.s.create({"title": "b", "milestone": "v9"}, tid="A-2")
		self.assertIn("v0.1, v0.2, v0.3", str(e.exception))
		self.assertIsNone(self.s.update("A-1", {"milestone": None})["milestone"])
		self.assertIsNone(self.s.create({"title": "c"}, tid="A-3")["milestone"])

	def test_no_milestones_says_how_to_add_them(self):
		s = board(tempfile.mkdtemp(dir=self.tmp.name), milestones=[])
		with self.assertRaises(TicketError) as e:
			s.create({"title": "a", "milestone": "v0.1"}, tid="A-1")
		self.assertIn("no milestones", str(e.exception))

	def test_a_removed_milestone_keeps_the_ticket_editable(self):
		self.s.create({"title": "a", "milestone": "v0.3"}, tid="A-1")
		self.s = board(self.tmp.name, milestones=MS[:2])
		self.assertEqual(self.s.update("A-1", {"title": "b"})["milestone"], "v0.3")
		self.assertEqual(self.s.roadmap()["unscheduled"]["total"], 1)  # no longer listed = unscheduled

	def test_roadmap_counts_order_and_next(self):
		self.s.create({"title": "done", "milestone": "v0.1", "status": "done"}, tid="A-1")
		self.s.create({"title": "ready", "milestone": "v0.1"}, tid="A-2")
		self.s.create({"title": "blocked", "milestone": "v0.1", "blocked_by": ["A-2"]}, tid="A-3")
		self.s.create({"title": "asks", "milestone": "v0.2", "waiting_on": "which db?"}, tid="A-4")
		self.s.create({"title": "loose"}, tid="A-5")
		r = self.s.roadmap()
		self.assertEqual([m["key"] for m in r["milestones"]], ["v0.1", "v0.2", "v0.3"])
		m1 = r["milestones"][0]
		self.assertEqual((m1["next"], m1["exit_criteria"], m1["target"]), (True, ["boots"], "2026-11-15"))
		self.assertEqual({k: v for k, v in m1["counts"].items() if v}, {"total": 3, "done": 1, "ready": 1, "blocked": 1})
		self.assertEqual(r["milestones"][1]["counts"]["waiting_on_human"], 1)
		self.assertEqual((r["next"], r["unscheduled"]["total"]), ("v0.1", 1))
		self.assertEqual([t["id"] for t in self.s.list(milestone="v0.1")], ["A-1", "A-2", "A-3"])
		self.assertEqual([t["id"] for t in self.s.list(milestone="-")], ["A-5"])

	def test_closing_moves_next_and_all_done_is_a_cue(self):
		self.s.create({"title": "a", "milestone": "v0.1", "status": "done"}, tid="A-1")
		self.assertTrue(self.s.roadmap()["milestones"][0]["all_done"])
		self.assertEqual(self.s.roadmap()["next"], "v0.1")  # a human closes it, not the last ticket
		s = board(self.tmp.name, milestones=[{**MS[0], "closed": True}, *MS[1:]])
		self.assertEqual(s.roadmap()["next"], "v0.2")

	def test_next_ready_prefers_the_current_milestone_only_on_a_tie(self):
		self.s.create({"title": "later", "milestone": "v0.2", "priority": "P2"}, tid="A-1")
		self.s.create({"title": "now", "milestone": "v0.1", "priority": "P2"}, tid="A-2")
		self.s.create({"title": "urgent later", "milestone": "v0.3", "priority": "P0"}, tid="A-3")
		self.assertEqual([t["id"] for t in self.s.next_ready()], ["A-3", "A-2", "A-1"])
		self.assertEqual(self.s.claim("w")["id"], "A-3")

	def test_split_children_keep_the_milestone(self):
		self.s.create({"title": "big", "milestone": "v0.2"}, tid="A-1")
		kids = self.s.split("A-1", ["x", "y"])["children"]
		self.assertEqual([k["milestone"] for k in kids], ["v0.2", "v0.2"])

	def test_cli_migrate_from_version_epics(self):
		self.s.create({"title": "Version 0.1"}, tid="REL-1")
		self.s.create({"title": "Epic"}, tid="EP-1")
		self.s.create({"title": "a", "parent": "REL-1"}, tid="A-1")
		self.s.create({"title": "b", "parent": "EP-1"}, tid="A-2")
		def call(*a):
			out = io.StringIO()
			with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
				code = cli.main(["--dir", str(self.s.dir), *a])
			return code, json.loads(out.getvalue()) if code == 0 else None
		self.assertEqual(call("migrate-milestones", "REL-1=v0.1"), (0, {"REL-1": ["A-1"]}))
		a1 = self.s.get("A-1")
		self.assertEqual((a1["milestone"], a1["parent"]), ("v0.1", None))
		self.assertEqual(self.s.get("A-2")["parent"], "EP-1")
		self.assertEqual(call("migrate-milestones", "REL-1")[0], 1)
		self.assertEqual(call("update", "A-2", "--milestone", "v0.2")[1]["milestone"], "v0.2")
		self.assertIsNone(call("update", "A-2", "--milestone", "-")[1]["milestone"])
		self.assertEqual(call("roadmap")[1]["next"], "v0.1")
		self.assertEqual([t["id"] for t in call("list", "--milestone", "v0.1")[1]], ["A-1"])

	def test_rest_roadmap_and_filter(self):
		from unblockd import web
		self.s.create({"title": "a", "milestone": "v0.2"}, tid="A-1")
		web.BOARDS = Boards([self.tmp.name])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		try:
			get = lambda path: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api{path}").read())
			self.assertEqual(get("/roadmap")["milestones"][1]["counts"]["ready"], 1)
			self.assertEqual([t["id"] for t in get("/tickets?milestone=v0.2")], ["A-1"])
			self.assertEqual(get("/tickets?milestone=v0.1"), [])
		finally:
			srv.shutdown()
			srv.server_close()


if __name__ == "__main__":
	unittest.main()
