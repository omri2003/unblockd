# Contributing

[AGENTS.md](AGENTS.md) is the real guide: architecture, conventions, traps. Read it first.

- Python stdlib only (the MCP server's `mcp` package is the one exception). The UI is one file, no build step, no CDN libraries.
- Before a PR: `python -m unittest`, then `NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs` for UI changes. Say in the PR what you did not verify.
- Docs move with the code, in the same commit.
- Work is tracked on the board in `tickets/` (one JSON file per ticket). Agents claim and complete tickets through the CLI or MCP rather than editing the files by hand.
