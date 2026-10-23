"""Strict checker for a compressed doc: did the rewrite keep everything that carries meaning?

python check.py ORIGINAL COMPRESSED [--facts FACTS]   (exit 1 on any error)

Errors (the rewrite is refused): a heading, code block, inline `code`, URL, path or number that is gone or changed; fewer
rule words in a family (negation, limits, obligation) than the original had; a line of FACTS (one required phrase per line)
missing. Warnings: more rule words than before (a new condition), fewer list items. Stdlib only.
"""
from __future__ import annotations

import re
import sys
from collections import Counter

FENCE = re.compile(r"^(```|~~~)[^\n]*\n.*?^\1[ \t]*$", re.M | re.S)
INLINE = re.compile(r"`[^`\n]+`")
URL = re.compile(r"https?://[^\s)`>\]]+")
HEADING = re.compile(r"^#{1,6} .*$", re.M)
PATH = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)+[\w.*-]+/?|(?<![\w/])[\w-]+\.(?:md|py|js|mjs|json|html|txt|yml|yaml|toml|sh|ps1|ts|css)\b")
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?:\s?(?:%|ms|s|min|k|KB|MB|GB|px|chars?|tokens?|days?))?(?![\w])")
ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
# a family counts every form, so "do not" -> "never" is fine but dropping one is not
FAMILIES = {
	"negation": r"\b(?:not|no|never|none|nothing|nobody|neither|nor|without|cannot)\b|n't\b",
	"limit": r"\b(?:only|unless|except|until|if|when|while|before|after|once)\b",
	"obligation": r"\b(?:must|always|required?|need(?:s|ed)?|should|refuse[sd]?|forbid(?:den)?)\b",
}


def _strip_code(text: str) -> str:
	return INLINE.sub(" ", FENCE.sub(" ", text))


def facts_of(text: str) -> dict:
	prose = _strip_code(text)
	return {
		"headings": HEADING.findall(text),
		"code blocks": [m.group(0) for m in FENCE.finditer(text)],
		"inline code": Counter(INLINE.findall(FENCE.sub(" ", text))),
		"urls": Counter(URL.findall(text)),
		"paths": Counter(PATH.findall(prose)),
		"numbers": Counter(n.replace(" ", "") for n in NUMBER.findall(prose)),
		"families": {k: len(re.findall(p, prose, re.I)) for k, p in FAMILIES.items()},
		"items": len(ITEM.findall(text)),
	}


def check(original: str, compressed: str, facts: list[str] = ()) -> tuple[list[str], list[str]]:
	a, b = facts_of(original), facts_of(compressed)
	errors, warnings = [], []
	if a["headings"] != b["headings"]:
		errors.append(f"headings changed: lost {[h for h in a['headings'] if h not in b['headings']]}, added {[h for h in b['headings'] if h not in a['headings']]}")
	if a["code blocks"] != b["code blocks"]:
		errors.append("code blocks not kept byte for byte")
	for k in ("inline code", "urls", "paths", "numbers"):
		lost = a[k] - b[k]
		if lost:
			errors.append(f"{k} lost: {', '.join(sorted(lost))}")
	for fam, n in a["families"].items():
		m = b["families"][fam]
		if m < n:
			errors.append(f"{fam} words: {n} -> {m} (a condition or rule may be gone)")
		elif m > n:
			warnings.append(f"{fam} words: {n} -> {m} (check no condition was added)")
	if b["items"] < a["items"]:
		warnings.append(f"list items: {a['items']} -> {b['items']}")
	low = compressed.lower()
	for f in facts:
		if f.strip() and f.strip().lower() not in low:
			errors.append(f"required fact missing: {f.strip()}")
	return errors, warnings


def tokens(text: str) -> int:
	return max(1, round(len(text) / 4))  # rough: ~4 chars per token for English prose


def main(argv: list[str]) -> int:
	if len(argv) < 2:
		print(__doc__)
		return 2
	read = lambda p: open(p, encoding="utf-8").read()
	orig, comp = read(argv[0]), read(argv[1])
	facts = read(argv[argv.index("--facts") + 1]).splitlines() if "--facts" in argv else []
	errors, warnings = check(orig, comp, facts)
	print(f"tokens ~{tokens(orig)} -> ~{tokens(comp)} ({100 - round(100 * tokens(comp) / tokens(orig))}% smaller)")
	for w in warnings:
		print("warning:", w)
	for e in errors:
		print("ERROR:", e)
	print("PASS" if not errors else "FAIL: keep the original")
	return 1 if errors else 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
