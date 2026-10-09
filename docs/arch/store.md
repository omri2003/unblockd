---
paths: unblockd/store.py
---
# Store
The core: ticket CRUD over one folder (one JSON file per ticket), validation (`FIELDS`), config (`load_config`, `DEFAULT_CONFIG` is the schema), derived fields (`_decorate`), routing, claims and the activity log. The only code that touches ticket files.

## Connections
- [knowledge](knowledge.md) — knowledge reads `store.config`, `store.dir`, `get`, `list` and the private `_path`, `_log`, `_overlap`; change their names or signatures and knowledge breaks. `delete` removes `tickets/files/<ID>/`, which knowledge owns
- [notify](notify.md) — `_log` appends JSON lines to `tickets/.unblockd/activity.jsonl` and rotates it to `activity.1.jsonl` at 5 MB; notify reads new bytes by offset, so never rewrite or reorder existing lines
