"""Tests for IterationProgressPanel + PulseState progress tracking.

Covers bead nervous-bus-4cw9 acceptance criteria:

  * iteration_start sets bar to 0/total
  * 5 case.result events → 5/20 bar position
  * per-case avg from rolling last-5 correctness
  * ETA formula with known latency
  * iteration_complete event resets for next iter (panel idles)
  * Headless: rendered bar fills proportionally
"""

from __future__ import annotations

import time

import pytest

from pulse_app.state import (
    DEFAULT_CASES_PER_ITERATION,
    DEFAULT_ITER_OVERHEAD_S,
    LATENCY_WINDOW_CASES,
    PulseState,
)
from pulse_app.widgets import (
    IterationProgressPanel,
    _build_unicode_bar,
    _format_abs_clock,
    _format_duration,
)


SID = "01KRQMD4M20RYCDS8X5CHWTPMP"


def _iter_start(iteration: int, session_id: str = SID) -> dict:
    return {
        "type": "autobench.iteration.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "harness_version": "v0",
            "status": "start",
        },
    }


def _iter_complete(iteration: int, score: float = 0.5, session_id: str = SID) -> dict:
    return {
        "type": "autobench.iteration.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "harness_version": "v0",
            "status": "complete",
            "aggregate_score": score,
            "verdict_counts": {"OK": 2},
            "improvement_delta": {"summary": "x"},
        },
    }


def _case_result(
    iteration: int,
    case_id: str,
    verdict: str = "OK",
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


def _iter_summary(iteration: int, num_cases: int = 20, session_id: str = SID) -> dict:
    return {
        "type": "autobench.iteration.summary.v1",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "aggregate_score": 0.5,
            "pass_rate": 0.5,
            "total_latency_ms": 20000.0,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "verdict_distribution": {"OK": num_cases},
            "num_cases": num_cases,
            "harness_version": "v0",
            "ce_rate": 0.0,
            "ok_rate": 0.5,
        },
    }


# ── State unit tests ────────────────────────────────────────────────────────


def test_iteration_start_zeros_the_bar():
    s = PulseState()
    s.apply(_iter_start(0))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["iteration"] == 0
    assert snap["cases_done"] == 0
    assert snap["cases_total"] == DEFAULT_CASES_PER_ITERATION  # 20
    assert snap["eta_s"] is None  # no latency samples yet
    assert snap["status"] == "start"


def test_five_case_results_advance_bar_to_5_of_20():
    s = PulseState()
    s.apply(_iter_start(0))
    for i in range(5):
        s.apply(_case_result(0, f"case-{i}", latency_ms=2000.0))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["cases_done"] == 5
    assert snap["cases_total"] == 20
    assert snap["verdict_counts"] == {"OK": 5}


def test_rolling_avg_uses_last_five_latencies():
    """Per-case avg comes from the LAST LATENCY_WINDOW_CASES samples."""
    s = PulseState()
    s.apply(_iter_start(0))
    # 3 early high-latency cases that should fall out of the window
    for i in range(3):
        s.apply(_case_result(0, f"slow-{i}", latency_ms=9000.0))
    # 5 recent fast cases (each 1000 ms) — these are what avg should reflect
    for i in range(5):
        s.apply(_case_result(0, f"fast-{i}", latency_ms=1000.0))
    snap = s.iteration_progress()
    assert snap is not None
    # window is last 5 → all fast cases → avg = 1000 ms
    assert snap["avg_case_latency_ms"] == pytest.approx(1000.0)
    # Sanity: LATENCY_WINDOW_CASES is the documented constant
    assert LATENCY_WINDOW_CASES == 5


def test_eta_formula_with_known_latency():
    """ETA = remaining * avg + iter_overhead."""
    s = PulseState()
    s.apply(_iter_start(0))
    s.apply(_iter_summary(0, num_cases=20))  # locks total to 20
    # 4 cases at 500ms each → avg 500ms
    for i in range(4):
        s.apply(_case_result(0, f"c-{i}", latency_ms=500.0))
    snap = s.iteration_progress(iter_overhead_s=15.0)
    assert snap is not None
    assert snap["cases_done"] == 4
    assert snap["cases_total"] == 20
    # remaining = 16, avg = 0.5s, overhead = 15s → eta = 16 * 0.5 + 15 = 23s
    assert snap["eta_s"] == pytest.approx(23.0, abs=1e-6)


def test_iteration_complete_marks_status_and_panel_idles():
    s = PulseState()
    s.apply(_iter_start(0))
    for i in range(3):
        s.apply(_case_result(0, f"c-{i}"))
    s.apply(_iter_complete(0))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["status"] == "complete"
    # Starting the NEXT iteration must reset cases_done back to 0
    s.apply(_iter_start(1))
    snap2 = s.iteration_progress()
    assert snap2 is not None
    assert snap2["iteration"] == 1
    assert snap2["cases_done"] == 0
    assert snap2["status"] == "start"


def test_iter_summary_locks_total_for_subsequent_iterations():
    """First summary sets sess.last_known_num_cases; next iter uses it."""
    s = PulseState()
    s.apply(_iter_start(0))
    s.apply(_iter_summary(0, num_cases=12))
    s.apply(_iter_complete(0))
    s.apply(_iter_start(1))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["cases_total"] == 12  # propagated from prior summary


def test_dedup_same_case_id_does_not_double_count():
    s = PulseState()
    s.apply(_iter_start(0))
    s.apply(_case_result(0, "dup", latency_ms=1000.0))
    s.apply(_case_result(0, "dup", latency_ms=1000.0))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["cases_done"] == 1


def test_iteration_progress_none_when_no_session():
    s = PulseState()
    assert s.iteration_progress() is None


# ── Widget helpers ──────────────────────────────────────────────────────────


def test_unicode_bar_fills_proportionally():
    # 0/20 → all empty
    bar = _build_unicode_bar(0, 20, width=20)
    assert bar == "░" * 20
    # 20/20 → all full
    assert _build_unicode_bar(20, 20, width=20) == "█" * 20
    # 12/20 with width=20 → 12 filled, 8 empty
    bar = _build_unicode_bar(12, 20, width=20)
    assert bar.count("█") == 12
    assert bar.count("░") == 8
    assert bar == "█" * 12 + "░" * 8


def test_format_duration_human_friendly():
    assert _format_duration(42) == "42s"
    assert _format_duration(504) == "8m24s"
    assert _format_duration(3723) == "1h02m"
    assert _format_duration(0) == "0s"
    # Negative clamps to 0
    assert _format_duration(-5) == "0s"


def test_format_abs_clock_returns_hhmm():
    out = _format_abs_clock(time.time())
    assert len(out) == 5
    assert out[2] == ":"
    h, m = out.split(":")
    assert 0 <= int(h) <= 23
    assert 0 <= int(m) <= 59


# ── Headless rendering smoke test ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_panel_renders_proportionally():
    """End-to-end: panel mounts and renders a 12/20 bar without crashing."""
    from textual.app import App, ComposeResult

    panel = IterationProgressPanel()

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield panel

    app = _Host()
    async with app.run_test() as pilot:
        snap = {
            "session_id": SID,
            "iteration": 1,
            "total_iterations": 5,
            "status": "start",
            "cases_done": 12,
            "cases_total": 20,
            "verdict_counts": {"OK": 2, "CE": 9, "TLE": 1},
            "avg_case_latency_ms": 42_000.0,
            "elapsed_s": 504.0,
            "eta_s": 336.0,
            "started_at": time.time() - 504.0,
            "completed_at": None,
        }
        panel.progress = snap
        await pilot.pause()
        # The widget's renderable text should mention header + counts.
        rendered = panel.render()
        text = str(rendered)
        # Heading + key fields present in the markup
        assert "Iteration 1 / 5" in text
        assert "12" in text and "20" in text
        # Bar must contain filled and empty blocks roughly proportional to 60%
        filled = text.count("█")
        empty = text.count("░")
        assert filled > 0 and empty > 0
        # 12/20 of 20-wide bar → 12 filled, 8 empty (allow ±1 for rounding)
        assert abs(filled - 12) <= 1
        assert abs(empty - 8) <= 1


@pytest.mark.asyncio
async def test_progress_panel_idle_without_data():
    """No progress snapshot → muted idle message, no crash."""
    from textual.app import App, ComposeResult

    panel = IterationProgressPanel()

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield panel

    app = _Host()
    async with app.run_test() as pilot:
        panel.progress = None
        await pilot.pause()
        text = str(panel.render())
        assert "Awaiting" in text or "computing" in text or "—" in text


@pytest.mark.asyncio
async def test_progress_panel_eta_computing_when_no_latency():
    """Degrade message when no latency samples yet."""
    from textual.app import App, ComposeResult

    panel = IterationProgressPanel()

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield panel

    app = _Host()
    async with app.run_test() as pilot:
        panel.progress = {
            "session_id": SID,
            "iteration": 0,
            "total_iterations": 5,
            "status": "start",
            "cases_done": 0,
            "cases_total": 20,
            "verdict_counts": {},
            "avg_case_latency_ms": None,
            "elapsed_s": 1.0,
            "eta_s": None,
            "started_at": time.time(),
            "completed_at": None,
        }
        await pilot.pause()
        text = str(panel.render())
        assert "computing" in text.lower()


def test_default_iter_overhead_constant_exists():
    """Guard so the constant isn't silently removed."""
    assert DEFAULT_ITER_OVERHEAD_S > 0


# ── nervous-bus-yn9v fix 1+2: post-complete collapse ────────────────────────


def test_iteration_progress_carries_history_snapshot_when_complete():
    """A completed iteration's progress snapshot includes the rollup row.

    Before yn9v fix 1+2 the panel rendered a 6-row "Iter N complete /
    Cases: 0/20 100% / Awaiting iter N+1…" block. Now we plumb the
    iteration.summary.v1 rollup into the snapshot so the panel can
    collapse to one row with real numbers.
    """
    s = PulseState()
    s.apply(_iter_start(0))
    for i in range(20):
        s.apply(_case_result(0, f"c-{i}", latency_ms=500.0))
    s.apply(_iter_complete(0, score=0.628))
    s.apply(_iter_summary(0, num_cases=20))
    snap = s.iteration_progress()
    assert snap is not None
    assert snap["status"] == "complete"
    hist = snap.get("history_snapshot")
    assert hist is not None, "expected iteration_history rollup on complete"
    assert hist.get("num_cases") == 20
    assert hist.get("verdict_counts") == {"OK": 20}
    assert snap.get("aggregate_score") is not None


def test_complete_panel_renders_single_row_summary():
    """Completed-iter render is <=2 rendered rows.

    Acceptance criterion (1): "IterationProgressPanel collapses when iter
    complete + next not started — visual test asserts <=2 rows".
    """
    panel = IterationProgressPanel()
    snap = {
        "session_id": SID,
        "iteration": 1,
        "total_iterations": None,
        "status": "complete",
        "cases_done": 0,  # the bug we're fixing — would have rendered 0/20
        "cases_total": 20,
        "verdict_counts": {},
        "avg_case_latency_ms": None,
        "elapsed_s": 0.0,
        "eta_s": None,
        "started_at": time.time(),
        "completed_at": time.time(),
        "history_snapshot": {
            "session_id": SID,
            "iteration": 1,
            "num_cases": 20,
            "verdict_counts": {"OK": 16, "WA": 2, "CE": 2},
            "aggregate_score": 0.628,
        },
        "aggregate_score": 0.628,
    }
    out = panel._render_complete(snap, snap["iteration"])
    # AC (1): max 2 rendered rows. We emit 1 line.
    assert out.count("\n") <= 1
    # AC (2): cases denominator matches the iteration's real num_cases —
    # NOT the "0/20" the live tally would yield.
    assert "20/20" in out
    # The bug was "Cases: 0/20" — that exact framing must be gone.
    assert "Cases:" not in out
    # Score is present
    assert "0.628" in out
    # Verdict roll-up present with palette colours
    assert "OK:16" in out
    assert "WA:2" in out
    assert "CE:2" in out


def test_complete_panel_falls_back_to_live_when_no_history():
    """Without an iteration_history snapshot we still render a sensible row."""
    panel = IterationProgressPanel()
    snap = {
        "session_id": SID,
        "iteration": 2,
        "total_iterations": None,
        "status": "complete",
        "cases_done": 5,
        "cases_total": 20,
        "verdict_counts": {"OK": 5},
        "avg_case_latency_ms": None,
        "elapsed_s": 0.0,
        "eta_s": None,
        "started_at": time.time(),
        "completed_at": time.time(),
        "history_snapshot": None,
        "aggregate_score": None,
    }
    out = panel._render_complete(snap, snap["iteration"])
    assert out.count("\n") <= 1
    # Without history, denominator falls back to cases_total
    assert "20/20" in out or "5/20" in out
    # Live verdict counts surface as the fallback
    assert "OK:5" in out
    # Score "—" when unknown
    assert "—" in out


def test_ahe_history_dots_scoped_to_latest_prediction_session():
    """Fix 6: scope dots to current session by default."""
    s = PulseState()
    # Two sessions, two predictions in old + 1 prediction in new
    s.apply({"type": "autobench.improver.prediction.v1",
             "data": {"session_id": "01KOLD000000000000000000AAA",
                      "iteration": 0, "confidence": 0.5,
                      "predicted_score_delta": 0.1, "rationale": "x"}})
    s.apply({"type": "autobench.improver.prediction.v1",
             "data": {"session_id": "01KOLD000000000000000000AAA",
                      "iteration": 1, "confidence": 0.5,
                      "predicted_score_delta": 0.1, "rationale": "x"}})
    s.apply({"type": "autobench.improver.prediction.v1",
             "data": {"session_id": "01KNEW000000000000000000BBB",
                      "iteration": 0, "confidence": 0.5,
                      "predicted_score_delta": 0.1, "rationale": "x"}})
    payload = s.ahe_prediction_panel_payload()
    assert payload is not None
    assert len(payload["history_dots"]) == 1
    assert payload["history_dots_scope"]["cross_session_count"] == 2


def test_latest_completed_iteration_summary_returns_most_recent():
    """Fix 3 plumbing: CEPatternPanel empty state can read this."""
    s = PulseState()
    s.apply(_iter_start(0))
    s.apply(_iter_complete(0))
    s.apply(_iter_summary(0, num_cases=20))
    snap = s.latest_completed_iteration_summary()
    assert snap is not None
    assert snap["num_cases"] == 20
    assert snap["verdict_counts"] == {"OK": 20}
