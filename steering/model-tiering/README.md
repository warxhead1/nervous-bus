# model-tiering

Makes every subagent dispatch name its model, chosen by what would catch a wrong answer.

## Why it exists

A dispatch with no `model` inherits the session model. In one session about 50 agents ran at the top tier by default and used up a five-hour usage window. A fan-out multiplies whatever tier it inherits.

The routing question is "if this model gets it subtly wrong, what finds out?" Work with a test, compiler or gate behind it can go to a small model, because a wrong answer is one retry. Work with nothing mechanical behind it (design, copy, naming, pricing, a final review) stays on the top model, because a miss ships. In another case a small model's packet made "hire counsel" the gate on a venture, and nothing caught it.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `model-tiering` | loaded before an Agent or Task call, a workflow script, or a fan-out | whether to dispatch at all, then which tier | |
| `dispatch-model-guard` | PreToolUse on Agent and Task | blocks (exit 2) a dispatch with no `model` | `MODEL_TIERING_DISABLE=1` |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt mentions cost, limits or agents | `STEERING_CUES_DISABLE=1` |

`MODEL_TIERING_ALLOW_TYPES` is a comma list of subagent types that pin their own model and may omit it.

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install model-tiering@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/model-tiering`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export model-tiering ./out`, then `claude plugin marketplace add ./out` and `claude plugin install model-tiering@model-tiering-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/model-tiering/skills/model-tiering -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
