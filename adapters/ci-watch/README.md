# ci-watch — CI/CD pipeline observability + project-contract lint

Measured problem (2026-08-30, `gh run list` sample against real warxhead1
repos): red MAINS nobody was watching. shader-garden "Test battery" failing
3 days straight, deer-flow "Frontend Unit Tests"/"Unit Tests"/"E2E Tests" red
on main, tengine "cpu-extended" red, career-ops 3 red workflows (Release
Please, CodeQL Analysis, gfi-claims), orca "Node next compatibility" red,
hearth CI showing "skipped" on main. Nothing polled this; it was discoverable
only by manually running `gh run list` per repo. This adapter closes that gap
deterministically (no LLM in the loop) and files the triage queue a separate
agent sweeps.

## Architecture

```
GitHub Actions (gh run list, per roster repo)  ->  watch.py  ->  ci.pipeline.status.v1  (via `nervous publish`)
                                                          |
                                                          +-> bd create  (auto-triage bead, deduped)
                                                          v
                                          ~/.cache/nervous-bus/ci-watch/
                                            state.json   (per repo+workflow+branch verdict, cooldown, bead id)
                                            report.md     (pipeline status table + contract lint scorecard)
```

Roster (which repos/branches/ignored workflows) is `roster.json` — data, not
code, per house convention (see `adapters/staleness/`).

## Two passes, one run, one report

### 1. Pipeline status

Per roster repo: one `gh run list -R <repo> --json workflowName,conclusion,
headBranch,headSha,updatedAt,url,databaseId --limit 20` call (one API round
trip per repo per poll, not per workflow). Runs are grouped by
`(workflowName, headBranch)` restricted to the watched branch(es) (default:
the repo's live default branch via `gh repo view`, never hardcoded to
`main`).

State classification per `conclusion`:
- `success` -> `green`
- `skipped` -> `skipped`
- empty/null (still running) -> `pending` — **never** overwrites the last
  known concluded state and **never** emits an event on its own.
- anything else (`failure`, `cancelled`, `timed_out`, `action_required`,
  `neutral`, `stale`, ...) -> `red`

`consecutive_failures` counts the leading run of `red` entries in the sampled
20-run window (newest-first), stopping at the first non-red entry. If the
*entire* sampled window is red, the persisted `red_since` from the prior poll
is kept (if earlier) so a streak longer than the window doesn't appear to
"restart" every 30 minutes once its start scrolls out of view.

Events (`ci.pipeline.status.v1`, schema `schemas/ci.pipeline.status.v1.json`)
fire **only** on:
- a state transition (`green->red`, `red->green`, first-ever-observed `red`
  for a workflow+branch pair), or
- a persistent-red reminder, at most once per 24h, while the state stays
  `red` without an intervening `green`.

No per-poll spam for a workflow that has been red for a week — the 30-minute
timer ticks 336 times before that reminder cooldown allows a second event.

### 2. Auto-triage beads

When a **default-branch** workflow is `red` for `>= 2` consecutive sampled
runs OR has stayed continuously red for `> 6h` (whichever trips first), and
no open bead already tracks it, ci-watch files exactly one:

```
bd create "CI red: <repo>/<workflow>" -t bug -p 2 -d "<run url> + last 30
  lines of the failing job log via `gh run view --log-failed`, secret-pattern
  lines redacted>"
```

Dedup: the bead id is recorded in `state.json` under that repo+workflow key.
Before filing, `bd show <id>` is checked — if still open, no refile. If the
workflow recovers to `green`, the bead id is cleared so a *future* new red
streak can file again (an old resolved bead does not permanently suppress a
new distinct incident). **This adapter only files the queue — it does not
triage or fix anything; that's a separate sweeper agent's job.**

`gh run view --log-failed` is only ever called on a *new* red transition that
crosses the bead-filing threshold, never on every poll (rate-limit
discipline).

### 3. Contract lint

Per roster repo, reading the **local checkout** under
`/home/eric/projects/<name>` (read-only — this adapter's write scope is its
own worktree + `~/.cache` + bd beads):

- Does `.github/workflows/` exist?
- Do tests actually run on push/PR to the default branch? Parses each
  workflow's `on:` trigger (PyYAML resolves a bare `on:` key as boolean
  `True` under the YAML 1.1 resolver — handled explicitly) and each job's
  `if:` condition. A workflow can *structurally* exist and still never run
  tests — e.g. `hearth/.github/workflows/ci.yml` has `push`/`pull_request`
  triggers on `main` but its single job carries `if: false` (line 49,
  hardcoded — not a path filter, not a branch mismatch). This is flagged by
  name in the scorecard notes rather than reported as an opaque "skipped".
  Path filters on `push` are also surfaced as an informational note (a
  legitimate choice, not automatically a defect) since a `paths:` list is the
  *other* common reason "tests never run on this branch" turns out true.
- Is `CLAUDE.md` or `AGENTS.md` present at the repo root?

Output: a scorecard table appended to the same `report.md`.

## Roster (`roster.json`)

```json
{
  "default_ignore_workflows": ["readme downloads badge", "dependency graph", "dependabot updates"],
  "repos": [
    { "repo": "owner/name" },
    { "repo": "owner/name2", "branches": ["main", "release"], "ignore_workflows": ["nightly build"] }
  ]
}
```

- `branches` empty/absent -> watch only the repo's live default branch.
- `ignore_workflows` (per repo) merges with `default_ignore_workflows`
  (global); both are case-insensitive substring matches against
  `workflowName`.

**Seeded with the active warxhead1 repos**: nervous-bus, hearth, deer-flow,
tengine, career-ops, orca, hearth-vault, shader-garden, hearth-loom,
nervous-autobench, claude-hook-fast, kb, market-ops, tachyonac-engine,
hearthsite. `README Downloads Badge`, `Dependency Graph`, and
`Dependabot Updates` are globally ignored (GitHub-native housekeeping
workflows, not project CI — they cycle `skipped`/`cancelled` by design and
would otherwise be permanent false-positive noise). orca's
`Hourly macOS Dev Build` is repo-scoped ignored (a scheduled build, not a
test gate). **Decision on forks/third-party clones**: checked `gh repo list warxhead1
--fork --json name` (2026-08-30) — the actual warxhead1-owned forks are
`socratic`, `Exiled-Exchange-2`, `Sidekick`, `nanoleaf-desktop`, `bitcoin`;
none run our own CI (no workflow activity beyond what a fork inherits, per
`gh run list`). Separately, `llama.cpp`, `opencode`, and `comet` under
`/home/eric/projects/` are not warxhead1 repos at all — `git remote -v` in
each shows `origin` pointing at `ggml-org/llama.cpp`, `anomalyco/opencode`,
and `g0ldyy/comet` respectively (upstream clones we don't own, checked out
locally for reference). `gh run list -R warxhead1/<name>` cannot even apply
to them. All excluded from the roster on this basis. If a fork later grows
its own CI, add it to `roster.json` explicitly — nothing here special-cases
forks structurally, it's just not seeded.

## Usage

```bash
python3 watch.py                                    # real run: poll, publish, file beads, write report
python3 watch.py --dry-run                          # compute + report, never publish or file beads
python3 watch.py --roster other-roster.json --state-file /tmp/s.json --report-file /tmp/r.md
```

Outputs:
- `~/.cache/nervous-bus/ci-watch/state.json` — per repo+workflow+branch verdict, cooldown, bead id
- `~/.cache/nervous-bus/ci-watch/report.md` — pipeline status + contract lint scorecard
- `ci.pipeline.status.v1` events on the bus
- triage beads in this repo's `.beads/` DB

## Running as a systemd user timer

```bash
mkdir -p ~/.config/systemd/user
cp systemd/ci-watch.service ~/.config/systemd/user/nervous-ci-watch.service
cp systemd/ci-watch.timer   ~/.config/systemd/user/nervous-ci-watch.timer
systemctl --user daemon-reload
# NOT enabled by this adapter — opt in explicitly:
# systemctl --user enable --now nervous-ci-watch.timer
```

## Tests

```bash
python3 test_watch.py -v
```

All `gh`/`bd`/`nervous publish` calls are stubbed via fixtures/monkeypatching
— no network, no real bd writes. One test (`test_lint_real_hearth_disabled_job`)
reads the real local hearth checkout read-only to confirm the `if: false`
detection against the actual file that motivated it; it skips gracefully if
that checkout isn't present in the running environment.

## Known limitations

- `consecutive_failures` and the fully-red-window `red_since` carry-forward
  are both bounded by the 20-run sample per poll; a workflow with very high
  run volume between two 30-minute polls could see failures interleaved with
  reruns invisible to any single poll's window. Not observed in the roster
  as seeded, but worth widening `RUN_LIMIT` if a future high-volume producer
  is added.
- Contract lint's "tests run on main" heuristic looks at trigger shape and
  job `if:` conditions, not full expression evaluation — an `if:` built from
  a more complex boolean (e.g. referencing a repo variable) is reported as
  "ok" rather than flagged, since evaluating arbitrary GitHub Actions
  expressions is out of scope for a static read. Only the literal `if: false`
  case (the one actually observed in the wild, on hearth) is called out by
  name.
