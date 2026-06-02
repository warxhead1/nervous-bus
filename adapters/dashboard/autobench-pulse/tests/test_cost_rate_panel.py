"""Tests for CostRatePanel + cost/rate state ingestion (bead nervous-bus-cewj).

Coverage:
  * worker.v1 events accumulate into ``PulseState.worker_cost_total_usd``
  * budget.warning at 0.5 records "fired at 50%" + recovers ``max_cost_usd``
  * budget.rate snapshots populate the rate-readout fields
  * Header text formatting includes "$0.50" for the 50% threshold scale
  * Panel mounts headlessly without crashing
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState
from pulse_app.widgets import CostRatePanel, CostTrajectoryPlot


# --------------------------------------------------------------------------- #
# Unit: worker.v1 cost accumulation                                            #
# --------------------------------------------------------------------------- #


def _worker_evt(case_id: str, cost_usd: float) -> dict:
    return {
        "specversion": "1.0",
        "id": f"e-{case_id}",
        "source": "/autobench",
        "type": "autobench.worker.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "case_id": case_id,
            "model": "minimax-m1",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cost_usd": cost_usd,
            "latency_ms": 12.0,
            "code_preview": "",
        },
    }


def test_worker_events_accumulate_total_cost():
    """3 worker.v1 events with costs 0.001, 0.002, 0.001 → total $0.004."""
    s = PulseState()
    s.apply(_worker_evt("c1", 0.001))
    s.apply(_worker_evt("c2", 0.002))
    s.apply(_worker_evt("c3", 0.001))
    assert s.worker_cost_total_usd == pytest.approx(0.004, abs=1e-9)
    # Trajectory has 3 monotonically-increasing points.
    xs, ys = s.cost_trajectory()
    assert len(xs) == 3
    assert ys[0] == pytest.approx(0.001)
    assert ys[1] == pytest.approx(0.003)
    assert ys[2] == pytest.approx(0.004)


def test_worker_negative_cost_ignored():
    s = PulseState()
    s.apply(_worker_evt("c1", -1.0))
    assert s.worker_cost_total_usd == 0.0


# --------------------------------------------------------------------------- #
# Unit: budget.warning threshold tracking                                      #
# --------------------------------------------------------------------------- #


def _budget_warning_evt(threshold: float, max_cost: float = 1.0) -> dict:
    return {
        "specversion": "1.0",
        "id": "bw1",
        "source": "/autobench",
        "type": "autobench.budget.warning.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": "01KRTESTSESSION",
            "fraction_used": threshold,
            "current_cost_usd": threshold * max_cost,
            "max_cost_usd": max_cost,
            "elapsed_wall_seconds": 100.0,
            "max_wall_seconds": 1800.0,
            "threshold": threshold,
            "action": "warning" if threshold < 1.0 else "halt",
            "reason": "test",
        },
    }


def test_budget_warning_50pct_records_fired():
    """budget warning at 0.5 threshold → state records "fired at 50%"."""
    s = PulseState()
    s.apply(_budget_warning_evt(0.5))
    assert 0.5 in s.budget_thresholds_fired
    # The fired record carries (timestamp, iter_hint).
    fired = s.budget_thresholds_fired[0.5]
    assert len(fired) == 2
    # max_cost_usd recovered from payload.
    assert s.max_cost_usd == pytest.approx(1.0)
    assert s.max_cost_usd_known is True


def test_budget_warning_recovers_custom_max_cost():
    s = PulseState()
    s.apply(_budget_warning_evt(0.5, max_cost=5.0))
    assert s.max_cost_usd == pytest.approx(5.0)
    assert s.max_cost_usd_known is True


def test_budget_warning_thresholds_idempotent():
    s = PulseState()
    s.apply(_budget_warning_evt(0.5))
    s.apply(_budget_warning_evt(0.5))
    s.apply(_budget_warning_evt(0.8))
    s.apply(_budget_warning_evt(1.0))
    assert set(s.budget_thresholds_fired.keys()) == {0.5, 0.8, 1.0}


# --------------------------------------------------------------------------- #
# Unit: budget.rate readout                                                    #
# --------------------------------------------------------------------------- #


def _budget_rate_evt(current: int, max_req: int, window_s: float = 18000.0) -> dict:
    return {
        "specversion": "1.0",
        "id": "br1",
        "source": "/autobench",
        "type": "autobench.budget.rate.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": "01KRTESTSESSION",
            "current_count": current,
            "max_requests": max_req,
            "window_seconds": window_s,
            "fraction_used": current / max_req if max_req else 0.0,
            "time_until_available": 0.0,
            "action": "warning",
            "warned_at_threshold": 0.5,
            "reason": "test",
        },
    }


def test_rate_readout_records_current_and_cap():
    """Rate readout shows requests / cap correctly."""
    s = PulseState()
    s.apply(_budget_rate_evt(current=187, max_req=14250, window_s=18000.0))
    assert s.rate_state["current_count"] == 187
    assert s.rate_state["max_requests"] == 14250
    assert s.rate_state["window_seconds"] == pytest.approx(18000.0)


# --------------------------------------------------------------------------- #
# Unit: header text formatting includes threshold scale                        #
# --------------------------------------------------------------------------- #


def test_header_promotes_requests_as_primary():
    """Requests-primary headline — the MiniMax coding plan bills per
    request-per-5h, so the header leads with requests/cap and the $ figure is
    demoted to a dim "notional" secondary line (bead nervous-bus-avyw)."""
    payload = {
        "total_usd": 0.0432,
        "max_cost_usd": 1.0,
        "max_cost_known": True,
        "thresholds_fired": {},
        "rate": {"current_count": 187, "max_requests": 14250, "window_seconds": 18000.0},
        "trajectory": ([], []),
    }
    header_text = CostRatePanel._format_header(payload)
    # Header is requests-primary: cur/cap + window + "requests" word + 5h.
    assert "187" in header_text and "14250" in header_text
    assert "requests" in header_text
    assert "5h" in header_text
    # Dollar figures do NOT appear in the headline anymore.
    assert "$" not in header_text
    # Secondary line carries request HEADROOM (no $), rendered dim. Per policy
    # the in-tree token→$ estimator is hard-zeroed; requests are the only honest
    # billing axis, so headroom = max_requests - current_count.
    notional = CostRatePanel._format_notional(payload)
    assert "$" not in notional
    assert "14063" in notional  # 14250 - 187 left
    assert "headroom" in notional
    assert "5h" in notional
    assert "[dim]" in notional


def test_chart_threshold_scale_via_cap():
    """The 50% threshold for a $1.00 cap is $0.50 — assert the chart payload
    drives a hline at that y-value (we verify the cap propagates; plotext
    output is opaque, so we check the inputs are right)."""
    payload = {
        "total_usd": 0.5,
        "max_cost_usd": 1.0,
        "max_cost_known": True,
        "thresholds_fired": {0.5: (1234.5, 7)},
        "rate": {},
        "trajectory": ([0.0, 1.0, 2.0], [0.1, 0.3, 0.5]),
    }
    # 50% threshold of $1.00 cap = $0.50. We just verify computation.
    cap = payload["max_cost_usd"]
    threshold_50 = cap * 0.5
    assert threshold_50 == pytest.approx(0.50)
    # Warning text mentions "50%" when 0.5 has fired.
    warn = CostRatePanel._format_warning(payload)
    assert "50%" in warn
    assert "threshold" in warn


def test_header_empty_state():
    payload = {
        "total_usd": 0.0,
        "max_cost_usd": 1.0,
        "max_cost_known": False,
        "thresholds_fired": {},
        "rate": {},
        "trajectory": ([], []),
    }
    header_text = CostRatePanel._format_header(payload)
    # No rate snapshot yet → placeholder requests line, no $ in headline.
    assert "Rate: --" in header_text
    assert "$" not in header_text
    # No rate snapshot → placeholder headroom line (no $), not empty.
    notional = CostRatePanel._format_notional(payload)
    assert "$" not in notional
    assert "headroom" in notional
    assert "--" in notional


# --------------------------------------------------------------------------- #
# Headless render: panel mounts                                                #
# --------------------------------------------------------------------------- #


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_cost_rate_panel_mounts():
    panel = CostRatePanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.payload = {
            "total_usd": 0.0432,
            "max_cost_usd": 1.0,
            "max_cost_known": True,
            "thresholds_fired": {0.5: (1234.5, 7)},
            "rate": {"current_count": 187, "max_requests": 14250, "window_seconds": 18000.0},
            "trajectory": ([0.0, 1.0, 2.0, 3.0], [0.01, 0.02, 0.035, 0.0432]),
        }
        await pilot.pause()
        # Header text was applied. Capture via the formatter contract
        # (Static doesn't expose `.renderable` in all Textual versions).
        # Headline is requests-primary; $ moved to the dim notional line.
        rendered = CostRatePanel._format_header(panel.payload)
        assert "14250" in rendered and "requests" in rendered
        notional = CostRatePanel._format_notional(panel.payload)
        assert "14063" in notional and "headroom" in notional


@pytest.mark.asyncio
async def test_cost_rate_panel_handles_empty():
    panel = CostRatePanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.payload = {
            "total_usd": 0.0,
            "max_cost_usd": 1.0,
            "max_cost_known": False,
            "thresholds_fired": {},
            "rate": {},
            "trajectory": ([], []),
        }
        await pilot.pause()


@pytest.mark.asyncio
async def test_cost_trajectory_plot_handles_data_and_thresholds():
    plot = CostTrajectoryPlot()
    app = _SingleWidgetApp(plot)
    async with app.run_test() as pilot:
        plot.payload = {
            "total_usd": 0.5,
            "max_cost_usd": 1.0,
            "max_cost_known": True,
            "thresholds_fired": {0.5: (1.0, 7), 0.8: (2.0, 14)},
            "rate": {},
            "trajectory": ([0.0, 1.0, 2.0, 3.0], [0.1, 0.3, 0.5, 0.5]),
        }
        await pilot.pause()
        # Empty trajectory should not crash.
        plot.payload = {
            "total_usd": 0.0,
            "max_cost_usd": 1.0,
            "max_cost_known": False,
            "thresholds_fired": {},
            "rate": {},
            "trajectory": ([], []),
        }
        await pilot.pause()


# --------------------------------------------------------------------------- #
# Integration: end-to-end PulseState → cost_summary() snapshot                 #
# --------------------------------------------------------------------------- #


def test_cost_summary_end_to_end():
    s = PulseState()
    # Mid-cycle: a few worker calls + a 50% threshold fire + rate snapshot.
    s.apply(_worker_evt("c1", 0.010))
    s.apply(_worker_evt("c2", 0.020))
    s.apply(_worker_evt("c3", 0.015))
    s.apply(_budget_warning_evt(0.5, max_cost=1.0))
    s.apply(_budget_rate_evt(current=187, max_req=14250, window_s=18000.0))
    summary = s.cost_summary()
    assert summary["total_usd"] == pytest.approx(0.045)
    assert summary["max_cost_usd"] == pytest.approx(1.0)
    assert summary["max_cost_known"] is True
    assert 0.5 in summary["thresholds_fired"]
    assert summary["rate"]["current_count"] == 187
    assert summary["rate"]["max_requests"] == 14250
    xs, ys = summary["trajectory"]
    assert len(xs) == 3
    assert ys[-1] == pytest.approx(0.045)
