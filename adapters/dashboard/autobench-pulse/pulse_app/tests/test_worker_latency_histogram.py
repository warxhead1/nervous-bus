"""Unit + Pilot tests for WorkerLatencyHistogram (bead nervous-bus-s5h9).

Covers:
  * bucketing — every defined bucket fills correctly at edges
  * stats — count / mean / p50 / p95 over realistic latency distributions
  * 60s+ bucket — fills only when latency_ms >= 60_000
  * rolling window — 201st sample evicts the oldest
  * Pilot render — chart + stats line are present in the widget output
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState, WORKER_LATENCY_WINDOW
from pulse_app.widgets import (
    WORKER_LATENCY_BUCKETS,
    WorkerLatencyHistogram,
    _bucket_for_latency_ms,
    compute_worker_latency_buckets,
    compute_worker_latency_stats,
)


def _worker_evt(latency_ms: float, sid: str = "sess-x") -> dict:
    """Minimal autobench.worker.v1 envelope shaped like a real bus event."""
    return {
        "specversion": "1.0",
        "id": f"e-{latency_ms}",
        "source": "/autobench",
        "type": "autobench.worker.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T06:43:06.149Z",
        "data": {
            "case_id": "",
            "model": "MiniMax-M2.7",
            "prompt_tokens": 500,
            "completion_tokens": 500,
            "cost_usd": 0.0008,
            "latency_ms": float(latency_ms),
            "code_preview": "",
            "session_id": sid,
        },
    }


# ---------------------------------------------------------------------------- #
# Bucketing                                                                    #
# ---------------------------------------------------------------------------- #


def test_bucket_for_latency_ms_known_points():
    # interior of each bucket
    assert _bucket_for_latency_ms(2_500) == "0-5s"
    assert _bucket_for_latency_ms(7_500) == "5-10s"
    assert _bucket_for_latency_ms(12_500) == "10-15s"
    assert _bucket_for_latency_ms(17_500) == "15-20s"
    assert _bucket_for_latency_ms(25_000) == "20-30s"
    assert _bucket_for_latency_ms(37_500) == "30-45s"
    assert _bucket_for_latency_ms(50_000) == "45-60s"
    # 60s edge sits in the timeout bucket (right-open intervals)
    assert _bucket_for_latency_ms(60_000) == "60s+"
    assert _bucket_for_latency_ms(99_999) == "60s+"


def test_bucket_zero_lands_in_first():
    assert _bucket_for_latency_ms(0.0) == "0-5s"


def test_compute_worker_latency_buckets_distribution():
    # 5 events spread across the buckets
    latencies = [1_000, 7_000, 14_000, 32_000, 75_000]
    buckets = compute_worker_latency_buckets(latencies)
    assert buckets["0-5s"] == 1
    assert buckets["5-10s"] == 1
    assert buckets["10-15s"] == 1
    assert buckets["30-45s"] == 1
    assert buckets["60s+"] == 1
    # empty buckets are present as zeroes (chart layout depends on this)
    assert buckets["15-20s"] == 0
    assert buckets["20-30s"] == 0
    assert buckets["45-60s"] == 0
    # total preserved
    assert sum(buckets.values()) == len(latencies)


def test_buckets_cover_all_eight_labels():
    buckets = compute_worker_latency_buckets([])
    assert set(buckets) == {lbl for lbl, _, _ in WORKER_LATENCY_BUCKETS}
    assert all(v == 0 for v in buckets.values())


def test_timeout_bucket_only_fills_at_60s_or_above():
    # 59_999 ms is the long-tail, 60_000 is timeout
    assert compute_worker_latency_buckets([59_999])["60s+"] == 0
    assert compute_worker_latency_buckets([59_999])["45-60s"] == 1
    assert compute_worker_latency_buckets([60_000])["60s+"] == 1
    assert compute_worker_latency_buckets([60_000])["45-60s"] == 0
    assert compute_worker_latency_buckets([120_000])["60s+"] == 1


# ---------------------------------------------------------------------------- #
# Stats                                                                        #
# ---------------------------------------------------------------------------- #


def test_stats_empty_input_is_zeroed():
    s = compute_worker_latency_stats([])
    assert s == {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0}


def test_stats_single_sample():
    s = compute_worker_latency_stats([12_345.0])
    assert s["count"] == 1
    assert s["mean"] == 12_345.0
    assert s["p50"] == 12_345.0
    assert s["p95"] == 12_345.0


def test_stats_known_distribution():
    # 5 samples — median is the middle value, p95 (nearest-rank) is the max.
    latencies = [1_000, 3_000, 5_000, 9_000, 27_000]
    s = compute_worker_latency_stats(latencies)
    assert s["count"] == 5
    assert s["mean"] == pytest.approx(9_000.0)
    assert s["p50"] == pytest.approx(5_000.0)
    assert s["p95"] == pytest.approx(27_000.0)


def test_stats_p95_climbs_with_tail():
    # 10 samples — nearest-rank p95 is the top sample, so a tail event must
    # show through. (With 19 baseline samples the p95 index lands on a
    # baseline value; using 9 baseline + 1 tail puts the tail at index 9 = p95.)
    base = [5_000] * 9
    no_tail = compute_worker_latency_stats(base)
    with_tail = compute_worker_latency_stats(base + [58_000])
    # the long-tail sample must push p95 up materially
    assert with_tail["p95"] > no_tail["p95"]
    assert with_tail["p95"] == pytest.approx(58_000.0)


# ---------------------------------------------------------------------------- #
# State integration                                                            #
# ---------------------------------------------------------------------------- #


def test_state_collects_worker_latencies_from_events():
    s = PulseState()
    for ms in (1_000, 7_500, 22_000, 61_500):
        s.apply(_worker_evt(ms))
    assert list(s.worker_latencies_ms) == [1_000.0, 7_500.0, 22_000.0, 61_500.0]


def test_state_rolling_window_evicts_oldest():
    s = PulseState()
    # Push WORKER_LATENCY_WINDOW + 1 events; the first must be evicted.
    for i in range(WORKER_LATENCY_WINDOW + 1):
        s.apply(_worker_evt(float(i)))
    assert len(s.worker_latencies_ms) == WORKER_LATENCY_WINDOW
    # window size constant matches the bead spec
    assert WORKER_LATENCY_WINDOW == 200
    # oldest (0.0) is gone; newest (200.0) is in
    assert 0.0 not in s.worker_latencies_ms
    assert float(WORKER_LATENCY_WINDOW) in s.worker_latencies_ms


def test_state_ignores_worker_event_without_latency():
    s = PulseState()
    evt = _worker_evt(0)
    del evt["data"]["latency_ms"]
    s.apply(evt)
    assert len(s.worker_latencies_ms) == 0


def test_state_does_not_double_count_worker_cost():
    """Worker cost is informational only — it must NOT bump session.cost_usd
    or the burn gauge will overstate spend when both improver and worker fire.
    """
    s = PulseState()
    s.apply(_worker_evt(5_000))
    sess = next(iter(s.sessions.values()))
    assert sess.cost_usd == 0.0


# ---------------------------------------------------------------------------- #
# Headless render                                                              #
# ---------------------------------------------------------------------------- #


class _HostApp(App):
    def __init__(self, widget):
        super().__init__()
        self._w = widget

    def compose(self) -> ComposeResult:
        yield self._w


@pytest.mark.asyncio
async def test_headless_empty_state():
    w = WorkerLatencyHistogram()
    app = _HostApp(w)
    async with app.run_test() as pilot:
        w.latencies_ms = []
        await pilot.pause()
        rendered = str(w.render())
        assert "no worker calls yet" in rendered


@pytest.mark.asyncio
async def test_headless_chart_and_summary_appear():
    """Realistic 37-call payload — verify chart rows + stats line render."""
    # Distribution chosen so every bucket gets non-trivial signal AND the
    # tail/timeout buckets are visible.
    latencies = (
        [2_000] * 6
        + [7_000] * 18
        + [12_000] * 8
        + [17_000] * 3
        + [25_000] * 1
        + [50_000] * 1
    )
    assert len(latencies) == 37
    w = WorkerLatencyHistogram()
    app = _HostApp(w)
    async with app.run_test() as pilot:
        w.latencies_ms = latencies
        await pilot.pause()
        rendered = str(w.render())
        # bucket labels present
        for label, _, _ in WORKER_LATENCY_BUCKETS:
            assert label in rendered
        # stats line present
        assert "mean" in rendered
        assert "p50" in rendered
        assert "p95" in rendered
        # warning annotations present
        assert "long tail" in rendered
        assert "timeout" in rendered
        # border title carries n=37
        assert "n=37" in w.border_title


@pytest.mark.asyncio
async def test_headless_single_sample_degrades_gracefully():
    w = WorkerLatencyHistogram()
    app = _HostApp(w)
    async with app.run_test() as pilot:
        w.latencies_ms = [12_345.0]
        await pilot.pause()
        rendered = str(w.render())
        # single-point degrade message instead of a misleading 100% bar
        assert "only 1 sample" in rendered
        # stats still computed
        assert "mean 12.3s" in rendered
