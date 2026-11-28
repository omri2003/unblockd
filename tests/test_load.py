"""tests/load.py (CORE-40) at a tiny size, so the script keeps working: files and SQLite, every check passes."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import load


class LoadScriptTest(unittest.TestCase):
	def run_load(self, *args):
		out = io.StringIO()
		with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
			code = load.main(["--tickets", "60", "--open", "20", "--agents", "4", "--rounds", "1", *args])
		return code, json.loads(out.getvalue())

	def check(self, code, r):
		self.assertEqual(code, 0, r["check"]["problems"])
		c = r["check"]
		self.assertEqual((c["problem_count"], c["agents_gave_up"], c["created"]), (0, 0, 4))
		self.assertEqual(c["claimed"], c["completed"])
		self.assertEqual(set(r["single"]), {"list", "get", "next_ready", "needs", "claim_release", "create_delete"})

	def test_files(self):
		self.check(*self.run_load())

	def test_sqlite(self):
		with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
			self.check(*self.run_load("--location", "sqlite:///" + (Path(d) / "l.db").as_posix() + "#load"))
