"""Branch evidence (unblockd/branches.py + Store._branch_links): real git repos, an origin and two clones (the board's checkout
and a worker's), so what a worker pushes from elsewhere shows on the board without touching the board's files."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unblockd import branches
from unblockd.store import Store

HAS_GIT = bool(shutil.which("git"))
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Cloud Worker", "GIT_AUTHOR_EMAIL": "w@example.com", "GIT_COMMITTER_NAME": "Cloud Worker",
       "GIT_COMMITTER_EMAIL": "w@example.com", "GIT_TERMINAL_PROMPT": "0"}


def git(cwd, *args, env=None):
	return subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env or ENV,
	                      check=True, capture_output=True, text=True).stdout


class IdsTest(unittest.TestCase):
	def test_ids_in_names_and_subjects(self):
		known = {"NUT-85", "WEAR-34", "UI-12"}
		self.assertEqual(branches.ids_in("nut-85-recipe-coverage", known), ["NUT-85"])
		self.assertEqual(branches.ids_in("feat/WEAR_34-login", known), ["WEAR-34"])
		self.assertEqual(branches.ids_in("ui-123 and ui-012, dev2-ui-12", known), ["UI-12"])  # ui-123 is not UI-12; 012 is
		self.assertEqual(branches.ids_in("claude/confident-keller-3firif", known), [])
		self.assertEqual(branches.ids_in("wear-34 again wear-34", None), ["WEAR-34"])

	def test_token_goes_to_that_host_only_and_never_on_a_command_line(self):
		env = branches.git_env("https://github.com/o/r.git", "sekret")
		self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraHeader")
		self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
		self.assertNotIn("GIT_CONFIG_KEY_0", branches.git_env("git@github.com:o/r.git", "sekret"))  # ssh: its own keys
		self.assertNotIn("GIT_CONFIG_KEY_0", branches.git_env("http://intranet/r.git", "sekret"))   # never over plain http
		self.assertEqual(branches._clean_url("https://bob:pw@gitlab.com/o/r.git"), "https://gitlab.com/o/r.git")
		self.assertNotIn("GIT_CONFIG_KEY_0", branches.git_env("https://gitlab.com/o/r.git", None, "gh-token"))  # a GitHub token never goes to another host
		self.assertEqual(branches.git_env("https://github.com/o/r.git", None, "gh-token")["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraHeader")
		self.assertIn("GIT_CONFIG_KEY_0", branches.git_env("https://gitlab.com/o/r.git", "any-host-token", "gh-token"))


@unittest.skipUnless(HAS_GIT, "needs git")
class BranchesTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		r = Path(self.tmp.name).resolve()
		self.origin, self.board, self.worker = r / "origin.git", r / "board", r / "worker"
		git(r, "init", "--bare", "-q", str(self.origin))
		git(r, "clone", "-q", str(self.origin), str(self.board))
		s = Store(self.board / "tickets")
		(self.board / "tickets" / "unblockd.json").write_text('{"title": "Demo"}\n', encoding="utf-8")
		for i in range(1, 6):
			s.create({"title": f"ticket {i}"}, tid=f"WEAR-{i}")
		git(self.board, "add", "-A")
		git(self.board, "commit", "-qm", "board")
		git(self.board, "push", "-q", "origin", "HEAD:main")
		git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
		git(r, "clone", "-q", str(self.origin), str(self.worker))
		self.store = Store(self.board / "tickets")

	def tearDown(self):
		self.tmp.cleanup()

	def branch(self, name, files, msg, days_ago=0, base="main"):
		"""A commit on a new branch in the worker's clone, pushed to origin (the board's checkout never sees it)."""
		w = self.worker
		git(w, "checkout", "-q", "-B", name, f"origin/{base}" if base == "main" else base)
		for p, text in files.items():
			(w / p).parent.mkdir(parents=True, exist_ok=True)
			(w / p).write_text(text, encoding="utf-8")
		git(w, "add", "-A")
		when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
		git(w, "commit", "-qm", msg, env={**ENV, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})
		git(w, "push", "-q", "-f", "origin", f"HEAD:refs/heads/{name}")

	def sync(self):
		data = branches.Scanner().scan(self.store)
		branches.write(self.store.rt, data)
		return data

	def test_pushed_branch_shows_as_in_progress_without_touching_the_boards_files(self):
		self.branch("wear-1-login", {"src/a.txt": "a"}, "login form")
		before = sorted(p.name for p in (self.board / "tickets").glob("*.json"))
		data = self.sync()
		self.assertEqual(data["source"], "origin")
		self.assertEqual(data["base"], "main")
		t = self.store.get("WEAR-1")
		self.assertEqual(t["status"], "todo")  # stored status untouched
		self.assertEqual(t["effective_status"], "doing")
		b = t["branches"][0]
		self.assertEqual((b["name"], b["commits"], b["via"], b["author"], b["active"]), ("wear-1-login", 1, ["name"], "Cloud Worker", True))
		self.assertEqual(sorted(p.name for p in (self.board / "tickets").glob("*.json")), before)
		self.assertTrue((self.store.rt / "git" / "HEAD").is_file() and (self.store.rt / ".gitignore").read_text() == "*\n")
		self.assertEqual(git(self.board, "status", "--porcelain"), "")  # the user's repo: the mirror is ignored, no new refs
		self.assertNotIn("wear-1-login", git(self.board, "branch", "-a"))

	def test_ticket_edits_on_a_branch_are_reported_and_count_as_work(self):
		t = json.loads((self.board / "tickets" / "WEAR-3.json").read_text(encoding="utf-8"))
		t["status"], t["notes"] = "doing", [{"by": "w", "at": "x", "kind": "note", "text": "in PR #9"}]
		self.branch("misc-cleanup", {"tickets/WEAR-3.json": json.dumps(t, indent=2)}, "tidy")
		self.sync()
		b = self.store.get("WEAR-3")["branches"][0]
		self.assertEqual(b["via"], ["file"])
		self.assertEqual(b["edit"], {"status": "doing", "notes": 1})
		self.assertEqual(self.store.get("WEAR-3")["effective_status"], "doing")

	def test_a_commit_mention_is_listed_but_is_not_work_in_progress(self):
		self.branch("refactor", {"src/b.txt": "b"}, "split helper (prep for WEAR-2)")
		self.sync()
		t = self.store.get("WEAR-2")
		self.assertEqual(t["branches"][0]["via"], ["commit"])
		self.assertFalse(t["branches"][0]["active"])
		self.assertEqual(t["effective_status"], "todo")

	def test_merged_old_and_done_are_left_out(self):
		self.branch("wear-4", {"src/c.txt": "c"}, "c")
		git(self.worker, "push", "-q", "origin", "wear-4:main")  # merged (fast-forward)
		self.branch("wear-5-old", {"src/d.txt": "d"}, "d", days_ago=30)
		self.branch("wear-1", {"src/e.txt": "e"}, "e")
		self.store.update("WEAR-1", {"status": "done"})
		data = self.sync()
		self.assertEqual({b["name"] for b in data["branches"]}, {"wear-5-old", "wear-1"})
		self.assertEqual(self.store.get("WEAR-4")["branches"], [])
		old = self.store.get("WEAR-5")
		self.assertFalse(old["branches"][0]["active"])  # listed, but 30 days quiet is not in progress
		self.assertEqual(old["effective_status"], "todo")
		self.assertEqual(self.store.get("WEAR-1")["branches"], [])  # done tickets carry no branches

	def test_claim_next_skips_branch_work_and_claim_by_id_continues_it(self):
		self.branch("wear-1", {"src/a.txt": "a"}, "a")
		self.branch("wear-2", {"src/b.txt": "b"}, "b")
		self.sync()
		got = self.store.claim_next("w-1")
		self.assertNotIn(got["id"], ("WEAR-1", "WEAR-2"))
		t = self.store.claim("w-2", tid="WEAR-1")
		self.assertEqual(t["status"], "doing")
		self.assertTrue(any("branch wear-1 already has 1 commit" in w for w in t["warnings"]))
		for tid in ("WEAR-3", "WEAR-4", "WEAR-5"):
			if tid != got["id"]:
				self.store.claim("w-3", tid=tid)
		dry = self.store.claim_next("w-4")
		self.assertIsNone(dry["ticket"])
		self.assertIn("WEAR-2", dry["ids"]["in_flight"])
		self.assertNotIn("WEAR-2", dry["orphaned"])

	def test_moving_it_back_to_todo_dismisses_that_tip_until_a_new_commit(self):
		self.branch("wear-1", {"src/a.txt": "a"}, "a")
		self.sync()
		self.store.update("WEAR-1", {"status": "todo"})
		t = self.store.get("WEAR-1")
		self.assertEqual(t["effective_status"], "todo")
		self.assertFalse(t["branches"][0]["active"])
		git(self.worker, "commit", "-q", "--allow-empty", "-m", "more")
		git(self.worker, "push", "-q", "origin", "HEAD:refs/heads/wear-1")
		self.sync()
		self.assertEqual(self.store.get("WEAR-1")["effective_status"], "doing")

	def test_checkout_behind_origin_is_reported(self):
		(self.worker / "x.txt").write_text("x", encoding="utf-8")
		git(self.worker, "checkout", "-q", "main")
		git(self.worker, "add", "-A")
		git(self.worker, "commit", "-qm", "merged elsewhere")
		git(self.worker, "push", "-q", "origin", "main")
		t = json.loads((self.worker / "tickets" / "WEAR-1.json").read_text(encoding="utf-8"))
		(self.worker / "tickets" / "WEAR-9.json").write_text(json.dumps({**t, "id": "WEAR-9"}), encoding="utf-8")
		git(self.worker, "add", "-A")
		git(self.worker, "commit", "-qm", "a ticket made elsewhere")
		git(self.worker, "push", "-q", "origin", "main")
		c = self.sync()["checkout"]
		self.assertEqual((c["branch"], c["behind"], c["ahead"], c["missing"]), ("main", 2, 0, ["WEAR-9"]))
		(self.board / "y.txt").write_text("y", encoding="utf-8")
		git(self.board, "add", "y.txt")
		git(self.board, "commit", "-qm", "not pushed")
		c = self.sync()["checkout"]
		self.assertEqual((c["behind"], c["ahead"]), (2, 1))  # diverged: still counted from the last commit both have

	def test_no_origin_reads_the_checkouts_own_refs(self):
		git(self.board, "remote", "remove", "origin")
		git(self.board, "branch", "wear-2-local")
		git(self.board, "checkout", "-q", "wear-2-local")
		(self.board / "z.txt").write_text("z", encoding="utf-8")
		git(self.board, "add", "z.txt")
		git(self.board, "commit", "-qm", "z")
		git(self.board, "checkout", "-q", "main")
		data = self.sync()
		self.assertEqual(data["source"], "local")
		self.assertIn("no origin", data["error"])
		self.assertEqual(self.store.get("WEAR-2")["effective_status"], "doing")

	def test_unchanged_scan_does_not_rewrite_the_file(self):
		self.branch("wear-1", {"src/a.txt": "a"}, "a")
		self.sync()
		stamp = (self.store.rt / "branches.json").stat().st_mtime_ns
		self.assertFalse(branches.write(self.store.rt, branches.Scanner().scan(self.store)))
		self.assertEqual((self.store.rt / "branches.json").stat().st_mtime_ns, stamp)

	def test_not_a_repository_is_an_error_not_a_crash(self):
		s = Store(Path(self.tmp.name) / "plain" / "tickets")
		with self.assertRaises(branches.GitError):
			branches.Scanner().scan(s)
		self.assertEqual(s.list(), [])


if __name__ == "__main__":
	unittest.main()
