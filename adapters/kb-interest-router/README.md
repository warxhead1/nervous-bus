# adapters/kb-interest-router/ — DESIGN NOTE (not implemented — blocked)

## What this was supposed to be

A small adapter modeled 1:1 on `adapters/signal-router/router.py`:
consume `kb.entry.created.v1` (confirmed live — see the 2026-08-23
kb-recall-loop emission audit, `schemas/kb.entry.created.v1.json`), match
the event's `project` field against live `agent.session.heartbeat.v1`
sessions for the SAME project, and republish a `bus.notify.v1`-shaped
event so an active agent session gets nudged that a new vault entry landed
in a project it's currently working.

## Why it's not built

Join key was specified as **the plain `project` string field only** — no
derived/indirect key. `kb.entry.created.v1` carries `project` as a required
field (source-verified: `src/commands/ingest.rs:390`, `landmark.rs:136`,
`watch.rs:658`/`:1321` in the kb repo all set it explicitly).

`agent.session.heartbeat.v1` does **not** carry a `project` field.
`schemas/agent.session.heartbeat.v1.json` declares `additionalProperties:
false` with this exact property set: `pane_id_qualified`, `session_id`,
`ts`, `healthy`, `agent_type`, `focus_bead`, `current_phase`,
`subagents_in_flight`, `recent_tool_calls`, `context_percent`,
`parent_main_session_id`. No `project`, no `project_short`, nothing
project-shaped. This was checked directly against the schema file, not
inferred from a producer.

Per the dispatch instruction, a missing join key on the consumer side
stops the build here rather than forcing a substitute join (e.g. matching
on the CloudEvents envelope's `source` path, which by convention embeds a
project-like segment such as `/claude-host/<project>` — see
`bus.rs::make_envelope`'s `source` field in kb, and the live sample
`"source":"/claude-host/unreal-battlebots-gamedev"` observed during the
phase-1 live-verification pass). That convention is real but it is NOT the
schema-declared `project` field the task specified, and CloudEvents
`source` conventions vary by producer/bridge in ways `project` does not —
substituting it here would be inventing a join key, not using the one
specified.

## What would unblock this

One of:

1. Add an optional `project` field to `agent.session.heartbeat.v1`
   (schema change — the per-CLI session bridges, e.g.
   `claude-session-bridge`, would need to start populating it; this is a
   schema-first change per this repo's CLAUDE.md, so the schema lands
   before any producer emits it).
2. Accept `pane_id_qualified` + a `nervous schemas`-style lookup adapter
   (see `adapters/lookup/README.md`'s contract) to resolve
   `pane_id_qualified` → project via CCM/hearth-api, and use THAT as the
   join key instead of a raw event field. This is a materially different
   design (adds a lookup-adapter dependency + a resolution hop) and was
   out of scope for this pass.

## Emission-side status (for whoever picks this up)

`kb.entry.created.v1` is fully live and carries what this adapter would
need on the producer side:

| Field | Present | Notes |
|---|---|---|
| `project` | yes, required | plain string, e.g. `"kb"`, `"tachyonac-engine"` |
| `entry_id` | yes, required | UUID |
| `title` | yes, required | |
| `source_type` | yes, required | enum |
| `confidence` | yes, required | |

No code changes needed on the `kb.entry.created.v1` producer side to
support this adapter once a join key exists on the consumer side.
