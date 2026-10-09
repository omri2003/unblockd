"""Per-board ticket schema (strict for agents, warnings for humans) and the proposal gate."""
import asyncio
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

from unblockd.boards import Boards
from unblockd.store import Store, TicketError

SCHEMA = {"prefixes": {"FOG": "fog + explored map", "UI": "the web board"}, "required": ["done_when", "priority"],
          "tags": ["bug", "chore"], "body_template": "## Why\n\n## Approach\n"}
GOOD = {"title": "x", "done_when": ["it works"], "priority": "P2", "body": "## Why\nbecause"}


def store(tmp, **cfg):
	d = Path(tmp) / "tickets"
	d.mkdir(exist_ok=True)
	(d / "unblockd.json").write_text(json.dumps(cfg))
	return Store(d)


class SchemaTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()

	def tearDown(self):
		self.tmp.cleanup()

	def test_no_schema_changes_nothing(self):
		s = store(self.tmp.name)
		t = s.create({"title": "x"}, prefix="ANY")
		self.assertNotIn("warnings", t)
		self.assertEqual(t["proposed_by"], "")
		self.assertEqual(s.config["schema"], None)
		with self.assertRaisesRegex(TicketError, "give an id or a prefix"):
			s.create({"title": "x"})

	def test_agent_refused_with_every_problem_listed_and_capped(self):
		s = store(self.tmp.name, schema=SCHEMA, agent_tickets="open")
		with self.assertRaises(TicketError) as cm:
			s.create({"title": "x", "tags": ["wibble"]}, prefix="FOGG", agent="bot-1")
		m = str(cm.exception)
		for word in ("prefix FOGG is not one of FOG, UI", "done_when is required", "priority is required", "tag wibble", "body is required", "board_info", "ask_human"):
			self.assertIn(word, m)
		self.assertNotIn("fog + explored", m)  # keys only, meanings are in board_info
		self.assertEqual(s.list(), [])
		long = {"title": "x", "tags": [f"t{i}" * 8 for i in range(20)]}
		with self.assertRaises(TicketError) as cm:
			s.create(long, prefix="FOG", agent="bot-1")
		self.assertLess(len(str(cm.exception)), 460)

	def test_agent_ok_and_human_gets_warnings(self):
		s = store(self.tmp.name, schema=SCHEMA, agent_tickets="open")
		self.assertNotIn("warnings", s.create(GOOD, prefix="FOG", agent="bot-1"))
		t = s.create({"title": "just a title"}, prefix="NOPE")  # a human: created, with warnings
		self.assertEqual(t["id"], "NOPE-1")
		self.assertEqual(len(t["warnings"]), 3)
		self.assertNotIn("warnings", s.get("NOPE-1"))  # not stored

	def test_explicit_id_prefix_is_checked_but_split_is_exempt(self):
		s = store(self.tmp.name, schema=SCHEMA, agent_tickets="open")
		with self.assertRaisesRegex(TicketError, "prefix OTHER"):
			s.create(GOOD, tid="OTHER-7", agent="bot-1")
		s.create(GOOD, tid="FOG-7", agent="bot-1")
		s.create({"title": "legacy"}, tid="OLD-1")  # a human, any id
		kids = s.split("OLD-1", ["a", "b"])["children"]  # reuses OLD: not refused, no warnings
		self.assertEqual([k["id"] for k in kids], ["OLD-2", "OLD-3"])
		s.update("OLD-1", {"title": "still editable"}, agent="bot-1")  # existing tickets are exempt

	def test_default_prefix_when_none_given(self):
		s = store(self.tmp.name, schema={"prefixes": {"ONE": "the only one"}})
		self.assertEqual(s.create({"title": "x"})["id"], "ONE-1")
		s = store(self.tmp.name, schema=SCHEMA)
		with self.assertRaisesRegex(TicketError, "prefixes are FOG, UI"):
			s.create({"title": "x"})

	def test_config_validation(self):
		for bad in ({"wat": 1}, {"prefixes": {"fog": "x"}}, {"prefixes": {"FOG": "x" * 81}}, {"required": ["nope"]},
		            {"tags": ["Has Space"]}, {"body_template": 3}, [], {"prefixes": []}):
			with self.assertRaisesRegex(TicketError, "schema"):
				store(self.tmp.name, schema=bad)
		with self.assertRaisesRegex(TicketError, "agent_tickets"):
			store(self.tmp.name, agent_tickets="maybe")
		s = store(self.tmp.name)
		self.assertEqual((s.config["schema"], s.config["agent_tickets"]), (None, "open"))


class ProposalTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.s = store(self.tmp.name, agent_tickets="propose")

	def test_default_is_open(self):
		o = store(self.tmp.name)
		t = o.create({"title": "a"}, prefix="OPN", agent="bot-1")
		self.assertEqual((t["proposed_by"], t["effective_status"]), ("", "todo"))
		self.assertEqual(o.claim("bot-2")["id"], "OPN-1")

	def tearDown(self):
		self.tmp.cleanup()

	def test_agent_ticket_is_a_proposal_until_accepted(self):
		t = self.s.create({"title": "idea", "owner": "Claude"}, prefix="BUG", agent="bot-1")
		self.assertEqual((t["proposed_by"], t["effective_status"]), ("bot-1", "pending"))
		self.assertEqual(self.s.next_ready(), [])
		self.assertIsNone(self.s.claim("bot-2", tid=None))  # nothing claimable
		with self.assertRaisesRegex(TicketError, "can't be claimed"):
			self.s.claim("bot-2", tid="BUG-1")
		with self.assertRaisesRegex(TicketError, "accept it first"):
			self.s.update("BUG-1", {"status": "doing"})
		with self.assertRaisesRegex(TicketError, "read, not written"):
			self.s.update("BUG-1", {"proposed_by": ""})
		self.assertEqual(self.s.claim_next("bot-2")["counts"]["waiting_on_human"], 1)
		a = self.s.accept("BUG-1", "omri")
		self.assertEqual((a["proposed_by"], a["effective_status"]), ("", "todo"))
		self.assertIn("Accepted by omri", a["body"])
		self.assertEqual([e["event"] for e in self.s.activity("BUG-1")][:2], ["accepted", "proposed"])
		self.assertEqual(self.s.claim("bot-2")["id"], "BUG-1")
		with self.assertRaisesRegex(TicketError, "isn't a proposal"):
			self.s.accept("BUG-1", "omri")

	def test_open_mode_human_and_split_are_exempt(self):
		self.assertEqual(self.s.create({"title": "h"}, prefix="BUG")["proposed_by"], "")  # a human
		self.assertEqual(self.s.create({"title": "h"}, prefix="BUG", agent="")["proposed_by"], "")  # empty agent = human
		self.s.update("BUG-1", {"status": "doing"})
		kids = self.s.split("BUG-1", ["a", "b"], by="bot-1")["children"]
		self.assertEqual([k["proposed_by"] for k in kids], ["", ""])
		o = store(self.tmp.name, agent_tickets="open")
		self.assertEqual(o.create({"title": "a"}, prefix="OPN", agent="bot-1")["effective_status"], "todo")

	def test_reject_is_delete_with_a_logged_reason(self):
		self.s.create({"title": "idea"}, prefix="BUG", agent="bot-1")
		self.s.delete("BUG-1", reason="duplicate of BUG-9")
		self.assertIn("rejected: duplicate of BUG-9", self.s.activity(limit=1)[0]["note"])


class ApiTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		d = Path(self.tmp.name) / "p" / "tickets"
		d.mkdir(parents=True)
		(d / "unblockd.json").write_text(json.dumps({"schema": SCHEMA, "agent_tickets": "propose"}))
		from unblockd import web
		web.BOARDS = Boards([self.tmp.name])
		self.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=self.srv.serve_forever, daemon=True).start()
		self.base = f"http://127.0.0.1:{self.srv.server_port}"

	def tearDown(self):
		self.srv.shutdown()
		self.srv.server_close()
		self.tmp.cleanup()

	def call(self, method, path, body=None):
		req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
		                             headers={"Content-Type": "application/json"})
		try:
			with urllib.request.urlopen(req) as r:
				return r.status, json.loads(r.read())
		except urllib.error.HTTPError as e:
			return e.code, json.loads(e.read())

	def test_rest_flow_and_needs_proposal_kind(self):
		self.assertEqual(self.call("GET", "/api/meta")[1]["config"]["schema"]["prefixes"]["UI"], "the web board")
		code, t = self.call("POST", "/api/tickets", {"prefix": "UI", "title": "bare"})  # a human
		self.assertEqual((code, len(t["warnings"])), (201, 2))
		self.assertEqual(self.call("POST", "/api/tickets", {"prefix": "UI", "title": "bare", "agent": "bot-1"})[0], 400)
		code, p = self.call("POST", "/api/tickets", {**GOOD, "prefix": "UI", "agent": "bot-1"})
		self.assertEqual((code, p["proposed_by"]), (201, "bot-1"))
		items = self.call("GET", "/api/needs")[1][0]["items"]
		self.assertEqual([(i["id"], i["kind"], i["proposed_by"], i["done_when"]) for i in items], [(p["id"], "proposal", "bot-1", ["it works"])])
		self.assertEqual(self.call("POST", f"/api/tickets/{p['id']}/accept", {"by": "omri"})[1]["proposed_by"], "")
		self.assertEqual(self.call("GET", "/api/needs")[1], [])
		code, q = self.call("POST", "/api/tickets", {**GOOD, "prefix": "FOG", "agent": "bot-1"})
		self.assertEqual(self.call("DELETE", f"/api/tickets/{q['id']}?reason=nope")[0], 200)

	def test_notify_lines(self):
		from unblockd import notify
		self.assertIn("proposed by bot-1", notify._line("b", {"event": "proposed", "ticket": "UI-1", "agent": "bot-1"}, {"UI-1": "t"}))
		self.assertIn("accepted by omri", notify._line("b", {"event": "accepted", "ticket": "UI-1", "agent": "omri"}, {"UI-1": "t"}))


try:
	import mcp  # noqa: F401
except ImportError:
	mcp = None


@unittest.skipIf(mcp is None, "mcp not installed")
class McpTest(unittest.TestCase):
	def test_create_ticket_requires_agent_and_accept_exists(self):
		from unblockd import mcp_server as m
		tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
		self.assertIn("agent", tools["create_ticket"].input_schema["required"])
		self.assertIn("accept_ticket", tools)
