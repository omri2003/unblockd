# unblockd reference

Everything the [README](../README.md) leaves out: boards and env vars, notifications, agent profiles, claims, knowledge, chat, config, the ticket format, the UI, MCP (shared HTTP server, Docker, Claude Desktop), CLI and REST API.

## Boards

One server, many boards. A board is a `tickets/` folder with an
`unblockd.json` in it: every `<project>/tickets/unblockd.json` under
`UNBLOCKD_ROOT` is picked up (the config file is the opt-in, so unrelated
`tickets/` folders are ignored). Switch boards from the header; **New
board…** creates `<root>/<id>/tickets/unblockd.json`. The first board is the
default; the board you last opened is remembered.

| Env var | Meaning |
|---|---|
| `HOST`, `PORT` | Address the web server binds (default `127.0.0.1`, `8787`). A non-loopback `HOST` without `UNBLOCKD_TOKEN` refuses to start (also the HTTP MCP server) unless `UNBLOCKD_ALLOW_OPEN=1`. |
| `UNBLOCKD_ALLOW_OPEN` | `1` = allow a non-loopback `HOST` with no token. compose sets it because it publishes `127.0.0.1` only; `docker run -p 8787:8787` without a token refuses to start on purpose. |
| `UNBLOCKD_URL`, `UNBLOCKD_BOARD` | CLI only: send commands to a running server (with `UNBLOCKD_TOKEN`) instead of opening the folder. |
| `UNBLOCKD_ROOT` | Folder(s) of projects to scan (`:`-separated, `;` on Windows). Needed to create boards. |
| `TICKETS_DIR`, `UNBLOCKD_BOARDS` | Explicit tickets folders, config optional, or database boards (`sqlite:///x.db#payments`, see Storage connectors). |
| `UNBLOCKD_SYSTEM_DB` (or `UNBLOCKD_SYSTEM_DB_FILE`) | Where unblockd keeps its own data (registered boards, connectors; see The system database): a SQLite file path, `sqlite:///path.db` or `postgres://user:pass@host/db`. Default `~/.unblockd/system.db` (Docker image: `/data/system.db`), created on first start. `_FILE` = a file holding the value (Docker / Kubernetes secrets); setting both is refused. |
| `UNBLOCKD_TOKEN` | Optional shared token. Set: `/api/*` needs `Authorization: Bearer <token>` or the cookie that opening `/?token=<token>` once sets. Unset: no auth, so keep it on localhost. |
| `UNBLOCKD_WEBHOOK` | Optional webhook URL: pushes finished / stuck / "board ran dry" messages (see Notifications). Unset: off. |
| `UNBLOCKD_WEBHOOK_FORMAT`, `UNBLOCKD_PUBLIC_URL` | `json` (default, Slack + Discord) or `text` (ntfy); base URL of this server, appended to messages as a board link. |
| `UNBLOCKD_GIT` | `0` turns branch evidence off (see Branch evidence). On by default when `git` is installed and the project is a git repository. |
| `UNBLOCKD_SLOW_STORAGE` | `1` always shows the slow storage notice, `0` never (see Docker Desktop below). Unset = measured. |
| `UNBLOCKD_GIT_TOKEN` | Optional token for a private https `origin` on any host. `GITHUB_TOKEN` is used too, but only when origin is on github.com. Sent only to that host, through git's environment (never on a command line). |
| `GITHUB_TOKEN` (or `UNBLOCKD_GITHUB_TOKEN`), `UNBLOCKD_GITHUB_API` | Optional GitHub token: every 60 s the server reads the PRs in `result.refs` of tickets in `review` (see GitHub PR sync). Unset: off. API base defaults to `https://api.github.com`. |
| `UNBLOCKD_LEGACY` | Extra old-backlog files for numbering (see `legacy_files`). |
| `UNBLOCKD_AUTO_UPGRADE` | `0` = don't upgrade an older database on start: it is refused until you run `python -m unblockd.cli db upgrade` (see Database upgrades). Default: upgrade automatically. |
| `UNBLOCKD_SECRET_KEY` (or `UNBLOCKD_SECRET_KEY_FILE`) | At least 32 random characters; encrypts the connector passwords kept in the system database. Unset, with a SQLite system database: generated when the first password is stored, saved as `secret.key` next to it (the server says so once), then reused. **Back it up separately from the system database** (one backup holding both defeats the encryption), or set `UNBLOCKD_SECRET_KEY_FILE` from a secrets manager; losing it means re-entering every connector password. A Postgres system database never generates one: set the same key on every server before storing a password. |

Every secret-shaped variable also has a `_FILE` form holding the value in a file (Docker / Kubernetes secrets): `UNBLOCKD_TOKEN`, `UNBLOCKD_WEBHOOK`, `GITHUB_TOKEN`, `UNBLOCKD_GITHUB_TOKEN`, `UNBLOCKD_GIT_TOKEN`, `ANTHROPIC_API_KEY`, `TICKETS_DIR`, `UNBLOCKD_BOARDS` (they may hold database passwords), `UNBLOCKD_SYSTEM_DB`, `UNBLOCKD_SECRET_KEY`. Setting both `NAME` and `NAME_FILE` is refused.

## Notifications

Set `UNBLOCKD_WEBHOOK` and the server posts a batched message every ~30 s when agents finish (✅), a human approves one (✅ `approved by`), a person adds the summary a hand-closed ticket lacked (📄 `summary added by`), get stuck (⚠: expired lease or released) or the board runs dry (🏁). Keep the URL in `.env`, it is a secret.
Slack / Discord: `UNBLOCKD_WEBHOOK=https://hooks.slack.com/services/...` or `https://discord.com/api/webhooks/...` (json is the default; one payload carries both `text` and `content`).
ntfy: `UNBLOCKD_WEBHOOK=https://ntfy.sh/my-topic UNBLOCKD_WEBHOOK_FORMAT=text`. Only events after startup are sent.

## GitHub PR sync

With `GITHUB_TOKEN` set, a ticket in `review` whose `result.refs` has `https://github.com/<owner>/<repo>/pull/<n>` shows the PR's state and CI (`PR #12 · open · ✓3 ✗0 …1`, red when a check failed) in the Needs you review row and the drawer. A merged PR moves the ticket to done (merge = approval; note signed `github`). A PR closed without merging adds one note and the ticket stays in review. The token needs read access to the repo's pull requests, checks and statuses. State is cached in `tickets/.unblockd/prs.json`, not in the ticket.

## Branch evidence: work that lives on branches

The board shows the ticket files of the checkout it was started on. Work pushed from somewhere else (a cloud session, a container, a teammate's laptop) lives on a branch until it is merged and that checkout pulls, so it used to be invisible. Every 2 minutes the web server reads the branches of the project's `origin` and links them to tickets. Any git host works (GitHub, GitLab, Bitbucket, Gitea, a bare repo on a share): it is plain git, not a forge API.

- **Where it reads**: a private bare mirror of `origin` in `tickets/.unblockd/git` (a blob-less partial clone: commits and trees, plus the few ticket files it reads). Your own repository is never written: no fetch into it, no new refs. No `origin`, or `origin` can't be read (offline, no credentials): it reads your checkout's own branches instead and the git endpoint says why.
- **Linking**: a branch is linked to a ticket by its **name** (`wear-34`, `feat/WEAR-34-login`, `nut_85`), by a **commit subject** that names it, or by an **edit to the ticket's file** on the branch. Only branches not merged into origin's default branch count; a ticket that is done shows none.
- **In progress**: a `todo` ticket with a linked branch (by name or file edit) that has a commit in the last 14 days reads as **In progress** (`effective_status` doing) and its card shows the branch instead of a claim. Nothing is written: the ticket file still says todo. A commit subject alone is a mention, listed in the drawer but not counted. `claim_next` skips such a ticket (someone is already on it elsewhere); `claim_ticket` by id takes it, with a warning to build on that branch.
- **Not it after all**: move the card back to To do (Move to, drag, or `update status todo`). That branch tip stops counting for the ticket; a new commit on it counts again. Kept in `tickets/.unblockd/branches_ignored.json`.
- **No duplicate ids across clones**: the board lock only works on one filesystem, so two clones used to pick the same next number. A board with branch evidence picks the next number above every id that origin's default branch, or an unmerged branch, already has a ticket file for (`id_max` in `branches.json`): a checkout that is behind makes `WEAR-9`, not a second `WEAR-6`. A clone with no branch scan (no web server running there: the CLI or stdio MCP alone never scans) still counts only its own files: pull before creating. The scan runs every 120 s, so a ticket pushed less than two minutes ago is not counted yet; two clones creating in that window can still pick the same number, and the second file then conflicts at merge (rename one).
- **Ticket edits on a branch** (a note or a status a worker committed there) are reported in the drawer: "On that branch this ticket is In progress with 2 new notes. You see that here once the branch is merged and this checkout pulls."
- **Behind origin**: when the checkout is behind origin (or has unpushed commits) the board shows a notice with the counts and how many tickets origin has that the checkout doesn't. Dismiss hides it until the numbers change. Pulling is yours to do: the server never changes your checkout.
- **Private repos**: set `UNBLOCKD_GIT_TOKEN` for an https remote (a `GITHUB_TOKEN` that can read contents also works, for github.com only: it is never sent to another host). The notice says when origin can't be read; an ssh remote uses the keys of the user the server runs as. The Docker image ships `git`.
- State: `tickets/.unblockd/branches.json` (REST `GET git` returns its summary: source, remote, base, checkout, error). Off: `UNBLOCKD_GIT=0`.

## Agent profiles: which agent works which ticket

Tickets have `tags`. Agent profiles in `unblockd.json` claim tickets **by tag
or by pillar**; an explicit `agent:<name>` tag overrides both. Each ticket
gets a derived `agent` (shown on its card, filterable in the bar).

```json
"agents": [
  {"name": "backend", "pillars": ["COMBAT"], "tags": ["gdscript"], "model": "sonnet",
   "instructions": "Typed GDScript; run the tests before done."},
  {"name": "blender", "pillars": ["ART"], "instructions": "Batch-process models in Blender."}
]
```

A ticket that routes to no profile (no pillar or tag a profile works, no
`agent:` tag, not owned by a person) is never offered by `claim_next`, so
create / update / batch create answer with a warning saying what to set, and
the board shows how many open tickets are like that (Show them = the Unrouted
filter). The New ticket form and quick add set the pillar from the prefix
when the prefix is a pillar key.

`work_plan` (MCP, `GET .../plan`, CLI `plan`) returns the ready tickets **not
owned by a human**, grouped per profile with its `instructions`, `model`, `skills` and `mcp`:
everything an orchestrator needs to spawn one agent per group. A single agent
follows the `work_loop` prompt (`claim_next`); `next_ready(agent="backend")` only lists a profile's queue (MCP: without human-owned tickets, and a name matching no profile is refused).

## Agents at work: claims and the live view

Several agents can work one board at once without stepping on each other.
Each worker uses a unique name (`backend-1`: profile + number) and loops:

```
claim_next(agent)          -> best ready ticket for its profile, set to doing (or {ticket: null, reason, counts, ids, orphaned}: why nothing, and which tickets)
heartbeat(id, agent, note) -> "what I'm doing now", at least every few minutes
complete(id, agent, summary)   or   release(id, agent, reason)
```

- **Never two on one ticket**: claims take a file lock; tested with six
  workers at once.
- **Never two on the same files**: give tickets `touches` (paths or globs).
  `claim_next` skips a ticket whose `touches` overlap a live claim.
  **Except across worktrees** (CORE-80): a worker in its own git worktree passes `branch` (MCP `claim_next` / `claim_ticket`
  arg, CLI `claim --branch` or env `UNBLOCKD_BRANCH`, REST body key); it is kept in the claim. When both claims name a branch and
  they differ, the edits only meet at merge, so the claim succeeds with a warning ("expect a merge conflict in store.py with
  CORE-74 (backend-2 on branch core-74)"), and `claim_next` still takes tickets that overlap nothing first. The same branch, or
  a side with no branch (a holder without one: the board's branch evidence is used when exactly one active branch is linked
  to its ticket), is refused as before, and the refusal says to pass `branch` if you have your own worktree.
- **Never a human's ticket**: tickets owned by a `kind: human` owner are skipped.
- **Git worktrees share one lock**: when the tickets folder is inside a linked worktree (`git worktree add`), its lock, `claims.json`
  and `activity.jsonl` live in the MAIN checkout's copy of that folder (`<main>/tickets/.unblockd/`), so workers in different worktrees
  of one repo never claim the same ticket; the ticket files stay per branch. The CLI refuses a `--dir` that does not exist instead of
  creating an empty board (a worktree of a repo whose `tickets/` is not committed has none): pass `--dir` or set `TICKETS_DIR` to the
  main checkout's tickets folder. The worker protocol says: one worktree per ticket, branch and PR in `complete(refs=...)`.
- **Whose agent is it?** `claim_next` / `claim_ticket` take an optional `principal`: the `kind: human` owner the agent works for
  (CLI `--principal` or env `UNBLOCKD_PRINCIPAL`; REST body key; MCP arg). Anything else is refused, listing the human owners.
  It is stored in the claim (shown as "backend-1 for Omri" in `agents_now`, the card tooltip and the drawer), copied to `result.principal`
  by `complete`, and carried by the `claim` / `complete` / `release` activity events. Pass the same principal to `heartbeat` / `release` /
  `complete`: when both the claim and the call name one and they differ, the call is refused, so two people's `backend-1` cannot
  act on each other's claim. On a board with 2+ human owners a claim without one carries `warnings: ["no principal: ..."]` (not stored).
- **Two workers under one name** (CORE-88): a claim stores an optional `session` = an id of the running worker (stdio MCP server: a uuid
  made at start; CLI: env `UNBLOCKD_SESSION`, unset = none; REST body key `session` or header `X-Unblockd-Session`; `Remote` sends it;
  the shared `--http` MCP server sends none). When the claim AND a `heartbeat` / `complete` / `release` both carry one and they differ,
  the reply warns "backend-1 is another session here: ... pick another name", the log gets a `session_clash` event, and `agents_now`
  rows show `session` and `clash: true`; the call still goes through. Board config `strict_sessions: true` refuses it instead, saying
  how to fix it. No session on either side = no check (old clients, one-command CLI workers). A lease that lapsed is taken back by the
  same name whatever the session (a restarted worker recovers its own ticket; the new claim gets the new session). A self-declared id:
  it catches accidents (two chats both started as backend-1), not abuse.
- **Crashed workers don't hold tickets**: a claim with no heartbeat for
  `lease_minutes` (default 15) expires and the ticket goes back to todo. Any call that names the holder
  (`add_note`, `update_ticket`/`update --agent`, `heartbeat`) renews the lease, so a note before a long test run keeps the claim.
- **A lead's claim lives while its child agents work** (AI-59): a lead that claimed a parent ticket and handed its children to
  sub-agents can't heartbeat while it waits on them. Every heartbeat, `add_note`, `update(agent=)` and `complete` on a child also
  renews each LIVE claim up the child's `parent` chain (parent, grandparent ...) whose `principal` equals the child claim's. Both
  claims must name one: with no principal on either side nothing is renewed, because a worker name alone does not show the two serve
  the same person, and a stranger's child must not keep someone else's claim alive. A claim that already lapsed is not revived, no
  status changes, and the renewal goes into the same `claims.json` write as the child's heartbeat.
- **Longer leases for slow work** (AI-58): a profile may set its own `lease_minutes` (`agents: [{"name": "artist", "lease_minutes": 30}]`);
  its claims, their expiry and every reply use it. For one long run, `heartbeat(id, agent, note, until=<UTC ISO>)` (CLI `heartbeat --until`,
  REST body `until`) keeps the claim alive until that time with no more heartbeats; it is capped at `lease_max_minutes` (default 240)
  from now (the reply warns when it was capped), so a worker that died mid-run still frees its ticket. Replies carry `expires_at` and
  `lease_left_s` counting the longer of the two.
- **Lease left, and a lapsed claim taken back** (AI-35): the claim result and every write reply to the holder (`heartbeat`, `add_note`,
  `update_ticket` with `agent`, MCP short replies too) carry `lease_left_s`, plus a `warnings` line when less than 3 min is left.
  A worker whose claim expired gets `warnings: ["you no longer hold X ..."]` on `add_note` (the note is kept). `heartbeat` and
  `complete` take the claim back for that same worker when the activity log shows its claim expired and nothing happened since
  (no other claim, release, complete, question) and the ticket is still claimable: a 20-minute test run then completing works.
  Otherwise they refuse with `you no longer hold X ... and it can't be taken back: <why>`.
- **Workers are told what failed before**: a claim result (`claim_next`, `claim_ticket`, CLI `claim`, `POST claim`) also has
  `lease_minutes`, `expires_at` and `previous_attempts`: up to 5 `{agent, outcome: expired|released|rejected, at, note}`, newest first, read from the activity log.
- Moving a claimed ticket off *doing* by hand ends the claim.
- **Retries are safe** (CORE-74): `complete`, `release`, `add_note`, `ask_human` / `ask` and `create` take an optional `request_id`
  (MCP arg, CLI `--request-id`, REST body key, `Remote`; 1-100 of `A-Z a-z 0-9 . _ : -`). A repeat by the same agent with the same id
  changes nothing and returns the ticket with `already: true`, `first_at` and `request_id` (create: the ticket it made); the same id
  on another write or ticket is refused. The last 500 ids per board are kept (runtime doc `requests`). Without an id, the same agent
  repeating its own complete, release or question (nobody holds the ticket now and its newest transition is that agent's) also gets
  `already: true` instead of "isn't claimed".
- **Finish what you can, hand the rest on** (AI-60): when part of a ticket is done and the rest waits on another ticket,
  `complete(id, agent, summary, followup={title, done_when?, blocked_by: [ids], body?, priority?, touches?})` (MCP arg, CLI
  `--followup '<json>'`, REST body key) completes the ticket AND creates the rest as a new ticket in one step, all or nothing: same
  prefix, parent, pillar, owner, tags, touches, docs and components, body `Rest of <id>.`, waiting on `blocked_by`, so it becomes
  ready by itself when they are done. The reply's `followup` names it; `result.followup` keeps its id. A `release` whose reason names
  another ticket id answers with a `hint` pointing at this.
- **Ask without a claim** (AI-56): `ask_human` / `ask` works on an unclaimed todo ticket with no question yet (no claim, so no
  context is sent only to ask); a ticket someone else holds is still refused, and so is one that already waits on a human (add a
  note instead). A `release` whose reason names a person or a decision (a human owner's name, `human`, `decide`, `approve`,
  `confirm`, `sign-off`...) answers with a `hint`: use `ask_human` (a release counts as a failed attempt).
- **Work that landed under another ticket** (AI-57): `complete(id, agent, summary, closes=[ids])` (MCP arg, CLI `--closes ID`
  repeatable, REST body key, `Remote.complete`) also moves those open tickets to the same status (done, or review on a review board)
  with a note and a `result` "Done under <id>: <first line of summary>" (`result.under` = id; the completing ticket's `result.closes`
  lists them; the reply's `closed` too). All or nothing: a missing one, one already done / in review, one another worker holds, one waiting on a
  human's answer, an unaccepted proposal, one still blocked, or (for a worker) one a person owns refuses the complete, every problem
  named: closing is never a way around a person's decision or a blocker. At most 20. Finding them: `list_tickets(stale_days=N)` (REST `GET tickets?stale_days=N`,
  CLI `list --stale-days N`) = todo tickets with no activity event and no edit for N days, each with `idle_days`; `work_plan` marks such
  rows (`idle_days`, `stale_hint`; top-level `stale` = their count) using the board's `stale_days`.
- **Touches vs the real edits** (CORE-47): `complete(..., touched=[paths])` (MCP arg, CLI `--touched PATH` repeatable, REST body key,
  `Remote.complete`) = the paths the work really edited, stored as `result.touched`; the ones no `touches` entry covers (same path,
  inside a folder, glob match) go to `result.outside`, and the reply `warnings` name them and every other doing ticket (claimed,
  orphaned, or work on a branch) whose touches meet one of them. Never refused. `files` alone is not checked. A claim reply carries
  `overlapping` [{id, title, agent (live claim's, or null), files, branch}] = the other doing tickets whose touches meet the claimed
  one's (with no live claim in the way, or on another branch, the claim is allowed; this says who else is in those files).
- **Notes that land as a worker finishes** (AI-50): `complete`, `release` and `ask_human` replies carry the same `inbox` (same caps,
  flag `unread_before_complete` / `_release` / `_ask`) for notes left after the worker's last heartbeat, and `complete` stores their
  numbers as `result.unread` for the reviewer.
- **Notes reach the holder**: a `heartbeat` reply (MCP, REST, CLI) carries `inbox` {count, notes: [{ticket, n, by, kind, at, text, reply_to?, truncated?}], truncated?} when
  someone else added notes to a ticket the worker holds since it was last shown them (the last 10; each note's whole text up to 2000 chars, 6000 for all of
  them, the newest first served; a cut note has `truncated: true` and only then does the worker need `get_ticket`, AI-44). To ask
  another worker for a change in a file its ticket touches, `add_note` on THAT ticket: its holder sees it within one heartbeat.
- **Too big? Split it** instead of half-doing it: `split_ticket(id, agent, titles | parts, sequential?, claim?)`
  (REST `POST tickets/<id>/split` `{titles | parts, sequential?, by?, claim?, principal?}`, CLI `split --title ... | --parts FILE|-  [--by A --claim K]`, `Remote.split`, drawer menu *Split…*).
  `titles` (the short form) makes one child per title with the parent's pillar, owner, priority, tags, touches, docs, components, skills, mcp and milestone and the body "Split from X.".
  `parts` (CORE-50) = `[{title, done_when, touches, body, components, blocked_by, ...}]`: each part is what `create_ticket` takes minus id / prefix / parent / status, and what it leaves out is copied from the parent as above; give each part its own `touches` (parts with the parent's touches overlap, so only one could be claimed at a time) and `done_when`. A part's `blocked_by` may name existing ids or `"#N"` = the Nth part (every part also inherits the parent's `blocked_by`). Parts are validated like `create_tickets` items, all or nothing: one bad part refuses the split with every problem named by part (`#2: invalid title`), nothing written. `claim=k` (with `by` / `agent` a worker name) claims part #k for it in the same call, returned as `claimed` (with `context` from MCP / CLI); the part must have no open blocker and no human owner (refused before anything is written); if the claim still fails (its touches overlap another live claim) the split stands and the reply has `claim_error`.
  The parent now waits on all of them (your claim ends). *Do in order* (`sequential`) chains the parts. A ticket with open children is refused ("already split into ...": work those, or split one of them); once they are all done it can be split again. The drawer's *Split…* form is titles only.

A claimed card carries a **liveness line** (agent, `live · 40 s` with a filled dot, or
`quiet · 11 min` with a hollow dot once the last heartbeat is older than half the lease, plus a
thin bar of heartbeat age over `lease_minutes`) and the claim note; the Agents button says
`Agents · 2 live`. The graph has a dashed ring, and each ticket's **Agent activity** in its
drawer. Claims and the activity log live in `tickets/.unblockd/`, which
ignores itself in git.

**Worker guardrails.** On a board with agent profiles, an actor whose name resolves to a profile (`backend`, `backend-1`) cannot `answer`, `reject`, `accept` or `approve`, cannot set a ticket's status to doing / done / review with `update_ticket` (use `claim_ticket` / `complete` / `ask_human`), and cannot delete a human-owned ticket. Nobody can approve their own work: `answer` by the asker, `accept` by the proposer, `reject` and `approve` by the completer are refused. Names that match no profile (`claude`, `omri`, `human`) and boards without profiles are unrestricted, so a solo owner's own Claude works as before. This is accident prevention, not security: an agent with a shell can edit the ticket files, and per-user tokens are the later fix. For worker subagents, also deny the tools client-side, e.g. in `.claude/settings.json` `permissions.deny`: `mcp__unblockd__answer`, `mcp__unblockd__reject`, `mcp__unblockd__accept_ticket`, `mcp__unblockd__approve_ticket`, `mcp__unblockd__create_board`, `mcp__unblockd__arch_*` (one entry per `arch_` tool).

**"Work on all tasks you can"**: an orchestrator calls `work_plan`, spawns
one worker per agent profile with that profile's `instructions` and `model`,
and each worker runs the loop above until `claim_next` says to stop: `reason` "conflict" = the reply names the claims in the way (`held_by` [{id, agent, files, blocks, expires_at}]: the same overlap rule as claims, so with `branch` a claim on another branch is not in the way) and `retry_after_s` (5-60 s, never past the first of their leases); `claim_next(wait_s=120)` (CLI `claim --wait 120`, REST `POST claim {"wait_s"}`) looks again every 2 s and returns as soon as one frees up (complete, release, an expired lease), with `waited_s`; the shared `--http` MCP server caps the wait at `MCP_WAIT_MAX` (default 30 s: each waiting call holds one of its threads), stdio and REST at 120. "waiting" / "dry" = stop and report. The empty answer carries `ids` (the ticket ids behind each `counts` key) and `orphaned` (`doing` with no live claim: moved by hand, or the claim was lost; `claim_next` never hands it out, but `claim_ticket(id)` takes it over with a warning, since nobody else could ever finish it). `claim_ticket` refusals name the cause (blocker ids, the question, the proposal, the human owner, or the overlapping holder with its files and when its claim expires); `claim_ticket(id, wait_s=)` (CLI `claim --id X --wait`, REST `POST tickets/<id>/claim {"wait_s"}`) waits for that overlapping claim to end instead of refusing at once.

### Teams

Run ONE shared server (`python -m unblockd.web`, plus `mcp_server --http` or the Remote CLI for agents on other machines): claims live in
`tickets/.unblockd/` and are not in git, so two clones of the same repo can both claim a ticket. List each person as a `kind: human` owner and have
each person's agents claim with their `principal`; the Done ticket then reads "Done by backend-1 for Omri". On a board with `review: true`, **Approve** (Needs you, `approve_ticket`, `approve ID`, `POST tickets/<id>/approve`) records who looked: `result.approved_by` / `approved_at`, shown as "Done by backend-1 for Omri · approved by Dana" (muted "self-approved" when they are the same person; "no human recorded" on a board with 2+ humans when a done ticket has neither). A plain move to Done records no approver, because nobody approved. A ticket can only be moved INTO review when it has a result (the error says to finish it with `complete`, which records summary, files and PR link), and a new ticket can't be created in review; moving a finished ticket back to review (Undo of Approve, Undo of a move) still works. **Done without a summary:** moving a ticket to done by hand (drag, `update`, `create` with status done) when it has no result answers with a warning (finish with `complete()`: it records the summary, files and PR) and logs `done` with who moved it (PATCH body / CLI `--by`, else `human`). Tickets done that way from 2026-11-21 on read `unreported: true` (older ones are left alone); a person adds the summary afterwards with `report` (`report_ticket`, `python -m unblockd.cli report ID "summary" [--by NAME] [--file PATH] [--ref URL]`, `POST tickets/<id>/report {by?, summary, files?, refs?}`), which sets `result` with `by` = that person. So "who took responsibility" means the approver on a review board and the principal otherwise. It is a label, not proof, until per-user
tokens exist: anyone with the shared token can name any principal or approver.

## Knowledge: tickets come with their instructions

Stop re-telling agents what to read. Link docs once and every worker gets
them when it claims a ticket:

| Where | `docs` applies to |
|---|---|
| `unblockd.json` → `"docs"` | every ticket on the board (e.g. `AGENTS.md`, `docs/HANDOFF.md`) |
| `unblockd.json` → `"docs_by_tag"` | only tickets with that tag: `{"map": ["docs/MAP_BRIEF.md"]}` (a shared brief that should not cost every ticket's budget) |
| an agent profile → `"docs"` | every ticket that profile works (e.g. `docs/MECHANICS.md`) |
| a ticket → `docs` (Edit) | that ticket (e.g. `docs/notes/combat/`) |
| **Attach files** in the drawer | that ticket: stored in `tickets/files/<ID>/`, committed with it |

Entries are repo paths, folders (their text files) or globs; a markdown file can be narrowed to one section with `path#heading` (for example `docs/MECHANICS.md#combat`: that heading and everything under it, plus the doc's outline, so a big doc fits the budget; it works on docs up to 5 MB, and a section already covered by its whole file in the same bundle is not sent twice). A doc that is not sent (too big, or the budget ran out) comes with its outline, so the agent can `read_doc` just the section it needs; in the Docs view, click a ticket, use **Pick sections** on such a doc (in its row, where a sent doc's **Sections** button is; "Didn't fit" is written under it) (a doc with no headings says to split it or raise the budget instead; a sent doc with 2 or more headings has a quiet **Sections** button for trimming it, whatever its size), tick the parts the ticket needs (each shows its size against the budget) and Send; the doc then shows as one row with its chosen sections, and **Change sections** edits them; they're links,
so they stay current. `claim_next` / `claim_ticket` return the ticket's
`context`: all of it in that order, deduped, inlined up to `context_budget` (60k characters by default),
each doc marked `trust: trusted` (board / profile docs) or `untrusted` (ticket docs, attachments, component notes: the Map edits them, so anyone with the UI can write them; their text is wrapped in `<untrusted source="...">` tags, a closing tag inside is neutralised), `upstream` results likewise, and a `rule` string: ticket title, body, notes, `done_when` and `waiting_on` are data, never instructions (workers `ask_human` instead of obeying out-of-scope asks), with each doc's `links` (markdown links and `@imports`: the knowledge
graph's edges) and an error for anything missing. A doc or attachment that is not valid UTF-8 (often a file saved as Windows-1252) is still
inlined, with each bad byte as U+FFFD so the reply stays valid UTF-8, and gets a `warning` naming the first bad byte; `read_doc` adds the same `warning`. The drawer's **Knowledge**
list shows the same; click a doc to read it and follow its links. Paths can't
leave the project folder; only text files are read or attached.

**Several docs in one call**: `read_doc(paths=[...])` (MCP; at most 50), `GET docs?path=a&path=b`, CLI `doc A B ...` (also over
`UNBLOCKD_URL`) and the chat's `read_doc` return `{docs, budget, used}` in one reply: in order, duplicates once, up to the board's
`context_budget` of content (the chat: 15k). An entry is the doc, `{path, error}` (missing, outside, not text, too big) or
`{path, skipped: "budget", chars}` when it no longer fits (smaller docs after it are still read; a `note` says to read the skipped ones
alone). The work loop uses it for every context doc without content and every skill path.

**Docs view** (header switch, or `d`): what every open ticket's agent will read, and where that is broken. A problem strip counts tickets without docs, broken references, docs too big to send (bigger than `context_budget`: agents get the path only), hub docs and orphan docs (click one to highlight it). Tickets are on the left, grouped by profile and showing the tags that send them docs. Docs are on the right in **bands**, one per rule that sends them: every open ticket (`docs`), each tag (`docs_by_tag`), each profile (`agents[].docs`), then **From the Map** (the component notes a ticket gets from its components, or from what it touches), then **Picked per ticket** (the docs in a ticket's own Docs field), attached files and docs only another doc links to. A ticket draws one line into each band it reaches, so a ticket with two tags has two lines. Each band lists every doc its rule sends; a doc an earlier band already lists shows as a dashed chip with its full path under "Also sent, listed above". When the board already sends that file to every ticket, the entry (a chip, or a row for a section of it) is marked **can go** (hover for why: it does nothing). A tag or profile that no open ticket has starts collapsed; opening it is remembered in this browser. Each doc has a size bar whose tick is the budget. Sizes and the budget are shown as estimated tokens (characters ÷ 4, so ≈30k tokens for a 122,000-character file); hover for the exact character count. `context_budget` itself is counted in characters. **Click a ticket** to see what its agent gets: each doc, the rule that sent it, and whether it is sent in full or only as a path (too big, or the budget ran out), with a bar of the budget used. When a doc is not sent in full, the bar stretches to what every doc of that ticket needs: the docs that did not fit follow the sent ones as red hatched parts, a tick marks today's budget, and a short label under it gives the number to set, for example "Needs ≈18k tokens, the budget is ≈15k · set `context_budget` to 72000 characters" (the token number is the total everything needs; `context_budget` counts characters, about 4 per token) (a bigger budget is paid on every claim; sending only some sections of a big doc is often the better fix). Click a band to light its tickets, click a doc for who reads it and what it links to. The chevron on a profile group or a band collapses it to its header (lines then end at the header; remembered per board in this browser), and Collapse all / Expand all does every group. The search box (`/` in this view) matches ticket ids, titles and doc paths: matches stay bright, the rest fades, and a collapsed group with a match opens while you search. **Suggested docs**: a ticket with no doc of its own whose `touches` a doc of the repo clearly names gets up to 3 suggestions in its preview, each with the reason in words ("Names static/index.html in a heading", "Links to unblockd/store.py · 3 mentions") and **Add to ID**, which puts the doc in the ticket's Docs field (Undo in the toast); the row says `doc suggested` and the problem strip counts them. Scoring: an exact path mention is worth 3, the file name alone 2 (only when no other file in the repo has that name), the folder 1; in a heading or as a markdown link target 6 / 5 / 2; each further mention of the same touch adds 0.5, at most 1. A doc needs 3 in all, and folders count only in a doc that also names one of the ticket's files (its path or file name): a doc that only says "code lives in unblockd/, UI in static/, tests in tests/" is never suggested, nor is one for a ticket that touches only a glob (`unblockd/*.py`). Mentions in code blocks count like prose (a repo's file map is often one). Never a doc the board already sends to every ticket or one in the tickets folder; a doc over a third of the budget comes after every smaller one. A doc bigger than `context_budget` comes after every doc that fits, and only when it names the ticket's files under one of its headings: it is marked **too big: pick sections** in red and its button is **Pick sections**, which adds it and opens its section picker right there (the same one as Didn't fit) with the sections that name the files already ticked and a line saying so, so you see why it was offered and can Send just those (untick them and the line says what was suggested; **Reset** ticks them again, and nothing changes on the ticket until Send); one with no headings, or that names the files only before its first heading, is never suggested. When no doc qualifies, the preview shows at most 2 **weak matches** instead: docs that name the ticket's folder only, or its file once in passing. They are grey, labelled `weak match`, say why they are weak ("Only names the folder scripts/, not a file this ticket touches"), and have the same Add; they do not tag the row or count in the problem strip. Suggestion cards are flat, with a dashed outline and a tint, to read as not added yet: jade for a suggestion, a red edge for too big, grey for a weak match. At most 400 `.md` / `.txt` files are looked at, each read again only when it changed (what it names is kept in memory per file); a board with no repo (a database board) gets no suggestions. Esc clears the selection and the search. A collapsed group or band still shows its problems (for example `1 too big`). An image (png, jpg, gif, webp) is not a broken reference: agents get its path and open it with their own tools, and the row says "image · path sent". Same data: `GET /knowledge` (each read's `from` = the tag or profile, `rules`, `budget`, `suggest` = {ticket: [{path, size, score, why: [{touch, kind: path|file|folder, name, where: heading|link|text, count}], too_big?, outline?, sections? (the anchors of the sections naming the files), weak?}]}), MCP `knowledge_graph`; the preview is `GET tickets/<id>/context?content=0` (each doc's `sent`: full, too_big, budget or error; `fits_budget` when one did not fit).

**Map view** (header switch, or `m`): the architecture notes as a diagram. On by default (`architecture: "docs/arch"`, used when that folder exists); with no notes yet it shows **Add your first component**. Components are cards with their open tickets by state (doing, ready, pending, review); darker = more open work, amber outline = being worked on, a red dot = a problem (a `paths` glob that matches no file, a broken link, an edge with no contract). Arrows go from the note that lists a link to the note it links to; two notes that link to each other share one curve with an arrowhead at each end. Hover an edge to read its contract (both, for a pair). Click a component to dim everything but it and its direct neighbours and open its panel (paths, what it links to and what links to it with each contract, its open tickets, View note); Esc or a click on empty canvas leaves. The strip on top counts the problems and spans of tickets over 2+ components; click one to light the parts involved. A ticket's menu has **Show on map**, and its component chips (drawer, Details) open the map on that component. A component's panel has **Show N tickets on the board** (the Board with the Component filter set). **Zoom and move**: the wheel (or a trackpad pinch) zooms around the pointer, dragging the background pans, `+` `-` zoom and `0` fits everything (also the **Fit** button; the map is fitted when you first open it). Drag a component to move it, its edges follow; a plain click still opens its panel. Positions are saved per board in this browser only (not in the repo); **Reset layout** forgets them and components without a saved position keep the automatic layout. Below 700px it is a list with each component's connections underneath.

**Editing the map** (no files to touch; the server writes the notes, so it is committed and reviewed in git like tickets): **+ Component** in the toolbar (or `a`) asks for a title (the key, the note's file name, follows it as a slug and is fixed once saved), a summary and the paths (one glob per line). A component's panel has **Edit** (title, summary, paths), **⋯ Delete component…** (asks first and says how many connections go; refused while an open ticket lists it in `components`, with the tickets named) and, on each connection line, a pencil (edit the contract: Enter saves, Esc cancels) and a x (remove, with Undo). **+ Connection** picks the component it links to and takes the contract (what crosses the edge; empty asks once). Lines under *In* edit the other component's note. Agents do the same with `arch_*` tools (below).

## Architecture: one note per component

The architecture is on by default: put one markdown note per component of your app in `docs/arch/` (frontend, api, database, s3...), or add them from the Map (**+ Component**) or with the `arch_*` tools. Set `"architecture"` in `unblockd.json` to another repo folder, or to `null` to switch it off. The links in a note's **Connections** section are the edges, and the rest of that line is the **contract**: what crosses the edge and which invariants hold.

```
---
paths: src/storage/**, infra/s3.tf
---
# S3
One paragraph: what this component is.

## Connections
- [database](database.md) — every upload writes a row in `photos` (key, owner_id, size); deleting an object must delete the row
- [[api]] — api issues presigned PUT URLs; never proxy file bytes
```

- Front matter is optional: a leading block between `---` lines; only `paths:` is read (comma-separated globs relative to the project root; no YAML library).
- Key = file stem; title = first `# ` heading, else the stem; summary = first non-empty paragraph that is not a heading, cut at 400 characters.
- Edges = links on lines under a heading called "Connections" (any level, any case; it ends at the next heading of the same or a higher level) to another note in the folder: `[x](file.md)` (relative to the note) or `[[name]]`. One edge per line and target; the edge text is the line without the link syntax and leading list marker, dash or colon. Links elsewhere in the note are not edges; links to missing notes or outside the folder are `broken_links`.
- **Editing from the UI / REST / MCP** rewrites only the *managed parts* of a note and keeps everything else byte for byte (other sections, prose, other front-matter lines, other Connections lines, line endings): the front-matter `paths:` line (the `---` block is created or removed with it), the first `# ` title, the summary (the first paragraph outside Connections; inserted after the title when missing) and, inside `## Connections`, the lines that link to one other note of the folder. An edge is written as `- [<Target title>](<target-key>.md) — <contract>`; a hand-written line (wikilink style, a colon...) is left alone until its edge is replaced from the UI. A new edge goes last in Connections (the section is appended when there is none); a value that equals what the note has is not rewritten. A Connections line that links several notes is never edited (`set_edge` / `remove_edge` refuse with the line number). Keys are lowercase `[a-z0-9][a-z0-9-]{0,39}` and cannot be renamed (they are the file stem); title <= 80 chars, summary <= 1000, contract <= 500 on one line without markdown links, paths <= 20 globs without `,` or `..`. Deleting a component removes its note and the lines in other notes that link only to it, and is refused while an open ticket lists it in `components` (done tickets keep the key).
- A ticket belongs to a component when it lists it in its `components` field (explicit: the keys, checked against the notes; Edit → Components, bulk bar, `--component`, `create_ticket(components=...)`) or when one of its `touches` overlaps the note's `paths` (derived). Naming the note in `docs` does not link it. `components` is refused when the board has no architecture (the error says how to set it up) or the key is not a note. Explicit comes first in `ticket_map`; `split` copies it. Agents are told (`WORK_LOOP.md`) to set it when they create or split tickets.
- The ticket's `context` then carries those notes in full (source `component:<key>`) plus `architecture`: `{"components": [keys], "neighbours": [{"key", "title", "path", "dir", "trust": "untrusted", "text", "summary"}], "others": [keys]}`, one contract line for each edge (`text` and `summary` are wrapped in `<untrusted>` tags like any note content) to a 1-hop neighbour (`dir` `out` = the ticket's note links to it, `in` = its note links to the ticket's) and only the names of everything else. Whole-architecture dumps hurt agents; the neighbours' contracts are what they need before changing an edge. `WORK_LOOP.md` tells workers to read them and to update the Connections line in the same commit when they change what crosses an edge.
- `GET /architecture` and MCP `architecture_map`: `{folder, components: [{key, path, title, summary, paths, tickets, counts: {todo, pending, doing, review}}], edges: [{from, to, text}], broken_links, ticket_map: {open id: [keys]}, problems: {no_contract: [{from, to}], dead_paths: [{key, glob}], broken_links, cross: [{ticket, components}]}}`. `cross` = open tickets on two or more components (blast radius). `dead_paths` is rechecked at most once a minute (a file you add or remove shows within 60 s; a write through unblockd shows at once). A folder that does not exist yet gives `folder` and no components (no error; the first `POST architecture/components` creates it); a folder outside the project gives an `error` string; `folder: null` = off.

## Skills and MCP servers

unblockd **declares** what a worker should have; the agent harness (Claude Code, your orchestrator) **loads** it. unblockd never starts, installs or probes a server and never stores a secret. One committed registry in `unblockd.json` is the allow-list; everything else selects from it by name, so a ticket (UI form, agent proposal) can only point at what the board already vouches for.

```json
{
  "skills": {"pytest-runner": {"path": ".claude/skills/pytest-runner/SKILL.md", "description": "Run and read the test suite (one line, <= 200 chars)"}},
  "mcp": {"postgres": {"hint": "claude mcp add postgres -- <command> (see docs/mcp-postgres.md)", "doc": "docs/mcp-postgres.md", "required": true}},
  "use_skills": ["pytest-runner"],
  "agents": [{"name": "backend", "skills": ["pytest-runner"], "mcp": ["postgres"]}]
}
```

- **Registry rules** (checked when the config loads): a skill `path` is a text file inside the project and **not** under the tickets folder (`tickets/files/<ID>/` is writable by anyone who can attach a file); an `mcp` entry has only `hint` (one line, <= 300 chars), optional `doc` (a project path) and `required` (bool): no command, URL or env. Every agent can read this config through `board_info`, so **never put credentials in `hint`**.
- **Levels**: `use_skills` / `use_mcp` = every ticket on the board; `agents[].skills` / `mcp` = tickets routed to that profile; a ticket's own `skills` / `mcp` (Edit form pickers, `--skill` / `--mcp` on the CLI, `create_ticket` / `update_ticket` in MCP) = just that ticket. They add up, deduped by name; a ticket cannot remove a board-level entry. Names removed from the registry later stay on old tickets and are ignored. `split` copies them.
- **Board rules in the claim**: `context.rules` = the board's `schema`, `agent_tickets`, `review`, `humans` (human owner names), `limits` (as in `board_info`) and `components` [{key, title}] when it has an architecture, so a worker needs no `board_info` call before it creates or splits a ticket.
- **In the claim**: `context.capabilities` = `{skills: [{name, description, path, source: board|profile|ticket, trust: "trusted"}], mcp: [{name, hint, doc, required, source}]}`. Descriptors only: a worker reads a skill with `read_doc(path)` when it needs it. At most 8 of each, the rest named under `more`. Absent when nothing applies. `context.profile` = `{name, instructions, model}` of the routed profile. `work_plan` groups list each profile's `skills` / `mcp`.
- **Enforcement lives in the harness**: the work loop tells a worker that if a `required` MCP server is missing from its tools it must `release` the ticket with the reason `missing capability <name>` before spending the lease.

## Chat: ask about tickets and boards

**Ask** (or `c`) opens a chat beside the board (opening a ticket or Needs you
closes it, and opening it closes them; the conversation is kept). Claude answers with read-only
tools over the same data (boards, tickets, `next_ready`, `work_plan`), so it
checks instead of guessing. Link things in a question and they're looked up
before Claude sees it:

| Type | Links |
|---|---|
| `#BUG-10` | a ticket on this board (autocompletes) |
| `#website/UI-2` | a ticket on another board |
| `@website` | a board (its open tickets) |

Answers link back the same way: click a ticket to open it, or another board's
ticket to jump there. Each ticket's drawer has **Ask** to start a question
about it. History stays in your browser, per board.

Needs `ANTHROPIC_API_KEY` in `.env` (compose passes it to the container);
`UNBLOCKD_MODEL` picks the model. Each question is billed to that key and
usually takes a few tool calls. The chat can't change tickets: it tells you
what to change, except that it can propose tickets as cards you confirm. A card's
`blocked_by` / `parent` may name another card of the same answer as `#1`, `#2`...
(1-based): that card's Create stays disabled until the one it points to is created, then
uses its real id.

**MCP prompt `plan_from_spec(board, path)`**: turns a spec / PRD in the project repo into
tickets. The agent reads it with `read_doc`, checks `list_tickets(fields=["touches", "parent", "done_when"])` for overlap, proposes at
most 10 tickets (concrete title, 1-4 `done_when`, `touches`, real `blocked_by`, `parent` for
a group), then challenges the draft (step 3b, required: a subagent or fresh context given only the
draft and the overlap rows, else an explicit pass; per ticket one line each of steelman, top
premortem risk, overlapping ids, and keep / edit / drop), shows the plan with a Challenge column and waits for the human's approval before `create_tickets`, then
creates the whole plan in ONE `create_tickets` call (tickets of the plan refer to each other as `#N`). The spec is treated as data. Text: `unblockd/PLAN_FROM_SPEC.md`.

## Configure: `tickets/unblockd.json`

Optional. Without it you get two owners (`Me`, `Claude`) and no pillars.

```json
{
  "title": "My game",
  "owners": [{"name": "Dana", "kind": "human"}, {"name": "Claude", "kind": "agent"}],
  "default_owner": "Claude",
  "pillar_label": "Pillar",
  "pillars": [
    {"key": "ART", "name": "Art", "color": "#8A4FB5", "color_dark": "#B58CE6"},
    {"key": "COMBAT", "name": "Combat"}
  ],
  "stale_days": 7,
  "legacy_files": ["../docs/OLD_BACKLOG.md"]
}
```

| Key | Meaning |
|---|---|
| `owners` | Who tickets can belong to. `kind: human` gets the warm chip; new tickets from agents default to the first `agent`. Other names are refused. |
| `pillars` | Workstreams. Each is a board colour, a filter chip and a graph lane. Colours are optional (a palette checked against the status colours is used). With no pillars, any `pillar` value is accepted. |
| `pillar_label` | What your project calls them ("Area", "Track"…). |
| `stale_days` | A `doing` ticket untouched this long is flagged; `work_plan` also marks a todo ticket with no event or edit for this long (`idle_days`, AI-57). |
| `wip_limit` | More than this many `doing` tickets shows a WIP badge on the column (default `null` = none). |
| `done_window_days` | The board's Done column shows only tickets done within this many days (default 7, whole number > 0), newest first, at most 20 cards, with `+N more → view all`. UI only: the API, MCP and CLI still list every done ticket. Edited in `unblockd.json` like every other board setting. |
| `history_days` | Database boards (SQL, MongoDB) delete activity events older than this many days (default 365, 1 to 36500; `null` = keep forever). Checked at most once a day per board, right after a write. Folder boards ignore it: their `activity.jsonl` rotates at 5 MB instead. See Storage connectors. |
| `lease_minutes` | A claim with no heartbeat this long expires (default 15). A profile can set its own (`agents[].lease_minutes`). |
| `lease_max_minutes` | How far ahead `heartbeat(until=)` may keep a claim alive (default 240, at most 1440). |
| `legacy_files` | Old ticket lists (relative to the tickets folder): new IDs never reuse a number seen in them. |
| `id` | Board id (default: the project folder's name). |
| `agents` | Agent profiles, see below (each may have its own `docs`). |
| `docs` | Knowledge every ticket on the board comes with, see Knowledge. |
| `docs_by_tag` | `{tag: [repo paths]}`: knowledge only tickets with that tag get, after the board docs and before the profile's; trusted like board docs; shown as "by tag" in the Docs view. |
| `architecture` | Repo folder of component notes (default `docs/arch`, on when the folder exists; `null` = off), see Architecture. |
| `context_budget` | Characters of docs inlined per ticket context (default 60000); the rest is listed. |
| `skills`, `mcp` | Registries of the skills and MCP servers your agent harness provides, see Skills and MCP servers. Default `{}`. |
| `use_skills`, `use_mcp` | Registry names every ticket on the board gets (default `[]`). A profile (`agents[].skills` / `mcp`) and a ticket (`skills` / `mcp`) add more. |
| `schema` | Optional rules for new tickets, see below. Default `null`: anything goes. |
| `milestones` | The roadmap, in order: `[{"key": "v0.1", "title": "Foundations", "goal": "one line", "exit_criteria": ["..."], "target": "2026-11-15", "closed": false}]`. Only `key` (letters, digits, `. _ -`) and `title` are required. See Milestones below. Default `[]`. |
| `strict_sessions` | `true` = a heartbeat / complete / release from another session of the claim's worker name is refused, not just warned (default `false`; see Two workers under one name). |
| `session_tickets`, `docs_resend` | Claims per worker session before `claim_next` says stop (default 5) and how long an already-sent board / profile doc is skipped (`{"claims": 3, "minutes": 30}`); see Lean worker context. |
| `worker_tools`, `agents[].tools` | MCP tools a worker's server exposes with `--tools worker` (default: every tool the work loop names: claim_next, claim_ticket, heartbeat, add_note, complete, release, ask_human, read_doc, get_ticket, create_ticket, create_tickets, split_ticket, update_ticket, arch_set_edge, arch_remove_edge, agents_now) and what a profile adds; see Lean worker context. |
| `agent_tickets` | `"open"` (default): agents create ready tickets. `"propose"`: a ticket an agent creates is a proposal until a human accepts it; turn it on for boards where several people or agents create work you want to vet. |

### Ticket schema and the proposal gate

```json
"schema": {
  "prefixes": {"FOG": "fog + explored map", "UI": "web board"},
  "required": ["done_when", "priority"],
  "tags": ["bug", "spike", "chore"],
  "body_template": "## Why\n\n## Approach\n"
}
```

Every key is optional. `prefixes` maps a prefix to a one-line meaning (80 chars); `required` is any of `done_when priority touches docs pillar owner parent body`; `tags` is an allow-list (`agent:<name>` tags stay governed by agent profiles); `body_template` prefills the New ticket form and, for agents, means the body may not be empty. A wrong config is refused with the reason.

- **Strict for agents** (`create_ticket` needs your `agent` name; CLI `create --agent`; REST body `agent`): a ticket that breaks the schema is refused with every problem in one short message, and the prefix list (keys only; the meanings are in `board_info`). If no prefix fits, the agent asks a human (`ask_human`); it never edits `unblockd.json`. The explicit-`id` form is checked too.
- **Guided for humans**: the ticket is created and the response has `warnings` (the CLI prints them in its JSON). In the UI the New ticket form offers the prefixes with their meaning, marks required fields with `*` and highlights empty ones (you can still submit), prefills the body template and suggests allowed tags; the quick-add row uses an allowed prefix and a title-only ticket on a schema board shows a toast saying what to add, with an Open button.
- **Not checked**: split children (they reuse the parent's prefix) and tickets that already exist.
- No prefix and no id: a schema with exactly one prefix uses it; with several, the error lists them.

**Proposal gate** (opt-in, `agent_tickets: "propose"`): a ticket created with an agent name gets `proposed_by`, is pending and cannot be claimed. It appears in **Needs you** as a *Proposal* (who proposed it, Done when, body on expand) with **Accept** (`accept_ticket`, `accept ID`, `POST tickets/<id>/accept`) and **Reject** (deletes it; an optional reason is logged). Tickets created by humans (UI, chat Create button, CLI without `--agent`, or any front door with the name of a `kind: human` owner as the agent) and split children are never proposals. The webhook reports `proposed` and `accepted`.

### Milestones (the roadmap)

A milestone is a version of the roadmap: v0.1, v0.2... Put them in `milestones` in `unblockd.json`, **in order** (the position is the order; reordering the roadmap is a one-line diff in a PR). A ticket points at one with its `milestone` field, which is separate from `parent`, so a ticket can be part of a feature epic AND ship in a version. A key the board doesn't list is refused (one already on a ticket is kept, so removing a milestone never locks a ticket; such a ticket counts as unscheduled).

- **Progress is derived**, never stored: per milestone the counts of done, in review, in progress, ready, waiting on a person and blocked tickets.
- **Next** = the first milestone that is not `closed`. A human closes one by setting `"closed": true` after checking its exit criteria: the last ticket finishing does not close it (the Roadmap shows "All done: close it?").
- **Agents**: `claim_next` prefers the next milestone's tickets when two ready tickets have the same score (a tie-break: a higher score from another milestone still wins).
- **Unscheduled** = tickets with no milestone: the backlog.
- **Web**: the Roadmap view (`r`): one row per milestone, in order, with a bar split by state (done, in review, in progress, not started, waiting on a person, blocked), `done / total`, the Next badge and the target date (marked late when it has passed). Click a row to expand it: its goal, its tickets as chips (click one to open it), its exit criteria and Show on the board. The Next milestone starts expanded; tickets with no milestone are the Unscheduled row. Filters → Milestone (and Unscheduled), Group by → Milestone, the edit form's Milestone, the drawer's "Milestone" link.
- **Agents and scripts**: REST `GET roadmap` and `GET tickets?milestone=v0.1` (`-` = none), MCP `roadmap` and `list_tickets(milestone=)`, `create_ticket(milestone=)`, CLI `roadmap`, `list --milestone`, `create|update --milestone KEY` (`-` clears).
- **Moving from version epics** (one epic ticket per version with every ticket's `parent` set to it): add the milestones to the config, then `python -m unblockd.cli migrate-milestones REL-1=v0.1 REL-2=v0.2`: every ticket under each epic gets that milestone and its `parent` cleared. The epic tickets stay; delete or close them when you're happy.

## Tickets

```json
{"id": "BUG-10", "title": "Search ignores accents", "status": "todo", "priority": "P1",
 "owner": "Claude", "pillar": "COMBAT", "pillar_step": 1, "section": "",
 "blocked_by": ["CMB-4"], "tags": ["gdscript"], "touches": ["scripts/demon_3d.gd"], "components": ["combat"], "skills": ["pytest-runner"], "mcp": [], "waiting_on": "", "body": "cause -> fix -> tests",
 "rev": 1, "created": "2026-10-03", "updated": "2026-10-03"}
```

`rev` is a counter every write of the ticket file bumps (create = 1; old files without it read as 0; claims' heartbeats live in `claims.json` and do not bump it). It is read-only. To avoid overwriting a concurrent change, send the rev you last saw: REST `PATCH` with `If-Match: <rev>` answers 409 (`{"error": ...}`, "reload and apply again") when the ticket was written since; the web UI's edit form does this and keeps your typed draft on a 409. Without the header the last write wins (CLI and MCP do not send it).

`status` is `todo` · `doing` · `review` · `done`; `pending` is derived: a `todo`
with an open blocker or a `waiting_on` question *shows* as pending on its own. `ask_human` also records `asked_at` / `asked_by` (read-only; `answer` clears them): the web board's *Needs you* panel lists only questions an agent asked, a `waiting_on` set by hand shows as *Parked* on its card (clear it from the drawer). Create with a prefix (`BUG`) and the
next free number is picked. A `blocked_by` that would make a loop (A waits on
B waits on A) is refused with the loop spelled out.

Every ticket comes back with derived fields: `effective_status`,
`open_blockers`, `blocks`, `unblocks` (open tickets downstream, transitive),
`missing_blockers`, `stale_days`, `agent` and `score` = priority weight
(P0 8, P1 4, P2 2, P3 1) x (1 + `unblocks`).

**Lists for planners** (AI-47): `list_tickets` (MCP) returns short rows (`id`, `title`, `effective_status`, `priority`, `owner`, `pillar`, `milestone`, `tags`, `agent`, `open_blockers`); `fields=["touches", "parent", "done_when"]` adds exactly those keys per row (any ticket field or derived one: `claim`, `children`, `unblocks`...; an unknown name is refused listing them), so `body` and `notes`, the big ones, come only when named. That is what a planner needs to check overlap, at a fraction of `full=true` (every body and every note). `limit=N` = at most N rows (in id order, after the filters). **Who works on this file?** (CORE-52): `touches="unblockd/store.py"` (a path or glob) keeps the tickets whose `touches` overlap it by the same rule claims use (same path, one folder inside the other, a glob match), not a text search (`q=store.py` also hits every ticket that merely mentions it); `component=KEY` keeps the tickets that name that architecture component or touch its paths (the Map's rule). With either, each row also carries `touches` and `holder` (the agent holding a live claim, or null). REST `GET tickets?fields=touches,parent&limit=20&touches=static/*&component=web` (without `fields`, `touches` or `component` the rows are whole tickets, as the UI reads them), CLI `list --fields touches,parent --limit 20 --touches PATH --component KEY`, `Remote.list(...)` with the same keys.

**Batch create** (a plan in one step): `create_tickets(tickets, agent)` (MCP), `create-many --file PATH|-` (CLI, a JSON array), `POST tickets/batch` (REST, `Remote.create_many`). Each item is what `create_ticket` takes (prefix or id, title, fields). In `blocked_by` and `parent` an item may name an existing id or `"#N"` = the Nth item of the same list (1-based), so the tickets of a plan can wait on each other before they have ids; forward refs are fine, loops are refused and spelled out. It runs under one lock and is all or nothing: one bad item refuses the whole batch with every problem named by item (`#2: title is required | #4: no ticket NOPE-1 on this board`) and nothing is written. The schema and the proposal gate apply to each item exactly as for `create_ticket`. At most 50 per call. The reply: `tickets`, `ids` (`{"#1": "API-7", ...}`) and, for a human whose items break the schema, `warnings` by item. One `created` event per ticket (plus `proposed` on a gated board).

**Batch update** (CORE-51: re-prioritise, retag, set milestone / parent / components on a set): `update_tickets(tickets, agent)` (MCP), `update-many --file PATH|- [--agent A]` (CLI, a JSON array), `PATCH tickets` with `{"tickets": [...], "agent"?}` (REST, `Remote.update_many`; the web board's bulk bar sends its whole selection this way, and its Undo is one more such request). Each item is `{"id", "changes", "expected_rev"?}`: `changes` is what `update_ticket` takes, `expected_rev` the rev you read (a ticket written since = a conflict, REST 409). Every item gets `update`'s checks (fields, the holder rule, the worker limits, blockers, loops; the batch's own new `blocked_by` / `parent` / `status` count, so two items can't make a loop and a blocker set to done in the same batch lets its dependent move to doing). It runs under one lock and is all or nothing: one bad item refuses the whole batch with every problem named by item and ticket (`#3 UI-12: invalid priority ...`), nothing written. At most 200 per call. The reply: `tickets` (MCP: one short ack per ticket, in order). `migrate-milestones` uses it (one call per 200 tickets).

**These tickets** (API-23): `list_tickets(ids=["AI-56", "API-23"])` (MCP), REST `GET tickets?ids=AI-56,API-23`, CLI `list --ids AI-56,API-23`, `Remote.list(ids=[...])` return exactly those tickets, in that order (at most 200; ids not on the board are left out, and the MCP reply then is `{tickets, hint}` naming them); other filters still apply. `q` is a search where every word must appear in one ticket, so a pasted list of ids finds nothing: a `q` with no hits answers `{tickets: [], hint}` on MCP (CLI: `hint:` on stderr) saying so, and pointing at `ids=` when the words look like ticket ids.

**Notes** are a list on the ticket, separate from `body` (the description): `"notes": [{"by": "backend-1", "at": "2026-10-20T10:00:00+00:00", "kind": "note", "text": "found the cause"}]`, oldest first. `add_note` (MCP / CLI `note` / REST `POST tickets/<id>/notes`) adds a note of kind `note` (progress), `decision` (a choice and why), `finding` (a cause, an error, a fact found) or `warning` (don't do X: it breaks Y); notes are numbered (`n`), `reply_to` = the note answered, `supersedes` = a pinned note it replaces (a reversed decision); asking, answering (`q` holds the question), approving, rejecting, accepting, releasing with a reason, an expired claim and `complete` (`status`, plus the `done_when` it was finished against) add their own kinds. They are append-only: `update` refuses `notes`, and editing the body never touches them. Tickets written before notes were a field kept them as lines in the body; they are split out when read and saved as `notes` on the ticket's next write.

## The board

![Needs you panel with a review row expanded](screenshots/needs-dark.webp)

**Needs you**: the purple `! N need you` badge next to the Board / Graph / Docs switch (any view, any board; hidden at 0; `i` opens it) counts questions agents asked (`ask_human`), work in review, agent-proposed tickets waiting for Accept / Reject and stuck work (a `doing` ticket with open blockers, with no update for `stale_days`, or whose blocker was deleted, or an unclaimed ticket that failed 3 or more times (expired / released / rejected) since a human last touched it: the row says why and has an *Add a note for the next attempt* box, and posting it clears the row; *Release to To do* or open it), for the board you are on (another board's items show when you open that board). It opens a side panel, ordered by score, then oldest. Each row has a one-line answer (or a primary Approve with a quiet "Request changes…" that opens the reason box) and expands (click it) to the full question, the result, Done when and the last 3 notes, with a multi-line box (Enter = newline, Ctrl/Cmd+Enter sends). A `waiting_on` you set by hand is *Parked*, not listed. **Notes after the work was finished** (AI-50): a review row says `A note after backend-1 finished: <the latest>` and, expanded (and in the drawer), lists them above the result, marking the ones that came before it finished but that the agent never read (`result.unread`). A *person's* note on a **done** ticket (written after it was done; an agent's note there stays a plain note) is listed as *Note after done* with **Reopen** (back to To do: the next worker gets the note, pinned) or **Dismiss** (nothing to do; Undo lists it again; REST `POST tickets/<id>/dismiss {notes: [n], by?, undo?}`, CLI `dismiss ID N... [--by] [--undo]`). Data: `GET /api/needs`. Motion (card slides, view cross-fade, panel rows) is short and switched off by `prefers-reduced-motion`. The same list is the MCP tool `needs_you(board=None)`, so your own Claude can triage it, then `answer`, `approve_ticket`, `reject`, `accept_ticket`, which reply with a short ack (`get_ticket` for the whole ticket).

**Result box** (a ticket in review or done, the review row in Needs you): the agent's `complete` summary comes first, as markdown, folded past about 600 characters (Show more); a trailing `NOT verified: ...` is pulled out as its own warning line, so you see what to check; then who did it, Files changed, References, Handoff. A done ticket's drawer opens on its Result (above Details), as a review ticket does, and the notes list does not repeat the summary. A ticket moved to Done by hand with no summary says *Finished without a summary* there, with a box to add one (Add summary); its Done card carries a small `no summary` mark and the digest counts them under Finished. Needs you does not list them.

**Priority** means: P0 breaks things or blocks people now; P1 must happen for the goal; P2 should happen; P3 nice to have; none counts like P3. The pickers (new/edit ticket, bulk bar) and the `?` sheet show these lines. Keyboard: `p` then `0`-`3` (or `-` to clear) sets the priority of the open ticket, else the focused card, with an Undo toast.

- **Links**: the view is in the address bar, so a link reproduces it: `?view=graph&q=auth&priority=P0,P1&pillar=…&owner=…&agent=…&parent=…&show=paths&group=pillar&dep=FREE&me=1` (`dep` only in the Graph; only non-default values appear; `?board=` and `#ID` are kept; bad values are ignored). A link with any of these defines the whole view; without them your remembered filters apply. Switching board drops them.
- **Header**: one row: logo, board title (switcher), view switch and the Needs you badge on the left; Ask, New ticket, Agents, ⚙ and `?` on the right. There is no status strip: the column heads carry the counts.
- **Ticket drawer**: opens beside the board (on wide screens the board moves over; it stays clickable, and clicking another card shows that ticket). `j` / `k` or the ↑ ↓ buttons go to the next or previous ticket. Its Details are a list of labelled fields. Editing still opens a form you save or cancel.
- **Columns**: To do, In progress, **Waiting** (the derived `pending` status; filters and links still say `pending`), Review, Done. Proposals are not shown as cards: Waiting starts with a **N proposals to review →** button that opens Needs you, where you accept or reject them (a search still finds them on the board). With no Group by, Waiting has sections **On you** (an agent question), **On other work** (open or missing blockers) and **Parked** (a hand-set `waiting_on`); each card opens with its reason (`Question from backend-1`, `Blocked by API-2`...). Owners show a person glyph (human) or a bot glyph (agent) beside the name; card titles clamp to two lines.
- **Next up**: the best ready tickets by score. Its button sits in the search row next to Filters; the list opens above the columns. It starts collapsed at every width until you pick a size (remembered per board).
- **Empty columns** shrink to a thin strip on wide screens (you can still drop a card on one).
- **List**: the icons next to Filters (or `l`) show the board as one line per ticket instead of columns: sections by status, or by the Group by choice (e.g. milestone). Filters, search and Group by apply the same; `?layout=list` links to it.
- **Pillar outlines** on cards, pillar dots on the filter chips.
- **Component** (boards with an architecture): the parts of the system a ticket changes, as against the *pillar*, the kind of work (one per ticket: prefix, colour, which agent). Set it in the edit / New ticket form (a picker, like Blocked by: chips are the value, options are the components), for the selected cards from the bulk bar (**Component…** *adds* it to each), or `components` over REST / MCP / CLI `--component` (repeatable; on `update` it replaces the list). The Filters panel has a **Component** filter and Group by has **component**: a ticket matches / is listed under each of its components, explicit or derived from `touches` (a ticket on two components is in both lanes: that is its blast radius; no component = last lane). The drawer shows them as chips.
- **Picking from existing values** (edit / New ticket form): Docs, Touches and Tags suggest what the project and board already have (doc files and folders, any file or folder, tags in use plus `agent:<name>`), but take anything you type: Enter or a comma adds it, so globs and paths that do not exist yet still work. Section and the ID prefix (also the To do quick-add prefix, without a schema) offer the values used on the board. A project with more than 3000 files lists only the first ones and the suggestions say so.
- **Warnings**: missing blocker, doing with no update for `stale_days` (an amber dot in the graph, and a *Stuck* row in the Needs you panel).
- **Free / Blocked** filter (Graph view only; the Board ignores it): *Free* = open, nothing open blocks it, not waiting on a person; *Blocked* = an open ticket blocks it. Counts on the chips; the graph dims the rest.
- **Show: Only / Paths / Dim** (next to the filters, remembered per view): *Only* shows just the tickets the filters match; *Paths* adds every ticket upstream and downstream of a match (the whole chain, not siblings); *Dim* shows everything and fades the rest. Works with every filter, so "owner Omri, Paths" is every chain that touches one of his tickets. Board default is Only, graph default is Dim.
- **Regex search**: wrap the search in slashes, `/^CMB-|BUG-10/`, to match ids and titles (case-insensitive). An invalid pattern turns the box red and matches nothing.
- Drag a card between columns to change its status. Polls every 4 s, so agent edits show up live.

![The board at phone width: one column at a time behind tabs](screenshots/mobile.webp)

**Graph** (Board / Graph switch): arrow A -> B means A blocks B. Fill =
status, ring = pillar, size = how much is waiting on it.

- **Flow** layout (default): columns = chain depth, rows = pillar lanes,
  crossings reduced by barycenter sweeps. **Free**: force layout.
- Done tickets are hidden until you tick **Show done**; **More** holds Mentions in details, Critical path and Isolate on click. On a big board (over 80 tickets drawn) the links stay faint until you click a ticket.
- Hover = direct links; click = the whole chain and the longest open chain
  to it; **Critical path** toggle; double-click opens the ticket.
- In Only / Paths the graph drops hidden tickets and re-lays out what's left; in Paths, chain-only tickets are drawn faded. Click a ticket, then **Show only this chain** to search its id and switch to Paths. Clear the search to go back.
- **Isolate on click** (toolbar checkbox, on by default): clicking a ticket draws only its whole chain (everything upstream and downstream, over the whole board, ignoring the filters, Show mode and "Linked tickets only") and re-lays it out. **Back to all**, Esc or a click on empty canvas returns to your view, filters untouched. Untick the box for the old highlight-and-dim focus.
- **Shift-drag A onto B** makes A block B; click a link to remove it.
- "Chains as text" under the canvas for screen readers; pinch and wheel zoom.

Keyboard: `Ctrl/Cmd+K` command palette (jump to a ticket by id or title, new ticket, switch view or board, Needs you, and the open ticket's status and priority), `/` search, `l` columns or list, `g` Board/Graph, `d` Docs, `n` new, `i` Needs you, `Shift+B` board switcher, `p` priority, `j`/`k` move, `Enter` open,
`t` theme, `?` shortcuts, `Esc` close. The ⚙ button sets theme (System / Light / Dark), density
(Comfortable / Compact) and the "While you were away" digest, per browser. On a board with 2+ human owners it also has **I am** (one of them, or Not set): the web UI then sends that name as `by` on answer, reject, accept, notes and split, so the signed body note and the activity log say who clicked instead of `human`, and the Filters panel gets **Mine** (tickets you own, or that an agent claimed or finished for you; link param `me=1`). It is a label you pick, not a login.
**Agents** (header button, labelled `Agents · N live` while claims are held) shows, per agent profile over 7 or 30 days, tickets finished, median time to finish, stuck rate (expired + released claims, flagged at 30% or more), splits and who is working now.

### Lean worker context (fewer tokens per claim)

- **Short write replies**: the MCP `heartbeat`, `add_note`, `update_ticket`, `release`, `ask_human` and `complete` (and the human side: `answer`, `approve_ticket`, `reject`, `accept_ticket`) return `{ok, id, status, effective_status, rev, note, lease_left_s?, warnings?}` instead of the whole ticket (REST and CLI still return the ticket).
- **Finish and take the next in one call** (AI-42): `complete`, `release` and `ask_human` take `next=true` and answer `{done: <the short reply>, next: <what claim_next answers>}`: the next ticket with its context, or `{ticket: null, reason}` (conflict / waiting / dry / `session_done`: the per-session cap still ends the session, and docs already sent are skipped the same way). A claim that fails after the write landed comes back as `next: {ticket: null, error}`. CLI: `complete|release|ask ... --next` prints the same shape (works over `UNBLOCKD_URL` too: two requests).
- **Worker tool set**: `python -m unblockd.mcp_server --tools worker [--profile gui]` (or env `UNBLOCKD_MCP_TOOLS=worker`, `UNBLOCKD_PROFILE=gui`) exposes only `claim_next, heartbeat, add_note, complete, release, ask_human, read_doc, get_ticket` (every tool description is sent on every model call, so fewer tools = fewer tokens per turn). The board's `worker_tools` replaces that list, a profile's `tools` adds to it and its `tools_off` removes from it (`"agents": [{"name": "gui", "tools": ["attach_file"], "tools_off": ["read_doc"]}]`), and `--tools a,b,c` names them directly. **In the web board:** Agents → Profiles and tools: one box for every worker and one per profile; click a tool to switch it (dashed = inherited from every worker; switching it off in a profile affects that profile only), then Save to unblockd.json. Workers pick it up when they next start. REST: `GET tools`, `PUT tools`. Without `--tools` (or `all`) every tool is there, for you and orchestrators. Outside tools (a browser MCP for GUI work) are declared with the `mcp` registry and loaded by the agent's harness.
- **Docs once per session**: on the stdio server (one process = one agent session) a board or profile doc already sent to that worker within `docs_resend` (default `{"claims": 3, "minutes": 30}`: both must hold), unchanged, comes back as `"content": null, "unchanged": "... read_doc(path) if you no longer have it"`. The ticket's own docs, component notes and attachments are always sent; a changed doc, or one sent longer ago than the window, is sent again; `claim_next(fresh=true)` sends everything (after the agent's context was compacted).
- **Missing tools**: a worker without a tool it needs calls `release(id, agent, reason, missing="browser", why="must click the login button")` (CLI `release --missing --why`, REST body keys). The ticket gets `needs: [{tool, why, profile, by, at}]` and a pinned warning note, Needs you shows it as stuck ("Needs tool browser (profile gui doesn't have it): ... Give that profile the tool, then Clear."), and workers of that profile skip the ticket (`claim_next` counts it as waiting on a human, so they don't retry) until someone clears `needs` (the Clear button, or `update needs: []`).
- **Tickets per session**: `session_tickets` (default 5, `null` = no limit): after that many claims in one stdio MCP session `claim_next` answers `{ticket: null, reason: "session_done"}` and the worker stops, so a long session doesn't run into context compaction (where the agent's harness summarises away what it was sent). Elsewhere (CLI, the shared HTTP server) the protocol says the same.
- **Writing rule** (in the worker protocol): short, but keep everything a brand-new agent would need to do the work without knowing what happened before: every rule, condition, number, path, command, decision with its reason, and warning.
- **Notes on claim**: every note while their text is short (3,000 chars); past that every pinned note plus the last 5 others, and any note a kept one replies to or supersedes; `notes_omitted` says how many were left out (`get_ticket` has them all). Pinned = `decision`, `finding`, `warning` notes, anything a human wrote, answers, rejections, approvals.

## Connect the MCP server

**One shared server (recommended).** `docker compose up -d` starts the board (`:8787`) and `unblockd-mcp` (`:8788`, published on this machine only), both with the same `PROJECTS_DIR`, so every project's board is reachable through one MCP endpoint and the tools are always the code in this checkout (Python edits: `docker compose restart`). Without Docker: `python -m unblockd.mcp_server --http`.

```sh
claude mcp add --transport http --scope user unblockd http://localhost:8788/mcp
claude mcp add --transport http --scope user unblockd http://localhost:8788/mcp --header "Authorization: Bearer <token>"   # when UNBLOCKD_TOKEN is set
```

`--scope user` = every project of this user (stored in `~/.claude.json`); `local` (default) = this folder only, `project` = a committed `.mcp.json` (each person approves it once). Replacing an old stdio registration: `claude mcp remove unblockd --scope user`, then add it again. Sessions started before the change need a restart (`/mcp` shows the state).

| Env | Meaning |
|---|---|
| `MCP_PORT` | Port of `--http` (default `8788`). |
| `HOST` | Bind address (default `127.0.0.1`; compose sets `0.0.0.0` inside the container and publishes `127.0.0.1:8788` only). |
| `UNBLOCKD_TOKEN` | Set: every request needs `Authorization: Bearer <token>` (constant-time compare, else 401 JSON saying how to fix it). Unset: open; beyond loopback it refuses to start unless `UNBLOCKD_ALLOW_OPEN=1`. Same token as the web API. |
| `MCP_WAIT_MAX` | Longest `claim_next(wait_s=)` wait on the shared `--http` server, in seconds (default 30, at most 120): each waiting call holds one of the server's threads. |
| `MCP_ALLOWED_HOSTS` | Extra `Host` values, comma list (`mypc:*`, `name:8788`). DNS-rebinding protection stays on: only `localhost`, `127.0.0.1` and `[::1]` (any port, so a remapped compose port works) are accepted otherwise (421). Needed when you reach the server by another name, e.g. from WSL or another machine. |

The server is shared, so the agent must say which board: a repo's board id is its `id` in `unblockd.json` (folder name when created by `create_board`), and every tool takes `board=`. Put this in each repo's `CLAUDE.md`:

```md
Tickets for this repo live on the unblockd board `<repo>`; pass board=<repo> to every unblockd tool.
```

Better: let unblockd write it. `python -m unblockd.cli --dir <repo>/tickets agents-md` prints a block of at most 15 lines (between `<!-- unblockd:agents-md start -->` / `end -->` markers) naming the board and the steps every session must take: claim before you start, heartbeat, `complete(summary, files, refs)` (never `update` to done), `ask_human` / `release`, plus the CLI form for agents without MCP; it points at `unblockd://work-loop` for the rest. `--board ID` names another board; `--write CLAUDE.md` appends it to that file (created if missing; refused when the markers are already there; every existing byte kept, CRLF files get CRLF). `create_board` (MCP, `POST /api/boards`) replies carry the same block as `agents_md` plus an `agents_md_hint`. The tools it names are checked against `WORK_LOOP.md` by tests/test_agents_md.py, so the two cannot drift.

**Without Docker / per client (stdio).** Each client spawns its own server; the Docker form uses the built image, so tools stay as old as the last `docker compose build`:

```sh
claude mcp add unblockd --scope user -- docker run -i --rm -v /path/to/projects:/projects unblockd python -m unblockd.mcp_server
```

Or install the package (`pyproject.toml`; stdlib only; extras: `[mcp]` for the MCP server, `[postgres]` / `[mongo]` / `[db]` (both) for database boards, `[all]` for everything) and use its commands, no `PYTHONPATH`: `pip install ".[mcp]"` from a clone, then `claude mcp add unblockd -e UNBLOCKD_ROOT=/path/to/projects -- unblockd-mcp`. It also installs `unblockd-web` (the board, same env vars as `python -m unblockd.web`) and `unblockd` (the CLI). Without installing: `uvx --from . unblockd-web` (uv). An installed copy serves the UI from `unblockd/static/` inside the package; a checkout serves `static/`.

**Claude Desktop** (`claude_desktop_config.json`) only starts local (stdio) commands:

```json
{"mcpServers": {"unblockd": {"command": "docker",
  "args": ["run", "-i", "--rm", "-v", "/path/to/projects:/projects", "unblockd", "python", "-m", "unblockd.mcp_server"]}}}
```

Build the image once first (`docker compose build`), or use `ghcr.io/omri2003/unblockd` in place of `unblockd`. For the HTTP server in Desktop use its Connectors UI or an stdio bridge such as `npx mcp-remote http://localhost:8788/mcp` (not tried here).

Workers: the protocol is the file `unblockd/WORK_LOOP.md` (shipped in the package, same for every board, not configurable). The `work_loop` prompt (`board`, `profile`) and the `unblockd://work-loop` resource carry the claim, heartbeat, complete / release / `ask_human` protocol, so any MCP client can run it without the orchestrator skill. The file ends with two rules: *Never go silent* (stop a claimed ticket only through `ask_human` or `release`, never by just stopping) and *Your board's policy* (board / profile docs such as `AGENT_POLICY.md` say how to do the work; if they conflict with the protocol, the protocol wins and the worker says so in its summary). The Docs view lists the protocol as a **built-in** doc (`unblockd://work-loop`, `builtin: true` in `GET /knowledge`, openable with View file / `read_doc`, shown with generic values: workers get their board, profile and name filled in) that every open ticket reads; it is not inlined into `context()`, which carries `protocol: "unblockd://work-loop"` instead.

Tools: `list_boards`, `create_board`, `board_info` (owners, pillars, agent
profiles, unblockd's `version`; the server's initialize reply carries it too), `work_plan` (orchestrators start here), `next_ready` (single agents
start here for a read-only queue; `agent=`, `tag=`), `list_tickets` (`fields=`, `limit=`, `touches=`, `component=`: see Tickets), `get_ticket`, `create_ticket` (needs `agent`), `create_tickets` (a plan in one call, see Tickets), `needs_you` (what waits on a person: the Needs you panel's items, same as `GET /api/needs`), `accept_ticket`, `approve_ticket` (the human side of review: done + who approved),
`update_ticket`, `update_tickets` (several in one all-or-nothing call, see Tickets), `split_ticket`, `delete_ticket`; for workers `claim_next`, `claim_ticket`,
`heartbeat`, `release`, `ask_human`, `complete`, `agents_now`; for knowledge `get_context`,
`knowledge_graph`, `architecture_map`, `read_doc`, `attach_file`, `detach_file`; to edit the architecture `arch_add_component`, `arch_update_component`, `arch_delete_component`, `arch_set_edge`, `arch_remove_edge` (each takes `agent`; they rewrite only the managed parts of the notes, see Architecture; there is no CLI command for them). Every tool takes `board=`
(default: the first board).

**Errors and limits.** A write that breaks field rules is refused with ONE error listing every problem and its rule, e.g.
`2 problems: invalid done_when[1]: 251 chars, max 200; invalid priority: one of P0, P1, P2, P3 or null (limits: board_info)`; the input
is never echoed back. `board_info` returns those numbers as `limits` (`fields`: {items, chars} per field, plus `note_kinds`,
`question_chars`, `summary_chars`, `handoff_chars`, `notes_per_ticket`). `update_ticket`'s argument is `changes` (an object);
`create_ticket` requires a name; `add_note` takes `kind="progress"` as a plain note. `get_ticket` (and REST `GET tickets/<id>`,
CLI `get`) adds `blockers`: each `blocked_by` id with its `effective_status` (`missing` = no such ticket) and title.

**Who is acting: `agent` or `by`.** Every tool that names an actor accepts both names (worker tools document `agent`, the human side
`answer`, `approve_ticket`, `reject`, `accept_ticket` documents `by`); `agent` wins when both are given, and a call with neither is refused
with "say who is acting". So `approve_ticket(id, agent="Omri")` works like `approve_ticket(id, by="Omri")`. (`agent` on `next_ready` /
`list_tickets` is a filter, not an actor.)

### Checking a real client

Verified with Claude Code 2.1 (`claude -p`) against `python -m unblockd.mcp_server --http`: `work_loop` / `plan_from_spec` prompts, `claim_next` (lease, trust labels), `add_note`, `complete`, Bearer auth. Repeat it on any board:

```
claude mcp add --transport http unblockd http://127.0.0.1:8788/mcp            # add: --header "Authorization: Bearer $UNBLOCKD_TOKEN"
claude -p "/mcp__unblockd__work_loop <board> <profile>" --allowedTools "mcp__unblockd"
claude -p "/mcp__unblockd__plan_from_spec <board> <path-in-repo>" --allowedTools "mcp__unblockd"
```

MCP prompts are slash commands (`/mcp__<server>__<prompt> args`), not tools. `<profile>` is the profile name (`backend`), not the worker name; the worker then claims as `backend-1`. Check the result with `unblockd.cli get <ID>` and `tickets/.unblockd/activity.jsonl` (`claim`, `note`, `complete`). Without the header a token-protected server answers 401 and the client lists no tools.

## CLI

```
python -m unblockd.cli --dir <repo>/tickets next --owner Claude --agent backend
python -m unblockd.cli --dir <repo>/tickets plan --per-agent 3
python -m unblockd.cli --dir <repo>/tickets claim --agent backend-1 [--principal Omri] [--wait 120]   # or env UNBLOCKD_PRINCIPAL; heartbeat/release/complete take it too; --wait: on a conflict, wait for it to free up
python -m unblockd.cli --dir <repo>/tickets heartbeat BUG-10 --agent backend-1 --note "running tests"
python -m unblockd.cli --dir <repo>/tickets complete BUG-10 --agent backend-1 --summary "cause -> fix -> tests" [--next]   # --next: also claim the next ticket, prints {done, next}
python -m unblockd.cli --dir <repo>/tickets list   # open tickets (every status but done); --all adds done ones, --status S picks one, --ids gives exactly those whatever their status (REST / MCP: status=open)
python -m unblockd.cli --dir <repo>/tickets list --status todo
python -m unblockd.cli --dir <repo>/tickets knowledge   # open tickets with no doc of their own (+ the docs suggested for each) and broken doc references
python -m unblockd.cli system info|boards|connectors   # the system database (registered boards, connectors): see The system database
python -m unblockd.cli --dir <repo>/tickets agents-md [--write CLAUDE.md]   # the short block for CLAUDE.md / AGENTS.md (see Connect the MCP server)
python -m unblockd.cli --dir <repo>/tickets list --fields touches,parent,done_when --limit 50   # extra keys per row; body / notes only when named
python -m unblockd.cli --dir <repo>/tickets list --touches unblockd/store.py   # tickets whose touches overlap it (the claim rule), with touches + holder
python -m unblockd.cli --dir <repo>/tickets get BUG-10
python -m unblockd.cli --dir <repo>/tickets create --prefix BUG --title "..." --json '{"priority":"P1"}' --component api --component store --skill pytest-runner --mcp postgres   # names from the board's registry
python -m unblockd.cli --dir <repo>/tickets create --prefix BUG --title "..." --agent backend-1   # as a worker: strict schema, may park as a proposal
python -m unblockd.cli --dir <repo>/tickets create-many --file plan.json --agent planner   # a JSON array, all or nothing; "#N" = the Nth item (--file - reads stdin)
python -m unblockd.cli --dir <repo>/tickets accept BUG-11 --by omri
python -m unblockd.cli --dir <repo>/tickets split BUG-10 --title "part one" --title "part two" --sequential
python -m unblockd.cli --dir <repo>/tickets update BUG-10 --json '{"status":"done"}'
python -m unblockd.cli --dir <repo>/tickets update-many --file changes.json   # [{"id": "BUG-10", "changes": {"priority": "P1"}}, ...], all or nothing
python -m unblockd.cli --dir <repo>/tickets note BUG-10 "a signed, dated finding added to the ticket's notes"
python -m unblockd.cli --dir <repo>/tickets roadmap                          # milestones in order with progress
python -m unblockd.cli --dir <repo>/tickets update BUG-10 --milestone v0.2   # - clears it
python -m unblockd.cli --dir <repo>/tickets migrate-milestones REL-1=v0.1 REL-2=v0.2   # version epics -> milestones
python -m unblockd.cli health                                               # remote: GET /healthz; local: the folder; exit 1 when down
python -m unblockd.cli --version                                            # unblockd 0.2.0 (also: unblockd --version once installed)
python -m unblockd.cli --dir <repo>/tickets update BUG-10 --rev 3 --json '{"title":"..."}'   # refused (conflict) if written since rev 3
```

**Errors a program can parse.** Every reply is JSON. Errors too, whenever stdout is not a terminal (an agent or a
script reading it) or with `--json` before the command (`cli --json claim ...`): ONE object on stdout,
`{"error": "...", "code": "refused|not_found|conflict|down|usage"}`, exit 1 (`down` = the server did not answer,
`usage` = a bad command line). At a terminal a person still gets `error: ...` on stderr (argparse's usage errors: exit 2).

**Agents without MCP, and sub-agents** (a supported interface, not a workaround): MCP tools are configured per client, so a
sub-agent or a cloud session often has none. Set `UNBLOCKD_URL` (+ `UNBLOCKD_TOKEN`, `UNBLOCKD_BOARD`) and every CLI command
above goes to the shared server over REST (`cli.Remote`, the same operations and errors), or call the REST API below directly.
When nothing answers, every remote command fails with one message that starts `unblockd server down: nothing answers at <url>`
and says what to do (exit 1); `health` checks first. The server runs where you started it: a laptop that sleeps or shuts
down takes the board with it (the containers restart with Docker, `restart: unless-stopped`, but not while the machine is off),
so keep the host awake while agents work, or run it on a machine that stays on.

## API

```
GET    /healthz                       (no /api, no auth) {"status": "ok", "version": "0.2.0"}: container health check / k8s probes
GET    /api/boards                    every board with ticket counts
GET    /api/needs                     every board's tickets waiting on a person, for the UI's Needs you panel (read-only):
                                      [{"id", "title", "items": [{"id", "kind": "question"|"review"|"proposal"|"stuck", "title", "priority", "score", "unblocks",
                                      "since", "waiting_on", "missing_blockers", "stale_days" (only when stale), "done_when", "result",
                                      "body" (last 6000 chars); stuck items also "open_blockers": [{"id", "effective_status", "title"}] and "claim_note": {"by", "at", "text"}|null, "attempts": [...] and "failed": "Failed 3 times (expired ×2, rejected ×1)" (empty unless failing)}]}], boards with items only.
                                      Stuck = a missing blocker, a doing ticket with an open blocker (immediately), or a doing ticket stale for stale_days
GET    /api/needs?board=<id>          the same for one board only (the UI sends its own board; the others are not read); unknown id = 404
GET    /api/chat                      {"enabled", "model"}
POST   /api/chat                      {"board", "messages": [{"role", "content"}]} -> {"reply", "tools", "refs"}
POST   /api/boards                    {"id": "my-game", "title": "My game", "root"?: one of UNBLOCKD_ROOT}  -> the board + "path" (+ "host_path" in Docker);
                                      in a container a root that is not a mounted volume is refused (the board would vanish with the container)
# per board under /api/b/<board>/ ; without the prefix = the default board
GET    meta                           statuses, priorities, fields, config, version, storage {per_file_ms, passes, slow} (files boards; null on a database board)
GET    tickets?status=&pillar=&owner=&priority=&tag=&agent=&parent=&milestone=&q=&limit=&fields=&touches=&component=
                                      fields=a,b: summary rows with those keys only; touches= (overlap rule) / component= add touches + holder; none of the three = whole tickets
GET    next?owner=&pillar=&agent=&tag=&limit=
GET    plan?per_agent=
GET    agents                         who is working what now + recent activity
GET    activity?ticket=&limit=
POST   claim                          {"agent", "profile"?, "note"?, "principal"?}
POST   tickets/<id>/claim|heartbeat|release|complete   {"agent", "note" | "reason" | "summary", "principal"?, "session"?}   (session: or header X-Unblockd-Session; CORE-88)
GET    tickets/<id>/context?inline=0  docs + attached files to read before working it (?content=0: each doc's `sent`, without its text; a sent doc with 2 or more headings also gets `outline` + `outline_chars`; when a doc is too big or the budget ran out, `fits_budget` = the smallest `context_budget` in characters at which every doc would be sent)
GET    knowledge                      who reads what (tickets, docs, edges with the tag / profile that sent them, rules, budget, problems)
GET    architecture                   component notes, edges with contracts, open tickets per component, problems
POST   architecture/components        {key, title, summary?, paths?: [globs], by?}  new note (folder created) -> the architecture payload (201)
PATCH  architecture/components/<key>  {title?, summary?, paths?, by?}  only the managed parts of the note change
DELETE architecture/components/<key>?by=   400 while an open ticket lists it; else deletes the note and the lines linking to it
PUT    architecture/edges             {from, to, text?, by?}  create or replace the Connections line in `from`'s note
DELETE architecture/edges?from=&to=&by=   remove it   (by defaults to human; each write logs one `arch` activity event)
GET    files                          project file paths for the form's Docs / Touches suggestions (skips dot-folders, node_modules, tickets/; max 3000, `truncated`: the form then says it shows only the first files)
GET    doc?path=                      a text file from the project repo, with its links
GET    docs?path=a&path=b[&budget=]   several in ONE reply, within the board's context_budget: {docs, budget, used, note?}; each entry is a doc, {path, error} or {path, skipped: "budget", chars}
POST   tickets/<id>/files             {"name", "content"}     GET / DELETE tickets/<id>/files/<name>
GET    tickets/<id>
POST   tickets                        {"prefix": "BUG", "title": "...", "agent"?: worker name, ...}  (a human's ticket may return "warnings")
POST   tickets/batch                  {"tickets": [{"prefix"|"id", "title", ..., "blocked_by": ["#1"]}], "agent"?}  <= 50, all or nothing -> 201 {"tickets", "ids": {"#1": id}, "warnings"?: {"#N": [...]}}
POST   tickets/<id>/accept            {"by"?}  accept an agent's proposal
PATCH  tickets                        {"tickets": [{"id", "changes", "expected_rev"?}], "agent"?}  <= 200, all or nothing (update_many) -> {"tickets"}; 409 on an expected_rev conflict
PATCH  tickets/<id>                   {"status": "done"}   (If-Match: <rev> optional: 409 if the ticket changed since you read it)
DELETE tickets/<id>                   ?agent=, ?reason= (rejecting a proposal; logged)
```

**Auth**: none unless `UNBLOCKD_TOKEN` is set (then `Authorization: Bearer <token>` or the cookie from opening `/?token=<token>`). It listens on 127.0.0.1 by default (`HOST`, `PORT`); don't expose it wider without a token (a non-loopback `HOST` without one refuses to start unless `UNBLOCKD_ALLOW_OPEN=1`). Against cross-site requests (a web page POSTing to localhost): writes (POST/PATCH/PUT) must send `Content-Type: application/json` (else 415), an `Origin` header must match the request's `Host` and scheme (else 403; none = curl/CLI, allowed), DELETE needs no body. Responses carry `X-Content-Type-Options: nosniff` and `Content-Security-Policy: frame-ancestors 'none'`; the token cookie is `Secure` when `X-Forwarded-Proto: https`; a bad `Content-Length` is a 400.

## Storage connectors

A board's tickets live in one connector. **Files** (the default) is the `tickets/` folder in your repo. **SQL**
(experimental, CORE-30) keeps many boards in one database: SQLite (`sqlite:///path/to/unblockd.db`, nothing to install)
or Postgres (`postgres://user:password@host:5432/db`). **MongoDB** (experimental, CORE-31): `mongodb://user:password@host:27017/db`
(database `unblockd` when the URL names none; Atlas `mongodb+srv://` works the same). Postgres and MongoDB need
`pip install -r requirements-db.txt`, or with the package `pip install "unblockd[db]"` (`[postgres]` / `[mongo]` for one) (included in the Docker image).

**Serve a database board** by naming it, with `#board` after the URL, in `UNBLOCKD_BOARDS` (or `TICKETS_DIR`), next to
any folders; entries are separated like paths (`:` on Linux/macOS, `;` on Windows; a URL's own `:` is fine):

```bash
UNBLOCKD_BOARDS="sqlite:///unblockd.db#payments" python -m unblockd.web
```

```bash
python -m unblockd.cli --dir "sqlite:///unblockd.db#payments" list
```

The web board, MCP and CLI then work on it like on a folder board. Its id is the name after `#`; a board that is not in
the database yet is created on first use. The password never appears in the board list or the logs. A database that
can't be reached shows an error on its own board only and is tried again every 30 seconds.

**Move a board** between any two places (folder, SQLite, Postgres, MongoDB) with the CLI; the target must be empty
(`--force` replaces what is there):

```bash
python -m unblockd.cli migrate --from tickets --to "sqlite:///unblockd.db#demo"
python -m unblockd.cli migrate --from "sqlite:///unblockd.db#demo" --to other/tickets
```

It copies the settings, every ticket exactly as stored (ids, `rev`, notes, results), attachments and the history,
then reads it all back and compares. Live claims are not copied: workers claim again (a ticket that was `doing` shows
as orphaned until someone moves it). Settings that point at repo files (`skills` paths, `mcp` docs) can't move into a
database yet: the command says so before it writes anything.

What every connector guarantees is one test suite, `tests/contract.py` (one ticket claimed by 8 at once has one holder,
parallel creates never share an id, same-rev edits: one wins, one gets 409...). On SQL everything the store does under
the board lock is one transaction: it all commits, or none of it does. MongoDB does the same on a replica set (Atlas, or
any `--replSet` server); a standalone `mongod` has no transactions, so there each write applies on its own, in order, still
one writer per board at a time (a crash halfway keeps the writes made so far). A lock left by a crashed worker expires
after 30 seconds (by the database server's clock). A database board has no repo yet, so repo docs
report "no repo linked", the Map is off and `components` are refused (CORE-36); attachments are stored in the database.
Tables are prefixed `ub_` and can share a database with other apps.

**History cleanup** (CORE-37): on a database board the activity log (claims, notes, releases, expiries, completions: what
While you were away, the Agents stats, failed attempts and notifications read) is kept for `history_days` (default 365,
`null` = forever). The first write of a UTC day, from any front door (web, MCP, CLI: a board with no web server is cleaned
too), deletes the events stamped before the cutoff, after the write's lock is released, and records the day, the cutoff
and how many went in the runtime document `history`, so other processes skip it that day. Only whole old events go: newer
ones, the event numbers and notify's cursor stay, so failed-attempt counts and stats count what is kept. An event with no
timestamp is kept. A cleanup that fails never fails the write (one line on stderr, tried again the next day). Folder boards
keep the 5 MB rotation of `activity.jsonl` (one old file kept) and ignore `history_days`. A migrated board brings its whole
history; the database side then trims it on the first write.


**In Docker.** The image has both drivers (about 24 MB more, in their own layer; CI's `image-db` job prints it with
`docker history`), so a database board needs no rebuild: set `UNBLOCKD_BOARDS` in `.env`. For a local database, compose
has optional `postgres` (16) and `mongo` (7, a one-node replica set, so locked writes are transactions) services, each behind
its own profile (`postgres`, `mongo`; name both for both), published on 127.0.0.1 only, with default passwords meant for local use:

```bash
docker compose --profile postgres up -d                    # Postgres only
docker compose --profile mongo up -d                       # MongoDB only
docker compose --profile postgres --profile mongo up -d    # both
```

```bash
# .env, one line; boards separated by ":" (host-side URLs for the CLI: comments at the end of compose.yaml)
UNBLOCKD_BOARDS=postgres://unblockd:unblockd@postgres:5432/unblockd#pay:mongodb://mongo:27017/unblockd?replicaSet=rs0#ops
```

**Load**: `python -m tests.load` (20,000 tickets, 200 agents; numbers in AGENTS.md).

## The system database

unblockd's own data, separate from every board's tickets (CORE-34): the boards registered with it and the **connectors**
(named database addresses boards live in). It is a SQLite file at `~/.unblockd/system.db` unless `UNBLOCKD_SYSTEM_DB`
says otherwise (a path, `sqlite:///...` or `postgres://...`; MongoDB holds boards, not this). Nothing to set up: the
first start creates it. The Docker image keeps it at `/data/system.db`; compose puts `/data` on the `unblockd-data`
volume so it survives recreating the containers.

Registered boards appear in the board list (web, MCP `list_boards`, `GET /api/boards`, marked `"registered": true`)
after the boards found by scanning (`UNBLOCKD_ROOT`, `TICKETS_DIR`, `UNBLOCKD_BOARDS`), and work from every front door.
A registered id that a scanned board already uses gets `-2`; a folder that is both scanned and registered is listed once.
A system database that can't be read hides only the registered boards (one line on stderr says why). `add-board` creates a
database board that is not there yet and says so (`new`: check the `#board` name if you meant an existing one); after that a
registered board is never created by opening it, so a mistyped or deleted one shows an error instead of an empty board.

```bash
python -m unblockd.cli system info
python -m unblockd.cli system add-board ops --location /srv/ops/tickets --title Ops
python -m unblockd.cli system add-connector main "sqlite:////srv/unblockd/boards.db"
python -m unblockd.cli system add-board payments --connector main            # board "payments" in that database
python -m unblockd.cli system boards
python -m unblockd.cli system remove-board ops                                # the registration only: tickets stay
```

`--location` is a folder (must exist) or a database URL + `#board`; with `--connector` it is the board's key in that
database (default: the id). A connector that registered boards use can't be removed.

**Passwords** belong to connectors, never to a board's address (refused there). A password in the connector's URL, or
typed with `--password-stdin` (keeps it out of shell history), is taken out of the address and stored **encrypted**
with the secret key (Fernet, from the `cryptography` package: `pip install "unblockd[secrets]"`, part of `[db]`,
`requirements-db.txt` and the Docker image; only needed once a password is stored). No command or API returns it
again: `system connectors` says `has_password`, every address shown has `***` in its place.

```bash
printf '%s\n' "$PGPASS" | python -m unblockd.cli system add-connector main postgres://app@db.internal:5432/unblockd --password-stdin
printf '%s\n' "$PGPASS" | python -m unblockd.cli system set-password main --password-stdin   # new password, or after a lost key
python -m unblockd.cli system set-password main --clear
```

A wrong or lost secret key does not crash anything: the boards in that connector show "the secret key ... is not the
one the stored connector passwords were encrypted with", every other board works, and the web server says so at start.
Put the old key back, or re-enter each password with `set-password`; adding a new password under a different key is
refused until then (it would mix two keys). Tables are prefixed `us_`
(boards use `ub_`), so the system database may share a database with boards. Every row names its organisation
(`default` for now), so organisations, users and roles come as new tables, not a rewrite.

## Database upgrades

Every database unblockd keeps data in (the system database, and each SQL or MongoDB database boards live in) stores its
schema version. When unblockd connects to one made by an older version it applies the missing numbered upgrade steps
itself: on SQLite and Postgres all of them in one transaction (all or nothing; two servers starting at once upgrade it
once), on MongoDB one step at a time (a crash resumes at the step that did not finish). One line on stderr says what was
upgraded. A database made by a NEWER unblockd is refused with "was set up by a newer unblockd (schema N, this one knows
M): upgrade unblockd". No migration library, nothing to run by hand.

Admins who want to choose the moment set `UNBLOCKD_AUTO_UPGRADE=0`: an older database is then refused ("run python -m
unblockd.cli db upgrade") until they run:

```bash
python -m unblockd.cli db status                       # every database: its schema and the one this unblockd needs; changes nothing
python -m unblockd.cli db upgrade                      # apply the missing steps
python -m unblockd.cli db upgrade --url "sqlite:///unblockd.db"   # just this one (repeatable)
```

Without `--url` they cover the system database, the databases named in `UNBLOCKD_BOARDS` / `TICKETS_DIR`, registered
boards and connectors (passwords hidden in the output). Exit code 1 when any database failed (its row says why). A new,
empty database is set up either way. Back up before upgrading production; there is no downgrade.

A database that is already current is only read on connect (no lock, no DDL), so the app's database user needs no CREATE
right (Postgres 15 denies it on `public` by default). When an upgrade is due and that user may not change the schema,
unblockd says so: run `python -m unblockd.cli db upgrade` as the database owner, then start it again. A server that
starts while another one is upgrading waits up to 10 s, then stops with "another unblockd is upgrading <database>; try
again in a minute".

## Develop

- New ticket field: one line in `FIELDS` in `store.py`; show it in `static/index.html`.
- New MCP tool: a function with `@tool` in `mcp_server.py` calling `Store`.
- New API route: a branch in `Handler._route` in `web.py`.

Tests: `python -m unittest`. UI changes show on a browser reload; Python
changes after `docker compose restart`.

## Docker Desktop (Windows / macOS): bind mounts are slow

unblockd reads the board from files, so a request stats files in the mounted project. On Docker Desktop a bind mount of a Windows or macOS folder costs about 0.6 ms per file stat, roughly 75-100x a native disk. Same code, same board (a 527-ticket project), on Windows:

| Request | Native Python | Container over the bind mount |
|---|---|---|
| `list()` (the board's tickets) | 4 ms | 300 ms |
| `GET /architecture` | 80 ms | 1.3-3.3 s |
| `GET /files` | 56 ms | 2.1 s |

What to do:

- **On Docker Desktop, prefer running the server natively**: `python -m unblockd.web` with `UNBLOCKD_ROOT` = your projects folder (the README's "Without Docker").
- **Or keep the repos inside the WSL filesystem** (e.g. `~/projects` in your distro) and mount that, not a `C:\` folder.
- **Linux bind mounts are fine**: there the mount is the native disk.

**The board tells you.** The server times the file stats its board reads already make (no extra ones) and keeps the median per file of the last 3 passes. Inside a container, over 0.1 ms per file (a native disk is about 0.02 ms) on a board of at least 10 tickets, `GET meta` says `storage.slow: true` and the board header shows a "Slow storage" notice linking here; Dismiss hides it for that board in that browser. Outside a container it never shows. `UNBLOCKD_SLOW_STORAGE=1` / `0` forces it on or off.

**Branch evidence needs `git` in the image.** The Dockerfile installs `git` for [branch evidence](#branch-evidence-work-that-lives-on-branches). An image built before branch evidence existed has no `git`, and the feature is silently off. Rebuild: `docker compose build`, then `docker compose up -d`. After that the scan runs `git` every 120 s per board on the mounted repo (its bare mirror lives in `tickets/.unblockd/git`), so on a Docker Desktop bind mount it pays the same per-file cost.

## Deploying the image (Kubernetes / OpenShift)

- **User**: the image runs as uid 1001 and `/app`, `/data`, `/projects` are owned by group 0 with group = owner permissions, so it also runs under OpenShift's random UID (restricted SCC). compose.yaml sets `user: "0"` only because Linux bind mounts of your own projects are not writable by 1001; don't copy that to a cluster.
- **Base**: `python:3.12-slim-bookworm` (the floating `3.12-slim` is not used). Pin a `@sha256` digest for fully reproducible builds.
- **Health**: `GET /healthz` returns `200 {"status": "ok", "version": "<running version>"}` with no auth even when `UNBLOCKD_TOKEN` is set. The Dockerfile `HEALTHCHECK` uses it; use it for liveness/readiness probes too.
- **Storage (files backend)**: tickets are files and the claim lock is a file lock, so run `replicas: 1`, `strategy: Recreate` (a rolling update would briefly run two pods on one volume) and mount a ReadWriteOnce PVC at `/data` (`TICKETS_DIR=/data/tickets`) or at `/projects`. Set `UNBLOCKD_TOKEN` (the container binds `0.0.0.0`).
