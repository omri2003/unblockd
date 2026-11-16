# Writing for agents

Docs agents load every session (AGENTS.md, WORK_LOOP.md, board / profile docs, long ticket bodies) cost tokens on every run. Write them dense; never thin. DOCS-17; first applied to AGENTS.md.

## Never cut

- Rules and their conditions: not, no, never, only, unless, until, if, when, before, after, must, always, required. "Only A" must not become "A".
- Numbers + units, limits, defaults (`SUMMARY_MAX` (10000), `LEASE_WARN_S` = 180, 4 s poll).
- Paths, commands, env vars, function / class / key / CSS-class names, ticket ids, URLs. Each must survive at least once.
- Owner decisions with date and ticket ("owner decision 2026-10-24, UI-52: compact wins").
- The why that stops re-litigating a decision: "Why files, not a database", "Why only 1 hop", "Why not last N".
- Every item under Traps; test expectations; verification steps; conventions (tabs, stdlib only, commit author).
- User-visible strings in quotes: UI copy is matched by tests and people.

## Cut

- Repetition: one fact in one place; elsewhere a pointer ("see Claims"). E.g. the `#depSeg` / `[hidden]` trap folded into the `[hidden]` trap.
- History that no longer guides action: "used to be X, now Y" keeps only Y. Keep the old way only when someone would reintroduce it (Traps: the bare `lock.unlink()` steal).
- Stale statements contradicted elsewhere ("The drawer does not show `result` yet").
- Filler, hedging, "which is the whole point", "as before", articles where the line still reads.

## Prefer

- A model-native term over a paragraph, only when a strong model reliably knows it: idempotent, debounce, FLIP, fail-closed, invariant, memoize, fan-out, TOCTOU, steelman, premortem. Project terms (worker-shaped, principal, front door) are defined once.
- Symbols for relations: `→` leads to, `=` is, `+` and, `|` or.
- `key: value` lines, lists and tables over prose; sub-bullets for a long bullet (Claims split into lock, complete, principal, lease...).
- Front-door lists in one fixed order: MCP, CLI, REST, `Remote`.

## Shape

- Keep headings: other docs, tests and branches point at them.
- A code fence holding prose (the Files map) is still prose: one line per entry, no hard-wrap indentation (24 spaces of padding per wrapped line cost more than the words).

## Check

`python .claude/skills/unblockd-compress/check.py OLD NEW` refuses a lost heading, code block, `code`, URL, path, number or rule word. It counts occurrences: a deliberately removed duplicate or a rephrased negation is a refusal you justify in review, never one you ignore. It checks words, not meaning: read the diff for swapped conditions.

## Know the floor

Dense reference docs are near their information floor: rewriting cut AGENTS.md ~7%. Loading less cuts more: split by audience and link. The UI conventions moved verbatim to `.claude/skills/unblockd-design/UI-REFERENCE.md` (owner 2026-11-16), so AGENTS.md is ~45% smaller for non-UI agents; it keeps only the UI rules a backend change can break, plus a pointer.
