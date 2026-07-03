# nbus — Rust SDK for nervous-bus

Typed Rust bindings for publishing to and consuming from nervous-bus.

Two publish paths exist today for backwards compatibility (`publish` /
`native_publish`, feature-gated below), plus a real Redis Streams module
(`streams`, feature `streams`) that is the recommended path for any new
consumer — it is the first reusable home for the `XREADGROUP`/`XACK`/
`XAUTOCLAIM` consumer-group pattern that's otherwise been copy-pasted
independently into tengine, hearth, and tachyonac-engine.

## Feature flags

| Feature | Default | Adds | Purpose |
|---|---|---|---|
| `subprocess` | yes | — | `publish()` shells out to the `nervous` CLI. Zero extra deps. |
| `native` | no | — | `native_publish()` writes directly to `debug.jsonl` (+ Zellij pipe if present), skipping the subprocess hop. |
| `listener` | no | `notify` | File-tail `debug.jsonl` with inode-rotation awareness (`listener::Listener`). |
| `streams` | no | `redis`, `tokio` | **Real Redis Streams consumer groups** — bootstrap, `XREADGROUP` read loop, `XACK`, `XAUTOCLAIM` reaper, `XADD` publisher. See below. |
| `streams-live-tests` | no | (implies `streams`) | Enables the `streams` module's live-Redis integration test suite (`cargo test --features streams-live-tests`). Not needed to *use* `streams` — only to run its live tests. |

`streams` is independent of `subprocess`/`native`/`listener` and does not
pull in `redis`/`tokio` unless enabled, so existing shell/file-tail
consumers of this crate are unaffected.

## Why `streams` exists

`publish()` and `native_publish()` get an event *onto* the bus (subprocess
or file-tail into `debug.jsonl`, which `adapters/redis-mirror/mirror.py`
then mirrors into Redis). Neither gives you a way to *consume* a Redis
Stream as one of several competing workers with delivery guarantees —
there's no consumer-group bootstrap, no `XREADGROUP`, no ack, no recovery
path for a worker that reads an entry and then crashes before acking it.

Three independent, hand-rolled ports of that missing piece already exist:

- **tengine** — `tengine-dgc-hal/src/silo/nbus_redis.rs` (`RedisStreamConsumer`,
  blocking/thread-based). This is the reference this module was extracted
  from.
- **hearth** — `crates/hearth-api/src/nbus_consumer.rs` (async, `tokio`) —
  ported from tengine's pattern this week; **this is the closest real-world
  analogue to this module's API** and the best migration reference if
  you're adopting `streams` in an async service.
- **tachyonac-engine** — `subscriber.go` (Go) — a third port of the same
  reaper pattern.

All three converge on the same conventions: entries carry the full
CloudEvents-lite envelope in a `_raw` field, and the reaper reclaims PEL
entries idle longer than `REAP_MIN_IDLE_MS = 120_000` (2 minutes). This
module keeps both.

**This is foundation work.** hearth/tengine/tachyonac-engine are NOT being
migrated onto this module yet — that's an explicit follow-up wave. Today
this just gives new consumers (and eventually those three, later) a real,
tested, non-bespoke place to get Streams support from.

## API surface (`streams` module)

```rust
use nbus::streams::{StreamConsumer, StreamsPublisher, PublishOptions, RunLoopOptions, run_consumer_loop};
use std::time::Duration;
use serde_json::json;
```

### Publish (`XADD`)

```rust
let mut publisher = StreamsPublisher::connect("redis://127.0.0.1:6379").await?;
let entry_id = publisher
    .publish("nbus:my.stream.v1", "my.event.type.v1", &json!({"k": "v"}))
    .await?;
```

- Envelope shape matches `publish()`/`native_publish()`
  (`{specversion,id,source,type,time,datacontenttype,data}`), stored whole
  in the `_raw` field, plus flat `type`/`event_id`/`timestamp`/`source`
  fields for anything that scans field-level entries without parsing JSON.
- `publish_with(..., &PublishOptions { maxlen: Some(50_000) })` applies an
  approximate `MAXLEN` trim, matching `redis-mirror`'s convention.

### Consume (`XREADGROUP` / `XACK` / `XAUTOCLAIM`)

```rust
let mut consumer = StreamConsumer::connect(
    "redis://127.0.0.1:6379",
    "nbus:my.stream.v1",
    "my-consumer-group",
    "my-consumer-1",
).await?; // idempotently bootstraps the group (XGROUP CREATE ... $ MKSTREAM)

loop {
    let entries = consumer.read_new(16, Duration::from_secs(2)).await?;
    for entry in entries {
        if let Some(data) = entry.data() {
            // ... handle it ...
        }
        consumer.ack(&entry.id).await?;
    }
}
```

- `StreamConsumer::connect` bootstraps the group; a `BUSYGROUP` reply
  (group already exists) is treated as success, so every process can call
  `connect` on startup unconditionally.
- `read_new(count, block)` — blocking `XREADGROUP ... >`, new entries only.
- `ack(id)` / `ack_many(ids)` — `XACK`.
- `pending_count()` — best-effort `XPENDING` summary (returns `0` on error;
  safe to call from a heartbeat/metrics path).
- `reap_stale(min_idle)` — `XAUTOCLAIM`: reclaims PEL entries idle longer
  than `min_idle` under this consumer's name. Run this periodically (or at
  startup) so a crashed sibling consumer's unacked entries eventually come
  back around instead of sitting in the PEL forever. Production callers
  should pass `Duration::from_millis(streams::REAP_MIN_IDLE_MS)`
  (2 minutes, matching tengine/hearth); tests can pass a much shorter window.

### Convenience loop

`run_consumer_loop` wires `reap_stale` + `read_new` + a handler + `ack` into
the loop every consumer ends up hand-rolling:

```rust
run_consumer_loop(
    &mut consumer,
    RunLoopOptions::default(),
    |entry| async move {
        // handle `entry`; return true to ack, false to leave pending
        // (a later reap sweep will redeliver it)
        true
    },
    || should_shut_down.load(Ordering::Relaxed),
)
.await?;
```

This is optional — call the primitives directly (as hearth's
`nbus_consumer.rs` does) if you need different scheduling, e.g. reaping
once at startup before joining the live loop.

## Testing

```bash
cargo test                              # default features — no extra infra
cargo test --features streams           # + streams module's pure parsing tests
cargo test --features streams-live-tests -- --test-threads=1   # + live Redis
```

The live suite talks to a real Redis/Valkey instance — point
`NERVOUS_REDIS_TEST_URL` at one, or rely on the default
(`redis://127.0.0.1:6379/15`, db 15, deliberately separate from the db 0
that a locally-running nervous-bus/hearth stack mirrors production traffic
into). Every test uses a randomly-suffixed stream/group name and cleans up
its stream key on completion. `--test-threads=1` avoids cross-test PEL
timing flakiness on a loaded box; the suite is fast (well under a second)
so serializing it costs nothing.

The live suite covers the full lifecycle end to end, not just reply
parsing: idempotent group bootstrap, publish→consume→ack, multi-entry
`ack_many`, and — the case the reaper exists for — a consumer that reads an
entry and never acks it, reclaimed by a second consumer via
`XAUTOCLAIM`/`reap_stale`, plus the negative case (a freshly-delivered entry
must NOT be reclaimed under the production idle window).

## Migrating an existing bespoke consumer

If you're maintaining one of the three existing hand-rolled ports and want
to move onto this module (not required yet — see "foundation work" above),
hearth's `crates/hearth-api/src/nbus_consumer.rs` is the closest match:
same async `redis`+`tokio` stack, same `_raw`/`XAUTOCLAIM`/`REAP_MIN_IDLE_MS`
conventions, just inlined into that service instead of factored out as a
crate. Diffing its `start_redis_consumer`/`reap_stale_pel`/
`process_stream_entry` functions against `StreamConsumer`/`run_consumer_loop`
here should make the mapping obvious.
