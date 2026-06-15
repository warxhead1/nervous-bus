"""detectors/unverified_completion.py — the grounded "no-verification-step" mode
(F1 verification_theater, adapted to what the bus actually carries).

MAST's highest-leverage failure mode is the missing verification step: adding one
lifted ChatDev by +15.6pp, the largest single intervention in the study. For a
solo operator whose agent is BOTH worker and verifier, the "think-I'm-done vs
actually-checked" gap dominates.

TWO GROUNDING CONSTRAINTS (discovered against live data)
========================================================
1. The reflex bus carries ONLY tool_call events — no assistant prose. So we
   cannot detect the *claim* ("tests pass"); we detect the stronger, fully-
   grounded fact: code was modified and NO build/test ran.
2. A delegated agent is NOT a separate run. The recorder attributes every
   subagent's tool calls to the parent host session, discriminated only by the
   worktree cwd. Host sessions, meanwhile, only ever close on idle_timeout —
   there is no semantic "I'm done". So a CLOSE-gated detector is structurally
   dead on this substrate. The right unit is the WORKTREE SEGMENT: one delegated
   agent's slice of the parent run (dispatch_lineage.segment_by_worktree).

SIGNAL
======
Per closed run, segment events by worktree slug (= delegated agent). Flag each
segment where the delegated agent made >= EDIT_THRESHOLD code edits and ran ZERO
build/test command — it shipped substantive code without verifying it. One
candidate per run, aggregating that run's flagged segments; occurrences = number
of unverified delegated agents.

EDIT_THRESHOLD guards against trivial touch-ups where running the suite would be
overkill; the pattern of interest is substantive code change shipped unverified.

REMEDIATION LADDER
==================
AUTOMATE: a per-agent pre-finish gate that requires a build/test invocation (or an
explicit recorded "no test applicable") before a delegated agent that modified
code reports completion. Pairs with red_baseline_dispatch: that gates the START of
delegated work on a green baseline; this gates the END on verification.
"""
from __future__ import annotations

import sqlite3

from detectors.base import BaseDetector, PatternCandidate
from detectors.dispatch_lineage import (
    load_run_events,
    segment_by_worktree,
)
from detectors.verification import build_verifier

# Minimum distinct Edit/Write calls in a delegated segment for an unverified
# finish to be worth flagging. 1-2 edits is often a doc tweak or trivial fix;
# >=3 substantive code edits with no test run is the shape we care about.
EDIT_THRESHOLD = 3


class UnverifiedCompletionDetector(BaseDetector):
    """Detect delegated agents that shipped code edits with no build/test (F1)."""

    DETECTOR_NAME = "unverified_completion"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        runs_cur = conn.execute(
            """
            SELECT run_id, project
            FROM runs
            WHERE close_reason IS NOT NULL
            ORDER BY started
            """
        )
        runs = [(r[0], r[1]) for r in runs_cur.fetchall()]
        is_verify = build_verifier()

        candidates: list[PatternCandidate] = []
        for run_id, project in runs:
            events = load_run_events(conn, run_id)
            if not events:
                continue
            segments = segment_by_worktree(events, is_verify=is_verify)
            # Flag only delegated agents that edited CODE (a verification
            # obligation) and ran no build/test. Doc/.planning-only agents
            # (code_edit_count == 0) are exempt — they owe no test.
            flagged = [
                seg for seg in segments.values()
                if seg.code_edit_count >= EDIT_THRESHOLD and seg.build_test_count == 0
            ]
            if not flagged:
                continue

            flagged.sort(key=lambda s: s.code_edit_count, reverse=True)
            total_edits = sum(s.code_edit_count for s in flagged)
            signature = f"{project}:{self.DETECTOR_NAME}"

            evidence = [
                f"project={project}",
                f"run_id={run_id}",
                f"unverified_agents={len(flagged)}",
                f"total_unverified_code_edits={total_edits}",
            ]
            for seg in flagged[:6]:
                evidence.append(f"{seg.slug}: code_edits={seg.code_edit_count} build_test=0")

            candidates.append(PatternCandidate(
                project=project,
                pattern_name="unverified_completion",
                signature=signature,
                detector=self.DETECTOR_NAME,
                occurrences=len(flagged),
                evidence=evidence,
                run_ids=[run_id],
                proposed_remediation=(
                    "AUTOMATE: "
                    f"{len(flagged)} delegated agent(s) in this run made code edits "
                    "and ran NO build/test — completion with no verification step "
                    "(MAST's highest-leverage missing intervention). Add a per-agent "
                    "pre-finish gate requiring a build/test run (or an explicit "
                    "recorded 'no test applicable') before a code-modifying delegated "
                    "agent reports done."
                ),
                extra={
                    "unverified_agents": len(flagged),
                    "total_unverified_code_edits": total_edits,
                    "remediation_rung": "automate",
                    "segments": [
                        {
                            "slug": s.slug,
                            "child_agent_id": s.child_agent_id,
                            "edit_count": s.edit_count,
                            "code_edit_count": s.code_edit_count,
                            "build_test_count": s.build_test_count,
                        }
                        for s in flagged[:20]
                    ],
                },
            ))

        return candidates
