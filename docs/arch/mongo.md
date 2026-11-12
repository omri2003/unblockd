---
paths: unblockd/mongo.py
---
# MongoDB
The MongoDB storage connector: many boards in one database, the board lock as a lock document taken with one atomic update (server clock expiry), and one transaction per lock where the server supports it (replica set, mongos). Same methods as Files.

## Connections
- [store](store.md) — serves the rules only through the connector methods; every write takes the board lock; inside the lock on a replica set it is one transaction, on a standalone server writes apply in Store's order; must pass tests/contract.py in both modes
