"""Windows: os.replace over a file another reader has open raises PermissionError (CORE-86). files.replace retries it for
a bounded time, then refuses with a how-to-fix TicketError; elsewhere a PermissionError is real and is raised at once."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from unblockd import files
from unblockd.errors import TicketError
from unblockd.store import Store


class ReplaceTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name)
		self.src = self.dir / "x.tmp"
		self.dst = self.dir / "x.json"
		self.src.write_text("new", encoding="utf-8")
		self.dst.write_text("old", encoding="utf-8")
		self.real = os.replace

	def tearDown(self):
		self.tmp.cleanup()

	def flaky(self, fails):
		calls = []

		def rep(a, b):
			calls.append(1)
			if len(calls) <= fails:
				raise PermissionError(13, "Access is denied")
			self.real(a, b)
		return rep, calls

	def test_windows_retries_until_the_reader_lets_go(self):
		rep, calls = self.flaky(3)
		with mock.patch.object(files, "WINDOWS", True), mock.patch.object(files.os, "replace", rep):
			files.replace(self.src, self.dst)
		self.assertEqual(len(calls), 4)
		self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")

	def test_windows_gives_up_with_how_to_fix(self):
		rep, _ = self.flaky(10 ** 9)
		with mock.patch.object(files, "WINDOWS", True), mock.patch.object(files, "REPLACE_WAIT", 0.05), \
				mock.patch.object(files.os, "replace", rep):
			with self.assertRaises(TicketError) as e:
				files.replace(self.src, self.dst)
		self.assertIn("another program kept it open", str(e.exception))
		self.assertIn("close it and try again", str(e.exception))
		self.assertFalse(self.src.exists())  # no temp file left behind
		self.assertEqual(self.dst.read_text(encoding="utf-8"), "old")

	def test_elsewhere_a_permission_error_is_real(self):
		rep, calls = self.flaky(10 ** 9)
		with mock.patch.object(files, "WINDOWS", False), mock.patch.object(files.os, "replace", rep):
			with self.assertRaises(PermissionError):
				files.replace(self.src, self.dst)
		self.assertEqual(len(calls), 1)
		self.assertFalse(self.src.exists())

	def test_windows_read_racing_a_replace_retries(self):  # open() of a file being replaced: Errno 13 on Windows
		real, calls = Path.read_text, []

		def rt(p, *a, **k):
			calls.append(1)
			if len(calls) <= 2:
				raise PermissionError(13, "Permission denied")
			return real(p, *a, **k)
		with mock.patch.object(files, "WINDOWS", True), mock.patch.object(Path, "read_text", rt):
			self.assertEqual(files.read_text(self.dst), "old")
		self.assertEqual(len(calls), 3)

	def test_every_store_write_goes_through_it(self):  # ticket file, claims.json, unblockd.json
		d = self.dir / "tickets"
		d.mkdir()
		(d / "unblockd.json").write_text("{}\n", encoding="utf-8")
		s = Store(d)
		rep, calls = self.flaky(1)
		with mock.patch.object(files, "WINDOWS", True), mock.patch.object(files.os, "replace", rep):
			t = s.create({"title": "a"}, prefix="T")
			self.assertEqual(len(calls), 2)
			calls.clear()
			s.claim("w", tid=t["id"])
			self.assertGreaterEqual(len(calls), 2)  # the claims.json write failed once, then went through
			calls.clear()
			s.backend.write_config({"title": "B"})
			self.assertEqual(len(calls), 2)
		self.assertEqual(s.get(t["id"])["claim"]["agent"], "w")

	@unittest.skipUnless(os.name == "nt", "the race only exists on Windows")
	def test_real_reader_holding_claims_open(self):
		d = self.dir / "tickets"
		d.mkdir()
		s = Store(d)
		ids = [s.create({"title": f"t{i}"}, prefix="T")["id"] for i in range(3)]
		s.claim("w0", tid=ids[0])
		p = s.backend.rt / "claims.json"
		stop = time.monotonic() + 1.5

		def reader():  # opens claims.json the way an unlocked read does, over and over
			while time.monotonic() < stop:
				with open(p, encoding="utf-8") as f:
					f.read()
					time.sleep(0.002)
		th = threading.Thread(target=reader)
		th.start()
		try:
			for i in range(30):
				s.heartbeat(ids[0], "w0")
		finally:
			th.join()
		self.assertEqual(s.get(ids[0])["claim"]["agent"], "w0")


if __name__ == "__main__":
	unittest.main()
