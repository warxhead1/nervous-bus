"""Live iteration-header tally tests (bead nervous-bus-2ktd).

The IterationProgressPanel header used to show ``iteration 0  cases 0/0
Verdicts: OK 0  CE 0  WA 0...`` for the entire duration of an iteration,
because the header was driven only by ``autobench.iteration.summary.v1``
which fires at iteration BOUNDARIES.

These tests pin the contract that:

1. Each ``autobench.case.result.v1`` event for the current iteration
   advances the in-flight tally exposed by ``PulseState.iteration_progress``.
2. On ``autobench.iteration.summary.v1`` the final tally is snapshotted to
   ``PulseState.iteration_history`` so the lineage strip can render prior
   iterations after the running tally has rolled into the next iteration.
3. On the next ``autobench.iteration.v1`` start, the running tally resets
   to zero for the new iteration without losing the snapshotted history.
"""

from __future__ import annotations

from pulse_app.state import PulseState

SID = "sess-live"


def _iter_start(iteration: int, session_id: str = SID) -> dict:
    return {
        "type": "autobench.iteration.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "status": "start",
            "harness_version": "v0",
        },
    }


def _iter_complete(iteration: int, session_id: str = SID) -> dict:
    return {
        "type": "autobench.iteration.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "status": "complete",
            "aggregate_score": 0.42,
            "verdict_counts": {"OK": 3, "CE": 1, "WA": 1},
            "harness_version": "v0",
        },
    }


def _case_result(
    iteration: int,
    case_id: str,
    verdict: str,
    latency_ms: float = 1000.0,
    session_id: str = SID,
) -> dict:
    return {
        "type": "autobench.case.result.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "case_id": case_id,
            "language": "python",
            "verdict": verdict,
            "p_score": 1.0 if verdict == "OK" else 0.0,
            "latency_ms": latency_ms,
            "generated_code": "x",
            "generated_code_length": 1,
        },
    }


def _iter_summary(
    iteration: int,
    distribution: dict,
    num_cases: int = 5,
    session_id: str = SID,
) -> dict:
    return {
        "type": "autobench.iteration.summary.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "aggregate_score": 0.4,
            "pass_rate": 0.4,
            "total_latency_ms": 5000.0,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "verdict_distribution": distribution,
            "num_cases": num_cases,
            "harness_version": "v0",
            "ce_rate": float(distribution.get("CE", 0)) / max(1, num_cases),
            "ok_rate": float(distribution.get("OK", 0)) / max(1, num_cases),
        },
    }


def test_five_case_results_within_iteration_drive_header_tally():
    """5 mixed case.result.v1 events → header tally reflects 5 cases + verdict mix."""
    s = PulseState()
    s.apply(_iter_start(0))

    mix = [("OK", "c0"), ("OK", "c1"), ("CE", "c2"), ("WA", "c3"), ("OK", "c4")]
    for verdict, case_id in mix:
        s.apply(_case_result(0, case_id, verdict))

    snap = s.iteration_progress()
    assert snap is not None
    assert snap["iteration"] == 0
    assert snap["status"] == "start"
    # Header counter shows live tally — NOT zero.
    assert snap["cases_done"] == 5
    # Verdict bar reflects the actual mix.
    assert snap["verdict_counts"] == {"OK": 3, "CE": 1, "WA": 1}


def test_iteration_summary_snapshots_to_history_and_running_tally_persists():
    """On iteration.summary.v1 boundary the final tally lands in history."""
    s = PulseState()
    s.apply(_iter_start(0))
    for verdict, cid in [("OK", "c0"), ("OK", "c1"), ("CE", "c2"), ("WA", "c3"), ("OK", "c4")]:
        s.apply(_case_result(0, cid, verdict))

    # No history yet — still mid-iteration.
    assert len(s.iteration_history) == 0

    # Boundary fires.
    s.apply(_iter_summary(0, {"OK": 3, "CE": 1, "WA": 1}, num_cases=5))

    assert len(s.iteration_history) == 1
    entry = s.iteration_history[0]
    assert entry["iteration"] == 0
    assert entry["num_cases"] == 5
    assert entry["verdict_counts"] == {"OK": 3, "CE": 1, "WA": 1}
    assert entry["session_id"] == SID


def test_next_iteration_start_resets_running_tally_but_keeps_history():
    """iteration.v1 status=start for iter N+1 zeros the live tally, history kept."""
    s = PulseState()
    s.apply(_iter_start(0))
    for verdict, cid in [("OK", "c0"), ("CE", "c1"), ("WA", "c2")]:
        s.apply(_case_result(0, cid, verdict))
    s.apply(_iter_complete(0))
    s.apply(_iter_summary(0, {"OK": 1, "CE": 1, "WA": 1}, num_cases=3))

    # History captured iter 0.
    assert len(s.iteration_history) == 1

    # Begin iter 1.
    s.apply(_iter_start(1))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["iteration"] == 1
    assert snap["cases_done"] == 0
    assert snap["verdict_counts"] == {}

    # History still intact — lineage strip can read prior iter tally.
    assert len(s.iteration_history) == 1
    assert s.iteration_history[0]["iteration"] == 0


def test_history_grows_across_multiple_iterations():
    """Each summary appends; oldest evicted only at the deque cap."""
    s = PulseState()
    for n in range(3):
        s.apply(_iter_start(n))
        s.apply(_case_result(n, f"c{n}", "OK"))
        s.apply(_iter_complete(n))
        s.apply(_iter_summary(n, {"OK": 1}, num_cases=1))

    assert len(s.iteration_history) == 3
    assert [e["iteration"] for e in s.iteration_history] == [0, 1, 2]
