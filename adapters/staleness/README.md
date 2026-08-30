# staleness — watch the watchers

Detects nervous-bus channels that have gone silent: a producer that used to
publish regularly and then simply stopped, without crashing or logging
anything. Measured motivation: deer-flow bead-enrichment feedback
(`deer-flow.bead.enrichment.complete`) went dark 2026-05-14 and nobody
noticed for weeks; several tengine CI emitters have no systemd unit backing
them and may be dead too. Nothing in the bus previously alerted on absence —
only on malformed events (`bus.dead_letter`). This adapter closes that gap.

## Architecture

```
Redis Streams (nbus:<channel>)  →  monitor.py  →  bus.channel.stale.v1  (via `nervous publish`)
                                          ↓
                                 ~/.cache/nervous-bus/staleness/
                                   baselines.json  (per-channel cadence + cooldown state)
                                   report.md        (human-readable standing report)
```

monitor.py is a **pure consumer** — it reads existing `nbus:*` streams via
`SCAN`/`XREVRANGE`/`XLEN`. It never touches debug.jsonl and never mutates any
stream except by publishing its own `bus.channel.stale.v1` alert.

## Algorithm — no hardcoded thresholds

A single global "alert after N minutes of silence" threshold is wrong: a
channel that fires every few seconds (e.g. `tengine.session.frame.v1`) and one
that fires twice a day (e.g. `deer-flow.council.started`) have wildly
different definitions of "quiet." Instead, each run:

1. Enumerates `nbus:*` streams (excluding `nbus:all` and `nbus:dedup:*`,
   which are fan-in/bookkeeping keys, not per-channel cadence).
2. Samples the last `sample_size` (default 50) entries per stream via
   `XREVRANGE ... COUNT N` — cheap regardless of total stream length, since
   Redis Stream IDs already encode a millisecond timestamp (`<ms>-<seq>`), so
   no extra timestamp field needs to be read out of the payload.
3. Channels with fewer than `SPARSE_MIN_EVENTS` (5) lifetime events
   (`XLEN`) are **sparse**: there isn't enough history to know what a normal
   gap even looks like, so they are reported separately and never alerted on.
   `deer-flow.bead.enrichment.complete` currently has 4 lifetime events and
   falls in exactly this bucket — see "Known limitation" below.
4. For the rest, compute inter-event gaps between consecutive sampled
   timestamps and take the **P95**. The alert threshold is
   `max(p95_gap * multiplier, floor_seconds)`:
   - `multiplier` defaults to **3.0** — P95 by definition means ~1 in 20
     historical gaps already exceeded it, so alerting at 1x P95 would
     false-positive on ~5% of healthy runs. 3x pushes past that tail for the
     bursty/geometric inter-arrival shape actually observed on these
     channels (confirmed against the real `nbus:*` population — see the
     measured run below).
   - `floor_seconds` defaults to **1800** (30 min) — protects a channel whose
     historical P95 happens to be tiny (e.g. a burst of heartbeats a second
     apart) from getting an unreasonably twitchy threshold; also comfortably
     above the timer's own 15-minute cadence, so at least two ticks of
     continued silence are needed before the shortest possible threshold
     could fire.
   Both are CLI-overridable (`--multiplier`, `--floor-seconds`), not baked in.
5. If observed silence (`now - last_event_at`) exceeds the threshold, the
   channel is **stale**: `bus.channel.stale.v1` is published (rate-limited by
   a 6h cooldown, `--alert-cooldown-s`, persisted per-channel in
   `baselines.json` so it survives restarts) and the channel is highlighted
   in `report.md`.
6. `baselines.json` is updated for every evaluated channel (not just stale
   ones) each run, so runs are incremental — no full-history re-scan, and the
   cooldown state persists across the 15-minute systemd timer ticks.

## Usage

```bash
python3 monitor.py                              # real run: evaluate, publish stale alerts, write report
python3 monitor.py --dry-run                    # compute + write report, never call `nervous publish`
python3 monitor.py --pattern 'nbus:test.*'      # scope to a subset (used by tests)
python3 monitor.py --multiplier 4 --floor-seconds 3600
```

Outputs:
- `~/.cache/nervous-bus/staleness/baselines.json` — per-channel cadence + last-alert state
- `~/.cache/nervous-bus/staleness/report.md` — human-readable standing report (stale / ok / sparse tables)
- `bus.channel.stale.v1` events on the bus (schema: `schemas/bus.channel.stale.v1.json`)

## Running as a systemd user timer

```bash
mkdir -p ~/.config/systemd/user
cp systemd/staleness.service ~/.config/systemd/user/nervous-staleness.service
cp systemd/staleness.timer   ~/.config/systemd/user/nervous-staleness.timer
systemctl --user daemon-reload
# NOT enabled by this adapter — opt in explicitly:
# systemctl --user enable --now nervous-staleness.timer
```

Inspect: `systemctl --user status nervous-staleness.timer`, `journalctl --user -u nervous-staleness.service -f`.

## Tests

```bash
python3 test_monitor.py -v
```

Runs against a dedicated `nbus:test.staleness.*` namespace on the real Redis
(`NERVOUS_REDIS_URL` overridable), cleaning up before and after. Covers:
sparse-channel suppression, a healthy regular channel, a channel that went
silent, the floor protecting a bursty-then-normal channel from a
multiplier-only false positive, an end-to-end `run()` (report + baseline
files), and alert-cooldown suppression of repeat publishes.

## Known limitation: the sparse floor vs. the motivating incident

`deer-flow.bead.enrichment.complete` — the channel whose real 2026-05-14 death
motivated this adapter — currently has only 4 lifetime events in the stream
(below `SPARSE_MIN_EVENTS=5`), so it is classified **sparse**, not stale, by
this run. This is not a bug: with 4 events there is no defensible cadence to
compute a P95 gap from, and alerting on "a channel with almost no history
also has no recent history" would be indistinguishable from alerting on any
channel that was always supposed to be low-volume. The honest fix is
structural, not a lower threshold: `SPARSE_MIN_EVENTS` bounds what THIS
detector can conclude from gap statistics alone. A follow-up worth filing:
pair this gap-distribution detector with a **registry-driven** check for
channels an owner has explicitly flagged as "must publish at least once every
X" regardless of history depth (would need a small annotation file or schema
extension — out of scope here since it requires owner input this adapter
can't derive from Redis alone).
