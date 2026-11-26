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

## Install

Pick one. Each needs no other plugin from this repo.

1. **Marketplace, whole repo.** `claude plugin marketplace add warxhead1/nervous-bus`, then `claude plugin install isolated-dispatch@nervous-bus`. This clones the full repository.
2. **Marketplace, one folder.** `claude plugin marketplace add warxhead1/nervous-bus --sparse .claude-plugin steering/isolated-dispatch`, then the same install command.
3. **Standalone copy.** From a clone, run `steering/tools/export isolated-dispatch ./out`, then `claude plugin marketplace add ./out` and `claude plugin install isolated-dispatch@isolated-dispatch-standalone`.
4. **Skill only.** `npx skills add https://github.com/warxhead1/nervous-bus/tree/main/steering/isolated-dispatch/skills/isolated-dispatch -a claude-code`. This gives the method, not the guards.

More on these paths: [steering/README.md](../README.md#take-just-one-plugin).
