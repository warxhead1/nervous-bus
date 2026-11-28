---
name: neon-review
description: Adversarial review of a branch by a Neon medium agent, returned as JSON with a PASS or FAIL verdict. Use before any rapid-dev merge, and on Claude-built branches as the opposite-brain review. Works from any harness (Claude Code, Codex, cron, shell).
---

# neon-review

Run `neon-review` from `adapters/neon-review/`. JSON on stdout, errors on stderr, `neon-review --help` is the reference.

```sh
neon-review --repo <dir> --base origin/main [--spec <file>] [--depth medium|ub] [--model M] [--timeout S] [--out FILE]
```

A branch in rapid development merges once this returns `"verdict": "PASS"`. It builds one prompt from `rubric.md` and names the diff, diff stat, commit log and spec as files, so large diffs work. It runs `agy -p` inside the repo.

## When to run it

- Before every rapid-dev merge, after the author's own tests are green.
- On Claude-built branches, so a different model family reviews them.
- Use `--depth ub` for branches that change logic, state or security. The tool runs `ub-gate crap` and `ub-gate dry` and hands their JSON to the reviewer, which then runs `ub-gate mutate` on the two riskiest files. It needs `ub-gate` installed and costs several minutes more.
- Pass `--spec` with the bead, issue or brief so each of its items becomes a spec row.

## Reading the result

| Field | Meaning |
|---|---|
| `verdict` | `PASS`, `FAIL`, `unparsed` (no final `VERDICT:` line), or `timeout` |
| `findings[]` | `severity` (BLOCKER, HIGH, MEDIUM, NIT), `claim`, `where` (file:line), `fix` |
| `spec_rows[]` | `row`, `status` (landed, not landed, landed differently), `detail` |
| `raw_path` | the full review text, kept outside the checkout |
| `gates` | at `ub` depth, the crap and dry digests the reviewer received |

Exit codes: `0` PASS, `1` FAIL, `unparsed` or `timeout`, `2` usage, `3` agy (or `ub-gate` at `ub` depth) unreachable.

`unparsed` and `timeout` are never a pass. Read `raw_path`, then rerun. A reviewer PASS that lists any BLOCKER or HIGH finding is reported as FAIL with a `note`.

## The loop

1. Run `neon-review` on the branch.
2. On a non-zero exit with `findings`, send every BLOCKER and HIGH finding (claim, `where`, `fix`) back to the author lane. MEDIUM findings go back too unless there is a stated reason to defer.
3. The author fixes on the branch and adds the test each finding names.
4. Rerun. Merge only on exit `0`.

A finding that cannot be shown is a question: the reviewer lists it as a question, and the author answers it in the next commit body. Do not edit the rubric to make a branch pass.

## Notes

- The reviewer edits nothing on the branch. Gate output (`.metrics/`, `target/`) that this run creates is removed afterwards.
- Raw text and input files go to `NEON_REVIEW_SCRATCH`, else `$XDG_CACHE_HOME/neon-review`, else `~/.cache/neon-review`. A scratch dir inside the repo is refused.
- Uncommitted changes are not reviewed: the diff is `<base>...HEAD`. Commit first.
- Keep specs free of private paths and secrets when the repo is public.
