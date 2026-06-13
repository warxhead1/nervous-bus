"""Tests for the multi-advocate population-cycle view (nervous-bus-uwdq).

wire-pop Phase-1 follow-up. Covers:
  * autobench.population.summary.v1 is recorded as the cycle boundary.
  * iteration.v1 events are grouped by per-advocate session_id into N
    parallel trajectories.
  * The winning advocate is flagged via ``is_winner`` and badged in render.
  * Backward compat: a single-advocate cycle (and a no-summary state) returns
    ``None`` so the panel stays hidden and single-session runs are unchanged.
  * Headless N=3 layout snapshot — all three advocates + the winner badge
    render in the panel's emitted text.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState
from pulse_app.widgets import MultiAdvocatePanel


# --------------------------------------------------------------------------- #
# Event-shape helpers — mirror the CloudEvents envelopes the bus emits.
# --------------------------------------------------------------------------- #


def _iteration_complete_evt(
    session_id: str,
    iteration: int,
    aggregate_score: float,
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"iter-{session_id}-{iteration}",
        "source": "/autobench",
        "type": "autobench.iteration.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-17T05:33:34.722Z",
        "data": {
            "session_id": session_id,
            "iteration": iteration,
            "status": "complete",
            "aggregate_score": aggregate_score,
            "verdict_counts": {"OK": 7, "CE": 2, "WA": 1},
            "harness_version": "v3",
        },
    }


def _population_summary_evt(
    cycle_id: str,
    advocates: list[dict],
    winner_id: str,
    winner_score: float,
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"pop-{cycle_id}",
        "source": "/autobench",
        "type": "autobench.population.summary.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-17T05:40:00.000Z",
        "data": {
            "session_id": f"pop-{cycle_id}",
            "cycle_id": cycle_id,
            "advocates": advocates,
            "winner_id": winner_id,
            "winner_score": winner_score,
            "cycle_started_at": "2026-05-17T05:30:00.000Z",
            "cycle_ended_at": "2026-05-17T05:40:00.000Z",
        },
    }


# --------------------------------------------------------------------------- #
# state ingest
# --------------------------------------------------------------------------- #


def test_no_summary_returns_none():
    s = PulseState()
    assert s.multi_advocate_view() is None


def test_single_advocate_cycle_returns_none():
    """A 1-advocate cycle must NOT surface the panel (backward compat)."""
    s = PulseState()
    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_SINGLE",
            advocates=[
                {
                    "advocate_id": "advocate-0",
                    "session_id": "01SESS_A",
                    "final_score": 0.55,
                    "best_iter": 2,
                }
            ],
            winner_id="advocate-0",
            winner_score=0.55,
        )
    )
    assert s.multi_advocate_view() is None


def test_three_advocate_cycle_groups_trajectories():
    s = PulseState()
    # Three advocates, each with its OWN session_id + iteration.v1 trajectory.
    s.apply(_iteration_complete_evt("01SESS_A", 0, 0.40))
    s.apply(_iteration_complete_evt("01SESS_A", 1, 0.50))
    s.apply(_iteration_complete_evt("01SESS_B", 0, 0.42))
    s.apply(_iteration_complete_evt("01SESS_B", 1, 0.61))
    s.apply(_iteration_complete_evt("01SESS_C", 0, 0.45))

    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_TRIO",
            advocates=[
                {"advocate_id": "advocate-0", "session_id": "01SESS_A",
                 "final_score": 0.50, "best_iter": 1},
                {"advocate_id": "advocate-1", "session_id": "01SESS_B",
                 "final_score": 0.61, "best_iter": 1},
                {"advocate_id": "advocate-2", "session_id": "01SESS_C",
                 "final_score": 0.45, "best_iter": 0},
            ],
            winner_id="advocate-1",
            winner_score=0.61,
        )
    )

    view = s.multi_advocate_view()
    assert view is not None
    assert view["cycle_id"] == "01CYCLE_TRIO"
    assert view["winner_id"] == "advocate-1"
    assert len(view["advocates"]) == 3

    by_id = {a["advocate_id"]: a for a in view["advocates"]}
    # Each advocate's trajectory is grouped from ITS OWN session_id.
    assert by_id["advocate-0"]["scores"] == [0.40, 0.50]
    assert by_id["advocate-1"]["scores"] == [0.42, 0.61]
    assert by_id["advocate-2"]["scores"] == [0.45]
    # Winner flagged correctly — only advocate-1.
    assert by_id["advocate-1"]["is_winner"] is True
    assert by_id["advocate-0"]["is_winner"] is False
    assert by_id["advocate-2"]["is_winner"] is False
    # Latest-iter / latest-score reflect the live trajectory tail.
    assert by_id["advocate-1"]["latest_iter"] == 1
    assert by_id["advocate-1"]["latest_score"] == 0.61


def test_advocate_falls_back_to_final_score_without_live_iters():
    """Summary-only replay (no iteration.v1 yet) still draws a trajectory."""
    s = PulseState()
    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_REPLAY",
            advocates=[
                {"advocate_id": "advocate-0", "session_id": "01SESS_X",
                 "final_score": 0.30, "best_iter": 1},
                {"advocate_id": "advocate-1", "session_id": "01SESS_Y",
                 "final_score": 0.70, "best_iter": 3},
            ],
            winner_id="advocate-1",
            winner_score=0.70,
        )
    )
    view = s.multi_advocate_view()
    assert view is not None
    by_id = {a["advocate_id"]: a for a in view["advocates"]}
    assert by_id["advocate-0"]["scores"] == [0.30]
    assert by_id["advocate-1"]["scores"] == [0.70]
    assert by_id["advocate-1"]["latest_score"] == 0.70


def test_newest_summary_displaces_older():
    s = PulseState()
    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_OLD",
            advocates=[
                {"advocate_id": "advocate-0", "session_id": "01OLD_A",
                 "final_score": 0.1, "best_iter": 0},
                {"advocate_id": "advocate-1", "session_id": "01OLD_B",
                 "final_score": 0.2, "best_iter": 0},
            ],
            winner_id="advocate-1",
            winner_score=0.2,
        )
    )
    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_NEW",
            advocates=[
                {"advocate_id": "advocate-0", "session_id": "01NEW_A",
                 "final_score": 0.8, "best_iter": 2},
                {"advocate_id": "advocate-1", "session_id": "01NEW_B",
                 "final_score": 0.3, "best_iter": 1},
            ],
            winner_id="advocate-0",
            winner_score=0.8,
        )
    )
    view = s.multi_advocate_view()
    assert view["cycle_id"] == "01CYCLE_NEW"
    assert view["winner_id"] == "advocate-0"


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_panel_renders_n3_layout_with_winner_badge():
    """N=3 layout snapshot — all three advocates + winner badge render."""
    s = PulseState()
    s.apply(_iteration_complete_evt("01SESS_A", 0, 0.40))
    s.apply(_iteration_complete_evt("01SESS_A", 1, 0.50))
    s.apply(_iteration_complete_evt("01SESS_B", 0, 0.42))
    s.apply(_iteration_complete_evt("01SESS_B", 1, 0.61))
    s.apply(_iteration_complete_evt("01SESS_C", 0, 0.45))
    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_TRIO",
            advocates=[
                {"advocate_id": "advocate-0", "session_id": "01SESS_A",
                 "final_score": 0.50, "best_iter": 1},
                {"advocate_id": "advocate-1", "session_id": "01SESS_B",
                 "final_score": 0.61, "best_iter": 1},
                {"advocate_id": "advocate-2", "session_id": "01SESS_C",
                 "final_score": 0.45, "best_iter": 0},
            ],
            winner_id="advocate-1",
            winner_score=0.61,
        )
    )

    panel = MultiAdvocatePanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.payload = s.multi_advocate_view()
        await pilot.pause()
        rendered = panel._render_payload(panel.payload)
        # All three advocates appear.
        assert "advocate-0" in rendered
        assert "advocate-1" in rendered
        assert "advocate-2" in rendered
        # Winner badge + winner id in the header summary.
        assert "WINNER" in rendered
        # Winner's best score surfaces.
        assert "0.610" in rendered or "+0.610" in rendered
        # Panel is NOT hidden when multi-advocate.
        assert not panel.has_class("hidden")


@pytest.mark.asyncio
async def test_panel_hidden_for_single_advocate():
    """Panel collapses (display:none) when the view is single/None."""
    s = PulseState()
    s.apply(
        _population_summary_evt(
            cycle_id="01CYCLE_SINGLE",
            advocates=[
                {"advocate_id": "advocate-0", "session_id": "01SESS_SOLO",
                 "final_score": 0.55, "best_iter": 2}
            ],
            winner_id="advocate-0",
            winner_score=0.55,
        )
    )
    panel = MultiAdvocatePanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.payload = s.multi_advocate_view()
        await pilot.pause()
        # multi_advocate_view() is None for a single advocate → hidden.
        assert panel.has_class("hidden")
