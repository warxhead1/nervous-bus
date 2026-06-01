# silo-watcher

Republish tengine silo lifecycle to nervous-bus.

## Why

tengine writes a per-run directory under `~/.tengine/sessions/silo_<NAME>_<DATE>_<TIME>[_<HEX>]/` whenever it starts a silo. The dir appears on start; `verification_report.json` appears only on clean completion. Crashed runs leave the dir without a report. Today this whole stream is invisible to nervous-bus — agents don't know a silo started, didn't finish, or finished with anomalies.

This watcher closes that loop without modifying tengine itself.

## Channels emitted

| Source | Channel | Schema |
|---|---|---|
| New `silo_*/` dir | `tengine.silo.started.v1` | `schemas/tengine.silo.started.v1.json` |
| `verification_report.json` appears in known dir | `tengine.silo.verify.v1` | `schemas/tengine.silo.verify.v1.json` |

A consumer can pair them by `session_id` (the dirname). Started without verify within window N → silo crashed mid-run.

## How it works

1. Polls `~/.tengine/sessions/` every 2s (configurable).
2. On first run, snapshots current dirs as "seen" without emitting retroactive `started` events.
3. For each new `silo_*/` dir thereafter, parses dirname → emits `started`.
4. For each known dir, checks for `verification_report.json` — emits `verify` once when it appears.
5. Persists `(seen_dirs, verified_dirs)` to `~/.cache/nervous-bus/silo-watcher-offset.json` so restarts don't replay history.

Polling beats inotify here — no extra deps, 2s lag is fine for minute-scale silo runs, works on any FS.

## Verify event payload

Subset of the full `verification_report.json` (which can be 100KB+). The event carries: silo, session_id, success, message, frames_rendered/requested, fps numbers, anomaly_count + top 5 codes, analysis_status. Full report stays on disk; consumers that want it follow `session_dir`.

## Usage

```bash
# Daemon (intended for systemd)
python3 watcher.py

# One-shot: scan once, emit pending verify events for any dirs that completed
# while the watcher wasn't running. Useful for backfills.
python3 watcher.py --once

# Custom config
python3 watcher.py --config /path/to/config.toml
```

## Running as a user service

```bash
mkdir -p ~/.config/systemd/user
cp systemd/nervous-silo-watcher.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nervous-silo-watcher.service
journalctl --user -u nervous-silo-watcher.service -f
```

## Verifying

```bash
# Trigger a fake "new silo started"
mkdir -p ~/.tengine/sessions/silo_test_20260503_120000_deadbeef

# Watch the bus
~/projects/nervous-bus/sdk/shell/probe tengine --since 10s

# Trigger a fake "verify"
echo '{"silo":"test","verification":{"success":true,"message":"OK"},"analysis":{"status":"ok","anomalies":[]},"fps":{"average_fps":60.0,"instant_fps":60.0,"min_fps":58.0,"max_fps":62.0,"is_critical":false,"is_warning":false},"frames_rendered":600,"frames_requested":600}' > ~/.tengine/sessions/silo_test_20260503_120000_deadbeef/verification_report.json

# Cleanup
rm -rf ~/.tengine/sessions/silo_test_20260503_120000_deadbeef
```

## Caveats

- Bench dirs (`bench_*`) are ignored — only `silo_*` is watched. File a follow-up if you want bench coverage.
- `started_at` is parsed from the dirname date+time and stamped UTC — but tengine writes local time in dirnames. Drift up to ~hours possible if the host isn't on UTC. Consumers should treat `started_at` as approximate; use the event's `time` (CloudEvents envelope) for precise wall-clock.
- `verification_report.json` is the only "done" signal. Crashed runs never emit `verify` — pair detection is the consumer's job.
- Polling at 2s means up to 2s lag between dir creation and the started event. Not relevant for silo timescales (minutes per run).
