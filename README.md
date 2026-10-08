# unblockd

![unblockd board: agents working, a digest of what changed, tickets in four columns](docs/screenshots/board.png)

**A ticket board where humans and AI agents work the same backlog.**
Tickets are JSON files in your repo: reviewed in PRs next to the code, no SaaS, no database.
Front doors: web, MCP, CLI, chat; all go through `store.py`.
Humans get a live board and a dependency graph; agents get lease-based claims, so several of them work in parallel without stepping on each other.

**Why not Trello / Linear / GitHub Projects?** Those track work for people. Here a ticket knows what blocks it, which files it touches and which docs a worker must read, and an agent can claim it, heartbeat and finish it through MCP or a CLI, with no server round-trip to a third party.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/graph-dark.webp" alt="Dependency graph in dark mode: tickets as nodes, arrows from blocker to blocked"><br><sub>Blocking graph</sub></td>
<td width="50%"><img src="docs/screenshots/drawer.webp" alt="Ticket drawer for a P0 ticket: agent, details, blockers, what it blocks"><br><sub>Ticket drawer</sub></td>
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

*Docker instead (not run here, no daemon available):* `docker compose up -d --build` serves `./example` on the same port; set `PROJECTS_DIR=/path/to/projects` in `.env` for your own.

**2. Open the board** at <http://localhost:8787>: five tickets in columns, `g` switches to the graph.

**3. Create a ticket the backend agent can take** (pillar `API`, owned by Claude; the demo's other open tickets are human-owned, blocked or already claimed). Press `n` in the board, or from another terminal:

```sh
python -m unblockd.cli --dir /tmp/demo/shop/tickets create --prefix API --title "My first ticket" --json '{"pillar":"API","owner":"Claude"}'
```

Expected: JSON with `"id": "API-3"`, `"status": "todo"`; the card appears within 4 s, and `cli plan` lists it under `backend`.

**4. Connect Claude Code, no Docker:**

```sh
pip install mcp
claude mcp add unblockd -e UNBLOCKD_ROOT=/tmp/demo -e PYTHONPATH=$PWD -- python -m unblockd.mcp_server
claude mcp list
```

Expected: `unblockd: python -m unblockd.mcp_server - Connected`. `PYTHONPATH` must be this checkout, because the package is not pip-installed. Use an absolute `python` path if Claude Code's `python` differs from the one you ran `pip` with.

**5. Tell Claude:** *"use unblockd: run work_plan and work all ready tickets."* It plans, claims, heartbeats and completes tickets; watch cards move on the board. An empty claim returns `{ticket: null, reason}`: `dry` = nothing ready, `conflict` = ready tickets touch files a live claim holds (retry soon), `waiting` = work exists but is in flight, blocked, in review or waiting on a human.
If the board defines agent profiles (the demo has `frontend` and `backend`), worker names must be `<profile>-N`, e.g. `backend-1`; anything else is refused with a message listing the profiles.

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
| **waiting_on / ask_human** | A decision only a human can make: the worker asks, the claim ends, the ticket waits until a human answers. |

## Config and auth, briefly

| Env var | Meaning |
|---|---|
| `UNBLOCKD_ROOT` | Folder(s) of projects; every `<project>/tickets/unblockd.json` is a board. |
| `HOST`, `PORT` | Web server bind address (default `127.0.0.1`, `8787`). A non-loopback `HOST` without a token prints a warning. |
| `UNBLOCKD_TOKEN` | Optional shared token. Set: `/api/*` needs `Authorization: Bearer <token>`, or open `/?token=<token>` once to set a cookie. Unset: no auth, so keep it on localhost. MCP and CLI open the folder directly and ignore it. |

## More

- [docs/reference.md](docs/reference.md): all env vars, boards, profiles and claims, knowledge docs, chat, `unblockd.json` keys, ticket format, UI features, MCP tools (incl. Docker and Claude Desktop), CLI, REST API, notifications.
- [AGENTS.md](AGENTS.md): internals and design decisions for contributors.
- Tests: `python -m unittest`. MIT license.
