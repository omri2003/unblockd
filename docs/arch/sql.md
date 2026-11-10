---
paths: unblockd/sql.py, requirements-db.txt
---
# SQL
The SQL storage connector: many boards in one SQLite or Postgres database (tables prefixed ub_), with the board lock as one transaction on the board's row. Same methods as Files.

## Connections
- [store](store.md) — serves the rules only through the connector methods; `lock()` = one transaction (commit or roll back as a whole), `version()` changes on every ticket / claim / attachment write and is never reused inside a transaction; must pass tests/contract.py on SQLite and Postgres
