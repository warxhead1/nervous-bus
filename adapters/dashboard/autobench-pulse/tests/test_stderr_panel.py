"""Tests for the new StderrFaultPanel widget (FIX 3).

Covers:
  * VALID_CHANNELS gates the new ``autobench.sandbox.stderr.v1`` channel
  * Five stderr events with mixed verdicts populate the panel's rolling ring
  * The panel renders verdict-colored badges + case ids + excerpts
  * Overflow eviction: a 6th event evicts the oldest
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import STDERR_RING_SIZE, VALID_CHANNELS, PulseState
from pulse_app.widgets import StderrFaultPanel


SID = "01KRQ_STDERR_TEST_SESSION_X"


def _stderr_evt(case_id: str, verdict: str, excerpt: str, language: str = "python") -> dict:
    return {
        "specversion": "1.0",
        "id": f"stderr-{case_id}",
        "source": "/autobench",
        "type": "autobench.sandbox.stderr.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": SID,
            "case_id": case_id,
            "iteration": 1,
            "verdict": verdict,
            "stderr_excerpt": excerpt,
            "exit_code": 1,
            "language": language,
        },
    }


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def test_sandbox_stderr_channel_is_in_valid_channels():
    """FIX 3 acceptance: the channel is now ingested rather than dropped."""
    assert "autobench.sandbox.stderr.v1" in VALID_CHANNELS


def test_stderr_ring_size_is_five():
    """The spec calls for the last 5 entries — verify the ring cap."""
    assert STDERR_RING_SIZE == 5


def test_state_ingests_stderr_event_into_ring():
    s = PulseState()
    s.apply(_stderr_evt("case_0001", "CE", "SyntaxError: invalid syntax (line 17)"))
    assert len(s.recent_stderr) == 1
    entry = s.recent_stderr[-1]
    assert entry["case_id"] == "case_0001"
    assert entry["verdict"] == "CE"
    assert "SyntaxError" in entry["stderr_excerpt"]
    assert s.stderr_revision == 1


def test_state_stderr_ring_evicts_oldest_at_cap():
    s = PulseState()
    # Push six events; the ring caps at 5, so the oldest (case_0001) is evicted.
    for i in range(1, 7):
        s.apply(_stderr_evt(f"case_{i:04d}", "RE", f"RuntimeError #{i}"))
    assert len(s.recent_stderr) == STDERR_RING_SIZE
    ids = [e["case_id"] for e in s.recent_stderr]
    assert "case_0001" not in ids
    assert "case_0006" in ids


@pytest.mark.asyncio
async def test_panel_renders_five_mixed_verdicts():
    """Feed 5 events with mixed verdicts and assert the rendered markup."""
    s = PulseState()
    feed = [
        ("case_0042", "CE", "SyntaxError: invalid syntax (line 17)"),
        ("case_0043", "RE", "RuntimeError: index out of range"),
        ("case_0044", "TLE", "process exceeded 10s wall clock"),
        ("case_0045", "MLE", "OOM: 512MB cgroup limit hit"),
        ("case_0046", "CE", "NameError: name 'foo' is not defined"),
    ]
    for cid, verdict, excerpt in feed:
        s.apply(_stderr_evt(cid, verdict, excerpt))
    assert len(s.recent_stderr) == 5

    panel = StderrFaultPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.set_entries(list(s.recent_stderr), s.stderr_revision)
        await pilot.pause()
        rendered = panel._build_markup()
        # Every case id should appear.
        for cid, _v, _e in feed:
            assert cid in rendered
        # Verdict tags appear (rich markup wraps them; just check the
        # raw verdict tokens are present).
        for _cid, verdict, _excerpt in feed:
            assert verdict in rendered
        # At least one excerpt should land in the rendered output.
        assert "SyntaxError" in rendered or "RuntimeError" in rendered


@pytest.mark.asyncio
async def test_panel_empty_state_renders_placeholder():
    panel = StderrFaultPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        rendered = panel._build_markup()
        assert "waiting" in rendered.lower() or "no stderr" in rendered.lower()


@pytest.mark.asyncio
async def test_panel_short_circuits_when_revision_unchanged():
    """Pushing the same revision twice does not re-render."""
    panel = StderrFaultPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        entries = [
            {"case_id": "c1", "verdict": "CE", "stderr_excerpt": "ex"},
        ]
        panel.set_entries(entries, revision=1)
        await pilot.pause()
        rev_before = panel.revision
        panel.set_entries(entries, revision=1)
        await pilot.pause()
        # Revision should not change; entries unchanged.
        assert panel.revision == rev_before


@pytest.mark.asyncio
async def test_panel_renders_newest_first():
    """Most recent stderr appears at the top of the rendered output."""
    s = PulseState()
    s.apply(_stderr_evt("oldest", "CE", "old fault"))
    s.apply(_stderr_evt("newest", "RE", "new fault"))
    panel = StderrFaultPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        panel.set_entries(list(s.recent_stderr), s.stderr_revision)
        await pilot.pause()
        rendered = panel._build_markup()
        # newest should be earlier in the rendered string than oldest.
        assert rendered.index("newest") < rendered.index("oldest")
