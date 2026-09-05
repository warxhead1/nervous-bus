# Ecosystem utility implementation design

User-authorized 2026-09-05. Build on bounded KB packets, shared Beads access,
Orca supervision and existing Deer operations. The research basis is
`/home/eric/data2/scratch/ecosystem-utility-synthesis-20260905.md`.

## Authority and interfaces

Beads owns scoped work and dependencies. Orca owns outer Run/Task/Dispatch,
placement and accepted delivery. Deer owns bounded research within that attempt.
KB owns durable evidence and supersession. A correlation ID never grants
authority or establishes successful execution. Preserve separate project,
repository/revision, bead, Deer thread/run, Orca run/task/dispatch, provider
session, actual model and evidence URI fields; unknown stays explicit.

Context has one owner per dispatch. Mandatory constraints remain outside ranked
retrieval. Research produces cited claims, uncertainty and proposed acceptance;
it cannot silently expand a bead or promote historical evidence to current fact.

## Independent implementation lanes

### Discovery and project capabilities

Provide a small progressive entry point backed by installed command truth.
Expose per-project support for query, local intelligence and current indexes;
do not equate profile registration with audited support or index freshness.
Unify discoverability without weakening the six-project local operations boundary.
Use explicit model policy and distinguish local-only operations from provider runs.
Keep detailed instructions separate from the compact entry point.

Acceptance includes command/registry contract tests, unsupported-project cases,
dirty/stale index handling and a concrete nervous-bus discovery example. No
global skill installation until the reviewed source is ready to activate.

### Knowledge invalidation and research handoff

Implement a demonstrated KB supersession consumer on the actual Deer retrieval
path, opt-in where appropriate. Persist processing state and tolerate duplicate,
reordered and replayed events across restart. Invalidate only knowledge with
explicit KB lineage; never infer lineage for DeerMem or graph records.
Revalidate or remove prior injected context before reuse. Keep caller identity
distinct from locally verified provenance. Return a bounded artifact suitable
for a scoped bead and supervised execution.

Acceptance includes actual assembled call-path tests, superseded retrieval
exclusion, duplicate/reorder/restart cases, disabled behavior and explicit
failure/degraded status. Existing event contracts are preferred; any required
bus schema change gets a separately scoped nervous-bus bead, new major file,
retained prior version and dependent Deer integration bead.

### Automation, MMX recipes and verified learning

Extend Orca's existing automation/usage paths rather than adding a scheduler.
Use cheap source/evidence prechecks and idempotent input identity; model calls
occur only for meaningful changed inputs. Provide reusable bounded recipe
contracts and a previewable pilot configuration across infrastructure and an
application project. Preserve exact worker settlement and independent patch
review. Bound work in flight by review capacity; no blind retry or unattended
historical harvest/merge.

Capture actual provider/model/attempt identity and available usage. Missing
usage is unknown, not zero; automation completion is not verified acceptance.
Keep construction, research, retrieval, execution, retry coordination and review
disjoint. Count retried execution once. Export evidence compatible with the
existing memory evaluation ledger, without fabricating attribution.

Acceptance includes precheck skip/change/dedup, partial/failed usage, model and
attempt matching, failure/no-op denominators and replay/idempotency tests.
Production schedules are activated only after reviewed pilot artifacts and a
bounded execution check; source tests alone do not claim deployed behavior.

## Credential work

Coordinator owns MiniMax credential setup. Inspect the supported Orca auth path
first: API execution credentials and website cookies may be different features.
Consume only the selected vault credential via `hearth-vault exec --redact`.
Never put keys in model prompts, command arguments, reports or source; do not
mount vault/host credentials into an implementation sandbox. A live bounded
request verifies execution auth; quota display requires its own supported proof.

## Integration and evaluation

Workers use isolated Orca worktrees, claim scoped repository beads with runnable
acceptance before edits, commit only owned changes, and return exact receipts.
Independent lanes avoid overlapping source ownership. Coordinator reviews and
integrates compatible commits, resolves interface gaps and validates installed
behavior before reporting activation. No mass cleanup of historic work.

Pilot by project and task family against a comparable baseline. Record eligibility,
routing/skips, verified outcome, all attempts, total cost, reviewer effort,
latency, queue age, stale-context incidents and cleanup residue. Account for
allocated subscription cost separately from incremental spend. Increase use
only where correctness is preserved and total effort or latency improves.
