# grill

Sends an open game design question to the crucible ledger instead of a guess, a chat message or a Needs-you bullet, so only taste, money or direction reaches the owner.

## Why it exists

- A guessed design call is a decision nobody logged and nobody can flip. A question asked in chat or in a Needs-you bullet depends on a live session, so its answer never reaches the ledger or the lane that asked.
- The owner's attention is the scarce resource. `crucible grill ask` looks the answer up, measures it, takes a small reversible call and logs it, or posts one card.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `grill` | loaded when about to guess a design choice, ask the owner one, or draft a card | the method | |
| `ledgered-ask-guard` | PreToolUse on Bash | blocks (exit 2) a `hearth-ask post` with no `--id q-<ulid>` when the working directory is inside a repo that has `design/ledger`. The message names `crucible grill ask`. A post outside such a repo passes, and so does a post whose `--id` is a shell variable | `LEDGERED_ASK_GUARD_DISABLE=1` |
| `cue` | UserPromptSubmit | adds the rule to context on "grill me" and on design-question phrasing | `STEERING_CUES_DISABLE=1` |

`evals/trigger.json` holds the prompts the cue must catch and the load rate measured for them.

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [grill.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/grill.md).

> # grill
>
> An open design question goes to `crucible grill ask`. Do not guess the answer, and do not ask it in chat or in a Needs-you bullet. The command looks the answer up, measures it, takes it as a logged lead decision, or posts one card to the owner.
>
> Ask the owner only for taste, money or direction, and only when a wrong guess costs more than the owner's attention. Look up a fact. Measure what a run can measure. Decide what follows from settled decisions, and log it. Hard questions, not many questions.
>
> A card is one idea with two to four real options. Each option says what it changes, the recommended option comes first, and the rationale stays one tap down. A one-way card or a money card carries no default.
>
> The owner's answer words are reserved: "yes", "send", "call it", "you decide", "later", "test it", "why", "wrong question", "harder" and "enough". Each one matches by exact rule, and no model interprets it.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/grill/skills/grill/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`ledgered-ask-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/grill/hooks/ledgered-ask-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Bash) and `tool_input.command`. Exit 0 allows. Exit 2 means the post has no ledger id, so its answer never reaches the ledger; the message names `crucible grill ask`, with the message on stderr. Bad input or a kill switch always exits 0. | block | PreToolUse on Bash | `LEDGERED_ASK_GUARD_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/grill/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/grill.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/grill.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/grill.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/grill.json), [omp/pre](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/grill.pre.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install grill@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/grill`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export grill ./out`, then `claude plugin marketplace add ./out` and `claude plugin install grill@grill-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/grill/skills/grill -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
