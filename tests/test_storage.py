"""Slow storage notice (UI-81): GET meta flags a board whose files sit on a slow Docker Desktop mount, timed from the stat
passes the board's reads already make (no extra filesystem calls)."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import files
from unblockd.boards import Boards
from unblockd.store import Store


def slow_stat(e):  # a Docker Desktop bind mount: ~0.6 ms per stat
	time.sleep(0.0006)
	return e.stat()


class StorageTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = Path(self.tmp.name) / "p" / "tickets"
		self.dir.mkdir(parents=True)
		(self.dir / "unblockd.json").write_text(json.dumps({"title": "P"}), encoding="utf-8")
		for i in range(1, 13):
			(self.dir / f"T-{i}.json").write_text(json.dumps({"id": f"T-{i}", "title": "x"}), encoding="utf-8")
		files._STAT_MS.clear()
		self.addCleanup(files._STAT_MS.clear)
		self.addCleanup(self.tmp.cleanup)
		env = mock.patch.dict(os.environ, {}, clear=False)
		env.start(); self.addCleanup(env.stop)
		os.environ.pop("UNBLOCKD_SLOW_STORAGE", None)

	def measure(self, stat=lambda e: e.stat(), container=True, passes=3):
		files._STAT_MS.clear()
		with mock.patch.object(files, "_stat", stat), mock.patch.object(files, "in_container", lambda: container), \
				mock.patch.object(files, "SCAN_REUSE_S", 0.0):
			for _ in range(passes):
				Store(self.dir).list()
			return files.storage(self.dir)

	def test_a_slow_mount_in_a_container_is_flagged(self):
		r = self.measure(slow_stat)
		self.assertTrue(r["slow"], r)
		self.assertGreater(r["per_file_ms"], files.SLOW_STAT_MS)
		self.assertEqual(r["passes"], files.SLOW_PASSES)

	def test_a_fast_disk_is_not(self):  # measured here: a native disk is ~0.02 ms per stat
		r = self.measure()
		self.assertFalse(r["slow"], r)
		self.assertLess(r["per_file_ms"], files.SLOW_STAT_MS)

	def test_never_outside_a_container(self):  # a native board on a slow disk (a network share) is not a Docker Desktop mount
		self.assertFalse(self.measure(slow_stat, container=False)["slow"])

	def test_too_few_files_to_judge(self):
		for p in list(self.dir.glob("T-*.json"))[files.SLOW_MIN - 1:]:
			p.unlink()
		r = self.measure(slow_stat)
		self.assertEqual((r["slow"], r["per_file_ms"], r["passes"]), (False, None, 0))

	def test_nothing_read_yet_says_so(self):
		self.assertEqual(files.storage(self.dir), {"per_file_ms": None, "passes": 0, "slow": False})

	def test_median_of_the_last_passes(self):  # one slow pass (a cold cache) does not flag a fast board
		files._STAT_MS.clear()
		with mock.patch.object(files, "in_container", lambda: True), mock.patch.object(files, "SCAN_REUSE_S", 0.0):
			with mock.patch.object(files, "_stat", slow_stat):
				Store(self.dir).list()
			for _ in range(2):
				Store(self.dir).list()
			self.assertFalse(files.storage(self.dir)["slow"])

	def test_env_forces_it_either_way(self):
		os.environ["UNBLOCKD_SLOW_STORAGE"] = "1"
		self.assertTrue(self.measure(container=False)["slow"])
		os.environ["UNBLOCKD_SLOW_STORAGE"] = "0"
		self.assertFalse(self.measure(slow_stat)["slow"])

	def test_get_meta_carries_the_flag(self):
		from unblockd import web
		web.BOARDS = Boards([str(Path(self.tmp.name))])
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
		get = lambda p: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/{p}").read())
		with mock.patch.object(files, "_stat", slow_stat), mock.patch.object(files, "in_container", lambda: True):
			self.assertEqual(get("meta")["storage"]["passes"], 0)  # a fresh server: the UI asks again after its first /tickets
			get("tickets")
			self.assertTrue(get("meta")["storage"]["slow"])
		files._STAT_MS.clear()
		get("tickets")
		self.assertIs(get("meta")["storage"]["slow"], False)


if __name__ == "__main__":
	unittest.main()
