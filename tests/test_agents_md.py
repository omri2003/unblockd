"""AI-55: the short block for a project's CLAUDE.md / AGENTS.md (CLI `agents-md`, `--write`, create_board reply)."""
import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from unblockd import boards as bd
from unblockd import cli
from unblockd import store as st

ROOT = Path(__file__).resolve().parent.parent


class AgentsMd(unittest.TestCase):
	def setUp(self):
		self.tmp = Path(tempfile.mkdtemp())
		self.dir = self.tmp / "tickets"
		self.dir.mkdir()
		(self.dir / "unblockd.json").write_text(json.dumps({"id": "pay", "title": "Payments"}), encoding="utf-8")

	def run_cli(self, *args):
		out, err = io.StringIO(), io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
			code = cli.main(["--dir", str(self.dir), *args])
		return code, out.getvalue(), err.getvalue()

	def test_block_is_short_and_names_the_steps(self):
		code, out, _ = self.run_cli("agents-md")
		self.assertEqual(code, 0)
		lines = out.splitlines()
		self.assertLessEqual(len(lines), 15)
		self.assertEqual(lines[0], bd.AGENTS_MD_START)
		self.assertEqual(lines[-1], bd.AGENTS_MD_END)
		self.assertIn("`pay`", out)  # the board's own id
		for tool in bd.AGENTS_MD_TOOLS:
			self.assertIn(tool, out)
		self.assertIn(f"python -m unblockd.cli --dir {self.dir}", out)
		self.assertIn("unblockd://work-loop", out)
		code, out, _ = self.run_cli("agents-md", "--board", "other")
		self.assertIn("`other`", out)

	def test_block_cannot_drift_from_the_protocol(self):
		loop = (ROOT / "unblockd" / "WORK_LOOP.md").read_text(encoding="utf-8")
		for tool in bd.AGENTS_MD_TOOLS:
			self.assertIn(tool, loop, f"{tool} is in the agents-md block but not in WORK_LOOP.md")
			self.assertIn(tool, st.MCP_TOOLS)
		named = set(re.findall(r"`([a-z_]+)\(", bd.agents_md("b")))
		self.assertEqual(named, set(bd.AGENTS_MD_TOOLS))
		block = bd.agents_md("b")
		for cmd, flags in {"claim": ("--agent", "--id"), "heartbeat": ("--agent", "--note"), "ask": ("--agent", "--question"),
		                   "complete": ("--agent", "--summary", "--file", "--ref")}.items():  # every CLI form in the block is a real command + flags
			self.assertIn(f"{cmd} ", block)
			with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
				cli.main([cmd, "--help"])
			for flag in flags:
				self.assertIn(flag, out.getvalue())

	def test_write_appends_between_markers_and_keeps_the_rest(self):
		f = self.tmp / "CLAUDE.md"
		before = b"# My project\r\n\r\nRules here.\r\nno final newline"
		f.write_bytes(before)
		code, out, _ = self.run_cli("agents-md", "--write", str(f))
		self.assertEqual(code, 0, out)
		after = f.read_bytes()
		self.assertTrue(after.startswith(before))  # every old byte kept
		added = after[len(before):].decode("utf-8")
		self.assertTrue(added.startswith("\r\n\r\n" + bd.AGENTS_MD_START))
		self.assertNotIn("\n", added.replace("\r\n", ""))  # the file's CRLF kept
		self.assertEqual(added.strip().replace("\r\n", "\n") + "\n", bd.agents_md("pay", str(self.dir)))
		code, _, err = self.run_cli("agents-md", "--write", str(f))  # second time: refused, file unchanged
		self.assertEqual(code, 1)
		self.assertIn("already has the unblockd block", err)
		self.assertEqual(f.read_bytes(), after)

	def test_write_creates_a_missing_file(self):
		f = self.tmp / "AGENTS.md"
		self.assertEqual(self.run_cli("agents-md", "--write", str(f))[0], 0)
		self.assertEqual(f.read_text(encoding="utf-8"), bd.agents_md("pay", str(self.dir)))

	def test_database_location_hides_the_password(self):
		self.assertNotIn("secret", bd.agents_md("b", "postgres://u:secret@h/db#b"))

	def test_create_board_reply_carries_the_block(self):
		root = self.tmp / "root"
		root.mkdir()
		got = bd.Boards([str(root)], [], []).create("game", "Game")
		self.assertEqual(got["agents_md"], bd.agents_md("game", got["path"]))
		self.assertIn("CLAUDE.md", got["agents_md_hint"])

	def test_this_repo_carries_the_block(self):
		text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
		self.assertIn(bd.AGENTS_MD_START, text)
		self.assertIn(bd.AGENTS_MD_END, text)


if __name__ == "__main__":
	unittest.main()
