"""The secret key, and encrypting the passwords kept in the system database (CORE-41).

The key: `UNBLOCKD_SECRET_KEY` (or `UNBLOCKD_SECRET_KEY_FILE`, Docker / Kubernetes secrets), at least 32 characters. Unset,
with a SQLite system database: generated when the FIRST password is stored (never on start: no password, no key), saved as
`secret.key` next to it, said once on stderr ("back it up separately from the system database": one backup holding both
defeats the encryption), then reused. A Postgres system database never gets a generated key (2026-11-27 review: each server
would make its own in its home folder, a container's is gone on redeploy, and two servers would break each other's
key_check): storing a password there is refused until every server is given the same UNBLOCKD_SECRET_KEY.

Encryption is Fernet from the `cryptography` package (owner decision 2026-11-08: no hand-rolled cipher; Python's standard
library has none). It is an optional dependency (`pip install "unblockd[secrets]"`, part of `[db]`, requirements-db.txt
and the Docker image), imported only when a password is stored or read. The Fernet key is derived from the secret key
(SHA-256), so the secret key can be any long random string.

A wrong or lost key is a clear TicketError naming what to do (put the old key back, or re-enter the passwords), never a
crash: the system database keeps `key_check` (a known text encrypted with the key) in us_meta to tell the two apart.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path

from .errors import TicketError

ENV = "UNBLOCKD_SECRET_KEY"
MIN_LEN = 32
CHECK = "unblockd key check"


def key_file(system) -> Path:
	"""Where a generated key lives: next to a SQLite system database, else ~/.unblockd/secret.key."""
	if system.url.startswith("sqlite:///"):
		return Path(system.url[len("sqlite:///"):]).parent / "secret.key"
	return Path.home() / ".unblockd" / "secret.key"


def source(system) -> str:
	"""Where the key comes from, for messages (never the key itself)."""
	if os.environ.get(ENV):
		return ENV
	if os.environ.get(ENV + "_FILE"):
		return f"{ENV}_FILE ({os.environ[ENV + '_FILE']})"
	return key_file(system).as_posix()


def key(system, create: bool = False) -> str:
	"""The secret key: from the environment, else the saved one. create (storing a password): else a new one, saved and then
	reused; only next to a SQLite system database. Without create a missing key is the lost-key error."""
	from .system import env  # noqa: PLC0415
	k = env(ENV)
	if k is None:
		p = key_file(system)
		try:
			k = p.read_text(encoding="utf-8").strip()
		except FileNotFoundError:
			if not create:
				raise TicketError(lost(system)) from None
			if not system.url.startswith("sqlite:///"):
				raise TicketError(f"storing a password with a Postgres system database needs {ENV} (or {ENV}_FILE) set to the same "
				                  "value on every server: a key made by one server would be lost on redeploy and differ between servers. "
				                  "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(48))\"") from None
			k = _generate(p)
		except OSError as e:
			raise TicketError(f"can't read the secret key {p.as_posix()} ({e.strerror or e}): fix its permissions, or set {ENV}") from None
		where = p.as_posix()
	else:
		where = ENV
	if len(k) < MIN_LEN:
		raise TicketError(f"the secret key ({where}) is {len(k)} characters; use at least {MIN_LEN} random ones, "
		                  "e.g. python -c \"import secrets; print(secrets.token_urlsafe(48))\"")
	return k


def _generate(p: Path) -> str:
	p.parent.mkdir(parents=True, exist_ok=True)
	k = secrets.token_urlsafe(48)
	try:
		fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # two first starts at once: one writes, the other reads it
	except FileExistsError:
		return p.read_text(encoding="utf-8").strip()
	with os.fdopen(fd, "w", encoding="utf-8") as f:
		f.write(k + "\n")
	print(f"unblockd: new secret key at {p.as_posix()}: back it up, separately from the system database (a backup holding both "
	      f"defeats the encryption), or set {ENV}_FILE from a secrets manager", file=sys.stderr, flush=True)
	return k


def _fernet(system, create: bool = False):
	try:
		from cryptography.fernet import Fernet  # noqa: PLC0415 - optional: only stored passwords need it
	except ImportError:
		raise TicketError("storing or reading a connector password needs the cryptography package: pip install \"unblockd[secrets]\" "
		                  "(or pip install -r requirements-db.txt; the Docker image includes it)") from None
	return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key(system, create).encode("utf-8")).digest()))


def lost(system) -> str:
	return (f"the secret key ({source(system)}) is not the one the stored connector passwords were encrypted with (it changed, "
	        "or the saved key was lost). Put the old key back, or re-enter each password: "
	        "python -m unblockd.cli system set-password NAME --password-stdin")


def encrypt(system, text: str) -> str:
	return _fernet(system, create=True).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(system, token: str) -> str:
	f = _fernet(system)
	from cryptography.fernet import InvalidToken  # noqa: PLC0415
	try:
		return f.decrypt(token.encode("ascii")).decode("utf-8")
	except (InvalidToken, ValueError):
		raise TicketError(lost(system)) from None
