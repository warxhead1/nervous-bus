<!--
brief-template.md — filled by dispatch.sh (envsubst-style ${VAR} substitution)
into <worktree>/.maintenance-brief.md before the mmx worker is launched.
Placeholders: ${BEAD_ID} ${BEAD_TITLE} ${BEAD_BODY} ${REPO} ${REPO_BRANCH}
${BUILD_CMD} ${TEST_CMD} ${REPORT_PATH}
-->
# Maintenance task: ${BEAD_ID} — ${BEAD_TITLE}

You are a sandboxed maintenance worker (MiniMax-backed, bwrap-isolated, no
credentials, no host network localhost). This worktree is a disposable branch
(`${REPO_BRANCH}`) of `${REPO}` cut on the HOST side — you do not need to
create your own worktree or branch. Work directly in this directory.

## Bead body (verbatim)

${BEAD_BODY}

## Machine-readable acceptance criteria

1. The specific issue named in the bead title/body is fixed — not a
   surrounding refactor, not an unrelated cleanup.
2. `${BUILD_CMD}` exits 0 in this worktree after your change.
3. `${TEST_CMD}` exits 0 in this worktree after your change.
4. Your changes are committed to this branch with descriptive commit
   message(s). Do **NOT** attempt to push — you have no git credentials
   inside this sandbox by design; the host pushes and opens the PR after
   independently re-running the build/test commands above.
5. If the bead is unfixable as scoped (missing context, flaky external
   dependency, requires a decision only Eric can make), do NOT fake a fix.
   Commit nothing, and write that finding to the completion report instead.

## Exhaustion clause

Before reporting the acceptance criteria unmet or the bead unfixable:
enumerate every file/command you actually checked, name what you did NOT
check, and state the gap as a property of YOUR investigation, not a
property of the codebase. "I grepped X and Y, did not check Z" is fundable;
"this is not fixable" alone is not.

## Completion report (REQUIRED, exact path)

Write your final report to exactly this absolute path, creating parent dirs
if needed: `${REPORT_PATH}`

The report must contain, in order:
1. One-paragraph summary of what you changed and why.
2. `git log --oneline -n 20` output for this branch (or "NO COMMITS" plus
   the exhaustion-clause explanation if you made none).
3. The exact output of `${BUILD_CMD}` and `${TEST_CMD}` as YOU ran them here
   (the host re-verifies independently and does not trust this section
   alone — include it anyway so a human reviewing the PR sees your evidence).
4. Anything discovered that is out of scope for this bead but worth a
   follow-up bead, with file:line citations.

The supervising process on the host unblocks ONLY when this exact file
exists and is non-empty. Any other filename strands this run.
