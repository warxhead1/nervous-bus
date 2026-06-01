"""Tests for IterationLineageStrip (nervous-bus-4fz3).

Covers:
  * lineage builds from iteration.summary.v1 events for completed iters
  * pending column shows the staked prediction's predicted_score_delta
  * AHE outcome dots reflect prediction status per column
  * 4-column max (N-3..N pending)
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import LINEAGE_STRIP_COLUMNS, PulseState
from pulse_app.widgets import IterationLineageStrip


SID = "01KRQ_LINEAGE_TEST_SESSION_X"


def _summary_evt(
    iteration: int,
    aggregate_score: float,
    verdict_distribution: dict | None = None,
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"summ-{iteration}",
        "source": "/autobench",
        "type": "autobench.iteration.summary.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": SID,
            "iteration": iteration,
            "aggregate_score": aggregate_score,
            "pass_rate": 0.7,
            "total_latency_ms": 12000.0,
            "total_cost_usd": 0.05,
            "total_tokens": 12000,
            "verdict_distribution": verdict_distribution or {"OK": 7, "CE": 2, "WA": 1},
            "num_cases": 10,
            "harness_version": "v3",
            "ce_rate": 0.2,
            "ok_rate": 0.7,
        },
    }


def _prediction_evt(
    iteration: int,
    predicted_score_delta: float = 0.04,
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
            "model": "claude-sonnet",
            "predicted_score_delta": predicted_score_delta,
            "predicted_verdict_class_changes": {"OK": 2, "CE": -2},
            "confidence": 0.8,
            "rationale": "tweak prompt",
        },
    }


# ---- state ingest -----------------------------------------------------------


def test_lineage_empty_with_no_data():
    s = PulseState()
    assert s.iteration_lineage() == []


def test_lineage_records_completed_summaries():
    s = PulseState()
    s.apply(_summary_evt(iteration=0, aggregate_score=0.50))
    s.apply(_summary_evt(iteration=1, aggregate_score=0.55))
    cells = s.iteration_lineage()
    assert len(cells) == 2
    iters = [c["iteration"] for c in cells]
    assert iters == [0, 1]
    assert all(c["kind"] == "completed" for c in cells)
    assert cells[0]["aggregate_score"] == pytest.approx(0.50)


def test_lineage_appends_pending_column_from_staked_prediction():
    """3 completed summaries + 1 staked prediction → 4 columns; last is pending."""
    s = PulseState()
    s.apply(_summary_evt(iteration=0, aggregate_score=0.50))
    s.apply(_summary_evt(iteration=1, aggregate_score=0.53))
    s.apply(_summary_evt(iteration=2, aggregate_score=0.55))
    # Stake a prediction at iter 2 targeting iter 3.
    s.apply(_prediction_evt(iteration=2, predicted_score_delta=0.04))
    cells = s.iteration_lineage()
    assert len(cells) == LINEAGE_STRIP_COLUMNS == 4
    iters = [c["iteration"] for c in cells]
    assert iters == [0, 1, 2, 3]
    assert cells[-1]["kind"] == "pending"
    assert cells[-1]["aggregate_score"] is None
    assert cells[-1]["predicted_score_delta"] == pytest.approx(0.04)
    assert cells[-1]["ahe_status"] == "pending"


def test_lineage_caps_at_four_columns():
    """More than 4 summaries → only the most recent 4 columns survive."""
    s = PulseState()
    for i in range(6):
        s.apply(_summary_evt(iteration=i, aggregate_score=0.50 + 0.01 * i))
    cells = s.iteration_lineage()
    assert len(cells) == LINEAGE_STRIP_COLUMNS == 4
    iters = [c["iteration"] for c in cells]
    # Most recent 4 iterations are kept.
    assert iters == [2, 3, 4, 5]


# ---- rendering --------------------------------------------------------------


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_strip_renders_all_four_columns():
    """Feed 3 summaries + 1 pending prediction; assert all 4 columns render."""
    s = PulseState()
    s.apply(_summary_evt(iteration=0, aggregate_score=0.50))
    s.apply(_summary_evt(iteration=1, aggregate_score=0.53))
    s.apply(_summary_evt(iteration=2, aggregate_score=0.552))
    s.apply(_prediction_evt(iteration=2, predicted_score_delta=0.04))
    strip = IterationLineageStrip()
    app = _SingleWidgetApp(strip)
    async with app.run_test() as pilot:
        strip.cells = s.iteration_lineage()
        await pilot.pause()
        rendered = strip._render_cells(strip.cells)
        # All four iteration indexes present in the header row.
        assert "iter 0" in rendered
        assert "iter 1" in rendered
        assert "iter 2" in rendered
        assert "iter 3" in rendered
        # Pending column shows the predicted delta (with sign).
        assert "+0.040" in rendered or "+0.04" in rendered
        # Completed scores show with 3-decimal precision.
        assert "0.552" in rendered
        assert "0.500" in rendered or "0.530" in rendered
        # Pending-status dot is present.
        assert "·" in rendered


@pytest.mark.asyncio
async def test_strip_idle_state_renders_placeholder():
    strip = IterationLineageStrip()
    app = _SingleWidgetApp(strip)
    async with app.run_test() as pilot:
        strip.cells = []
        await pilot.pause()
        idle = strip._render_idle()
        assert "iteration.summary" in idle.lower() or "awaiting" in idle.lower()
