# unblockd

**A ticket board where humans and AI agents work the same backlog.**
Tickets are JSON files in your repo: reviewed in PRs next to the code, no SaaS, no database.

![An agent claims a ticket and completes it; the card moves to Done](docs/screenshots/agent-loop.gif)

Front doors: web, MCP, CLI, chat (experimental).
Humans get a live board and a dependency graph; agents get lease-based claims, so several work in parallel without colliding.

![unblockd board: agents working, tickets in columns, a badge for what needs you](docs/screenshots/board.webp)

**Why not Trello / Linear / GitHub Projects?** Those track work for people. A ticket here knows what blocks it, which files it touches and which docs a worker must read, and an agent claims, heartbeats and finishes it through MCP or a CLI.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/needs-dark.webp" alt="Needs you panel: a question, a stuck ticket, a result to review"><br><sub>Needs you: answer agents without leaving the board</sub></td>
<td width="50%"><img src="docs/screenshots/graph-dark.webp" alt="Dependency graph: arrows from blocker to blocked"><br><sub>Blocking graph</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/drawer.webp" alt="Ticket drawer: result, done when, blockers"><br><sub>Ticket drawer</sub></td>
<td width="50%"><img src="docs/screenshots/mobile.webp" alt="The board at phone width"><br><sub>Phone width</sub></td>
</tr>
</table>

## Try it in 5 minutes

Python 3.10+; the board and CLI are stdlib only.

**1. Run the board** with a copy of the demo project (never point it at `example/` itself):

```sh
git clone https://github.com/omri2003/unblockd && cd unblockd
mkdir -p /tmp/demo/shop && cp -r example/tickets /tmp/demo/shop/
UNBLOCKD_ROOT=/tmp/demo python -m unblockd.web
```

Expected: `unblockd on http://localhost:8787  boards: example (...)`

**2. Open the board** at <http://localhost:8787>: five tickets, `g` switches to the graph, `d` to the docs agents read, `?` lists every shortcut.

**3. Create a ticket the backend agent can take** (pillar `API`, owned by Claude; the demo's other open tickets are human-owned, blocked or claimed). Press `n`, or from another terminal:

```sh
python -m unblockd.cli --dir /tmp/demo/shop/tickets create --prefix API --title "My first ticket" --json '{"pillar":"API","owner":"Claude"}'
```

Expected: JSON with `"id": "API-3"`; the card appears within 4 s.

**4. Connect Claude Code.** One shared server for all your projects: `docker compose up -d` (pulls the image; `PROJECTS_DIR` in `.env`, default `./example`) serves the board and MCP at `http://localhost:8788/mcp`. Board only, no clone (PowerShell: `${PWD}`):

```sh
docker run -d -p 127.0.0.1:8787:8787 -e UNBLOCKD_ALLOW_OPEN=1 -v "$PWD:/projects" ghcr.io/omri2003/unblockd
```

Then:

```sh
claude mcp add --transport http --scope user unblockd http://localhost:8788/mcp
```

With `UNBLOCKD_TOKEN` set add `--header "Authorization: Bearer <token>"`. Each repo's CLAUDE.md names its board (snippet in docs/reference.md). Or one stdio process per client:

```sh
pip install mcp
claude mcp add unblockd -e UNBLOCKD_ROOT=/tmp/demo -e PYTHONPATH=$PWD -- python -m unblockd.mcp_server
claude mcp list
```

Expected: `unblockd: python -m unblockd.mcp_server - Connected`. `PYTHONPATH` must be this checkout (not pip-installed).

**5. Tell Claude:** *"use unblockd: run work_plan and work all ready tickets."* Watch cards move on the board. An empty claim returns `{ticket: null, reason}`: `dry` = nothing ready, `conflict` = ready tickets touch files a live claim holds (retry), `waiting` = work exists but is not ready.
With agent profiles (the demo has `frontend` and `backend`), worker names must be `<profile>-N`, e.g. `backend-1`; others are refused.

**In the UI:** the **Needs you** badge (`i`) collects, across all boards, agents' questions, work awaiting review and stuck tickets; answer, approve or reject inline. The title switches boards (`Shift+B`). Priorities: P0 breaks things now, P1 must happen, P2 should, P3 nice to have (`p` then `0`-`3`).

## Concepts

| Term | Meaning |
|---|---|
| **Ticket** | One JSON file in `<project>/tickets/`. Status `todo`, `doing`, `review`, `done` (`pending` is derived: a todo with an open blocker or a `waiting_on` question). |
| **Ready** | Open, nothing open blocks it, not human-owned, not waiting on a person. Agents only claim ready tickets. |
| **Blocked / pending** | Derived: a `todo` with an open `blocked_by` is pending until its blockers are done. |
| **Pillar** | A workstream (UI, API, ...): a colour, a filter and a graph lane, defined per board. |
| **Profile** | An agent type in the board config: its tags/pillars, model and instructions. `work_plan` groups ready tickets per profile. |
| **Claim, lease, heartbeat** | `claim_next` gives a worker one ticket and sets it to `doing`. With no `heartbeat` for `lease_minutes` (15) the claim expires and the ticket returns to todo. |
| **done_when** | Short acceptance criteria, shown first in the worker's context. |
| **review** | Optional gate (`"review": true`): an agent's `complete` lands in `review`; a human moves it to done. |
| **ask_human / Parked** | A decision only a human can make: the claim ends and the ticket waits in Needs you until you answer. A `waiting_on` you type yourself is *Parked*: never listed there. |

## Configuring agents

Put your project rules (tests, commits, when to ask a human) in a doc such as `AGENT_POLICY.md` and list it in `docs` in `unblockd.json`; every worker reads it with its ticket. The protocol itself (`WORK_LOOP.md`: claim, heartbeat, complete / release / ask) is built in; your docs cannot change it. See `example/AGENT_POLICY.md`.

## Config and auth, briefly

| Env var | Meaning |
|---|---|
| `UNBLOCKD_ROOT` | Folder(s) of projects; every `<project>/tickets/unblockd.json` is a board. |
| `HOST`, `PORT` | Bind address (default `127.0.0.1`, `8787`). |
| `UNBLOCKD_TOKEN` | Optional shared token. Set: `/api/*` needs `Authorization: Bearer <token>`, or open `/?token=<token>` once to set a cookie. Unset: no auth, and a non-loopback `HOST` refuses to start unless `UNBLOCKD_ALLOW_OPEN=1` (compose sets it; its ports bind 127.0.0.1 only). Writes must be `application/json`; a foreign `Origin` is refused. The HTTP MCP server (`MCP_PORT`, default `8788`) needs the same Bearer token; stdio MCP and CLI ignore it. |

## More

- [docs/reference.md](docs/reference.md): all env vars, boards, profiles and claims, knowledge docs, chat, `unblockd.json` keys, ticket format, UI features, MCP tools (incl. Docker and Claude Desktop), deploying the image (non-root / OpenShift, `/healthz`, one replica), CLI, REST API, notifications, GitHub PR sync (experimental).
- [AGENTS.md](AGENTS.md): internals and design decisions for contributors.
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) (report privately), [CHANGELOG.md](CHANGELOG.md).
- Tests: `python -m unittest`. MIT.
