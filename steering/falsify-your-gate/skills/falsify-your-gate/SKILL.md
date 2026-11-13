---
name: falsify-your-gate
description: >
  Proves that a check can fail. Use when writing or changing a test, CI gate,
  lint rule, detector, monitor, alert, eval, or guard hook; when choosing a
  threshold; when a failing test was made to pass by changing the test; when a
  green result is about to be trusted for a merge or deploy; or when the user
  asks why something passed, whether a test would have caught a bug, or about
  false positives. Don't use for running an existing suite you did not change
  and have no reason to doubt.
---

# Falsify your gate

A check you have never seen fail is a guess. Before you count it:

1. **Make it fail.** Break the code it guards, or plant the defect, and watch
   it go red for that reason. Then restore. A test written after the fix is
   run once against the unfixed code.
2. **Make it pass on good work.** Run a detector or lint rule on work you know
   is fine. If it fires there, it will be ignored or turned off.
3. **Check that it ran.** Zero tests collected, a filter that matched nothing,
   a skipped suite, and an exit code hidden behind a pipe all look green.

## Tests

- Assert on what the code produced. A test that restates a constant, checks
  only that a mock was called, or asserts something always true cannot fail.
- If you fixed a red test by changing the test, two things must hold: the
  assertions are untouched, and the thing it measures still happens. Either
  one missing means the test now hides the bug.
- A test worth running twice lives in the repository, not in a scratch file.

## Detectors, gates, thresholds

- Give every detector a known-answer case: a small input where the right
  output is certain.
- Take a threshold from measured data. Look at the spread on good and bad
  cases first. A number picked to turn the current run green is thresholding.
- Replay a new guard over real history before turning it on. Count how often
  it fires and read a sample by hand.
- A simulated "would have prevented" is a hypothesis. Measure on the real tree.
- Check the gate tested the commit you meant.

## Reporting

Say what you saw fail and what you saw pass, with the counts. If you did not
see it fail, say that.

The hook reports a test an edit adds that cannot fail: an always-true
assertion, or a Python test with no assertion. Off: `VACUOUS_TEST_GUARD_DISABLE=1`.
