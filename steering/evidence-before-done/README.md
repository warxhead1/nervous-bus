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

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [evidence-before-done.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/evidence-before-done.md).

> # evidence-before-done
>
> A claim of done, fixed, or passing needs what you observed this turn: run the real thing, read its output, then say what you ran and what it showed. Check a subagent's specific claim before repeating it.
>
> "Pre-existing" is a claim to disprove: show the failure on the base commit, or treat it as yours and fix it.
>
> A skipped test, a suppression, or a stub hides the problem. Fix the cause. If one has to stay, report it with the reason and do not call the work done.
>
> The user ran it and it failed. Look at the failure path they describe; do not explain why it should have worked. Reproduce it before changing anything.
>
> Tests do not see a screen. After a visual change, run the real build, take a screenshot, read it, and say what you saw against what was asked. If you cannot look, report the result as unseen.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/evidence-before-done/skills/evidence-before-done/SKILL.md) with 1 more file(s) listed in [catalog.json](../catalog.json).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`unfinished-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/evidence-before-done/hooks/unfinished-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Write, Edit, MultiEdit) and `tool_input` with `file_path` and `content` or `old_string`/`new_string`. Exit 0 allows. Exit 2 means the edit added a stub or suppression; the write stands, with the message on stderr. Bad input or a kill switch always exits 0. | report | PostToolUse on Write\|Edit\|MultiEdit | `UNFINISHED_GUARD_DISABLE=1` |
| [`visual-proof-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/evidence-before-done/hooks/visual-proof-guard) | One JSON object on stdin, as Claude Code sends to a hook. `session_id`, `transcript_path` (a Claude Code transcript .jsonl), `stop_hook_active`. Exit 0 allows. Exit 2 means the agent should look at the result before it finishes, with the message on stderr. Bad input or a kill switch always exits 0. | send-back | Stop | `VISUAL_PROOF_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/evidence-before-done/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/evidence-before-done.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/evidence-before-done.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/evidence-before-done.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/evidence-before-done.json), [omp/post](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/evidence-before-done.post.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install evidence-before-done@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/evidence-before-done`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export evidence-before-done ./out`, then `claude plugin marketplace add ./out` and `claude plugin install evidence-before-done@evidence-before-done-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/evidence-before-done/skills/evidence-before-done -a claude-code`. This gives the method and rubric, not the hooks.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
