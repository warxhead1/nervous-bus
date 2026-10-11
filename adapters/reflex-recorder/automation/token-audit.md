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
  Read the TTL from the row, never assume it: a row whose writes are all
  `ttl5m_tokens` is on the 5-minute TTL. Before proposing a longer TTL, do the
  trade-off: a 1h write costs 2.0 vs 1.25, and it applies to EVERY write that
  agent kind makes, not only to the rewrites. `all_cache_writes` gives that
  write base per agent kind: net = rewrites*(1.25-0.1) - (all writes -
  rewrites)*0.75. Never estimate a number the digest already measures.
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
   mechanical fix exists. Before settling for INFORM, check these surfaces for
   a lever (mark any lever not listed below "verify exists"): PreToolUse/
   PostToolUse hooks (a hook can truncate or redirect large tool output, or
   block a known-wasteful command), wrappers around build/test commands, and
   these Claude Code levers, verified in the installed 2.1.296 binary on
   2026-10-11:
   - Agent definition frontmatter (`~/.claude/agents/<type>.md`):
     `experimental: {cacheTtl: 1h}` (per-type TTL; ignored during
     subscription overage), `omitClaudeMd: true` (no CLAUDE.md files when
     spawned as a subagent), `autoCompactWindow: <tokens>` (per-agent
     compaction threshold), `maxTurns`, `model`, `tools`.
   - settings.json `subagentPromptCacheTtl` / env
     `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` ("5m"|"1h"): EVERY subagent type,
     and also during overage. Prefer the per-type frontmatter key.
   - settings.json `promptCacheTtl` / env `CLAUDE_CODE_PROMPT_CACHE_TTL`: the
     main conversation (already 1h on a subscription).
   Already applied (do not re-propose): `sonnet-worker` has `cacheTtl: 1h`
   since 2026-10-11 (claude-hook-fast#38).
   For a TTL proposal, use `ttl_by_agent_type`: it nets the trade-off per
   agent type (`net_if_1h_iet`). Propose 1h only for types where it is
   positive.
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
