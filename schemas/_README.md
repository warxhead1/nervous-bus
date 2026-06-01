# nervous-bus schemas

One JSON Schema file per channel event type, named `<type>.v<n>.json`.

## Authoring rules

1. **Schema first.** Add the schema BEFORE any publisher emits.
2. **Major versions only.** Breaking changes bump `v<n>` and ship as a new file. Old file stays with `"deprecated": true` until all consumers migrate.
3. **CloudEvents envelope is implicit.** Schemas describe the `data` payload only.
4. **Keep payloads small.** ≤ 10 fields, ≤ 2KB serialized. Big things go to durable storage; bus carries the reference.
5. **Carry `saga_id` where relevant.** Any event that belongs to a coordinated plan should include `saga_id` (from `bus.saga`) for correlation.

## Status legend

| Symbol | Meaning |
|---|---|
| ✓ live | Fired on the bus in the last 24h |
| ◐ live-quiet | Fired in last 7d but not last 24h (works, bursty) |
| ⚠ wired-not-deployed | Producer code + schema exist, but no service is currently running (deploy gap) |
| ◯ planned | Schema declared; producer is TBD or in-flight (tracking bead noted) |
| ✗ no-schema | Channel fires on bus but no schema in this repo (gap A — file the schema or kill the producer) |

Refresh the table by running `python3 sdk/python/classify_channels.py` (TODO; for now: `jq -R 'fromjson? | .type' ~/.cache/nervous-bus/debug.jsonl | sort | uniq -c`).

## Channels

| Schema file | Channel.event | Owner | Status |
|---|---|---|---|
| `agent.session.v1.json` | `agent.session` | shadow-binary wrappers + hearth-loom ccm | ✓ live — coding agent identity + heartbeat |
| `agent.session.heartbeat.v1.json` | `agent.session.heartbeat` | per-CLI session-bridge skill | ◯ planned — periodic liveness from registered sessions (tracker: nervous-bus-ql7g) |
| `agent.session.linked.v1.json` | `agent.session.linked` | per-CLI session-bridge skill + `nervous session link` | ◯ planned — peer/subordinate/council link declaration; carries human-initiated guarantee via `spawned_by` (tracker: nervous-bus-ql7g) |
| `agent.session.snapshot.v1.json` | `agent.session.snapshot` | per-CLI session-bridge skill | ◯ planned — on-demand full session-state dump in response to `agent.session.query.v1` / `nervous session show` (tracker: nervous-bus-ql7g) |
| `agent.message.v1.json` | `agent.message` | peer skill (peer-send/share/inbox) | ✓ live — A2A directed messaging |
| `bus.bead.created.v1.json` | `bus.bead.created` | nervous-bus / nbd wrapper | ⚠ wired-not-deployed — `sdk/shell/nbd` works but only fires when invoked as `nbd` (or `alias bd=nbd`); plain `bd` does not auto-route (tracker: nervous-bus-58m) |
| `bus.bead.pr_opened.v1.json` | `bus.bead.pr_opened` | hearth-loom dispatcher | ◯ planned — depends on loom-dq6b6 wiring the PR producer past the `loom/*` branch filter |
| `bus.bead.closed.v1.json` | `bus.bead.closed` | nbd wrapper | ⚠ wired-not-deployed — same gating as `bus.bead.created` (tracker: nervous-bus-58m) |
| `bus.bead.updated.v1.json` | `bus.bead.updated` | nbd wrapper | ⚠ wired-not-deployed — fires on `nbd update --claim` / `--status=` / generic updates; same `nbd` invocation gating as the rest of the bus.bead.* family |
| `bus.bead.scored.v1.json` | `bus.bead.scored` | deer-flow | ◯ planned — feedback signal for executor selection loop |
| `bus.saga.v1.json` | `bus.saga` | nervous-bus / Jarvis planner | ◯ planned — multi-bead plan correlation root |
| `bus.dead_letter.v1.json` | `bus.dead_letter` | nervous-bus plugin | ◐ live-quiet — only fires on malformed events (1 ever) |
| `bus.intrinsic.marker.v1.json` | `bus.intrinsic.marker` | any project (hearth, hearth-loom, deer-flow, etc.) | ◯ planned — standardized lifecycle + health markers for sysmap aggregation; replaces ad-hoc per-project event wiring for cycle/build/deploy/quality signals |
| `bus.dashboard.v1.json` | `bus.dashboard` | cc-bus-dashboard | ◯ planned — dashboard heartbeat / state-of-bus channel |
| `bus.redis-mirror.config.v1.json` | n/a (config schema, not an event) | adapters/redis-mirror | n/a — describes adapter config shape |
| `loom.lifecycle.v1.json` | `loom.lifecycle` | hearth-loom | ✓ live — phase transitions + cost deltas + terminal disposition |
| `loom.lifecycle.pr.v1.json` | `loom.lifecycle.pr` | hearth-loom | ◯ planned — PR opened/merged/failed/reverted (tracker: nervous-bus-5o8) |
| `loom.lifecycle.ci.v1.json` | `loom.lifecycle.ci` | hearth-loom | ◯ planned — CI run outcome (tracker: nervous-bus-5o8) |
| `loom.lifecycle.phase.v1.json` | `loom.lifecycle.phase` | hearth-loom | ◯ planned — agent-internal phase transitions (tracker: nervous-bus-5o8) |
| `loom.lifecycle.retry.v1.json` | `loom.lifecycle.retry` | hearth-loom | ◯ planned — retry / gate / AC evidence signals (tracker: nervous-bus-5o8) |
| `loom.plan.v1.json` | `loom.plan` | hearth-loom | ◯ planned — filed when human approves a plan |
| `loom.plan.step.v1.json` | `loom.plan.step` | hearth-loom | ◯ planned — per-bead execution progress |
| `hearth.device.state.v1.json` | `hearth.device.state` | adapters/hearth-bridge | ✓ live — nervous-hearth-bridge.service active since 2026-05-10; publishes device state + presence from Redis (tracker: nervous-bus-p82) |
| `hearth.presence.v1.json` | `hearth.presence` | adapters/hearth-bridge | ✓ live — same service, publishes aggregate occupancy/presence (tracker: nervous-bus-p82) |
| `hearth.router.decision.v1.json` | `hearth.router.decision` | hearth-nbus crate (in hearth repo) | ◯ planned — AI router model selection + fallback tracking |
| `hearth.ember.insight.v1.json` | `hearth.ember.insight` | hearth-cognitive (in hearth repo) | ◯ planned — cognitive insight events (tracker: hearth-4yzn) |
| `home-automation.news.article.v1.json` | `home-automation.news.article` | home-automation/hearth-brain | ✓ live — keyword-boost scoring (no external LLM); fires on articles with boost >= 1.2 |
| `tengine.session.frame.v1.json` | `tengine.session.frame` | tengine/silo_tester | ✓ live — fires when silo_tester runs with --emit-frame-jsonl (tracker: nervous-bus-ah1) |
| `tengine.silo.started.v1.json` | `tengine.silo.started` | adapters/silo-watcher | ✓ live — fires on ~/.tengine/sessions/silo_*/ dir creation |
| `tengine.silo.verify.v1.json` | `tengine.silo.verify` | adapters/silo-watcher | ✓ live — fires on verification_report.json appearance; pass/fail + FPS subset |
| `tengine.session.fps_drop.v1.json` | `tengine.session.fps_drop` | tengine/silo_tester | ✓ live — threshold-crossing when FPS drops below silo's min_fps (tracker: nervous-bus-8vw, closed as fixed) |
| `tengine.code.changed.v1.json` | `tengine.code.changed` | tengine hooks | ✓ live — but producer ships wrong shape (same tracker) |
| `deer-flow.audit.recommendation.v1.json` | `deer-flow.audit.recommendation` | deer-flow stack-tuner / auditor | ✓ live — auditor verdict + remediation (compact + full forms) |
| `deer-flow.metaprobe.cycle.v1.json` | `deer-flow.metaprobe.cycle` | deer-flow stack-tuner | ✓ live — compact rollup of cycle outcome |
| `deer-flow.stack-tuner.cycle.start.v1.json` | `deer-flow.stack-tuner.cycle.start` | deer-flow stack-tuner | ✓ live — opens cycle saga |
| `deer-flow.stack-tuner.cycle.done.v1.json` | `deer-flow.stack-tuner.cycle.done` | deer-flow stack-tuner | ✓ live — terminal cycle event with per-stage breakdown |
| `deer-flow.stack-tuner.stage.start.v1.json` | `deer-flow.stack-tuner.stage.start` | deer-flow stack-tuner | ✓ live — model invocation begins |
| `deer-flow.stack-tuner.stage.done.v1.json` | `deer-flow.stack-tuner.stage.done` | deer-flow stack-tuner | ✓ live — model invocation ends with cost/score/error |
| `deer-flow.stack-tuner.integrity.warn.v1.json` | `deer-flow.stack-tuner.integrity.warn` | deer-flow stack-tuner | ✓ live — out-of-band integrity warning during a stage |
| `deer-flow.cycle.snapshot.v1.json` | `deer-flow.cycle.snapshot` | deer-flow stack-tuner | ◯ planned — mid-cycle progress view (fires per stage transition; complements terminal cycle.done) |
| `deer-flow.council.session.v1.json` | `deer-flow.council.session` | deer-flow council | ◯ planned — unified council lifecycle on one channel (supplements council.started + council.completed) |
| `deer-flow.agent.thread.v1.json` | `deer-flow.agent.thread` | deer-flow dispatch | ◯ planned — deer-flow's own view of agent threads it spawns (distinct from agent.session external wrappers) |
| `deer-flow.audit.recommendation.snapshot.v1.json` | `deer-flow.audit.recommendation.snapshot` | deer-flow auditor | ◯ planned — recommendation lifecycle tracking after verdict (pending→actioned\|dismissed\|expired) |
| `deer-flow.tool.usage.v1.json` | `deer-flow.tool.usage` | deer-flow / token_usage_middleware | ◯ planned — per-LLM-call tokens + cost + latency + fallback (tracker: nervous-bus-277) |
| `deer-flow.sandbox.risk.v1.json` | `deer-flow.sandbox.risk` | deer-flow / sandbox_audit_middleware | ◯ planned — per-command risk classification + action (tracker: nervous-bus-4c7) |
| `deer-flow.sandbox.result.v1.json` | `deer-flow.sandbox.result` | deer-flow / sandbox_audit_middleware | ◯ planned — verdict + latency + output from sandbox execution (tracker: nervous-bus-4c7) |
| `deer-flow.subagent.lifecycle.v1.json` | `deer-flow.subagent.lifecycle` | deer-flow / SubagentExecutor | ◯ planned — LangGraph subagent state transitions (tracker: nervous-bus-dca) |
| `deer-flow.bead.filed.v1.json` | `deer-flow.bead.filed` | deer-flow / gateway beads router | ◯ planned — bead creation broadcast at request-handler boundary (tracker: nervous-bus-lio) |

## Gap A: channels firing without a schema

These types appear on the bus but have no schema file. Either declare a schema or remove the producer.

| Channel.event | 7d count | Sources | Action |
|---|---|---|---|
| `bus.test.probe` | 1 | `/test` | low-priority — test fixture; either drop or schematize |
| `deer-flow.cycle.wait.exit` | 8 | `?` (lost in malformed line region) | check deer-flow producer; likely needs schema |
| `deer-flow.metaprobe.bridge_test` | 2 | `/deer-flow/stack-tuner`, `/tengine` | bridge-validation event; either drop after bridge proven or schematize |

## Naming

- File: `<channel>.<event>.v<n>.json` — e.g. `loom.lifecycle.pr.v1.json`
- `$id`: `nervous-bus/schemas/<filename>`
- `title`: `<channel>.<event> v<n>`
