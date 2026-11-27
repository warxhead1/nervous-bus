# remediation-ladder

Makes an agent fix a recurring problem at the highest rung it can reach: eliminate, then automate, and write a rule only last.

## Why it exists

A reachable vulnerability had no upstream fix and blocked every deploy. The agent called "remove the dependency" a large, risky refactor and offered an operator bypass as the fast path. The owner chose removal. It took under five minutes: a standard-library zip and XML reader that was byte-identical to the replaced library on a differential test, plus a test for the vulnerability's own crash case. The agent had flinched at its own estimate.

The same pattern shows up smaller: a retry, a longer timeout, a loosened threshold, a suppression, or `--no-verify` makes the symptom stop and leaves the cause running.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `remediation-ladder` | loaded when a problem recurs, or a check is in the way | the three rungs, a band-aid test, and "cost the real fix honestly before offering a bypass" | |
| `bypass-guard` | PreToolUse on Bash | blocks (exit 2) a command that skips a check: `--no-verify`, `HUSKY=0`, `SKIP=`, a guard's own kill switch set inline, `-DskipTests` | `BYPASS_GUARD_DISABLE=1` in the environment the hook runs in. Setting it inside the command does nothing |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt says band-aid, root cause or "keeps happening" | `STEERING_CUES_DISABLE=1` |

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [remediation-ladder.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/remediation-ladder.md).

> # remediation-ladder
>
> Fix the cause, not the symptom. A change that skips, masks, retries, or loosens a check until it passes is a band-aid: say why the check fails first. Estimate the real fix honestly before offering a way around it; it is often small.
>
> For a problem that recurs, take the highest rung you can: 1) eliminate it, so the mistake cannot be made; 2) automate it, with a hook, guard, or test that needs no one to remember; 3) only then write a rule. Say which rung you chose and why not the one above.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/remediation-ladder/skills/remediation-ladder/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`bypass-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/remediation-ladder/hooks/bypass-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Bash) and `tool_input.command`. Exit 0 allows. Exit 2 means the command skips a check (`--no-verify`, `HUSKY=0`, `-DskipTests`), with the message on stderr. Bad input or a kill switch always exits 0. | block | PreToolUse on Bash | `BYPASS_GUARD_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/remediation-ladder/hooks/cue) | One JSON object on stdin with `prompt` (UserPromptSubmit) or `tool_name`/`tool_input`/`tool_response` (PostToolUse). Prints the rule on stdout (or `hookSpecificOutput.additionalContext` JSON on PostToolUse) when `cues.json` matches, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | PostToolUse; UserPromptSubmit on Bash\|Write\|Edit\|MultiEdit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/remediation-ladder.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/remediation-ladder.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/remediation-ladder.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/remediation-ladder.json), [omp/post](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/remediation-ladder.post.ts), [omp/pre](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/remediation-ladder.pre.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install remediation-ladder@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/remediation-ladder`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export remediation-ladder ./out`, then `claude plugin marketplace add ./out` and `claude plugin install remediation-ladder@remediation-ladder-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/remediation-ladder/skills/remediation-ladder -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
