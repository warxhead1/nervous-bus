"""Tests for AHEPredictionPanel (nervous-bus-a5mx).

Covers:
  * watermark computation from predicted_verdict_class_changes + actuals
  * refutation_reason rendering when the prediction is refuted_live
  * history-dots row containing one glyph per recent prediction
  * payload empty-state rendering
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState
from pulse_app.widgets import AHEPredictionPanel


SID = "01KRQ_AHE_PANEL_TEST_SESSION_X"


def _prediction_evt(
    iteration: int,
    confidence: float = 0.85,
    predicted_score_delta: float = 0.05,
    predicted_verdict_class_changes: dict | None = None,
    rationale: str = "stake on tightening the system prompt",
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
            "predicted_verdict_class_changes": predicted_verdict_class_changes
            or {"OK": 4, "CE": -4, "WA": 0},
            "confidence": confidence,
            "rationale": rationale,
        },
    }


def _refuted_live_evt(
    iteration_n_plus_1: int,
    refutation_reason: str = "OK: predicted +4 but max achievable is +1",
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
                "predicted_score_delta": 0.05,
                "predicted_verdict_class_changes": {"OK": 4},
                "confidence": 0.85,
                "rationale": "embedded",
            },
            "actuals_so_far": {"OK": 1},
            "remaining_cases": 2,
            "is_refuted": True,
            "refutation_reason": refutation_reason,
            "confidence_at_refute": 0.85,
        },
    }


# ---- watermark math ---------------------------------------------------------


def test_watermark_uses_smallest_positive_predicted_change_when_no_actuals():
    """No iter N+1 cases yet → watermark falls back to min positive predicted."""
    s = PulseState()
    s.apply(
        _prediction_evt(
            iteration=0,
            predicted_verdict_class_changes={"OK": 4, "CE": -4},
        )
    )
    payload = s.ahe_prediction_panel_payload()
    assert payload is not None
    # Only positive entry is OK:+4 → smallest positive slack is 4.
    assert payload["watermark"] == 4


def test_watermark_decreases_as_actuals_arrive():
    """Live actuals shrink the watermark to remaining slack."""
    from pulse_app.state import IterationStats

    s = PulseState()
    s.apply(_prediction_evt(iteration=0, predicted_verdict_class_changes={"OK": 5}))
    # Synthesise iter 1 with 3 OKs landed.
    rec = list(s.predictions.values())[0]
    sess = s.sessions[rec.session_id]
    it = IterationStats(iteration=1)
    it.live_verdict_counts = {"OK": 3}
    sess.iterations[1] = it
    payload = s.ahe_prediction_panel_payload()
    # 5 predicted - 3 actual = 2 slack remaining
    assert payload["watermark"] == 2


# ---- rendering --------------------------------------------------------------


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_panel_renders_watermark_and_refutation_reason():
    """Feed 2 prediction events + 1 refuted_live; assert AC fields render."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0))
    s.apply(_prediction_evt(iteration=1))
    s.apply(
        _refuted_live_evt(
            iteration_n_plus_1=2,
            refutation_reason="OK: predicted +4 but max achievable is +1",
        )
    )
    panel = AHEPredictionPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        rendered = panel._render_payload(s.ahe_prediction_panel_payload() or {})
        # Watermark line is present.
        assert "watermark" in rendered.lower()
        # Refutation reason is foregrounded in bold red.
        assert "predicted +4" in rendered
        assert "max achievable" in rendered
        # Status label is the refuted_live one.
        assert "REFUTED" in rendered or "refuted" in rendered.lower()


@pytest.mark.asyncio
async def test_panel_renders_history_dots_row():
    s = PulseState()
    # Three predictions → three pending dots in the history row.
    for i in range(3):
        s.apply(_prediction_evt(iteration=i))
    panel = AHEPredictionPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        payload = s.ahe_prediction_panel_payload()
        panel.payload = payload
        await pilot.pause()
        rendered = panel._render_payload(payload or {})
        assert "history" in rendered.lower()
        # Pending dot character should appear at least once.
        assert "·" in rendered


@pytest.mark.asyncio
async def test_panel_idle_state_with_no_predictions():
    s = PulseState()
    panel = AHEPredictionPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        idle = panel._render_idle()
        assert "no falsifiable prediction" in idle.lower() or "awaiting" in idle.lower()


def test_panel_payload_includes_required_keys():
    """The state-level payload contains prediction + watermark + history_dots."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0))
    payload = s.ahe_prediction_panel_payload()
    assert payload is not None
    assert "prediction" in payload
    assert "watermark" in payload
    assert "history_dots" in payload
    assert isinstance(payload["history_dots"], list)
