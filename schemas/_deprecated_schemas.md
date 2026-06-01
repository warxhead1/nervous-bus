# Deprecated schemas

Schemas listed here remain in the directory for historical reference and to
avoid breaking any tooling that globs `bus.bead.*.v1.json`, but they are NOT
emitted by any current producer and NOT consumed by any current consumer.

Each deprecated schema also carries a top-level `"deprecated": true` field and
its `description` is prefixed with a `DEPRECATED (<date>, <bead-id>)` marker.

| Schema file                       | Deprecated on | Bead              | Superseded by                                                                 |
|-----------------------------------|---------------|-------------------|-------------------------------------------------------------------------------|
| `bus.bead.created.v1.json`        | 2026-05-16    | nervous-bus-immg  | (none — bead creation flows through bd hooks, not the bus)                    |
| `bus.bead.updated.v1.json`        | 2026-05-16    | nervous-bus-immg  | (none — bead metadata flows through bd hooks, not the bus)                    |
| `bus.bead.pr_opened.v1.json`      | 2026-05-16    | nervous-bus-immg  | `bus.bead.lifecycle.v1` with `event_type="pr_opened"`                         |
| `bus.bead.closed.v1.json`         | 2026-05-16    | nervous-bus-immg  | `bus.bead.lifecycle.v1` with `event_type="bead_closed"`                       |
| `bus.bead.scored.v1.json`         | 2026-05-16    | nervous-bus-immg  | derived from `bus.bead.lifecycle.v1` (pr_merged/falsified) + `bus.bead.bench_completed.v1` |

## Canonical channels

- `bus.bead.lifecycle.v1` — composite stream, discriminated by `event_type`.
  Producer: hearth-loom. Consumer: deer-flow Forge (dispatches by event_type).
- `bus.bead.bench_completed.v1` — per-channel bench result with baseline /
  treatment metric, optional CI, and a `passes_threshold` gate flag.

## Why mark instead of delete?

We chose the least-disruptive path: zero-risk for any external tool that
already pulled these schemas (e.g. for typed code-gen) and a clear signal to
new consumers via the top-level `deprecated: true` field.
