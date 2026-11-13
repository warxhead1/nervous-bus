---
name: keep-moving
description: >
  Keeps work moving without stalling or spinning. Use when about to end a turn
  waiting on a build, test, deploy, review, or another agent; when about to ask
  the user for a go-ahead they already gave; when a command has been run
  several times with the same result; when blocked on one item with others
  open; or when the user asks why you stopped, paused, are waiting, or keep
  asking. Don't use to skip a confirmation before a destructive, costly, or
  outward-facing step.
---

# Keep moving

## Do not stall

- A wait is never the only thing happening. Before you wait, name the
  independent work that can run now and start it.
- One blocked item does not block the rest.
- If the user asked for the outcome, the steps toward it are approved. Ask only
  when the choice is theirs: money, scope, policy, or something hard to undo.
- Do not hand back a step you can do.
- A status carries the clock: time elapsed, the known cap, and what happens at
  the cap, read from the job itself this turn.
- Watch for the next phase, not the last. A job that fails early should wake
  you early. Check that the file and marker you wait on exist.

## Do not spin

- Running the same command again with nothing changed gives the same result.
  Read the output you have, or read the code.
- After two failed attempts at one idea, write down what you tried and what each
  showed, then change the idea.
- A slow run costs the machine and the person's time. Before starting one, say
  what you expect to differ.
- If you are stuck and have no next idea, say so and ask. That is not stalling.

The hook blocks the third identical build or test run when no file changed in
between. Off: `RERUN_GUARD_DISABLE=1`.
