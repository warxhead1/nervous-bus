# reflex-recorder

Reflexarc FLYWHEEL (b2): captures `bus.agent.activity.v1` events from `nbus:all`,
segments them into runs using the hardened composite-key model, persists to SQLite,
and emits `bus.agent.run.closed.v1` on each run close.

## Segmentation model

**run_key_kind=session**: host main-tree work, no worktree context.
`run_key = conversation_id`

**run_key_kind=worktree**: dispatched subagent/workflow shard.
`run_key = conversation_id + '#' + worktree_slug`

Key insight: `conversation_id` alone is NOT a valid run key. One conversation_id
was observed spanning 26 parallel worktrees (shards all inherit the parent host
session ULID into `agent_id`/`session_id`/`conversation_id`). The worktree slug
is the only field in the live stream that separates parallel shards.

The `worktree` field in the output is the **reconstructed absolute path** (derived
from the activity `cwd` field), never the raw slug. The downstream worktree-leak
detector joins against `git worktree list` (absolute paths).

## Querying

`query.py` is the read-only query layer over the SQLite run-store.  It exposes a
`reflex` CLI and is importable as a library.  All queries open the DB in
`mode=ro` (no write locks, WAL-safe).

### Subcommands

```
reflex runs        [--project P] [--outcome O|unlabeled] [--since TS] [--days N] [--json]
reflex thrash      [--project P] [--since TS] [--days N] [--json]
reflex prevalence  [--project P] [--days N] [--json]
reflex issues      [--project P] [--since TS] [--days N] [--json]
reflex stats       [--project P] [--since TS] [--days N] [--json]
reflex sql  "<SELECT ...>"  [--json]
reflex schema      [--json]
```

### Copy-paste examples

```bash
# All runs for tengine (default 50 most recent)
python3 query.py runs --project tengine

# Work that reached a clean outcome in the last 7 days
python3 query.py runs --project tengine --outcome clean --days 7 --json

# Work that was discarded (abandoned or reverted) this week
python3 query.py thrash --project hearth-loom --days 7

# Detector hit prevalence across all projects (last 7 days)
python3 query.py prevalence

# Per-project stats: run count, outcome breakdown, avg events, read/write ratio
python3 query.py stats --json

# Recurring issues per project
python3 query.py issues --project tachyonac-engine

# Worktree-leak prevalence for tachyonac-engine
python3 query.py prevalence --project tachyonac-engine --days 30
```

### Agent recipes

`reflex` is the **generic read-only behavioral-memory handle** for autonomous
agents and tools.  Shell out to `python3 .../query.py` to compose questions
about past agent behavior without reading store.py.

```bash
# 1. Self-describe the schema before composing a custom query
python3 query.py schema --json

# 2. Arbitrary SELECT — get the tool distribution for long runs
python3 query.py sql "SELECT project, tool_histogram, event_count FROM runs WHERE event_count > 50 ORDER BY event_count DESC" --json

# 3. Find worktree runs that ended cleanly (good candidates for leak audit)
python3 query.py sql "SELECT run_id, project, worktree_slug, git_branch FROM runs WHERE labeled_at IS NOT NULL AND outcome = 'clean' AND worktree_slug IS NOT NULL" --json

# 4. Count unlabeled runs per project (NOT-YET-LABELED; outcome NULL != clean)
python3 query.py sql "SELECT project, COUNT(*) as n FROM runs WHERE labeled_at IS NULL GROUP BY project" --json
```

**Null-vs-clean contract** (critical for agent self-composition): `outcome IS NULL`
means NOT-YET-LABELED, never "clean".  Any query filtering on `outcome` must
also gate on `labeled_at IS NOT NULL` — the `reflex sql` guard does NOT enforce
this for you.  Use `reflex schema` to get the full column semantics, including
the labeled_at caveat, before writing a custom query.

## File layout

```
adapters/reflex-recorder/
  recorder.py          # Main entrypoint — XREADGROUP consumer, shutdown handler
  segment.py           # Segmentation logic: run_key computation, OpenRun, Segmenter
  store.py             # SQLite persistence (abstracted for future dolt swap-in)
  query.py             # CLI/library query layer (reflex subcommands + sql passthrough)
  reflex-recorder.toml # Configuration
  systemd/
    reflex-recorder.service  # User systemd unit (do not enable without orchestrator review)
  tests/
    test_segment.py    # 28 unit tests for segmentation logic
    test_query.py      # 69 unit tests for query layer
```

## Running

```bash
# Continuous (live mode)
python3 recorder.py

# Drain current stream entries and exit
python3 recorder.py --once

# Offline replay from debug.jsonl
python3 recorder.py --replay ~/.cache/nervous-bus/debug.jsonl
```

## Storage

Default DB: `~/.cache/nervous-bus/reflex/runs.db`

Tables:
- `runs` — one row per closed run (the bus.agent.run.closed.v1 payload)
- `run_events` — ordered raw activity events per run_id (for b3 feature/label backfill)

## Enable systemd service (after orchestrator review)

```bash
cp adapters/reflex-recorder/systemd/reflex-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reflex-recorder.service
systemctl --user status reflex-recorder.service
```

## Run close triggers

1. Explicit `ended` event from the activity stream
2. Idle timeout (default 15 min, configurable) with no new events on the run_key
3. Graceful recorder shutdown (`close_reason=recorder_shutdown`)

Idle-split runs set `continues_run_id` to the prior run_id so fragments can be
re-stitched downstream.
