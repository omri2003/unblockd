"""CORE-68: a ticket moved to done without complete(): a warning, who moved it, the derived `unreported` flag (only after the cut-off),
and a person adding the missing summary (report) through the store, REST, Remote and the CLI."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import cli
from unblockd import store as stm
from unblockd.boards import Boards
from unblockd.store import Store, TicketError


def board(root: Path, cfg: dict) -> Path:
	d = root / "p" / "tickets"
	d.mkdir(parents=True)
	(d / "unblockd.json").write_text(json.dumps({"id": "p", **cfg}), encoding="utf-8")
	return d


class ReportStore(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = board(Path(self.tmp.name), {"agents": [{"name": "backend", "tags": ["api"]}]})
		self.s = Store(self.dir)

	def tearDown(self):
		self.tmp.cleanup()

	def test_update_to_done_without_result_warns_and_names_who(self):
		t = self.s.create({"title": "a"}, prefix="A")
		got = self.s.update(t["id"], {"status": "done"}, by="omri")
		self.assertTrue(any("complete()" in w for w in got["warnings"]), got.get("warnings"))
		self.assertTrue(got["unreported"])
		ev = self.s.activity(t["id"], limit=1)[0]
		self.assertEqual((ev["event"], ev["agent"]), ("done", "omri"))
		t2 = self.s.create({"title": "b"}, prefix="A")
		self.s.update(t2["id"], {"status": "done"})  # no name at all = a human
		self.assertEqual(self.s.activity(t2["id"], limit=1)[0]["agent"], "human")
		self.assertNotIn("warnings", self.s.update(t2["id"], {"priority": "P1"}))  # only the move to done warns

	def test_create_done_warns(self):
		got = self.s.create({"title": "c", "status": "done"}, prefix="A")
		self.assertTrue(any("complete()" in w for w in got["warnings"]))
		self.assertTrue(self.s.get(got["id"])["unreported"])
		self.assertNotIn("warnings", self.s.create({"title": "d", "tags": ["api"]}, prefix="A"))  # routable: no CORE-82 warning either

	def test_complete_is_not_unreported_and_does_not_warn(self):
		t = self.s.create({"title": "e", "tags": ["api"]}, prefix="A")
		self.s.claim("backend-1", tid=t["id"])
		got = self.s.complete(t["id"], "backend-1", "cause -> fix -> tests")
		self.assertNotIn("unreported", got)
		self.assertNotIn("warnings", got)

	def test_cut_off_leaves_old_done_tickets_alone(self):
		t = self.s.create({"title": "old"}, prefix="A")
		self.s.update(t["id"], {"status": "done"})
		raw = json.loads((self.dir / f"{t['id']}.json").read_text(encoding="utf-8"))
		raw["done_at"] = "2026-10-01T10:00:00+00:00"  # done before the flag shipped
		(self.dir / f"{t['id']}.json").write_text(json.dumps(raw), encoding="utf-8")
		self.assertNotIn("unreported", self.s.get(t["id"]))
		raw["done_at"] = ""  # an old file with no done_at at all
		(self.dir / f"{t['id']}.json").write_text(json.dumps(raw), encoding="utf-8")
		self.assertNotIn("unreported", self.s.get(t["id"]))
		with mock.patch.object(stm, "UNREPORTED_SINCE", "2026-09-01"):  # the cut-off is the one constant
			raw["done_at"] = "2026-10-01T10:00:00+00:00"
			(self.dir / f"{t['id']}.json").write_text(json.dumps(raw), encoding="utf-8")
			self.assertTrue(self.s.get(t["id"])["unreported"])

	def test_report_adds_the_summary(self):
		t = self.s.create({"title": "f", "status": "done"}, prefix="A")
		got = self.s.report(t["id"], "omri", "Fixed the cache. NOT verified: Docker", files=["a.py"], refs=["https://x/pull/1"])
		self.assertNotIn("unreported", got)
		self.assertEqual({k: got["result"][k] for k in ("summary", "files", "refs", "by")},
		                 {"summary": "Fixed the cache. NOT verified: Docker", "files": ["a.py"], "refs": ["https://x/pull/1"], "by": "omri"})
		self.assertEqual((got["notes"][-1]["kind"], got["notes"][-1]["by"]), ("complete", "omri"))
		self.assertEqual(self.s.activity(t["id"], limit=1)[0]["event"], "reported")
		self.assertEqual(got["status"], "done")
		with self.assertRaisesRegex(TicketError, "already has a summary"):
			self.s.report(t["id"], "omri", "again")

	def test_report_refusals(self):
		t = self.s.create({"title": "g"}, prefix="A")
		with self.assertRaisesRegex(TicketError, "not done"):
			self.s.report(t["id"], "omri", "x")
		self.s.update(t["id"], {"status": "done"})
		with self.assertRaisesRegex(TicketError, "human action"):
			self.s.report(t["id"], "backend-1", "x")  # a worker finishes with complete
		with self.assertRaisesRegex(TicketError, "needs a summary"):
			self.s.report(t["id"], "omri", "  ")
		with self.assertRaisesRegex(TicketError, "needs `by`"):
			self.s.report(t["id"], "", "x")
		self.assertTrue(self.s.get(t["id"])["unreported"])  # nothing changed


class ReportFrontDoors(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = board(Path(self.tmp.name), {})
		from unblockd import web
		web.BOARDS = Boards([str(Path(self.tmp.name))])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.r = cli.Remote(f"http://127.0.0.1:{self.srv.server_port}")

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def test_rest_and_remote(self):
		t = self.r.create({"title": "h"}, prefix="A")
		got = self.r.update(t["id"], {"status": "done"}, by="dana")  # PATCH passes the warning and the name through
		self.assertTrue(any("complete()" in w for w in got["warnings"]))
		self.assertTrue(got["unreported"])
		self.assertEqual(Store(self.dir).activity(t["id"], limit=1)[0]["agent"], "dana")
		got = self.r.report(t["id"], "dana", "what changed", files=["b.py"])
		self.assertEqual((got["result"]["by"], got["result"]["files"]), ("dana", ["b.py"]))
		with self.assertRaisesRegex(TicketError, "already has a summary"):
			self.r.report(t["id"], "dana", "again")

	def test_cli(self):
		s = Store(self.dir)
		t = s.create({"title": "i", "status": "done"}, prefix="A")
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			code = cli.main(["--dir", str(self.dir), "report", t["id"], "the summary", "--by", "omri", "--file", "c.py", "--ref", "PR 3"])
		self.assertEqual(code, 0)
		got = json.loads(out.getvalue())
		self.assertEqual((got["result"]["summary"], got["result"]["by"], got["result"]["refs"]), ("the summary", "omri", ["PR 3"]))
		t2 = s.create({"title": "j"}, prefix="A")
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			cli.main(["--dir", str(self.dir), "update", t2["id"], "--json", '{"status": "done"}', "--by", "omri"])
		self.assertIn("warnings", json.loads(out.getvalue()))
		self.assertEqual(s.activity(t2["id"], limit=1)[0]["agent"], "omri")


if __name__ == "__main__":
	unittest.main()
