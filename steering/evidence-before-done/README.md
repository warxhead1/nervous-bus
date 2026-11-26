# evidence-before-done

Holds a claim of "done" to what the agent observed this turn, and holds code to being finished.

## Why it exists

Four failures repeated across agent projects:

- An agent called a failure "pre-existing" without running the base commit.
- A report described an external page from stale notes. An invisible frame was called a CAPTCHA, and an account was said to need creating that already existed.
- A design picked a payment vendor that had shut down its API months earlier, because nobody searched the web for the vendor's status.
- A subagent report carried precise-sounding detail (status codes, row counts, "file absent") that the agent had invented. Its direction was right and its mechanism was wrong.

Tests also do not see a screen. A page can pass every test and look wrong, so the plugin sends the agent back to look.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `evidence-before-done` with `visual-rubric.md` | loaded on a done, fixed or works claim, or after a UI change | the method and a rubric for judging a screenshot | |
| `unfinished-guard` | PostToolUse on Write, Edit, MultiEdit | reports stubs (TODO, `todo!()`, "for now") and suppressions the edit adds (exit 2). The write stands | `UNFINISHED_GUARD_DISABLE=1` |
| `visual-proof-guard` | Stop | sends the agent back once if it edited a UI file and never looked at a rendered result. It fires once per edit | `VISUAL_PROOF_DISABLE=1` |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt mentions a look or a screenshot | `STEERING_CUES_DISABLE=1` |

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install evidence-before-done@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/evidence-before-done`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export evidence-before-done ./out`, then `claude plugin marketplace add ./out` and `claude plugin install evidence-before-done@evidence-before-done-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/evidence-before-done/skills/evidence-before-done -a claude-code`. This gives the method and rubric, not the hooks.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
