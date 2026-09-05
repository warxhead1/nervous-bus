# Task context, supersession, and evaluation

This integration extends KB's existing retrieval boundary. It keeps knowledge,
work ownership, execution receipts, and evaluation results separately
addressable. The design basis is
[the research proposal](research/agent-memory-context-2026.md).

## Contracts and authority

KB owns the bounded brief and canonical knowledge status. Deer Flow owns its
retrieval consumer and derived memory. Beads owns work scope. Orca owns the
Run, Task, Dispatch, launch, and completion receipts. Nervous-bus carries
causal correlation using the existing [trace-context contract](TRACE-CONTEXT.md).

Do not equate a KB `trace_uri`, Deer Flow run ID, W3C trace ID, Beads ID, Orca
Task, Orca Dispatch, or provider session. A packet carries explicit bindings;
caller-supplied bindings are correlation labels, not attested execution.
Missing bindings remain unknown. A source's project/revision describes its
historical applicability; it does not certify the current checkout or grant
authority to act.

Required constraints belong outside lossy ranked items. If the mandatory
packet cannot fit the requested transport budget, the producer must fail
without printing a partial packet. Account for UTF-8 bytes of actual stdout,
including serialization whitespace and the final newline. Never budget a
compact representation and then print a larger pretty representation.

Skill discovery carries a reference and content hash. Read the selected skill
instructions when applying it, then resolve additional references as needed.
A reference/hash is not evidence that a new provider session discovered or
obeyed the skill. Keep provider-native continuation objects separate from
portable knowledge and evidence.

The KB extension uses the existing command with opt-in packet output:

```sh
kb brief "memory and trace integration" --project nervous-bus --json --packet \
  --budget-bytes 4096 --bindings bindings.json \
  --constraint "Verify the current repository revision before acting." \
  --skill-reference /path/to/global-agent-ecosystem/SKILL.md
```

Run the newly built KB binary until its installation has been updated.
`bindings.json` accepts `project`, `bead_id`, `deer_run_id`, `orca_run_id`,
`orca_task_id`, `orca_dispatch_id`, `provider_session_id`, and `traceparent`.
Use explicit current values or `null`; do not copy execution IDs from a past
example. `project`, if supplied, must match `--project`. The bindings file is
limited to 64 KiB and each selected skill file to 1 MiB. These limits do not
expand the final stdout budget.

## Supersession boundary

The canonical KB context scorer must exclude superseded entries before
ranking, so merging context and prime cannot resurrect retired knowledge.
Historical evidence remains available through explicit source inspection.

The selected downstream contract is the KB gateway retrieval path. Its
consumer must retain monotonic invalidation by canonical entry identity;
duplicate delivery, late creation, and replay must not resurrect an entry.
Durable projection and input replay are distinct requirements. Persisting
tombstones alone does not recover events missed while the consumer was down.
Revalidate previously injected context instead of relying on a permanent
"already injected" marker. On revalidation failure, old context must not be
silently presented as current.

DeerMem facts and code-graph results without a KB source identity cannot be
selectively invalidated by guessing from their text or paths. Those surfaces
require explicit lineage before any coverage claim. The existing
`kb.entry.superseded.v1` payload needs no alteration for the selected consumer.

## Evaluation records

`tools/memory_evaluation.py` consumes a JSONL ledger of immutable attempt
snapshots. Each attempt has its own ID, an execution ID shared across retries,
a task, project, condition, model, corpus hash, explicit external identities,
costs, and an optional `bus.exec.evidence.v1` envelope. The tool performs no
provider calls, bus publishing, service changes, or automatic promotion.

The conditions are `baseline`, `excerpts`, and `packet`. Freeze task state and
the source corpus across conditions. Keep held-out answers and verifiers
outside both the agent context and memory construction. Record started,
failed, timed-out, cancelled, and completed attempts; do not build the ledger
from surviving branches or successful worker outputs alone. Export a new
snapshot when an attempt settles rather than appending conflicting states for
the same attempt to one input ledger.

Costs have six disjoint phases: `construction`, `research`, `retrieval`,
`execution`, `retry`, and `review`. Each records `duration_ms`, `input_tokens`,
`output_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `cost_usd`.
Use `null` for unobserved counters, including subscription billing amounts
that cannot be attributed. A retry attempt has its own execution costs;
the retry phase records additional retry overhead, not those costs again.
Summed phase durations are resource time, not concurrent wall-clock latency.

Every distinct attempt contributes to the completion-rate denominator.
Verification requires a validated runtime-complete receipt matching the
attempt, execution, bead, and actual model. A completed Orca Task or a green
source build alone does not meet this criterion. The ledger reader validates
receipt structure and identity; the receipt issuer remains responsible for
the truth and independence of its evidence. It does not authenticate an
arbitrary input file or independently rerun its gates.

Unknown metric totals remain `null`, alongside the observed subtotal and
unknown count. The report groups results by project, model, condition, and
corpus hash. Empty input has no completion rate. `promotion: NOT_EVALUATED`
is intentional: accounting alone cannot establish retrieval quality,
constraint preservation, cross-model skill transfer, or improved outcomes.

```sh
python3 tools/memory_evaluation.py /path/to/attempts.jsonl > evaluation.json
```

The CLI renders monetary totals as decimal strings to preserve precision;
token and duration totals are integers. Consumers must retain this distinction
instead of converting missing or string-valued amounts to zero.
Summation uses input-derived precision, independent of the caller's Decimal
context. A required precision above 10,000 significant digits is an explicit
error; the CLI does not round or print a partial result.

For the proposed pilot, use six within-project recall cases, six cross-project
joins, six revisions/contradictions, and six procedure-reuse cases with changed
bindings. Include irrelevant-memory and confidently-wrong-memory controls.
Reject observed authority/project regressions. Require preserved independently
verified completion plus measured resource improvement before adoption; do
not infer either from reduced packet size.

## Delivery evidence

The nervous-bus branch includes validated Rust envelope tracing at `77ab67b`
and attempt accounting at `19f50af` plus the exact-decimal repair at `c485cde`.
Independent checks passed 13 SDK unit tests, the SDK all-features build check,
and 153 evaluation/reference-receipt tests. A separate probe under Decimal
precision 2 confirmed that `1e28 + 1` remains exact and an unsupported digit
span produces an explicit error.

The KB companion branch `warxhead1/memory-brief-provenance` implements packet
output and superseded-entry filtering at `b7cf3b1`, with executable CLI
acceptance and trust-boundary repairs at `b4e6e6c`. Independent validation
passed `cargo check --all-targets` and all 36 selected tests:

```sh
cargo nextest run -E 'binary(brief_packet) | test(brief) | test(context)'
```

Sixteen tests run the actual CLI under isolated vault, state, home, and Git
fixtures. They cover Unicode, exact byte boundaries, empty stdout on rejected
budgets, constraints, bindings, skill hashes, and current versus historical
identity. These are fixture execution results; no installed KB binary or live
vault was changed.

Deer Flow implementation is blocked under coordination bead
`nervous-bus-zn3g`: its remote-backed Beads database requires a designated
migration owner before the required companion bead can be created. No database
migration, consumer installation, durable invalidation replay, or end-to-end
Deer Flow retraction is claimed. KB-local exclusion closes the producer-side
retrieval gap only. No controlled cross-project/model pilot has run, and no
completion improvement or observed total billing amount is claimed.
