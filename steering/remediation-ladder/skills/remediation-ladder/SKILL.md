---
name: remediation-ladder
description: >
  Chooses the kind of fix. Use when a problem has happened before, when a
  check, test, hook, or guard is in the way and skipping it is tempting, when
  about to add a retry, a longer timeout, a looser threshold, a suppression, or
  a special case, when about to write a rule or note so a mistake is not
  repeated, or when the user asks for the root cause, calls something a
  band-aid or workaround, or says it keeps happening. Don't use for a
  first-time bug with an obvious one-line cause.
---

# Remediation ladder

For anything that recurs, climb as high as you can:

1. **Eliminate.** Change the structure so the mistake cannot be made. Remove
   the dependency, the duplicate, the manual step, the second source of truth.
2. **Automate.** A hook, guard, test, or wrapper catches it every time with no
   one remembering.
3. **Inform.** A written rule that someone has to recall. Last resort.

Say which rung you chose, and why not the one above.

## Is it a band-aid?

It is if it makes the symptom stop and leaves the cause running:

- a retry, a sleep, or a longer timeout around something that should not fail
- a threshold loosened until the check passes
- a suppression, a skipped test, a caught-and-ignored error
- a special case for the one input that broke
- "pre-existing" used as a reason to leave a failure alone
- turning a feature or tool off because one caller misuses it

Ask: after this change, does the bad thing still happen and go unseen?

## Rules

- A check blocks a mechanism, not the goal. Find the path it allows. Do not
  skip it and do not hand the skipped step to the user.
- If the check is wrong, fix the check, and say so.
- Estimate the real fix honestly before offering a way around it. The
  structural fix is often minutes. Do not make it sound large so that the
  bypass sounds reasonable.
- Measure before you fix. A proposed remedy is a guess until it has been tried
  on the real case.
- When the cause is fixed, remove the mitigation that covered for it.
- A rule you add gets a way to tell if it works. One that shows no effect is
  cut.

The hook blocks a command that skips a check: `--no-verify`, `HUSKY=0`,
`SKIP=`, a guard's own kill switch set inline, `-DskipTests`. Off:
`BYPASS_GUARD_DISABLE=1` in the environment.
