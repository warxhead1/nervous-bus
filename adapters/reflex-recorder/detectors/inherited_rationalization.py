"""detectors/inherited_rationalization.py — C1, the keystone backward-correlator.

When a fan-out's sibling agents independently converge on the SAME outcome class
because the PARENT seeded it, the fix belongs to the dispatch, not the children.
Lived this session: 5 agents each "confirmed pre-existing" because the dispatch
told them to. MAST calls this the inter-agent misalignment / cascading family;
remediation points at the seed (Inform), upstream of where the cost was paid.

SESSION SCOPE (substrate truth)
===============================
A fan-out dispatch and the child worktree activity it spawned land in DIFFERENT
idle-split runs of the same session, so this detector pools a session's runs
(dispatch_lineage.session_run_ids + load_session_events) and joins each cohort to
its children's DERIVED outcomes (join_cohort_to_children). Only cohorts whose
children we can actually observe (>= MIN_MATCHED matched siblings) are judged.

SIGNAL
======
For each fan-out cohort with >= MIN_COHORT_WIDTH matched children: if >= MAJORITY
of the matched siblings share ONE derived outcome_class (e.g. all "unverified",
all "left_red"), fire ONE candidate scoped to the dispatch. Shared convergence on
a degenerate class (unverified / left_red) is the high-signal case — siblings
inheriting a bad pattern from the seed. A shared "verified" convergence is benign
and does not fire.

REMEDIATION LADDER
==================
INFORM (points at the seed): surface the dispatch + the shared class + the cohort,
so the orchestrator fixes the directive once instead of N children each absorbing
it. Upgrades toward Eliminate when joined with A1/A2 (the seed was a red baseline
or a false "pre-existing" claim).
"""
from __future__ import annotations

import sqlite3
from collections import Counter

from detectors.base import BaseDetector, PatternCandidate
from detectors.dispatch_lineage import (
    derive_subagent_outcomes,
    group_cohorts,
    join_cohort_to_children,
    load_session_events,
    parse_dispatches,
    session_run_ids,
)
from detectors.verification import build_verifier

# A cohort must have at least this many OBSERVABLE (matched) children to judge
# convergence — two siblings agreeing is the minimum signal of a shared pattern.
MIN_COHORT_WIDTH = 2

# Fraction of matched siblings that must share one class to call it convergence.
MAJORITY = 0.6

# Outcome classes whose shared convergence is worth flagging (degenerate finishes
# the seed plausibly caused). A shared "verified"/"readonly" is benign.
_FLAGGABLE_CLASSES = {"unverified", "left_red"}


class InheritedRationalizationDetector(BaseDetector):
    """Detect fan-out cohorts whose siblings converged on a seeded bad class (C1)."""

    DETECTOR_NAME = "inherited_rationalization"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        project_of = dict(conn.execute("SELECT run_id, project FROM runs").fetchall())
        is_verify = build_verifier()
        candidates: list[PatternCandidate] = []

        for session_id, run_ids in session_run_ids(conn).items():
            events = load_session_events(conn, run_ids)
            if not events:
                continue
            outcomes = derive_subagent_outcomes(events, is_verify=is_verify)
            if not outcomes:
                continue

            # project = the dominant project among this session's runs.
            projs = [project_of.get(r, "") for r in run_ids if project_of.get(r)]
            project = Counter(projs).most_common(1)[0][0] if projs else "unknown"

            cohorts = group_cohorts(parse_dispatches(events))
            for cohort in cohorts:
                joined = join_cohort_to_children(cohort, outcomes)
                matched = [cc.outcome for cc in joined if cc.matched]
                if len(matched) < MIN_COHORT_WIDTH:
                    continue

                class_counts = Counter(o.outcome_class for o in matched)
                top_class, top_n = class_counts.most_common(1)[0]
                if top_class not in _FLAGGABLE_CLASSES:
                    continue
                if top_n / len(matched) < MAJORITY:
                    continue

                candidates.append(self._candidate(
                    project, session_id, run_ids, cohort, matched, top_class, top_n,
                ))

        return candidates

    def _candidate(self, project, session_id, run_ids, cohort, matched,
                   top_class, top_n) -> PatternCandidate:
        signature = f"{project}:{self.DETECTOR_NAME}:{top_class}"
        share = top_n / len(matched)
        seed_phrase = self._seed_phrase(cohort, top_class)

        evidence = [
            f"project={project}",
            f"session_id={session_id}",
            f"cohort_width={len(cohort)}",
            f"matched_children={len(matched)}",
            f"shared_class={top_class}",
            f"share={top_n}/{len(matched)} ({share:.0%})",
        ]
        child_ids = [o.child_agent_id for o in matched if o.outcome_class == top_class][:6]
        if child_ids:
            evidence.append("converged_children=" + ",".join(child_ids))
        if seed_phrase:
            evidence.append(f"likely_seed_phrase={seed_phrase!r}")

        remediation = (
            f"INFORM (fix the seed): {top_n} of {len(matched)} sibling agents in this "
            f"fan-out converged on '{top_class}' — a pattern the dispatch likely "
            "seeded. Fix the DIRECTIVE once (it propagated to every child) rather "
            "than each child's output. "
            + ("The '" + top_class + "' convergence means the siblings shipped code "
               "without verifying (or left it red) as a group — check whether the "
               "dispatch told them to skip verification or treat failures as "
               "pre-existing." if top_class in _FLAGGABLE_CLASSES else "")
        )

        # run_ids = the session's runs (so the hit attributes to observed runs).
        return PatternCandidate(
            project=project,
            pattern_name="inherited_rationalization",
            signature=signature,
            detector=self.DETECTOR_NAME,
            occurrences=top_n,
            evidence=evidence,
            run_ids=list(run_ids),
            proposed_remediation=remediation,
            extra={
                "shared_class": top_class,
                "cohort_width": len(cohort),
                "matched_children": len(matched),
                "share": round(share, 3),
                "converged_child_ids": child_ids,
                "remediation_rung": "inform",
                "dispatch_names": [d.name for d in cohort if d.name][:8],
            },
        )

    @staticmethod
    def _seed_phrase(cohort, top_class) -> str:
        """Best-effort: surface a shared dispatch-prompt phrase that may be the seed.

        We look for the longest common-ish hint in the cohort's prompts that maps
        to the degenerate class (no NLP — just flag the presence of known
        bad-seed phrasings). Returns "" if none found.
        """
        seeds = {
            "unverified": ("don't run", "do not run", "skip the test", "no need to test",
                           "without testing", "don't bother"),
            "left_red": ("pre-existing", "preexisting", "treat as pre", "ignore the failure",
                         "already failing", "known failure"),
        }.get(top_class, ())
        for d in cohort:
            low = (d.prompt or "").lower()
            for s in seeds:
                if s in low:
                    return s
        return ""
