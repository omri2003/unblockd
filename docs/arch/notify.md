---
paths: unblockd/notify.py
---
# Notify
Webhook notifications: one daemon thread started by the web server (`UNBLOCKD_WEBHOOK`) that batches claim, complete, question and stuck events into one message per board per tick.

## Connections
- [store](store.md) — calls `store.list()` first so expired claims are swept and reported, then reads each board's `activity.jsonl` by byte offset; a shrunk file means rotation (finish `activity.1.jsonl`, restart at 0)
