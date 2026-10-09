# unblockd

![unblockd board: agents working, tickets in columns, a badge for what needs you](docs/screenshots/board.webp)

**A ticket board where humans and AI agents work the same backlog.**
Tickets are JSON files in your repo: reviewed in PRs next to the code, no SaaS, no database.
Front doors: web, MCP, CLI, chat.
Humans get a live board and a dependency graph; agents get lease-based claims, so several of them work in parallel without stepping on each other.

**Why not Trello / Linear / GitHub Projects?** Those track work for people. Here a ticket knows what blocks it, which files it touches and which docs a worker must read, and an agent can claim, heartbeat and finish it through MCP or a CLI.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/needs-dark.webp" alt="Needs you panel in dark mode: an agent's question, a stuck ticket, a result waiting for review"><br><sub>Needs you: answer agents without leaving the board</sub></td>
<td width="50%"><img src="docs/screenshots/graph-dark.webp" alt="Dependency graph in dark mode: tickets as nodes, arrows from blocker to blocked"><br><sub>Blocking graph</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/drawer.webp" alt="Ticket drawer for a ticket in review: result, done when, notes, blockers"><br><sub>Ticket drawer</sub></td>
<td width="50%"><img src="docs/screenshots/mobile.webp" alt="The board at phone width"><br><sub>Phone width</sub></td>
</tr>
</table>

## Try it in 5 minutes

Needs Python 3.10+. The board and CLI are stdlib only.

**1. Run the board** with a copy of the demo project (never point it at `example/` itself):

```sh
git clone https://github.com/omri2003/unblockd && cd unblockd
mkdir -p /tmp/demo/shop && cp -r example/tickets /tmp/demo/shop/
UNBLOCKD_ROOT=/tmp/demo python -m unblockd.web
```

Expected: `unblockd on http://localhost:8787  boards: example (/tmp/demo/shop/tickets)`

*Docker instead (not run here):* `docker compose up -d --build` serves `./example`; set `PROJECTS_DIR` in `.env` for your own.

**2. Open the board** at <http://localhost:8787>: five tickets in columns, `g` switches to the graph, `d` to the docs agents read, `?` lists every shortcut.

**3. Create a ticket the backend agent can take** (pillar `API`, owned by Claude; the demo's other open tickets are human-owned, blocked or already claimed). Press `n` in the board, or from another terminal:

```sh
python -m unblockd.cli --dir /tmp/demo/shop/tickets create --prefix API --title "My first ticket" --json '{"pillar":"API","owner":"Claude"}'
```

Expected: JSON with `"id": "API-3"`, `"status": "todo"`; the card appears within 4 s.

**4. Connect Claude Code, no Docker:**

```sh
pip install mcp
claude mcp add unblockd -e UNBLOCKD_ROOT=/tmp/demo -e PYTHONPATH=$PWD -- python -m unblockd.mcp_server
claude mcp list
```

Expected: `unblockd: python -m unblockd.mcp_server - Connected`. `PYTHONPATH` must be this checkout, because the package is not pip-installed.

**5. Tell Claude:** *"use unblockd: run work_plan and work all ready tickets."* It plans, claims, heartbeats and completes tickets; watch cards move on the board. An empty claim returns `{ticket: null, reason}`: `dry` = nothing ready, `conflict` = ready tickets touch files a live claim holds (retry soon), `waiting` = work exists but is in flight, blocked, in review or waiting on a human.
If the board defines agent profiles (the demo has `frontend` and `backend`), worker names must be `<profile>-N`, e.g. `backend-1`; anything else is refused with a message listing the profiles.

**What the UI gives you:** the **Needs you** badge (`i`) collects, across all boards, questions agents asked, work waiting for your review and stuck tickets, and you answer, approve or reject inline. The title is the board switcher (`Shift+B`). Priority means P0 breaks things now, P1 must happen, P2 should, P3 nice to have (`p` then `0`-`3` sets it).

## Concepts

| Term | Meaning |
|---|---|
| **Ticket** | One JSON file in `<project>/tickets/`. Status `todo`, `doing`, `review`, `done` (`pending` is derived: a todo with an open blocker or a `waiting_on` question). |
| **Ready** | Open, nothing open blocks it, not owned by a human, not waiting on a person. Agents only claim ready tickets. |
| **Blocked / pending** | Derived, never set by hand: a `todo` with an open `blocked_by` shows as pending until its blockers are done. |
| **Pillar** | A workstream (Art, API, ...): a colour, a filter and a graph lane. Defined per board. |
| **Profile** | An agent type in the board config: which tags/pillars it works, its model and instructions. `work_plan` groups ready tickets per profile. |
| **Claim, lease, heartbeat** | `claim_next` gives a worker one ticket and sets it to `doing`. The worker sends `heartbeat` notes; with none for `lease_minutes` (15) the claim expires and the ticket returns to todo. |
| **done_when** | Short acceptance criteria: the bar for done, shown at the top of the worker's context. |
| **review** | Optional gate (`"review": true`): agents' `complete` lands in `review` and a human moves it to done. |
| **ask_human / Parked** | A decision only a human can make: the worker asks, the claim ends, the ticket waits in Needs you until you answer. A `waiting_on` you type yourself is *Parked*: it never shows in Needs you. |

## Configuring agents

Put your project rules (tests, commits, PRs, when to ask a human, summary language) in a doc such as `AGENT_POLICY.md` and list it in `docs` in `unblockd.json`; every worker reads it with its ticket. The protocol itself (`WORK_LOOP.md`: claim, heartbeat, complete / release / ask; a worker never goes silent) is built in and shows in the Docs view; your docs cannot change it. See `example/AGENT_POLICY.md`.

## Config and auth, briefly

| Env var | Meaning |
|---|---|
| `UNBLOCKD_ROOT` | Folder(s) of projects; every `<project>/tickets/unblockd.json` is a board. |
| `HOST`, `PORT` | Bind address (default `127.0.0.1`, `8787`). |
| `UNBLOCKD_TOKEN` | Optional shared token. Set: `/api/*` needs `Authorization: Bearer <token>`, or open `/?token=<token>` once to set a cookie. Unset: no auth, so keep it on localhost. MCP and CLI open the folder directly and ignore it. |

## More

- [docs/reference.md](docs/reference.md): all env vars, boards, profiles and claims, knowledge docs, chat, `unblockd.json` keys, ticket format, UI features, MCP tools (incl. Docker and Claude Desktop), CLI, REST API, notifications.
- [AGENTS.md](AGENTS.md): internals and design decisions for contributors.
- Tests: `python -m unittest`. MIT license.
