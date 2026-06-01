"""Tests for the AHEPredictionPanel refutation flash + bell (FIX 1).

Covers:
  * trigger_refutation_flash bumps the flash_count counter (acts as the
    "watch hook" the spec asks tests to assert on)
  * a payload transition from pending → refuted_live fires the flash exactly
    once (subsequent payloads with the same status do not retrigger)
  * the bell byte (``\\a``) is written to stdout when the flash fires
"""

from __future__ import annotations

import io
import sys

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState
from pulse_app.widgets import AHEPredictionPanel


SID = "01KRQ_FLASH_TEST_SESSION_X"


def _prediction_evt(iteration: int) -> dict:
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
            "predicted_verdict_class_changes": {"OK": 4, "CE": -4},
            "confidence": 0.85,
            "rationale": "flash-test stake",
        },
    }


def _refuted_live_evt(iter_n_plus_1: int, reason: str = "OK: predicted +4 max +1") -> dict:
    return {
        "specversion": "1.0",
        "id": f"refute-{iter_n_plus_1}",
        "source": "/autobench",
        "type": "autobench.improver.prediction.refuted_live.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:35.000Z",
        "data": {
            "session_id": SID,
            "iteration": iter_n_plus_1,
            "prediction": {
                "predicted_score_delta": 0.05,
                "predicted_verdict_class_changes": {"OK": 4},
                "confidence": 0.85,
                "rationale": "embedded",
            },
            "actuals_so_far": {"OK": 1},
            "remaining_cases": 2,
            "is_refuted": True,
            "refutation_reason": reason,
            "confidence_at_refute": 0.85,
        },
    }


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def test_trigger_refutation_flash_bumps_counter():
    """Direct call to trigger_refutation_flash increments flash_count."""
    panel = AHEPredictionPanel()
    assert panel.flash_count == 0
    panel.trigger_refutation_flash()
    assert panel.flash_count == 1
    panel.trigger_refutation_flash()
    assert panel.flash_count == 2


def test_trigger_refutation_flash_writes_bell_byte(monkeypatch):
    """The bell byte (\\a) is written to stdout when the flash fires."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    panel = AHEPredictionPanel()
    panel.trigger_refutation_flash()
    # Restore stdout via monkeypatch teardown; inspect the buffer.
    assert "\a" in buf.getvalue()


@pytest.mark.asyncio
async def test_payload_transition_to_refuted_live_fires_flash_once():
    """A pending → refuted_live transition triggers exactly one flash.

    The watch_payload path is the contract surface; we mount the panel via
    Pilot to exercise it under a real Textual loop. We then push a second
    refuted_live payload and verify the counter does NOT advance — flashes
    only fire on the TRANSITION, not on every payload tick.
    """
    s = PulseState()
    s.apply(_prediction_evt(iteration=0))
    panel = AHEPredictionPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        # First: pending payload — flash must NOT fire.
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        assert panel.flash_count == 0
        # Drive the transition into refuted_live.
        s.apply(_refuted_live_evt(iter_n_plus_1=1))
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        assert panel.flash_count == 1
        # Re-push the same status — must NOT re-trigger.
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        assert panel.flash_count == 1


@pytest.mark.asyncio
async def test_refuted_live_then_recovered_then_refuted_again_fires_twice():
    """If the status leaves refuted_live and returns, the flash fires again."""
    s = PulseState()
    s.apply(_prediction_evt(iteration=0))
    panel = AHEPredictionPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        # Start pending.
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        # First refutation.
        s.apply(_refuted_live_evt(iter_n_plus_1=1))
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        assert panel.flash_count == 1
        # Simulate a status revert to pending then back to refuted_live.
        rec = list(s.predictions.values())[0]
        rec.status = "pending"
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        rec.status = "refuted_live"
        panel.payload = s.ahe_prediction_panel_payload()
        await pilot.pause()
        assert panel.flash_count == 2
