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

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install keep-moving@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/keep-moving`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export keep-moving ./out`, then `claude plugin marketplace add ./out` and `claude plugin install keep-moving@keep-moving-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/keep-moving/skills/keep-moving -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
