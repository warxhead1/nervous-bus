"""Tests for QueuePressureBar (nervous-bus-m3so).

Covers:
  * colour transitions at the documented deviation thresholds (0.25, 0.50)
  * state.apply ingest of autobench.worker.queue_pressure.v1
  * rolling tps window pushes
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState, QUEUE_PRESSURE_WINDOW
from pulse_app.widgets import QueuePressureBar, _queue_pressure_color


def _qp_evt(
    deviation: float,
    current: float = 17.9,
    baseline: float = 49.0,
    sid: str = "01KRQ_QP_TEST_SESSION_X",
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"qp-{deviation}",
        "source": "/autobench",
        "type": "autobench.worker.queue_pressure.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": sid,
            "model": "MiniMax-M2.7",
            "current_rate_tps": current,
            "baseline_tps": baseline,
            "deviation_factor": deviation,
            "recent_timeouts_count": 0,
            "latest_latency_ms": 12_000.0,
            "window_size": 5,
        },
    }


# ---- colour thresholds ------------------------------------------------------


def test_color_below_quarter_is_cyan():
    """deviation < 0.25 maps to cyan (normal)."""
    assert _queue_pressure_color(0.0) == "cyan"
    assert _queue_pressure_color(0.24) == "cyan"


def test_color_at_quarter_threshold_transitions_to_yellow():
    """deviation == 0.25 crosses into yellow (watch)."""
    assert _queue_pressure_color(0.25) == "yellow"
    assert _queue_pressure_color(0.36) == "yellow"
    assert _queue_pressure_color(0.49) == "yellow"


def test_color_at_half_threshold_transitions_to_red():
    """deviation >= 0.50 crosses into red (critical)."""
    assert _queue_pressure_color(0.50) == "red"
    assert _queue_pressure_color(0.75) == "red"
    assert _queue_pressure_color(1.0) == "red"


# ---- state ingest -----------------------------------------------------------


def test_apply_records_queue_pressure_snapshot():
    s = PulseState()
    s.apply(_qp_evt(deviation=0.36, current=17.9, baseline=49.0))
    snap = s.queue_pressure_summary()
    assert snap is not None
    assert snap["latest"]["current_rate_tps"] == pytest.approx(17.9)
    assert snap["latest"]["baseline_tps"] == pytest.approx(49.0)
    assert snap["latest"]["deviation_factor"] == pytest.approx(0.36)
    assert list(snap["tps_window"]) == [17.9]


def test_tps_window_bounded():
    s = PulseState()
    # Push more than the window size; oldest are evicted.
    for i in range(QUEUE_PRESSURE_WINDOW + 5):
        s.apply(_qp_evt(deviation=0.3, current=float(i), baseline=49.0))
    snap = s.queue_pressure_summary()
    assert len(snap["tps_window"]) == QUEUE_PRESSURE_WINDOW
    # The most-recently-pushed value is at the right edge.
    assert snap["tps_window"][-1] == pytest.approx(QUEUE_PRESSURE_WINDOW + 4)


# ---- rendering --------------------------------------------------------------


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_bar_renders_at_each_color_tier():
    """The rendered line embeds the correct rich-tag colour for each tier."""
    bar = QueuePressureBar()
    app = _SingleWidgetApp(bar)
    async with app.run_test() as pilot:
        # cyan
        s = PulseState()
        s.apply(_qp_evt(deviation=0.10))
        bar.payload = s.queue_pressure_summary()
        await pilot.pause()
        rendered = bar._render_payload(bar.payload or {})
        assert "[cyan]" in rendered
        # yellow
        s = PulseState()
        s.apply(_qp_evt(deviation=0.36))
        bar.payload = s.queue_pressure_summary()
        await pilot.pause()
        rendered = bar._render_payload(bar.payload or {})
        assert "[yellow]" in rendered
        # red
        s = PulseState()
        s.apply(_qp_evt(deviation=0.75))
        bar.payload = s.queue_pressure_summary()
        await pilot.pause()
        rendered = bar._render_payload(bar.payload or {})
        assert "[red]" in rendered


@pytest.mark.asyncio
async def test_bar_idle_state_before_any_event():
    bar = QueuePressureBar()
    app = _SingleWidgetApp(bar)
    async with app.run_test() as pilot:
        bar.payload = None
        await pilot.pause()
        idle = bar._render_idle()
        assert "queue pressure" in idle.lower()


@pytest.mark.asyncio
async def test_bar_includes_sparkline_and_baseline_label():
    s = PulseState()
    # Three samples to build a non-empty sparkline.
    for v in (40.0, 25.0, 17.9):
        s.apply(_qp_evt(deviation=0.36, current=v, baseline=49.0))
    bar = QueuePressureBar()
    app = _SingleWidgetApp(bar)
    async with app.run_test() as pilot:
        bar.payload = s.queue_pressure_summary()
        await pilot.pause()
        rendered = bar._render_payload(bar.payload or {})
        assert "baseline" in rendered
        assert "tps:" in rendered
        assert "dev:" in rendered
