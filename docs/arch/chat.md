---
paths: unblockd/chat.py
---
# Chat
Messages API over urllib: resolves `#ID`, `#board/ID` and `@board`, then runs a read-only tool loop (max 8 calls). Needs `ANTHROPIC_API_KEY`.

## Connections
- [boards](boards.md) — reads tickets only through `Boards` -> `Store`; its single "write" is `propose_tickets`, which creates nothing and returns the proposals for the UI to submit
- [knowledge](knowledge.md) — `get_context` and `read_doc` tools call `kn.context` (budget 15000) and `kn.read_doc`
