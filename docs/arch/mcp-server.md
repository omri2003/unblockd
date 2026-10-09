---
paths: unblockd/mcp_server.py, unblockd/WORK_LOOP.md
---
# MCP server
MCP tools, the `work_loop` prompt and the `unblockd://work-loop` resource (`WORK_LOOP.md`, the worker protocol, filled in with `string.Template`). Needs the `mcp` package, the repo's only dependency.

## Connections
- [boards](boards.md) — every tool takes `board=` and resolves it with `BOARDS.get(board)`; no behaviour of its own, each tool is one call into Boards -> Store
- [knowledge](knowledge.md) — `get_context`, `knowledge_graph`, `architecture_map`, `read_doc`, `attach_file` and the five `arch_*` writes (`agent` is the `by` of the logged `arch` event) pass straight through; `knowledge.PROTOCOL_FILE` is the same `WORK_LOOP.md` served here
