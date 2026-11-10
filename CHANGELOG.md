# Changelog

## Unreleased

### Storage connectors (experimental)
- The rules are separated from storage: files stay the default; every connector passes the same race tests (`tests/contract.py`).
- SQL connector: many boards in one SQLite or Postgres database, one transaction per board lock.
- MongoDB connector: many boards in one database; one transaction per board lock on a replica set, ordered writes on a standalone server.
- `python -m unblockd.cli migrate --from <place> --to <place>`: move a board between a folder, SQLite, Postgres and MongoDB, checked after copying.
- A database board (`sqlite:///x.db#payments`, `postgres://...#b`, `mongodb://...#b`) in `UNBLOCKD_BOARDS` / `TICKETS_DIR` or the CLI's `--dir` works from the web board, MCP and CLI next to folder boards.

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
