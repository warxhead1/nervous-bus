"""detectors/foreground_wait.py — a lane blocked in hand-rolled poll loops.

SIGNAL
======
Within one run: foreground Bash calls that are hand-rolled waits. A wait is a
loop whose body sleeps (`until grep -q "CHECK PASS" log; do sleep 10; done`,
`for i in $(seq 1 55); do ...; sleep 10; done`) or a standalone `sleep` of at
least 30 s (transcript_steps.is_poll_wait). A call made with
`run_in_background` is not a foreground wait. The detector fires when the run
spent at least MIN_WAIT_MIN minutes inside such calls.

While the loop polls, the agent can do nothing else. Usually it is waiting on
a job it launched itself, detached, a few calls earlier. Running that job in
the foreground with a timeout gives the same result with no polling, and
`run_in_background` lets the harness wake the agent on exit while independent
work goes ahead. Either one also keeps the result tied to the job, so the agent
does not read a marker in a log that may be stale.

The threshold was derived from real lanes. The derivation and its margins are
in the PR that added this detector.

Reads transcripts (transcript_steps); the bus summary does not carry durations.
Remediation rung: INFORM. Automate candidate: a PreToolUse hook that warns on a
foreground sleep-poll loop.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate
from transcript_steps import Step, StepCache, is_foreground_bash, is_poll_wait, run_refs

MIN_WAIT_MIN = 4.0


def wait_calls(steps: list[Step]) -> list[Step]:
    """Foreground Bash calls that are hand-rolled waits, in run order."""
    return [s for s in steps if is_foreground_bash(s) and is_poll_wait(s.command)]


def wait_minutes(calls: list[Step]) -> float:
    return sum(s.duration for s in calls) / 60.0


def fires(calls: list[Step], t: float = MIN_WAIT_MIN) -> bool:
    return wait_minutes(calls) >= t


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(s: str, n: int = 120) -> str:
    one = " ".join(s.split())
    return one if len(one) <= n else one[: n - 3] + "..."


def describe(calls: list[Step], wall_s: Optional[float] = None) -> list[str]:
    mins = wait_minutes(calls)
    share = f" ({100 * mins * 60 / wall_s:.0f}% of the run's wall time)" if wall_s else ""
    ev = [f"{len(calls)} foreground poll-wait calls, {mins:.1f} min blocked{share}"]
    for s in sorted(calls, key=lambda s: -s.duration)[:3]:
        ev.append(f"  {_iso(s.t0)} {s.duration / 60:.1f} min: {_short(s.command)}")
    return ev


class ForegroundWaitDetector(BaseDetector):
    """Flag runs that spend minutes inside foreground sleep-poll loops."""

    DETECTOR_NAME = "foreground_wait"
    transcript_roots: Optional[list[Path]] = None

    def detect(self, conn: sqlite3.Connection,
               since_ts: Optional[str] = None) -> list[PatternCandidate]:
        cache = StepCache()
        out: list[PatternCandidate] = []
        for ref in run_refs(conn, since_ts=since_ts, roots=self.transcript_roots,
                            min_events=2):
            steps = cache.steps_for(ref)
            calls = wait_calls(steps)
            if not fires(calls):
                continue
            wall = (max(s.t1 or s.t0 for s in steps) - steps[0].t0) if steps else None
            out.append(PatternCandidate(
                project=ref.project,
                pattern_name="foreground_wait",
                signature=f"{ref.project}:{self.DETECTOR_NAME}",
                detector=self.DETECTOR_NAME,
                occurrences=len(calls),
                evidence=[f"run_id={ref.run_id}", f"agent={ref.agent_label}"]
                + describe(calls, wall),
                run_ids=[ref.run_id],
                proposed_remediation=(
                    "Do not poll in the foreground. Run the job itself in the foreground "
                    "with a timeout when nothing else can proceed, or with run_in_background "
                    "and do the independent work while it runs (the harness wakes you on "
                    "exit)."),
                extra={
                    "remediation_rung": "inform",
                    "wait_calls": len(calls),
                    "wait_min": round(wait_minutes(calls), 1),
                    "thresholds": {"min_wait_min": MIN_WAIT_MIN},
                },
            ))
        return out
