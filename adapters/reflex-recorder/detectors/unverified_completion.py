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

TRANSCRIPT TIER
===============
The bus summary keeps only a verb for a Bash call since 2026-10-04, so the event
tier cannot tell `python3 -m pytest` from `python3 script.py`: after that date it
fired on 71% of runs that verified and 75% of those that did not. When a run's
session transcript resolves, the detector reads it instead. It flags a run whose
transcript shows >= EDIT_THRESHOLD code edits (Edit/Write/MultiEdit to a code
path) and no verifying Bash command, judged on the FULL command text, after the
last of those edits. A run with no transcript falls back to the event tier.

REMEDIATION LADDER
==================
AUTOMATE: a per-agent pre-finish gate that requires a build/test invocation (or an
explicit recorded "no test applicable") before a delegated agent that modified
code reports completion. Pairs with red_baseline_dispatch: that gates the START of
delegated work on a green baseline; this gates the END on verification.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate
from pathlib import Path

from detectors.dispatch_lineage import (
    is_code_path,
    load_run_events,
    segment_by_worktree,
)
from detectors.verification import build_verifier
from transcript_steps import StepCache, run_refs

# Minimum distinct Edit/Write calls in a delegated segment for an unverified
# finish to be worth flagging. 1-2 edits is often a doc tweak or trivial fix;
# >=3 substantive code edits with no test run is the shape we care about.
EDIT_THRESHOLD = 3

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def transcript_verdict(steps, project: str, is_verify):
    """(code_edits, verified_after_last_edit) for one run's transcript steps."""
    edits = [s for s in steps if s.tool in _EDIT_TOOLS and is_code_path(s.file_path)]
    if not edits:
        return 0, True
    last = max(s.t0 for s in edits)
    verified = any(
        s.tool == "Bash" and s.t0 >= last and is_verify(s.command, project)
        for s in steps
    )
    return len(edits), verified


class UnverifiedCompletionDetector(BaseDetector):
    """Detect delegated agents that shipped code edits with no build/test (F1)."""

    DETECTOR_NAME = "unverified_completion"

    #: Transcript roots for tests; None uses the live and mirror roots.
    transcript_roots: Optional[list[Path]] = None

    def detect(
        self, conn: sqlite3.Connection, since_ts: Optional[str] = None
    ) -> list[PatternCandidate]:
        """since_ts (issue #32): bounds the run scan to runs started at/after
        this RFC3339 cutoff. None (default) is unbounded."""
        since_clause = "AND started >= ?" if since_ts else ""
        params: list = [since_ts] if since_ts else []
        runs_cur = conn.execute(
            f"""
            SELECT run_id, project
            FROM runs
            WHERE close_reason IS NOT NULL
              {since_clause}
            ORDER BY started
            """,
            params,
        )
        runs = [(r[0], r[1]) for r in runs_cur.fetchall()]
        is_verify = build_verifier()

        closed = {r for r, _p in runs}
        refs = {
            ref.run_id: ref
            for ref in run_refs(conn, since_ts=since_ts, roots=self.transcript_roots)
            if ref.run_id in closed and ref.transcript is not None
        }
        cache = StepCache()

        candidates: list[PatternCandidate] = []
        for run_id, project in runs:
            ref = refs.get(run_id)
            steps = cache.steps_for(ref) if ref else []
            if steps:
                n_edits, verified = transcript_verdict(steps, project, is_verify)
                if n_edits >= EDIT_THRESHOLD and not verified:
                    candidates.append(self._transcript_candidate(project, run_id, n_edits, ref))
                continue
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

    def _transcript_candidate(self, project, run_id, n_edits, ref) -> PatternCandidate:
        return PatternCandidate(
            project=project,
            pattern_name="unverified_completion",
            signature=f"{project}:{self.DETECTOR_NAME}",
            detector=self.DETECTOR_NAME,
            occurrences=1,
            evidence=[
                f"project={project}",
                f"run_id={run_id}",
                f"agent={ref.agent_label}",
                f"code_edits={n_edits} no_verify_after_last_edit",
                "tier=transcript",
            ],
            run_ids=[run_id],
            proposed_remediation=(
                "AUTOMATE: this agent made code edits and ran NO build/test after "
                "the last one. Add a pre-finish gate requiring a build/test run (or "
                "an explicit recorded 'no test applicable') before a code-modifying "
                "agent reports done."
            ),
            extra={
                "unverified_agents": 1,
                "total_unverified_code_edits": n_edits,
                "remediation_rung": "automate",
                "tier": "transcript",
            },
        )
