"""Unit + headless-render tests for the AHEPredictionTracker widget.

The tracker shows the lifecycle of each falsifiable prediction the improver
emits. Tests cover:

  * Pending state — prediction event arrived, nothing else yet.
  * Refuted-live state — partial-actuals refutation came in mid-iteration.
  * Confirmed state — verification event arrived with outcome=confirmed.
  * The 5-record display cap when more than 5 predictions exist.
  * Headless render assertions that each status glyph (⚠/◯/✓/◐/✗) actually
    appears in the widget's emitted text.

We exercise the widget by setting its ``records`` reactive directly. The full
state-machine path (event-in → state-mutation → widget-update) is covered by
the state-level tests in ``tests/test_state.py``; these focus on the *visual
contract*.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import (
    PREDICTION_STATUS_CONFIRMED,
    PREDICTION_STATUS_PARTIAL,
    PREDICTION_STATUS_PENDING,
    PREDICTION_STATUS_REFUTED,
    PREDICTION_STATUS_REFUTED_LIVE,
    PredictionRecord,
    PulseState,
)
from pulse_app.widgets import (
    PREDICTION_TRACKER_LIMIT,
    AHEPredictionTracker,
)


# --------------------------------------------------------------------------- #
# Event-shape helpers — mirror the CloudEvents envelopes the bus actually emits.
# --------------------------------------------------------------------------- #

SID = "01KRQAHE_TRACKER_TEST_SESSION_X"


def _prediction_evt(
    iteration: int,
    confidence: float = 0.7,
    predicted_score_delta: float = 0.15,
    predicted_verdict_class_changes: dict | None = None,
    rationale: str = "tweak the system prompt to unlock OK on TLE-bound cases",
    model: str = "claude-sonnet",
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"pred-{iteration}",
        "source": "/autobench",
        "type": "autobench.improver.prediction.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": SID,
            "iteration": iteration,
            "model": model,
            "predicted_score_delta": predicted_score_delta,
            "predicted_verdict_class_changes": predicted_verdict_class_changes or {"OK": 3, "TLE": -3},
            "confidence": confidence,
            "rationale": rationale,
        },
    }


def _refuted_live_evt(
    iteration_n_plus_1: int,
    refutation_reason: str = "OK: predicted +30 but max achievable is +7 (so_far=2, prior=5, remaining=5)",
    is_refuted: bool = True,
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"refute-{iteration_n_plus_1}",
        "source": "/autobench",
        "type": "autobench.improver.prediction.refuted_live.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:35.000Z",
        "data": {
            "session_id": SID,
            "iteration": iteration_n_plus_1,
            "prediction": {
                "predicted_score_delta": 0.15,
                "predicted_verdict_class_changes": {"OK": 30},
                "confidence": 0.85,
                "rationale": "embedded payload for synth path",
            },
            "actuals_so_far": {"OK": 2, "TLE": 1},
            "remaining_cases": 5,
            "is_refuted": is_refuted,
            "refutation_reason": refutation_reason,
            "confidence_at_refute": 0.85,
        },
    }


def _verified_evt(
    iteration_n_plus_1: int,
    outcome_label: str = "confirmed",
    actual_score_delta: float = 0.025,
    score_delta_error: float = 0.005,
    confidence_calibration: float = 0.06,
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"verify-{iteration_n_plus_1}",
        "source": "/autobench",
        "type": "autobench.improver.prediction.verified.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:36.000Z",
        "data": {
            "session_id": SID,
            "iteration": iteration_n_plus_1,
            "predicted": {
                "predicted_score_delta": 0.02,
                "predicted_verdict_class_changes": {"OK": 1},
                "confidence": 0.45,
                "rationale": "conservative tweak",
            },
            "actual_score_delta": actual_score_delta,
            "actual_verdict_class_changes": {"OK": 1},
            "score_delta_error": score_delta_error,
            "verdict_match_ratio": 1.0,
            "outcome_label": outcome_label,
            "confidence_calibration": confidence_calibration,
        },
    }


# --------------------------------------------------------------------------- #
# Unit tests — state lifecycle.
# --------------------------------------------------------------------------- #


def test_prediction_event_creates_pending_record():
    """A single prediction event yields a PredictionRecord in pending state."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0, confidence=0.72, predicted_score_delta=0.150))
    records = s.recent_predictions()
    assert len(records) == 1
    r = records[0]
    assert r.status == PREDICTION_STATUS_PENDING
    assert r.iteration == 0
    assert abs(r.confidence - 0.72) < 1e-9
    assert abs(r.predicted_score_delta - 0.150) < 1e-9


def test_prediction_then_refuted_live_marks_record():
    """A refuted_live event flips the matching pending record to refuted_live."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0, confidence=0.85, predicted_score_delta=0.150,
                            predicted_verdict_class_changes={"OK": 30}))
    s.apply(_refuted_live_evt(
        iteration_n_plus_1=1,
        refutation_reason="OK: predicted +30 but max achievable is +7",
    ))
    records = s.recent_predictions()
    assert len(records) == 1
    r = records[0]
    assert r.status == PREDICTION_STATUS_REFUTED_LIVE
    assert "predicted +30" in r.refutation_reason
    assert "max achievable" in r.refutation_reason


def test_prediction_then_verified_confirmed_marks_record():
    """A verified(confirmed) event finalises the record with actuals + calibration."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=2, confidence=0.45, predicted_score_delta=0.020))
    s.apply(_verified_evt(
        iteration_n_plus_1=3,
        outcome_label="confirmed",
        actual_score_delta=0.025,
        score_delta_error=0.005,
        confidence_calibration=0.06,
    ))
    records = s.recent_predictions()
    assert len(records) == 1
    r = records[0]
    assert r.status == PREDICTION_STATUS_CONFIRMED
    assert r.actual_score_delta == pytest.approx(0.025)
    assert r.score_delta_error == pytest.approx(0.005)
    assert r.confidence_calibration == pytest.approx(0.06)


def test_predictions_capped_at_five_displayed():
    """Six prediction events exist in state; only the most recent 5 surface."""
    s = PulseState()
    for i in range(6):  # iterations 0..5
        s.apply(_prediction_evt(iteration=i))
    # State retains all six (so older ones are still observable for analysis…
    assert len(s.predictions) == 6
    # …but the widget-facing query caps at 5.
    capped = s.recent_predictions(limit=PREDICTION_TRACKER_LIMIT)
    assert len(capped) == PREDICTION_TRACKER_LIMIT == 5
    # Newest first → iteration 5 leads, iteration 0 falls off entirely.
    iters = [r.iteration for r in capped]
    assert iters[0] == 5
    assert 0 not in iters


def test_verified_outcomes_map_to_each_status():
    """Each outcome_label routes to its dedicated status constant."""
    cases = [
        ("confirmed", PREDICTION_STATUS_CONFIRMED),
        ("partial", PREDICTION_STATUS_PARTIAL),
        ("refuted", PREDICTION_STATUS_REFUTED),
    ]
    for label, expected in cases:
        s = PulseState()
        s.apply(_prediction_evt(iteration=0))
        s.apply(_verified_evt(iteration_n_plus_1=1, outcome_label=label))
        r = s.recent_predictions()[0]
        assert r.status == expected, f"{label} did not produce {expected}"


# --------------------------------------------------------------------------- #
# Headless-render tests — each status glyph appears in the rendered output.
# --------------------------------------------------------------------------- #


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def _mk_record(iteration: int, status: str, **overrides) -> PredictionRecord:
    base = dict(
        session_id=SID,
        iteration=iteration,
        confidence=0.7,
        predicted_score_delta=0.05,
        predicted_verdict_class_changes={"OK": 1},
        rationale="r",
        model="claude-sonnet",
        status=status,
    )
    base.update(overrides)
    return PredictionRecord(**base)


@pytest.mark.asyncio
async def test_tracker_empty_state_renders_placeholder():
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = tracker._render_content()
        assert "no predictions yet" in rendered.lower()


@pytest.mark.asyncio
async def test_tracker_renders_pending_glyph():
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        tracker.records = [_mk_record(0, PREDICTION_STATUS_PENDING)]
        await pilot.pause()
        text = tracker._render_content()
        assert "◯" in text
        assert "pending" in text


@pytest.mark.asyncio
async def test_tracker_renders_refuted_live_glyph_and_reason():
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        rec = _mk_record(
            1,
            PREDICTION_STATUS_REFUTED_LIVE,
            refutation_reason="OK: predicted +30 but max achievable is +7",
        )
        tracker.records = [rec]
        await pilot.pause()
        text = tracker._render_content()
        assert "⚠" in text
        # Reason snippet survives truncation at default 60 chars.
        assert "predicted +30" in text


@pytest.mark.asyncio
async def test_tracker_renders_confirmed_glyph():
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        tracker.records = [_mk_record(
            2,
            PREDICTION_STATUS_CONFIRMED,
            actual_score_delta=0.025,
            score_delta_error=0.005,
            confidence_calibration=0.06,
        )]
        await pilot.pause()
        text = tracker._render_content()
        assert "✓" in text
        assert "confirmed" in text


@pytest.mark.asyncio
async def test_tracker_renders_partial_glyph():
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        tracker.records = [_mk_record(
            3,
            PREDICTION_STATUS_PARTIAL,
            actual_score_delta=0.01,
            score_delta_error=0.05,
            confidence_calibration=0.4,
        )]
        await pilot.pause()
        text = tracker._render_content()
        assert "◐" in text
        assert "partial" in text


@pytest.mark.asyncio
async def test_tracker_renders_refuted_glyph():
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        tracker.records = [_mk_record(
            4,
            PREDICTION_STATUS_REFUTED,
            actual_score_delta=-0.041,
            score_delta_error=0.191,
            confidence_calibration=0.6,
        )]
        await pilot.pause()
        text = tracker._render_content()
        assert "✗" in text
        assert "refuted" in text


@pytest.mark.asyncio
async def test_tracker_caps_display_at_five():
    """Six records into a tracker → only the first five appear in the output."""
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        recs = [_mk_record(i, PREDICTION_STATUS_PENDING) for i in range(6)]
        tracker.records = recs
        await pilot.pause()
        text = tracker._render_content()
        # Each rendered header reads "iter N → N+1". The tracker should keep
        # the first five (newest-first ordering is the caller's responsibility).
        for i in range(5):
            assert f"iter {i} → {i + 1}" in text
        assert "iter 5 → 6" not in text


@pytest.mark.asyncio
async def test_tracker_truncates_long_rationale():
    """Refutation reason longer than the configured cap renders with an ellipsis."""
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        long_reason = "x" * 200
        rec = _mk_record(
            0,
            PREDICTION_STATUS_REFUTED_LIVE,
            refutation_reason=long_reason,
        )
        tracker.records = [rec]
        await pilot.pause()
        text = tracker._render_content()
        # The widget should NOT spill the full 200 chars verbatim into the row.
        assert long_reason not in text
        assert "…" in text


@pytest.mark.asyncio
async def test_tracker_pending_shows_case_progress():
    """When the widget knows case progress, it renders X/Y in the pending line."""
    tracker = AHEPredictionTracker()
    app = _SingleWidgetApp(tracker)
    async with app.run_test() as pilot:
        rec = _mk_record(1, PREDICTION_STATUS_PENDING)
        tracker.case_progress = {(SID, 1): (12, 20)}
        tracker.records = [rec]
        await pilot.pause()
        text = tracker._render_content()
        assert "12/20" in text
