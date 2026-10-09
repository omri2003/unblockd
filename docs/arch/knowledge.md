---
paths: unblockd/knowledge.py
---
# Knowledge
Docs linked to tickets (board, profile, ticket `docs`), files attached to tickets, the knowledge graph, the architecture notes, and `context()`: the bundle a worker reads before it starts.

## Connections
- [store](store.md) — project root is the tickets folder's parent and `_inside()` refuses anything outside it; it never writes ticket JSON, only `tickets/files/<ID>/` and the architecture notes (the writes run under `store._locked()` and log one `arch` event with `_log`); missing or bad doc entries come back as an `error` item, never an exception
- [mcp-server](mcp-server.md) — `claim_next` / `claim_ticket` return `{...ticket, context}` via `with_context`; the bundle keys (`docs`, `upstream`, `architecture`, `protocol`) are what `WORK_LOOP.md` tells workers to read
