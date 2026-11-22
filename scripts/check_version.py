"""CI guard (CORE-84): a pushed v* tag must equal unblockd.__version__, so the image tag, /healthz and the release agree.

python scripts/check_version.py v0.2.0   (CI passes $GITHUB_REF_NAME)   exit 0 = match, 1 = mismatch (message says what to fix)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source_version(root: Path = ROOT) -> str:
	"""__version__ read from the file, not imported (no package install needed in CI)."""
	m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', (root / "unblockd" / "__init__.py").read_text(encoding="utf-8"), re.M)
	if not m:
		raise SystemExit("unblockd/__init__.py has no __version__ = \"X.Y.Z\" line")
	return m.group(1)


def check(tag: str, version: str) -> str | None:
	"""None when the tag names this version, else the error to print."""
	if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
		return f"tag {tag!r} is not vX.Y.Z: delete it (git push origin :refs/tags/{tag}) and tag v{version}"
	if tag[1:] != version:
		return (f"tag {tag} but unblockd/__init__.py says __version__ = \"{version}\": bump __version__ in a release PR first "
		        f"(docs/releasing.md), or delete the tag (git push origin :refs/tags/{tag}) and tag v{version}")
	return None


def main(argv: list[str] | None = None) -> int:
	argv = sys.argv[1:] if argv is None else argv
	if len(argv) != 1:
		print("usage: python scripts/check_version.py vX.Y.Z", file=sys.stderr)
		return 2
	version = source_version()
	err = check(argv[0], version)
	print(err or f"tag {argv[0]} = __version__ {version}", file=sys.stderr if err else sys.stdout)
	return 1 if err else 0


if __name__ == "__main__":
	sys.exit(main())
