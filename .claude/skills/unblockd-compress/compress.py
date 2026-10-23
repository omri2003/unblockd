"""Compress one doc with the claude CLI, then run the strict checker; one fix round if it fails. Never overwrites the original.

python compress.py SOURCE OUT [--facts FACTS]

Writes OUT only when the checker passes; prints the checker's report either way. The text goes to the claude CLI
(your own Claude Code login), nowhere else.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check import check, main as report  # noqa: E402

RULES = (Path(__file__).parent / "RULES.md").read_text(encoding="utf-8")


def ask(prompt: str) -> str:
	r = subprocess.run(["claude", "-p", "--setting-sources", "", "--strict-mcp-config"], input=prompt, capture_output=True,
	                   text=True, encoding="utf-8", timeout=600, check=True)
	out = r.stdout.strip()
	if out.startswith("```") and out.endswith("```"):  # a model that wrapped the whole file in a fence
		out = out.split("\n", 1)[1].rsplit("```", 1)[0].strip()
	return out + "\n"


def main(argv: list[str]) -> int:
	src, out = Path(argv[0]), Path(argv[1])
	facts = Path(argv[argv.index("--facts") + 1]).read_text(encoding="utf-8").splitlines() if "--facts" in argv else []
	original = src.read_text(encoding="utf-8")
	comp = ask(f"{RULES}\n\nReturn ONLY the rewritten document.\n\nDOCUMENT:\n{original}")
	errors, _ = check(original, comp, facts)
	if errors:  # one targeted fix: restore only what the checker named
		comp = ask(f"{RULES}\n\nA rewrite of DOCUMENT broke these rules:\n- " + "\n- ".join(errors)
		           + f"\n\nFix ONLY those, restoring the exact words from DOCUMENT. Return ONLY the fixed rewrite.\n\nDOCUMENT:\n{original}\n\nREWRITE:\n{comp}")
	tmp = out.with_suffix(out.suffix + ".candidate")
	tmp.write_text(comp, encoding="utf-8")
	code = report([str(src), str(tmp)] + (["--facts", argv[argv.index("--facts") + 1]] if facts else []))
	if code == 0:
		tmp.replace(out)
	return code


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
