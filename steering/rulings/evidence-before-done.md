# evidence-before-done

A claim of done, fixed, or passing needs what you observed this turn: run the real thing, read its output, then say what you ran and what it showed. Check a subagent's specific claim before repeating it.

"Pre-existing" is a claim to disprove: show the failure on the base commit, or treat it as yours and fix it.

A skipped test, a suppression, or a stub hides the problem. Fix the cause. If one has to stay, report it with the reason and do not call the work done.

The user ran it and it failed. Look at the failure path they describe; do not explain why it should have worked. Reproduce it before changing anything.

Tests do not see a screen. After a visual change, run the real build, take a screenshot, read it, and say what you saw against what was asked. If you cannot look, report the result as unseen.
