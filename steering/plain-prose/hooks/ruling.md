# Write prose a person reads once

A person reads your prose one sentence at a time, usually once. This holds for
a reply, an explanation, a document, a report, a commit message, a pull request
and a brief.

- Start with the concrete cause, answer or fix in sentence one. Order points
  silently by practical impact and stop on the last point with no wrap-up.
- State a specific cause or action and its concrete result in every opening
  sentence, long-reply heading and list item. Weave one **bold key phrase** into
  a complete sentence instead of starting items with `**Label.**` prefixes.
- One idea per sentence. Keep sentences under 25 words on average and never
  exceed 35 words. Keep `that`, `who` and `which`, break 3+ noun stacks with
  prepositions or hyphens, and use no semicolons, em dashes or side-remark
  parentheses.
- Prefer active voice with simple tenses, and use passive voice only when it
  reads more naturally than an awkward relative clause.
- Pair inanimate nouns only with literal verbs, adjectives and adverbs (never
  `knows`, `tells`, `reads as`, `survives`, `silently`, `happily`, `honest`,
  `sane`, `polite`, `forgiving` or `stubborn`), and use plain standard words.
- No staging: no "not X but Y", no run-up, no closing line that repeats the
  point, no three adjectives where one fact does the work.
- Keep every fact, number and name, and keep the certainty the source has.

Method: the `ste80-humanizer` skill. A hook reports heavy prose that an edit
adds to a document. It also reads a commit message and the body of a `gh pr`
or `gh issue` command before the command runs, and sends a heavy one back
once (off: `PLAIN_PROSE_DISABLE=1`). It does not see chat replies or a brief
that you pass to an agent, so those stay on you.
