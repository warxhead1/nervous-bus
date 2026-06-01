"""Tests for the AHEPredictionPanel watermark thermometer drain (FIX 2).

Covers:
  * watermark_initial is captured on the prediction record when the
    prediction is first staked and used as the bar's denominator
  * Progressive case events drop the bar's value 5 → 4 → 3 → ... → 0
  * The zero_pulse reactive flips to True when the bar hits 0 and the
    rendered bar contains the bold-red empty-glyph row at that point
  * watermark_initial never decreases below the high-water mark
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import IterationStats, PulseState
from pulse_app.widgets import AHEPredictionPanel, _watermark_thermometer


SID = "01KRQ_THERMO_TEST_SESSION_X"


def _prediction_evt(iteration: int, predicted_verdict_class_changes: dict) -> dict:
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
            "model": "claude-sonnet",
            "predicted_score_delta": 0.05,
            "predicted_verdict_class_changes": predicted_verdict_class_changes,
            "confidence": 0.85,
            "rationale": "thermometer drain test",
        },
    }


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def _set_actuals(state: PulseState, ok_count: int) -> None:
    """Synthesise iter N+1 with ``ok_count`` OK verdicts landed."""
    rec = list(state.predictions.values())[0]
    sess = state.sessions[rec.session_id]
    it = sess.iterations.get(rec.iteration + 1)
    if it is None:
        it = IterationStats(iteration=rec.iteration + 1)
        sess.iterations[rec.iteration + 1] = it
    it.live_verdict_counts = {"OK": int(ok_count)}


def test_watermark_initial_is_captured_on_first_observation():
    """First call to prediction_watermark seeds watermark_initial."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0, predicted_verdict_class_changes={"OK": 5}))
    payload = s.ahe_prediction_panel_payload()
    assert payload is not None
    assert payload["watermark"] == 5
    assert payload["watermark_initial"] == 5
    rec = list(s.predictions.values())[0]
    assert rec.watermark_initial == 5


def test_watermark_drains_5_to_0_progressively():
    """Feed OK actuals 0..5 and assert the watermark drops 5 → 0."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0, predicted_verdict_class_changes={"OK": 5}))
    # Prime watermark_initial.
    payload0 = s.ahe_prediction_panel_payload()
    assert payload0["watermark"] == 5
    assert payload0["watermark_initial"] == 5

    expected_sequence = [5, 4, 3, 2, 1, 0]
    actual_sequence: list[int] = []
    for ok in range(0, 6):
        _set_actuals(s, ok_count=ok)
        payload = s.ahe_prediction_panel_payload()
        assert payload is not None
        actual_sequence.append(int(payload["watermark"]))
        # Denominator is locked.
        assert payload["watermark_initial"] == 5
    assert actual_sequence == expected_sequence


def test_watermark_initial_never_decreases_below_high_water_mark():
    """If a fresh prediction would compute a lower initial, the bar's
    denominator stays at the high-water mark so the bar doesn't visually
    'refill' mid-drain.
    """
    s = PulseState()
    s.apply(_prediction_evt(iteration=0, predicted_verdict_class_changes={"OK": 5}))
    _ = s.ahe_prediction_panel_payload()  # seed initial=5

    # Drain partly.
    _set_actuals(s, ok_count=2)
    payload = s.ahe_prediction_panel_payload()
    assert payload["watermark"] == 3
    assert payload["watermark_initial"] == 5

    # Now mutate the prediction's predicted shape so the computed slack
    # would be smaller (simulating a re-stake). watermark_initial must not
    # go below 5.
    rec = list(s.predictions.values())[0]
    rec.predicted_verdict_class_changes = {"OK": 3}
    # Reset actuals so the slack is now 3.
    _set_actuals(s, ok_count=0)
    payload = s.ahe_prediction_panel_payload()
    assert payload["watermark"] == 3
    assert payload["watermark_initial"] == 5


@pytest.mark.asyncio
async def test_panel_zero_pulse_flips_when_bar_drains():
    """The panel's ``zero_pulse`` reactive transitions False → True at 0."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0, predicted_verdict_class_changes={"OK": 3}))
    panel = AHEPredictionPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        # Drain progressively and observe zero_pulse only at 0.
        for ok in (0, 1, 2, 3):
            _set_actuals(s, ok_count=ok)
            panel.payload = s.ahe_prediction_panel_payload()
            await pilot.pause()
            if ok < 3:
                assert panel.zero_pulse is False
                assert panel.watermark_value == 3 - ok
            else:
                assert panel.zero_pulse is True
                assert panel.watermark_value == 0


def test_watermark_thermometer_helper_renders_bold_red_at_zero():
    """Bar helper flips to bold COLOR_AHE_MISS markup at value=0."""
    bar_full = _watermark_thermometer(5, 5, "cyan")
    bar_half = _watermark_thermometer(3, 5, "cyan")
    bar_zero = _watermark_thermometer(0, 5, "cyan")
    # Mid-drain uses the requested colour.
    assert "[cyan]" in bar_half
    # Zero flips to bold red (AHE_MISS).
    assert "bold" in bar_zero
    # Full bar contains the fill glyph.
    assert "█" in bar_full
    # Zero bar contains only empty glyphs (no fill chars).
    assert "█" not in bar_zero


def test_watermark_thermometer_handles_zero_initial():
    """With initial==0 the bar renders all-empty in dim — no division error."""
    bar = _watermark_thermometer(0, 0, "cyan")
    assert "░" in bar
    # No crash, no fill block.
    assert "█" not in bar
