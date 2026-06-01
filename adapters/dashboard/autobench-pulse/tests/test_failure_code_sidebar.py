"""Tests for the FailureCodeSidebar widget + PulseState failure-ring plumbing.

Covers (nervous-bus-ybye):
  * Unit:    5 case.result events (3 CE + 2 OK) → ring has 3 entries, all CE
  * Unit:    6th CE evicts the oldest CE (FIFO via deque maxlen)
  * Unit:    only CE/RE/TLE/MLE pushed; OK/WA never enter the ring
  * Headless: widget rendered through Textual Pilot reflects the right case_ids
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import (
    FAILURE_RING_SIZE,
    FAILURE_VERDICTS,
    FailureCase,
    PulseState,
)
from pulse_app.widgets import FailureCodeSidebar


# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _case_result_evt(
    case_id: str,
    verdict: str,
    *,
    session_id: str = "S1",
    iteration: int = 1,
    language: str = "python",
    generated_code: str = "print('hi')\nx = 1\n",
    generated_code_length: int | None = None,
    p_score: float = 0.0,
    latency_ms: float = 10.0,
) -> dict[str, Any]:
    """Build a CloudEvents-lite envelope for autobench.case.result.v1."""
    if generated_code_length is None:
        generated_code_length = len(generated_code)
    return {
        "specversion": "1.0",
        "id": f"e-{case_id}",
        "source": "/autobench",
        "type": "autobench.case.result.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:38.728Z",
        "data": {
            "case_id": case_id,
            "iteration": iteration,
            "language": language,
            "verdict": verdict,
            "p_score": p_score,
            "latency_ms": latency_ms,
            "generated_code": generated_code,
            "generated_code_length": generated_code_length,
            "session_id": session_id,
        },
    }


# ---------------------------------------------------------------------------- #
# Unit tests — PulseState ring                                                 #
# ---------------------------------------------------------------------------- #


def test_three_ce_two_ok_yields_three_ce_in_ring():
    state = PulseState()
    state.apply(_case_result_evt("c1", "CE"))
    state.apply(_case_result_evt("c2", "OK"))
    state.apply(_case_result_evt("c3", "CE"))
    state.apply(_case_result_evt("c4", "OK"))
    state.apply(_case_result_evt("c5", "CE"))

    assert len(state.failure_cases) == 3
    assert [fc.case_id for fc in state.failure_cases] == ["c1", "c3", "c5"]
    assert all(fc.verdict == "CE" for fc in state.failure_cases)


def test_sixth_ce_evicts_oldest_ce_fifo():
    state = PulseState()
    for i in range(1, 4):  # CE-1, CE-2, CE-3
        state.apply(_case_result_evt(f"ce{i}", "CE"))
    assert [fc.case_id for fc in state.failure_cases] == ["ce1", "ce2", "ce3"]

    # 4th CE arrives — oldest (ce1) is evicted.
    state.apply(_case_result_evt("ce4", "CE"))
    assert [fc.case_id for fc in state.failure_cases] == ["ce2", "ce3", "ce4"]

    # Verify the ring stays bounded at FAILURE_RING_SIZE.
    state.apply(_case_result_evt("ce5", "CE"))
    state.apply(_case_result_evt("ce6", "CE"))
    assert len(state.failure_cases) == FAILURE_RING_SIZE
    assert [fc.case_id for fc in state.failure_cases] == ["ce4", "ce5", "ce6"]


def test_only_failure_verdicts_enter_the_ring():
    state = PulseState()
    # OK / WA / PASS / "" should all be ignored.
    for bad in ("OK", "WA", "PASS", "", "ok"):  # case-folded — "ok" → "OK"
        state.apply(_case_result_evt(f"x-{bad or 'empty'}", bad))
    assert len(state.failure_cases) == 0

    # All four failure verdicts should be admitted.
    for v in sorted(FAILURE_VERDICTS):
        state.apply(_case_result_evt(f"f-{v}", v))
    assert {fc.verdict for fc in state.failure_cases} <= FAILURE_VERDICTS
    # Ring is bounded — we pushed >FAILURE_RING_SIZE, only most recent kept.
    assert len(state.failure_cases) == FAILURE_RING_SIZE


def test_revision_increments_on_each_failure():
    state = PulseState()
    assert state.failure_revision == 0
    state.apply(_case_result_evt("c1", "CE"))
    assert state.failure_revision == 1
    state.apply(_case_result_evt("c2", "OK"))  # ignored
    assert state.failure_revision == 1
    state.apply(_case_result_evt("c3", "RE"))
    assert state.failure_revision == 2


def test_long_code_is_truncated_with_flag():
    state = PulseState()
    big = "x" * 5000
    state.apply(_case_result_evt("c1", "CE", generated_code=big))
    fc = state.failure_cases[0]
    assert len(fc.code_preview) == 200
    assert fc.code_truncated is True


# ---------------------------------------------------------------------------- #
# Headless Pilot test — widget renders the right case_ids                      #
# ---------------------------------------------------------------------------- #


class _SidebarHostApp(App):
    """Mount one FailureCodeSidebar + drive it from a PulseState."""

    def __init__(self, state: PulseState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        yield FailureCodeSidebar(id="failure-sidebar")

    def push_state(self) -> None:
        sidebar = self.query_one(FailureCodeSidebar)
        sidebar.set_cases(list(self._state.failure_cases), self._state.failure_revision)


@pytest.mark.asyncio
async def test_widget_renders_case_ids_via_pilot():
    state = PulseState()
    state.apply(_case_result_evt("alpha", "CE"))
    state.apply(_case_result_evt("beta", "OK"))     # ignored
    state.apply(_case_result_evt("gamma", "RE"))
    state.apply(_case_result_evt("delta", "TLE"))

    app = _SidebarHostApp(state)
    async with app.run_test() as pilot:
        app.push_state()
        await pilot.pause()
        await pilot.pause()
        sidebar = app.query_one(FailureCodeSidebar)
        markup = sidebar._build_markup()
        # All three failure case_ids should appear; the OK one must NOT.
        assert "alpha" in markup
        assert "gamma" in markup
        assert "delta" in markup
        assert "beta" not in markup
        # Each failure verdict should be referenced in its badge form.
        assert "CE" in markup
        assert "RE" in markup
        assert "TLE" in markup


@pytest.mark.asyncio
async def test_widget_empty_state_via_pilot():
    state = PulseState()  # no failures
    app = _SidebarHostApp(state)
    async with app.run_test() as pilot:
        app.push_state()
        await pilot.pause()
        sidebar = app.query_one(FailureCodeSidebar)
        markup = sidebar._build_markup()
        assert "waiting for failures" in markup


@pytest.mark.asyncio
async def test_widget_revision_short_circuits_on_no_change():
    state = PulseState()
    state.apply(_case_result_evt("c1", "CE"))
    app = _SidebarHostApp(state)
    async with app.run_test() as pilot:
        app.push_state()
        await pilot.pause()
        sidebar = app.query_one(FailureCodeSidebar)
        rev_before = sidebar.revision
        # Repeat-call with identical revision — widget must NOT bump its rev.
        sidebar.set_cases(list(state.failure_cases), state.failure_revision)
        await pilot.pause()
        assert sidebar.revision == rev_before


def test_render_budget_under_50ms_worst_case():
    """Render path must stay under the 10 Hz budget even with the worst-case
    ring (FAILURE_RING_SIZE entries × 200-char previews × many newlines).
    """
    import time as _time

    state = PulseState()
    # Build a pathological preview: 200 chars, every char a newline.
    nasty = "\n" * 200
    for i in range(FAILURE_RING_SIZE):
        state.apply(_case_result_evt(f"c{i}", "CE", generated_code=nasty))

    sidebar = FailureCodeSidebar()
    sidebar._cases = list(state.failure_cases)
    t0 = _time.perf_counter()
    for _ in range(50):
        sidebar._build_markup()
    elapsed_per_call = (_time.perf_counter() - t0) / 50.0
    # Generous ceiling — typical run is ~0.1 ms. 50 ms is the §7.5.5 hard cap.
    assert elapsed_per_call < 0.050, f"render too slow: {elapsed_per_call*1000:.2f}ms"


def test_failure_case_dataclass_smoke():
    """Direct FailureCase construction round-trip (sanity, no widget)."""
    fc = FailureCase(
        case_id="probe-1",
        verdict="CE",
        iteration=2,
        language="python",
        p_score=0.0,
        latency_ms=12.3,
        code_preview="print(1)",
        code_truncated=False,
        session_id="S",
    )
    assert fc.case_id == "probe-1"
    assert fc.verdict in FAILURE_VERDICTS
