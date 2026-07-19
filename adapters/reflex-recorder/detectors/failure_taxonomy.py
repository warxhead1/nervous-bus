"""detectors/failure_taxonomy.py — A2: failure-taxonomy classifier.

Harness Engineering Adoption Map, Part 2 Tier 1, A2. Classifies each analyzed
run's failure signals into the deepset four-bucket taxonomy:

    context_failure       wrong/missing/stale context
    constraint_failure     permission denials, contract violations
    verification_failure   claimed-done-but-gate-failed, reverted outcomes
    planning_failure       thrash/loops, abandoned runs, scope drift

MAPPING PRINCIPLE — no new capture
===================================
Every signal below is something ALREADY captured by this recorder before this
detector exists: other detectors' recorded hits (detector_hits, populated by
detectors that ran earlier in the same synthesis pass — see the registration
order in synthesis.py, this detector is registered LAST so every other
built-in has already had a chance to fire on this run), the run's labeled
`outcome` (clean/landed/corrected/thrashed/abandoned/reverted — label.py is
the source of truth for this vocabulary), permission_requested events already
stored in run_events (the same signal repeated_question.py reads), and the
run's raw event_count (a blunt tool-call-cadence proxy). Nothing here invents
a new probe.

A run may match MULTIPLE buckets (e.g. a reread_same_file hit AND a reverted
outcome) — this is intentionally multi-label, not a forced single verdict.
A run that matches NONE of the mapped signals is tagged `unclassified`
honestly, rather than being crammed into the nearest bucket.

Bucket → signal mapping
========================
context_failure:
  - reread_same_file hit          (chronic re-reads = stale/missing context)
  - directive_ground_truth_mismatch hit (dispatch asserted a baseline reality
    contradicts — a directly-observed WRONG context propagated to children)
  - repeated_question hit         (same question re-asked = context not
    retained across turns/runs)

constraint_failure:
  - >=1 permission_requested event in this run (a permission gate was hit;
    this is the only "denial"-shaped signal the bus carries today — we do not
    have a separate contract-violation probe, so this bucket is currently
    permission-only and that limitation is stated here rather than papered
    over with a fabricated second signal)

verification_failure:
  - unverified_completion hit     (delegated agent shipped code edits, ran
    no build/test before reporting done)
  - edit_build_fail_revert hit    (build failed, then the edit was reverted
    within the same run — the gate caught a bad claim)
  - outcome == 'reverted' (labeled_at IS NOT NULL) (claimed-done work was
    later undone — the strongest possible verification-failure signal)

planning_failure:
  - outcome in ('thrashed', 'abandoned') (labeled_at IS NOT NULL)
  - red_baseline_dispatch hit     (fan-out launched on a red/unestablished
    baseline — a planning precondition was skipped)
  - inherited_rationalization hit (sibling cohort converged on one seeded
    bad outcome — scope/plan drift propagated from the dispatch)
  - event_count >= THRASH_EVENT_COUNT_FLOOR AND outcome is not confirmed
    clean (labeled_at IS NOT NULL AND outcome in clean/landed/corrected) —
    the weakest signal here, an honest cadence heuristic: a very long run
    that did NOT end in a confirmed-clean outcome is treated as a loop/thrash
    candidate, not a diagnosis.

Output
======
One PatternCandidate per (project, bucket) — including an explicit
`unclassified` bucket — grouping every run that matched, so the Kyoko
prevalence/recurrence layer tracks "how much of this project's work falls
into which failure bucket" over time. Per-run tags are reconstructable from
detector_hits (run_id, signature) without any extra state, exactly like every
other cross-run detector in this engine (see reread_same_file).

Remediation ladder
==================
INFORM only. This is a classifier/surfacing detector — it does not propose a
deterministic fix (there is no single fix for "context failure" as a
category); it exists to make the digest's aggregate counts possible and to
let a human or a later, more specific detector target the bucket that is
actually dominating.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate

# ── Constants ─────────────────────────────────────────────────────────────────

CONTEXT_FAILURE = "context_failure"
CONSTRAINT_FAILURE = "constraint_failure"
VERIFICATION_FAILURE = "verification_failure"
PLANNING_FAILURE = "planning_failure"
UNCLASSIFIED = "unclassified"

BUCKETS = (CONTEXT_FAILURE, CONSTRAINT_FAILURE, VERIFICATION_FAILURE, PLANNING_FAILURE)

# Detector-hit signal sets per bucket (see module docstring for rationale).
_CONTEXT_DETECTORS = frozenset({
    "reread_same_file", "directive_ground_truth_mismatch", "repeated_question",
})
_VERIFICATION_DETECTORS = frozenset({"unverified_completion", "edit_build_fail_revert"})
_PLANNING_DETECTORS = frozenset({"red_baseline_dispatch", "inherited_rationalization"})

# Outcome vocabulary (label.py is the source of truth).
_FAILURE_OUTCOMES_PLANNING = frozenset({"thrashed", "abandoned"})
_CLEAN_OUTCOMES = frozenset({"clean", "landed", "corrected"})

# A run with no confirmed-clean outcome and an event_count at/above this floor
# is treated as weak evidence of a thrash/loop (planning_failure). Chosen well
# above typical single-purpose run sizes seen elsewhere in this engine's own
# detectors (edit_build_fail_revert's cycle span constants top out at 20); an
# order of magnitude above that is a conservative "this run went long" floor,
# not a calibrated threshold — it is documented as the weakest signal in the
# bucket for exactly that reason.
THRASH_EVENT_COUNT_FLOOR = 150


def _hit_detectors_for_run(conn: sqlite3.Connection, run_id: str) -> set[str]:
    """Distinct detector names that already recorded a hit for this run.

    Relies on this detector being registered AFTER every detector it reads
    from in synthesis.py's DETECTOR_CLASSES list, so their detector_hits rows
    already exist in this same pass.
    """
    rows = conn.execute(
        "SELECT DISTINCT detector FROM detector_hits WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return {r[0] for r in rows}


def _permission_request_count(conn: sqlite3.Connection, run_id: str) -> int:
    """Count permission_requested events for this run (constraint-failure signal)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND event_type = 'permission_requested'",
        (run_id,),
    ).fetchone()
    return row[0] if row else 0


def classify_run(
    hit_detectors: set[str],
    outcome: Optional[str],
    labeled_at: Optional[str],
    permission_count: int,
    event_count: int,
) -> dict[str, list[str]]:
    """Return {bucket: [reason, ...]} for every bucket this run matches.

    Empty dict means no mapped signal fired — caller tags 'unclassified'.
    Pure function (no DB access) so it is directly unit-testable.
    """
    buckets: dict[str, list[str]] = {}

    context_reasons = []
    for name in sorted(hit_detectors & _CONTEXT_DETECTORS):
        context_reasons.append(f"{name} hit")
    if context_reasons:
        buckets[CONTEXT_FAILURE] = context_reasons

    constraint_reasons = []
    if permission_count > 0:
        constraint_reasons.append(f"{permission_count} permission_requested event(s)")
    if constraint_reasons:
        buckets[CONSTRAINT_FAILURE] = constraint_reasons

    verification_reasons = []
    for name in sorted(hit_detectors & _VERIFICATION_DETECTORS):
        verification_reasons.append(f"{name} hit")
    if labeled_at is not None and outcome == "reverted":
        verification_reasons.append("outcome=reverted")
    if verification_reasons:
        buckets[VERIFICATION_FAILURE] = verification_reasons

    planning_reasons = []
    if labeled_at is not None and outcome in _FAILURE_OUTCOMES_PLANNING:
        planning_reasons.append(f"outcome={outcome}")
    for name in sorted(hit_detectors & _PLANNING_DETECTORS):
        planning_reasons.append(f"{name} hit")
    is_confirmed_clean = labeled_at is not None and outcome in _CLEAN_OUTCOMES
    if not is_confirmed_clean and event_count >= THRASH_EVENT_COUNT_FLOOR:
        planning_reasons.append(
            f"event_count={event_count}>={THRASH_EVENT_COUNT_FLOOR} (cadence heuristic, outcome not confirmed clean)"
        )
    if planning_reasons:
        buckets[PLANNING_FAILURE] = planning_reasons

    return buckets


# ── Detector ──────────────────────────────────────────────────────────────────

class FailureTaxonomyDetector(BaseDetector):
    """Classify each closed run's failure signals into the four-bucket taxonomy."""

    DETECTOR_NAME = "failure_taxonomy"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        runs_cur = conn.execute(
            """
            SELECT run_id, project, outcome, labeled_at, event_count
            FROM runs
            WHERE close_reason IS NOT NULL
            ORDER BY started
            """
        )
        runs = runs_cur.fetchall()

        # {(project, bucket): [{"run_id":..., "reasons":[...]}]}
        cross_run: dict[tuple[str, str], list[dict]] = defaultdict(list)

        for run_id, project, outcome, labeled_at, event_count in runs:
            hit_detectors = _hit_detectors_for_run(conn, run_id)
            permission_count = _permission_request_count(conn, run_id)
            buckets = classify_run(
                hit_detectors, outcome, labeled_at, permission_count, event_count or 0
            )

            if not buckets:
                cross_run[(project or "", UNCLASSIFIED)].append({
                    "run_id": run_id,
                    "reasons": [
                        f"no mapped signal (outcome={outcome!r}, "
                        f"event_count={event_count or 0})"
                    ],
                })
                continue

            for bucket, reasons in buckets.items():
                cross_run[(project or "", bucket)].append({
                    "run_id": run_id,
                    "reasons": reasons,
                })

        candidates: list[PatternCandidate] = []
        for (project, bucket), hits in cross_run.items():
            run_ids = [h["run_id"] for h in hits]

            evidence = [
                f"bucket={bucket}",
                f"project={project}",
                f"runs_tagged={len(hits)}",
            ]
            for h in hits[:8]:
                evidence.append(f"run={h['run_id']}: {'; '.join(h['reasons'])}")

            signature = f"{project}:{self.DETECTOR_NAME}:{bucket}"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="failure_taxonomy",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(hits),
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=None,
                    extra={
                        "bucket": bucket,
                        "runs_tagged": len(hits),
                        "remediation_rung": "inform",
                        "remediation_rung_justification": (
                            "Classifier/surfacing detector — no single deterministic "
                            "fix exists for a failure CATEGORY. Value is in the "
                            "aggregate counts (which bucket dominates) and per-run "
                            "tags for downstream, more specific detectors/humans."
                        ),
                    },
                )
            )

        return candidates
