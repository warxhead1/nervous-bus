# reflex-recorder

Reflexarc FLYWHEEL (b2): captures `bus.agent.activity.v1` events from `nbus:all`,
segments them into runs using the hardened composite-key model, persists to SQLite,
and emits `bus.agent.run.closed.v1` on each run close.

## Engine vs. adapters (the public/private split)

This directory is the **generic Reflexarc engine** and is PUBLIC. It ships:
- the pipeline (`recorder`/`segment`/`enrich`/`store`/`query`),
- outcome attribution (`label`/`git_outcome`),
- **generic** detectors (`worktree_leak`, `reread_same_file`, `repeated_question`,
  `file_reads_to_finding`) and **rust-ecosystem** detectors (`rebuild_cache_miss`,
  `edit_build_fail_revert` — they key on `cargo` alone, not any one project),
- **orchestration-quality** detectors (`red_baseline_dispatch`,
  `unverified_completion`, `directive_ground_truth_mismatch`,
  `inherited_rationalization`) built on the dispatch-lineage substrate
  (`detectors/dispatch_lineage.py`): they reason about how runs were *spawned* —
  fan-outs launched on a red/unestablished baseline (A1), a dispatch prompt
  asserting a clean baseline that reality contradicts (A2), delegated agents that
  shipped code edits with no build/test (grounded MAST "no-verification-step", F1),
  and sibling cohorts that converged on a seeded bad outcome (C1, session-scoped
  via the cohort→child-outcome join). The substrate is truncation-tolerant
  (bounded summaries are regex-recovered) and joins lineage at SESSION scope
  (delegated agents are folded into the parent session, split across idle runs).
  See the spec in the kb vault (`reflexarc-orchestration-detectors.md`),
- the inductive trajectory profiler (`tier2/trajectory_profile.py`),
- the **project-adapter contract** (`adapter_api.py`) + a scaffold
  (`templates/reflex-adapter/`).

Anything specific to ONE project — its build/run command taxonomy, bespoke
detectors, extra cost signals (build reports, GPU diagnostics) — is PRIVATE and
lives in the overlay at `$NERVOUS_HOME/adapters/reflex-<project>/adapter.py`
(default `~/.config/nervous-bus/adapters/`). The engine discovers them at runtime
via `adapter_api.load_adapters()`; with no overlay it runs generic-only. See
`templates/reflex-adapter/README.md` to build one. Reference: `reflex-tengine`.

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

## Transcript snapshotter

`transcript_snapshot.py` incrementally mirrors Claude Code's per-session
`*.jsonl` transcripts from `~/.claude/projects/` into a durable cache at
`~/.cache/nervous-bus/reflex/transcripts/`. Worktree dirs (munged names
containing the literal substring `worktrees`) are processed first because
Claude Code deletes them whole when the worktree is reaped, losing the
delegated agent's full transcript.

The mirror is *append-only with rotation recovery* — a 2-line src file is
appended to byte-for-byte; a rotation (inode change or shrink) is detected
and re-copied; the destination is *never* shrunk or pruned (a vanished src
file is a normal event, not an error and not a deletion in dst).

```bash
# One pass (used by the systemd timer):
python3 adapters/reflex-recorder/transcript_snapshot.py --once

# Manifest summary (mirrored files + total bytes):
python3 adapters/reflex-recorder/transcript_snapshot.py --stats

# Poll loop (foreground; the timer unit replaces this in production):
python3 adapters/reflex-recorder/transcript_snapshot.py --watch 60
```

State on disk:

- `<dst>/<munged-dir>/<sessionid>.jsonl` — byte-for-byte mirror of the src file.
- `<dst>/.manifest.json` — `relpath -> {"inode": int, "size": int}`; the
  incremental copy baseline. Atomic write (temp file + `os.replace`).

Enable the systemd user timer (every 2 min, catches up after sleep):

```bash
cp adapters/reflex-recorder/systemd/nervous-transcript-snapshot.{service,timer} \
    ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nervous-transcript-snapshot.timer
systemctl --user list-timers nervous-transcript-snapshot.timer
journalctl --user -u nervous-transcript-snapshot.service -n 50
```

Stdlib only (no `pip`), no network. The destination is a durable archive
that retains content even after the worktree is reaped.

## Struggle Ledger

`struggle_ledger.py` answers a different question than the quality detectors: not
"did the agent verify its work?" but **"what are the agents fighting, is it shared,
and has it been fixed?"** — the lived friction the verified-% scorecards throw away.

It reads the durable transcript archive (mirrored by `transcript_snapshot.py`),
classifies records against `StruggleClass` patterns (generic friction shipped by the
engine — cargo build-lock, address-in-use, OOM — plus project-specific ones a private
adapter contributes via `ProjectAdapter.struggle_classes()`, so proprietary tool names
stay private), and for each struggle builds a longitudinal record:

- events, distinct sessions/agents, first/last seen, daily sparkline
- **status** — `open` (still happening) / `dormant` / `resolved`
- **fix verdict** — correlates the friction curve against remediation events
  (fix-commits, bead closes) to score whether a claimed fix actually dropped it:
  `fixed` / `partial_still_open` / `unfixed_open` / `unfixed_no_attempt` /
  `resolved_no_fix_found`. Picks the remediation that best explains the decline
  (largest before→after drop), not merely the latest.

```bash
python3 struggle_ledger.py                      # whole-fleet ledger
python3 struggle_ledger.py --project tengine     # one project
python3 struggle_ledger.py --struggle gpu_lock_wait --project tengine  # drill-in: timeline + examples + fix
python3 struggle_ledger.py --json                # machine-readable
```

The fix-correlation is an honest heuristic (keyword-matched remediations, ±7-day
before/after windows, edge-truncation flagged) — a starting signal for "is this wall
still standing", not a proof.

## Nightly Analysis Loop

Capture (`recorder.py`) has run as a systemd service since the engine's first bead.
Outcome labeling (`label.py`), the 12 detectors (`synthesis.py`), and the struggle
ledger (`struggle_ledger.py`) were code-complete but only ever invoked BY HAND —
a three-quarter loop: capture shipped without a schedule to consume it. The
ecosystem charter's rule is that nothing ships as "done" until it closes the full
capture → analyze → surface → consume loop; `nightly_analysis.py` + its timer is
what closes the analyze/surface half for reflex-recorder.

`nightly_analysis.py` runs three steps in order, each subprocess-wrapped with a
hard per-step timeout (default 300s) and a logged-but-non-fatal failure — a slow
or broken step never blocks the ones after it, and a bad night never wedges the
timer:

1. `label.py --since-days 30 --unlabeled-only` — incremental outcome labeling.
   `--unlabeled-only` means a settled outcome (landed/reverted/abandoned/clean/
   thrashed) is never re-verified; only runs still sitting at `outcome=NULL` get
   another attempt (their PR may have merged, their bead may have closed since).
   `--since-days` bounds cost against an ever-growing run history. A first pass
   against a mostly-unlabeled DB may not finish inside the timeout — that's fine,
   `apply_label` autocommits per-row, so the next night resumes where this one
   left off.
2. `synthesis.py` (default dry-run) — runs every built-in + private-overlay
   detector and persists hits/issues to `detector_hits`/`issues`. Dry-run only
   gates the `nervous publish` calls; the local persistence that `reflex
   prevalence`/`reflex issues` read happens either way.
3. `struggle_ledger.py --days 14 --json` — captured (not persisted, it has no
   store of its own) to feed the digest below.

Then it writes/updates a rolling digest at
`~/knowledge/indexed/shared/reflex-digest.md` (kb vault; a fixed id in the
frontmatter means each run rewrites the same entry rather than forking a new
file) with: run counts + labeled-outcome distribution (7d), top detector
prevalence (7d), and top OPEN struggle-ledger items (14d). The digest is a
rolling log, not a decision record — this script does **not** git-commit it;
commit a snapshot by hand if you want history of one.

```bash
# Manual run (same as the timer):
python3 nightly_analysis.py

# Custom DB / windows / timeout:
python3 nightly_analysis.py --db /path/to/runs.db --step-timeout 300 \
    --label-since-days 30 --window-days 7 --struggle-window-days 14
```

Enable the systemd user timer (daily, ~05:30 local, catches up after sleep):

```bash
cp adapters/reflex-recorder/systemd/reflex-analysis.{service,timer} \
    ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reflex-analysis.timer
systemctl --user list-timers reflex-analysis.timer
journalctl --user -u reflex-analysis.service -n 100
```
