"""CORE-84: one version (unblockd.__version__) shown on /healthz, GET meta, MCP board_info, the CLI; CI's tag check."""
import importlib.util
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import unblockd
from unblockd import cli

ROOT = Path(__file__).resolve().parent.parent


def _script(name):
	spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


class VersionTest(unittest.TestCase):
	def test_one_source(self):
		self.assertRegex(unblockd.__version__, r"^\d+\.\d+\.\d+$")
		py = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
		self.assertIn('dynamic = ["version"]', py)
		self.assertIn('version = {attr = "unblockd.__version__"}', py)
		self.assertIsNone(re.search(r'^version\s*=\s*"', py, re.M))  # no second copy in [project]
		self.assertEqual(_script("check_version").source_version(), unblockd.__version__)

	def test_cli_version(self):
		out = io.StringIO()
		with redirect_stdout(out), self.assertRaises(SystemExit) as e:
			cli.main(["--version"])
		self.assertEqual((e.exception.code, out.getvalue().strip()), (0, f"unblockd {unblockd.__version__}"))

	def test_tag_check(self):
		check = _script("check_version").check
		self.assertIsNone(check("v0.2.0", "0.2.0"))
		err = check("v0.2.1", "0.2.0")
		self.assertIn('__version__ = "0.2.0"', err)
		self.assertIn("release PR", err)
		self.assertIn("not vX.Y.Z", check("v0.2", "0.2.0"))
		self.assertIn("not vX.Y.Z", check("0.2.0", "0.2.0"))

	def test_tag_check_main(self):
		main = _script("check_version").main
		v = unblockd.__version__
		with redirect_stdout(io.StringIO()):
			self.assertEqual(main([f"v{v}"]), 0)
		import contextlib
		with contextlib.redirect_stderr(io.StringIO()):
			self.assertEqual(main(["v99.0.0"]), 1)
			self.assertEqual(main([]), 2)


if __name__ == "__main__":
	unittest.main()
