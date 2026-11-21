"""DOCS-21: scripts/release_notes.py on a tiny real git repo + board: PRs joined to tickets, groups, flags."""
import importlib.util
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from unblockd.store import Store

ROOT = Path(__file__).resolve().parent.parent
HAS_GIT = bool(shutil.which("git"))
ENV = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T",
       "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_TERMINAL_PROMPT": "0"}
OLD = {**ENV, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def git(cwd, *args, env=None):
	return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
	                       "-c", "tag.gpgsign=false", *args], cwd=cwd, env=env or ENV, check=True, capture_output=True, text=True).stdout


def _load():
	spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts" / "release_notes.py")
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


@unittest.skipUnless(HAS_GIT, "needs git")
class ReleaseNotesTest(unittest.TestCase):
	def setUp(self):
		self.rn = _load()
		self.tmp = Path(tempfile.mkdtemp())
		self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
		r = self.repo = self.tmp / "repo"
		r.mkdir()
		git(r, "init", "-q")
		(r / "a.txt").write_text("0\n")
		git(r, "add", "-A")
		git(r, "commit", "-qm", "first", env=OLD)
		git(r, "tag", "v0.1.0")
		self.pr(5, "feat/claims", "Faster claims", "store: claim reads once (CORE-1)")
		self.pr(6, "fix/crash", "Fix a crash on start", "web: no crash")
		self.pr(7, "docs/readme", "README wording", "docs: words")
		self.pr(8, "feat/db", "Database boards", "sql: connector")
		git(r, "commit", "-q", "--allow-empty", "-m", "straight on main (CORE-9)")  # not a PR: never listed
		s = self.store = Store(self.tmp / "tickets")
		s.create({"title": "Claims read the board once"}, prefix="CORE")  # CORE-1: id in the PR's commit
		s.claim("w-1", "CORE-1")
		s.complete("CORE-1", "w-1", "Cause: three reads under the lock. Fix: one read per claim. Tests: test_claims.")
		s.create({"title": "Crash on start", "tags": ["bug"]}, prefix="BUG")  # BUG-1: joined by a note
		s.add_note("BUG-1", "omri", "Merged in PR #6 (https://github.com/o/r/pull/6)")
		s.claim("w-1", "BUG-1")
		s.complete("BUG-1", "w-1", "Start no longer crashes when the folder is empty.\nmore lines")
		s.create({"title": "Tickets in a database", "tags": ["experimental"]}, prefix="CORE")  # CORE-2: refs name the branch, no result
		s.create({"title": "Done by hand, no PR", "status": "done"}, prefix="CORE")  # CORE-3
		s.create({"title": "Still open"}, prefix="CORE")  # CORE-4
		s.create({"title": "Mentioned by a direct commit", "status": "done"}, prefix="CORE")  # CORE-5

	def pr(self, n, branch, title, commit):
		r = self.repo
		git(r, "checkout", "-q", "-b", branch)
		(r / f"{n}.txt").write_text(str(n))
		git(r, "add", "-A")
		git(r, "commit", "-qm", commit)
		git(r, "checkout", "-q", "main")
		git(r, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from me/{branch}", "-m", title)

	def test_draft(self):
		d = self.rn.draft(self.repo, self.store, "v0.1.0", "HEAD")
		self.assertEqual([p["number"] for p in d["prs"]], [5, 6, 7, 8])
		added = {i["id"]: i for i in d["groups"]["Added"]}
		self.assertEqual(added["CORE-1"]["text"], "one read per claim.")  # Cause: ... Fix: <first sentence>
		self.assertEqual(added["CORE-1"]["pr"], 5)
		fixed = d["groups"]["Fixed"]
		self.assertEqual([(i["id"], i["pr"], i["text"]) for i in fixed], [("BUG-1", 6, "Start no longer crashes when the folder is empty.")])
		self.assertEqual([p["number"] for p in d["pr_no_ticket"]], [7, 8])  # CORE-2 has no result and no ref yet
		self.assertEqual(sorted(t["id"] for t in d["done_no_pr"]), ["CORE-3", "CORE-5"])  # a commit straight on main is no PR

	def test_refs_branch_join_no_result_and_experimental(self):
		s = self.store
		s.claim("w-1", "CORE-2")
		s.complete("CORE-2", "w-1", "Cause: nothing in a database.", refs=["branch feat/db, .claude/commit.sh"])  # no Fix: part
		d = self.rn.draft(self.repo, s, "v0.1.0", "HEAD")
		item = {i["id"]: i for i in d["groups"]["Added"]}["CORE-2"]
		self.assertEqual((item["pr"], item["text"], item["experimental"]), (8, "Tickets in a database", True))
		self.assertEqual(d["no_result"], [{"id": "CORE-2", "title": "Tickets in a database", "pr": 8}])
		self.assertEqual([p["number"] for p in d["pr_no_ticket"]], [7])
		text = self.rn.render(d)
		self.assertIn("- Tickets in a database (experimental) (CORE-2, #8)", text)
		self.assertIn("- one read per claim. (CORE-1, #5)\n  ticket: Claims read the board once", text)
		self.assertIn("PRs with no ticket (1):\n- #7 README wording (docs/readme)", text)
		self.assertIn("## Fixed", text)
		self.assertNotIn("## Breaking", text)  # empty groups left out

	def test_groups(self):
		g = self.rn.group_of
		self.assertEqual(g({"id": "UI-1", "title": "x", "tags": ["breaking"]}), "Breaking")
		self.assertEqual(g({"id": "UI-1", "title": "Bug: y"}), "Fixed")
		self.assertEqual(g({"id": "BUG-1", "title": "y"}), "Fixed")
		self.assertEqual(g({"id": "CORE-1", "title": "y", "tags": ["perf"]}), "Changed")
		self.assertEqual(g({"id": "DOCS-1", "title": "y"}), "Changed")
		self.assertEqual(g({"id": "CORE-1", "title": "Fixtures for tests"}), "Added")  # "fix" inside a word is not a fix

	def test_main_refuses_a_missing_board(self):
		from contextlib import redirect_stderr
		with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
			self.rn.main(["v0.1.0", "--repo", str(self.repo), "--board", str(self.tmp / "nope")])
		self.assertIn("no board at", err.getvalue())
		self.assertFalse((self.tmp / "nope").exists())

	def test_main_prints_the_draft(self):
		(self.tmp / "tickets" / "unblockd.json").write_text("{}")
		out = io.StringIO()
		with redirect_stdout(out):
			self.assertEqual(self.rn.main(["v0.1.0", "--repo", str(self.repo), "--board", str(self.tmp / "tickets")]), 0)
		self.assertTrue(out.getvalue().startswith("# Draft release notes: v0.1.0..HEAD\n\n4 merged PRs, 2 tickets."))



class ReleaseBodyTest(unittest.TestCase):
	CHANGELOG = "# Changelog\n\n## Unreleased\n\n## v0.2.0 (2026-11-23)\n\nIntro.\n\n### Added\n- a\n\n## v0.1.0 (2026-10-11)\n\n- old\n"

	def test_body_is_that_section_plus_how_to_get_it(self):
		body = _load().release_body(self.CHANGELOG, "v0.2.0")
		self.assertTrue(body.startswith("Intro.\n\n### Added\n- a\n\n### Get it\n"), body)
		self.assertIn("docker pull ghcr.io/omri2003/unblockd:0.2.0", body)
		self.assertNotIn("old", body)
		self.assertEqual(_load().release_body(self.CHANGELOG, "v0.1.0").split("\n\n### Get it")[0], "- old")

	def test_missing_section_says_what_to_do(self):
		with self.assertRaises(SystemExit) as e:
			_load().release_body(self.CHANGELOG, "v0.3.0")
		self.assertIn("moves Unreleased under it", str(e.exception))
		with self.assertRaises(SystemExit):
			_load().release_body(self.CHANGELOG, "v0.2")  # v0.2 must not match the v0.2.0 heading


if __name__ == "__main__":
	unittest.main()
