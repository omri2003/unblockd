"""update_many: several tickets changed in one locked, all-or-nothing step with update()'s checks per item (Store, REST, CLI, Remote)."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import UPDATE_MAX, Conflict, Store, TicketError


def store(tmp, **cfg):
	d = Path(tmp) / "p" / "tickets"
	d.mkdir(parents=True, exist_ok=True)
	(d / "unblockd.json").write_text(json.dumps(cfg))
	return Store(d)


class UpdateManyTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.s = store(self.tmp.name, owners=[{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
		               agents=[{"name": "backend", "tags": ["be"]}], milestones=[{"key": "v1", "title": "One"}])
		for i in range(1, 5):
			self.s.create({"title": f"t{i}", "owner": "Claude"}, tid=f"A-{i}")

	def tearDown(self):
		self.tmp.cleanup()

	def files(self):
		return {p.name: p.read_bytes() for p in self.s.dir.glob("*.json")}

	def test_changes_land_together(self):
		out = self.s.update_many([{"id": "A-1", "changes": {"priority": "P1", "tags": ["x"]}}, {"id": "A-2", "changes": {"milestone": "v1", "parent": "A-1"}},
		                          {"id": "A-3", "changes": {"status": "done"}}])
		self.assertEqual([t["id"] for t in out["tickets"]], ["A-1", "A-2", "A-3"])
		self.assertEqual((self.s.get("A-1")["priority"], self.s.get("A-2")["milestone"], self.s.get("A-2")["parent"]), ("P1", "v1", "A-1"))
		self.assertEqual(self.s.get("A-1")["children"], ["A-2"])
		self.assertTrue(self.s.get("A-3")["done_at"])
		self.assertEqual([(e["event"], e["ticket"]) for e in self.s.activity(limit=5) if e["event"] != "created"], [("done", "A-3")])

	def test_one_bad_item_changes_nothing_and_every_problem_is_named(self):
		before = self.files()
		with self.assertRaises(TicketError) as e:
			self.s.update_many([{"id": "A-1", "changes": {"priority": "P1"}}, {"id": "A-2", "changes": {"priority": "P9"}},
			                    {"id": "NOPE-1", "changes": {"title": "x"}}, {"id": "A-3", "changes": {"blocked_by": ["ZZ-1"]}},
			                    {"id": "A-1", "changes": {"title": "again"}}, "junk", {"id": "A-4", "changes": {}, "colour": 1}])
		msg = str(e.exception)
		for part in ("nothing was changed", "#2 A-2: invalid priority", "#3 NOPE-1: no ticket NOPE-1", "#4 A-3: no ticket ZZ-1",
		             "#5 A-1: A-1 is given twice", "#6: each item must be an object", "#7 A-4: unknown key(s) colour"):
			self.assertIn(part, msg)
		self.assertNotIn("#1 ", msg)
		self.assertEqual(self.files(), before)
		with self.assertRaisesRegex(TicketError, "non-empty list"):
			self.s.update_many([])
		with self.assertRaisesRegex(TicketError, f"at most {UPDATE_MAX}"):
			self.s.update_many([{"id": "A-1", "changes": {}}] * (UPDATE_MAX + 1))

	def test_the_batch_counts_its_own_changes(self):
		with self.assertRaisesRegex(TicketError, "#2 A-2: blocked_by makes a loop"):
			self.s.update_many([{"id": "A-1", "changes": {"blocked_by": ["A-2"]}}, {"id": "A-2", "changes": {"blocked_by": ["A-1"]}}])
		with self.assertRaisesRegex(TicketError, "parent makes a loop"):
			self.s.update_many([{"id": "A-1", "changes": {"parent": "A-2"}}, {"id": "A-2", "changes": {"parent": "A-1"}}])
		self.s.update("A-2", {"blocked_by": ["A-1"]})
		with self.assertRaisesRegex(TicketError, "A-2 is blocked by A-1"):
			self.s.update_many([{"id": "A-2", "changes": {"status": "doing"}}])
		self.s.update_many([{"id": "A-1", "changes": {"status": "done"}}, {"id": "A-2", "changes": {"status": "doing"}}])  # its blocker is done in the same batch
		self.assertEqual(self.s.get("A-2")["status"], "doing")

	def test_expected_rev_is_a_conflict(self):
		rev = self.s.get("A-1")["rev"]
		self.s.update("A-1", {"priority": "P2"})
		with self.assertRaises(Conflict) as e:
			self.s.update_many([{"id": "A-1", "changes": {"priority": "P0"}, "expected_rev": rev}, {"id": "A-2", "changes": {"priority": "P0"}}])
		self.assertIn("#1 A-1: A-1 changed since you loaded it", str(e.exception))
		self.assertEqual(self.s.get("A-2")["priority"], None)
		self.s.update_many([{"id": "A-1", "changes": {"priority": "P0"}, "expected_rev": rev + 1}])
		self.assertEqual(self.s.get("A-1")["priority"], "P0")

	def test_holder_rule_and_worker_guardrails(self):
		self.s.claim("backend-1", tid="A-1")
		with self.assertRaisesRegex(TicketError, "#1 A-1: A-1 is claimed by backend-1"):
			self.s.update_many([{"id": "A-1", "changes": {"priority": "P1"}}], agent="backend-2")
		with self.assertRaisesRegex(TicketError, "#1 A-2: a worker can't set status done"):
			self.s.update_many([{"id": "A-2", "changes": {"status": "done"}}], agent="backend-1")
		out = self.s.update_many([{"id": "A-1", "changes": {"priority": "P1"}}], agent="backend-1")  # the holder: allowed, lease reported
		self.assertIn("lease_left_s", out["tickets"][0])
		self.s.update_many([{"id": "A-1", "changes": {"status": "todo"}}])  # a human moves it off doing: the claim ends
		self.assertIsNone(self.s.get("A-1")["claim"])


class FrontDoorTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.s = store(self.tmp.name, milestones=[{"key": "v1", "title": "One"}])
		for i in range(1, 4):
			self.s.create({"title": f"t{i}"}, tid=f"A-{i}")
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def call(self, body):
		req = urllib.request.Request(self.base + "/api/tickets", method="PATCH", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
		try:
			with urllib.request.urlopen(req) as r:
				return r.status, json.loads(r.read())
		except urllib.error.HTTPError as e:
			return e.code, json.loads(e.read())

	def cli(self, *a, stdin=""):
		out, old = io.StringIO(), sys.stdin
		sys.stdin = io.StringIO(stdin)
		try:
			with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
				code = cli.main(["--dir", str(self.s.dir), *a])
		finally:
			sys.stdin = old
		return code, json.loads(out.getvalue()) if code == 0 else None

	def test_rest(self):
		code, out = self.call({"tickets": [{"id": "A-1", "changes": {"priority": "P1"}}, {"id": "A-2", "changes": {"priority": "P2"}}]})
		self.assertEqual((code, [t["priority"] for t in out["tickets"]]), (200, ["P1", "P2"]))
		code, out = self.call({"tickets": [{"id": "A-1", "changes": {"priority": "P3"}}, {"id": "A-2", "changes": {"priority": "nope"}}]})
		self.assertEqual(code, 400)
		self.assertIn("#2 A-2: invalid priority", out["error"])
		self.assertEqual(self.s.get("A-1")["priority"], "P1")
		code, out = self.call({"tickets": [{"id": "A-1", "changes": {"priority": "P3"}, "expected_rev": 1}]})
		self.assertEqual(code, 409)
		self.assertEqual(self.call({})[0], 400)

	def test_remote_and_cli(self):
		r = cli.Remote(self.base)
		self.assertEqual(r.update_many([{"id": "A-3", "changes": {"tags": ["x"]}}])["tickets"][0]["tags"], ["x"])
		with self.assertRaisesRegex(TicketError, "#1 A-9: no ticket A-9"):
			r.update_many([{"id": "A-9", "changes": {"tags": ["x"]}}])
		f = Path(self.tmp.name) / "changes.json"
		f.write_text(json.dumps([{"id": "A-1", "changes": {"milestone": "v1"}}, {"id": "A-2", "changes": {"milestone": "v1"}}]))
		code, out = self.cli("update-many", "--file", str(f))
		self.assertEqual((code, [t["milestone"] for t in out["tickets"]]), (0, ["v1", "v1"]))
		code, _ = self.cli("update-many", "--file", "-", stdin=json.dumps([{"id": "A-1", "changes": {"milestone": "zz"}}]))
		self.assertEqual(code, 1)

	def test_migrate_milestones_is_one_batch(self):
		self.s.update("A-2", {"parent": "A-1"})
		self.s.update("A-3", {"parent": "A-1"})
		code, out = self.cli("migrate-milestones", "A-1=v1")
		self.assertEqual((code, out), (0, {"A-1": ["A-2", "A-3"]}))
		self.assertEqual([(self.s.get(x)["milestone"], self.s.get(x)["parent"]) for x in ("A-2", "A-3")], [("v1", None)] * 2)
		self.s.update("A-2", {"parent": "A-1"})
		self.assertEqual(self.cli("migrate-milestones", "A-1=ghost")[0], 1)  # refused: nothing moved
		self.assertEqual(self.s.get("A-2")["parent"], "A-1")


if __name__ == "__main__":
	unittest.main()
