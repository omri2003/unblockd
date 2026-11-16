---
paths: unblockd/store.py
---
# Store
The rules: ticket CRUD, validation (`FIELDS`), config (`load_config` / `config_from`, `DEFAULT_CONFIG` is the schema), derived fields (`_decorate`), routing, claims and the activity log. It stores nothing itself: every read and write goes through its connector (`self.backend`).

## Connections
- [MongoDB](mongo.md) — the same methods as Files; on a standalone server a failure under `_locked` keeps the writes made so far; Store's order (claims saved, then the ticket, then the event) leaves only states that heal or show: a claim on a todo ticket expires after the lease, a doing ticket without a claim is reported as `orphaned`; keep any new multi-write flow to that rule
- [SQL](sql.md) — the same methods as Files; Store must not assume a folder (`store.dir` / `store.rt` are None, `knowledge.root()` is None) and every read-modify-write must sit inside `_locked`, which is the transaction
- [Files](files.md) — the default connector: Store calls only its public methods (tickets, version, read, write, delete, numbers, lock, claims, events, attachments, docs) and holds `lock()` for every write; `_parse` upgrades old tickets for every connector, connectors never apply rules
- [knowledge](knowledge.md) — knowledge reads `store.config`, `store.dir`, `store.backend`, `get`, `list` and the private `_valid`, `_log`, `_overlap`; change their names or signatures and knowledge breaks. `delete` removes the ticket's attachments (through the connector), which knowledge writes
- [notify](notify.md) — `_log` appends one event per call through the connector (files: `activity.jsonl`, rotated to `activity.1.jsonl` at `ACTIVITY_MAX`); notify reads new ones with `store.backend.events_after(cursor)`, so never rewrite or reorder existing events
