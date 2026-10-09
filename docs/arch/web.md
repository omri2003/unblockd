---
paths: unblockd/web.py
---
# Web server
Stdlib HTTP server and the REST API (`Handler._route`). Per-board routes live under `/api/b/<board>/`, the same paths without the prefix use the first board. Optional shared-token auth, ETag + 304 on every GET.

## Connections
- [boards](boards.md) — every route gets its Store through `Boards`, never by opening a folder; the route table in the module docstring is kept in sync
- [knowledge](knowledge.md) — `knowledge`, `architecture`, `doc` and `tickets/<id>/context` call `kn.*` read-only and return its result unchanged; `architecture/components` (POST, PATCH, DELETE) and `architecture/edges` (PUT, DELETE) call the `kn` write ops (`by` defaults to `human`) and answer with the new architecture payload, so the UI repaints from the answer
- [chat](chat.md) — `POST /api/chat` calls `chat.ask`; chat errors come back as JSON with the fix-it message
- [notify](notify.md) — `main` starts the notifier thread only when `UNBLOCKD_WEBHOOK` is set; notify gets the Boards object and must not need the HTTP server
- [ui](ui.md) — serves `static/index.html` at `/`; JSON field names in responses are the UI's contract, and a new Store operation needs a route here AND a `Remote` method in the CLI
