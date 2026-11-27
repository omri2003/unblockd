"""Perf guard (CORE-64): how many filesystem calls each GET route makes on a warm ~500-ticket board.

Wall-clock tests are flaky; counting is not. On a Docker Desktop bind mount every one of these calls costs ~0.6 ms
(docs/reference.md), so a route that starts touching the disk per ticket or per file again shows up here as a failed
budget instead of a slow board.

What counts as one call: os.stat / os.lstat / os.scandir / os.listdir / DirEntry.stat() and the os.path checks
(exists, isfile, isdir, islink, lexists, getsize, getmtime, realpath). Only the outermost one counts, so Path.exists()
is one call whether the Python version implements it with os.stat (3.12) or os.path.exists (3.13), and
Path.resolve() is one call although posix realpath lstats every path component. Reads (open) are not counted.

Budgets are about 1.5x the count measured on origin/main (the number in the comment), so a doubling fails. When a fix
makes a route cheaper, lower its budget to ~1.5x the new count. Print the counts:
    UNBLOCKD_PERF_PRINT=1 python -m unittest tests.test_perf
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from unblockd.boards import Boards
from unblockd.store import Store

N = 500  # tickets on the big board

# route -> (budget, measured on origin/main 1c0258d: Windows py3.13.0 / Linux py3.12.3 (WSL, native disk)).
# What the numbers say today: ~30 calls to find the board (Boards scans UNBLOCKD_ROOT on every call); a route that reads
# the tickets adds ~1 per ticket file (the cache's stamp check in Store._scan); GET tickets/<id> pays that twice and
# knowledge three times. Budgets = ceil(1.5 x the larger of the two), at least 2.
BUDGETS = {
	"/api/boards": (861, "574 / 564"),
	"/api/needs": (867, "578 / 568"),
	"/api/b/big/meta": (50, "33 / 29"),
	"/api/b/big/git": (50, "33 / 29"),
	"/api/b/big/next": (812, "541 / 537"),
	"/api/b/big/plan": (812, "541 / 537"),
	"/api/b/big/agents": (806, "537 / 533"),
	"/api/b/big/activity": (53, "35 / 31"),
	"/api/b/big/knowledge": (914, "609 / not measured: CORE-67 board with lib/tool.py"),
	"/api/b/big/files": (197, "124 / 131"),
	"/api/b/big/tools": (50, "33 / 29"),
	"/api/b/big/roadmap": (812, "541 / 537"),
	"/api/b/big/architecture": (924, "577 / 616"),
	"/api/b/big/doc?path=docs/guide.md": (60, "40 / 39"),
	"/api/b/big/tickets": (812, "541 / 537"),
	"/api/b/big/tickets?status=todo": (812, "541 / 537"),
	"/api/b/big/tickets?fields=touches,done_when&limit=20": (812, "541 / 537"),  # AI-47, CORE-52, AI-45 rows (CORE-92): measured on main + worker-turns
	"/api/b/big/tickets?limit=20": (812, "541 / 537"),
	"/api/b/big/tickets?touches=src/store/m1.py": (812, "541 / 537"),
	"/api/b/big/tickets?component=store": (824, "549 / 548"),
	"/api/b/big/docs?path=docs/guide.md&path=docs/api.md&path=docs/ui.md": (89, "54 / 59"),
	"/api/b/big/tickets/API-7": (1574, "1049 / 1045"),
	"/api/b/big/tickets/API-7/context": (887, "574 / 591"),
	"/api/b/big/tickets/API-7/context?content=0": (896, "577 / 597"),
	"/api/b/big/tickets/API-1/files/notes.txt": (51, "34 / 30"),
	"/api/tickets": (66, "44 / 40"),  # the default board: "other", 5 tickets
	"/": (2, "0 / 0"),
}


class Counter:
	"""Counts filesystem calls made while patched; nested calls (os.path.exists -> os.stat) count once."""

	def __init__(self):
		self.n = 0
		self.local = threading.local()

	def wrap(self, f):
		def counted(*a, **k):
			depth = getattr(self.local, "d", 0)
			if depth == 0:
				self.n += 1
			self.local.d = depth + 1
			try:
				return f(*a, **k)
			finally:
				self.local.d = depth
		return counted

	def scandir(self, real):
		counter = self

		class Entry:  # DirEntry is a C type: wrap it to count .stat()
			__slots__ = ("_e",)

			def __init__(self, e):
				self._e = e

			def __getattr__(self, k):
				return getattr(self._e, k)

			def __fspath__(self):
				return self._e.__fspath__()

			stat = property(lambda self: counter.wrap(self._e.stat))

		class It:
			def __init__(self, it):
				self.it = it

			def __iter__(self):
				return self

			def __next__(self):
				return Entry(next(self.it))

			def __enter__(self):
				return self

			def __exit__(self, *a):
				self.it.close()

			def close(self):
				self.it.close()

		return self.wrap(lambda *a, **k: It(real(*a, **k)))

	def patches(self):
		ps = [mock.patch.object(os, name, self.wrap(getattr(os, name))) for name in ("stat", "lstat", "listdir")]
		ps.append(mock.patch.object(os, "scandir", self.scandir(os.scandir)))
		ps += [mock.patch.object(os.path, name, self.wrap(getattr(os.path, name)))
		       for name in ("exists", "lexists", "isfile", "isdir", "islink", "getsize", "getmtime", "realpath")]
		import glob
		if hasattr(glob, "_StringGlobber"):  # 3.13+: pathlib's glob holds its own references to os.scandir / os.lstat
			ps.append(mock.patch.object(glob._StringGlobber, "scandir", staticmethod(self.scandir(os.scandir))))
			ps.append(mock.patch.object(glob._StringGlobber, "lstat", staticmethod(self.wrap(os.lstat))))
		return ps


def write(p: Path, text: str):
	p.parent.mkdir(parents=True, exist_ok=True)
	p.write_text(text, encoding="utf-8")


def make_project(root: Path) -> Path:
	"""A ~500-ticket board with docs, 4 architecture notes, source files, attachments, live claims and activity."""
	proj = root / "proj"
	tdir = proj / "tickets"
	cfg = {
		"id": "big", "title": "Big",
		"owners": [{"name": "Me", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
		"pillars": [{"key": "API"}, {"key": "UI"}],
		"agents": [{"name": "backend", "pillars": ["API"]}, {"name": "frontend", "pillars": ["UI"], "docs": ["docs/ui.md"]}],
		"docs": ["docs/guide.md"], "docs_by_tag": {"perf": ["docs/perf.md"]},
		"milestones": [{"key": "v1", "title": "One"}, {"key": "v2", "title": "Two"}],
		"review": True,
	}
	write(tdir / "unblockd.json", json.dumps(cfg))
	for name in ("guide", "perf", "ui", "api", *(f"topic{i}" for i in range(10))):
		write(proj / "docs" / f"{name}.md", f"# {name}\n\nSee [the guide](guide.md).\n\n## Part\n\nText.\n")
	arch = {"store": ("src/store/**", ["web"]), "web": ("src/web/**", ["store", "ui"]), "ui": ("src/ui/**", ["web"]), "cli": ("src/cli/**", ["store"])}
	for key, (paths, links) in arch.items():
		conn = "".join(f"- [{t}]({t}.md) — {key} calls {t}\n" for t in links)
		write(proj / "docs" / "arch" / f"{key}.md", f"---\npaths: {paths}\n---\n# {key}\nThe {key} part.\n\n## Connections\n{conn}")
	write(proj / "lib" / "tool.py", "x = 1\n")  # touched by every 20th ticket: no component, so those have no doc (suggest() works)
	write(proj / "docs" / "lib.md", "# Lib\n\nlib/tool.py holds the tool.\n")
	for part in arch:
		for i in range(10):
			write(proj / "src" / part / f"m{i}.py", "x = 1\n")
	statuses = ["todo"] * 5 + ["done"] * 3 + ["review", "doing"]
	for i in range(1, N + 1):
		pre = "API" if i % 2 else "UI"
		tid = f"{pre}-{i}"
		t = {
			"id": tid, "title": f"ticket {i}", "status": statuses[i % 10], "priority": f"P{i % 4}",
			"owner": "Me" if i % 7 == 0 else "Claude", "pillar": pre,
			"blocked_by": [f"{'UI' if i % 2 else 'API'}-{i - 1}"] if i % 3 == 0 and i > 1 else [],
			"parent": "API-1" if i % 50 == 0 else None,
			"tags": ["perf"] if i % 5 == 0 else [], "touches": [f"src/{list(arch)[i % 4]}/**" if i % 20 != 1 else "lib/tool.py"],
			"docs": ["docs/api.md"] if i % 4 == 0 else [], "components": ["store"] if i % 9 == 0 else [],
			"milestone": "v1" if i % 2 else "v2", "body": "Some text.", "created": "2026-11-01", "updated": "2026-11-10",
		}
		if t["status"] in ("doing", "review", "done"):
			t["blocked_by"] = []
		if i % 25 == 1 and t["status"] == "todo":
			t.update(waiting_on="Which one?", asked_by="backend-1", asked_at="2026-11-10T10:00:00+00:00")
		if t["status"] == "review":
			t["result"] = {"summary": "did it", "files": [], "refs": [], "by": "backend-1", "at": "2026-11-10T10:00:00+00:00"}
		if t["status"] == "done":
			t["done_at"] = "2026-11-10T10:00:00+00:00"
		write(tdir / f"{tid}.json", json.dumps(t))
	write(tdir / "files" / "API-1" / "notes.txt", "attached\n")
	s = Store(tdir)
	for agent in ("backend-1", "backend-2", "frontend-1"):
		s.claim(agent)
	return tdir


class PerfTest(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.tmp = tempfile.mkdtemp()
		root = Path(cls.tmp)
		make_project(root)
		other = root / "other" / "tickets"
		write(other / "unblockd.json", json.dumps({"id": "other", "title": "Other"}))
		for i in range(1, 6):
			write(other / f"OT-{i}.json", json.dumps({"id": f"OT-{i}", "title": f"other {i}", "status": "todo"}))
		from unblockd import web
		cls.web, cls.old = web, web.BOARDS
		web.BOARDS = Boards([str(root)])
		cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
		threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

	@classmethod
	def tearDownClass(cls):
		cls.srv.shutdown()
		cls.srv.server_close()
		cls.web.BOARDS = cls.old
		shutil.rmtree(cls.tmp, ignore_errors=True)

	def get(self, path):
		c = http.client.HTTPConnection("127.0.0.1", self.srv.server_port)
		try:
			c.request("GET", path)
			r = c.getresponse()
			body = r.read()
		finally:
			c.close()
		self.assertEqual(r.status, 200, f"{path}: {body[:300]!r}")
		return body

	def test_filesystem_calls_per_route(self):
		for path in BUDGETS:  # warm: the 4 s poll's steady state, not a cold start
			self.get(path)
			self.get(path)
		counter, got = Counter(), {}
		with ExitStack() as stack:
			for p in counter.patches():
				stack.enter_context(p)
			for path in BUDGETS:
				counter.n = 0
				self.get(path)
				got[path] = counter.n
		if os.environ.get("UNBLOCKD_PERF_PRINT"):
			print(f"\nfilesystem calls per warm GET ({sys.platform}, Python {sys.version.split()[0]}):", file=sys.stderr)
			for path, n in got.items():
				print(f"  {n:6}  {path}", file=sys.stderr)
		for path, (budget, measured) in BUDGETS.items():
			with self.subTest(route=path):
				self.assertLessEqual(got[path], budget, f"GET {path} made {got[path]} filesystem calls, budget {budget} "
				                     f"(measured {measured}). A route touching the disk per request again? See tests/test_perf.py.")

	def test_a_warm_knowledge_reads_no_doc(self):
		"""CORE-67: GET knowledge's suggestions come from the mention index kept per doc stamp; with no doc changed it opens none
		(it read every repo doc, up to 400, on every call). Opens are not in the call budgets above, so they are counted here."""
		import io
		self.get("/api/b/big/knowledge")
		n, real = [0], io.open

		def counted(f, *a, **k):
			if str(f).endswith((".md", ".txt")):
				n[0] += 1
			return real(f, *a, **k)

		with mock.patch.object(io, "open", counted):
			self.get("/api/b/big/knowledge")
		self.assertEqual(n[0], 0)


if __name__ == "__main__":
	unittest.main()
