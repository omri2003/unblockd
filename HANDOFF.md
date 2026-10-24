# Handoff (2026-10-24, evening)

Repo: `C:\Users\omri0\unblockd` (code). Board: `C:\Users\omri0\unblockd\tickets` (separate private repo). Live board: http://localhost:8787 (Docker: `docker compose up -d --build`).

## State
`main` is clean at the merge of PR #33 (this file). No open PRs, no work in flight.

The Docker board (:8787, `unblockd` + `unblockd-mcp` containers) bind-mounts `./static` and `./unblockd`, so UI edits show at once, but the Python server only loads code at start: it has run since before CORE-26, so restart it (`docker compose restart`) to get that fix live.

## Done today (all merged)
- CORE-26 (PR #27): an MCP `create` whose `agent` is a `kind: human` owner counts as that human, so it is never a proposal (it used to make proposals their owner could not accept). Also cleared 5 stray `proposed_by: "Omri"` tickets on Ultimate Health App (its commit 4d6846e).
- Board review epic UI-42, everything except onboarding:
  - PR #28: UI-45 proposals out of Waiting (one "N proposals to review" button), UI-46 cleaner cards (age only when stale, Settings > Tags), UI-48 Needs you rows one line, UI-50 calmer Graph (Show done off, More menu), UI-52 "Ask Claude" (view icons stay compact: owner call).
  - PR #29: UI-44 Ctrl/Cmd+K command palette.
  - PR #30: UI-47 Next up button in the search row, empty columns shrink to a strip.
  - PR #31: UI-49 the Board as a list (`l`, `?layout=list`).
  - PR #32: UI-51 the ticket drawer is a side peek (board stays usable, `j`/`k` step through tickets), Details as labelled rows.

## Owner calls made today (already in AGENTS.md; don't re-argue)
- View switch stays compact: only the active view has a label (UI-52 labels were tried and reverted). The chat button says "Ask Claude".
- Tags and the `→ agent` hint on cards are off by default, behind Settings > Tags (the drawer always shows them).
- The list is a Columns | List layout of the Board, not a sixth view.
- A ticket's drawer is a non-modal peek; the edit / New ticket form stays modal.
- Where CORE-26's five bad tickets came from is unproven (a cloud session; logs not on this machine). The fix closes the path either way.

## Next
- UI-38..41 onboarding (checklist, guide tab, field hints), the rest of UI-42.
- AI-34: verify the docs re-send window against a real context compaction.
- AI-33 "worth compressing" button (parked).

## Known small things (not tickets yet)
- Phone: the search placeholder is cut off ("Search, an ID, o…"), and the Columns | List switch pushes Next up to a second line in the bar.
- 1400px with the drawer open: the header wraps to two rows.

## How to work with Omri
- UI changes: show before / after and wait for his OK before committing. What works best: one self-contained HTML page per ticket, before left / after right, screenshots embedded as JPEG, under 512 KB (the file viewer shows nothing above that), sent with SendUserFile.
  - "Before" = the current code before the edit (the server reads `static/index.html` on every request), on a copy of a real board (`UNBLOCKD_ROOT=<copy>`, spare port). Do NOT use the Docker board at :8787: compose bind-mounts `./static`, so it shows uncommitted edits.
- Commits are signed with his SSH key: he runs `git add` / `commit` / `push` himself (he stops Claude from running them). Give ONE command per ```bash block (each block gets a Run button only then; no `;` / `if ($?)` chains). Claude opens the PR (`gh pr create`), closes the board ticket, and merges with `gh pr merge` when he says so.
- Bigger UI tickets worked well stacked: branch the next one off the previous PR's branch, open its PR against `main`, and say which to merge first.
- UI smoke on Windows: `npm i --no-save playwright` in a temp folder, then run a copy of `tests/ui_smoke.mjs` with `python3` -> `python`, `chromium.launch({ channel: 'chrome' })`, `repo` set to the absolute repo path, from inside that folder (ESM ignores NODE_PATH).
- Unit tests: `python -m unittest`. 3 failures are Windows-only and pre-existing (symlink privilege, CRLF in test_arch_write, multiprocess fork).
- Closing a ticket on the unblockd board over MCP: `claim_ticket` with a profile-shaped name (`frontend-1`, `backend-1`), then `complete` with the PR link.
- Read `AGENTS.md` first; keep it true in the same commit (and `.claude/skills/unblockd-design/SKILL.md` for UI conventions).
