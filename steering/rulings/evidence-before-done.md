# evidence-before-done

A claim of done, fixed, or passing needs what you observed this turn: run the real thing, read its output, then say what you ran and what it showed. Check a subagent's specific claim before repeating it.

First concrete step before calling a failure "pre-existing" or "unrelated": run that failing test on the base commit (for example `git worktree add /tmp/base main && (cd /tmp/base && pytest) && git worktree remove /tmp/base` or `git stash && git checkout main && pytest`). Never claim pre-existing from git diff alone; if you have not run it on main, fix it.

A skipped test, a suppression, or a stub hides the problem. Fix the cause. If one has to stay, report it with the reason and do not call the work done.

The user ran it and it failed. Look at the failure path they describe; do not explain why it should have worked. Reproduce it before changing anything.

Tests do not see a screen. After a visual change, run the real build, take a screenshot, read it, and say what you saw against what was asked. If you cannot look, report the result as unseen.
