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

Point it at your own project by putting `PROJECT_DIR=/path/to/your/repo` in a
`.env` file next to `compose.yaml` (the board reads `<repo>/tickets/`).
Without Docker (Python 3.10+): `TICKETS_DIR=/path/to/repo/tickets python -m unblockd.web`.

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

## Tickets

```json
{"id": "BUG-10", "title": "Search ignores accents", "status": "todo", "priority": "P1",
 "owner": "Claude", "pillar": "COMBAT", "pillar_step": 1, "section": "",
 "blocked_by": ["CMB-4"], "waiting_on": "", "body": "cause -> fix -> tests",
 "created": "2026-10-03", "updated": "2026-10-03"}
```

`status` is `todo` · `doing` · `pending` · `done`; a `todo` with an open
blocker *shows* as pending on its own. Create with a prefix (`BUG`) and the
next free number is picked. A `blocked_by` that would make a loop (A waits on
B waits on A) is refused with the loop spelled out.

Every ticket comes back with derived fields: `effective_status`,
`open_blockers`, `blocks`, `unblocks` (open tickets downstream, transitive),
`missing_blockers`, `stale_days` and `score` = priority weight (P0 8, P1 4,
P2 2, P3 1) x (1 + `unblocks`).

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
claude mcp add unblockd --scope user -- docker run -i --rm -v /path/to/repo:/data unblockd python -m unblockd.mcp_server
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{"mcpServers": {"unblockd": {"command": "docker",
  "args": ["run", "-i", "--rm", "-v", "/path/to/repo:/data", "unblockd", "python", "-m", "unblockd.mcp_server"]}}}
```

Build the image once first (`docker compose build`), or use
`ghcr.io/omri2003/unblockd` in place of `unblockd`.

Tools: `next_ready` (start here), `list_tickets`, `get_ticket`,
`create_ticket`, `update_ticket`, `delete_ticket`.

## CLI

```
python -m unblockd.cli --dir <repo>/tickets next --owner Claude
python -m unblockd.cli --dir <repo>/tickets list --status todo
python -m unblockd.cli --dir <repo>/tickets get BUG-10
python -m unblockd.cli --dir <repo>/tickets create --prefix BUG --title "..." --json '{"priority":"P1"}'
python -m unblockd.cli --dir <repo>/tickets update BUG-10 --json '{"status":"done"}'
python -m unblockd.cli --dir <repo>/tickets note BUG-10 "dated finding appended to the body"
```

## API

```
GET    /api/meta             statuses, priorities, fields, config
GET    /api/tickets?status=&pillar=&owner=&priority=&q=
GET    /api/next?owner=&pillar=&limit=
GET    /api/tickets/<id>
POST   /api/tickets          {"prefix": "BUG", "title": "...", ...}
PATCH  /api/tickets/<id>     {"status": "done"}
DELETE /api/tickets/<id>
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
