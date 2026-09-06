# Ecosystem daily evidence

`tools/ecosystem_daily_report.py` produces one JSON aggregate describing what
the ecosystem actually did in a UTC window. It is stdlib-only and strictly
read-only: it reads rotated journal files and opens the recorder database with
`mode=ro` + `PRAGMA query_only`. It never publishes, never writes to the
recorder, never restarts anything, and never changes a schema.

```bash
python3 tools/ecosystem_daily_report.py --hours 24
python3 tools/ecosystem_daily_report.py \
    --since 2026-09-05T01:25:55Z --until 2026-09-06T01:25:55Z \
    --output ~/.cache/nervous-bus/daily/2026-09-05.json
```

## Inputs

| Source | Path | Role |
| --- | --- | --- |
| Debug journal | `~/.cache/nervous-bus/debug.jsonl` + `.N.gz` | every producer writes here first; the widest surface |
| Recorder store | `~/.cache/nervous-bus/reflex/runs.db` | segment denominator, run identity coverage, `run_evals` ledger |
| Attempt ledger | `--ledger <file>.jsonl` (optional) | summarised through the **existing** `tools/memory_evaluation.py` v1 contract |

Rotations are discovered automatically and read oldest-first (`debug.jsonl.3.gz`
→ `.2.gz` → `.1.gz` → `debug.jsonl`), so a window spanning a rotation is one
continuous read.

## What the report guarantees

**Every rate names its denominator.** Journal *records*, recorder *segments*
and Orca *tasks* are three different populations counted three different ways.
`denominators` states all three with a `basis` string, and every coverage block
carries its own `denominator` and `denominator_basis`. There are no bare
percentages in the output.

**Unknown is never zero.** Usage metrics are accumulated into
`{known_sum, known_count, zero_count, unknown_count, invalid_count}`. A producer
that does not report tokens increments `unknown_count`; only a producer that
reported the number `0` increments `zero_count`. Identity fields are reported as
`present`/`missing`, never defaulted.

**Deduplication is exact, and collisions are never silent.** Two records are
collapsed only when the whole envelope is byte-identical after canonical
JSON serialisation (key order does not matter). The same `id` carrying a
*different* payload is an **identity collision**: both records are counted, and
the collision is reported in `records.id_collisions_kept` with a bounded sample.

This is not defensive theatre. Measured over the retained 24h journal on
2026-09-05: 201,884 raw records, 132,484 unique, 69,400 exact duplicates — and
**33 genuine id collisions**. They are all `agent.session.v1` compact events
whose envelope id is a content hash that excludes the timestamp, so two real
events six minutes apart share one id. Deduplicating on `id` alone deletes them
silently: an id-only count of the same window returns 132,451 distinct ids
against 132,484 real records, and nothing in that number says 33 events went
missing.

**Resources are bounded, and every bound that bites is flagged.** Files stream
line by line. Group-by cardinality is capped (`--max-cardinality`, default 500)
with overflow folded into `<other>` and `truncated: true`. The dedup table is
capped (`--max-tracked-ids`, default 500,000) and sets `id_tracking_truncated`.
Only a `(digest, type, time)` fingerprint is kept per id — never the payload.
Samples are capped (`--max-samples`, default 20) while the underlying *counts*
stay complete.

**UTC or nothing.** A `time` without an explicit offset is refused, not guessed
against the host's local zone. Malformed and missing timestamps land in their
own `parse` buckets with bounded samples; they never abort the run and never
enter a count. The window is half-open `[start, end)` so consecutive daily runs
neither overlap nor gap.

**Project and worktree are never merged.** `by_project` counts logical
projects; `by_worktree_slug` counts filesystem checkouts. A slug seen under more
than one project is listed in
`project_worktree_distinction.worktree_slugs_seen_under_multiple_projects`
rather than being silently treated as one thing.

**No fabricated acceptance.** The report carries
`evidence_level: "observed_telemetry"` and
`verified_acceptance: "NOT_EVALUATED"`. A worker reporting `outcome: succeeded`
is recorded as a *reported* outcome. Nothing in this tool verifies that any work
was accepted, and `limitations` says so in the output itself.

## Trace context: what is actually there

`journal.trace` reports `traceparent` exactly as observed. Over the measured 24h
window: **zero** envelopes carried one, and `producer_status` reads
`no_producer_observed`.

The mechanism is not missing — `sdk/shell/nervous` stamps the CloudEvents
`traceparent` extension whenever `NERVOUS_TRACEPARENT` is set and validates it
as W3C v00, and `nervous trace <trace-id>` reads the causal chain back out
across rotations. What is missing is a **root span**: nothing on this host sets
`NERVOUS_TRACEPARENT`. The only occurrences in the tree are the SDK's own
implementation and one research doc.

This tool therefore reports the gap and stops. It does **not** mint a trace id
per event. A fresh random id shared by no other event correlates nothing while
looking exactly like correlation, which is worse than an honest zero. Creating
root spans at dispatch and propagating them into producer environments is
separately scoped producer work.

## Derived identity correlation (observational only)

`identity_correlation` joins recorder segments to `orca.worker.lifecycle.v1`
identity by exact absolute worktree path. A slug alone is insufficient. It is labelled
`authority: "observational_join_only"` and reports `matched_unique`,
`matched_ambiguous` and `unmatched` separately. A worktree reused by successive
dispatches produces more than one candidate identity; that is reported as
ambiguous and never resolved by guessing. Nothing derived here is written back
to the recorder database.

## Known identity gaps (measured, not assumed)

1. **`bus.agent.activity.v1` carries no work identity at all.** Across 26,197
   activity envelopes in the retained window, `bead_id`, `task_id`,
   `dispatch_id` and `run_id` were present 0 times. The recorder cannot lose
   what it never received: `runs.bead_id` being NULL is a *producer* gap in the
   Claude Code / codex activity hooks, not a collector projection bug.
2. **The recorder only subscribes to `bus.agent.activity.v1`.**
   `adapters/reflex-recorder/recorder.py` filters `nbus:all` to that one type,
   so `orca.worker.lifecycle.v1` — which *does* carry `run_id`, `task_id`,
   `dispatch_id`, `bead_id` and an absolute `worktree` — never reaches the
   store. In the measured window that stream held 114 distinct task ids, 114
   dispatch ids and 12 bead ids that the recorder never saw. Ingesting it is a
   live-service behaviour change and is deliberately out of scope here.
3. **Branch-name bead derivation is structurally dead.** `enrich.derive_bead_id`
   parses the bead out of the git branch, which worked for `loom/<bead>`
   branches. Current branches are `warxhead1/<topic>` and Orca `xwt/<id>`, which
   encode no bead, so the fallback returns None correctly and there is no bead
   to recover from the branch.

### What was repaired here

`segment.reconstruct_worktree_path` searched `cwd` for the literal
`.claude/worktrees/` sentinel. Agent worktrees moved to
`~/data2/worktrees/<project>/<name>` on 2026-08-24, so every run recorded since
then stored a NULL absolute `worktree` — measured: 85/85 worktree-kind runs
started 2026-09-05 had `worktree IS NULL` while `worktree_slug` was populated
for all 85. That NULL removed the only join key between a recorder segment and
the Orca worker identity described above.

The function now anchors on the last `worktrees` path segment and truncates at
the slug, handling both layouts. This is a projection repair only: run keys are
computed from the slug and are unchanged, no schema moved, and existing rows are
not backfilled. **The change takes effect only after the coordinator restarts
`reflex-recorder.service`**. Historical rows with missing absolute paths remain unmatched.

## Scheduling it

A user timer is enough; there is no daemon.

`~/.config/systemd/user/ecosystem-daily-report.service`:

```ini
[Unit]
Description=nervous-bus ecosystem daily evidence report

[Service]
Type=oneshot
WorkingDirectory=%h/projects/nervous-bus
ExecStartPre=/usr/bin/mkdir -p %h/.cache/nervous-bus/daily
ExecStart=/usr/bin/python3 tools/ecosystem_daily_report.py --hours 24 \
    --output %h/.cache/nervous-bus/daily/report.json
Nice=10
IOSchedulingClass=idle
```

`~/.config/systemd/user/ecosystem-daily-report.timer`:

```ini
[Unit]
Description=Run the nervous-bus ecosystem daily evidence report

[Timer]
OnCalendar=*-*-* 02:15:00 UTC
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now ecosystem-daily-report.timer
systemctl --user list-timers ecosystem-daily-report.timer
```

Use `--since`/`--until` instead of `--hours` when you need a *reproducible*
window — `--hours` is relative to invocation time, so two runs of the same timer
never cover exactly the same span. For a fixed calendar day:

```bash
python3 tools/ecosystem_daily_report.py \
    --since "$(date -u -d 'yesterday 00:00' +%Y-%m-%dT%H:%M:%SZ)" \
    --until "$(date -u -d 'today 00:00' +%Y-%m-%dT%H:%M:%SZ)" \
    --output ~/.cache/nervous-bus/daily/"$(date -u -d yesterday +%F)".json
```

Measured on 2026-09-06 over the full retained journal (4 generations, ~45 MB
gzip plus a 55 MB live file, ~202k in-window records): **14.5 s wall, 82 MiB
peak RSS** at the default caps. Deployment of the unit is the coordinator's
call, not this tool's.

## Tests

```bash
python3 -m pytest tests/test_ecosystem_daily_report.py -q
```

Covers rotation ordering and cross-rotation duplicate collapse, key-order
insensitivity, id collisions kept and flagged, sample caps, missing/naive/
malformed timestamps, half-open window edges, unknown-vs-zero usage, missing
phase and cost metrics, identity missingness against a named denominator,
fixture-classification limits, project/worktree separation, source freshness for
sources absent from the window, cardinality truncation, read-only database
enforcement, the derived correlation's ambiguity reporting, and the optional
attempt ledger's all-or-nothing contract.
