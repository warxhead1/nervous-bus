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

## File layout

```
adapters/reflex-recorder/
  recorder.py          # Main entrypoint — XREADGROUP consumer, shutdown handler
  segment.py           # Segmentation logic: run_key computation, OpenRun, Segmenter
  store.py             # SQLite persistence (abstracted for future dolt swap-in)
  reflex-recorder.toml # Configuration
  systemd/
    reflex-recorder.service  # User systemd unit (do not enable without orchestrator review)
  tests/
    test_segment.py    # 28 unit tests for segmentation logic
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
