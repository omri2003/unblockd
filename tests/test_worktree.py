"""Git worktrees: every worktree of a repo shares the main checkout's lock and claims; the CLI never invents an empty board."""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from unblockd import cli
from unblockd.store import Store, TicketError, linked_worktree


def fake_worktree(root: Path) -> tuple[Path, Path]:
	"""The layout `git worktree add ../wt` leaves, without needing git."""
	main, wt = root / "main", root / "wt"
	gd = main / ".git" / "worktrees" / "wt"
	gd.mkdir(parents=True)
	(gd / "commondir").write_text("../..\n", encoding="utf-8")
	wt.mkdir()
	(wt / ".git").write_text(f"gitdir: {gd}\n", encoding="utf-8")
	return main, wt


class WorktreeTest(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.root = Path(self.tmp.name).resolve()

	def tearDown(self):
		self.tmp.cleanup()

	def test_worktrees_share_the_main_checkouts_claims(self):
		main, wt = fake_worktree(self.root)
		self.assertEqual(linked_worktree(wt / "tickets"), (wt, main))
		self.assertIsNone(linked_worktree(main / "tickets"))
		a = Store(main / "tickets")
		a.create({"title": "one"}, tid="A-1")
		(wt / "tickets").mkdir()
		shutil.copy(main / "tickets" / "A-1.json", wt / "tickets" / "A-1.json")  # the branch has the same ticket file
		b = Store(wt / "tickets")
		self.assertEqual(b.rt, main / "tickets" / ".unblockd")
		self.assertEqual(a.claim("w-1")["id"], "A-1")
		self.assertIsNone(b.claim("w-2"))  # the worktree sees the claim made in the main checkout: no double claim
		self.assertEqual(b.get("A-1")["claim"]["agent"], "w-1")
		self.assertFalse((wt / "tickets" / ".unblockd").exists())

	def test_submodule_and_plain_folders_keep_their_own_state(self):
		sub = self.root / "sub"
		(self.root / "repo" / ".git" / "modules" / "sub").mkdir(parents=True)  # a submodule's gitdir has no commondir
		sub.mkdir()
		(sub / ".git").write_text(f"gitdir: {self.root / 'repo' / '.git' / 'modules' / 'sub'}\n", encoding="utf-8")
		self.assertIsNone(linked_worktree(sub / "tickets"))
		self.assertEqual(Store(sub / "tickets").rt, sub / "tickets" / ".unblockd")

	def test_cli_refuses_a_missing_board(self):
		err = io.StringIO()
		with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
			self.assertEqual(cli.main(["--dir", str(self.root / "nope"), "list"]), 1)
		self.assertIn("no board at", err.getvalue())
		self.assertFalse((self.root / "nope").exists())

	@unittest.skipUnless(shutil.which("git"), "needs git")
	def test_real_git_worktree(self):
		main = self.root / "main"
		main.mkdir()
		git = lambda *a, cwd=main: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
		git("init", "-q")
		git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x")
		git("worktree", "add", "-q", str(self.root / "wt"))
		self.assertEqual(Store(self.root / "wt" / "tickets").rt.resolve(), (main / "tickets" / ".unblockd").resolve())


if __name__ == "__main__":
	unittest.main()
