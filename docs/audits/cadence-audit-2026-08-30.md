# Cadence Audit — 2026-08-30 (LANE J)

Read-only inventory of every recurring automation mechanism on this box: systemd
user timers/services, system timers, GitHub Actions `schedule:` workflows, Orca's
orchestration DB, and Redis/nbus cadence (via the staleness monitor's own report).

Scope note (exhaustion clause up front): this audit enumerates and classifies.
For "does anything consume X" claims below, the surfaces checked were: `grep -rl`
across `~/projects/*` (all checked-out repos, including private ones:
tachyonac-engine, hearth, hearth-loom, deer-flow, kb, career-ops, tengine,
job-search-hub), the systemd user unit directory (`~/.config/systemd/user/`),
and Orca's `orchestration.db` schema. NOT checked: hearth-loom's own internal
bead-router logic beyond grep (it's Go, and I did not read its full dispatch
table), any private `$NERVOUS_HOME` overlay adapters (per CLAUDE.md these are
outside this repo's boundary and I did not have a path to enumerate them),
and the tachyonac Go binary's internal consumers (only grepped, did not read
`internal/nbus/` call sites exhaustively).

---

## 1. systemd --user timers (47 total)

Full list captured via `systemctl --user list-timers --all`. Grouped by cadence
band with what each one does (from unit `Description=` + ExecStart target) and
health at snapshot time (all showed `status=0/SUCCESS` on LAST run unless noted).

### Sub-hourly (the hot loop — this session's cadence)

| unit | cadence | does | last run |
|---|---|---|---|
| kb-enrich.timer | ~20min | kb enrichment queue drain (topology graph builder) | ok |
| nervous-transcript-snapshot.timer | 2min | mirrors Claude Code JSONL transcripts → durable cache | ok, 1m33s ago |
| runner-watchdog.timer | 2min | GitHub Actions Runner Watchdog | ok |
| hearth-emu-reaper.timer | ~1min | kills orphaned hearth-test emulators with no valid lease | ok |
| tachyonac-option-capture-verifier.timer | ~1min | independent option-capture replay verifier | ok |
| hearth-benchmark.timer | ~5-6min | Hearth CSI Position Benchmark (single run) | ok |
| pattern-watchdog.timer | ~5min | pattern pipeline health check (oneshot) | ok |
| pipeline-snapshot.timer | ~5min | hearth-loom pipeline snapshot for orchestrator context | ok |
| debug-log-rotate.timer | ~15min | rotates `debug.jsonl` when >100MiB | ok |
| shadergen-reaper.timer | ~5min | TEngine shadergen idle-silo reaper | ok |
| tachyonac-outage-watchdog.timer | ~10min | proves the cron scheduler is alive independent of the engine | ok |
| tmp-gobuild-cleanup.timer | ~1h | cleans stale `/tmp/go-build*` (ENOSPC guard) | ok |

### 15-30 min (nervous-bus core — the "four enabled today")

| unit | cadence | does | last run |
|---|---|---|---|
| **staleness.timer** | 15min | channel staleness monitor; on threshold, publishes `bus.channel.stale.v1` | ok, 596ms, exit 0 |
| **ci-watch.timer** | 30min | CI/CD observability + contract lint; files/updates beads for RED GH Actions runs | ok, 12.1s |
| **dlq.service** (long-running daemon, not timer) | continuous | dead-letter persist+retry+quarantine; optional `bus.dlq.summary.v1` heartbeat | active, healthy |
| **bead-enrichment-consumer.service** (long-running, not timer) | continuous | consumes `nbus:deer-flow.bead.filed.v1` → BeadEnricher → `bd update` | **BROKEN — see §6** |

### Hourly-ish / off-peak nightly cluster (00:00–07:00 EDT)

| unit | fires at | does |
|---|---|---|
| worktree-gc@{hearth-loom,tachyonac-engine,deer-flow,hearth,tengine,nervous-bus} (×6 timers) | 00:00–01:00 | per-repo GC of stale agent worktrees |
| go-tmp-clean.timer | 00:00 | sweeps orphaned Go build scratch >1 day from GOTMPDIR |
| hearth-train.timer | 03:00 | Hearth CSI Pipeline V2 training cycle |
| mmx-automation@tachyonac-engine--build-warnings.timer | 03:18 | sandboxed MiniMax build-warnings sweep |
| tachyonac-full-gate.timer | 03:30 | nightly FULL deploy-test gate (heavy — backstop for daytime fast gate) |
| kb-catchup.timer | 04:32 | full-corpus catch-up ingest (heals files autoingest's 45min window missed) |
| mmx-automation@tengine--build-warnings.timer | 05:18 | sandboxed MiniMax build-warnings sweep (tengine) |
| reflex-analysis.timer | 05:30 | Reflexarc nightly analysis — outcome labeling, detectors, struggle ledger, digest |
| tachyonac-postgres-backup.timer | 05:32 | compressed Postgres backup |
| tachyonac-worktree-reap.timer | 06:45 | reaps landed/patch-equivalent tachyonac-engine agent worktrees |
| psd-resync.timer | hourly (:00) | "Timed resync" (generic description; not further traced) |
| oxsynth-automation-bus.timer | hourly (:00-ish, offset) | stage inbox + verify/publish mmx automation outboxes |
| deer-weekly-drift.timer | daily @ 02:24 | weekly stack-tuner audit drift report |
| tengine-stale-dispatch.timer | daily @ 02:30 | surfaces stalled P0/P1 tengine beads |
| tier2-analyst.timer | daily @ 02:30 UTC (fires, self-gates to 01:00-05:59 UTC window) | Reflexarc Tier-2 off-peak deer-flow analyst over labeled runs |
| kb-decay.timer | daily | time-based confidence decay across kb vault |
| beads-backup.timer | ~2h | encrypted Beads backup to Storage 2 |
| systemd-tmpfiles-clean.timer | daily | stdlib tmpfiles GC |
| arch-update.timer | daily | arch-update auto check |

### Weekly / multi-day

| unit | cadence | does |
|---|---|---|
| career-scanner.timer | daily @ ~09:33 (drifts) | career-ops demand-matching scanner, one cycle |
| career-pipeline-drain.timer | ~daily, drifted to 1d18h out | feeds `data/pipeline.md` into batch-runner |
| tachyonac-papertrade.timer | daily @ 09:30 | Tachyonac paper-trade harness |
| mmx-automation@tachyonac-engine--dead-wiring.timer | every 2 days | dead-wiring sweep |
| mmx-automation@tachyonac-engine--review-discrepancies.timer | ~5 days | review-discrepancy sweep |
| mmx-automation@tengine--review-discrepancies.timer | ~6 days | review-discrepancy sweep (tengine) |
| runner-diag-prune.timer | weekly | prunes GH Actions runner `_diag` logs, keeps last 100/runner |

### Other

- `tier2-analyst.service` at snapshot time exited immediately: **"not in off-peak
  window (01:00–05:59 UTC); exiting"** — this is by design (self-gating), not a
  failure; the *timer* fires more often than the *service* actually does work.

**Total: 47 timers**, all showed `SUCCESS` on their last completed run except
where noted in §6.

## 2. Long-running systemd --user services (not timer-driven, nervous-bus/deer-flow stack)

| service | role | health |
|---|---|---|
| redis-mirror.service | tails `debug.jsonl`, mirrors file-only producers into Redis; hot-reloads schemas/5min | active 6d, healthy |
| pattern-bundler.service | `nbus:all` + `nbus:logs` → windowed stats → `nbus:bundles` | active 1d15h (restarted more recently than siblings — worth checking why), healthy |
| pattern-consumer.service | `nbus:bundles` → LLM analysis → `bus.pattern.signal.v1` | active 6d, healthy |
| signal-router.service | pattern signals → annotations/drafts/beads | active 6d, healthy |
| reflex-recorder.service | agent run capture + segmentation (feeds tier2-analyst + reflex-analysis.timer) | active 6d, healthy |
| log-normalizer.service | Docker/journald/kernel → `nbus:logs` | running (not deep-probed) |
| nervous-silo-watcher.service | fs-tail tengine session dirs → silo lifecycle events | running |
| nervous-bus-exporter.service | Prometheus exporter | running |
| tree-wipe-tripwire.service | inotifywait on 7 project dirs (delete/moved_from), logs deleter identity | active 6d, 0 log entries at snapshot (no deletes fired — correct/quiet, per its own incident doc) |

## 3. System-level timers (`systemctl list-timers`, non-user)

Only 8, all OS-hygiene, none bus-relevant: `shadow.timer`, `fstrim.timer`,
`logrotate.timer`, `plocate-updatedb.timer`, `man-db.timer`,
`systemd-tmpfiles-clean.timer` (system copy), `archlinux-keyring-wkd-sync.timer`,
`cachyos-rate-mirrors.timer`. No system crontab exists (`crontab` binary not
installed; `/etc/cron.d`, `/etc/cron.daily` absent — only `/etc/cron.hourly/snapper`,
unrelated to this stack).

## 4. Orca orchestration DB

Path: `/home/eric/.config/orca/orchestration.db` (opened `mode=ro`).

Schema (16 tables): `runs`, `tasks`, `coordinator_runs`, `deliveries`,
`dispatch_contexts`, `decision_gates`, `external_worker_runs`,
`federated_dispatches`, `federation_relay_items`, `messages`,
`mutation_caller_identities`, `mutation_receipt_ledger`, `mutation_receipts`,
`question_threads`, `remote_dispatch_attachments`, `remote_questions`,
`run_coordinator_handles`, `worker_dispatches`, `worker_terminal_archives`,
`worker_terminal_resources`, `legacy_*` (4 tables).

**Finding: Orca has no built-in scheduled-automation table.** `runs` has no
cron/rrule/schedule column (schema dumped and read in full); no table name
contains "automat", "schedule", "cron", or "rrule". `coordinator_runs` is
empty (0 rows) at snapshot; `runs` has 124 rows, `tasks` 764 — this is a
run/dispatch *history* ledger, not a scheduler. The recurring "orca automations"
that actually exist on this box (the `mmx-automation@<repo>--<recipe>.timer`
family: build-warnings, dead-wiring, review-discrepancies) are wired through
**systemd timers calling `mmx-automation@` templated units**, not through any
state inside `orchestration.db`. Exhaustion clause: I checked the schema and
row counts only — I did not read the Rust/Go source that writes to these
tables to confirm no *application-level* cron logic exists elsewhere in Orca
outside this DB (e.g. a JS setInterval in the Orca IDE process itself);
that would require reading Orca's source tree, which I did not do (out of
file-scope and budget for this lane).

## 5. GitHub Actions scheduled workflows (`on: schedule`)

Grepped every `.github/workflows/*.yml(.yaml)` under `~/projects/*`:

| repo | workflow | cron | UTC |
|---|---|---|---|
| tengine | gpu-nightly-self-hosted.yml | `0 10 * * *` | daily 10:00 |
| tengine | cpu-extended.yml | `0 8 * * *` | daily 08:00 |
| deer-flow | nightly.yaml | `0 16 * * *` | daily 16:00 |
| career-ops | gfi-claims.yml | `23 6 * * *` | daily 06:23 |
| career-ops | pr-adoption.yml | `47 5 * * *` | daily 05:47 |
| career-ops | stale.yml | `0 6 * * 1` | weekly Mon 06:00 |
| orca | hourly-mac-build.yml | (hourly, not deep-read) | — |
| orca | daily-mac-build.yml | (daily, not deep-read) | — |
| llama.cpp, opencode, shaderc, hearth-vault, gitops-toolkit, runner-router | assorted (not this stack) | — | — |

**nervous-bus itself has zero `schedule:`-triggered workflows** — its only
workflow, `schema-coverage.yml`, is PR-triggered (per CLAUDE.md's description).
**hearth-loom has no `.github/workflows/` directory at all** — it does not use
GH Actions scheduling; it is driven entirely by bus consumption (`bus.bead.*`),
consistent with the project's stated architecture (bus is the scheduler, not cron).

## 6. Findings requiring action

### (a) LIVE BUG — `bead-enrichment-consumer.service` is not consuming

Enabled today at 13:07:43 EDT. From `journalctl --user -u bead-enrichment-consumer`:
92 consecutive `redis error: Timeout reading from socket; retrying in 10s` lines,
one every 10s since start, still failing at snapshot time (13:37:35 EDT, 30 min
straight). `redis-cli ping` from this session returns `PONG` immediately, and
every *other* nbus consumer (pattern-bundler, pattern-consumer, signal-router,
reflex-recorder, redis-mirror, dlq) is healthy against the same Redis instance —
so this is not a Redis outage, it's specific to this consumer's connection
(wrong host/port/timeout config, or a blocking call with too-short a read
timeout). **This is the consumer for `nbus:deer-flow.bead.filed.v1`
enrichment** — while it's stuck in this loop, bead enrichment (deer-flow
attaching context/labels via `bd update`) is not happening. Verdict: **fix
the connection config**, not a cadence problem.

### (b) Collision — nightly off-peak cluster, 00:00–07:00 EDT

Six `worktree-gc@*` timers (00:00–01:00), `go-tmp-clean` (00:00), `hearth-train`
training cycle (03:00), `mmx-automation@tachyonac-engine--build-warnings` (03:18),
`tachyonac-full-gate` — described as its own doc calls "nightly FULL deploy-test
gate" (03:30, explicitly the *heavy* backstop gate), `kb-catchup` full-corpus
ingest (04:32), `mmx-automation@tengine--build-warnings` (05:18), `reflex-analysis`
nightly digest (05:30), and `tachyonac-postgres-backup` (05:32) all land inside one
7-hour window with several genuinely CPU/IO-heavy jobs (a full deploy gate, a
training cycle, two sandboxed MiniMax build sweeps, a DB backup, 6 worktree GCs).
Per `~/.claude/memory-global/go-builds.md`, concurrent Go builds already have a
known GOCACHE-poisoning failure mode across worktrees; none of these overlap to
the *second*, but `tachyonac-full-gate` (03:30) and `mmx-automation@tachyonac-engine--build-warnings`
(03:18) are 12 minutes apart on the same repo and could genuinely contend for
the same build cache/CPU quota. **Verdict: tune** — stagger
`mmx-automation@tachyonac-engine--build-warnings` to run *after* `tachyonac-full-gate`
finishes (or move it outside the 03:00-04:00 tachyonac-heavy band) rather than
12 minutes before a full deploy gate starts consuming the same repo's build
cache.

### (c) Zombie-shaped but not actually zombie — `bus.channel.stale.v1` / `bus.dlq.summary.v1`

Grepped every repo under `~/projects/*` for `bus.channel.stale` and
`bus.dlq.summary`: **zero named subscribers** outside the producers' own
directories and the schema files. On its face this looks like a fire-and-forget
event nobody reads. But `pattern-bundler.service` subscribes to the fan-in
stream `nbus:all` (confirmed by reading `bundler.py:267`, `r.xread({"nbus:all":
...})`), which catches every published event including these two by name —
so they ARE consumed, just generically (windowed stats → `nbus:bundles` →
LLM pattern analysis → `signal-router` → possible bead/annotation), not by
any consumer that specifically understands "a channel just went stale" or
"the DLQ just spiked" as a distinct, actionable signal. **Verdict: feed-it-a-real-consumer.**
The generic path means a stale-channel or DLQ-spike event has to survive an
LLM pattern-extraction pass before anything acts on it — there is no direct
"stale channel → open a bead" or "DLQ spike → alert" path, which is exactly the
kind of event these two adapters were built to make fast. Exhaustion clause:
checked all of `~/projects/*` via grep, systemd units, and Orca's DB schema;
did NOT check the private `$NERVOUS_HOME` overlay (out of reach per this
repo's own boundary rule) for a private consumer of either channel.

### (d) Gap — memory's own claim about tengine CI emitters confirmed stale-but-true

`staleness/monitor.py`'s own module docstring (line 5) cites "tengine CI
emitters with no systemd unit" as a motivating incident. Consistent with §5:
tengine's two scheduled workflows (gpu-nightly, cpu-extended) exist as GH
Actions cron, but there is no local systemd unit mirroring their expected
cadence into the staleness monitor's baseline — so if the GH Actions runner
for tengine goes down, staleness watches the *bus event* for absence but there
is no independent liveness check on the GH Actions schedule itself (contrast
with `tachyonac-outage-watchdog.timer`, whose entire job is "prove the cron
scheduler is alive independent of the engine process" — tengine has no
equivalent). **Verdict: keep the staleness-based detection, but note the gap**
— nothing proves tengine's *GH Actions runner* itself is alive the way
`runner-watchdog.timer` (2min) does for the runner process; runner-watchdog is
generic across all 5 runners (deer-flow, hearth-loom, hearth-vault, hearth,
tengine, nervous-bus) and IS running, so this gap is narrower than the
docstring suggests — likely already closed by `runner-watchdog.service` being
added since that docstring was written. Flagging as **tune** (verify
runner-watchdog predates or postdates the docstring) rather than a live gap.

## 7. Memory/CPU load ranking (always-on services, RSS via `ps`)

Top 30 processes by RSS on the box (not filtered to this stack — shown for
contention context): `tsc` (4.9G), 6× `postgres` backends (2.8-4.3G each),
`worldserver` (2.6G), `dolt` (1.7G — this IS the beads backend,
`beads-dolt.service`), more `postgres`, `hearth-api` (891M), `stremio` (699M),
`node-MainThread` ×several (200-1.7G, various dev servers), `claude` (459M,
this session), `uvicorn` (409M), `orca-ide` ×2 (~380-401M), `buildkitd` (325M),
`codex` (222M), `WebKitWebProcess` (216M).

**None of the nervous-bus adapter processes (dlq, staleness, ci-watch,
redis-mirror, pattern-bundler/consumer, signal-router, reflex-recorder,
bead-enrichment-consumer) appear in the top 30 by RSS** — they're all
single-digit-to-low-double-digit MB (dlq 3.7M, staleness 39.8M peak,
ci-watch 192.9M peak — the largest of the bunch, likely from the GH API
client + LLM calls it makes for triage). The load on this box is dominated
by Postgres (tachyonac + hearth), the beads Dolt server, dev-server node
processes, and IDE/editor tooling — not by the bus automation layer. The
bus layer is cheap; the nightly-cluster CPU contention in §6(b) is a
scheduling-collision risk, not a steady-state memory risk.

## 8. Cadence Map (summary table)

| mechanism | cadence | produces | consumed by | verdict |
|---|---|---|---|---|
| staleness.timer | 15min | `bus.channel.stale.v1` + `report.md` | generic: pattern-bundler→LLM→signal-router; direct: humans reading report.md (this audit did) | feed-it-a-consumer (needs a direct stale→bead path) |
| ci-watch.timer | 30min | files/updates beads for RED CI runs | `bd` (direct bead pipeline) | keep |
| dlq.service | continuous | quarantine + optional `bus.dlq.summary.v1` | generic (pattern-bundler); forensics JSONL for humans | feed-it-a-consumer (same as staleness) |
| bead-enrichment-consumer.service | continuous | `bd update` enrichment | deer-flow bead pipeline | **BROKEN — fix redis connection** |
| tier2-analyst.timer | daily, self-gated 01:00-05:59 UTC | Reflexarc tier-2 labels | reflex-analysis.timer nightly digest | keep |
| reflex-analysis.timer | daily 05:30 | outcome labels, detectors, struggle ledger, digest | humans / future agent dispatch decisions | keep |
| tree-wipe-tripwire.service | continuous (inotify) | delete-event log | humans, post-incident forensics only | keep (cheap insurance) |
| redis-mirror.service | continuous, 5min schema hot-reload | mirrors file-only producers into Redis | every Redis-side consumer | keep |
| pattern-bundler → pattern-consumer → signal-router | continuous | `nbus:bundles` → `bus.pattern.signal.v1` → beads/annotations | working 3-stage pipeline | keep |
| worktree-gc@* (×6) | ~00:00-01:00 nightly | reaps stale agent worktrees per repo | disk hygiene | keep |
| go-tmp-clean.timer | daily 00:00 | sweeps GOTMPDIR >1day | disk hygiene | keep |
| tmp-gobuild-cleanup.timer | ~hourly | sweeps `/tmp/go-build*` | ENOSPC guard | keep |
| mmx-automation@tachyonac-engine--build-warnings.timer | daily 03:18 | sandboxed build-warnings report | consumed by oxsynth-automation-bus outbox verify/publish step | **tune** — 12min before tachyonac-full-gate, same repo, contends |
| tachyonac-full-gate.timer | daily 03:30 | nightly full deploy-test gate result | tachyonac deploy pipeline | keep (but see tune above) |
| mmx-automation@tengine--build-warnings.timer | daily 05:18 | build-warnings report | oxsynth outbox | keep |
| mmx-automation@{tachyonac-engine,tengine}--{dead-wiring,review-discrepancies}.timer | 2-6 days | audit reports | oxsynth outbox | keep |
| oxsynth-automation-bus.timer | ~hourly | stages/verifies/publishes mmx outboxes | downstream mmx report consumers | keep |
| kb-enrich / kb-autoingest / kb-catchup / kb-decay / kb-career-ops-ingest.timer | 20min / 45min-ish / daily / daily / periodic | kb vault ingestion + decay | kb vault (self-consuming) | keep |
| runner-watchdog.timer | 2min | GH Actions runner liveness | closes the "runner dead" gap noted in staleness docstring | keep |
| tachyonac-outage-watchdog.timer | ~10min | proves cron scheduler alive independent of engine | tachyonac ops | keep (exemplar pattern — tengine has no equivalent per §6d) |
| GH Actions `schedule:` (tengine ×2, deer-flow ×1, career-ops ×3) | daily/weekly | CI runs, GFI claims, PR adoption, staleness sweep | GH-side | keep |
| nervous-bus `schema-coverage.yml` | PR-triggered only, no schedule | schema coverage gate | PRs | keep (correctly not cadence-based) |
| hearth-loom | no GH Actions, no local timer | — | driven entirely by `bus.bead.*` consumption | keep (by design — bus IS its scheduler) |
| Orca `orchestration.db` | N/A — no cron table found | run/task/dispatch history only | — | not-an-automation-source (mmx-automation timers are the real mechanism) |

---

**Bottom line:** the automation layer is broad (47 user timers + ~15 long-running
services) but the bus-facing pieces (dlq, staleness, ci-watch, pattern
pipeline, reflex-recorder/tier2/reflex-analysis) are cheap and mostly healthy.
One live bug (`bead-enrichment-consumer` stuck in a 10s redis-timeout retry
loop since enablement at 13:07:43 today — bead enrichment is not happening
until this is fixed), one real scheduling collision worth staggering
(`mmx-automation@tachyonac-engine--build-warnings` 12min ahead of
`tachyonac-full-gate`), and two events (`bus.channel.stale.v1`,
`bus.dlq.summary.v1`) that reach only a generic LLM-pattern consumer instead
of a direct actionable path.
