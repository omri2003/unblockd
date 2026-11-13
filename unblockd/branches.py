"""Branch evidence from git: which tickets have work on a branch that is not merged yet. Works with any git host (or none).

Why: a worker in another clone (a cloud session, a container, a teammate's laptop) can't reach this board's files, and the
ticket edits it commits on its branch only reach the board when the branch is merged and the board's checkout pulls. The
branch itself is the shared evidence, so the board reads it. Same shape as notify.py / github.py: one daemon thread in the
web server. Per board, every INTERVAL s:

1. Refresh a private bare mirror of the project's `origin` in <rt>/git (a blob-less partial clone: commits and trees, the
   few ticket files it reads are fetched on demand). The user's own repository is never written. No origin, or the fetch
   failed (offline, no credentials): the user's repository's own refs are read instead, and `error` says why.
2. For every branch not merged into the default branch: its tip, last commit time, author, commit count, and the ticket ids
   it is linked to by its NAME (wear-34, feat/WEAR-34-login), its commit SUBJECTS, or ticket FILES it edits (with the
   status / note count it has there).
3. Write <rt>/branches.json when something changed. The store derives from it (Store._decorate: `branches`, and
   effective_status doing for a todo ticket with an active branch); no ticket file is written.

Off with UNBLOCKD_GIT=0, or when there is no git binary. UNBLOCKD_GIT_TOKEN = a token for a private https remote on any host;
GITHUB_TOKEN is used too, but only for a github.com remote. Sent only to that host, through git's environment (GIT_CONFIG_*),
never on a command line.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .store import ID_RE

INTERVAL = 120
MAX_BRANCHES = 200   # newest first; a repo with more open branches than this keeps only the newest
MAX_COMMITS = 50     # subjects read per branch
MAX_EDITS = 20       # ticket files read per branch
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)[-_](\d+)(?![0-9])")


class GitError(Exception):
	pass


class NotRepo(GitError):
	"""The project is not in a git repository: no evidence, and nothing is written."""


def load(rt: Path) -> dict:
	"""branches.json from a board's runtime folder (Store.rt), {} when absent or unreadable."""
	try:
		return json.loads((rt / "branches.json").read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}


def ids_in(text: str, known=None) -> list[str]:
	"""Ticket ids named in text (only this board's, when `known` is given), in order, once each: nut-85-recipe -> NUT-85, feat/WEAR_34 -> WEAR-34."""
	out = []
	for m in TOKEN_RE.finditer(text or ""):
		tid = f"{m[1].upper()}-{int(m[2])}"
		if (known is None or tid in known) and tid not in out:
			out.append(tid)
	return out


def _clean_url(url: str) -> str:
	"""A remote URL without user:password (it is written to branches.json and shown in the UI)."""
	p = urlsplit(url)
	if p.scheme in ("http", "https") and "@" in p.netloc:
		return urlunsplit(p._replace(netloc=p.netloc.rsplit("@", 1)[1]))
	return url


def git_env(url: str, token: str | None, github_token: str | None = None) -> dict:
	"""git's environment: never prompt (a background thread has no one to answer), and, for an https remote, a token as an
	Authorization header scoped to that host. Through GIT_CONFIG_* so it never shows in a process list. `token`
	(UNBLOCKD_GIT_TOKEN) is for whatever host origin is on; `github_token` (GITHUB_TOKEN) only ever goes to github.com."""
	env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_OPTIONAL_LOCKS": "0"}
	p = urlsplit(url)
	token = token or (github_token if p.hostname == "github.com" else None)
	if token and p.scheme == "https" and p.hostname:
		cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
		env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.https://{p.hostname}/.extraHeader",
		            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {cred}"})
	return env


def _git(cwd: Path, *args: str, env: dict | None = None, timeout: int = 60, ok=(0,)) -> str:
	try:
		r = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, capture_output=True, text=True,
		                   encoding="utf-8", errors="replace", timeout=timeout, env=env or {**os.environ, "GIT_TERMINAL_PROMPT": "0"})
	except (OSError, subprocess.TimeoutExpired) as e:
		raise GitError(f"git {args[0]}: {e}") from None
	if r.returncode not in ok:
		raise GitError(f"git {args[0]}: {(r.stderr or r.stdout).strip().splitlines()[-1][:300] if (r.stderr or r.stdout).strip() else 'exit ' + str(r.returncode)}")
	return r.stdout


class Scanner:
	def __init__(self, token: str | None = None, github_token: str | None = None):
		self.token, self.github_token = token, github_token
		self.memo: dict[str, dict] = {}  # (repo, sha, base sha, tickets path) -> details; a branch is re-read only when it moved
		self.warned: set[str] = set()     # boards whose current error was printed (once per streak)

	def scan(self, store) -> dict:
		"""The branches.json content for one board (raises GitError when the project is not a git repository)."""
		root = store.dir.parent
		try:
			top = Path(_git(root, "rev-parse", "--show-toplevel").strip())
		except GitError as e:
			raise NotRepo(str(e)) from None
		rel = store.dir.resolve().relative_to(top.resolve()).as_posix()
		cur = _git(top, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1)).strip()
		url = _git(top, "remote", "get-url", "origin", ok=(0, 2)).strip()
		if url and "://" not in url and not re.match(r"^[\w.-]+@[\w.-]+:", url):  # a local path, maybe relative to the repo
			url = str((top / url).resolve())
		out = {"source": "local", "remote": _clean_url(url) if url else None, "base": None, "checkout": None, "error": None, "branches": []}
		repo, prefixes = top, ("refs/heads", "refs/remotes/origin")
		if url:
			env = git_env(url, self.token, self.github_token)
			try:
				repo = self._mirror(store._rt(), url, env)
				out["source"], prefixes = "origin", ("refs/heads",)
			except GitError as e:
				out["error"] = f"could not read origin, showing this checkout's branches only: {e}"
				repo = top
		else:
			env = None
			out["error"] = "no origin remote: showing this checkout's branches only"
		if out["source"] == "origin":
			base = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1)).strip() or cur or "main"
			base_ref = f"refs/heads/{base}"
		else:
			o = _git(top, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD", ok=(0, 1)).strip()  # origin/main
			base, base_ref = (o.split("/", 1)[1], f"refs/remotes/{o}") if o else (cur or "HEAD", cur and f"refs/heads/{cur}" or "HEAD")
		out["base"] = base
		base_sha = _git(repo, "rev-parse", "--verify", "-q", base_ref + "^{commit}", ok=(0, 1)).strip()
		if not base_sha:
			raise GitError(f"no branch {base} to compare with")
		if out["source"] == "origin" and cur:
			out["checkout"] = self._checkout(repo, top, cur, base, rel, store.dir)
		known = {t["id"] for t in store._all_raw()}
		fmt = "%(refname)%09%(objectname)%09%(committerdate:unix)%09%(authorname)"
		rows = _git(repo, "for-each-ref", f"--no-merged={base_sha}", f"--format={fmt}", *prefixes).splitlines()
		seen: dict[str, tuple] = {}
		for row in rows:
			ref, sha, ts, author = (row.split("\t") + ["", "", "", ""])[:4]
			name = ref.split("/", 3)[3] if ref.startswith("refs/remotes/") else ref[len("refs/heads/"):]
			if name in ("HEAD", base) or (out["source"] == "local" and name == cur):
				continue  # the checkout's own branch is what the board already shows
			if name not in seen or int(ts or 0) > int(seen[name][1] or 0):
				seen[name] = (sha, ts, author)
		keep = set()
		for name, (sha, ts, author) in sorted(seen.items(), key=lambda kv: -int(kv[1][1] or 0))[:MAX_BRANCHES]:
			keep.add(sha)  # memo kept for every branch seen, linked or not, so an unlinked branch is not re-read each poll
			d = self._details(repo, env, sha, base_sha, rel, known)
			links: dict[str, list[str]] = {}
			for tid in ids_in(name, known):
				links.setdefault(tid, []).append("name")
			for tid in d["commit_ids"]:
				links.setdefault(tid, []).append("commit")
			for tid in d["edits"]:
				links.setdefault(tid, []).append("file")
			if not links:
				continue
			out["branches"].append({"name": name, "sha": sha, "at": datetime.fromtimestamp(int(ts or 0), timezone.utc).isoformat(timespec="seconds"),
			                        "author": author, "commits": d["commits"], "tickets": links, "edits": d["edits"]})
		self.memo = {k: v for k, v in self.memo.items() if k.split("|")[1] in keep or not k.startswith(str(repo) + "|")}
		return out

	def _mirror(self, rt: Path, url: str, env: dict) -> Path:
		m = rt / "git"
		if not (m / "HEAD").is_file():
			shutil.rmtree(m, ignore_errors=True)  # a half-made clone from a crash
			rt.mkdir(parents=True, exist_ok=True)
			_git(rt, "clone", "--bare", "--filter=blob:none", "--no-tags", "--quiet", url, str(m), env=env, timeout=600)
		else:
			_git(m, "fetch", "--prune", "--no-tags", "--quiet", "origin", "+refs/heads/*:refs/heads/*", env=env, timeout=300)
		return m

	def _checkout(self, mirror: Path, top: Path, cur: str, base: str, rel: str, tdir: Path) -> dict:
		"""How the board's checkout compares with its branch on origin. The board shows the checkout's files, so work merged on
		origin appears only after a pull: behind = commits origin has that the checkout lacks, ahead = local commits never pushed,
		missing = ticket files on origin that this checkout doesn't have yet (ids, at most 20, plus missing_count).
		Counted from the newest local commit origin also has, so a checkout with unpushed commits still gets a number."""
		up = cur if _git(mirror, "rev-parse", "--verify", "-q", f"refs/heads/{cur}", ok=(0, 1)).strip() else base
		c = {"branch": cur, "upstream": up, "behind": None, "ahead": None, "missing": [], "missing_count": 0}
		local = _git(top, "rev-list", "-n", "500", "HEAD").split()
		ours = set(_git(mirror, "rev-list", "--max-count=50000", f"refs/heads/{up}").split())  # no object lookups: a partial clone would fetch missing ones (git < 2.45 can't be told not to)
		fork = next((i for i, sha in enumerate(local) if sha in ours), None)
		if fork is not None:
			c["ahead"] = fork
			c["behind"] = int(_git(mirror, "rev-list", "--count", f"{local[fork]}..refs/heads/{up}").strip() or 0)
		there = {Path(p).name[:-5] for p in _git(mirror, "ls-tree", "--name-only", f"refs/heads/{up}", "--", rel + "/").splitlines()
		         if p.endswith(".json") and ID_RE.match(Path(p).name[:-5])}
		here = {p.stem for p in tdir.glob("*.json")}
		gone = sorted(there - here, key=lambda x: (x.split("-")[0], int(re.sub(r"\D", "", x.split("-")[1]) or 0)))
		c["missing"], c["missing_count"] = gone[:20], len(gone)
		return c

	def _details(self, repo: Path, env, sha: str, base: str, rel: str, known) -> dict:
		key = f"{repo}|{sha}|{base}|{rel}"
		if key not in self.memo:  # cached with every id-shaped name, filtered per call: a ticket created later still links
			self.memo[key] = self._read(repo, env, sha, base, rel)
		d = self.memo[key]
		return {**d, "commit_ids": [x for x in d["commit_ids"] if x in known], "edits": {k: v for k, v in d["edits"].items() if k in known}}

	def _read(self, repo: Path, env, sha: str, base: str, rel: str) -> dict:
		commits = int(_git(repo, "rev-list", "--count", f"{base}..{sha}").strip() or 0)
		subjects = _git(repo, "log", "--format=%s", f"-n{MAX_COMMITS}", f"{base}..{sha}")
		ids = ids_in(subjects)
		edits = {}
		try:
			names = _git(repo, "diff", "--name-only", "--no-renames", f"{base}...{sha}", "--", rel + "/").splitlines()
		except GitError:
			names = []  # no merge base (unrelated history): no file evidence
		for p in names:
			stem = p[len(rel) + 1:-5] if p.startswith(rel + "/") and p.endswith(".json") and "/" not in p[len(rel) + 1:] else ""
			if not ID_RE.match(stem) or len(edits) >= MAX_EDITS:
				continue
			try:
				now = json.loads(_git(repo, "show", f"{sha}:{p}", env=env))
				on_base = _git(repo, "ls-tree", "--name-only", base, "--", p).strip()  # trees are local; only blobs are fetched
				was = json.loads(_git(repo, "show", f"{base}:{p}", env=env)) if on_base else {}
			except (GitError, ValueError):
				continue  # a blob we could not fetch, or a file that isn't JSON on the branch: the link stays, without details
			n0, n1 = len(was.get("notes") or []), len(now.get("notes") or [])
			edits[stem] = {"status": now.get("status"), "notes": max(0, n1 - n0)}
		return {"commits": commits, "commit_ids": ids, "edits": edits}


class Syncer:
	def __init__(self, boards, token: str | None = None, github_token: str | None = None):
		self.boards, self.scanner = boards, Scanner(token, github_token)

	def poll(self) -> None:
		for bid in self.boards.all():
			try:
				store = self.boards.get(bid)
			except Exception as e:  # noqa: BLE001  one broken board must not stop the others
				print(f"branches: {bid}: {e}", flush=True)
				continue
			try:
				data = self.scanner.scan(store)
			except NotRepo:
				continue
			except GitError as e:
				data = {"error": str(e), "branches": []}
			if data.get("error") and bid not in self.scanner.warned:
				print(f"branches: {bid}: {data['error']}", flush=True)
				self.scanner.warned.add(bid)
			elif not data.get("error"):
				self.scanner.warned.discard(bid)
			write(store._rt(), data)


def write(rt: Path, data: dict) -> bool:
	"""Write branches.json only when it changed (the store re-derives the board on every change of its stamp)."""
	old = load(rt)
	if {k: v for k, v in old.items() if k != "since"} == data:
		return False
	rt.mkdir(parents=True, exist_ok=True)
	tmp = rt / "branches.json.tmp"
	tmp.write_text(json.dumps({**data, "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}, indent=1), encoding="utf-8")
	os.replace(tmp, rt / "branches.json")
	return True


def start(boards) -> Syncer | None:
	if os.environ.get("UNBLOCKD_GIT", "1").lower() in ("0", "off", "false", "no"):
		return None
	if not shutil.which("git"):
		print("branches: off, no git binary (rebuild the image: docker compose up -d --build)", flush=True)
		return None
	s = Syncer(boards, os.environ.get("UNBLOCKD_GIT_TOKEN"), os.environ.get("GITHUB_TOKEN") or os.environ.get("UNBLOCKD_GITHUB_TOKEN"))

	def loop():
		while True:
			try:
				s.poll()
			except Exception as e:  # noqa: BLE001  the thread must survive anything
				print(f"branches: {e}", flush=True)
			if threading.Event().wait(INTERVAL):
				break
	threading.Thread(target=loop, daemon=True, name="branches").start()
	return s
