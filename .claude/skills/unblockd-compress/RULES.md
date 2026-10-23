Rewrite DOCUMENT shorter for an AI agent to read. Same meaning, fewer words.

Cut:
- filler, hedging, politeness, repetition ("it is worth noting that", "basically", "in order to" -> "to")
- articles where the sentence still reads clearly
- a sentence that only restates the one before

Prefer:
- lists and `key: value` lines over paragraphs; one fact per line
- "e.g." for examples; short imperative rules ("Never X.", "Only when Y.")

Keep exactly (a checker refuses the rewrite otherwise):
- every heading line, every code block, every `inline code`, every URL
- every file path, command, ticket id, number and unit
- every negation and condition: not, no, never, without, only, unless, except, until, if, when, before, after, must, always, should, required
- the reason after a rule when it is given ("because ...", ": it breaks ..."): shorten it, never drop it
- who decided what, and anything a person wrote as a decision or warning

Never merge two rules into one sentence if a condition would fall out. Never add a rule, example or caveat that is not in DOCUMENT.
No outer code fence around the result.
