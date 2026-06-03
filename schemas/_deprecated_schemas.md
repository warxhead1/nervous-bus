# Deprecated schemas

Schemas listed here remain in the directory for historical reference and to
avoid breaking any tooling that globs `bus.bead.*.v1.json`, but they are NOT
emitted by any current producer and NOT consumed by any current consumer.

Each deprecated schema also carries a top-level `"deprecated": true` field and
its `description` is prefixed with a `DEPRECATED (<date>, <bead-id>)` marker.

| Schema file                                | Deprecated on | Bead              | Superseded by                                                                 |
|--------------------------------------------|---------------|-------------------|-------------------------------------------------------------------------------|
| `bus.bead.created.v1.json`                 | 2026-05-16    | nervous-bus-immg  | (none — bead creation flows through bd hooks, not the bus)                    |
| `bus.bead.updated.v1.json`                 | 2026-05-16    | nervous-bus-immg  | (none — bead metadata flows through bd hooks, not the bus)                    |
| `bus.bead.pr_opened.v1.json`               | 2026-05-16    | nervous-bus-immg  | `bus.bead.lifecycle.v1` with `event_type="pr_opened"`                         |
| `bus.bead.closed.v1.json`                  | 2026-05-16    | nervous-bus-immg  | `bus.bead.lifecycle.v1` with `event_type="bead_closed"`                       |
| `bus.bead.scored.v1.json`                  | 2026-05-16    | nervous-bus-immg  | derived from `bus.bead.lifecycle.v1` (pr_merged/falsified) + `bus.bead.bench_completed.v1` |
| `bus.tengine.bridge_path_verified.v1.json` | 2026-06-02    | kernel-unify wave | `bus.tengine.bridge.path_verified.v1` (dot-separated canonical)              |

## Kernel channel collapse (kernel-unify wave, 2026-06-02)

The 72 per-domain kernel schemas (`<domain>.kernel.started`, `<domain>.candidate.evaluated`,
… for the 8 domains `[sph,sdf,noise,phase,terrain,thermal,latent,tsp]`) were
**hard-deleted** (not marked) and collapsed into 9 unified `kernel.*` channels
plus the new `pulse.kernel.snapshot.v1` rollup. The unified channels carry a
required `data.domain` discriminator. The legacy files are gone from the repo;
during the merge window producers may still emit `<domain>.*` channels →
redis-mirror treats them as unknown channels (warn + per-type metric, never
dropped). Consumers in this repo (`tools/tsp_watch.py`, `tools/tsp_analyze.py`)
tolerate both spellings. See `KERNEL_CONTRACT_SPEC` for the authoritative shape.

## Duplicate channel-name spellings (document, do not delete yet)

### `bus.tengine.bridge_path_verified.v1` vs `bus.tengine.bridge.path_verified.v1`

Two spellings of the same intent (a tengine bridge heightmap path-verification
event) with **different data shapes**:

- `bus.tengine.bridge.path_verified.v1` (dotted) — **CANONICAL.** Full
  CloudEvents envelope; `additionalProperties:false`; produced by
  `tengine-nervous-bus-client emit_bridge_path_verified()`. Follows the
  dot-separated channel-naming convention.
- `bus.tengine.bridge_path_verified.v1` (underscore) — **DEPRECATED** (marked
  `deprecated: true`). Bare data-payload schema with a different field set
  (`silo/source/frame/hm_sample`). Kept rather than hard-deleted because
  tengine is a **sibling repo** — a producer there may still emit the underscore
  spelling. Audit tengine's emitters and migrate them to the dotted channel,
  then `git rm` this file.

### `hearth-loom.ac.verified.v1` vs `bus.hearth-loom.ac.verified.v1`

Two acceptance-criterion-verification schemas with the **same data shape** but
**different `source` constraints** and channel prefixes:

- `bus.hearth-loom.ac.verified.v1` — **RECOMMENDED CANONICAL.** `source` is
  `const: "/hearth-loom"`; uses the `bus.*` cross-bus prefix consistent with
  the other `bus.*` bead-lifecycle channels.
- `hearth-loom.ac.verified.v1` — no `bus.` prefix; `source` unconstrained
  (free string). Richer field docs (adds `stderr`, `correlation_id`, length
  caps) but otherwise equivalent.

**Both files are intentionally LEFT IN PLACE.** Per the kernel-unify spec, do
not delete either until a hearth-loom sibling-repo audit confirms which channel
its executor actually emits. New consumers should subscribe to
`bus.hearth-loom.ac.verified.*` (the recommended canonical) but tolerate the
un-prefixed spelling during transition.

## Canonical channels

- `bus.bead.lifecycle.v1` — composite stream, discriminated by `event_type`.
  Producer: hearth-loom. Consumer: deer-flow Forge (dispatches by event_type).
- `bus.bead.bench_completed.v1` — per-channel bench result with baseline /
  treatment metric, optional CI, and a `passes_threshold` gate flag.

## Why mark instead of delete?

We chose the least-disruptive path: zero-risk for any external tool that
already pulled these schemas (e.g. for typed code-gen) and a clear signal to
new consumers via the top-level `deprecated: true` field.
