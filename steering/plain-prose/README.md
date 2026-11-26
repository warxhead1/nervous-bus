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

In progress, pull request pending: a Stop hook that scores the agent's chat reply, and a hit log of what the guard reported. Neither is in the plugin yet.

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

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install plain-prose@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/plain-prose`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export plain-prose ./out`, then `claude plugin marketplace add ./out` and `claude plugin install plain-prose@plain-prose-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/plain-prose/skills/ste80-humanizer -a claude-code`. This is the upstream skill alone, with none of the added rules, hooks or thresholds. For that, install it from upstream directly.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin). License: MIT, with the upstream notice in `skills/ste80-humanizer/LICENSE`.
