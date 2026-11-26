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

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [model-tiering.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/model-tiering.md).

> # model-tiering
>
> Before dispatching: one agent with the right skill usually matches a team at lower cost. Dispatch only for parallel work in throwaway contexts, reading that would flood yours, or a second opinion. Set `model` on every dispatch by what would catch a wrong answer: a test or diff means a small model; nothing mechanical means the top model or yourself.
>
> Pick the model by what would catch a wrong answer: a test, compiler, or diff you will read means a small model; nothing mechanical (design, copy, pricing, final review) means the top model. State the plan in one line before more than three agents.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/model-tiering/skills/model-tiering/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`dispatch-model-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/model-tiering/hooks/dispatch-model-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Agent or Task) and `tool_input` with `prompt`, `subagent_type`, `model`, `isolation`. Exit 0 allows. Exit 2 means the dispatch has no `model`, with the message on stderr. Bad input or a kill switch always exits 0. | block | PreToolUse on Agent\|Task | `MODEL_TIERING_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/model-tiering/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/model-tiering.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/model-tiering.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/model-tiering.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/model-tiering.json).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install model-tiering@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/model-tiering`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export model-tiering ./out`, then `claude plugin marketplace add ./out` and `claude plugin install model-tiering@model-tiering-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/model-tiering/skills/model-tiering -a claude-code`. This gives the method, not the guard.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
