# sdk/go — nervous-bus Go SDK

A typed, native Go client for the nervous-bus Redis Streams transport:
consumer-group delivery (XREADGROUP/XACK), XAUTOCLAIM-based recovery of
pending entries orphaned by a crashed consumer, a publisher that emits
conformant CloudEvents-lite envelopes, and glob-style channel routing
(`*` = one segment, `#` = any suffix).

**Origin reference.** This package is a standalone port of
[`tachyonac-engine`](https://github.com/warxhead1/tachyonac-engine)'s
`internal/nbus` package — the most complete Go nervous-bus implementation in
the ecosystem as of this writing (`reapStale`/XAUTOCLAIM PEL recovery,
`processMessage` shared dispatch+XAck, a working XREADGROUP consumer-group
loop, and a publisher). Read `internal/nbus/subscriber.go` and
`internal/nbus/nbus_test.go` there if you want the reference this SDK was
generalized from, including the reaper tests
(`TestReapStale_ReclaimsOrphanedPELEntry`, `TestReapStale_NoOpWhenNothingStale`,
`TestReapStale_DoesNotReclaimFreshlyDeliveredEntry`) this SDK's own test suite
mirrors.

This is foundation work: `tachyonac-engine` and `tengine` are **not**
migrated onto this module yet. That's a separate follow-up wave once this
lands and is reviewed.

## Install

```bash
go get github.com/warxhead1/nervous-bus/sdk/go
```

Import the `nbus` package:

```go
import "github.com/warxhead1/nervous-bus/sdk/go/nbus"
```

## Wire format

Every event is a CloudEvents-lite envelope (matches `schemas/*.json` in the
repo root — every schema's `required` set is exactly these fields):

```json
{
  "specversion": "1.0",
  "id": "01J...",            // ULID, 26-char Crockford Base32
  "source": "/my-service",
  "type": "bus.notify.v1",
  "subject": "optional-entity-id",
  "time": "2026-07-03T12:00:00Z",  // RFC3339 UTC
  "datacontenttype": "application/json",
  "data": { "...": "..." }
}
```

Publishing writes the full envelope JSON to the `_raw` field of two Redis
Streams:

- `nbus:<channel>` (approx maxlen 10,000) — the typed stream, for consumers
  filtering on one channel.
- `nbus:all` (approx maxlen 50,000) — the canonical fanout stream every
  consumer-group subscriber reads by default.

Helper fields (`type`, `source`, `timestamp`, `event_id`) are also written
alongside `_raw` so tooling can filter/sort without decoding JSON first.

## Publishing

```go
rdb := redis.NewClient(&redis.Options{Addr: "localhost:6379"})
pub := nbus.NewPublisher(rdb, nbus.Config{Source: "/my-service"}, logger)

// Fire-and-forget — never blocks the caller, errors are logged and swallowed.
pub.Publish("bus.notify.v1", map[string]any{"message": "hello"})

// Blocking — use when the caller must confirm delivery (e.g. before shutdown).
if err := pub.PublishSync(ctx, "bus.notify.v1", payload); err != nil {
    log.Fatal(err)
}
```

A `nil *nbus.Publisher` is a safe no-op for both `Publish` and `PublishSync`,
so callers can wire in an optional publisher without nil-checking at every
call site.

## Subscribing (consumer group)

```go
sub := nbus.NewSubscriber(rdb, nbus.Config{
    Stream:   "nbus:all",  // default when empty
    Group:    "my-service",
    Consumer: "",          // defaults to os.Hostname()
}, logger)

sub.On("bus.notify.*", func(ctx context.Context, env nbus.Envelope) error {
    var payload MyPayload
    if err := env.Decode(&payload); err != nil {
        return err
    }
    // handle payload
    return nil
})

sub.Run(ctx) // blocks until ctx is cancelled
```

`Run` creates the consumer group if it doesn't exist, reclaims anything
orphaned in the pending-entries list (PEL) from a prior crash before reading
new entries, then loops on `XREADGROUP` — dispatching each entry to every
handler whose pattern matches and XACKing it, regardless of handler error.
A periodic sweep (every `nbus.ReapInterval`, default 30s) re-runs the
XAUTOCLAIM reclaim so entries orphaned by a consumer that crashes mid-loop
don't leak in the PEL forever.

### Pattern matching

- `*` matches exactly one dot-delimited segment: `loom.lifecycle.*` matches
  `loom.lifecycle.started` but not `loom.lifecycle.started.v1`.
- `#` matches any suffix, including zero segments: `loom.#` matches
  `loom.lifecycle.started.v1` and bare `loom`.

### PEL recovery tuning

```go
const (
    ReapMinIdle   = 2 * time.Minute  // idle time before an entry is reclaimable
    ReapInterval  = 30 * time.Second // periodic sweep cadence inside Run
    ReapBatchSize = 100              // XAUTOCLAIM COUNT per sweep
)
```

These are exported so a caller can reason about worst-case redelivery
latency; they are not currently configurable per-Subscriber (open a bead if
you need per-service tuning).

## Testing

```bash
cd sdk/go
go build ./...
go vet ./...
go test ./...
```

Tests use [`miniredis`](https://github.com/alicebob/miniredis) — no real
Redis required. They follow `tachyonac-engine`'s `internal/nbus/nbus_test.go`
conventions, including the three reaper tests that pin down the additive,
non-racing nature of PEL recovery:

- `TestReapStale_ReclaimsOrphanedPELEntry` — a crashed consumer's unacked
  entry is reclaimed by a different consumer identity once idle past
  `ReapMinIdle`, dispatched, and ACKed.
- `TestReapStale_NoOpWhenNothingStale` — an empty PEL sweep dispatches
  nothing and doesn't panic.
- `TestReapStale_DoesNotReclaimFreshlyDeliveredEntry` — an entry still
  within its normal in-flight processing window (idle time under
  `ReapMinIdle`) is never reclaimed out from under a live handler.

## Public API summary

| Symbol | Purpose |
|---|---|
| `nbus.Envelope` | CloudEvents-lite envelope struct (`Decode` unmarshals `Data`) |
| `nbus.NewEnvelope(source, channel, data)` | Build an envelope with a fresh ULID id + RFC3339 UTC time |
| `nbus.Config` | `{Stream, Source, Group, Consumer}` — shared publisher/subscriber config |
| `nbus.Publisher` / `nbus.NewPublisher(rdb, cfg, logger)` | XADD to `nbus:<channel>` + `nbus:all` |
| `(*Publisher) Publish(channel, data)` | Fire-and-forget publish |
| `(*Publisher) PublishSync(ctx, channel, data) error` | Blocking publish |
| `nbus.Subscriber` / `nbus.NewSubscriber(rdb, cfg, logger)` | Consumer-group reader |
| `(*Subscriber) On(pattern, handler)` | Register a glob-pattern handler |
| `(*Subscriber) Run(ctx)` | Blocking XREADGROUP + periodic XAUTOCLAIM reap loop |
| `nbus.Handler` | `func(ctx context.Context, env nbus.Envelope) error` |
| `nbus.ReapMinIdle` / `ReapInterval` / `ReapBatchSize` | PEL recovery tuning constants |

## Not yet implemented

- Per-Subscriber configurable reap tuning (currently package-level constants).
- Schema validation against `schemas/*.json` at publish time (the shell SDK
  and `redis-mirror` validate; this SDK does not yet — open a bead if a
  consumer needs it enforced client-side).
- Dead-letter routing to `nbus:bus.dead_letter` on handler error (handlers
  currently own their own error/retry strategy).
