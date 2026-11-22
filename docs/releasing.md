# Releasing unblockd

The steps the maintainer runs, in order. Which number comes next: [CONTRIBUTING.md](../CONTRIBUTING.md#versions) (0.x: minor = anything new or breaking, patch = fixes only). The example is v0.2.0 after v0.1.0; for a patch release see the end.

Run the commands in **Git Bash** (or any POSIX shell) from your clone of the repo unless a step says PowerShell. Commits and tags are signed with an SSH key, so load it once per shell window (step 1).

## 1. Load the signing key (once per window)

```bash
eval "$(ssh-agent -s)"
```

```bash
ssh-add <your signing key>
```

## 2. Get the latest main

```bash
cd <your clone>
```

```bash
git fetch origin
```

```bash
git switch main
```

```bash
git pull --ff-only
```

If your board lives in its own repo (cloned as `tickets/` inside this one, git-ignored), update it too, so the draft sees every finished ticket:

```bash
git -C tickets pull --ff-only
```

## 3. Draft the notes from data

Every merged PR since the last tag, joined to its board tickets (ticket id in the PR's commits, the PR or branch in the ticket's `result.refs`, or a "Merged in PR #N" note). Reading the board sweeps expired claims (a lapsed claim goes back to todo), so point `--board` at a copy if you don't want that. Write the draft outside the repo:

```bash
python scripts/release_notes.py v0.1.0 --to origin/main --board tickets > "${TMPDIR:-/tmp}/release-draft.md"
```

PowerShell:

```powershell
python scripts/release_notes.py v0.1.0 --to origin/main --board tickets > "$env:TEMP\release-draft.md"
```

Read the end of the draft first, under **Needs a look**:

- **PRs with no ticket**: write their bullets by hand from the PR title.
- **Squash- or rebase-merged PRs**: only their one commit on main was read; check their tickets by hand.
- **Tickets in a PR with no result**: the bullet is only the title; check it says what changed.
- **Done but in no merged PR**: work that was not merged yet, or never needed code; leave it out unless it shipped.

## 4. Edit the draft into the CHANGELOG

Open `CHANGELOG.md` and write the `## Unreleased` section from the draft, for people upgrading:

- An **Upgrading from vX** list first: what they must do or will notice (changed defaults, refused calls, new install steps). Nothing to do = say so.
- Then **Added / Changed / Fixed** (and **Breaking** when there is one), one plain sentence per bullet, user words, no function names unless people type them. Merge many small tickets into one bullet. A bug introduced and fixed inside the same release gets no Fixed bullet.
- Keep "(experimental)" on anything not verified for real (today: storage connectors, `migrate`, chat, GitHub PR sync).

## 5. The release PR

The PR bumps `__version__` in `unblockd/__init__.py` and renames `## Unreleased` to `## v0.2.0 (YYYY-MM-DD)` with an empty `## Unreleased` above it. The date is the planned release date; fix it on the branch if the merge slips.

```bash
git switch -c release/v0.2.0 origin/main
```

(edit `unblockd/__init__.py` and `CHANGELOG.md`, then)

```bash
python -m unittest
```

```bash
git commit -am "release: v0.2.0"
```

```bash
git push -u origin release/v0.2.0
```

```bash
gh pr create --base main --title "Release v0.2.0" --body "Version bump and CHANGELOG for v0.2.0."
```

A local helper script that runs these steps is fine; keep it out of the repo.

## 6. Merge it

Wait for CI:

```bash
gh pr checks --watch
```

Merge with a **merge commit** (not squash: `release_notes.py` reads PRs from their "Merge pull request #N" commits and only flags squash merges):

```bash
gh pr merge --merge --delete-branch
```

## 7. Tag the merge commit (signed)

```bash
git fetch origin
```

Check the newest commit on `origin/main` is the release merge, and that it says the new version:

```bash
git log -1 --oneline origin/main
```

```bash
git show origin/main:unblockd/__init__.py
```

```bash
git tag -s v0.2.0 -m "unblockd v0.2.0" origin/main
```

`git tag -v` checks an SSH signature only against `gpg.ssh.allowedSignersFile`. Once per machine, list your public key there (one line: your email, then the key):

```bash
echo "$(git config user.email) $(cat <your signing key>.pub)" >> ~/.ssh/allowed_signers
```

```bash
git config --global gpg.ssh.allowedSignersFile ~/.ssh/allowed_signers
```

```bash
git tag -v v0.2.0
```

## 8. Push the tag

```bash
git push origin v0.2.0
```

CI runs on the tag: tests, then the image job checks the tag equals `__version__` (`scripts/check_version.py`; a mismatch fails here and no image is built) and pushes `ghcr.io/omri2003/unblockd:0.2.0`. Follow it:

```bash
gh run watch
```

A wrong tag: delete it on GitHub and locally, fix, tag again.

```bash
git push origin :refs/tags/v0.2.0
```

```bash
git tag -d v0.2.0
```

## 9. The GitHub Release

The body = the CHANGELOG section plus how to get the image. Read the CHANGELOG as tagged, not from whatever branch is checked out:

```bash
git show v0.2.0:CHANGELOG.md > "${TMPDIR:-/tmp}/CHANGELOG.md"
```

```bash
python scripts/release_notes.py --body v0.2.0 --repo "${TMPDIR:-/tmp}" > "${TMPDIR:-/tmp}/release-body.md"
```

(Or `git switch main` and `git pull --ff-only` first, then run it without `--repo`.)

```bash
gh release create v0.2.0 --verify-tag --title "v0.2.0" --notes-file "${TMPDIR:-/tmp}/release-body.md"
```

## 10. Check the image says the version

PowerShell or Git Bash:

```bash
docker pull ghcr.io/omri2003/unblockd:0.2.0
```

```bash
docker run -d --rm --name ub-check -p 127.0.0.1:8799:8787 -e UNBLOCKD_ALLOW_OPEN=1 ghcr.io/omri2003/unblockd:0.2.0
```

Git Bash:

```bash
curl -s http://127.0.0.1:8799/healthz
```

PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8799/healthz
```

Expected: `{"status": "ok", "version": "0.2.0"}`.

```bash
docker stop ub-check
```

## 11. Close the board milestone

In the board's `unblockd.json`, add `"closed": true` to the milestone (`"key": "v0.2"`). If your board lives in its own repo (as `tickets/` above), commit and push it there:

```bash
git -C tickets commit -am "milestone v0.2 closed: v0.2.0 released"
```

```bash
git -C tickets push
```

The Roadmap then shows the next milestone as next.

## A patch release (v0.2.1)

Fixes only (CONTRIBUTING.md). Same steps with the new numbers:

- Draft from the last tag: `python scripts/release_notes.py v0.2.0 --to origin/main --board tickets`.
- If the draft has anything under Added (a feature, field, tool or flag), it is not a patch: release v0.3.0 instead.
- `## v0.2.1 (date)` usually has only **Fixed**, plus **Upgrading** when a fix changes what people see.
- Release PR `release/v0.2.1`, tag `v0.2.1`, image `ghcr.io/omri2003/unblockd:0.2.1`, Release `v0.2.1`. No milestone to close unless the board has one for it.
