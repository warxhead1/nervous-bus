---
name: isolated-dispatch
description: >
  Keeps parallel work from overwriting itself. Use before dispatching any agent
  that edits files, before running two agents or sessions on one repository,
  when the working tree holds changes you did not make, when about to stash,
  reset, or check out over uncommitted work, when merging agent branches back,
  or when the user says work was lost, overwritten, or landed on the wrong
  branch. Don't use for a single edit you make yourself in a clean tree.
---

# Isolated dispatch

## Before you dispatch

- Look at the tree: `git status`. Changes you did not make mean another session
  is live. Agents sent in now will pick up that work as their own.
- Run the tests. A red bar before you start gives every agent the same excuse.
- Split the work by file. Never two agents on one file, even for unrelated
  changes. A commit takes whatever is at that path.

## The dispatch

- Every agent that edits files gets its own worktree. Use the dispatch's
  isolation setting. A worktree path written in the prompt is not isolation:
  the agent can still write to your checkout and report success.
- Never mix isolated and unisolated agents on one checkout.
- Name the base branch in the prompt and tell the agent to check where it is
  (`git rev-parse --show-toplevel`, `git symbolic-ref --short HEAD`) before it
  edits.
- Let the agent use git: branch, commit, push its own branch after the first
  commit. A branch that is not pushed can be lost with the worktree.
- Give it a scratch directory outside the checkout.

## Bringing work back

- You do the merges, one branch at a time, tests between.
- A pushed branch is fixed. If another branch landed first, start a new branch
  from the main line and cherry-pick onto it. Do not rebase and force-push.
- Before deleting a worktree, check it holds nothing unpushed.

## Work that is not yours

- Never stash, reset, clean, or check out over changes you did not make. Read
  them first: `git diff`, `git log`.
- The stash stack is shared by every worktree. A bare `git stash` takes
  everything; `pop` takes the top entry, whoever pushed it. To set your own
  work aside, commit it as WIP on a branch.
- Name the branch before a commit, merge, or push. Do not assume it.
- One destructive git command per call, never chained with others.

Two hooks: one blocks a dispatch that asks for edits with no isolation (say
READ-ONLY in the prompt for a reader; off: `ISOLATED_DISPATCH_DISABLE=1`), one
blocks a bare `git stash` and `stash pop/drop/clear` (off: `STASH_GUARD_DISABLE=1`).
