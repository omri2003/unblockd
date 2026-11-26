# Changelog

## Unreleased

### Added
- `list_tickets(fields=[...], limit=)` adds only the asked keys per row (body and notes only when named), so a planner checks overlap without `full=true`; REST `GET tickets?fields=&limit=`, CLI `list --fields --limit`.
- `split_ticket(parts=[...])`: parts with their own `done_when`, `touches`, `body`, `components` and `"#N"` blockers, validated all or nothing; `claim=k` takes part #k in the same call. `titles` still works. REST `parts` / `claim`, CLI `split --parts FILE --claim K`.
- `update_tickets` changes several tickets in one all-or-nothing call (MCP, REST `PATCH tickets`, CLI `update-many`); the board's bulk bar sends one request (and one for Undo), and a refused bulk edit changes nothing.
- Database boards (SQL, MongoDB) delete activity history older than `history_days` (board setting, default 365, `null` = keep forever), at most once a day after a write; folder boards keep the 5 MB rotation (experimental, CORE-37).
- Two live workers under one name are noticed (CORE-88): claims carry a session id (stdio MCP: one per server; CLI: `UNBLOCKD_SESSION`; REST `session` / `X-Unblockd-Session`), and a heartbeat, complete or release from another session warns and logs `session_clash`; `strict_sessions: true` refuses it. No session = unchanged.
- `complete(closes=[ids])` moves other open tickets whose work landed in this one to done ("Done under X" note and result), all or nothing (AI-57); never one waiting on a human, an unaccepted proposal, one still blocked, or (for a worker) a person's own ticket. `list_tickets(stale_days=N)` lists todo tickets with no event or edit for N days (`idle_days`), and `work_plan` marks them with a hint. REST `closes` / `?stale_days=`, CLI `complete --closes`, `list --stale-days`.
- `complete(touched=[paths])` warns about edits outside the ticket's touches and in other doing tickets' touches (`result.touched` / `outside`); a claim reply lists the doing tickets whose touches overlap (`overlapping`) (CORE-47).
- `list_tickets(touches=PATH)` finds the tickets whose `touches` overlap a path or glob by the claim rule (not a text search), `component=KEY` those on an architecture component; rows show `touches` and `holder`. REST `?touches=&component=`, CLI `--touches --component`.
- `list_tickets(ids=[...])` (REST `?ids=`, CLI `list --ids`) returns exactly those tickets; a `q` that matches nothing says every word must match, and points at `ids=` for a pasted list of ids (API-23).
- `cli knowledge` lists the open tickets that would start with no doc of their own (with the docs suggested for each) and the broken doc references; locally and through `UNBLOCKD_URL`.
- `status=open` on `list_tickets`, REST `GET tickets` and the CLI = every status but done.

### Changed
- Big boards write fast: a write re-derives only the tickets it reaches instead of the whole board, and a files board reads its folder once per locked step. At 20,000 tickets a write holds the board lock 2-80 ms instead of 0.4-0.9 s, so 200 agents at once no longer give up waiting for it.
- `ask_human` works on an unclaimed todo ticket: no claim (and no context) needed just to ask; a ticket someone else holds is still refused. A `release` whose reason names a person or a decision hints at `ask_human` (AI-56).
- **CLI `list` shows open tickets by default** (every status but done): a planner's `list` no longer prints every finished ticket. `list --all` gives every ticket as before; `--status done` still lists done ones.
- The Docs view's suggestions (GET knowledge) keep what each repo doc names and read a doc again only when it changes: a warm call reads no doc (on a 350-doc, 500-ticket test board 40 s -> 1 s), same suggestions.

### Fixed
- Files board on Windows: a write no longer fails with `PermissionError` when another agent or program is reading the same file at that moment; it retries for up to 5 s, then says which file is held open (CORE-86).
- Files board on Windows: `unblockd.json` is written with LF line endings like the ticket files, so `migrate` back to a folder gives it byte for byte and saving tools in the Agents panel no longer turns a committed config CRLF (CORE-87).
- Python 3.10 and 3.11: `unblockd` imports again (one error message used an f-string only 3.12 can parse); CI now compiles everything on 3.10 (CORE-89).

## v0.2.0 (2026-11-22)

79 pull requests since v0.1.0. Drafted with `scripts/release_notes.py v0.1.0`, then edited (docs/releasing.md).

### Upgrading from v0.1.0
- Nothing to migrate: v0.1.0 boards and ticket files read as before. Notes kept in an old ticket's body move into its `notes` field on the next write.
- Install: `pip install ".[mcp]"` (from a clone) gives the `unblockd`, `unblockd-web` and `unblockd-mcp` commands; the image installs the package the same way.
- Scripts that read CLI errors: when stdout is not a terminal (or with `--json`), an error is now one JSON object `{error, code}` on stdout with exit 1, not text on stderr.
- Workers named after a profile (`backend-1`) can no longer set status doing / done / review with `update`, or answer, accept, approve or reject: they use `claim_ticket`, `complete` and `ask_human`; they also can't delete a ticket a person owns. Names that match no profile (and boards without profiles) keep these.
- Nobody approves their own work, on every board and for every name: answering your own question, accepting your own proposal, and approving or rejecting a ticket you completed are refused.
- `session_tickets` (default 5): a stdio MCP worker gets `reason: "session_done"` from `claim_next` after 5 claims and should stop. Set `"session_tickets": null` in `unblockd.json` to turn it off.
- The CLI refuses a missing board folder (`no board at ...`) instead of creating an empty board there.
- A board inside a linked git worktree uses the main checkout's claims and lock, so workers in different worktrees see each other's claims.
- Moving a ticket into review needs a result: finish it with `complete`, not a bare status change.
- Check the running version: `unblockd --version`, `GET /healthz` and `GET /api/meta` now report it.

### Added
- Storage connectors (experimental): a board can live in SQLite, Postgres or MongoDB as well as in a folder, with the same rules and the same race tests. Name it in `UNBLOCKD_BOARDS`, `TICKETS_DIR` or `--dir` (`sqlite:///x.db#payments`).
- `unblockd migrate --from <place> --to <place>` (experimental): move a board between a folder and the databases, checked after copying.
- Roadmap: `milestones` in the board config, a `milestone` field on tickets, a Roadmap view with progress per version; `claim_next` prefers the next milestone's tickets when scores tie.
- Branch evidence: branches pushed from any clone are linked to their tickets with plain git, and the board says when its checkout is behind origin. Two agents in different worktrees may touch the same file (a merge-conflict warning instead of a refusal), and two clones no longer pick the same next ticket number.
- Leases that fit long runs: per-profile `lease_minutes`, `heartbeat(until=)`, replies that say how much lease is left, and a lapsed claim the same agent can take back.
- Notes reach the worker: a note on a claimed ticket arrives in the next heartbeat reply, one written after the last heartbeat comes back from `complete`, and human notes, answers and decisions are always sent with the claim (pinned notes).
- `complete(followup=...)`: finish the part that is done and hand the rest on as a new ticket that waits on what it needs.
- Review gate: `approve` (MCP `approve_ticket`, CLI `approve`) moves a ticket in review to done and records who looked (`result.approved_by`).
- `report` (MCP `report_ticket`, CLI and REST too): a person adds the summary to a ticket that was moved to done without one.
- Late notes: a person's note on a finished ticket shows in Needs you, to reopen the ticket or dismiss the note.
- `create_board(root=...)` picks which projects folder the new board goes in; in Docker the reply also says where it is on the host (`host_path`).
- Batch creation: `create_tickets` / `create-many` creates a whole plan in one step, with `#N` references between the new tickets, all or nothing.
- Safe retries: an optional `request_id` on create, complete, release, note and ask, so a write that timed out but landed is not done twice.
- Worker MCP server (`--tools worker`): a short tool set that has every tool the work loop needs, short write replies, board docs sent once per session, `session_tickets`, and `release(missing=...)` to flag a tool the worker lacks. Tool sets per profile in the Agents panel (Profiles and tools tab).
- Board config: docs by tag (`docs_by_tag`), skills and MCP servers per board, profile and ticket, and the human an agent works for (`principal`). The claim carries the board's rules, so a worker needs no `board_info` call before creating tickets.
- Doc sections: a docs entry can point at one heading (`guide.md#setup`); a doc too big to send comes with its outline.
- Docs view: who reads what by audience, a preview of exactly what an agent gets, a trial budget, suggested docs for tickets with none, and Pick sections.
- Board UI: command palette (Ctrl/Cmd+K), List layout, side-peek drawer with j / k, switching boards without a reload, "While you were away" digest, loading states, an "I am" setting with a Mine filter, Failed N times on stuck cards, and a finished ticket that opens on its outcome.
- Warnings where work would get lost: a ticket no agent profile will take, a ticket moved to done without a summary, a `doing` ticket nobody holds (claimable by id).
- New boards print a short block to paste into the project's CLAUDE.md / AGENTS.md (`agents-md --write`), so every agent session claims, heartbeats and completes.
- `unblockd health` and a clear "server down" message when `UNBLOCKD_URL` points at nothing.
- Versions: `unblockd --version`; `/healthz`, `GET /api/meta` and MCP `board_info` show the version; CI refuses a release tag that differs from the code.

### Changed
- Much faster on slow disks such as Docker Desktop bind mounts: fewer file reads per request, cached board replies, and Needs you fetches only the current board. docs/reference.md explains the Docker Desktop numbers.
- An agent's heartbeat no longer makes every open tab refetch everything.
- Tool errors list every refused field in one message that says how to fix it.
- The board's look: one header row, liveness on cards, compact Done cards, a calmer phone layout with bigger touch targets.

### Fixed
- Ticket files written by a server running as root stay readable by other users.
- Agent replies are always valid UTF-8, even when an inlined doc is not.
- `create_board` in Docker refuses a folder that is not on a mounted volume (the board would vanish with the container).
- Windows: concurrent writes were sometimes lost while the lock file was being re-created.
- Agent loop: `claim_next` reports the right ids, a ticket can't be split twice, bad board config is refused with a clear message, and the profile hint no longer suggests an argument the tool doesn't have.

## v0.1.0 (2026-10-11)

First public release. One ticket = one JSON file in `<project>/tickets/`, committed with the code.

### Board UI
- Board, dependency graph, Docs view (what agents read) and Map view (architecture notes as a diagram).
- Cards show blockers, parents with progress, live claims with heartbeat age; Done column limited to recent tickets.
- Needs you: one badge and panel for agents' questions, work in review, proposals and stuck tickets, answered inline across boards.
- Several boards per server, filters, search (`/regex/`), bulk edit, priority shortcuts, light/dark, phone layout.

### Agents & MCP
- Lease-based claims: `claim_next` per agent profile, heartbeats, expiry back to todo, file-lock safe across processes.
- `touches` overlap keeps two workers off the same files; `work_plan` groups ready tickets per profile.
- `complete` with summary, files, refs and handoff for dependents; optional review gate; `ask_human` / `answer` / `reject`.
- Worker protocol (`WORK_LOOP.md`) served as an MCP prompt and resource; ticket context bundle with trusted/untrusted split.
- Optional board schema and proposal gate for agent-created tickets; architecture notes editable by agents.
- MCP over stdio or one shared HTTP server.

### CLI & API
- `python -m unblockd.cli` for every operation; `UNBLOCKD_URL` points it at a remote server.
- REST API with ETag, `If-Match` revision checks, activity log, webhook notifications.
- Chat that asks Claude about the boards: experimental, not verified against a live API key.
- GitHub PR sync for tickets in review: experimental, not verified against the live GitHub API.

### Security
- Optional shared token (Bearer or cookie); the server refuses a non-loopback bind without one unless `UNBLOCKD_ALLOW_OPEN=1`.
- CSRF checks on writes (JSON content type, Origin match); file reads stay inside the project.

### Deploy
- Image `ghcr.io/omri2003/unblockd`, runs non-root (OpenShift random UID), `/healthz` probe.
- `docker compose up` serves the board and MCP; stdlib-only Python 3.10+ (the `mcp` package only for the MCP server).
