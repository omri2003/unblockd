# unblockd

A dependency-aware ticket board for humans and AI agents. **One ticket = one
JSON file** in your repo, so a ticket change is committed with the code it
describes. Three front doors over one core:

| | What | Run |
|---|---|---|
| **Board** | Kanban columns + a blocking graph, live | `python -m unblockd.web` (stdlib only) |
| **MCP server** | Tools for Claude and other MCP agents | `python -m unblockd.mcp_server` (needs `mcp`) |
| **CLI** | The same operations for agents without MCP | `python -m unblockd.cli` |

All three go through `unblockd/store.py`, so they validate and number tickets
the same way and can never disagree.

## Quick start

```sh
git clone https://github.com/omri2003/unblockd && cd unblockd
docker compose up -d --build          # http://localhost:8787, shows ./example
```

Point it at your own projects by putting `PROJECTS_DIR=/path/to/your/projects`
in a `.env` file next to `compose.yaml`. Without Docker (Python 3.10+):
`UNBLOCKD_ROOT=/path/to/your/projects python -m unblockd.web`.

## Boards

One server, many boards. A board is a `tickets/` folder with an
`unblockd.json` in it: every `<project>/tickets/unblockd.json` under
`UNBLOCKD_ROOT` is picked up (the config file is the opt-in, so unrelated
`tickets/` folders are ignored). Switch boards from the header; **New
board…** creates `<root>/<id>/tickets/unblockd.json`. The first board is the
default; the board you last opened is remembered.

| Env var | Meaning |
|---|---|
| `UNBLOCKD_ROOT` | Folder(s) of projects to scan (`:`-separated, `;` on Windows). Needed to create boards. |
| `TICKETS_DIR`, `UNBLOCKD_BOARDS` | Explicit tickets folders, config optional. |
| `UNBLOCKD_LEGACY` | Extra old-backlog files for numbering (see `legacy_files`). |

## Agent profiles: which agent works which ticket

Tickets have `tags`. Agent profiles in `unblockd.json` claim tickets **by tag
or by pillar**; an explicit `agent:<name>` tag overrides both. Each ticket
gets a derived `agent` (shown on its card, filterable in the bar).

```json
"agents": [
  {"name": "godot-dev", "pillars": ["COMBAT"], "tags": ["gdscript"], "model": "sonnet",
   "instructions": "Typed GDScript; run the tests before done."},
  {"name": "blender", "pillars": ["ART"], "instructions": "Batch-process models in Blender."}
]
```

`work_plan` (MCP, `GET .../plan`, CLI `plan`) returns the ready tickets **not
owned by a human**, grouped per profile with its `instructions` and `model`:
everything an orchestrator needs to spawn one agent per group. A single agent
asks `next_ready(agent="godot-dev")` for its own queue.

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
| `stale_days` | A `doing` ticket untouched this long is flagged. |
| `legacy_files` | Old ticket lists (relative to the tickets folder): new IDs never reuse a number seen in them. |
| `id` | Board id (default: the project folder's name). |
| `agents` | Agent profiles, see below. |

## Tickets

```json
{"id": "BUG-10", "title": "Search ignores accents", "status": "todo", "priority": "P1",
 "owner": "Claude", "pillar": "COMBAT", "pillar_step": 1, "section": "",
 "blocked_by": ["CMB-4"], "tags": ["gdscript"], "waiting_on": "", "body": "cause -> fix -> tests",
 "created": "2026-10-03", "updated": "2026-10-03"}
```

`status` is `todo` · `doing` · `pending` · `done`; a `todo` with an open
blocker *shows* as pending on its own. Create with a prefix (`BUG`) and the
next free number is picked. A `blocked_by` that would make a loop (A waits on
B waits on A) is refused with the loop spelled out.

Every ticket comes back with derived fields: `effective_status`,
`open_blockers`, `blocks`, `unblocks` (open tickets downstream, transitive),
`missing_blockers`, `stale_days`, `agent` and `score` = priority weight
(P0 8, P1 4, P2 2, P3 1) x (1 + `unblocks`).

## The board

- **Next up**: the 5 best ready tickets by score, above the columns.
- **Pillar outlines** on cards, pillar dots on the filter chips.
- **Warnings**: missing blocker, doing with no update for `stale_days`; a *Needs attention* filter.
- Drag a card between columns to change its status. Polls every 4 s, so agent edits show up live.

**Graph** (Board / Graph switch): arrow A -> B means A blocks B. Fill =
status, ring = pillar, size = how much is waiting on it.

- **Flow** layout (default): columns = chain depth, rows = pillar lanes,
  crossings reduced by barycenter sweeps. **Free**: force layout.
- Hover = direct links; click = the whole chain and the longest open chain
  to it; **Critical path** toggle; double-click opens the ticket.
- **Shift-drag A onto B** makes A block B; click a link to remove it.
- "Chains as text" under the canvas for screen readers; pinch and wheel zoom.

Keyboard: `/` search, `g` Board/Graph, `n` new, `j`/`k` move, `Enter` open,
`?` shortcuts, `Esc` close.

## Connect the MCP server

**Claude Code:**

```sh
claude mcp add unblockd --scope user -- docker run -i --rm -v /path/to/projects:/projects unblockd python -m unblockd.mcp_server
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{"mcpServers": {"unblockd": {"command": "docker",
  "args": ["run", "-i", "--rm", "-v", "/path/to/projects:/projects", "unblockd", "python", "-m", "unblockd.mcp_server"]}}}
```

Build the image once first (`docker compose build`), or use
`ghcr.io/omri2003/unblockd` in place of `unblockd`.

Tools: `list_boards`, `create_board`, `board_info` (owners, pillars, agent
profiles), `work_plan` (orchestrators start here), `next_ready` (single agents
start here; `agent=`, `tag=`), `list_tickets`, `get_ticket`, `create_ticket`,
`update_ticket`, `delete_ticket`. Every ticket tool takes `board=` (default:
the first board).

## CLI

```
python -m unblockd.cli --dir <repo>/tickets next --owner Claude --agent godot-dev
python -m unblockd.cli --dir <repo>/tickets plan --per-agent 3
python -m unblockd.cli --dir <repo>/tickets list --status todo
python -m unblockd.cli --dir <repo>/tickets get BUG-10
python -m unblockd.cli --dir <repo>/tickets create --prefix BUG --title "..." --json '{"priority":"P1"}'
python -m unblockd.cli --dir <repo>/tickets update BUG-10 --json '{"status":"done"}'
python -m unblockd.cli --dir <repo>/tickets note BUG-10 "dated finding appended to the body"
```

## API

```
GET    /api/boards                    every board with ticket counts
POST   /api/boards                    {"id": "my-game", "title": "My game"}
# per board under /api/b/<board>/ ; without the prefix = the default board
GET    meta                           statuses, priorities, fields, config
GET    tickets?status=&pillar=&owner=&priority=&tag=&agent=&q=
GET    next?owner=&pillar=&agent=&tag=&limit=
GET    plan?per_agent=
GET    tickets/<id>
POST   tickets                        {"prefix": "BUG", "title": "...", ...}
PATCH  tickets/<id>                   {"status": "done"}
DELETE tickets/<id>
```

**No auth.** It listens on 127.0.0.1; don't expose the port wider.

## Develop

- New ticket field: one line in `FIELDS` in `store.py`; show it in `static/index.html`.
- New MCP tool: a function with `@tool` in `mcp_server.py` calling `Store`.
- New API route: a branch in `Handler._route` in `web.py`.

Tests: `python -m unittest`. UI changes show on a browser reload; Python
changes after `docker compose restart`.

## License

MIT
