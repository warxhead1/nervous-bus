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

## Non-goals

- No span trees, no OTEL collector, no sampling flags semantics
  (`00`/`01` accepted, ignored). If we ever want real OTEL export,
  `traceparent` is already the right wire format — that is the point of
  adopting the standard now.
