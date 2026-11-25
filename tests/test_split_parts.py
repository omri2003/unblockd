"""split(parts=...): parts with their own done_when / touches / body, "#N" refs between them, all or nothing, claim=k (CORE-50)."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd import cli
from unblockd.boards import Boards
from unblockd.store import Store, TicketError


class SplitPartsTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "p" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({"owners": [{"name": "Omri", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
		                                             "agents": [{"name": "ui", "tags": ["ui"]}]}), encoding="utf-8")
		self.s = Store(d)
		self.s.create({"title": "gate"}, tid="S-1")
		self.s.update("S-1", {"status": "done"})
		self.s.create({"title": "Settings page", "owner": "Claude", "priority": "P1", "tags": ["ui"], "touches": ["ui/settings/*"], "blocked_by": ["S-1"]}, tid="S-2")

	def tearDown(self):
		self.tmp.cleanup()

	def parts(self):
		return [{"title": "Audio", "touches": ["ui/settings/audio.js"], "done_when": ["volume saved"], "body": "the sliders"},
		        {"title": "Video", "touches": ["ui/settings/video.js"], "done_when": ["resolution applied"]},
		        {"title": "Keybindings", "done_when": ["rebinding works"], "blocked_by": ["#1"]}]

	def test_parts_carry_their_own_fields_and_refs(self):
		r = self.s.split("S-2", parts=self.parts())
		k = {x["title"]: x for x in r["children"]}
		self.assertEqual([x["id"] for x in r["children"]], ["S-3", "S-4", "S-5"])
		self.assertEqual((k["Audio"]["touches"], k["Audio"]["done_when"], k["Audio"]["body"]), (["ui/settings/audio.js"], ["volume saved"], "the sliders"))
		self.assertEqual((k["Video"]["body"], k["Video"]["priority"], k["Video"]["tags"], k["Video"]["parent"]), ("Split from S-2.", "P1", ["ui"], "S-2"))
		self.assertEqual(k["Keybindings"]["touches"], ["ui/settings/*"])  # not given: copied from the parent
		self.assertEqual(k["Keybindings"]["blocked_by"], ["S-1", "S-3"])  # the parent's blockers + "#1"
		self.assertEqual(r["parent"]["blocked_by"], ["S-1", "S-3", "S-4", "S-5"])
		self.s.claim("ui-1", tid="S-3")  # Audio and Video no longer overlap: both can run at once
		self.assertEqual(self.s.claim("ui-2")["id"], "S-4")

	def test_all_or_nothing(self):
		bad = self.parts() + [{"title": ""}, {"title": "x", "blocked_by": ["#99"]}, {"title": "y", "parent": "S-1"}, {"title": "z", "done_when": ["a" * 201]},
		                      {"title": "w", "blocked_by": ["#8"]}, {"title": "v", "blocked_by": ["#9"]}, "nope"]
		bad[1]["blocked_by"] = ["#2"]
		with self.assertRaises(TicketError) as e:
			self.s.split("S-2", parts=bad)
		msg = str(e.exception)
		for part in ("nothing was split", "#2: S-4 can't be blocked by itself", "#4: invalid title", "#5: #99 is not a part", "#8: S-10 can't be blocked by itself", "#6: a part can't set parent",
		             "#7: invalid done_when[0]", "#10: a part must be an object"):
			self.assertIn(part, msg)
		self.assertNotIn("#1:", msg)
		with self.assertRaisesRegex(TicketError, "blocked_by makes a loop"):
			self.s.split("S-2", parts=[{"title": "a", "blocked_by": ["#2"]}, {"title": "b", "blocked_by": ["#1"]}])
		with self.assertRaisesRegex(TicketError, "titles .* or parts, not both"):
			self.s.split("S-2", ["a", "b"], parts=self.parts())
		with self.assertRaisesRegex(TicketError, "at least 2 parts"):
			self.s.split("S-2", parts=[{"title": "only"}])
		self.assertEqual(len(self.s.list()), 2)
		self.assertEqual([e["event"] for e in self.s.activity(limit=3)], ["created", "done", "created"])  # only setUp's events

	def test_titles_form_unchanged(self):
		kids = self.s.split("S-2", ["a", " ", "b"], sequential=True)["children"]
		self.assertEqual([(k["body"], k["touches"], k["blocked_by"]) for k in kids], [("Split from S-2.", ["ui/settings/*"], ["S-1"]), ("Split from S-2.", ["ui/settings/*"], ["S-1", "S-3"])])

	def test_claim_part_k_in_the_same_call(self):
		self.s.claim("ui-1", tid="S-2")
		r = self.s.split("S-2", parts=self.parts(), by="ui-1", claim=2)
		self.assertEqual((r["claimed"]["id"], r["claimed"]["claim"]["agent"], r["claimed"]["status"]), ("S-4", "ui-1", "doing"))
		self.assertIn("lease_minutes", r["claimed"])
		self.assertEqual((r["parent"]["status"], r["parent"]["claim"]), ("todo", None))  # the parent's claim ended, the part's began

	def test_claim_refusals_write_nothing(self):
		for kw, msg in (({"claim": 3, "by": "ui-1"}, "#3: claim=3 refused: that part waits on S-3"), ({"claim": 4, "by": "ui-1"}, "not a part of this split"),
		                ({"claim": 1}, "needs by"), ({"claim": 1, "by": "nobody"}, "matches no agent profile"),
		                ({"claim": 1, "by": "ui-1", "principal": "Claude"}, "not a human owner")):
			with self.assertRaisesRegex(TicketError, msg):
				self.s.split("S-2", parts=self.parts(), **kw)
		parts = self.parts()
		parts[0]["owner"] = "Omri"
		with self.assertRaisesRegex(TicketError, "owned by Omri"):
			self.s.split("S-2", parts=parts, by="ui-1", claim=1)
		self.s.claim("ui-9", tid="S-2")
		with self.assertRaisesRegex(TicketError, "claimed by ui-9"):  # the holder rule still applies
			self.s.split("S-2", parts=self.parts(), by="ui-1", claim=1)
		self.assertEqual(len(self.s.list()), 2)

	def test_front_doors(self):
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		try:
			r = cli.Remote(f"http://127.0.0.1:{srv.server_port}")
			out = r.split("S-2", parts=self.parts(), by="ui-1", claim=1)
			self.assertEqual((out["claimed"]["id"], [k["id"] for k in out["children"]]), ("S-3", ["S-3", "S-4", "S-5"]))
			with self.assertRaisesRegex(TicketError, "#1: invalid title"):
				r.split("S-4", parts=[{"title": ""}, {"title": "b"}])
		finally:
			srv.shutdown()
			srv.server_close()
		f = Path(self.tmp.name) / "parts.json"
		f.write_text(json.dumps([{"title": "x", "touches": ["a/"]}, {"title": "y", "touches": ["b/"], "blocked_by": ["#1"]}]))
		out, old = io.StringIO(), sys.stdin
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
			code = cli.main(["--dir", str(self.s.dir), "split", "S-4", "--parts", str(f), "--by", "ui-2", "--claim", "1"])
		self.assertEqual(code, 0)
		got = json.loads(out.getvalue())
		self.assertEqual((got["claimed"]["id"], got["children"][1]["blocked_by"]), ("S-6", ["S-1", "S-6"]))  # S-4 waits on S-1, so its parts do too
		self.assertIn("context", got["claimed"])


if __name__ == "__main__":
	unittest.main()
