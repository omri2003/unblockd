# Contributing

[AGENTS.md](AGENTS.md) is the real guide: architecture, conventions, traps. Read it first.

- Python stdlib only (the MCP server's `mcp` package is the one exception). The UI is one file, no build step, no CDN libraries.
- Before a PR: `python -m unittest`, then `NODE_PATH=$(npm root -g) node tests/ui_smoke.mjs` for UI changes. Say in the PR what you did not verify.
- Docs move with the code, in the same commit.
- Report bugs and ideas as GitHub issues. The maintainer's own ticket board is a separate private repo, so there is no `tickets/` folder here; `example/tickets/` is only the demo board.

## Versions

The version lives in one place: `__version__` in `unblockd/__init__.py`. The package (`pyproject.toml`), `GET /healthz`, `GET /api/meta`, MCP `board_info` and server info, and `unblockd --version` all read it. Change it only in a release PR.

While unblockd is 0.x:

- **Minor** (v0.2.0 -> v0.3.0): any new feature, ticket field, MCP tool, CLI flag, config key or REST route, and any breaking change.
- **Patch** (v0.2.0 -> v0.2.1): fixes only. Nothing new to learn, nothing to change on upgrade.

CI refuses a `v*` tag that differs from `__version__` (`scripts/check_version.py`): no image is built for it.

## Release checklist

The exact commands, in order: [docs/releasing.md](docs/releasing.md).

1. CI is green on `main`.
2. Release notes drafted from merged PRs and ticket results (`python scripts/release_notes.py <last tag>`), then edited by hand into the CHANGELOG.
3. A release PR bumps `__version__` and moves the CHANGELOG `Unreleased` section under the new version; merged.
4. The maintainer tags the merge commit (signed) and pushes the tag.
5. CI builds `ghcr.io/omri2003/unblockd:<version>`; the pulled image's `/healthz` shows the version.
6. GitHub Release published with the same notes (`python scripts/release_notes.py --body <version>`).
7. The board's milestone for that version is closed.
