---
name: evidence-before-done
description: >
  Holds a claim of "done", "fixed", "works", or "passing" to evidence observed
  this turn, and holds code to being finished. Use before reporting a result,
  closing a task, committing, or relaying what a subagent said it did; when a
  test fails and the cause looks "pre-existing"; when tempted to skip a test,
  silence a warning, or leave a stub; and when the user says something still
  does not work. Don't use for plans or estimates, which claim nothing yet.
---

# Evidence before done

"Done" is a claim. State it only with what you observed.

## Before you say it works

1. Run the thing the claim is about: the real command, the real screen, the
   real device. A neighbouring check is not it.
2. Read the primary artifact this turn: the exit code, the log's last lines,
   the screenshot. Not a summary, not a monitor from an earlier run.
3. Say what you ran and what it showed. If a step was skipped, say that.

"It should work" means you have not checked. Say "not verified" instead.

## When the user says it still fails

They ran it. Look at the failure path. Do not explain why it should have worked.

## Claims that need disproving, not repeating

- **"Pre-existing failure."** Show it failing on the base commit, or it is yours.
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

The hook reports stub markers and suppressions an edit adds. Off:
`UNFINISHED_GUARD_DISABLE=1`.
