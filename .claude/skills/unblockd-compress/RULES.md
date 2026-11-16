Rewrite DOCUMENT shorter for an AI agent to read. Same meaning, fewer words.

The style guide is docs/writing-for-agents.md (what never to cut, what to cut, model-native terms, shape); compress.py sends it above these lines. Do not keep a second copy of it here.

What check.py enforces mechanically (a rewrite that breaks one is refused):
- every heading line, every code block, every `inline code`, every URL
- every file path, command, ticket id, number and unit
- every negation and condition: not, no, never, without, only, unless, except, until, if, when, before, after, must, always, should, required
- the reason after a rule when it is given ("because ...", ": it breaks ..."): shorten it, never drop it
- who decided what, and anything a person wrote as a decision or warning

Never merge two rules into one sentence if a condition would fall out. Never add a rule, example or caveat that is not in DOCUMENT.
No outer code fence around the result.
