"""The secret key and encrypted connector passwords (CORE-41). Needs the optional cryptography package (skipped without it,
except the cases about it being missing)."""
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd import cli, sql
from unblockd import secret as sk
from unblockd import system as sy
from unblockd.boards import Boards
from unblockd.errors import TicketError

try:
	import cryptography  # noqa: F401
except ImportError:
	cryptography = None

PW = "s3cret-Pa:ss@word/1"
KEYS = {"UNBLOCKD_SECRET_KEY": "", "UNBLOCKD_SECRET_KEY_FILE": ""}


class _Base(unittest.TestCase):
	def setUp(self):
		tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
		self.addCleanup(tmp.cleanup)
		self.addCleanup(sql.disconnect)
		self.tmp = Path(tmp.name)
		p = mock.patch.dict(os.environ, KEYS)
		p.start()
		self.addCleanup(p.stop)
		self.sys = sy.System(str(self.tmp / "sys" / "system.db"))
		self.addCleanup(self.sys.close)
		self.pg = f"postgres://app:{PW.replace(':', '%3A').replace('@', '%40').replace('/', '%2F')}@127.0.0.1:1/db"


@unittest.skipUnless(cryptography, "the cryptography package is not installed")
class SecretTest(_Base):
	def test_stored_encrypted_and_never_returned(self):
		got = self.sys.add_connector("pg", self.pg)
		self.assertEqual(got, {"name": "pg", "kind": "postgres", "url": "postgres://app@127.0.0.1:1/db", "has_password": True, "created": got["created"]})
		db = sqlite3.connect(self.tmp / "sys" / "system.db")
		self.addCleanup(db.close)
		dump = "\n".join(db.iterdump())
		self.assertNotIn("s3cret", dump)  # neither the password nor its URL form is anywhere in the file
		self.assertNotIn("s3cret%3A", dump)
		self.assertIn(PW.replace(":", "%3A").replace("@", "%40").replace("/", "%2F"), self.sys.connector_url("pg"))  # put back to connect
		b = Boards([], [], [], system=self.sys)
		self.sys.add_board("ops", connector="pg")
		every = json.dumps(b.summary()) + json.dumps(self.sys.connectors()) + json.dumps(self.sys.boards()) + json.dumps(self.sys.info())
		self.assertNotIn("s3cret", every)
		self.assertIn("app:***@", b.summary()[0]["path"])
		out = io.StringIO()
		with mock.patch.dict(os.environ, {"UNBLOCKD_SYSTEM_DB": self.sys.url}), contextlib.redirect_stdout(out):
			cli.main(["system", "connectors"])
		self.assertNotIn("s3cret", out.getvalue())
		with self.assertRaisesRegex(TicketError, "once"):
			self.sys.add_connector("two", self.pg, password="x")
		with mock.patch("sys.stdin", io.StringIO("typed-pw\n")), mock.patch.dict(os.environ, {"UNBLOCKD_SYSTEM_DB": self.sys.url}), contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(cli.main(["system", "add-connector", "mg", "mongodb://bot@h/db", "--password-stdin"]), 0)
		self.assertEqual(self.sys.connector_url("mg"), "mongodb://bot:typed-pw@h/db")

	def test_rest_never_shows_it(self):
		from unblockd import web
		self.addCleanup(setattr, web, "BOARDS", web.BOARDS)
		self.sys.add_connector("pg", self.pg)
		self.sys.add_board("ops", connector="pg")
		web.BOARDS = Boards([], [], [], system=self.sys)
		srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=srv.serve_forever, daemon=True).start()
		self.addCleanup(srv.server_close)
		self.addCleanup(srv.shutdown)
		base = f"http://127.0.0.1:{srv.server_address[1]}"
		body = urllib.request.urlopen(base + "/api/boards").read().decode()
		self.assertIn('"ops"', body)
		self.assertNotIn("s3cret", body)
		try:
			body = urllib.request.urlopen(base + "/api/b/ops/tickets").read().decode()
		except urllib.error.HTTPError as e:
			body = e.read().decode()
		self.assertNotIn("s3cret", body)

	def test_no_key_set_one_is_generated_and_reused(self):
		kf = self.tmp / "sys" / "secret.key"
		self.sys.add_connector("nopw", "postgres://app@127.0.0.1:1/db")  # no password, no key (2026-11-27: never on start either)
		self.assertEqual(self.sys.key_status()["passwords"], 0)
		self.assertFalse(kf.exists())
		err = io.StringIO()
		with contextlib.redirect_stderr(err):
			self.sys.add_connector("pg", self.pg)
		self.assertTrue(kf.is_file())
		self.assertIn("back it up, separately from the system database", err.getvalue())  # said once, when made
		self.assertGreaterEqual(len(kf.read_text(encoding="utf-8").strip()), sk.MIN_LEN)
		self.sys.close()
		again = sy.System(self.sys.url)  # a restart: same key file, the password still reads
		self.addCleanup(again.close)
		self.assertIn("127.0.0.1:1/db", again.connector_url("pg"))
		self.assertEqual(again.key_status(), {"key": kf.as_posix(), "passwords": 1})

	def test_a_postgres_system_database_never_generates_a_key(self):
		pg_sys = types.SimpleNamespace(url="postgres://u@db/unblockd")  # each server / redeployed container would make its own
		with mock.patch.object(Path, "home", return_value=self.tmp / "home"):
			with self.assertRaisesRegex(TicketError, "Postgres system database needs UNBLOCKD_SECRET_KEY .* every server"):
				sk.key(pg_sys, create=True)
			with self.assertRaisesRegex(TicketError, "is not the one"):  # reading with no key: the lost-key message
				sk.key(pg_sys)
			self.assertFalse((self.tmp / "home" / ".unblockd" / "secret.key").exists())
			with mock.patch.dict(os.environ, {"UNBLOCKD_SECRET_KEY": "k" * 40}):
				self.assertEqual(sk.key(pg_sys, create=True), "k" * 40)  # given one: fine

	def test_key_from_env_and_file(self):
		k = "k" * 40
		with mock.patch.dict(os.environ, {"UNBLOCKD_SECRET_KEY": k}):
			self.sys.add_connector("pg", self.pg)
		self.assertFalse((self.tmp / "sys" / "secret.key").exists())
		f = self.tmp / "key.txt"
		f.write_text(k + "\n", encoding="utf-8")
		with mock.patch.dict(os.environ, {"UNBLOCKD_SECRET_KEY_FILE": str(f)}):
			self.assertIn("app:", self.sys.connector_url("pg"))
		with mock.patch.dict(os.environ, {"UNBLOCKD_SECRET_KEY": k, "UNBLOCKD_SECRET_KEY_FILE": str(f)}):
			with self.assertRaisesRegex(TicketError, "both UNBLOCKD_SECRET_KEY and UNBLOCKD_SECRET_KEY_FILE"):
				self.sys.connector_url("pg")
		with mock.patch.dict(os.environ, {"UNBLOCKD_SECRET_KEY": "short"}):
			with self.assertRaisesRegex(TicketError, "5 characters; use at least 32"):
				self.sys.connector_url("pg")

	def test_a_wrong_or_lost_key_is_a_clear_message(self):
		self.sys.add_connector("pg", self.pg)
		self.sys.add_board("ops", connector="pg")
		folder = self.tmp / "game" / "tickets"
		folder.mkdir(parents=True)
		self.sys.add_board("game", str(folder))
		(self.tmp / "sys" / "secret.key").unlink()  # lost: reading now says so (no new key until a password is stored)
		with self.assertRaisesRegex(TicketError, "connector pg: the secret key .* is not the one .*set-password"):
			self.sys.connector_url("pg")
		rows = {b["id"]: b for b in Boards([], [], [], system=self.sys).summary()}  # only that board says so
		self.assertIn("is not the one", rows["ops"]["error"])
		self.assertNotIn("error", rows["game"])
		self.assertIn("error", self.sys.key_status())
		with self.assertRaisesRegex(TicketError, "is not the one"):  # a new password under another key would mix keys
			self.sys.add_connector("pg2", self.pg)
		self.sys.set_password("pg", "new-pw")  # re-entered: works under the new key
		self.assertIn("app:new-pw@", self.sys.connector_url("pg"))
		self.assertNotIn("error", self.sys.key_status())
		self.sys.add_connector("pg2", self.pg)
		self.assertFalse(self.sys.set_password("pg2", None)["has_password"])


class WithoutCryptographyTest(_Base):
	def test_clear_message_and_nothing_stored(self):
		with mock.patch.dict(sys.modules, {"cryptography": None, "cryptography.fernet": None}):
			with self.assertRaisesRegex(TicketError, r"needs the cryptography package: pip install \"unblockd\[secrets\]\""):
				self.sys.add_connector("pg", self.pg)
			self.assertEqual(self.sys.connectors(), [])
			self.sys.add_connector("nopw", "postgres://app@127.0.0.1:1/db")  # no password: no cryptography needed
			self.assertEqual(self.sys.connector_url("nopw"), "postgres://app@127.0.0.1:1/db")


class SecretEnvFileTest(unittest.TestCase):
	NAMES = ("UNBLOCKD_TOKEN", "UNBLOCKD_WEBHOOK", "GITHUB_TOKEN", "UNBLOCKD_GITHUB_TOKEN", "UNBLOCKD_GIT_TOKEN", "ANTHROPIC_API_KEY",
	         "UNBLOCKD_BOARDS", "TICKETS_DIR", "UNBLOCKD_SYSTEM_DB", "UNBLOCKD_SECRET_KEY")

	def test_every_secret_env_var_is_read_with_its_file_form(self):
		src = "\n".join(p.read_text(encoding="utf-8") for p in (Path(__file__).parent.parent / "unblockd").glob("*.py"))
		for name in self.NAMES:
			self.assertNotRegex(src, rf"environ(\.get\(|\[)\"{name}\"", f"{name} must be read with system.env (NAME_FILE form)")
		with tempfile.TemporaryDirectory() as d:
			f = Path(d) / "tok"
			f.write_text("from-a-file\n", encoding="utf-8")
			for name in self.NAMES:
				with mock.patch.dict(os.environ, {name: "", name + "_FILE": str(f)}):
					self.assertEqual(sy.env(name), "from-a-file")

	def test_token_file_guards_the_api(self):
		from unblockd import web
		with tempfile.TemporaryDirectory() as d:
			f = Path(d) / "tok"
			f.write_text("tok-from-file\n", encoding="utf-8")
			srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
			threading.Thread(target=srv.serve_forever, daemon=True).start()
			try:
				url = f"http://127.0.0.1:{srv.server_address[1]}/api/boards"
				with mock.patch.dict(os.environ, {"UNBLOCKD_TOKEN": "", "UNBLOCKD_TOKEN_FILE": str(f)}):
					with self.assertRaises(urllib.error.HTTPError) as e:
						urllib.request.urlopen(url)
					self.assertEqual(e.exception.code, 401)
					ok = urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer tok-from-file"}))
					self.assertEqual(ok.status, 200)
			finally:
				srv.shutdown()
				srv.server_close()


class GeneratedKeyRaceTest(unittest.TestCase):
	def test_two_first_starts_share_one_key(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "secret.key"
			first = sk._generate(p)
			self.assertEqual(sk._generate(p), first)  # the second sees the file and reads it
			self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{64}", first))


if __name__ == "__main__":
	unittest.main()
