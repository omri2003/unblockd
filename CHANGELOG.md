# Changelog

## Unreleased

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
