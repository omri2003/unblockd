# unblockd: notes for agents working on this repo

Read this first. It is the whole picture: what exists, why it's built this way, and the traps
that already bit. Keep it current: **docs move with the code**, in the same commit.

## What it is

A dependency-aware ticket board for humans and AI agents. One ticket = one JSON file inside a
project repo (`<project>/tickets/`), so ticket changes are committed with the code they describe.
Front doors over one core: a web board (`unblockd.web`), an MCP server (`unblockd.mcp_server`),
a CLI (`unblockd.cli`), and a chat that asks Claude about the boards (`unblockd.chat`).

## Files

```
unblockd/store.py       THE core. Ticket CRUD over one folder, validation (FIELDS), config
                        (load_config), derived fields (_decorate), routing (route, work_plan),
                        claims (claim/heartbeat/release/complete/activity/agents_now). The only
                        code that touches ticket files.
unblockd/boards.py      Several boards: discovers <root>/<project>/tickets/unblockd.json, ids,
                        create(). Every front door goes through Boards -> Store.
unblockd/web.py         Stdlib HTTP server + REST API (Handler._route). Serves static/index.html.
unblockd/mcp_server.py  MCP tools (needs the `mcp` package; the only dependency).
unblockd/cli.py         Same operations for agents without MCP (cloud sessions, other tools).
unblockd/chat.py        Messages API over urllib, read-only tool loop, #ID / @board resolution.
unblockd/knowledge.py   Docs linked to tickets (board / profile / ticket `docs`) + files attached
                        to tickets (tickets/files/<ID>/); context() = the bundle a worker reads.
static/index.html       The whole UI, one file, vanilla JS, no build step, no CDN libraries.
tests/                  stdlib unittest: test_store, test_boards (routing, boards, HTTP API),
                        test_claims (incl. 6 parallel workers), test_chat (fake Messages API),
                        test_knowledge (context order, dedupe, links, path safety, attachments).
example/tickets/        Demo board + config; what `docker compose up` shows with no .env.
.github/workflows/ci.yml  tests, then builds + pushes ghcr.io/<owner>/unblockd on main / v* tags.
```

## Data model

Ticket fields (`FIELDS` in store.py; add a field there and API + MCP accept it): `title`,
`status` (todo|doing|pending|done), `priority` (P0-P3|null), `owner`, `pillar`, `pillar_step`,
`section`, `blocked_by` (ids), `waiting_on`, `body`, `tags` (lowercase; `agent:<name>` routes),
`touches` (paths/globs the work edits), `docs` (repo paths/folders/globs to read), plus `id`,
`created`, `updated`.

Derived on every read (`_decorate`): `effective_status` (todo with an open blocker = pending),
`open_blockers`, `blocks`, `unblocks` (open tickets downstream, transitive), `missing_blockers`,
`stale_days` (doing only), `score` = PRIO_WEIGHT (P0 8, P1 4, P2 2, P3 1) x (1 + unblocks),
`agent` (routed profile), `claim` (live claim: agent, profile, claimed_at, heartbeat_at, note),
`files` (names attached in tickets/files/<ID>/).

Rules enforced in the store: IDs `PREFIX-N` (create with a prefix -> next free number, also
skipping numbers in `legacy_files`); `blocked_by` loops refused with the loop spelled out;
setting `doing` on a ticket with open blockers is refused (in `update`, so web/MCP/CLI all get it; `claim` only takes ready tickets);
owners and pillars must be the configured ones; unknown `agent:<x>` tags refused.

## Config: `<tickets>/unblockd.json`

Optional; `DEFAULT_CONFIG` in store.py is the schema and unknown keys are refused. Keys:
`id`, `title`, `owners` [{name, kind: human|agent}], `default_owner`, `pillar_label`, `pillars`
[{key, name, color, color_dark}], `stale_days`, `wip_limit` (int or null: the UI's In-progress WIP badge; none when null), `lease_minutes`, `legacy_files`, `agents`
[{name, tags, pillars, model, instructions, docs}], `docs` (knowledge for every ticket),
`context_budget` (chars inlined per context, default 60000).

## How the pieces work

- **Boards**: `UNBLOCKD_ROOT` (folders of projects) is scanned on every call; a `tickets/` folder
  is a board only if it has `unblockd.json` (opt-in). `TICKETS_DIR` / `UNBLOCKD_BOARDS` add
  explicit folders. First board = default. API: `/api/b/<board>/...`; un-prefixed paths = default.
  The UI keeps the board in `?board=` and localStorage.
- **Routing**: a ticket's `agent` = explicit `agent:<name>` tag, else the first profile sharing a
  tag or its pillar. `work_plan` = ready tickets NOT owned by a human, grouped per profile with
  its instructions/model, for an orchestrator to fan out.
- **Claims**: `claim(agent)` takes the best claimable ticket for the worker's profile (profile =
  the one its name starts with, e.g. godot-dev-1 -> godot-dev): ready, not human-owned, not
  claimed, `touches` not overlapping a live claim. Sets status doing. A file lock
  (`tickets/.unblockd/lock`, re-entrant within one Store) makes it atomic across processes.
  `heartbeat` keeps it alive; no heartbeat for `lease_minutes` -> swept back to todo on the next
  read. `complete` needs a summary and appends it to the body, signed. Moving a claimed ticket
  off doing by hand drops the claim. The log (`activity.jsonl`) also records `expired` (sweep), `dropped`, `created` and `done` (moved to done by hand),
  and `GET /activity?since=` filters it. Runtime files (`claims.json`, `activity.jsonl`) live in
  `tickets/.unblockd/`, which writes its own `.gitignore` (`*`).
- **Knowledge**: `knowledge.context(store, id)` = board `docs`, then the routed agent profile's
  `docs`, then the ticket's `docs`, then files attached to it; deduped by path, inlined until a
  `context_budget` (default 60k chars), the rest listed for `read_doc`. Each doc reports its `links` (markdown
  links + `@imports` that exist in the repo: the graph's edges, not followed). Missing / outside /
  non-text entries come back with an `error` instead of failing the bundle. Project root = the
  tickets folder's parent; `_inside()` refuses anything that resolves outside it. `claim_next` /
  `claim_ticket` (MCP, CLI) return `{...ticket, "context": ...}`, which is the whole point: a
  worker reads its instructions from the board instead of being told. The chat has
  `get_context` / `read_doc` too.
- **Auth**: optional shared token, `UNBLOCKD_TOKEN` (not per-user identity). Unset = open (and a warning at startup if bound beyond loopback).
  Set = every `/api/*` needs `Authorization: Bearer` or the `unblockd_token` cookie; any URL with `?token=` (correct) sets the cookie
  and 302s to the same URL without it. 401 JSON says how to fix it; the page loads anyway and the UI shows that message once
  (`authFail`; no reload). MCP and CLI use the store directly and are unaffected. Compared with `hmac.compare_digest`.
- **Chat**: browser keeps history per board (localStorage) and sends the last 20 messages to
  `POST /api/chat`. The server resolves `#ID`, `#board/ID`, `@board` into the user message, then
  loops tool calls (max 8) against `ANTHROPIC_BASE_URL/v1/messages` with read-only tools.
  Needs `ANTHROPIC_API_KEY`; `UNBLOCKD_MODEL` defaults to `claude-sonnet-5-5` (not verified
  against a live key). The chat can't write. It is person <-> Claude only: no shared threads.

## Conventions

- **Stdlib only** in Python (except `mcp` for the MCP server). Tabs. Python 3.10+.
- **One-file UI**: vanilla JS in `static/index.html`. Colours are CSS tokens on `:root` with a
  dark-mode block; pillar colours are generated from config at runtime (`applyConfig`).
- **Show mode** (`showSet` in index.html): M = `matches()`, then Only = M, Paths = M + ancestors + descendants over the
  visible universe (two separate walks), Dim = all. Mode is per view (`showModeBoard` / `showModeGraph`). The board uses
  `BV` (set in `render()`); the graph filters in `syncGraph()` before edges/layout, so Flow re-lays out the subset.
  **Isolate on click** (`gIsolate`, default on) is separate from Show mode: `G.isolate = id` (not persisted) makes `syncGraph()` skip `showSet`, the filters and "Linked only" and draw `withChains(T,{id})` (ancestors + descendants over all of T; done tickets only if Show done, the root always). `G.focus` is pinned to it (`setFocus(null)` re-focuses the root); leave with `leaveIsolate()` (Esc, empty canvas, Back to all, board view, unticking the box), which restores zoom/pan in Flow. Don't reuse `state.q`/`showGraph` for it.
  Search `/re/` is parsed once in `setQ()`; it is tested against `id title` and against `id` alone, so `/^BUG-10$/` works.
- **Filter chrome**: the bar holds search, a Filters `<details>` (`#fpanel`: pillar, owner, priority, agent, Show mode),
  removable pills for active non-default filters (each pill carries the `data-*` of the chip that resets it, so the
  existing click handler does the work) and the Free/Blocked chips. Header strip (`renderStrip`): status proportions,
  "N ready", "N agents working"; segments scroll to their column. Next up is a ranked one-line strip (3, "show all").
  Urgency on cards: `.p0` cinnabar wash, `.blocked` hatched + lock; claimed cards get an amber outline and an agent chip
  with heartbeat age (`tickAges`, late after lease/2). Feedback goes through `toast(msg,{err,undo})`; status moves use
  `moveTo()` (drag-drop, the card's native "Move to" `<details>`, the drawer's segmented status; it refuses
  doing for a ticket with open blockers) and Undo is one PATCH back. Cards are wrapped in `.cw` (a button can't hold
  the move menu). To do has an inline quick-add (`quickAdd`; Enter creates, Shift+Enter keeps typing; prefix remembered
  per board in localStorage; `renderBoard` restores a half-typed draft). Card ages (`data-up`) and heartbeats
  (`data-hb`) are filled by `tickAges`, never in the JSON. `emptyState()` covers a new board and "filters hide
  everything" (`clearFilters`). At <=600px `#tabs` shows one column at a time (`TAB`, sessionStorage); `body[data-tab]`
  drives CSS, so never use a bare `[data-tab]` selector in the click handler. The drawer is title, status/owner,
  details, blockers, then a collapsed Knowledge and activity `<details>`; Ask / Show in graph / Delete live in `.ovf`.
- **Digest ("While you were away", `#digest`, `loadDigest`/`renderDigest`)**: per board the browser keeps
  `unblockd.lastSeen.<board>` (ISO, localStorage via `store`). No value = first visit: set to now, show nothing. Otherwise one
  `GET /activity?since=<lastSeen>&limit=2000` on load (not on every poll), grouped client-side: Finished (`complete`, `done`
  by hand), Started (`claim`), Stuck (`expired`, `release`), New (`created`); events whose ticket is gone are skipped.
  Compact bar with counts + Expand/Collapse + Dismiss (Dismiss = lastSeen now, hidden for good). Only clock times, never
  "x ago", so nothing drifts. Hidden outside the board view and when empty. Events come from `activity.jsonl`, which is
  gitignored runtime state: tickets created or closed by `git pull` don't appear (only changes through the store do).
- **The UI polls every 4 s** and re-renders only when the tickets JSON text changes (and, while a card's Move-to, the drawer's `.ovf` or `#fpanel` `<details>` is open, `load()` keeps the data but defers the render to `paint`, flushed on their `toggle`; heartbeats must not close menus). So the
  ticket JSON must be stable while nothing happens: give timestamps, never "seconds ago"
  (that's why `claim` carries `claimed_at`/`heartbeat_at`, and ages are computed in the browser).
- Errors are `TicketError` with a message that says how to fix it; HTTP 400/404, MCP tool error.
- Commits: author `omri2003 <omri2003@users.noreply.github.com>`. Run `python -m unittest` first.

## How to verify a change

1. `python -m unittest` (all green).
2. UI: run `UNBLOCKD_ROOT=<a temp folder with a copy of example/ or real tickets> PORT=8822
   python -m unblockd.web`, then headless Chromium via Playwright (preinstalled in Claude's
   container; `NODE_PATH=$(npm root -g) node script.js`; never `playwright install`). Screenshot
   light, dark and 390 px wide; check zero page errors and `scrollWidth === 390` on mobile.
3. Chat without a key: point `ANTHROPIC_BASE_URL` at a tiny fake server (pattern in
   tests/test_chat.py) and set `ANTHROPIC_API_KEY=x`.
4. Say what you did NOT verify (real Docker, real API key, real MCP client, touch devices).

## Traps that already bit

- `.board` is the board's grid class: any other element with class `board` turns into a 4-column
  grid. (Board links in chat use `.bref`.)
- `[hidden]{display:none!important}` is required: component classes set `display` and would
  override the attribute.
- `pkill -f <pattern>` in a shell whose own command line contains the pattern kills the shell
  (exit 144). Use `pgrep -f "[u]nblockd.web" | xargs -r kill` in a command that doesn't start a
  server too.
- Expired claims are swept at the START of `list()`/`get()`; sweeping inside `_decorate` was too
  late (tickets were already read as doing).
- `_locked` must stay re-entrant: `claim()` holds the lock and calls `next_ready()`, which may
  sweep expired claims under the same lock.
- urllib capitalises header names (`X-api-key`); compare headers case-insensitively in tests.
- A regex like `/^BUG-10$/` never matches `id + ' ' + title` (the `$` meets the title), hence the extra test on the bare id in `matches()`.
- Closing the drawer rewrites the URL: keep `location.search` (`?board=`) when clearing the hash.
- `compose.yaml` must pass `.env` values into the container explicitly (`environment:`); `.env`
  alone only feeds `${...}` interpolation.

## Not built yet (ideas, in rough priority)

1. Shared chat between people: needs server-side threads + some identity (there's no auth).
2. Per-user identity (auth is only a shared token for now).
3. README screenshots or a GIF, before a public launch.
4. Knowledge graph view: draw docs + their `links` (+ which tickets link them) like the ticket
   graph. The data is already in `context()`.

Done since this list was written: the orchestrator lives as the user's `unblockd-work-all` skill
(not in this repo); it uses `work_plan` + the claim loop, and workers read `context`.
