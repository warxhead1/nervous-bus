# Adversarial review rubric

You review one branch against its base. Assume the author is competent and the tests are green. Your job is to find what the green bar hides. Every finding needs evidence observed in this review: a file and line, a command and its output, or a run you made. A finding you cannot show is a question, so list it as one.

## 1. Take in the breadth first (about 10 percent of your effort)

1. `git diff --stat <base>...HEAD` and `git log <base>..HEAD`. Read every commit body, because the author's claims are in there.
2. If a spec, issue or brief is given, list its items. Each one is a row you must close: landed (where), not landed, or landed differently.
3. Build a risk map before reading code. Rank the changed files:
   - entry points and handlers that take outside input;
   - auth, sandbox, secrets and network;
   - state, persistence, cursors and anything idempotent;
   - concurrency;
   - the tests and gates themselves.
   Read the top of the map in full. Skim the rest.
4. Find how the change is reached: callers, registrations, routes, CLI wiring. Code nobody calls is a finding.

## 2. Run things

- Run the repo's tests for the changed area. Note the count, so a later "all pass" can be compared.
- For each test that claims to cover a risk-map item, break the code it covers (flip a comparison, return early, drop a write) and run it. A test that still passes is vacuous. Revert your break.
- Deeper pass (`ub` depth) adds three steps, all on changed code only:
  - `ub-gate crap --changed --no-coverage`: report each changed function over CRAP 10.
  - `ub-gate dry --since <base> --against-repo`: report new near-copies of existing code, grouped.
  - `ub-gate mutate <file>` on the two highest-risk changed files: report each surviving mutant at a boundary, and name equivalent mutants as such.

## 3. What to hunt (each item has caught a real defect before)

**Thresholding to green**
- A test whose stimulus changed so it no longer exercises the behaviour, while its assertion survived.
- An assertion turned into a counter that the code under test increments itself.
- A control that withholds more than one cause, or a control that is a kill switch.
- A scenario retuned until it passes at a knife edge. Move each new param 10 percent and rerun.
- A threshold loosened in the same change that "fixes" the failure.

**Hollow passes**
- A check that passes because the thing it measures was absent: zero items, a dead party, an empty list, a skipped stage reported as ok, a time-cap fallback.
- Ask what must still be true when the check passes, and verify it holds.

**Over-wide claims**
- A bound proven on one path and claimed for all ("never", "any", "impossible"). Find the path it does not cover.

**Idempotence and partial failure**
- Cursors, offsets, retries and dedup: rerun after a crash mid-batch, at offset zero, on an empty file, and with an item still open.
- What happens when the Nth of M writes fails? Does a rerun duplicate the first N-1 or lose them?

**Security and privacy**
- Secrets in output, logs, URLs or error messages.
- Free text copied onto a shared or public surface. Labels, descriptions, commands and paths leak names and home dirs.
- A public repo carrying home paths, private names or private schemas.
- Input that reaches a shell, a path, SQL, HTML or a URL without validation: injection, traversal, SSRF.
- Auth and ownership: can an actor act on what it does not own?
- Sandboxes: network off where promised, iframe and CSP limits, file-size and MIME caps.
- Endpoints bound to all interfaces when they should be local.

**Determinism** (where the repo promises it)
- Floats, hash-map iteration, wall clock, thread order or unseeded RNG in simulated or replayed state.

**Gate honesty**
- Stages that skip and pass, cache keys missing a subtree, e2e that runs on a software renderer, a budget raised to make room.

**Missing functionality**
- Every spec item not landed and not named as not landed.
- Error paths with no test, documented flags that do nothing, exit codes that differ from the docs.
- A "not landed" list that holds items with no real blocker. Those are not done.

**Schema and contract drift**
- Data written that its own schema would reject: lengths, patterns, required fields, anchors such as `$` against `\Z`.

## 4. Report

List findings ranked BLOCKER, HIGH, MEDIUM, NIT. Each finding gives:
- the defect in one sentence;
- the evidence (file:line, plus a command and its output);
- the concrete fix, with the test that would catch it.

Then add the spec rows from step 1.2. End with exactly one line: `VERDICT: PASS` or `VERDICT: FAIL`. Any BLOCKER or HIGH is a FAIL. Edit nothing on the branch.
