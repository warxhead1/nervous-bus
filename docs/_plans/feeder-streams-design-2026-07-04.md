# Design spike: per-namespace feeder streams (nbus:all amplification)

_2026-07-04 — redo of a design spike lost to a context-compaction event before it
landed. Deferred item from the 2026-07-02 ecosystem-integration audit
(`hearth/docs/_plans/ecosystem-integration-audit-2026-07-02.md`, theme 9,
roadmap item `[P3/M] Evaluate per-namespace feeder streams to end the 3x
nbus:all read/ack amplification`). This is a DESIGN document only —
no code changes are made here. Every number below is measured against the
live production Redis instance (127.0.0.1:6379) on 2026-07-04, not assumed._

## 0. Correcting the source framing

The audit's theme 9 states: *"three consumer groups each XREADGROUP+XACK
100% of nbus:all regardless of interest (distinct group names, so no
collision — but 3x read/ack amplification)"* and names hearth, tengine,
tachyonac as the three. Live evidence below shows this **undercounts the
current state** — the ecosystem has grown since the audit's sub-audits ran,
and one more producer-side amplification path (tachyonac's legacy bridge)
was already flagged as a separate finding this session. The corrected
picture:

- **Write amplification today is 2x for every producer, 3x for one
  (tachyonac-engine)** — not a flat 3x.
- **Read amplification today is 5 live consumer groups + 1 zombie group +
  2 metadata-only pollers**, not 3.

Both are worse than the audit's headline number, which strengthens rather
than weakens the case for this work — but the roadmap item's "3x" should not
be repeated as a precise figure going forward.

## 1. Current publish architecture (evidence)

### 1.1 The canonical dual-write convention

Three independent implementations agree on the same wire convention — `XADD`
the full CloudEvents-lite envelope (as a single `_raw` JSON field, plus
`type`/`source`/`timestamp`/`event_id` helper fields) to **both**:

- `nbus:<channel>` — a typed stream, `MAXLEN ~10000`
- `nbus:all` — the universal fanout stream, `MAXLEN ~50000`

Confirmed sources:
- **Go SDK** (canonical, newly merged): `sdk/go/nbus/publisher.go:43-113`,
  `sdk/go/nbus/config.go:19-23` — `Config.Stream` is documented as unused by
  `Publisher`; it *"always writes to the canonical pair of streams"*, no way
  to opt out.
- **tachyonac-engine**: `internal/nbus/publisher.go:94-188` — delegates to
  the Go SDK's `Publisher` for the common case, confirming convergence.
- **deer-flow** (a *fourth* independent hand-rolled port, not called out in
  the original audit's SDK-sprawl theme): `backend/packages/harness/deerflow/bus.py:88,129,244-252`
  — Python, same dual-write, same field names.

### 1.2 The Rust SDK is now real but does not enforce the pair

`sdk/rust/src/streams.rs` (merged this session — `sdk-rust-streams` branch,
commits `fa4950d`/`9b5660f`) is a from-scratch, tested port of tengine's
`RedisStreamConsumer`/`reap_stale` pattern. It has genuine
`XREADGROUP`/`XACK`/`XAUTOCLAIM` primitives and a `StreamsPublisher`, but
**`StreamsPublisher::publish(stream, ...)` takes an explicit stream name and
writes to exactly that one stream** (`streams.rs:381-436`) — it does not
replicate the Go SDK's baked-in typed+fanout pair. Any Rust caller adopting
this module today must call `publish()` twice (once per stream) to match the
Go/tachyonac/deer-flow convention, or it will under-write relative to the
rest of the ecosystem. This asymmetry needs to be resolved as part of this
work's rollout (§6), not left as a footgun for the next adopter.

### 1.3 tachyonac-engine's confirmed third write (legacy bridge)

`internal/nbus/publisher.go:33-48,94-113,187-230` — when
`config.NBusConfig.Stream` is set to anything other than `""`/`"nbus:all"`,
`Publisher.publish()` takes `publishWithLegacyBridge()` instead of
delegating to the SDK: it builds **one** envelope and XADDs the **identical
bytes** to three streams — typed, fanout, and the legacy `{channel,payload}`
bridge stream (`MAXLEN 100000`) consumed by `tachyonac-bridge` (relays to
`debug.jsonl` + `nbus:all` pub/sub + `deerflow:bus:<channel>`).

This is not hypothetical or historical — it is live in production today:

```
deploy/systemd/tachyonac-engine.service:45:  Environment=NBUS-STREAM=nervous-bus
```

`internal/config/config.go:181` reads both `NBUS-STREAM` and `NBUS_STREAM`
(no typo — both forms are checked), so this env var is live, `Stream =
"nervous-bus"`, `hasLegacyBridge()` is `true`, and every tachyonac-engine
publish costs 3 XADDs today. The code is well-instrumented for its own
retirement (`legacyWrites`/`legacyErrors` counters, an opt-in
`NBUS_LEGACY_PANIC=1` hard-fail switch for stragglers) — this spike does not
need to design that retirement, it already exists; it only needs to account
for the 3x cost while the bridge is still configured.

### 1.4 redis-mirror is a second, independent writer for JSONL-tail producers

`adapters/redis-mirror/mirror.py` does not read from Redis Streams itself —
it tails `debug.jsonl` (the file the shell CLI and `native_publish`/`publish`
subprocess paths write to) and XADDs from there, with its own
`mirror_all`/`universal_stream` config (`mirror.py:184-294,540-573`). Any
producer still on the subprocess/file-tail path (this is where hearth's 13
inline `Command::new("nervous")` call sites and the shell CLI land) gets its
typed+fanout writes from the mirror, not from a direct SDK XADD. This spike
did **not** find evidence of the same logical event being written twice via
*both* the direct-XADD path and the mirror-tail path for one producer — but
it also did not exhaustively verify this for all 13+13 call sites across
hearth/kb/tengine. **Open verification item, folded into §7 rollout**: before
adding a fourth write (the namespace stream), confirm no producer is already
accidentally double-counted between the SDK-direct and mirror-tail paths.

## 2. Consumer inventory (live evidence, not assumption)

`XINFO GROUPS nbus:all` against the production Redis, 2026-07-04:

| Group | Consumers | Pending | Lag | Entries-read | Status |
|---|---|---|---|---|---|
| `hearth-consumer` | 1 | 39 | 0 | 8,402,423 | live |
| `reflex-recorder` | 8 | 0 | 0 | 8,402,423 | live |
| `tachyonac-fanout` | 1 | 1 | 0 | 8,402,423 | live |
| `tengine-cmd` | 602 | 52 | 582 | 8,401,841 | live |
| `tachyonac-engine` | 1 | 0 | **4,317,388** | 4,085,035 | **zombie** |

`nbus:all` itself: `XLEN` 50,001 (at its `MAXLEN ~50000` cap), `MEMORY USAGE`
45,892,020 bytes ⇒ **≈918 bytes/entry**. 821 distinct `nbus:<channel>` typed
streams already exist in Redis from the dual-write convention (`DBSIZE`
8,178; total `used_memory` 1.16 GB).

Plus a sixth consumer not visible in this snapshot (likely a different
Redis instance/host, or not running at query time — still a real,
code-confirmed consumer to design around): **`deerflow-gateway`**,
`backend/app/gateway/routers/bus.py:554-590` — `BusBroker._redis_loop` does
`XREADGROUP GROUP deerflow-gateway ... STREAMS nbus:all >`, then dispatches
to per-consumer `channel_glob` subscribers client-side.

Two more processes touch `nbus:all` but do **not** pay the full-payload
read tax — they call cheap `XLEN`/`XINFO GROUPS` metadata ops, not
`XREADGROUP`: `adapters/pattern-watchdog/watchdog.py:112-181` (lag
alerting across all registered groups) and
`adapters/exporter/prometheus_exporter.py:243-355` (Prometheus gauges).
`adapters/pattern-bundler/bundler.py:266-267` does a plain `XREAD` (no
consumer group, at-most-once, lossy-by-design) — a seventh reader, but not
one that needs PEL/ack semantics.

**Corrected count: 5 live `XREADGROUP` consumer groups, 1 zombie group
inflating lag metrics with no real work behind it, 1 plain-`XREAD` observer,
2 metadata-only pollers.** Every group above reads **100% of `nbus:all`**
regardless of what it actually acts on.

### 2.1 Does each consumer actually need cross-namespace visibility?

Answer, per consumer, from what each dispatch function actually branches on:

- **`hearth-consumer`** (`nbus_consumer.rs:1964` `handle_redis_event`) —
  genuinely broad. Grep of `starts_with("...")` prefixes it dispatches on
  spans ~15 distinct namespace roots: `autobench.`, `bus.bead.`,
  `bus.build.`, `bus.hearth.session.`, `bus.intrinsic.marker`, `bus.tengine.`,
  `deer-flow.*` (4 sub-kinds), `funsearch.`, `hearth.device.`, `kernel.`,
  `loom.lifecycle`, `noise.`, `phase.`, `pulse.kernel.snapshot`, `sdf.`,
  `sph.`, `tachyonos.`, `tengine.` (+ `tengine.evidence.`,
  `tengine.session.` separately), `terrain.`, `tsp.`. Hearth is a legitimate
  wide consumer — it is not a candidate for a single narrow stream, but it
  **is** a candidate for subscribing to a curated *list* of namespace
  streams instead of the unfiltered universal one (§4.3 — one
  multi-stream `XREADGROUP` call, not 15 round trips).
- **`tengine-cmd`** (`tengine-dgc-hal/src/silo/nbus_redis.rs:673-685`) —
  narrow. `event_type.starts_with("tengine.cmd.") || event_type ==
  "funsearch.engine_render.requested.v1"`. This consumer wants essentially
  one channel family; it is reading 8.4M entries to act on a small subset.
- **`reflex-recorder`** (`adapters/reflex-recorder/recorder.py:4-6,41`) —
  narrowest possible case. *"Consumes nbus:all via XREADGROUP ..., filters
  to `bus.agent.activity.v1`"* — **exactly one exact channel type**. This
  consumer doesn't even need a namespace stream; it needs the typed stream
  `nbus:bus.agent.activity.v1`, which **already exists today** as a
  byproduct of the dual-write convention. This is the single cheapest,
  zero-schema-change win available (§7, Phase 0).
- **`tachyonac-fanout`** (`deploy/systemd/tachyonac-fanout.service`,
  "Tachyonac Incident Fanout Relay") — narrow, tachyonos-scoped by design
  (relays tachyonac incidents outward); a namespace stream for `tachyonos.*`
  covers it.
- **`tachyonac-engine`** (zombie group, consumer `cacheman` idle
  1,371,070,993 ms ≈ **15.9 days**) — not a design input; this is dead
  registry state from a superseded consumer identity (superseded by
  `tachyonac-fanout`, per the naming and the fact that `tachyonac-fanout`'s
  own `cacheman` consumer is live with 645ms idle). **Recommend `XGROUP
  DESTROY nbus:all tachyonac-engine` as an immediate, independent cleanup**
  — its 4.3M lag is polluting every lag-monitoring dashboard
  (`pattern-watchdog`, `prometheus_exporter`) with a number that means
  nothing operationally.
- **`deerflow-gateway`** (`bus.py:554-590`) — narrow per-subscription
  (`channel_glob`), broad in aggregate across all active LangGraph
  consumers riding the same broker. Same shape as hearth: legitimate
  multi-namespace interest, filtered client-side after a full read.

### 2.2 Ruled out during this spike (checked, not found)

The task brief asked about `kb-prime-siblings` and the `silo-watcher`
adapter as possible `nbus:all` consumers:

- **`kb-prime-siblings`** (`claude-hook-fast/cmd_kb_prime_siblings.go`) —
  grepped for any `nbus` reference: zero hits. It works entirely off
  `~/.claude/sessions/<pid>.json` registry files, not the bus. Not a
  consumer of anything designed here.
- **`kb` (the CLI/library)** — grepped for `nbus:all`/`XREADGROUP`: zero
  hits. It talks to hearth-api over HTTP, not the bus directly.
- **`adapters/silo-watcher`** — the source file was removed during OSS-prep
  (`git log -- adapters/silo-watcher` → `d9dc91b remove private adapters
  from public repo`); only stale `.pyc` bytecode remains in this repo. It
  may still run from a private fork, but this repo has no evidence of its
  current stream subscription. Not designed around here; flag to whoever
  owns the private fork if this lands.

## 3. Design: per-namespace feeder streams

### 3.1 Naming scheme

`nbus:ns:<namespace>`, where `<namespace>` is **not** simply "the first dot
segment" — that would put tengine's internal diagnostics storm-in-waiting
(`tengine.diag.wt_telemetry.v1`, emitted once per active work-type every 30
frames / ~0.5s, flagged by the audit as "zero mitigation") in the same
bucket as the `tengine.contract.*`/`tengine.session.*` traffic hearth
already legitimately wants — defeating the entire point of this work.

Instead, the namespace boundary is **the existing governance boundary
nervous-bus already draws**, made routable:
`schemas/README.md`'s "Known private prefixes" table already singles out
`tengine.diag.*` as architecturally distinct from the rest of `tengine.*`
(private, gitignored, different owner-intent) — precisely the kind of
sub-namespace that needs its own feeder stream. The routing table is:

1. If the channel type matches an entry in a small, explicit **namespace
   routes table** (start it from the existing private-prefix list —
   `tachyonos.*`, `tengine.diag.*`, `hearth.market.state.*` — plus `bus.*`'s
   second segment, since `bus.*` alone covers 72 unrelated schemas:
   `bus.notify.*`, `bus.bead.*`, `bus.pattern.*`, `bus.agent.*`,
   `bus.dead_letter`, `bus.build.*`, `bus.intrinsic.*`, `bus.hearth.*`,
   `bus.tengine.*` — each with a different owner and a different consumer
   set, exactly the split hearth's own `STORM_RATE_LIMITS` /
   `starts_with()` dispatch table already treats them as today), route to
   `nbus:ns:<that prefix>`.
2. Otherwise, default to the first dot segment: `nbus:ns:tengine`,
   `nbus:ns:deer-flow`, `nbus:ns:kb`, `nbus:ns:hearth`, `nbus:ns:autobench`,
   `nbus:ns:kernel`, `nbus:ns:career-ops`, `nbus:ns:loom`, `nbus:ns:agent`,
   `nbus:ns:jobops`, `nbus:ns:funsearch`, `nbus:ns:hearth-loom`,
   `nbus:ns:greenhouse`, `nbus:ns:codemap`, `nbus:ns:sys`.

Today's public schema census (`ls schemas/*.json`, by root): `bus` 72,
`autobench` 53, `deer-flow` 46, `tengine` 42, `kb` 16, `hearth` 14, `kernel`
11, `loom` 10, `funsearch` 6, `career-ops` 6, plus 8 smaller roots — **~21
public namespace roots**, plus the private `tachyonos.*` overlay (27
channels in `$NERVOUS_HOME/schemas/`) and the two other private prefixes.
**Total: ~24 feeder streams at launch**, a fixed, small, human-reviewable
list — not one stream per channel (which would be ~299+ and impossible for
a consumer like hearth to enumerate by hand) and not one stream total (the
status quo).

This table is data, not code: ship it as
`nervous-bus/config/namespace_routes.toml` (ordered list of
`{prefix, stream}` pairs, longest-prefix-first, default-first-segment
fallback), loaded by both the router process (§3.3) and, later, any SDK
that wants to compute it publisher-side. Extending it (the next
`tengine.diag`-shaped storm) is a one-line config change and a redeploy of
the router — no schema change, no consumer migration required.

### 3.2 What happens to `nbus:all`

**Keep it — do not retire it, and do not hard-cutover any consumer off
it.** Three of today's five live consumer groups (`hearth-consumer`,
`deerflow-gateway`, and to a lesser extent `tachyonac-fanout`) have
legitimately broad, multi-namespace interest; forcing them onto ~15-24
individual stream subscriptions on day one is unnecessary churn for no
immediate win, and `nbus:all` remains valuable as a debugging/dashboard
catch-all (there is no reason to make ad hoc `redis-cli XREAD nbus:all`
triage harder for an operator chasing an unknown-shaped incident).

Phased fate instead:

- **Phase A (this rollout)**: `nbus:all` is untouched — full envelope,
  same `MAXLEN ~50000`, same dual-write. Namespace streams are purely
  additive. Existing consumers see zero behavior change until they
  individually opt in to reading a namespace stream instead.
- **Phase B (after burn-in — separate future task, not scoped here)**:
  once `XINFO GROUPS` shows every real payload-consuming group (not
  metadata pollers) has migrated its dispatch logic onto namespace streams
  and a burn-in window has passed with matching event counts on both
  paths, shrink `nbus:all`'s per-entry payload to a **pointer record**
  (`id`, `type`, `source`, `timestamp` — no `_raw` full envelope). Anyone
  still reading it (ad hoc dashboards, `pattern-bundler`) can resolve a
  pointer to a payload with one cheap `XRANGE` on the matching typed/
  namespace stream. This drops `nbus:all`'s per-entry cost from ~918 bytes
  to under ~150 bytes without ever telling a consumer "your stream is
  gone" — the stream and its group semantics stay identical, only the
  payload shrinks.

### 3.3 Fan-out placement: mirror-side router, not publisher-side triple-write

The task asks to weigh publisher-side dual-XADD vs. mirror-side republish.
Recommendation: **mirror-side**, via a new lightweight router process
(or an extension of `adapters/redis-mirror`, which already has the
config shape for exactly this — `mirror_all`/`universal_stream` in
`mirror.py:184-294` is the same "write one more copy, computed from a
table" pattern this needs, just keyed by namespace instead of on/off).

Reasons, grounded in what §1 already showed going wrong with the
publisher-side alternative:

- **Publisher-side would mean touching every producer in four languages**
  (Rust `sdk/rust/streams.rs`, Go `sdk/go/nbus`, Python `deerflow/bus.py`,
  the shell CLI) plus hearth's 13 inline `Command::new("nervous")` sites —
  the exact sprawl theme 2 of the audit already diagnosed as the root cause
  of the ecosystem's duplication problem. Adding a namespace write to that
  list multiplies the sprawl instead of curing it.
- **tachyonac's own legacy-bridge history (§1.3) is a cautionary tale for
  publisher-side multi-stream writes**: three independent producer-side
  XADDs of "the same" event, kept in careful lockstep (identical envelope
  bytes, same ID) specifically because two separately-minted envelopes for
  one logical event is an observable correctness bug for a downstream
  consumer correlating by ID. A namespace write done centrally by one
  router, working off the already-written typed stream, sidesteps this
  entirely — it re-emits the exact `_raw` bytes it read, never re-mints an
  envelope.
- **A single router is independently testable, deployable, and
  rate-limitable** in one place — exactly where storm mitigation (theme 9's
  other half: "no producer does emitter-side batching") should also live
  long-term, rather than being re-invented per producer.

**Router mechanics**: one process, one Redis connection, `XREADGROUP` over
**the 821 existing typed `nbus:<channel>` streams** (not `nbus:all` — no
reason to add an 8th reader of the universal stream just to feed this),
consumer group `nbus-namespace-router`. For each entry, resolve its
namespace via `namespace_routes.toml`, XADD the same `_raw`/`type`/
`source`/`timestamp`/`event_id` fields (byte-identical, not re-derived) to
`nbus:ns:<namespace>`, ack the typed-stream entry. New typed streams appear
automatically as new channels get published (`XREADGROUP` with a large
`STREAMS` list needs periodic re-bootstrapping when new streams appear —
call this out explicitly as an implementation gotcha for whoever builds it:
poll `SCAN nbus:*` on an interval, diff against the currently-joined stream
set, and rejoin, since Redis has no "subscribe to all streams matching a
pattern" primitive).

If 821-and-growing individual `STREAMS` arguments in one `XREADGROUP` call
turns out to have practical overhead (untested here — flag for the
implementation task to benchmark), the fallback is sharding the router by
namespace-routes-table row (one lightweight worker per feeder stream,
each reading only the typed streams that resolve to it) — still far
cheaper than every end consumer reading `nbus:all` today.

### 3.4 Backward compatibility

No consumer's existing subscription changes as part of this design. The
migration is purely additive until an individual consumer team chooses to
cut over (§4). `nbus:all`'s group semantics, retention, and payload shape
are unchanged in Phase A. The typed `nbus:<channel>` streams that already
exist are untouched — the router reads them, never writes to them.

## 4. Concrete before/after estimate

**Today (measured):**
- Write: 2 XADDs/event for most producers (~918 B × 2 ≈ 1.8 KB/event
  written across `nbus:<channel>` + `nbus:all`), 3 XADDs/event for
  tachyonac-engine while its legacy bridge is configured (~2.75 KB/event).
- `nbus:all` steady-state size: 50,001 entries / 45.9 MB (capped at
  `MAXLEN ~50000`).
- Read: every one of 5 live groups (§2) pays for **8.4M entries-read**
  each, i.e. ~42M total group-reads against one logical stream of ~50k
  live entries — even though `tengine-cmd` acts on a `tengine.cmd.*`-sized
  slice and `reflex-recorder` acts on exactly one channel type out of
  ~299+.

**After (steady state, once real consumers have migrated to namespace
streams, Phase A → B):**
- Write: unchanged in Phase A (namespace write is done once, centrally, by
  the router reading the typed stream — it does not add a write at the
  original producer). Net new Redis write volume system-wide: ~1 extra
  XADD per event (the router's namespace write) plus ~1 extra XREADGROUP+
  XACK per event (the router's own read of the typed stream) — a **fixed,
  one-time** cost regardless of how many consumers exist, replacing what is
  today an *O(number of consumer groups)* read cost. With 5 live groups
  today (and growing — `deerflow-gateway` is the 5th added since the
  audit), this is already a net win and the win grows with every future
  consumer.
- Read: `reflex-recorder` drops from 8.4M `nbus:all` reads to reading only
  `nbus:bus.agent.activity.v1` (or `nbus:ns:bus.agent`) — a channel with
  order-of-magnitude fewer entries. `tengine-cmd` drops similarly once
  scoped to `nbus:ns:tengine.cmd` (or the router splits `tengine.cmd.*`
  out as its own routes-table row, mirroring the `tengine.diag` split).
  `hearth-consumer`/`deerflow-gateway` keep broad interest but stop paying
  for namespaces they provably never dispatch on (e.g. any future
  `codemap.*`/`greenhouse.*` traffic they've never once branched on).
- Phase B (separate future task): `nbus:all` payload shrinks from ~918 B/
  entry to <150 B/entry (pointer-only) once no real consumer is reading its
  full payload anymore — at the same 50k-entry retention, that's a drop
  from ~45.9 MB to ~7.5 MB for that one stream, with the 24 namespace
  streams (sized independently per namespace's own volume/retention needs)
  absorbing the real payload traffic instead.

## 5. Non-goals (explicitly out of scope for this design)

- Retiring `nbus:all` outright (rejected in §3.2 — legitimate broad
  consumers exist today).
- Deciding retention/`MAXLEN` for each of the ~24 namespace streams — that
  is an implementation-task decision informed by each namespace's actual
  volume (e.g. `bus.notify` is low-volume/high-value, `tengine.diag` is
  the opposite), not a design-spike one.
- Fixing the zombie `tachyonac-engine` consumer group, the 602-consumer
  bloat on `tengine-cmd` (likely per-pid consumer names, e.g.
  `engine-<pid>`, never cleaned up via `XGROUP DELCONSUMER` on shutdown —
  same "stale identity" pattern as the zombie group, worth its own ticket),
  or resolving whether hearth's SDK-direct vs. mirror-tail paths ever
  double-write — all flagged as adjacent findings above, none designed
  here.
- Retrofitting the Rust SDK's `StreamsPublisher` to auto-write the
  typed+fanout(+namespace) trio like the Go SDK does — flagged in §1.2 and
  §6 as a rollout prerequisite, not designed in detail here.
- Emitter-side batching/coalescing for the `tengine.diag.wt_telemetry.v1`
  storm-in-waiting — theme 9's other half; isolating it into its own feeder
  stream (§3.1) caps its *blast radius* but does not reduce its *volume*.
  That is a tengine-side fix, tracked separately.

## 6. Rollout plan (phased; design only — no implementation here)

**Phase 0 — zero-schema-change quick win (can ship independently of
everything else here):**
- Point `reflex-recorder` (`adapters/reflex-recorder/recorder.py:41`) at
  `nbus:bus.agent.activity.v1` instead of `nbus:all`. The typed stream
  already exists (confirmed live: `XLEN` 10,000, `MEMORY USAGE` 12.35 MB).
  No router, no new stream, no schema change — just changing `STREAM_NAME`
  and dropping the client-side type filter (it becomes a tautology). This
  alone removes reflex-recorder from the "reads 8.4M events to act on one
  channel" column.
- `XGROUP DESTROY nbus:all tachyonac-engine` to retire the zombie group
  (idle 15.9 days) and stop polluting `pattern-watchdog`/
  `prometheus_exporter` lag metrics with a meaningless 4.3M number.

**Phase 1 — namespace routes table + router process:**
- Author `nervous-bus/config/namespace_routes.toml` seeded from §3.1's
  list, reviewed against `schemas/CHANNELS.md` and the private-prefix table
  in `schemas/README.md`.
- Build the router (new `adapters/nbus-namespace-router/` following the
  existing adapter layout, e.g. `adapters/redis-mirror/`'s structure) per
  §3.3. Ship with metrics parity to `adapters/exporter/prometheus_exporter.py`
  (namespace-stream lengths/lag) so the migration's health is observable
  from day one, not just trusted.
- Deploy alongside existing infra; verify namespace streams are populating
  correctly for a burn-in period before any consumer reads them.

**Phase 2 — per-consumer opt-in migration (each is independent, can
proceed in any order):**
- `tengine-cmd` (`tengine-dgc-hal/src/silo/nbus_redis.rs:673-710`): switch
  `CMD_ALL_STREAM` from `"nbus:all"` to the namespace stream(s) covering
  `tengine.cmd.*` + `funsearch.engine_render.requested.v1` (may need two
  streams named in one `XREADGROUP STREAMS` call, or a routes-table split
  for `tengine.cmd` specifically — decide during implementation based on
  §3.1's "split when volume/consumers diverge" principle).
- `tachyonac-fanout` (`deploy/systemd/tachyonac-fanout.service` /
  tachyonac-bridge codebase): switch to `nbus:ns:tachyonos`.
- `deerflow-gateway` (`backend/app/gateway/routers/bus.py:554-590`): switch
  `_redis_loop`'s hardcoded `"nbus:all"` to the curated namespace-stream
  list matching its active `channel_glob` subscriptions (it already tracks
  subscriber globs — deriving the stream list from those globs is a
  natural extension of `subscribe()`, not a rewrite).
- `hearth-consumer` (`crates/hearth-api/src/nbus_consumer.rs:1610-1613`):
  last, because it is the broadest consumer and the audit's own P0 (PEL
  recovery) and P1 (storm throttles) work already landed here recently —
  avoid stacking a third structural change onto this file in the same
  window. When it does move, `NBUS_STREAM`/`NBUS_GROUP` becomes a *list* of
  ~15-20 namespace streams read via one multi-stream `XREADGROUP ... STREAMS
  s1 s2 ... s_n > > ... >` call — one network round trip, not N.

**Phase 3 — SDK equalization (can run in parallel with Phase 1/2):**
- Fix the Rust/Go SDK asymmetry from §1.2: either give `sdk/rust/streams.rs`
  a `Publisher` type mirroring Go's baked-in typed+fanout(+namespace) trio,
  or explicitly document in the Rust SDK's README that `StreamsPublisher`
  is a single-stream primitive and callers wanting the canonical
  multi-stream convention must call it per-stream — pick one; don't leave
  it ambiguous for the next adopter migrating off the subprocess path.
- Once the namespace-routes table is stable (post Phase 1 burn-in),
  consider promoting the namespace write from the router into the Go/Rust
  SDKs directly (publisher computes its own namespace from the table and
  writes 3 streams instead of the router writing the 3rd) — this trades
  "one central place to update the table" for "no router process/extra
  hop," a genuine tradeoff to revisit with real Phase 1/2 data in hand, not
  a decision to make now.

**Phase 4 — nbus:all payload thinning (§3.2 Phase B):** only after Phase 2
confirms every live payload-consuming group has migrated and a burn-in
window shows no regression. Separate task; do not start it opportunistically
mid-Phase-2.

## 7. Evidence appendix (commands run against production Redis, 2026-07-04)

```
redis-cli XLEN nbus:all                    → 50001
redis-cli MEMORY USAGE nbus:all             → 45892020
redis-cli XINFO GROUPS nbus:all             → hearth-consumer, reflex-recorder,
                                               tachyonac-engine, tachyonac-fanout,
                                               tengine-cmd  (see table in §2)
redis-cli XINFO CONSUMERS nbus:all tachyonac-engine
                                             → cacheman, idle=1371070993 (≈15.9d)
redis-cli XINFO CONSUMERS nbus:all tachyonac-fanout
                                             → cacheman, idle=645 (live)
redis-cli --scan --pattern 'nbus:*' | wc -l → 823 keys (821 typed streams +
                                               nbus:all + nbus:bus.dead_letter)
redis-cli DBSIZE                            → 8178
redis-cli INFO memory | grep used_memory:   → 1161528728 (≈1.16 GB)
redis-cli XLEN nbus:bus.agent.activity.v1   → 10000  (MEMORY USAGE 12352812)
redis-cli XLEN nbus:tengine.contract.violation.v1
                                             → 10002 (MEMORY USAGE 8310304)
redis-cli XLEN nbus:tachyonos.kalshi.price_update.v1
                                             → 10001 (MEMORY USAGE 5933328)
```

Schema-root census (`ls schemas/*.json`, grouped by first dot segment):
`bus` 72, `autobench` 53, `deer-flow` 46, `tengine` 42, `kb` 16, `hearth`
14, `kernel` 11, `loom` 10, `funsearch` 6, `career-ops` 6, plus
`_per-project` 5, `agent` 5, `jobops` 4, `pulse` 2, `hearth-loom` 2,
`greenhouse` 2, `codemap` 2, `sys` 1, `shader` 1, `codeforces_problem` 1 —
21 public roots, 299 total public channels (`schemas/CHANNELS.md`), plus 27
private `tachyonos.*` channels in `$NERVOUS_HOME/schemas/`.
