"""unblockd: a dependency-aware ticket board for humans and AI agents."""

# The one place the version lives (CORE-84): pyproject reads it (dynamic version), /healthz, GET meta, MCP board_info /
# server info and `unblockd --version` show it, and CI refuses a v* tag that differs (scripts/check_version.py).
__version__ = "0.2.0"
