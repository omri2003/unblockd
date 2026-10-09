---
paths: api/**
---
# REST API
Thin HTTP layer over the store: `/tickets` CRUD plus `/next` (ready tickets ranked by priority). Validates nothing itself.

## Connections
- [[store]] — every route calls one store method; the API never opens ticket files; a store error becomes HTTP 400 (404 for an unknown id) with the message unchanged
- [web-ui](web-ui.md) — serves `web/index.html` at `/`; adding or renaming a JSON field here breaks the page, so change both in one commit
