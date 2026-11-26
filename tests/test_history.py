"""History cleanup (CORE-37): config history_days (default 365, null = keep forever) and folder boards keeping their 5 MB
rotation. The database side (events deleted by age, counts and cursors still right) is in tests/connector_cases.py, run on
SQLite always and on Postgres / MongoDB when their URLs are set."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unblockd import store as store_mod
from unblockd.store import Store, TicketError


class HistoryDaysTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(self.tmp.cleanup)
		self.dir = Path(self.tmp.name) / "tickets"
		self.dir.mkdir()
		self.cfg = self.dir / "unblockd.json"

	def test_default_365_and_validated(self):
		self.assertEqual(Store(self.dir).config["history_days"], 365)
		for ok in ("30", "null"):
			self.cfg.write_text('{"history_days": %s}' % ok)
			self.assertEqual(Store(self.dir).config["history_days"], json.loads(ok))
		for bad in ("0", "-1", "1.5", '"365"', "true", "36501"):
			self.cfg.write_text('{"history_days": %s}' % bad)
			with self.assertRaisesRegex(TicketError, "history_days"):
				Store(self.dir)

	def test_a_folder_board_keeps_its_history(self):
		self.cfg.write_text('{"history_days": 1}')
		s = Store(self.dir)
		s.create({"title": "a"}, prefix="T")
		old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat(timespec="seconds")
		with open(s.rt / "activity.jsonl", "a", encoding="utf-8") as f:
			f.write(json.dumps({"ts": old, "event": "expired", "ticket": "T-1", "agent": "w-1", "note": "old"}) + "\n")
		store_mod._PRUNED.clear()
		s.add_note("T-1", "Omri", "today")
		self.assertIn("old", [e["note"] for e in s.activity(limit=100)])  # files: the 5 MB rotation, never cleanup by age
		self.assertFalse((s.rt / "history.json").exists())


if __name__ == "__main__":
	unittest.main()
