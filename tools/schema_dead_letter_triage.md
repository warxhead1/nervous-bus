# Dead-Letter Triage — bus.agent.activity.v1 schema hygiene

Generated: 2026-05-30  
Source: `python3 tools/schema_hygiene.py --report` (301 dead letters, 19 groups)

## Summary

| Category | Groups | Events |
|---|---|---|
| FIXED (this PR) | 5 | 30 |
| PRODUCER-SIDE | 1 | 2 |
| STALE/EXPIRING | 4 | 224 |
| NEEDS INVESTIGATION | 9 | 45 |

---

## FIXED — bus.agent.activity.v1 (this PR)

Schema widened: added `started` and `permission_requested` to event enum.

| Group | Count | Value rejected |
|---|---|---|
| bus.agent.activity.v1 | 5 | `started` |
| bus.agent.activity.v1 | 4 | `started` |
| bus.agent.activity.v1 | 1 | `started` |
| bus.agent.activity.v1 | 15 | `permission_requested` |
| bus.agent.activity.v1 | 5 | `permission_requested` |

**Total fixed: 30 events.** New enum: `["tool_call","tool_return","error","heartbeat","ended","started","permission_requested"]`

---

## PRODUCER-SIDE — Codex harness gap

| Group | Count | Detail |
|---|---|---|
| bus.agent.activity.v1 | 2 | event=`codex` (agent name used as event type) |

**Action:** Codex CLI adapter hooks need updating to emit proper event types
(`started`, `tool_call`, `ended`) instead of emitting the agent name as the
`event` field. Follow up with Codex harness maintainer. Do NOT add `"codex"`
to the enum — it is not a meaningful event discriminator.

---

## STALE/EXPIRING — will drain without intervention

| Group | Count | Reason |
|---|---|---|
| bus.agent.activity.v1 (medium confidence) | 192 | Stale `heartbeat` entries. `heartbeat` was added to schema in v0.3.0; these pre-date that fix. Tool labels these medium-confidence. |
| bus.agent.activity.v1 | 22 | `ended` rejected — `ended` is already in current schema; pre-dates its addition. |
| bus.agent.activity.v1 | 3 | Same as above (second `ended` group) |
| tsp.kernel.started.v1 | 8 | Double-envelope smoke-test events from before the kernel was wired to the bus. Pre-date current emission path. |
| tsp.kernel.started.v1 | 1 | Same source as above (second group) |

**Total stale: 226 events.** No schema action needed; these expire as the
Redis stream rolls.

---

## NEEDS INVESTIGATION — no schema edits without root-cause

| Group | Count | Hypothesis |
|---|---|---|
| bus.pattern.signal.v1 | 16 | Unknown `event` value emitted by deer-flow signal-router; `signal_type` enum may be the culprit, or a new event discriminator field was added without schema update. |
| loom.shared-session.v1 | 10 | Violation on `data.event`; likely a new sleeve/tool-call event type added to hearth-loom without schema update. |
| bus.agent.activity.v1 (empty parse) | 8 | Violation detail didn't parse to a named value — likely a non-string or null in the `event` field, or a missing required field. |
| loom.lifecycle.v1 | 4 | Required field missing or unknown `phase` value; may reflect a new terminal disposition added to hearth-loom. |
| bus.dead_letter | 2 | A `bus.dead_letter` event itself failed schema validation — possibly a `failure_reason` value not in the enum, or a truncated payload exceeding `maxLength`. |
| bus.hearth.session.permission.requested.v1 | 1 | Missing required field; hearth-session publisher may have dropped a field in a recent refactor. |
| bus.hearth.session.permission.responded.v1 | 1 | Unknown `decision` value; `resolved` was recently added — may be a pre-fix stale entry, or a new decision variant. |
| tengine.shadergen.multiverse.v1 | 1 | Missing required field (`sub`, `rows`, `cols`, or `n`); tengine emitter may have changed its payload shape. |

**Recommended investigation order:** `bus.pattern.signal.v1` (16 events, high
volume), then `loom.shared-session.v1` (10 events). The `bus.agent.activity.v1`
unparseable group (8) likely resolves after the FIXED events expire from the stream.

---

*Do NOT touch tsp.\* schemas — owned by a separate PR (feat/tsp-horizons).*
