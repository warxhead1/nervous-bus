# Orca lifecycle audit + task-system adoption playbook (2026-08-30)

Scope: read-only audit of the live Orca orchestration database + source
(`/home/eric/data2/worktrees/orca/agent-20260830-133445-p1124202`, fork of
Orca, branch tip `c026ceb6bf` at audit time), plus a proposed adoption
playbook. No orca source was modified.

Surfaces searched: `~/.config/orca/orchestration.db` (live SQLite DB, opened
`mode=ro`), `src/main/runtime/orchestration/**`, `src/main/runtime/rpc/methods/
orchestration*.ts`, `src/cli/specs/orchestration*.ts`,
`src/main/persistence/scheduling-automations/**`,
`~/.config/orca/profiles/local-default/orca-data.json` (automations +
persisted state), and the four `~/.local/bin/orca-*` wrapper scripts already
in this box's PATH (`orca-audit-dispatch`, `orca-dispatch-lane`,
`orca-lane-status`, `orca-trust-worktree`). NOT searched: the Orca Electron
renderer/UI source (decision-gate panel, run dashboard) — findings about "how
a human resolves a gate today" are inferred from DB rows + resolution text,
not from reading the UI code. NOT searched: any macOS/Windows userData path
(this box is Linux; only `~/.config/orca` and the WSL/`/tmp` fallback paths
found by `find` were checked). NOT searched: git history of the orca-data.json
automations for older/deleted automation definitions.

## 1. Ground truth: is "we have never used orca's tasks" true?

**No — not literally.** The claim is true for *this specific project*
(nervous-bus) but false for Eric's estate overall. The live DB
(`/home/eric/.config/orca/orchestration.db`) shows sustained multi-day use:

| table | rows | notes |
|---|---|---|
| `runs` | 124 | real run rows; date range 2026-08-24 16:35 → 2026-08-30 17:33 (6 days) |
| `coordinator_runs` | 0 | dead/legacy table — the `orchestration.run` (single-spec autonomous coordinator loop) path has never been used; every real run went through `run-create` + manual `task-create`/`worker-start`, not the built-in coordinator |
| `tasks` | 764 | status: 620 completed, 122 failed, 34 blocked, 33 ready, 4 dispatched, 2 pending |
| `dispatch_contexts` | 746 | status: 620 completed, 122 failed, 4 dispatched, 0 circuit_broken |
| `decision_gates` | 4 | 1 resolved (by Eric, in-band), 3 pending since 2026-08-29 13:45–20:19 (still open at audit time, ~21–28h old) |
| `messages` | 1871 | worker_done 673, status 571, heartbeat 571, question 33, escalation 25 |
| `deliveries` | 802 | |
| `mutation_receipts` | 7170 | |
| `federated_dispatches` | 0 | federation (cross-machine dispatch) feature exists in schema, never used |

Task `spec` text already references bead IDs in free prose today (grep hit,
not a structured column): `task_6ec1130bb71f`/`task_61b0725934c7` reference
"Hearth bead hearth-qpkti"; `task_2a3f197b1a97`/`task_eb61aed4c215`/
`task_464148e4b7be` reference "bead deer-flow-n5ci". So the *practice* of
tying an Orca task to a bead already exists ad hoc; there is no schema column
for it (see §6).

**Correct framing of Eric's statement**: the *coordinator/task DAG* feature
(dependency-tracked sub-tasks under one `run`, `decision_gates` blocking a
task, the built-in `orchestration.run` autonomous loop) is close to unused —
`coordinator_runs` is literally 0 rows, and only 4 gates exist in 6 days of
otherwise heavy use. What *is* heavily used is the flat pattern: one `run` +
one `task` + one `worker-start` per lane, wired by `orca-audit-dispatch` /
`orca-dispatch-lane` — i.e. Orca as a dispatch+mailbox substrate, not as a
DAG/gate-driven planner. That gap (flat dispatch vs. structured DAG) is
exactly what this playbook should close.

Run objectives sampled (most recent 15) are almost all *other* projects
(sweepstakes/"sweepers-adventures", "biz-worthy", "VoltagePro"/mobile,
`arch-*`) — none reference nervous-bus by name. So for nervous-bus
specifically, zero runs/tasks exist; Eric's "we" is the aggregate practice,
not this repo.

## 2. Entry points — how a run/task gets created today

Fully CLI-reachable, no Electron UI required for the create/read/write path.
The `orca` binary (`/home/eric/.config/orca/linux-orca-cli-shim/orca`) is an
RPC client that talks to the **already-running** Orca Electron main process
(it does not start its own server) — so "headless" here means "no UI
interaction needed," not "the Electron app can be fully absent." Full verb
list, each backed by a Zod-validated RPC method
(`src/main/runtime/rpc/methods/orchestration*.ts`) and a CLI spec
(`src/cli/specs/orchestration.ts`, `orchestration-worker-specs.ts`):

- `orca orchestration run-create --objective <text> [--from <handle>] [--json]`
- `orca orchestration run-use / run-current / run-list / run-show`
- `orca orchestration task-create --spec <text> [--task-title] [--display-name] [--deps <json>] [--parent <task_id>] [--run <run_id>] [--json]`
- `orca orchestration task-list [--status ...] [--ready] [--brief] [--run <id>]`
- `orca orchestration task-update --id <task_id> --status <status> [--result <text>]`
- `orca orchestration worker-start --task <task_id> --worktree new-child --name <name> --agent <codex|claude|...> [--model <id>] [--json]` (spec: `orchestration-worker-specs.ts`)
- `orca orchestration worker-show / worker-read / worker-stop / worker-abandon / worker-release / worker-retain / worker-list`
- `orca orchestration gate-create --task <task_id> --question <text> [--options <json_array>]`
- `orca orchestration gate-resolve --id <gate_id> --resolution <text>`
- `orca orchestration gate-list [--task <id>] [--status pending|resolved|timeout] [--run <id>]`
- `orca orchestration send / check / reply / inbox / dispatch / dispatch-show / ask` — the mailbox layer
- `orca orchestration coordinator-start / coordinator-stop` — the built-in autonomous-loop coordinator (`orchestration.run`/`orchestration.runStop`, `cli-command.ts` calls it `Coordinator`); this is the path with 0 `coordinator_runs` rows — genuinely never used
- `orca orchestration reset --all|--tasks|--messages`

Evidence this is real and used, not just theoretical: `~/.local/bin/
orca-audit-dispatch:44-58` runs exactly `run-create` → `task-create` →
`worker-start`, and blocks on `orca orchestration check --wait --types
worker_done,escalation,question`. This wrapper alone accounts for a
meaningful fraction of the 124 runs (its objective strings start with
`"supervised: <name>"`, matching 6+ of the most recent runs verbatim, e.g.
`run_3f13f1d4e78c "supervised: sol-deployed-fixes-adversarial"`).

There is no separate HTTP surface found — RPC methods are `defineMethod()`
entries served over the same channel the CLI shim uses; no `express`/`http.
createServer` orchestration route was found in a search of `src/main/runtime/
rpc/*.ts` (only e2ee/mobile relay socket code, unrelated to orchestration).

## 3. Automations

Found in a **separate persistence layer** from the orchestration DB —
`src/main/persistence/scheduling-automations/*` + `PersistedState.automations`
/ `.automationRuns`, stored in `~/.config/orca/profiles/local-default/
orca-data.json`, not the sqlite DB.

4 automations defined, both trigger kinds have actually fired (correcting the
"automations are manual-only" hypothesis in the dispatch brief):

| automation | last run | 
|---|---|
| oxsynth-acceptance-watch | scheduled + manual, most recent 2026-08-30-ish |
| oxsynth-mmx-sweep | scheduled + manual |
| oxsynth-loss-forensics | manual only (3 runs) |
| sweepers-nightly-gate | scheduled |

12 `automationRuns` total: 11 `completed`, 1 `dispatch_failed` (a scheduled
run of `oxsynth-acceptance-watch`). Trigger field is exactly the
`'scheduled' | 'manual'` enum named in the dispatch brief
(`AutomationRunTrigger` type, `automation-run-operations.ts:6`), and both
values are exercised in practice — this part of Orca is not idle.

## 4. Decision gates in practice

- **Open**: any caller with `--task <task_id>` can call `gate-create`
  (`orchestration-gates.ts:111-152`) — this includes a worker running inside
  its own dispatched worktree, since the `orca` CLI shim is on PATH there too.
  No message-type check restricts who opens one; the RPC only checks the task
  belongs to the caller's resolved run.
- **Resolve**: `gate-resolve --id <gate_id> --resolution <text>`
  (`orchestration-gates.ts:154-180`). The one resolved gate in the live DB
  (`gate_f6475d3d1221`, "CONTRACTS.md is FROZEN...") carries resolution text
  "amend-contract-and-implement — authorized by Eric 2026-08-29" — i.e. this
  is how Eric answers a gate today: CLI `gate-resolve` (or the Electron
  decision-gate panel, not confirmed either way — UI source not audited per
  the exhaustion note above) with free-text justification, not just picking
  one of the `options`.
- **On timeout: nothing happens.** The schema supports a `'timeout'` status
  and there is a `timeoutGate(gateId)` DB method
  (`decision-gate-store.ts:120-124`, `UPDATE decision_gates SET status =
  'timeout' ... WHERE status = 'pending'`), but a full-repo grep for
  `.timeoutGate(` found exactly one caller: a unit test
  (`db.test.ts:532`). No cron, no coordinator loop, no RPC method calls it at
  runtime. **A decision gate that nobody resolves stays `pending` forever** —
  confirmed live: 3 of the 4 gates in the DB have sat `pending` for
  21–28 hours as of this audit with no automatic expiry, no re-notification
  logic found, and (since the blocked task's dispatch just sits `dispatched`
  with no failure/retry signal) no visible failure surfaced to whatever is
  waiting on that task's downstream deps.

## 5. Playbook — "Orca tasks: when and how"

**Decision table — where does a piece of work go?**

| situation | mechanism |
|---|---|
| One self-contained read/audit/edit task, no sub-steps, no cross-task blocking | Agent-tool lane (this session's own dispatch) — cheapest, no DB row, already fits the model-tiering rules |
| A lane needs a **child worktree + its own long-running terminal** that the parent must supervise to completion and be woken on completion | `orca-audit-dispatch <name> <report> <prompt> [model]` — wraps run-create+task-create+worker-start+check --wait in one supervised call (see §2) |
| Multiple **dependent** steps (B can't start until A's artifact exists), or a step that legitimately needs a human decision mid-flight | A real Orca **run** with **N tasks wired by `--deps`**, created by hand (not `orca-audit-dispatch`, which only ever makes 1 task) — this is the DAG feature that is *not yet* in practice (§1) |
| A recurring/periodic check (nightly gate, watch loop) | An Orca **automation** (`scheduling-automations`), not a run — this already works (§3), just under-adopted (4 automations exist across the whole estate) |
| Traceable engineering work with acceptance criteria that should survive independent of any one session/agent | A **bead** (`bd`) — beads are the durable record; an Orca run/task is the *execution* of a bead, not a replacement for filing one |

**Bead linkage convention (proposed — no `bead` column exists in `tasks` or
`runs` today, verified against the schema; the practice below is what agents
already do ad hoc, per §1's grep hits):**

Prefix the `--spec` (and, symmetrically, the `--objective` on the owning run)
with `[bead:<repo>-<id>]` as the first token, e.g.:

```
orca orchestration run-create --objective "[bead:nervous-bus-xyz] fix schema coverage gap" --json
orca orchestration task-create --run <run_id> --spec "[bead:nervous-bus-xyz] READ ONLY. ..." --json
```

This is discoverable (`task-list`/`run-list` output includes `spec`/
`objective` verbatim, so `grep '\[bead:'` over `task-list --json` output finds
every Orca task for a given bead) without a schema migration. If bead linkage
becomes load-bearing (e.g. an automated close-the-loop step that marks a bead
done when its task completes), promote it to a real column — see §6 gap #2.

**Exact command sequence for a structured multi-task run:**

```bash
# 1. create the run (one per objective, not one per task)
run_json=$(orca orchestration run-create --objective "[bead:X] <objective>" --json)
run_id=$(jq -r '.result.run.id' <<<"$run_json")

# 2. create tasks, wiring dependencies by id
t1=$(orca orchestration task-create --run "$run_id" --spec "[bead:X] step 1: ..." --json | jq -r '.result.task.id')
t2=$(orca orchestration task-create --run "$run_id" --spec "[bead:X] step 2: ..." --deps "[\"$t1\"]" --json | jq -r '.result.task.id')

# 3. dispatch a ready task into a worker
orca orchestration worker-start --task "$t1" --worktree new-child --name "step1" --agent codex --json

# 4. watch progress (poll, not a blocking supervisor, if you have >1 task in flight)
orca orchestration task-list --run "$run_id" --json

# 5. resolve a decision gate a worker opened
orca orchestration gate-list --run "$run_id" --status pending --json
orca orchestration gate-resolve --id <gate_id> --resolution "<decision + who approved>" --json
```

For the single-lane supervised case (the common one today), keep using
`orca-audit-dispatch` — it already implements steps 1–4 correctly for that
shape and is proven across ~100+ runs.

**What the new bus events make observable** (`orca.worker.lifecycle.v1` +
`orca.run.lifecycle.v1`, merged `c026ceb6bf`,
`src/main/runtime/orchestration/nbus-emit.ts`): any nervous-bus consumer
(deer-flow SSE, a zellij pane, a future dashboard) can now see run/worker
start/stop/failure without polling `task-list` or `check`. Combined with the
`[bead:...]` spec convention above, this gives a bead-to-run-to-worker trace
end-to-end through the bus without new Orca schema work.

## 6. Gaps blocking fuller adoption — ranked, with file:line + smallest fix

1. **Decision gates never expire — highest-severity, smallest fix.**
   Evidence: `timeoutGate()` defined at
   `src/main/runtime/orchestration/db/decision-gates/decision-gate-store.ts:120-124`,
   only ever called from `db.test.ts:532` (full-repo grep for
   `.timeoutGate(`). Live effect: 3 pending gates aged 21–28h with no
   escalation. **Smallest fix**: a periodic sweep (reuse whatever timer
   already drives automations/heartbeats) that calls `db.timeoutGate(id)` for
   gates past some age, plus emitting a `decision_gate.timeout` message to the
   run mailbox so a waiting `check --wait` unblocks instead of hanging. This
   is a few-line wiring change, not new logic — the DB method already exists
   and is tested.

2. **No bead-linkage column, so bead↔run traceability is grep-only.**
   Evidence: `tasks` schema (`create-graph-tables-sql.ts`, confirmed by
   `.schema tasks` on the live DB) has no `bead_id`/`external_ref` column;
   `runs` schema likewise. Practice already writes bead IDs into free-text
   `spec`/`objective` (§1). **Smallest fix**: add nullable `bead_id TEXT` to
   `runs` and `tasks` via a new `migrate-v*.ts` (pattern already established
   by `migrate-v13-v30.ts:125-131` adding `created_by_*` columns the same
   way), populate it from a `--bead <id>` CLI flag threaded through
   `run-create`/`task-create`, and keep accepting the `[bead:...]` prefix
   convention as a fallback for anything created before the migration.

3. **The built-in DAG/coordinator autonomy path is unexercised (0
   `coordinator_runs` rows) — not a defect, but means "run a spec and let
   Orca's own coordinator sequence + dispatch sub-tasks" (`orchestration.run`
   / `coordinator-start`, `orchestration-gates.ts:51-89`) is unvalidated in
   this estate. Every real run today is externally sequenced (a human or an
   agent calling `task-create`/`worker-start` by hand, or the
   `orca-audit-dispatch` wrapper doing it once per run).** This isn't
   something to "fix" — it's the actual gap the user named ("never used
   orca's tasks... make it more available/usable/structured"). Smallest next
   step to validate it without committing to it: pick one real
   multi-dependency piece of work and run it through §5's 5-step sequence by
   hand once, instead of through `coordinator-start`'s fully autonomous loop
   (safer first step — human stays in the loop on `worker-start` per task
   while still getting the DAG/dependency benefit).

4. **Gate resolution UX is CLI/UI, not agent-reachable from inside a
   dispatched worker in a machine-checkable way.** A worker can `gate-create`
   fine (§4), but nothing in `preamble.ts` (the text injected into a
   dispatched worker's first turn — checked, no `gate` mention found) tells a
   worker *how* to open one or that the verb exists. **Smallest fix**: add a
   one-line mention of `orchestration gate-create`/`gate-list --status
   pending` to the dispatch preamble alongside the existing verb-availability
   gating logic already in that file (`preamble.ts:34`).

5. **Automations are a second, disconnected persistence layer** (JSON
   `orca-data.json`) from the orchestration DB (sqlite) — a run created by an
   automation and a run created by `orca-audit-dispatch` are visible through
   different inspection paths (`task-list`/`gate-list` vs. reading
   `orca-data.json` directly; no CLI verb for "list automations" was found in
   `src/cli/specs/`). Not urgent (automations already work, §3), but worth
   naming: an operator can't currently see "automations + runs" in one place
   from the CLI.

## What I'd try next with more budget

- Read the Electron renderer's decision-gate panel to confirm/deny whether
  gate resolution happens there today vs. purely via CLI (I inferred "CLI or
  UI, not confirmed which" in §4 — this is the one claim in this report not
  independently confirmed against source).
- Grep the full `orca-data.json` for any older/pruned automation definitions
  (retention logic exists per `automation-run-operations.ts` imports of
  `pruneAutomationRuns`) to get a true historical automation count, not just
  the current 4 survivors.
- Instrument gate #1 fix behind a flag and dry-run it against the 3 live
  pending gates to see what actually would have happened, before wiring it
  for real.

## Status

Read-only audit of orca; no orca files touched. This report is the only
change in this branch.
