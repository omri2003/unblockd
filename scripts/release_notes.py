"""Draft release notes from data (DOCS-21): merged PRs since a tag, joined to the board's tickets.

python scripts/release_notes.py v0.1.0 [--to origin/main] [--board tickets] [--repo .] > draft.md
python scripts/release_notes.py --body v0.2.0 > release-body.md   (the GitHub Release body: that CHANGELOG section + image)

The draft is for a person to edit (docs/releasing.md); the script writes no file. Stdlib only; the board is read through
unblockd.store.Store, so any board location works (folder or database URL). Store.list() sweeps expired claims (a lapsed
claim goes back to todo and is logged), so run it on a copy of the board, or accept that it tidies the real one.

- PRs = merge commits on the first-parent line of --to since the tag whose subject is "Merge pull request #N from owner/branch";
  the PR title is the merge body's first line.
- Squash- or rebase-merged PRs = other (non-merge) commits on that line whose subject ends with "(#N)": joined to tickets the
  same way (their own message is the PR's text) and flagged under Needs a look, since only that one commit is seen.
- A ticket belongs to PR #N when any of these holds:
  its id is in the PR title or in a commit subject / body inside the PR (the "(CORE-84)" convention);
  its result.refs name the PR (".../pull/N", "#N", "PR N") or its branch ("branch prod/x");
  one of its notes says the PR merged it ("Merged in PR #N", "Done in PR #N", "PR #N merged", github.py's note).
- Bullet = the ticket's result.summary first line, cut to its "Fix:" part when it starts with "Cause:" (the work loop's
  cause -> fix -> tests shape), else the title; the title follows on a "ticket:" line when they differ. Groups, first match wins:
  Breaking: tag breaking, "breaking" in title / summary first line, or a note that starts with "Breaking" / says "breaking change";
  Fixed: tag bug / fix / bugfix / hotfix / regression, prefix BUG / FIX, or a title starting "Fix" / "Bug";
  Changed: tag perf / docs / test / tests / ci / refactor, prefix DOCS / TEST / CI, or a title starting "Test";
  Added: the rest.
  "(experimental)" when the ticket has tag experimental or says experimental in title / summary first line / its PR title.
- Flagged at the end: PRs with no ticket, squash- or rebase-merged PRs, tickets in a PR with no result (bullet = title), tickets done since the tag
  (done_at) that no PR names.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # run from a checkout without installing the package
	sys.path.insert(0, str(ROOT))

from unblockd import connectors  # noqa: E402
from unblockd.store import Store  # noqa: E402

PR_RE = re.compile(r"^Merge pull request #(\d+) from ([^/\s]+)/(\S+)")
SQUASH_RE = re.compile(r"^(.*\S)\s*\(#(\d+)\)\s*$")
ID_RE = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)\b")
GROUPS = ("Breaking", "Added", "Changed", "Fixed")
BULLET_MAX = 220


def git(repo: Path, *args: str) -> str:
	return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def merged_prs(repo: Path, since: str, to: str) -> list[dict]:
	"""PR merges on the first-parent line, oldest first: {number, branch, title, sha, date, text (all commit messages inside)}."""
	out = git(repo, "log", "--merges", "--first-parent", "--reverse", "--format=%H%x1f%ad%x1f%s%x1f%b%x1e", "--date=short", f"{since}..{to}")
	prs = []
	for rec in out.split("\x1e"):
		if not rec.strip():
			continue
		sha, date, subject, body = (rec.strip("\n").split("\x1f") + ["", "", "", ""])[:4]
		m = PR_RE.match(subject)
		if not m:
			continue
		lines = [x.strip() for x in body.splitlines() if x.strip()]
		inside = git(repo, "log", "--format=%s%n%b", f"{sha}^1..{sha}^2")
		prs.append({"number": int(m.group(1)), "branch": m.group(3), "title": lines[0] if lines else m.group(3),
		            "sha": sha, "date": date, "text": inside})
	return prs


def squashed_prs(repo: Path, since: str, to: str) -> list[dict]:
	"""Squash / rebase merges: non-merge commits on the first-parent line whose subject ends with "(#N)", oldest first
	(merged_prs' keys; branch "" since such a commit does not name it; squash True)."""
	out = git(repo, "log", "--no-merges", "--first-parent", "--reverse", "--format=%H%x1f%ad%x1f%s%x1f%b%x1e", "--date=short", f"{since}..{to}")
	prs = []
	for rec in out.split("\x1e"):
		if not rec.strip():
			continue
		sha, date, subject, body = (rec.strip("\n").split("\x1f") + ["", "", "", ""])[:4]
		m = SQUASH_RE.match(subject.strip())
		if m:
			prs.append({"number": int(m.group(2)), "branch": "", "title": m.group(1), "sha": sha, "date": date,
			            "text": subject + "\n" + body, "squash": True})
	return prs


def tag_time(repo: Path, tag: str) -> datetime:
	return datetime.fromtimestamp(int(git(repo, "log", "-1", "--format=%ct", tag).strip()), timezone.utc)


def _after(stamp: str | None, when: datetime) -> bool:
	"""done_at (UTC ISO, or a bare date on old tickets) later than when."""
	if not stamp:
		return False
	try:
		d = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
	except ValueError:
		return False
	return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) > when


def _summary_line(t: dict) -> str | None:
	res = t.get("result") or {}
	first = (res.get("summary") or "").strip().splitlines()[0].strip() if (res.get("summary") or "").strip() else ""
	if not first:
		return None
	if first.lower().startswith("cause:"):
		m = re.search(r"\bFix:\s*(.+)", first)
		if not m:
			return None
		first = m.group(1)
		cut = re.search(r"(?<=[a-z0-9)`'\"])\.\s", first)  # first sentence of the fix
		if cut:
			first = first[:cut.start() + 1]
	first = first.strip()
	return first if len(first) <= BULLET_MAX else first[:BULLET_MAX - 1].rstrip() + "…"


def _refs_pr(t: dict, pr: dict) -> bool:
	n, b = pr["number"], re.escape(pr["branch"])
	for r in (t.get("result") or {}).get("refs") or []:
		if re.search(rf"/pull/{n}\b|#{n}\b|\bPR {n}\b", r) or b and re.search(rf"(?<![\w/.-]){b}(?![\w/.-])", r):
			return True
	return False


def _breaking_note(t: dict) -> bool:
	return any(re.match(r"(?i)\s*breaking\b", n.get("text") or "") or re.search(r"(?i)\bbreaking change", n.get("text") or "")
	           for n in t.get("notes") or [])


def _notes_pr(t: dict, n: int) -> bool:
	for note in t.get("notes") or []:
		text = note.get("text") or ""
		if re.search(rf"(?i)\b(merged|done)\b[^\n]{{0,40}}?\bPR #{n}\b|\bPR #{n}\b merged", text):
			return True
	return False


def group_of(t: dict) -> str:
	tags = {x.lower() for x in t.get("tags") or []}
	title = t.get("title") or ""
	line = ((t.get("result") or {}).get("summary") or "").strip().splitlines()[:1]
	words = (title + " " + (line[0] if line else "")).lower()
	prefix = t["id"].split("-")[0]
	if "breaking" in tags or "breaking" in words or _breaking_note(t):
		return "Breaking"
	if tags & {"bug", "fix", "bugfix", "hotfix", "regression"} or prefix in ("BUG", "FIX") or re.match(r"(?i)(fix(es|ed)?|bug)\b", title):
		return "Fixed"
	if tags & {"perf", "docs", "test", "tests", "ci", "refactor"} or prefix in ("DOCS", "TEST", "CI") or re.match(r"(?i)tests?\b", title):
		return "Changed"
	return "Added"


def draft(repo: Path, store: Store, since: str, to: str = "HEAD") -> dict:
	"""The draft as data: {since, to, prs, squashed, groups {name: [item]}, pr_no_ticket, no_result, done_no_pr}."""
	squashed = squashed_prs(repo, since, to)
	prs = merged_prs(repo, since, to) + squashed
	tickets = store.list()
	by_id = {t["id"]: t for t in tickets}
	since_time = tag_time(repo, since)
	groups = {g: [] for g in GROUPS}
	pr_no_ticket, no_result, seen = [], [], set()
	for pr in prs:
		ids = [i for i in dict.fromkeys(ID_RE.findall(pr["title"] + "\n" + pr["text"])) if i in by_id]
		ids += [t["id"] for t in tickets if t["id"] not in ids and (_refs_pr(t, pr) or _notes_pr(t, pr["number"]))]
		ids = [i for i in ids if i not in seen]  # a ticket is told once, under its first PR
		if not ids:
			pr_no_ticket.append(pr)
			continue
		for i in ids:
			seen.add(i)
			t = by_id[i]
			line = _summary_line(t)
			if line is None:
				no_result.append({"id": i, "title": t["title"], "pr": pr["number"]})
			exp = "experimental" in {x.lower() for x in t.get("tags") or []} or "experimental" in (
				t["title"] + " " + (line or "") + " " + pr["title"]).lower()
			groups[group_of(t)].append({"id": i, "title": t["title"], "pr": pr["number"], "text": line or t["title"], "experimental": exp})
	done_no_pr = [{"id": t["id"], "title": t["title"], "done_at": t.get("done_at")} for t in tickets
	              if t["status"] == "done" and t["id"] not in seen and _after(t.get("done_at"), since_time)]
	return {"since": since, "to": to, "prs": prs, "squashed": squashed, "groups": groups, "pr_no_ticket": pr_no_ticket, "no_result": no_result,
	        "done_no_pr": sorted(done_no_pr, key=lambda x: x["done_at"] or "")}


def render(d: dict) -> str:
	n = sum(len(v) for v in d["groups"].values())
	out = [f"# Draft release notes: {d['since']}..{d['to']}", "",
	       f"{len(d['prs'])} merged PRs, {n} tickets. Edit into plain bullets for people upgrading before it goes in CHANGELOG.md.", ""]
	for g, items in d["groups"].items():
		if not items:
			continue
		out += [f"## {g}", ""]
		for it in items:
			out.append(f"- {it['text']}{' (experimental)' if it['experimental'] else ''} ({it['id']}, #{it['pr']})")
			if it["text"] != it["title"]:
				out.append(f"  ticket: {it['title']}")
		out.append("")
	out += ["## Needs a look", ""]
	out.append(f"PRs with no ticket ({len(d['pr_no_ticket'])}):")
	out += [f"- #{p['number']} {p['title']} ({p['branch'] or 'squashed ' + p['sha'][:7]})" for p in d["pr_no_ticket"]] or ["- none"]
	sq = d.get("squashed", [])
	out += ["", f"Squash- or rebase-merged PRs, only their one commit was read: check their tickets by hand ({len(sq)}):"]
	out += [f"- #{p['number']} {p['title']} ({p['sha'][:7]}, {p['date']})" for p in sq] or ["- none"]
	out += ["", f"Tickets in a PR with no result, bullet = title ({len(d['no_result'])}):"]
	out += [f"- {t['id']} {t['title']} (#{t['pr']})" for t in d["no_result"]] or ["- none"]
	out += ["", f"Done since {d['since']} but no merged PR names them ({len(d['done_no_pr'])}):"]
	out += [f"- {t['id']} {t['title']} (done {t['done_at'][:10]})" for t in d["done_no_pr"]] or ["- none"]
	return "\n".join(out) + "\n"


def release_body(changelog: str, version: str, image: str = "ghcr.io/omri2003/unblockd") -> str:
	"""The GitHub Release body (DOCS-21): the CHANGELOG section "## vX.Y.Z (date)" without its heading, then how to get it."""
	m = re.search(rf"^## {re.escape(version)}(?: [^\n]*)?\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
	if not m:
		raise SystemExit(f"CHANGELOG.md has no '## {version} (date)' section: the release PR moves Unreleased under it (docs/releasing.md)")
	n = version.lstrip("v")
	return (m.group(1).strip() + f"\n\n### Get it\n- Image: `docker pull {image}:{n}` (`GET /healthz` says `\"version\": \"{n}\"`)\n"
	        f"- From a clone: `git checkout {version}`, then `pip install \".[mcp]\"`\n")


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(prog="release_notes.py", description="Draft release notes from merged PRs and board tickets.")
	ap.add_argument("since", nargs="?", help="the last release tag, e.g. v0.1.0")
	ap.add_argument("--body", metavar="VERSION", help="print the GitHub Release body for VERSION (e.g. v0.2.0) from CHANGELOG.md instead")
	ap.add_argument("--to", default="HEAD", help="up to this ref (default HEAD; origin/main after git fetch)")
	ap.add_argument("--board", default=os.environ.get("TICKETS_DIR", "tickets"), help="board folder or database location (default: tickets)")
	ap.add_argument("--repo", default=".", help="git repo (default: current folder)")
	a = ap.parse_args(argv)
	if hasattr(sys.stdout, "reconfigure"):
		sys.stdout.reconfigure(encoding="utf-8")
	if a.body:
		sys.stdout.write(release_body((Path(a.repo) / "CHANGELOG.md").read_text(encoding="utf-8"), a.body))
		return 0
	if not a.since:
		ap.error("give the last release tag (e.g. v0.1.0), or --body VERSION")
	if not connectors.is_db(a.board) and not (Path(a.board) / "unblockd.json").is_file():
		ap.error(f"no board at {a.board}: pass --board <tickets folder> (one with unblockd.json) or a database location")
	sys.stdout.write(render(draft(Path(a.repo), Store(a.board), a.since, a.to)))
	return 0


if __name__ == "__main__":
	sys.exit(main())
