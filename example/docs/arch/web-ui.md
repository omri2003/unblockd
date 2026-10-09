---
paths: web/**
---
# Web UI
The single-page board: four status columns, a dependency graph view and a ticket drawer. Plain HTML and JS, no build step. It keeps no state except filters; the server is the source of truth.

## Connections
- [api](api.md) — polls `GET /tickets` every 4 s with `If-None-Match` and re-renders only when the JSON changed; every edit is one `PATCH /tickets/<id>`, never a bulk rewrite; shows the server's error message as is
