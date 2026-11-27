"""Tests never touch the real system database (~/.unblockd/system.db): every run gets its own, removed at exit."""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("UNBLOCKD_SYSTEM_DB") and not os.environ.get("UNBLOCKD_SYSTEM_DB_FILE"):
	_tmp = tempfile.mkdtemp(prefix="unblockd-sysdb-")
	os.environ["UNBLOCKD_SYSTEM_DB"] = os.path.join(_tmp, "system.db")
	atexit.register(shutil.rmtree, _tmp, True)
