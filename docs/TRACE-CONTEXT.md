# Trace-context threading (A4) — design

Status: **BINDING** for new causal-chain features; adopted 2026-07-19
(harness-engineering adoption map, Tier 1 item A4).

## Problem

Multi-step flows (steering question → phone card → answer → downstream
write; posting evaluated → inbox card → decision → tracker write) are
correlated today by hand-joining channel-specific ids (`data.id`,
`tracker_row`, …). There is no single key that names the *causal chain*,
so "show me everything that happened because of X" is a manual archaeology
exercise across `debug.jsonl`.

## Design

Adopt the **CloudEvents Distributed Tracing extension** verbatim rather
than inventing fields:

- `traceparent` (optional, envelope-level): W3C trace-context string
  `00-<32 lowercase hex trace-id>-<16 lowercase hex parent/span-id>-<2 hex flags>`.
- `tracestate` (optional, envelope-level): reserved; no producer sets it
  yet.

Rules:

1. **Envelope-level, not payload-level.** `traceparent` sits beside
   `id`/`source`/`type` in the CloudEvents-lite envelope. Channel `data`
   schemas are untouched — no per-channel schema churn.
2. **Mint at the chain root.** The first event of a causal chain
   generates a fresh trace-id + span-id. Every downstream event in the
   same chain reuses the trace-id with a fresh span-id (its own
   envelope `id`-derived span is fine; we do not build a span tree —
   flat trace membership is the 90% win).
3. **Persist alongside the correlating state.** Whatever state object
   already carries the chain's correlation key (a steering-queue entry,
   a tracker row reference) stores the `traceparent` so later writers
   can rejoin the chain.
4. **Optional forever.** Producers that don't thread context emit no
   field at all (never null). Consumers must treat absence as normal.
5. **Additive envelope fields vs strict envelope schemas.** Most
   channels validate `data` only, so `traceparent` is transparent to
   them. A minority of schemas validate the FULL envelope with
   `additionalProperties: false` (e.g. `autobench.budget.gauge.v1`,
   `bus.intrinsic.marker.v1`, `greenhouse.*`) — redis-mirror would
   dead-letter a traced event on those channels. Therefore: **a
   producer may only set `traceparent` on an envelope-validated channel
   after adding the optional property to that channel's schema.**
   The mirror's `_raw` field carries the full envelope through to Redis
   unmodified, so no mirror change is needed.

## Consumption surface (ships with this, per the no-¾-loops rule)

`nervous trace <trace-id-or-prefix>` — greps `debug.jsonl` (and rotated
windows if present) for envelopes whose `traceparent` contains the
trace-id, sorts by `time`, and prints one line per event:
`time  type  source  id`. This is the read path that makes the field
worth writing.

## Exemplar chain (implemented with this doc): the steering rail

- `POST /api/steering` (job-search-se `server/index.js` →
  `nervousBus.js`) **mints** a traceparent, emits it on
  `career-ops.steering-queue.requested.v1`, and persists it on the
  queue entry in `data/steering-queue.json`.
- `POST /api/steering/:id/answer` re-emits the stored traceparent on
  `career-ops.steering-queue.answered.v1` (fresh span-id).
- `DELETE /api/steering/:id` does the same on
  `career-ops.steering-queue.dismissed.v1`.

Result: `nervous trace <id>` shows ask → answer/dismiss as one chain,
including any future consumers (hearth-api notification lifecycle) that
propagate the header.

## Adoption order (later, per-lane, each with its own commit)

1. hearth-hermes: `career-ops.posting.evaluated.v1` → inbox card →
   decision verb → tracker write (pairs with H7 disposition receipts —
   the receipt stores the traceparent).
2. kb `src/bus.rs` `make_envelope()` — optional trace argument.
3. hearth-loom PR pipeline (`hearth-loom.pr.opened/merged.v1`).
4. autobench envelope-validated channels — schema property first (rule 5).

## Rust SDK surface (`sdk/rust`, bead nervous-bus-gvmq)

`nbus::Envelope` carries the extension as an optional, private field with
a validated public API:

```rust
use nbus::{Envelope, TraceContextError};

let envelope = Envelope::new("/kb", "kb.entry.created.v2", &payload)?
    .with_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")?;

assert_eq!(envelope.traceparent(), Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"));
```

- `Envelope::traceparent(&self) -> Option<&str>` — chain membership, or
  `None` for an untraced envelope.
- `Envelope::with_traceparent(self, &str) -> Result<Self, TraceContextError>`
  — consuming builder; the only way to set the field.
- `TraceContextError` — a plain `enum` implementing `Display` +
  `std::error::Error` by hand, with no error-derive dependency, so a
  consumer crate can match on the rejection reason without inheriting
  this crate's dependency graph.

### Accepted grammar (strict, v00 only)

Exactly `00-<32 lowercase hex>-<16 lowercase hex>-<2 lowercase hex>`.
Rejected: any version other than `00`, uppercase hex, wrong field
widths, a wrong number of hyphen-separated fields, and the two W3C
all-zero sentinels (`trace-id` of 32 zeroes, `span-id` of 16 zeroes).
Trace *flags* accept any two lowercase hex digits — per the non-goals
below, the wire shape is enforced and the sampling semantics are not.

### Where validation binds

At **both** boundaries — `with_traceparent` and deserialization — from a
single shared validator, so a header can never enter an `Envelope` by a
path the other would have refused. Concretely, `serde_json::from_str::<Envelope>`
FAILS on a present-but-invalid `traceparent`, including an explicit
`null` and any non-string JSON type. Only *absence* of the key is
accepted (rule 4 above: producers that don't thread context emit no
field at all).

### Preservation, not regeneration

The supplied header is stored and re-emitted byte-for-byte.
Serializing → persisting → deserializing → re-publishing an envelope
keeps it in the same chain with the same span; nothing is regenerated on
replay. That is what makes rule 3 (persist alongside the correlating
state) work for a durable Rust producer.

A **new** event that needs a fresh span-id mints one at the call site —
the SDK deliberately does not. It performs no environment reads (there
is no ambient `TRACEPARENT` pickup) and issues no trace implicitly; an
untraced `Envelope::new` serializes to exactly the bytes it did before
this field existed, with the key absent. `Envelope::id()` (a ULID, 80
bits of entropy) is available at the call site as span entropy.

### Backwards compatibility

`Envelope::new`, `make_envelope`, and every existing getter keep their
signatures unchanged. `tracestate` is not supported.

### Channel schemas are NOT modified by this SDK change

Restating rule 5 above, because it is the operational precondition for
using this API in production:

- **Data-only channels** (the majority — the schema validates `data`)
  accept the optional envelope extension transparently. Nothing to do.
- **Strict full-envelope channels** — those validating the whole
  envelope with `additionalProperties: false` (e.g.
  `autobench.budget.gauge.v1`, `bus.intrinsic.marker.v1`,
  `greenhouse.*`) — would DEAD-LETTER a traced event. Emitting a
  traceparent on one of those channels requires a **versioned schema
  migration adding the optional `traceparent` property first**. This
  bead adds no schema changes and no producer sets the field yet, so no
  channel's validation behaviour changes.

## Non-goals

- No span trees, no OTEL collector, no sampling flags semantics
  (`00`/`01` accepted, ignored). If we ever want real OTEL export,
  `traceparent` is already the right wire format — that is the point of
  adopting the standard now.
