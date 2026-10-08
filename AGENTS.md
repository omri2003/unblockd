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
unblockd/mcp_server.py  MCP tools + the `work_loop` prompt / `unblockd://work-loop` resource (the worker protocol; `WORK_LOOP`)
                        (needs the `mcp` package; the only dependency).
unblockd/cli.py         Same operations for agents without MCP (cloud sessions, other tools); `Remote` = the same over REST (UNBLOCKD_URL).
unblockd/notify.py      Webhook notifications: one daemon thread started by web.main (UNBLOCKD_WEBHOOK).
unblockd/chat.py        Messages API over urllib, read-only tool loop, #ID / @board resolution.
unblockd/knowledge.py   Docs linked to tickets (board / profile / ticket `docs`) + files attached
                        to tickets (tickets/files/<ID>/); context() = the bundle a worker reads.
static/index.html       The whole UI, one file, vanilla JS, no build step, no CDN libraries.
tests/                  stdlib unittest: test_store, test_boards (routing, boards, HTTP API),
                        test_claims (incl. 6 parallel workers), test_chat (fake Messages API),
                        test_notify (fake webhook receiver), test_mcp (work_loop prompt + resource; skipped without `mcp`), test_knowledge (context order, dedupe, links, path safety, attachments).
tests/ui_smoke.mjs      Playwright smoke: example board + CLI claim/split, 1400/390px x light/dark; board, graph, docs,
                        drawer, panels, toast; fails on page/console errors or horizontal overflow (CI job `ui`).
README.md               60-second pitch + 5-minute walkthrough + glossary (keep under ~1000 words); every command in it must have been run.
docs/reference.md       Everything long: env vars, boards, profiles, claims, knowledge, chat, config, ticket format, UI, MCP, CLI, REST API.
docs/screenshots/       README images. Regenerate: copy example/ to a temp root, add tickets + a few claims via the CLI,
                        run the server, capture 1400px (2x) with Playwright (board light, graph dark, drawer, 390px mobile).
example/tickets/        Demo board + config; what `docker compose up` shows with no .env.
.github/workflows/ci.yml  tests, then builds + pushes ghcr.io/<owner>/unblockd on main / v* tags.
```

## Data model

Ticket fields (`FIELDS` in store.py; add a field there and API + MCP accept it): `title`,
`status` (todo|doing|review|done writable; `pending` is derived only: writing it is refused, a stored `pending` from an old file is read as todo), `priority` (P0-P3|null), `owner`, `pillar`, `pillar_step`,
`section`, `blocked_by` (ids), `parent` (id of the group/epic this ticket is a part of; NOT a blocker), `waiting_on`, `body`, `tags` (lowercase; `agent:<name>` routes),
`done_when` (list of short acceptance criteria: the bar for done, apart from the background in `body`; shown at the top of `context()` and echoed under "Done when:" in the body entry `complete()` appends; the drawer lists it as a checklist and the edit form takes one per line; not copied by split),
`touches` (paths/globs the work edits), `docs` (repo paths/folders/globs to read), `result` (null until `complete()`: {summary, files, refs, handoff, by, at}; see Claims), plus `id`,
`created`, `updated`.

Derived on every read (`_decorate`): `effective_status` (todo with an open blocker = pending),
`open_blockers`, `blocks`, `children` + `progress` {done, total} (direct children only), `unblocks` (open tickets downstream, transitive), `missing_blockers`,
`stale_days` (doing only), `score` = PRIO_WEIGHT (P0 8, P1 4, P2 2, P3 1) x (1 + unblocks),
`agent` (routed profile), `claim` (live claim: agent, profile, claimed_at, heartbeat_at, note),
`files` (names attached in tickets/files/<ID>/).

Rules enforced in the store: IDs `PREFIX-N` (create with a prefix -> next free number, also
skipping numbers in `legacy_files`); `delete` runs under the lock: drops the claim, removes `tickets/files/<ID>/`, logs `deleted`, and returns the ticket plus `referenced_by` (tickets whose `blocked_by`/`parent` still name it; they are not edited and show it as a missing blocker); `blocked_by` loops refused with the loop spelled out; `blocked_by` ids ADDED by create/update must exist ("no ticket X on this board"), ids already on the ticket that went missing are tolerated (split children inherit them too); `parent` must exist and may not loop (create/update; `list(parent=X)`, CLI `list --parent`, MCP `list_tickets(parent=)`; no REST filter (the UI filters client-side); deleting a parent leaves children pointing at it, shown with no `children` on anyone);
setting `doing` on a ticket with open blockers is refused (in `update`, so web/MCP/CLI all get it; `claim` only takes ready tickets);
owners and pillars must be the configured ones; unknown `agent:<x>` tags refused.

## Config: `<tickets>/unblockd.json`

Optional; `DEFAULT_CONFIG` in store.py is the schema and unknown keys are refused. Keys:
`id`, `title`, `owners` [{name, kind: human|agent}], `default_owner`, `pillar_label`, `pillars`
[{key, name, color, color_dark}], `stale_days`, `wip_limit` (int or null: the UI's In-progress WIP badge; none when null), `lease_minutes`, `legacy_files`, `agents`
[{name, tags, pillars, model, instructions, docs}], `docs` (knowledge for every ticket),
`context_budget` (chars inlined per context, default 60000), `review` (bool, default false: the review gate, see Claims).

## How the pieces work

- **Boards**: `UNBLOCKD_ROOT` (folders of projects) is scanned on every call; a `tickets/` folder
  is a board only if it has `unblockd.json` (opt-in). `TICKETS_DIR` / `UNBLOCKD_BOARDS` add
  explicit folders. First board = default. API: `/api/b/<board>/...`; un-prefixed paths = default.
  The UI keeps the board in `?board=` and localStorage.
- **Routing**: a ticket's `agent` = explicit `agent:<name>` tag, else the first profile sharing a
  tag or its pillar. `list`/`next_ready` `agent=` takes a profile or a worker name (resolved with `profile_of`, same as claim). `work_plan` = ready tickets NOT owned by a human, grouped per profile with
  its instructions/model, for an orchestrator to fan out.
- **Claims**: `claim(agent)` takes the best claimable ticket for the worker's profile (profile =
  the one its name equals or starts with `<name>-`, e.g. godot-dev-1 -> godot-dev: `Store.profile_of`. On a board WITH profiles a name matching none is refused, listing the profiles; an explicit `profile=` is allowed; boards without profiles accept any name): ready, not human-owned, not
  claimed, `touches` not overlapping a live claim. Sets status doing. A file lock
  (`tickets/.unblockd/lock`, re-entrant within one Store) makes it atomic across processes; `create`, `update`, `delete` and `split` take it too (next-id + write, and read-modify-write, would otherwise race).
  `heartbeat` keeps it alive; no heartbeat for `lease_minutes` -> swept back to todo on the next
  read. `complete(id, agent, summary, files=None, refs=None, handoff=None)` needs a summary and appends it to the body, signed; it also stores `result` {summary, files (paths changed), refs (PR / commit / URL), handoff (<= 2000 chars: what dependents must know), by, at} (lists of <= 50 strings of <= 300 chars; bad input is refused before anything changes; MCP `complete`, CLI `complete --file/--ref/--handoff` (repeatable), REST body keys `files`/`refs`/`handoff`, `Remote.complete`). `knowledge.context()` adds `upstream` = for each id in the ticket's `blocked_by` that has a `result`: {id, title, summary, files, refs, handoff}. The drawer does not show `result` yet (UI-6). **Caps and notes**: `complete` summary <= `SUMMARY_MAX` (10000) and handoff <= 2000, clear errors. Every note appended to a body goes through `_append`/`_append_note`: explicit writes (`add_note`, `ask`, `answer`, `reject`, `complete`) are refused when the body would pass `BODY_MAX`, before any claim or status changes; automatic ones (expiry sweep, `release` reason) are skipped silently (the activity log still records the event). `result` is set only by `complete` (refused in create/update, readable) and `reject` clears it so dependents' `upstream` never shows rejected work. `answer`/`reject` refuse an empty `by` (REST/CLI still default to `human`). **Review gate**: with `review: true` in the config, `complete()` sets status `review` (body signed `(agent, review)`) instead of `done`. `review` is open work: it does NOT unblock dependents (decided on purpose: the human look is the point), is not claimable, and a human moves it to `done` (or back to todo) with `update`. The UI shows a Review column (plum) before Done when the config has `review` or any ticket is in that status (`setCols()`; the `--cols` CSS var sizes the grid); a review ticket is not Free (`isFree`: it waits on a human, not on work) and the digest lists a `complete` whose ticket is still in review under "Ready for review", not Finished. `claim_next(agent, profile, note)` = `claim` that never returns a bare None: when empty it answers `{ticket: None, reason, counts}` (`reason` conflict = ready tickets whose touches overlap a live claim: retry; waiting = open work but none ready (in flight / review / waiting on a human / blocked); dry = nothing open for the profile; `counts` = in_flight, ready_conflicting, in_review, waiting_on_human, blocked). MCP `claim_next`, CLI `claim` (without `--id`) and `POST /claim` use it (`Remote.claim_next`); `WORK_LOOP` tells workers to retry on conflict and stop on waiting/dry. `knowledge.with_context` passes an answer without `id` through. `ask(id, agent, question)` (MCP `ask_human`, CLI `ask --question`) is for a decision only a human can make: it ends the claim, sets `waiting_on` = question (status stays todo) and logs `question` (limit `QUESTION_MAX` = 1000 chars, the error says so; the `waiting_on` field and the edit form's input have the same cap, so saving a ticket with a long question works); a todo with a non-empty `waiting_on` has `effective_status` pending, so it is neither ready nor claimable until a human clears `waiting_on`. The question is also appended to the body as a note, so it survives the clearing.
  **Answer / reject** (the human side): `answer(id, by, text)` is refused unless `waiting_on` is set; it appends the note `Answer to: <question> — <text>`, clears `waiting_on` (ticket is ready again) and logs `answered`. `reject(id, by, reason)` is refused unless status is `review` (reason required); it sets status todo, appends `Rejected: <reason>` and logs `rejected`. Front doors: MCP `answer` / `reject` (`id, by, text|reason`), CLI `answer ID TEXT [--by]` / `reject ID REASON [--by]` (by defaults `human`), REST `POST tickets/<id>/answer {by?, text}` / `POST tickets/<id>/reject {by?, reason}`, `Remote.answer` / `Remote.reject`. `notify.py` reports both (`💬` answered, `↩` rejected), and `WORK_LOOP` step 3 tells a re-claimed ticket's worker to read the latest body notes first.
  **Notes** (`add_note(id, by, text)`; MCP `add_note(id, agent, text)`, CLI `note ID TEXT [--by]` (default `human`), REST `POST tickets/<id>/notes {by?, text}` (`by` defaults to `human`), `Remote.add_note`): under the lock, appends `\n\n— <by>, <ISO time>: <text>` to `body` (module helper `_append_note`, the one writer of that format) and logs `note`. It is the append-only way to report progress; `update(body=...)` replaces the whole body with no version check and can clobber a concurrent `complete` or answer. `ask` ("question: ..."), `release` with a reason ("released: ...") and the expiry sweep ("claim by X expired ...") append notes too. Works on any ticket, claimed or not; refused when the body would pass `BODY_MAX` (50k). Moving a claimed ticket
  off doing by hand drops the claim. `update(id, changes, agent=None)`: with `agent` set (MCP `update_ticket` requires it, CLI `update --agent`, `Remote` sends it in the PATCH body, `web` pops it) and a live claim held by someone else, `status` and `waiting_on` changes are refused (use complete / release / ask_human); `agent=None` (web UI, human CLI) is unrestricted. The log (`activity.jsonl`) also records `note`, `answered`, `rejected`, `expired` (sweep), `dropped`, `deleted`, `question`, `created`, `split` and `done` (moved to done by hand),
  and `GET /activity?since=` filters it. The log is bounded: past `ACTIVITY_MAX` (5 MB) `_log` renames it to `activity.1.jsonl` (one old file kept, older history is dropped) and `activity()` reads newest-first across both, stopping at `limit` or `since`. The notifier sees the smaller file as a rotation: it first reads the rest of `activity.1.jsonl` from its old offset, then the new file from 0 (a rotation where the new file already outgrew the old offset between two polls goes unseen). Runtime files (`claims.json`, `activity.jsonl`) live in
  `tickets/.unblockd/`, which writes its own `.gitignore` (`*`).
- **Remote CLI**: with `UNBLOCKD_URL` set (+ `UNBLOCKD_TOKEN`, `UNBLOCKD_BOARD` for a non-default board) `cli.main` builds a `cli.Remote` instead of a `Store`: same method names, each one a urllib call to the REST endpoint, server errors come back as `TicketError` (exit 1). The file lock only works on one filesystem, so this is how agents on other machines share a board; the server's Store does the locking. `--dir` is ignored then. New Store operations need a REST route AND a `Remote` method, or remote workers lose them (`ask` and `list?parent=` were added for this).
- **Split** (`store.split(id, titles, sequential, by)`; REST `POST tickets/<id>/split`, MCP `split_ticket`, CLI `split --title ...`, drawer
  menu "Split…"): a ticket too big for one go becomes >=2 children (same prefix, next free ids; `parent` = the split ticket; pillar, owner, priority, tags incl. `agent:<x>`,
  touches, docs copied; body "Split from X."; inherit the parent's `blocked_by`; `sequential` = child N also waits on child N-1) and the parent's
  `blocked_by` gains all children. A `doing` parent goes back to todo with its claim dropped (a doing ticket can't have open blockers).
  Refused: unknown or done parent, <2 non-empty titles, any invalid title (all validated before anything is created). Runs under the lock,
  logs one `split` event (note = child ids). Nothing is moved or deleted from the parent. The drawer keeps the half-typed form in `SPLIT`/`SPLITD`
  so the 4 s refresh doesn't lose it.
- **Knowledge**: `knowledge.context(store, id)` = board `docs`, then the routed agent profile's
  `docs`, then the ticket's `docs`, then files attached to it; deduped by path, inlined until a
  `context_budget` (default 60k chars), the rest listed for `read_doc`. Each doc reports its `links` (markdown
  links + `@imports` that exist in the repo: the graph's edges, not followed). Missing / outside /
  non-text entries come back with an `error` instead of failing the bundle. Project root = the
  tickets folder's parent; `_inside()` refuses anything that resolves outside it. `claim_next` /
  `claim_ticket` (MCP, CLI) return `{...ticket, "context": ...}`, which is the whole point: a
  worker reads its instructions from the board instead of being told. The chat has
  `get_context` / `read_doc` too.
- **Knowledge graph** (`knowledge.graph(store)`; REST `GET knowledge`, MCP `knowledge_graph`): what `context()` would give every OPEN ticket, without inlining
  (`expand()` results are cached per call). Returns `tickets`, `docs` {path, kind, size, readers, error?, linked_only?}, `reads` [{ticket, doc, via}], `links` [{from, to}]
  (doc -> doc, one hop from docs a ticket reads; targets must exist and be text; those no ticket reads become `linked_only` nodes), and the problem lists.
  `via` = board|profile|ticket|file, first source wins per ticket (same dedupe as context()); a doc's `kind` is its broadest `via`, or `linked`.
  **no_docs** = open tickets with no readable doc via profile, ticket or attachment (only board-wide docs, nothing, or only broken refs). **broken** = [{path, error, via, tickets}]
  (missing / outside the project / not text / too big). **hubs** = top 5 docs read by >= 2 tickets, board-wide docs excluded (everyone reads those). **orphans** = `linked_only` docs.
- **Auth**: optional shared token, `UNBLOCKD_TOKEN` (not per-user identity). Unset = open (and a warning at startup if bound beyond loopback).
  Set = every `/api/*` needs `Authorization: Bearer` or the `unblockd_token` cookie; any URL with `?token=` (correct) sets the cookie
  and 302s to the same URL without it. 401 JSON says how to fix it; the page loads anyway and the UI shows that message once
  (`authFail`; no reload). MCP and CLI use the store directly and are unaffected. Compared with `hmac.compare_digest`.
- **Notifications** (`notify.py`, started by `web.main` only if `UNBLOCKD_WEBHOOK` is set; `UNBLOCKD_WEBHOOK_FORMAT` json|text, `UNBLOCKD_PUBLIC_URL`
  optional board link): every 30 s `Notifier.poll()` reads NEW bytes of each board's `activity.jsonl` (offset per file; first sight = end of file so history
  is never replayed; size < offset = rotated: finish `activity.1.jsonl` from the offset, then restart at 0; a half-written last line waits). Sends `complete`, `expired`, `release`, `question`, `answered`, `rejected` and a derived "ran dry"
  (no ready non-human ticket and no live claim, announced once after activity, re-armed when work appears), ONE batched message per board per tick (10 lines, "+N more").
  It lives in the web server, not the store, because agents log from their own CLI/MCP processes: a store hook would never see them. It calls `store.list()`
  first, which sweeps expired claims, so `expired` is reported even with no browser open. Env, not board config: webhook URLs are secrets and the config is
  committed. Failures print once per streak and the batch is dropped (no retry queue).
- **Chat**: browser keeps history per board (localStorage) and sends the last 20 messages to
  `POST /api/chat`. The server resolves `#ID`, `#board/ID`, `@board` into the user message, then
  loops tool calls (max 8) against `ANTHROPIC_BASE_URL/v1/messages` with read-only tools.
  Needs `ANTHROPIC_API_KEY`; `UNBLOCKD_MODEL` defaults to `claude-sonnet-5-5` (not verified
  against a live key). The chat can't write: its one "write" is the `propose_tickets` tool, which creates nothing (`ask()` returns the whitelisted fields as `proposals`; blank titles dropped). The UI renders each as a card (`propHtml`) with Create (the normal `POST /tickets` with `qaPrefix()`, so the store validates; `propAct`) or Discard; per-proposal state lives in the log message (`pst`). A split is proposed as children with `parent` = the split ticket; the parent's `blocked_by` is not touched (use the drawer's Split for that). Proposals can't reference each other (`blocked_by` = existing ids). It is person <-> Claude only: no shared threads.

## Why files, not a database

Decision, don't re-litigate: **one JSON file per ticket in the project's git repo is the product.** Tickets are reviewed in PRs next to the code
they describe, work offline, diff and merge like source, and agents need no server to read or write them (CLI/MCP open the folder).
A database would give up exactly that. Measured at 5,000 tickets (earlier measurement, before CORE-5's single read per claim; not re-run): `store.list()` 0.15 s, `claim` 0.31 s: fine.

The real limits, and what already answers each:

| Limit | Answer |
|---|---|
| The file lock only works on one filesystem, so every agent had to run where the tickets are | Remote CLI: `UNBLOCKD_URL` sends commands to the one web server, whose Store does the locking |
| Every open browser re-downloaded the whole board every 4 s | `ETag` + 304 on GET; the UI sends `If-None-Match` |
| `activity.jsonl` grew forever and was parsed whole | Rotated at 5 MB, read newest-first and stopped early |
| Parallel `create`/`update` raced (lost tickets, lost claims) | `create`/`update`/`delete`/`split` run under the same lock as claims |

Revisit SQLite only for the **runtime** state (claims, activity), never for tickets, and only if remote mode stops being enough.

## Conventions

- **Stdlib only** in Python (except `mcp` for the MCP server). Tabs. Python 3.10+.
- **One-file UI**: vanilla JS in `static/index.html`. Colours are CSS tokens on `:root`, each one
  `light-dark(light, dark)` with `color-scheme: light dark`: there is no separate dark block, add a token once with both values. Pillar colours
  are generated into the same form from config (`color`, `color_dark`) in `applyConfig`.
- **Settings (⚙ in the header, `#spanel`)**: per browser, `store` keys `theme` (system|light|dark), `density` (comfortable|compact), `digest`
  (on|off); `SET` / `setPref(k,v)` / `renderSettings()`, buttons carry `data-pref="k:v"`. Theme = `data-theme` on `<html>` (unset = System)
  picking `color-scheme` in three tiny rules; Density = class `compact` on `<html>` (a few `.compact` rules after `:root`). A tiny inline
  `<script>` in `<head>` applies both before first paint (no flash). `t` cycles the theme. **A custom property holding `light-dark()` reads back
  unresolved**, so JS that needs real colours (`readColors()` for the graph canvas and grain) resolves them through a hidden probe element's
  `color`; `setPref('theme')` and the OS `matchMedia` change both call `readColors(); kick()`. Never read a token with `getPropertyValue` for
  drawing, and never `closest('[data-theme]')` (the `<html>` carries it).
- **Show mode** (`showSet` in index.html): M = `matches()`, then Only = M, Paths = M + ancestors + descendants over the
  visible universe (two separate walks), Dim = all. Mode is per view (`showModeBoard` / `showModeGraph`). The board uses
  `BV` (set in `render()`); the graph filters in `syncGraph()` before edges/layout, so Flow re-lays out the subset.
  **Isolate on click** (`gIsolate`, default on) is separate from Show mode: `G.isolate = id` (not persisted) makes `syncGraph()` skip `showSet`, the filters and "Linked only" and draw `withChains(T,{id})` (ancestors + descendants over all of T; done tickets only if Show done, the root always). `G.focus` is pinned to it (`setFocus(null)` re-focuses the root); leave with `leaveIsolate()` (Esc, empty canvas, Back to all, board view, unticking the box), which restores zoom/pan in Flow. Don't reuse `state.q`/`showGraph` for it.
  Search `/re/` is parsed once in `setQ()`; it is tested against `id title` and against `id` alone, so `/^BUG-10$/` works.
- **Filter chrome**: the bar holds search, a Filters `<details>` (`#fpanel`: pillar, owner, priority, agent, Show mode),
  removable pills for active non-default filters (each pill carries the `data-*` of the chip that resets it, so the
  existing click handler does the work) and the Free/Blocked chips. Header strip (`renderStrip`): status proportions,
  "N ready", "N agents working" (opens the Agents panel); segments scroll to their column. Next up is a ranked one-line strip (3, "show all").
  Urgency on cards: `.p0` cinnabar wash, `.blocked` hatched + lock; claimed cards get an amber outline and an agent chip
  with heartbeat age (`tickAges`, late after lease/2). Feedback goes through `toast(msg,{err,undo})` (`msg` may be a DOM node: the Delete toast lists `referenced_by` as `.idlink` buttons, 6 s); status moves use
  `moveTo()` (drag-drop, the card's native "Move to" `<details>`, the drawer's segmented status; it refuses
  doing for a ticket with open blockers) and Undo is one PATCH back. Cards are wrapped in `.cw` (a button can't hold
  the move menu). To do has an inline quick-add (`quickAdd`; Enter creates, Shift+Enter keeps typing; prefix remembered
  per board in localStorage; `renderBoard` restores a half-typed draft). Card ages (`data-up`) and heartbeats
  (`data-hb`) are filled by `tickAges`, never in the JSON. `emptyState()` covers a new board and "filters hide
  everything" (`clearFilters`). At <=600px `#tabs` shows one column at a time (`TAB`, sessionStorage); `body[data-tab]`
  drives CSS, so never use a bare `[data-tab]` selector in the click handler. The drawer is title, status/owner,
  details, blockers, then a collapsed Knowledge and activity `<details>`; Ask / Show in graph / Delete live in `.ovf`.
- **Parents on the board**: a card shows `↑ PARENT` and, when it has children, a jade progress bar `done/total` (`.prog`, from `progress`). Filters has **Parent** (chips = tickets that have children; state `parent`, remembered; `renderBar` resets it to ALL if that parent is gone; a removable pill like the other filters; it shows the parts, not the parent itself) and **Group by** (none|pillar|parent|agent; `state.group`, remembered, not a filter so no pill): `lanes(ts)` in `renderBoard` puts a small `.lane` heading per group inside each column, no-key group last.
- **Needs you strip** (`#needs`, `renderNeeds`, above the status strip, board view only, hidden when empty): tickets with `waiting_on` (Question) or status `review` (Review), oldest first (`sinceOf`: `result.at`, else the `updated` date). Row = ID + title (opens the drawer), kind badge, age (`data-since`, filled by `tickAges`, never in the HTML), then inline forms: Question = answer + Send (`POST answer`), Review = Approve (`moveTo(id,'done')`, so Undo goes back to review) + reject reason + Reject (`POST reject`). One submit handler for every `form[data-nform]` (`submitNeeds`: answer | reject | notes), strip and drawer alike. Half-typed text lives in `DRAFTS` (inputs carry `data-draft`, saved on `input`, refilled by `draftField`); the strip is rebuilt only when its signature (id/status/title/waiting_on) changes and `keepFocus` restores the caret, so the 4 s poll doesn't eat it. The drawer (UI-6, shows `result` per AI-10): in review it leads with the result (`resultHtml`: summary, files, refs as links only if http(s), handoff) and Done when, and opens Knowledge and activity (without touching the remembered `knOpen`); done tickets show the result below Details; a ticket with `waiting_on` has an Answer box, every ticket an Add note box (`POST notes`).
- **Bulk edit** (`SEL`, `renderBulk`, `bulk(field,val)`): Shift/Ctrl/Cmd-click a card toggles it in `SEL` (`.card.sel`) instead of opening the drawer; a fixed `#bulk` bar (Status, Owner, Priority, Pillar selects, `+ tag`, `Parent ID or -`; `-` = none) applies one `api('PATCH')` per ticket on `change`, lists failures in one error toast (the server's refusals: doing with open blockers, parent loops...) and gives ONE Undo that PATCHes the old values back. The bar is rebuilt only on selection change / after an edit, never by the poll (it would eat a half-typed tag); `render()` just drops ids that vanished. Esc or Clear empties it. `body.hasbulk` pads the page and lifts the toasts above the bar.
- **Ticket picker** (`pickHtml`/`pickList`/`pickAdd`/`pickIds`, `.pick`): the one way to enter ticket ids. Blocked by in the edit form + New ticket is multi (removable `.pc` chips ARE the value, read with `pickIds('f-blocked')`; self and chosen ones are not offered; ids not on the board are dropped from the chips); Parent in the bulk bar is single (`data-single`: choosing calls `bulk('parent', id|'-')`, a None row = `-`). Search matches id + title, done tickets last/dimmed, 8 results. Not a `MENUS` popover (a plain list under the input, `focusout` hides it; options act on `mousedown` so focusout doesn't eat the click); Arrows/Enter pick, Backspace on empty removes the last chip, Esc closes only the list. No free-text id can be submitted.
- **Docs view** (third view, `d`; `#docsWrap`, `loadDocs`/`renderDocs`/`markDocs`/`openDocNode`, state `DV`): fetches `GET /knowledge` on entering, on Refresh and when the tickets JSON changed
  (in `paint`), never on every poll. Top: problem strip (tickets without docs, broken references, hub docs, orphan docs; click = `DV.f` highlights those nodes, others fade; Esc clears).
  Below: tickets left, docs right (most-read first), one SVG of curves between row markers (board-wide edges only on hover, doc -> doc dashed on the right). Hover/focus a row lights its lines
  (`DV.hl`, `markDocs` only touches classes + the SVG). Click a doc: drawer panel (readers, links to / from, error, View file); click a ticket: the normal drawer. At <=700px: no SVG, docs list then tickets list.
  Deliberately DOM + SVG, not the canvas force engine: it is a bipartite graph, so two ordered columns read better, rows are real buttons, and tokens resolve in CSS (no `readColors` needed).
- **Digest ("While you were away", `#digest`, `loadDigest`/`renderDigest`)**: per board the browser keeps
  `unblockd.lastSeen.<board>` (ISO, localStorage via `store`). No value = first visit: set to now, show nothing. Otherwise one
  `GET /activity?since=<lastSeen>&limit=2000` on load (not on every poll), grouped client-side: Finished (`complete`, `done`
  by hand), Started (`claim`), Stuck (`expired`, `release`), New (`created`); events whose ticket is gone are skipped.
  Compact bar with counts + Expand/Collapse + Dismiss (Dismiss = lastSeen now, hidden for good). Only clock times, never
  "x ago", so nothing drifts. Hidden outside the board view and when empty. Events come from `activity.jsonl`, which is
  gitignored runtime state: tickets created or closed by `git pull` don't appear (only changes through the store do).
- **Agent stats (header "Agents", `#apanel`; the "N agents working" badge opens it)**: a `.spanel` like Settings (Esc / outside click close it). One `GET /activity?since=<now-range>&limit=1000000`
  when the panel opens or the 7 / 30 day range changes (`loadAgents`; range kept in `store` `agentDays`), never on the poll; `agentStats()` groups client-side per profile (`profOf`: longest configured
  profile name the agent equals or starts with `<name>-`, else "other"). Finished = `complete`; median = claim -> complete of the same ticket+agent (a complete whose claim is before the range counts as
  finished but has no duration); stuck rate = (`expired` + `release`) / `claim`s, highlighted (amber + warning sign) at >= 30%; splits = `split` events with an agent; working now = live claims in `T`.
  Events don't carry the profile, so it is derived from the agent name. No server change.
- **The UI polls every 4 s** (`load()` sends the last `ETag` as `If-None-Match`; `web.Handler._send` adds an md5 ETag to every 200 GET and answers 304 with no body on a match, so an idle board costs headers only; `no-store` stays, so the browser never adds the header itself) and re-renders only when the tickets JSON text changes (and, while any popover `<details>` is open (`MENUS`: a card's Move-to, the drawer's `.ovf`, `#fpanel`, `#spanel`, `#apanel`), `load()` keeps the data but defers the render to `paint`, flushed on their `toggle`; heartbeats must not close menus). `MENUS` + `closeMenus(keep)` are also the one outside-click and Esc closer for those popovers (at most one is open at a time: opening one is a click outside the others). A new popover goes into `MENUS`, nowhere else. So the
  ticket JSON must be stable while nothing happens: give timestamps, never "seconds ago"
  (that's why `claim` carries `claimed_at`/`heartbeat_at`, and ages are computed in the browser).
- **Shared UI helpers, use them instead of writing another**: `jf(url,method,body)` (every fetch: JSON in/out, throws `Error(server message)`; `api()` is `jf` on `/tickets`), `chips`/`chipsHtml` (every segmented control: filters, settings, drawer status, agents range), `store` (localStorage with JSON + try/catch), `pickOf(v,allowed,default)` (guard stored values), `hm`/`ago` (clock time / relative age), `toast`.
- Errors are `TicketError` with a message that says how to fix it; HTTP 400/404, MCP tool error.
- Commits: author `omri2003 <omri2003@users.noreply.github.com>`. Run `python -m unittest` first.

## How to verify a change

1. `python -m unittest` (all green).
2. `NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs` (CI runs it; ~5 s). Fix what it reports before going on.
3. UI: run `UNBLOCKD_ROOT=<a temp folder with a copy of example/ or real tickets> PORT=8822
   python -m unblockd.web`, then headless Chromium via Playwright (preinstalled in Claude's
   container; `NODE_PATH=$(npm root -g) node script.js`; never `playwright install`). Screenshot
   light, dark and 390 px wide; check zero page errors and `scrollWidth === 390` on mobile.
4. Chat without a key: point `ANTHROPIC_BASE_URL` at a tiny fake server (pattern in
   tests/test_chat.py) and set `ANTHROPIC_API_KEY=x`.
5. Say what you did NOT verify (real Docker, real API key, real MCP client, touch devices).

## Traps that already bit

- `.board` is the board's grid class: any other element with class `board` turns into a 4-column
  grid. (Board links in chat use `.bref`.)
- `[hidden]{display:none!important}` is required: component classes set `display` and would
  override the attribute.
- `_locked` steals a lock older than 30 s through `_steal`, which holds `lock.steal` and re-checks the age first. A bare `lock.unlink()` after reading the stale mtime lets two processes both "steal": the second deletes the fresh lock the first just created and both run the critical section. `claims.json` that is not valid JSON raises `TicketError` telling the user to delete that file (claims are runtime state, live workers re-claim); it used to read as `{}`, silently dropping every live claim. After `_steal` the loop falls through to the deadline check and sleep (a stuck `lock.steal` guard must not busy-spin or outlive the 10 s deadline).
- **Holder rule** (`Store._holder_only`): when a ticket has a live claim and the caller names itself as an agent that is not the holder, `update` (any field), `split` (`by`) and `delete` (`agent`) are refused; no agent identity (web without `agent`, human CLI) is unchanged. MCP `split_ticket`/`delete_ticket` require `agent`; CLI `--agent` on update/delete (`split --by`); REST `DELETE tickets/<id>?agent=`, `Remote.delete`.
- `pending` is derived, never settable: the bulk bar, move menu and drawer omit it, and the chat prompt lists it only as a filter value.
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

Done since this list was written: the Docs view (knowledge graph); the orchestrator lives as the user's `unblockd-work-all` skill
(not in this repo); it uses `work_plan` + the claim loop, and workers read `context`.
