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

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install remediation-ladder@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/remediation-ladder`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export remediation-ladder ./out`, then `claude plugin marketplace add ./out` and `claude plugin install remediation-ladder@remediation-ladder-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/remediation-ladder/skills/remediation-ladder -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
