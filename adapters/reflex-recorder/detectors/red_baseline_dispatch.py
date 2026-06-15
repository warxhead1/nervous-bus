"""detectors/red_baseline_dispatch.py — A1 in the orchestration-quality family.

Fires when an orchestrator launched a fan-out (>=1 Agent/Task dispatch) while its
own baseline was RED — the last build/test before the fan-out had failed — or
while NO green signal had been established before a multi-agent fan-out.

WHY THIS MATTERS (lived this session)
=====================================
The orchestrator dispatched 5 agents onto a failing test suite, then seeded
"treat the synthesis failure as pre-existing" into their prompts. Every child
then burned turns re-confirming a breakage that was actually ours. The cause was
a single dispatch-time fact — red baseline — that no detector saw. This is the
Eliminate rung: a pre-dispatch gate that refuses (or snapshots+labels) a red
baseline removes the whole downstream tax (the B1 preexisting_confirmation_tax
and C1 inherited_rationalization both descend from it).

SIGNAL
======
Per closed run, extract Agent/Task dispatches and group them into fan-out cohorts
(dispatch_lineage.group_cohorts). For each cohort, take the FIRST dispatch and
reconstruct the baseline as of that moment (last_test_signal_before):

  - baseline "failed"  -> fire kind=red          (always; this is the lived case)
  - baseline "absent"  -> fire kind=no_baseline  ONLY when cohort width >= MIN_NO_BASELINE_WIDTH
                          (a real fan-out launched with no established green)
  - baseline "passed" / "unknown" -> no fire

One candidate per firing cohort. The signature is stable across runs
(project + kind) so the recurrence/prevalence layer answers "how often does this
orchestrator dispatch on red?" run_ids carries the offending runs.

REMEDIATION LADDER
==================
kind=red          -> ELIMINATE: pre-dispatch gate. Fix the baseline (or explicitly
                    snapshot+label it) before fan-out; never seed "pre-existing".
kind=no_baseline  -> AUTOMATE: auto-inject a baseline snapshot into child prompts
                    so children don't each rediscover (or wrongly assume) the state.
"""
from __future__ import annotations

import sqlite3

from detectors.base import BaseDetector, PatternCandidate
from detectors.dispatch_lineage import (
    group_cohorts,
    last_test_signal_before,
    load_run_events,
    parse_dispatches,
)

# A fan-out must be at least this wide for an ABSENT baseline to be worth flagging.
# A single delegated agent with no prior test is routine; >=3 parallel agents with
# no established green is the over-fan-on-unknown-ground shape worth informing.
MIN_NO_BASELINE_WIDTH = 3


class RedBaselineDispatchDetector(BaseDetector):
    """Detect fan-outs launched on a red or unestablished baseline (A1)."""

    DETECTOR_NAME = "red_baseline_dispatch"

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

        candidates: list[PatternCandidate] = []
        for run_id, project in runs:
            events = load_run_events(conn, run_id)
            if not events:
                continue
            dispatches = parse_dispatches(events)
            if not dispatches:
                continue
            cohorts = group_cohorts(dispatches)

            for cohort in cohorts:
                first = cohort[0]
                width = len(cohort)
                signal = last_test_signal_before(events, first.seq)

                if signal.status == "failed":
                    kind = "red"
                elif signal.status == "absent" and width >= MIN_NO_BASELINE_WIDTH:
                    kind = "no_baseline"
                else:
                    continue

                candidates.append(self._candidate(
                    project, run_id, kind, cohort, signal, width,
                ))

        return candidates

    def _candidate(
        self, project, run_id, kind, cohort, signal, width,
    ) -> PatternCandidate:
        first = cohort[0]
        signature = f"{project}:{self.DETECTOR_NAME}:{kind}"

        if kind == "red":
            remediation = (
                "ELIMINATE: fan-out launched while the baseline was RED (last "
                f"build/test failed: `{signal.command}`). Gate the dispatch on a "
                "green baseline — fix the failure first, or snapshot+label it "
                "explicitly. Do NOT seed 'treat as pre-existing' into child "
                "prompts; that propagates the wrong assumption to every sibling "
                "(see inherited_rationalization / preexisting_confirmation_tax)."
            )
        else:
            remediation = (
                "AUTOMATE: a "
                f"{width}-agent fan-out launched with NO established green "
                "baseline (no build/test ran before dispatch). Auto-inject a "
                "baseline snapshot into the child prompts so each sibling starts "
                "from known ground instead of rediscovering or assuming it."
            )

        evidence = [
            f"project={project}",
            f"run_id={run_id}",
            f"kind={kind}",
            f"cohort_width={width}",
            f"first_dispatch_seq={first.seq}",
            f"baseline_status={signal.status}",
        ]
        if signal.command:
            evidence.append(f"last_build_cmd={signal.command}")
            evidence.append(f"baseline_signal_seq={signal.seq}")
        # A few child ids for the lineage join (C-family will consume these).
        child_ids = [d.child_agent_id for d in cohort if d.child_agent_id][:5]
        if child_ids:
            evidence.append("child_agent_ids=" + ",".join(child_ids))

        return PatternCandidate(
            project=project,
            pattern_name="red_baseline_dispatch",
            signature=signature,
            detector=self.DETECTOR_NAME,
            occurrences=width,
            evidence=evidence,
            run_ids=[run_id],
            proposed_remediation=remediation,
            extra={
                "kind": kind,
                "cohort_width": width,
                "baseline_status": signal.status,
                "last_build_cmd": signal.command,
                "remediation_rung": "eliminate" if kind == "red" else "automate",
                "child_agent_ids": child_ids,
                "dispatch_names": [d.name for d in cohort if d.name][:8],
                "dispatch_models": sorted({d.model for d in cohort if d.model}),
            },
        )
