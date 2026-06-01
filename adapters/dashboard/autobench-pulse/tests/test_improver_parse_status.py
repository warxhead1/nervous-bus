"""Tests for ane0 — improver parse_status badge on the AHE panel.

Cycle 5 (session 01KRS6CWS6JDE946S2RJMT57WX) iter 0 had a silent parser
fallback: the LLM emitted slightly-malformed JSON, the parser couldn't
recover, and the dashboard showed "improver completed" with no signal that
anything had gone wrong. This test family ensures the parse_status field
now propagates from the autobench.improver.reasoning.v1 event into the
AHE panel payload, and that the widget renders distinct badges per state.
"""

from __future__ import annotations

from pulse_app.state import PulseState
from pulse_app.widgets import _format_parse_status_badge


def _make_state_with_reasoning(status: str, session_id: str = "01TEST_SESS") -> PulseState:
    state = PulseState()
    state.apply({
        "type": "autobench.improver.reasoning.v1",
        "data": {
            "session_id": session_id,
            "iteration": 0,
            "parse_status": status,
            "fallback_reason": "json_extract_failed" if "fail" in status or "fallback" in status else None,
        },
    })
    return state


def test_reasoning_event_in_valid_channels():
    from pulse_app.state import VALID_CHANNELS
    assert "autobench.improver.reasoning.v1" in VALID_CHANNELS


def test_parse_status_recorded_on_session():
    state = _make_state_with_reasoning("ok")
    sess = state.sessions["01TEST_SESS"]
    assert sess.last_improver_parse_status == "ok"
    assert sess.last_improver_iteration == 0


def test_parse_status_summary_returns_latest():
    state = _make_state_with_reasoning("fell_back_to_rule_based")
    snap = state.improver_parse_status_summary()
    assert snap is not None
    assert snap["parse_status"] == "fell_back_to_rule_based"
    assert snap["session_id"] == "01TEST_SESS"


def test_no_summary_before_first_reasoning_event():
    state = PulseState()
    assert state.improver_parse_status_summary() is None


def test_parse_status_threads_into_ahe_panel_payload():
    state = PulseState()
    # Need a prediction to get a non-None payload.
    sid = "01TEST_SESS"
    state.apply({
        "type": "autobench.improver.prediction.v1",
        "data": {
            "session_id": sid,
            "iteration": 0,
            "predicted_score_delta": 0.08,
            "predicted_verdict_class_changes": {"OK": 3, "CE": -3},
            "confidence": 0.7,
        },
    })
    state.apply({
        "type": "autobench.improver.reasoning.v1",
        "data": {
            "session_id": sid,
            "iteration": 0,
            "parse_status": "fell_back_to_rule_based",
            "fallback_reason": "json_extract_failed",
        },
    })
    payload = state.ahe_prediction_panel_payload()
    assert payload is not None
    assert payload["parse_status"] == "fell_back_to_rule_based"


def test_badge_renders_per_status():
    # Distinct visuals for the four operational states.
    ok = _format_parse_status_badge("ok")
    fallback = _format_parse_status_badge("fell_back_to_rule_based")
    no_change = _format_parse_status_badge("no_change")
    repaired = _format_parse_status_badge("ok_after_repair")
    # Distinct markup strings.
    assert ok != fallback != no_change != repaired != ok
    # Empty string when no signal yet.
    assert _format_parse_status_badge(None) == ""
    assert _format_parse_status_badge("") == ""
    # Unknown status renders something readable, not a crash.
    weird = _format_parse_status_badge("unknown_status_x")
    assert "unknown_status_x" in weird
