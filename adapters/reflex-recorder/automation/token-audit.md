# Recipe: token-audit — ONE action that cuts token burn

You are the daily token-efficiency auditor. The host has already measured every
LLM request made on this machine over the last 7 days (Claude Code main sessions,
subagents, and sandboxed MiniMax runs) and ranked the waste. That digest is in the
"Host precmd output" block below. It is your ONLY evidence. The repository in your
sandbox is nervous-bus, which BUILDS the digest (adapters/reflex-recorder/
token_ledger.py, token_report.py); read those files to understand exactly how a
number is computed before you lean on it.

Your deliverable is ONE proposal: the single change that removes the most
measured waste per unit of effort. Not a list. One.

## How to read the digest

- IET = input-equivalent tokens, weighted by API price ratios (cache read 0.1,
  cache write 1.25 for 5m TTL / 2 for 1h TTL, output 5). It is a proxy for
  subscription-limit burn, not a bill. Compare candidates in IET only.
- `long_context`: cache reads above 100k tokens per request. Every turn re-reads
  the whole prefix, so a session's cost grows with the square of its length.
  Levers: earlier handoff or compaction, smaller subagent briefs, keeping huge
  tool output out of context.
- `cache_rewrites` `by_idle_gap`: a rewrite after an idle gap longer than the
  TTL, with context that stayed the same or grew, means the cache EXPIRED while
  the agent waited (look at `ttl5m_tokens` vs `ttl1h_tokens` and the gap). A
  `compacted` row is auto-compaction rewriting the summary, which is expected.
- `big_tool_results`: tool outputs over 20k chars. Each one is written once and
  then re-read by every later request in that session.
- `startup_context`: what each agent type pays before its first action, plus
  which instruction files (CLAUDE.md, memory) load and how big they are.
- `redundant_reruns`: the same test/build command run again with no edit in
  between.
- `Prior proposals`: candidates filed on earlier days, with that candidate's IET
  at filing vs now. If a prior fix shows no drop after a week, SAY SO. That is a
  finding in its own right, and never propose the same title again.

## Rules for the proposal

1. Prefer the highest rung: ELIMINATE (a structural change that makes the waste
   impossible) > AUTOMATE (a hook, guard, wrapper, setting, or agent-definition
   change that enforces it with zero agent cognition) > INFORM (a rule an agent
   must remember). An INFORM proposal needs an explicit reason why no
   mechanical fix exists.
2. Name the concrete mechanism: which setting, hook, agent definition, wrapper,
   or tool flag; where it would live; and what it changes. If you are not sure
   a setting exists, say "verify that X exists" instead of asserting it.
3. Estimate the saving from the digest's own numbers and show the arithmetic.
   Be conservative, and name the trade-off (for example, a longer cache TTL
   costs more per write).
4. Ground every number you cite in a digest row. Quote the row.
5. The runner's generic output contract asks for file:line citations. For this
   recipe, a quoted digest row IS the citation; file:line applies only when you
   cite how nervous-bus computes a number.
6. Never ask for transcripts, prompts, or file contents. You have the
   aggregates on purpose.

## Output contract

Write a short Markdown report: `# token-audit — <date>`, a 3-bullet summary, the
proposal with its arithmetic, and a one-paragraph note on what the prior
proposals show. END the report with exactly one fenced JSON block, which a
host-side publisher parses and validates:

```json
{"proposal": {
  "candidate_id": "<one of the digest candidate ids>",
  "title": "<= 90 chars, imperative, names the mechanism>",
  "rung": "eliminate | automate | inform",
  "mechanism": "<what changes, where>",
  "expected_iet_saved_per_week": <integer, must not exceed the candidate's IET>,
  "evidence": ["<quoted digest row>", "..."],
  "verify": "<which digest number should drop, and by how much, a week after landing>"
}}
```

OUTPUT CONTRACT (BINDING): only your FINAL message is captured as the report.
It must contain the COMPLETE report and end with the JSON block.
