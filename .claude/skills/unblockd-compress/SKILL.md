---
name: unblockd-compress
description: Use when asked to shorten, compress or make terse a doc that AI agents read (WORK_LOOP.md, AGENTS.md, board or profile docs, a long ticket body) to save tokens without losing a rule, number, path or human decision. Proposes a rewrite plus a strict check; never overwrites without approval.
---

# unblockd-compress

Shorter agent docs, same meaning. Built on the idea of caveman-compress (juliusbrussee/caveman, Apache-2.0), plus a stricter checker: every rule word (negations, conditions, obligations), number, path, inline code, URL, heading and code block must survive, and optional required facts.

## When
- A doc agents read on every claim or session: biggest saving per word cut.
- Not: secrets, legal text, a file where every sentence is a hard rule you have not reviewed.

## Steps
1. Optional: write the facts that must survive, one phrase per line, to `<scratch>/facts.txt` (decisions, warnings, limits a person stated).
2. Rewrite. Either:
   - headless: `python .claude/skills/unblockd-compress/compress.py SOURCE <scratch>/OUT.md [--facts facts.txt]` (uses your `claude` CLI login, one automatic fix round), or
   - yourself: follow `RULES.md` exactly, write the result to `<scratch>/OUT.md`.
3. Check: `python .claude/skills/unblockd-compress/check.py SOURCE <scratch>/OUT.md [--facts facts.txt]`. FAIL = keep the original or fix only what it names. Read every warning.
4. Show the person: the token saving, the warnings, and a diff (`git diff --no-index SOURCE OUT.md`). Replace SOURCE only after they say yes.

## Rules
- Never overwrite SOURCE yourself before approval; never compress a file the person did not name.
- The checker catches dropped words, not changed meaning: read the diff for swapped conditions ("only A" -> "only B").
- Text goes to Claude only (the CLI you are already logged into); nothing else leaves the machine.
