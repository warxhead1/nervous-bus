"""Textual Pilot tests for individual widgets.

These spin up minimal harness Apps that host one widget each, drive a few
interactions through the Pilot, and assert the widget didn't crash. They're
explicitly *not* pixel-snapshot tests (those need pytest-textual-snapshot which
we don't depend on); they're "does this thing mount and survive a refresh"
smoke tests, which catches the bulk of regressions per §8.6.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState
from pulse_app.widgets import (
    BurnGauge,
    HeaderStats,
    ParetoScatter,
    ScoreSpark,
    SessionTree,
    VerdictHistogram,
)


class _SingleWidgetApp(App):
    """Host one widget so we can drive it through Pilot."""

    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_session_tree_mounts_and_rebuilds(sample_events):
    state = PulseState()
    for evt in sample_events:
        state.apply(evt)
    tree = SessionTree("autobench sessions")
    app = _SingleWidgetApp(tree)
    async with app.run_test() as pilot:
        tree.rebuild(state.sessions_by_recency())
        await pilot.pause()
        # at least one session node added
        assert len(tree._session_nodes) == 1


@pytest.mark.asyncio
async def test_score_spark_mounts():
    spark = ScoreSpark([0.1, 0.2, 0.5, 0.7])
    app = _SingleWidgetApp(spark)
    async with app.run_test() as pilot:
        spark.data = [0.1, 0.2, 0.3, 0.4, 0.9]
        await pilot.pause()


@pytest.mark.asyncio
async def test_burn_gauge_updates():
    gauge = BurnGauge(budget=2.0)
    app = _SingleWidgetApp(gauge)
    async with app.run_test() as pilot:
        gauge.burn = 1.0
        await pilot.pause()
        gauge.burn = 5.0  # overshoot — should clamp
        await pilot.pause()


@pytest.mark.asyncio
async def test_verdict_histogram_handles_empty_and_data():
    hist = VerdictHistogram()
    app = _SingleWidgetApp(hist)
    async with app.run_test() as pilot:
        hist.counts = {}
        await pilot.pause()
        hist.counts = {"OK": 5, "WA": 1, "TLE": 2}
        await pilot.pause()


@pytest.mark.asyncio
async def test_pareto_scatter_handles_empty_and_data():
    scatter = ParetoScatter()
    app = _SingleWidgetApp(scatter)
    async with app.run_test() as pilot:
        scatter.points = []
        await pilot.pause()
        scatter.points = [(0.01, 0.5), (0.02, 0.7), (0.05, 0.9), (0.03, 0.6)]
        await pilot.pause()


@pytest.mark.asyncio
async def test_header_stats_updates():
    stats = HeaderStats()
    app = _SingleWidgetApp(stats)
    async with app.run_test() as pilot:
        stats.text = "sessions: 3  evt/s: 4.2"
        await pilot.pause()
