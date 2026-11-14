# Write prose a person reads once

A person reads your prose one sentence at a time, usually once. This holds for
a document, a report, a commit message, a pull request and a brief.

- One idea per sentence. About 25 words at most. Start with the answer.
- Active voice, and name who acts. Simple tenses.
- Plain words, and one term per thing. No semicolons. No dash that joins two
  clauses: use a period.
- No staging: no "not X but Y", no run-up, no closing line that repeats the
  point, no three adjectives where one fact does the work.
- Keep every fact, number and name, and keep the certainty the source has.

Method: the `ste80-humanizer` skill. A hook reports heavy prose that an edit
adds to a document. It also reads a commit message and the body of a `gh pr`
or `gh issue` command before the command runs, and sends a heavy one back
once (off: `PLAIN_PROSE_DISABLE=1`). It does not see a brief you pass to an
agent, so that is on you.
