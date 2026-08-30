# board — the total board across every project

Federates 10 project beads DBs (`app_to_market`, `biz_worthy`, `deer_flow`,
`hearth`, `` `hearth-loom` ``, `nervous_bus`, `sweepers_adventures`,
`temple_stuart_accounting`, `tengine`, `unreal_battlebots_gamedev` — all on one
dolt SQL server, `127.0.0.1:39502`, data_dir `/home/eric/.beads/dolt`) with
live orca dispatch state and hearth-loom PR lifecycle into one JSON contract
and one human kanban. Motivation: `bd ready` only ever sees one project's DB,
and neither bd nor orca is aware the other exists — a bead can look "ready" in
beads while an orca worker is already grinding on it, or "in_progress" while
its worker died hours ago with no lifecycle event to say so. This closes that
gap in one place.

## Architecture

```
beads_global.all_issues / all_dependencies  (federated SQL views, create_view.sql)
      │  pymysql over dolt's MySQL-protocol port
      ▼
orca orchestration.db (sqlite, mode=ro)  ──┐
      │ tasks.spec bead extraction         │
      ▼                                    │
Redis nbus:* streams (orca lifecycle,      │
  hearth-loom PR + generic lifecycle)  ────┤
                                            ▼
                                       board.py
                                            │
                    ┌───────────────────────┴───────────────────────┐
                    ▼                                                ▼
   ~/.cache/nervous-bus/board/board.json          ~/.cache/nervous-bus/board/report.md
   (FROZEN machine contract)                       (human kanban, one section per lane)
```

## Layer 1 — the SQL view (`create_view.sql`)

`CREATE OR REPLACE VIEW beads_global.all_issues` UNIONs the `issues` table of
all 10 project DBs, adding a literal `project` column mapped to the
repo-facing kebab-case name (`nervous_bus` → `nervous-bus`, `` `hearth-loom` ``
stays `hearth-loom`, etc). `beads_global` itself is excluded — it is the
federation/routing DB, not a project with its own work queue.

A companion `beads_global.all_dependencies` view UNIONs every project's
`dependencies` table. It exists because each project DB's own `blocked_issues`
view only exposes a `blocked_by_count`, not the actual blocking ids — board.py
needs the raw edges to populate `blocked_by` in the contract.

Apply (idempotent, safe to re-run):

```bash
dolt --data-dir /home/eric/.beads/dolt sql < create_view.sql
```

Verify:

```bash
dolt --data-dir /home/eric/.beads/dolt sql -q \
  "SELECT project, status, count(*) FROM beads_global.all_issues GROUP BY project, status" -r csv
```

## Layer 2 — the aggregator (`board.py`)

Each run:

1. Queries `beads_global.all_issues` + `all_dependencies` over dolt's
   MySQL-protocol port via `pymysql` (one round trip, not N `dolt sql -q`
   subprocess spawns).
2. Opens orca's `orchestration.db` **read-only** (`mode=ro` — board.py never
   writes orca state). Extracts a bead reference from each `tasks.spec`/
   `task_title` using the exact same regex convention as orca's own
   `src/main/runtime/orchestration/nbus-emit.ts` `extractBeadId()`
   (`[bead:<id>]` tag, else loose `bead <id>` prose), then joins to that
   task's most recent `dispatch_contexts` row for state + heartbeat.
3. Samples the last ~200 entries of orca's worker-lifecycle stream
   (`nbus:orca.worker.lifecycle.v1`) and hearth-loom's PR + generic lifecycle
   streams via `XREVRANGE`. PR-event stream naming is **not** consistent
   between the checked-in schema files and what has actually been observed
   live (see comments in `board.py` — `PR_STREAMS`); all known variants are
   read, none assumed canonical.
4. Derives a lane per issue (precedence `in_flight > in_review > blocked >
   in_progress > ready`, `done_7d` handled first since closed excludes
   everything else) and a `score` (`priority_weight * log2(2 + age_days) *
   1.5-if-blocked`), then writes both outputs atomically (tmp + rename).

### Output contract (`board.json`) — FROZEN

Two sibling lanes code against this shape. Only additive optional fields may
be introduced; existing keys/types never change:

```json
{
  "generated_at": "2026-08-30T18:00:00Z",
  "lanes": ["ready", "in_progress", "in_flight", "in_review", "blocked", "done_7d"],
  "issues": [{
    "id": "...", "project": "...", "title": "...", "status": "...", "lane": "...",
    "priority": 2, "issue_type": "...", "assignee": null,
    "created_at": "...", "updated_at": "...", "age_days": 3.2, "score": 12.4,
    "blocked_by": [], "orca": null, "pr": null
  }],
  "summary": {"per_project": {"nervous-bus": {"ready": 3}}, "per_lane": {"ready": 10}}
}
```

## Usage

```bash
python3 board.py                                              # real run
python3 board.py --board-file /tmp/b.json --report-file /tmp/r.md
```

Env overrides: `NERVOUS_BOARD_DOLT_HOST/PORT/USER/DB`,
`NERVOUS_BOARD_ORCA_DB`, `NERVOUS_REDIS_URL`, `NERVOUS_BOARD_CACHE`.

## Running as a systemd user timer

```bash
mkdir -p ~/.config/systemd/user
cp systemd/board.service ~/.config/systemd/user/nervous-board.service
cp systemd/board.timer   ~/.config/systemd/user/nervous-board.timer
systemctl --user daemon-reload
# NOT enabled by this adapter — opt in explicitly:
# systemctl --user enable --now nervous-board.timer
```

## Tests

```bash
python3 test_board.py -v
```

All external I/O (dolt/pymysql, orca sqlite, redis) is stubbed with fixture
data or exercised against a throwaway temp sqlite file — never a live dolt
server or real orchestration.db. Covers bead-id extraction (mirrors orca's
regex exactly), lane-precedence derivation, scoring, the orca sqlite join
(including "latest dispatch context wins" when a bead is referenced by more
than one task), PR-event "most recent across naming variants wins", and the
`board.json` contract shape (exact top-level + issue key sets).

## Known limitations (exhaustion notes)

- **`blocked_by` join is simplified relative to each project DB's own
  `blocked_issues` view.** That view excludes blockers whose status is in a
  per-project `custom_statuses` category of `done`/`frozen`, in addition to
  `closed`. `all_dependencies`/`all_issues` here use `blocker.status !=
  'closed'` only, since `custom_statuses` is not uniformly populated across
  all 10 DBs and resolving it generically would require a per-DB special
  case this adapter does not currently have data to justify. Read-only
  surfaces checked: `beads_global.custom_statuses` (present, categories
  columns exist) but not cross-checked row-by-row against every project DB's
  actual custom status set within this dispatch's time budget. Practical
  effect: a bead blocked only by a `frozen`-category blocker would show as
  blocked here when the project's own `bd ready` would already consider it
  unblocked — false-blocked, not false-ready, so it errs toward
  under-promising availability, not over-promising it.
- **`nbus:orca.worker.lifecycle.v1` was empty at authoring time** (0 events,
  despite a live schema at `schemas/orca.worker.lifecycle.v1.json` describing
  it) — orca is not currently emitting to it. board.py still queries it
  (an empty/nonexistent stream XREVRANGEs to `[]`, not an error) since this is
  a live-system emission gap on orca's side, not a reason for board.py to stop
  reading the source it's documented to use.
