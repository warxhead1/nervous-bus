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

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install falsify-your-gate@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/falsify-your-gate`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export falsify-your-gate ./out`, then `claude plugin marketplace add ./out` and `claude plugin install falsify-your-gate@falsify-your-gate-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/falsify-your-gate/skills/falsify-your-gate -a claude-code`. This gives the method, not the hook.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
