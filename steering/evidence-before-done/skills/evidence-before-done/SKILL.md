---
name: evidence-before-done
description: >
  Holds a claim of "done", "fixed", "works", or "passing" to evidence observed
  this turn, and holds code to being finished. Use before reporting a result,
  closing a task, committing, or relaying what a subagent said it did; when a
  test fails and the cause looks "pre-existing"; when tempted to skip a test,
  silence a warning, or leave a stub; and when the user says something still
  does not work; and after changing anything a person looks at. Don't use for
  plans or estimates, which claim nothing yet.
---

# Evidence before done

"Done" is a claim. State it only with what you observed.

## When a test fails: check the base commit first

Never call a failing test "pre-existing", "unrelated", or "not caused by my change" by reading code or `git diff` alone. Your first concrete step is to prove it on the base commit or fix it:

1. Run the failing test on the base commit (`main`):
   ```bash
   git worktree add /tmp/base-check main && (cd /tmp/base-check && <test command>) ; git worktree remove --force /tmp/base-check
   ```
   (Or `git stash && git checkout main && <test command>; git checkout - && git stash pop` when worktrees are not available.)
2. Read the base commit's actual test output.
3. If the same test fails on `main`, report that observed result and still report the branch as **not ready to merge** while the suite is red (or fix the failure if it affects your code). If you did not run it on `main`, treat the failure as yours and fix it.

## Before you say it works

1. Run the thing the claim is about: the real command, the real screen, the
   real device. A neighbouring check is not it.
2. Read the primary artifact this turn: the exit code, the log's last lines,
   the screenshot. Not a summary, not a monitor from an earlier run.
3. Say what you ran and what it showed. If a step was skipped, say that.

"It should work" means you have not checked. Say "not verified" instead.

## When a person will look at it

Tests do not see a screen. After changing a page, a component, a style, or a
layout: run the real build, take a screenshot, read it, and say what you saw
against what was asked. Judge it with [visual-rubric.md](visual-rubric.md),
from the screenshot and the code together. If you cannot look, report it as
unseen.

## When the user says it still fails

They ran it. Look at the failure path. Do not explain why it should have worked.

## Other claims that need disproving, not repeating

- **A subagent's report.** Direction is usually right; details are often invented.
  Check the specific claim before repeating it.
- **A new gate or test.** Break the thing on purpose and watch it fail. A check
  that has never failed has not been shown to work.
- **"Never", "always", "impossible".** The proof covered one path. Name it.

## Finished means finished

- No stubs, placeholders, or "for now". Write the real code.
- No skipped test, lint suppression, swallowed error, or bypass to get green.
  Fix the cause. If one has to stay, report it with the reason.
- Fix every instance of a bug, not the one you were shown.

Two hooks: one reports stub markers and suppressions an edit adds (off:
`UNFINISHED_GUARD_DISABLE=1`); one sends you back once if you changed a UI file
and end the turn without having looked at the result (off:
`VISUAL_PROOF_DISABLE=1`). A cue hook also fires on failing test output.
