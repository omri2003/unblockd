---
paths: unblockd/cli.py
---
# CLI
The same operations for agents without MCP. With `UNBLOCKD_URL` set, `Remote` replaces the local `Store` and sends each call to the REST API.

## Connections
- [store](store.md) — direct mode calls Store methods with the same names and arguments as MCP and REST; `Remote` must offer the same method names, so a new Store operation gets a `Remote` method or remote workers lose it
- [web](web.md) — `Remote` is one urllib call per REST route; a server error body comes back as `TicketError` (exit 1) with the message unchanged
- [knowledge](knowledge.md) — `context`, `doc` and `attach` call `kn.*` locally or the matching REST route remotely
