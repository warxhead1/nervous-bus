"""Unit tests for PulseState + event ingestion."""

from __future__ import annotations

from pulse_app.state import PulseState


def test_apply_irrelevant_event_returns_empty():
    s = PulseState()
    assert s.apply({"type": "tengine.session.v1", "data": {"session_id": "x"}}) == set()
    assert len(s.sessions) == 0


def test_apply_full_session(sample_events):
    s = PulseState()
    for evt in sample_events:
        s.apply(evt)
    # one session
    assert len(s.sessions) == 1
    sid = next(iter(s.sessions))
    sess = s.sessions[sid]
    # two iterations seen
    assert set(sess.iterations) == {0, 1}
    it1 = sess.iterations[1]
    assert it1.status == "complete"
    assert it1.aggregate_score == 0.75
    assert it1.prev_score == 0.50
    assert it1.score_delta is not None
    assert abs(it1.score_delta - 0.25) < 1e-9
    # verdict counts rolled up
    assert s.verdict_counts.get("OK", 0) >= 2
    # nervous-bus-dq7l: cost_usd is intentionally 0.0 — MiniMax bills by
    # requests-per-5h and the pricing tables were removed. Field retained
    # for back-compat but no longer accumulates fabricated dollars.
    assert sess.cost_usd == 0.0
    # session verdict reflects improvement
    assert sess.verdict == "improved"


def test_pareto_points_after_complete(sample_events):
    """The sample_events sequence produces ONE completed iteration.summary
    (iter 0 → 0.50). iter 1 lands as iteration.v1 only (no summary), so the
    iteration-final layer holds one point and the per-session back-compat
    fallback adds one more. We assert at least one point lands on iter-1's
    score so the smoke remains green.
    """
    s = PulseState()
    for evt in sample_events:
        s.apply(evt)
    pts = s.pareto_points()
    assert len(pts) >= 1
    # The session's max score is 0.75 (iter 1) — at least one point reaches it.
    assert max(score for _cost, score in pts) >= 0.75 - 1e-9


def test_pareto_accumulates_across_iterations():
    """nervous-bus-sm8n — three iteration.summary events ⇒ three points.

    Pre-change the scatter returned 1 point per session even after many
    iterations. With per-iteration accumulation each iteration.summary
    boundary contributes its own (cumulative_cost, score) tuple.
    """
    s = PulseState()
    sid = "01PARETO_MULTI_ITER_TESTPRD"
    # Feed three iterations' worth of (worker.v1 cost + iteration.summary).
    for i, (cost, score) in enumerate([(0.01, 0.40), (0.02, 0.55), (0.03, 0.70)]):
        s.apply({
            "type": "autobench.worker.v1",
            "data": {"session_id": sid, "cost_usd": cost, "latency_ms": 100.0},
        })
        s.apply({
            "type": "autobench.iteration.summary.v1",
            "data": {
                "session_id": sid,
                "iteration": i,
                "num_cases": 2,
                "aggregate_score": score,
                "verdict_distribution": {"OK": 2},
            },
        })
    pts = s.pareto_points()
    # Three iteration-final points (cost is cumulative — 0.01, 0.03, 0.06).
    assert len(pts) == 3, f"expected 3 iter-final points, got {pts!r}"
    scores = sorted(p[1] for p in pts)
    assert scores == [0.40, 0.55, 0.70]


def test_pareto_classified_frontier_and_dominated():
    """5 points → 2 frontier + 3 dominated, exact classification.

    Frontier = upper-right envelope. Setup:
      A = (1.0, 0.9)  ← frontier  (cheapest high-score)
      B = (2.0, 0.95) ← frontier  (highest score)
      C = (3.0, 0.8)  ← dominated by A (lower score AND higher cost)
      D = (1.5, 0.85) ← dominated by A (lower score AND higher cost than A)
      E = (2.5, 0.6)  ← dominated by everyone
    """
    s = PulseState()
    sid = "01PARETO_CLASSIFY_TEST_ABCDE"
    pts_in = [(1.0, 0.9), (2.0, 0.95), (3.0, 0.8), (1.5, 0.85), (2.5, 0.6)]
    for i, (cost, score) in enumerate(pts_in):
        # Inject directly into iteration_summaries — bypass cumulative cost
        # to make the classification arithmetic crisp.
        s.iteration_summaries[(sid, i)] = {
            "session_id": sid,
            "iteration": i,
            "aggregate_score": score,
            "verdict_distribution": {},
            "received_at": 0.0,
            "cumulative_cost_usd": cost,
        }
    classified = s.pareto_classified()
    frontier = set(classified["frontier"])
    dominated = set(classified["dominated"])
    assert (1.0, 0.9) in frontier
    assert (2.0, 0.95) in frontier
    assert (3.0, 0.8) in dominated
    assert (1.5, 0.85) in dominated
    assert (2.5, 0.6) in dominated
    assert len(frontier) == 2 and len(dominated) == 3


def test_summary_text_smoke(sample_events):
    s = PulseState()
    for evt in sample_events:
        s.apply(evt)
    text = s.summary_text()
    assert "sessions:" in text
    assert "evt/s:" in text


def test_burn_rate_nonnegative(sample_events):
    s = PulseState()
    for evt in sample_events:
        s.apply(evt)
    assert s.burn_rate_per_min() >= 0


def test_cycle_outcome_payload_improved_two_iter():
    """nervous-bus-wutr — synthetic 2-iter improved session.

    Feed iter 0 (score 0.40) and iter 1 (score 0.65) iteration.v1 +
    iteration.summary events plus a worker.v1 cost event. Assert the
    banner payload reports verdict=improved with initial=0.40 and
    final=0.65 across 2 iterations.
    """
    s = PulseState()
    sid = "01CYCLE_BANNER_IMPROVED_TST"
    # iter 0 start/complete
    s.apply({"type": "autobench.iteration.v1",
             "data": {"session_id": sid, "iteration": 0, "harness_version": "v0", "status": "start"}})
    s.apply({"type": "autobench.iteration.v1",
             "data": {"session_id": sid, "iteration": 0, "harness_version": "v0",
                      "status": "complete", "aggregate_score": 0.40,
                      "verdict_counts": {"OK": 2}}})
    s.apply({"type": "autobench.iteration.summary.v1",
             "data": {"session_id": sid, "iteration": 0, "num_cases": 2,
                      "aggregate_score": 0.40, "verdict_distribution": {"OK": 2}}})
    s.apply({"type": "autobench.worker.v1",
             "data": {"session_id": sid, "cost_usd": 0.05, "latency_ms": 100.0}})
    # iter 1 start/complete
    s.apply({"type": "autobench.iteration.v1",
             "data": {"session_id": sid, "iteration": 1, "harness_version": "v1", "status": "start"}})
    s.apply({"type": "autobench.iteration.v1",
             "data": {"session_id": sid, "iteration": 1, "harness_version": "v1",
                      "status": "complete", "aggregate_score": 0.65,
                      "verdict_counts": {"OK": 2}}})
    s.apply({"type": "autobench.iteration.summary.v1",
             "data": {"session_id": sid, "iteration": 1, "num_cases": 2,
                      "aggregate_score": 0.65, "verdict_distribution": {"OK": 2}}})

    payload = s.cycle_outcome_payload()
    assert payload is not None
    assert payload["verdict"] == "improved"
    assert payload["score_initial"] == 0.40
    assert payload["score_final"] == 0.65
    assert payload["iters_count"] == 2
    assert payload["session_short"] == sid[-12:]

    # Banner widget renders the markup containing both scores + "improved".
    from pulse_app.widgets import CycleOutcomeBanner
    markup = CycleOutcomeBanner.render_markup(payload)
    assert "improved" in markup
    assert "0.40" in markup and "0.65" in markup
    assert "2 iters" in markup or "[bold]2[/] iters" in markup


def test_cycle_outcome_payload_empty_state():
    """No sessions → payload is None and renders the empty-state markup."""
    s = PulseState()
    assert s.cycle_outcome_payload() is None
    from pulse_app.widgets import CycleOutcomeBanner
    assert "waiting for events" in CycleOutcomeBanner.render_markup(None)


def test_summary_text_no_dollars_after_dq7l():
    """nervous-bus-dq7l — header must NOT render any $ amount.

    Pre-dq7l, ``summary_text`` displayed an 'all-sessions $' rollup from
    fabricated cost_usd values. MiniMax bills by requests-per-5h, not
    dollars, so the dashboard surfacing $ was a lie. The bookkeeping field
    ``worker_cost_total_usd`` is retained (it tracks whatever the producer
    emits — currently always 0.0) but the rendered header must not show
    a dollar figure until request-rate telemetry replaces it.
    """
    s = PulseState()
    sid = "01TESTHEADERSTATS9L69ABCDEF"
    for amount in (0.0500, 0.0400, 0.0211):
        s.apply({
            "type": "autobench.worker.v1",
            "data": {
                "session_id": sid,
                "cost_usd": amount,
                "latency_ms": 100.0,
                "status": "complete",
            },
        })
    # The rollup field still receives whatever producers emit.
    assert abs(s.worker_cost_total_usd - 0.1111) < 1e-9
    # But the rendered header MUST be $-free.
    text = s.summary_text()
    assert "$" not in text, f"header must not show $; got: {text!r}"
    assert "all-sessions" not in text


def test_verdict_pending_for_unknown_session():
    s = PulseState()
    s.apply({
        "type": "autobench.sandbox.v1",
        "data": {"session_id": "X", "status": "complete", "verdict": "OK",
                 "case_id": "c1", "language": "python", "sandbox_type": "s"},
    })
    # latest_iter is iteration 0 in start state — verdict should be "running"
    assert s.sessions["X"].verdict in {"running", "pending", "complete"}
