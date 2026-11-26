# falsify-your-gate

Counts a test, gate or detector only after the agent has watched it fail on the bad case.

## Why it exists

- A gate that failed and then passed on rerun was treated as green. In one case a `paste | awk '...exit'` pipeline under `pipefail` died of SIGPIPE at random. It stayed hidden through 18 clean reruns and showed up on the first run that kept its log.
- A failing test was "fixed" by changing the test. Without checking that the assertions survived and the measured event still happens, that edit is thresholding to green, and it looks the same in a report as a real fix.
- A detector's "would have prevented 100%" came from a simulation. Measured on the real tree it was a hypothesis.

The rule: make it fail, make it pass on good work, and check that it ran at all (zero tests collected and a filter that matched nothing both look green).

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `falsify-your-gate` | loaded when writing or changing a test, gate, lint rule or detector, or choosing a threshold | the method | |
| `vacuous-test-guard` | PostToolUse on Write, Edit, MultiEdit | reports (exit 2) a test the edit adds that cannot fail: an always-true assertion (`assert True`, `expect(true).toBe(true)`) or a Python test with no assertion. The write stands | `VACUOUS_TEST_GUARD_DISABLE=1` |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt asks why something passed | `STEERING_CUES_DISABLE=1` |

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [falsify-your-gate.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/falsify-your-gate.md).

> # falsify-your-gate
>
> A check counts only after you have seen it fail on the bad case. Break the code or plant the defect, watch it go red, restore. Assert on what the code produced, not on a constant.
>
> A green result means nothing until you know the check ran and can go red: count what it ran, run it on a known-bad case, and run it on known-good work to see how often it cries wolf. A threshold comes from measured data. If a fix changed the test, the assertions and the thing it measures must still be there.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/falsify-your-gate/skills/falsify-your-gate/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`vacuous-test-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/falsify-your-gate/hooks/vacuous-test-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Write, Edit, MultiEdit) and `tool_input` with `file_path` and `content` or `old_string`/`new_string`. Exit 0 allows. Exit 2 means the edit added a test that asserts nothing or something always true; the write stands, with the message on stderr. Bad input or a kill switch always exits 0. | report | PostToolUse on Write\|Edit\|MultiEdit | `VACUOUS_TEST_GUARD_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/falsify-your-gate/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/falsify-your-gate.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/falsify-your-gate.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/falsify-your-gate.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/falsify-your-gate.json), [omp/post](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/falsify-your-gate.post.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install falsify-your-gate@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/falsify-your-gate`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export falsify-your-gate ./out`, then `claude plugin marketplace add ./out` and `claude plugin install falsify-your-gate@falsify-your-gate-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/falsify-your-gate/skills/falsify-your-gate -a claude-code`. This gives the method, not the hook.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
