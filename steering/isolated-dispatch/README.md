# isolated-dispatch

Gives every agent that edits files its own worktree, and keeps anyone's uncommitted work from being hidden or overwritten.

## Why it exists

- Two agents in one checkout overwrite each other, and `git commit --only <path>` commits whatever is at that path, whoever wrote it.
- A worktree path written into a prompt is not isolation. The agent can still write to the main checkout and report success, because the untouched worktree still compiles.
- The stash stack is shared by every worktree of a repository. A bare `git stash` takes every tracked change, including another session's work. `git stash push <file>` saves nothing when the change is already committed, and the `pop` that follows then applies someone else's entry to your tree.

## What it ships

| piece | event | effect | off |
|---|---|---|---|
| skill `isolated-dispatch` | loaded before dispatching an editing agent, or when the tree holds changes you did not make | the method, including how to merge branches back | |
| `dispatch-isolation-guard` | PreToolUse on Agent and Task | blocks (exit 2) a dispatch whose prompt asks for edits and sets no isolation. A prompt that says READ-ONLY passes, and so do reader types (`Explore`, `Plan`, names like review or audit) | `ISOLATED_DISPATCH_DISABLE=1` |
| `stash-guard` | PreToolUse on Bash | blocks an anonymous `git stash` and a `pop` or `drop` that names no entry. Allowed: `stash push -m <tag>`, `list`, `show`, `apply`, `create`, `store`, `branch`, and `pop` or `drop` of a named entry | `STASH_GUARD_DISABLE=1` |
| `cue` | UserPromptSubmit | adds the rule to context when the prompt calls for it | `STEERING_CUES_DISABLE=1` |

`ISOLATED_DISPATCH_READONLY_TYPES` is a comma list of extra agent types that only read.

<!-- catalog:start -->
## Use it in any agent

Each piece below works alone. Nothing here needs Claude Code, and nothing needs the rest of this repository.

**The rule.** Paste this into `AGENTS.md`, `GEMINI.md`, a Kiro steering file or any system prompt. File: [isolated-dispatch.md](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/rulings/isolated-dispatch.md).

> # isolated-dispatch
>
> Every agent that edits files gets its own worktree and its own branch; never two agents on one file. Check the tree is clean and the tests are green before dispatching. Each agent pushes its branch after its first commit; you do the merges.
>
> Changes in the tree that you did not make are someone's work. Read them (`git diff`, `git log`) before anything else; never stash, reset, or check out over them. Name the branch you are on before a commit, merge or push.

**The skill.** An [Agent Skills](https://agentskills.io) folder: [`SKILL.md`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/isolated-dispatch/skills/isolated-dispatch/SKILL.md).

**The checks.** Plain Python 3 scripts with no dependencies.

| check | what it takes | effect | event and matcher | off |
|---|---|---|---|---|
| [`dispatch-isolation-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/isolated-dispatch/hooks/dispatch-isolation-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Agent or Task) and `tool_input` with `prompt`, `subagent_type`, `model`, `isolation`. Exit 0 allows. Exit 2 means the dispatch edits files with no isolation, with the message on stderr. Bad input or a kill switch always exits 0. | block | PreToolUse on Agent\|Task | `ISOLATED_DISPATCH_DISABLE=1` |
| [`stash-guard`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/isolated-dispatch/hooks/stash-guard) | One JSON object on stdin, as Claude Code sends to a hook. `tool_name` (Bash) and `tool_input.command`. Exit 0 allows. Exit 2 means the stash is anonymous and shared across worktrees, with the message on stderr. Bad input or a kill switch always exits 0. | block | PreToolUse on Bash | `STASH_GUARD_DISABLE=1` |
| [`cue`](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/isolated-dispatch/hooks/cue) | One JSON object on stdin with `prompt`. Prints the rule on stdout when the prompt matches `cues.json`, nothing otherwise. Always exits 0. Keep with it: `cues.json`. | context | UserPromptSubmit | `STEERING_CUES_DISABLE=1` |

**Ready-made hook config** (set `STEERING_HOME`, see the harness page): [claude-code](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/claude-code/isolated-dispatch.settings.json), [codex](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/codex/isolated-dispatch.hooks.json), [gemini](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/gemini/isolated-dispatch.settings.json), [kiro](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/kiro/isolated-dispatch.json), [omp/pre](https://raw.githubusercontent.com/warxhead1/nervous-bus/main/steering/adapters/omp/isolated-dispatch.pre.ts).

Wiring, hook events, input shapes and what each hook can block: [Claude Code](../adapters/claude-code.md), [Codex CLI](../adapters/codex.md), [Gemini CLI](../adapters/gemini.md), [Kiro CLI](../adapters/kiro.md), [oh-my-pi](../adapters/omp.md).
<!-- catalog:end -->

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install isolated-dispatch@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/isolated-dispatch`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export isolated-dispatch ./out`, then `claude plugin marketplace add ./out` and `claude plugin install isolated-dispatch@isolated-dispatch-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/isolated-dispatch/skills/isolated-dispatch -a claude-code`. This gives the method, not the guards.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
