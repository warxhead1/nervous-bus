# plain-prose

Keeps an agent's prose short enough for a person to read once: one idea per sentence, plain words, no AI tells.

## Why it exists

Agent prose reads as heavy: long sentences that pack cause, condition and fix together, semicolons and dashes used as glue, stock words (`delve`, `seamless`, `serves as`), and a closing line that repeats the point. A reader pays for every one of them. The method comes from the open-source [ste80-humanizer](https://github.com/Dodaios/ste80-humanizer) skill. The gap this plugin closes is that a skill is rarely loaded when it applies, and it never sees a commit message or a pull request body, which cannot be rewritten after a push.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `ste80-humanizer` | loaded when prose is written or tightened | the upstream method, unmodified | |
| `hooks/ruling.md` | SessionStart | prints the merged ruling into the session | remove the plugin |
| `prose-guard` | PostToolUse on Write, Edit, MultiEdit | reports (exit 2) heavy prose the edit adds to a `.md`, `.mdx`, `.txt` or `.rst` file. The write stands. A file is reported at most twice a session | `PLAIN_PROSE_DISABLE=1` |
| `prose-guard` | PreToolUse on Bash | scores a `git commit` message and the body of a `gh pr` or `gh issue` command before it runs, and blocks (exit 2) a heavy one once. The next such command runs unchecked, so the same text or a rewrite always goes through | same switch |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt asks for writing | `STEERING_CUES_DISABLE=1` |

A Stop hook (`hooks/reply-score`) scores the agent's final reply and prints nothing. Every check also appends counts, never text, to a local hit log at `$AGENT_STEERING_HITS` (default `~/.local/state/agent-steering/hits.jsonl`; off: `AGENT_STEERING_LOG=0`). An edit that rewrites prose is checked for lost numbers, code, flags, URLs, quotes and hedges with upstream's `--compare`.

## What this adds to upstream

The upstream skill is copied unmodified (commit pinned in `skills/ste80-humanizer/UPSTREAM.md`). Everything below lives outside it, so an upstream update does not conflict.

**Four rules the upstream skill does not give.** Each is in `hooks/ruling.md` and `hooks/cues.json`:

| added rule | what upstream leaves open |
|---|---|
| Pair inanimate nouns only with literal verbs, adjectives and adverbs (never `knows`, `tells`, `reads as`, `silently`, `sane`, `forgiving`) | It flags `serves as` and `boasts` but permits these |
| State a specific cause or action and its result in every opening sentence, heading and list item, with one **bold key phrase** inside a full sentence | It removes `**Label:**` prefixes but gives no positive list pattern |
| Order points by impact without announcing the order, and stop on the last point | It removes chatbot closers but ignores ordering announcements |
| Apply the ruling to chat replies and explanations, not only documents | The hook checks edited Markdown, commits and PR bodies only, so replies stay on the writer |

The ruling also settles two clashes: passive voice is allowed where it avoids an awkward relative clause, and the terse imperative voice is for briefs only.

**Thresholds in `prose-guard`.** The upstream checker has no pass or fail line. The guard keeps its own, and counts only text the edit adds:

- A tell reports on one sighting: a not-X-but-Y frame, chatbot residue, a run-up, a one-line closer, a strawman, a stacked hedge, vague authority, inflation, or a stock word from a short list.
- A sentence over 30 words counts 1, over 45 counts 2, a clause-joining semicolon counts 1, a clause-joining dash counts a half.
- A file reports when that load reaches 4 and is at least 1 per 100 added words, so a long document with a few long sentences stays quiet.
- Quoted text and block quotes are skipped, because they are someone else's words.

The ruling itself asks for more than the guard enforces: under 25 words on average and never over 35. The guard is a floor.

**Ten-topic evaluation.** Ten drafts across ten domains (glass cleaning, a PostgreSQL scan, idempotency keys, form validation, key rotation, an LLM judge, a rebase, a retry loop, a loan, a proxy error) were written under the merged ruling. All ten averaged under 20.5 words a sentence and passed `prose-guard` with zero warnings. First drafts failed in three repeated ways: cause, condition and fix packed into one sentence (topics 07 and 09 averaged 25.3 and 28.4 words, then 17.0 and 19.6 once split), developer slang such as `spin up` and `silent fallback`, and list items opening with gerunds, which the upstream checker flags as `ing-form` and the guard ignores. Full table and samples: [evals/10-topic-style-eval.md](evals/10-topic-style-eval.md). This is one run by one writer, a worked check and not a benchmark.

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [ruling.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/plain-prose/hooks/ruling.md).

> # Write prose a person reads once
>
> A person reads your prose one sentence at a time, usually once. This holds for
> a reply, an explanation, a document, a report, a commit message, a pull request
> and a brief.
>
> - Start with the concrete cause, answer or fix in sentence one. Order points
>   silently by practical impact and stop on the last point with no wrap-up.
> - State a specific cause or action and its concrete result in every opening
>   sentence, long-reply heading and list item. Weave one **bold key phrase** into
>   a complete sentence instead of starting items with `**Label.**` prefixes.
> - One idea per sentence. Keep sentences under 25 words on average and never
>   exceed 35 words. Keep `that`, `who` and `which`, break 3+ noun stacks with
>   prepositions or hyphens, and use no semicolons, em dashes or side-remark
>   parentheses.
> - Prefer active voice with simple tenses, and use passive voice only when it
>   reads more naturally than an awkward relative clause.
> - Pair inanimate nouns only with literal verbs, adjectives and adverbs (never
>   `knows`, `tells`, `reads as`, `survives`, `silently`, `happily`, `honest`,
>   `sane`, `polite`, `forgiving` or `stubborn`), and use plain standard words.
> - No staging: no "not X but Y", no run-up, no closing line that repeats the
>   point, no three adjectives where one fact does the work.
> - Keep every fact, number and name, and keep the certainty the source has.
>
> Method: the `ste80-humanizer` skill. A hook reports heavy prose that an edit
> adds to a document. It also reads a commit message and the body of a `gh pr`
> or `gh issue` command before the command runs, and sends a heavy one back
> once (off: `PLAIN_PROSE_DISABLE=1`). It does not see chat replies or a brief
> that you pass to an agent, so those stay on you.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/plain-prose/skills/ste80-humanizer/SKILL.md) with 9 more file(s) listed in [catalog.json](../catalog.json).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`prose-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/plain-prose/hooks/prose-guard) | One JSON object on stdin, as Claude Code sends to a hook. `session_id`, `hook_event_name`, `tool_name` (Write, Edit, MultiEdit) and `tool_input` with `file_path` and `content` or `old_string`/`new_string`, or `tool_name` (Bash) and `tool_input.command` for a `git commit` or `gh pr`/`gh issue` command. Exit 0 allows. Exit 2 means the text is heavy (a document edit is reported and stands; a commit or PR command is sent back once), with the message on stderr. Bad input or a kill switch always exits 0. Keep with it: `ste_check.py`, `steering_hits.py`, `prose_score.py`. | report and block-once | PostToolUse; PreToolUse on Write\|Edit\|MultiEdit; Bash | `PLAIN_PROSE_DISABLE=1` |
| [`ste_check.py`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/plain-prose/skills/ste80-humanizer/scripts/ste_check.py) | `ste_check.py [--strict] [--json] FILE...`, or `-` for stdin. Prints findings with line numbers; with `--json`, a JSON list. | cli | run by hand |  |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/plain-prose/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/plain-prose.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/plain-prose.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/plain-prose.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/plain-prose.json), [omp/post](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/plain-prose.post.ts), [omp/pre](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/plain-prose.pre.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install plain-prose@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/plain-prose`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export plain-prose ./out`, then `claude plugin marketplace add ./out` and `claude plugin install plain-prose@plain-prose-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/plain-prose/skills/ste80-humanizer -a claude-code`. This is the upstream skill alone, with none of the added rules, hooks or thresholds. For that, install it from upstream directly.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin). License: MIT, with the upstream notice in `skills/ste80-humanizer/LICENSE`.
