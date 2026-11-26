# keep-moving

Keeps an agent working on the next thing instead of waiting, and stops it from rerunning a command whose result cannot change.

## Why it exists

A person waited more than 30 minutes on a turn that held only a waiter. Another waiter sat dead for 50 minutes because it watched for a marker that the script never printed. On the other side, agents rerun the same build or test with no edit in between and expect a different result, which burns time and machine.

The rule: a wait is never a turn's only activity, a status carries the clock (elapsed, cap, what happens at the cap), and a rerun needs a change first.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `keep-moving` | loaded when about to wait, ask for a go-ahead already given, or rerun | the method | |
| `rerun-guard` | PreToolUse on Bash | blocks (exit 2) the third identical build or test command in a session when no file was edited in between. The next attempt passes | `RERUN_GUARD_DISABLE=1` |
| `rerun-guard` | PostToolUse on Write, Edit, MultiEdit | resets the count when a file changes | same switch |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt calls for it | `STEERING_CUES_DISABLE=1` |

The guard knows common runners (`cargo`, `pytest`, `go`, `npm test`, `make` and others). State lives under `$XDG_CACHE_HOME/rerun-guard`.

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [keep-moving.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/keep-moving.md).

> # keep-moving
>
> A wait is never the only thing happening: name the independent work that can run now and start it. One blocked item does not block the rest. Any status carries the clock: elapsed, the cap, what happens at the cap.
>
> If the user asked for the outcome, the steps toward it are approved. Ask only when the choice is theirs: money, scope, policy, or something hard to undo. Do not hand back a step you can do.
>
> Running the same command with nothing changed gives the same result. Read the output you have or read the code, write down what each attempt showed, then change the idea.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/keep-moving/skills/keep-moving/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`rerun-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/keep-moving/hooks/rerun-guard) | One JSON object on stdin, as Claude Code sends to a hook. `session_id`, `hook_event_name`, `tool_name` (Bash) and `tool_input.command`. Exit 0 allows. Exit 2 means the same command ran twice already with no edit between, with the message on stderr. Bad input or a kill switch always exits 0. | block | PreToolUse (counts) and PostToolUse (resets on an edit) on Bash; Write\|Edit\|MultiEdit | `RERUN_GUARD_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/keep-moving/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/keep-moving.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/keep-moving.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/keep-moving.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/keep-moving.json), [omp/post](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/keep-moving.post.ts), [omp/pre](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/keep-moving.pre.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install keep-moving@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/keep-moving`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export keep-moving ./out`, then `claude plugin marketplace add ./out` and `claude plugin install keep-moving@keep-moving-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/keep-moving/skills/keep-moving -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
