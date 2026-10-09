---
paths: unblockd/boards.py
---
# Boards
Discovers boards under `UNBLOCKD_ROOT` (`<project>/tickets/unblockd.json`, opt-in), explicit `TICKETS_DIR` / `UNBLOCKD_BOARDS` folders, ids and `create()`. Scanned on every call; the first board is the default.

## Connections
- [store](store.md) — builds one `Store` per tickets folder and hands it out; it never reads or writes ticket files itself and adds no ticket logic
