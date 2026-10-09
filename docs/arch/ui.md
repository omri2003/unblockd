---
paths: static/index.html
---
# UI
The whole board in one file: vanilla JS, no build step, no CDN libraries. Board, graph and docs views, drawer, Needs you panel, chat sidebar. Colours are `light-dark()` CSS tokens.

## Connections
- [web](web.md) — talks only to `/api/*`: polls tickets every 4 s with `If-None-Match` and re-renders when the JSON text changed, so the ticket JSON must be stable (timestamps, never "seconds ago"); every write is a PATCH, POST, PUT or DELETE that the store validates and the UI shows the error of (the Map's architecture edits repaint from the payload the server answers with)
