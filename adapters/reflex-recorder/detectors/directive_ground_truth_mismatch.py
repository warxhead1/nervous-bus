"""detectors/directive_ground_truth_mismatch.py — A2 in the orchestration family.

A dispatch prompt asserts a baseline fact that the parent's OWN test/build reality
contradicts: "tests are green", "X is pre-existing", "the baseline is clean" — when
the last build/test before the fan-out actually FAILED. Lived this session: seeded
"treat the synthesis failure as pre-existing" into 5 prompts when it was our own
fresh breakage, and every child then burned turns re-confirming it.

This is the directive-side twin of A1 (red_baseline_dispatch): A1 fires on the red
baseline itself; A2 fires when the dispatch text actively MISREPRESENTS that red
baseline as clean/known. A2 is strictly worse than a silent red baseline because it
propagates a false ground truth.

SIGNAL (per run — prompt and baseline co-occur in the same run)
==============================================================
For each Agent/Task dispatch whose prompt makes a baseline-clean / pre-existing
assertion (CLEAN_CLAIM_PATTERNS) AND whose reconstructed baseline
(last_test_signal_before) is "failed": fire. The prompt now carries up to 1000
chars (dispatch-tool bound) and is recovered truncation-tolerantly by
parse_dispatches, so the assertion is visible.

REMEDIATION LADDER
==================
INFORM -> AUTOMATE: replace the asserted-but-false claim with the REAL baseline
snapshot, auto-injected. The orchestrator should never hand-assert baseline state
it didn't verify; the snapshot (A1's machinery) is ground truth.
"""
from __future__ import annotations

import re
import sqlite3

from detectors.base import BaseDetector, PatternCandidate
from detectors.dispatch_lineage import (
    group_cohorts,
    last_test_signal_before,
    load_run_events,
    parse_dispatches,
)

# Prompt phrasings that assert a clean / known-failing baseline. Kept conservative
# (high precision) — we only fire when the prompt AFFIRMATIVELY claims state.
CLEAN_CLAIM_PATTERNS = (
    r"tests?\s+(?:are\s+)?(?:all\s+)?(?:green|passing|clean)",
    r"suite\s+is\s+(?:green|passing|clean)",
    r"baseline\s+is\s+(?:green|clean|passing)",
    r"pre.?existing\s+(?:failure|breakage|test)",
    r"as\s+pre.?existing",
    r"treat\s+.{0,40}?\bas\s+pre.?existing",
    r"(?:failure|test)\s+is\s+pre.?existing",
    r"already\s+(?:failing|broken)\s+(?:before|on\s+main)",
    r"known(?:[ -]good)?\s+baseline",
)

_CLAIM_RE = re.compile("|".join(CLEAN_CLAIM_PATTERNS), re.IGNORECASE)


def find_clean_claim(prompt: str) -> str:
    """Return the matched baseline-clean assertion in a prompt, or ""."""
    if not prompt:
        return ""
    m = _CLAIM_RE.search(prompt)
    return m.group(0).strip() if m else ""


class DirectiveGroundTruthMismatchDetector(BaseDetector):
    """Detect dispatches asserting a clean baseline that reality contradicts (A2)."""

    DETECTOR_NAME = "directive_ground_truth_mismatch"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        runs = conn.execute(
            "SELECT run_id, project FROM runs WHERE close_reason IS NOT NULL ORDER BY started"
        ).fetchall()

        candidates: list[PatternCandidate] = []
        for run_id, project in runs:
            events = load_run_events(conn, run_id)
            if not events:
                continue
            dispatches = parse_dispatches(events)
            if not dispatches:
                continue
            # Group into cohorts so one fan-out that repeats a claim across N
            # siblings fires once, not N times.
            for cohort in group_cohorts(dispatches):
                claim = ""
                claimant = None
                for d in cohort:
                    c = find_clean_claim(d.prompt)
                    if c:
                        claim, claimant = c, d
                        break
                if not claim:
                    continue
                signal = last_test_signal_before(events, cohort[0].seq)
                if signal.status != "failed":
                    continue

                candidates.append(self._candidate(
                    project, run_id, cohort, claimant, claim, signal,
                ))

        return candidates

    def _candidate(self, project, run_id, cohort, claimant, claim, signal):
        signature = f"{project}:{self.DETECTOR_NAME}"
        evidence = [
            f"project={project}",
            f"run_id={run_id}",
            f"cohort_width={len(cohort)}",
            f"asserted_claim={claim!r}",
            f"actual_baseline={signal.status}",
            f"contradicting_cmd={signal.command}",
        ]
        if claimant and claimant.name:
            evidence.append(f"claimant_dispatch={claimant.name}")

        return PatternCandidate(
            project=project,
            pattern_name="directive_ground_truth_mismatch",
            signature=signature,
            detector=self.DETECTOR_NAME,
            occurrences=len(cohort),
            evidence=evidence,
            run_ids=[run_id],
            proposed_remediation=(
                f"AUTOMATE: the dispatch asserts {claim!r} but the parent's last "
                f"build/test FAILED (`{signal.command}`) — a false ground truth seeded "
                "into the fan-out. Never hand-assert baseline state; auto-inject the "
                "real baseline snapshot (A1 machinery) so children start from truth "
                "instead of re-confirming a wrong claim."
            ),
            extra={
                "asserted_claim": claim,
                "actual_baseline": signal.status,
                "contradicting_cmd": signal.command,
                "cohort_width": len(cohort),
                "remediation_rung": "automate",
            },
        )
