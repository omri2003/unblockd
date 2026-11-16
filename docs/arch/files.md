---
paths: unblockd/files.py, unblockd/errors.py
---
# Files
The default storage connector: a board as a folder of JSON files (one per ticket) plus the lock file, `claims.json`, `activity.jsonl`, attachments and small runtime docs. Its public methods are the connector API every connector implements.

## Connections
- [store](store.md) — serves the rules only through its public methods; `write` does not bump `rev` (Store does), `lock(steal)` is not re-entrant (Store makes it so), `tickets()` returns cached dicts the caller must copy; every connector must pass tests/contract.py
