"""Tests for the DivergenceHighlights ribbon widget + its state plumbing.

Covers the four ribbon states described in nervous-bus-ebgi:
  1. delta.diff with no_change=true → muted "no change" message
  2. delta.diff with system_prompt_diff → headline mentions system_prompt
  3. delta.diff with budget_changes → headline shows budget delta inline
  4. improver.divergence event → headline shows both proposals
Plus most-recent-wins (newer event displaces older) and a headless mount
smoke test driven through Textual's Pilot.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import (
    DIVERGENCE_KIND_DELTA_DIFF,
    DIVERGENCE_KIND_DIVERGENCE,
    DivergenceEvent,
    PulseState,
)
from pulse_app.widgets import DivergenceHighlights


SID = "01KRQMD4M20RYCDS8X5CHWTPMP"


def _delta_diff_event(
    *,
    iteration: int = 2,
    system_prompt_diff: str = "",
    tool_surface_diff: str = "",
    rollout_protocol_change=None,
    context_manager_change=None,
    budget_changes=None,
    no_change: bool = False,
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"ddiff-{iteration}",
        "source": "/autobench",
        "type": "autobench.improver.delta.diff.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.000Z",
        "data": {
            "session_id": SID,
            "iteration": iteration,
            "system_prompt_diff": system_prompt_diff,
            "tool_surface_diff": tool_surface_diff,
            "rollout_protocol_change": rollout_protocol_change,
            "context_manager_change": context_manager_change,
            "budget_changes": budget_changes or {},
            "no_change": no_change,
        },
    }


def _divergence_event(
    *,
    iteration: int = 1,
    llm_delta: dict | None = None,
    heuristic_delta: dict | None = None,
    divergent: bool = True,
    summary: str = "system_prompt_delta differs",
) -> dict:
    return {
        "specversion": "1.0",
        "id": f"div-{iteration}",
        "source": "/autobench",
        "type": "autobench.improver.divergence.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:34:00.000Z",
        "data": {
            "session_id": SID,
            "iteration": iteration,
            "llm_delta": llm_delta or {"system_prompt_delta": "+rule"},
            "heuristic_delta": heuristic_delta or {"context_manager": "fifo"},
            "divergent": divergent,
            "divergence_summary": summary,
        },
    }


# ---------------------------------------------------------------------------- #
# State ingestion                                                              #
# ---------------------------------------------------------------------------- #


def test_state_ingests_delta_diff_no_change():
    """no_change=true must reach the widget so it can render the muted line."""
    s = PulseState()
    s.apply(_delta_diff_event(iteration=2, no_change=True))
    evt = s.latest_divergence_event
    assert isinstance(evt, DivergenceEvent)
    assert evt.kind == DIVERGENCE_KIND_DELTA_DIFF
    assert evt.no_change is True
    assert evt.iteration == 2


def test_state_ingests_delta_diff_with_system_prompt_diff():
    diff = (
        "--- a/system_prompt\n"
        "+++ b/system_prompt\n"
        "@@ -1,2 +1,3 @@\n"
        " context line\n"
        "+Begin response with `import` or `def`. No prose.\n"
        "+Do not include <think> blocks.\n"
        "-old guidance\n"
    )
    s = PulseState()
    s.apply(_delta_diff_event(iteration=2, system_prompt_diff=diff))
    evt = s.latest_divergence_event
    assert evt is not None
    assert evt.no_change is False
    assert "Begin response" in evt.system_prompt_diff


def test_state_ingests_divergence():
    s = PulseState()
    s.apply(
        _divergence_event(
            iteration=1,
            llm_delta={"system_prompt_delta": "+x"},
            heuristic_delta={"context_manager": "fifo"},
            divergent=True,
        )
    )
    evt = s.latest_divergence_event
    assert evt is not None
    assert evt.kind == DIVERGENCE_KIND_DIVERGENCE
    assert evt.divergent is True
    assert "system_prompt_delta" in evt.llm_delta
    assert "context_manager" in evt.heuristic_delta


def test_state_most_recent_wins():
    """Newer event displaces older regardless of kind."""
    s = PulseState()
    s.apply(_divergence_event(iteration=1, divergent=True))
    first = s.latest_divergence_event
    assert first is not None and first.kind == DIVERGENCE_KIND_DIVERGENCE

    s.apply(_delta_diff_event(iteration=2, no_change=True))
    second = s.latest_divergence_event
    assert second is not None
    assert second.kind == DIVERGENCE_KIND_DELTA_DIFF
    assert second.iteration == 2


# ---------------------------------------------------------------------------- #
# Widget render output                                                         #
# ---------------------------------------------------------------------------- #


def _render(evt: DivergenceEvent | None) -> str:
    """Render an event via the widget's pure helpers — no mount required."""
    w = DivergenceHighlights()
    if evt is None:
        return w._empty_text
    if evt.kind == DIVERGENCE_KIND_DELTA_DIFF:
        return w._render_delta_diff(evt)
    if evt.kind == DIVERGENCE_KIND_DIVERGENCE:
        return w._render_divergence(evt)
    return w._empty_text


def test_widget_empty_state_when_no_event():
    out = _render(None)
    assert "Awaiting iteration boundary" in out


def test_widget_no_change_message():
    s = PulseState()
    s.apply(_delta_diff_event(iteration=2, no_change=True))
    out = _render(s.latest_divergence_event)
    # muted "no change" path
    assert "no change since last apply" in out
    assert "iter 1 → 2" in out


def test_widget_headline_mentions_system_prompt():
    diff = (
        "--- a/system_prompt\n"
        "+++ b/system_prompt\n"
        "@@ -1,1 +1,2 @@\n"
        " context\n"
        "+Begin response with `import` or `def`. No prose.\n"
    )
    s = PulseState()
    s.apply(_delta_diff_event(iteration=2, system_prompt_diff=diff))
    out = _render(s.latest_divergence_event)
    assert "system_prompt" in out
    # The diff excerpt line is rendered too (success-coloured + line)
    assert "Begin response" in out
    assert "iter 1 → 2" in out


def test_widget_headline_shows_budget_delta_inline():
    s = PulseState()
    s.apply(
        _delta_diff_event(
            iteration=3,
            budget_changes={
                "max_tokens": {"before": 4096, "after": 2048},
            },
        )
    )
    out = _render(s.latest_divergence_event)
    assert "max_tokens" in out
    assert "4096" in out and "2048" in out
    # No diff excerpt should appear since neither system_prompt nor
    # tool_surface changed.
    assert "Begin response" not in out


def test_widget_divergence_shows_both_proposals():
    s = PulseState()
    s.apply(
        _divergence_event(
            iteration=1,
            llm_delta={
                "system_prompt_delta": "+rule",
                "improvement_summary": "noisy",
            },
            heuristic_delta={"context_manager": "fifo"},
            divergent=True,
            summary="system_prompt vs context_manager",
        )
    )
    out = _render(s.latest_divergence_event)
    # Headline mentions both proposal field-sets (improvement_summary is
    # filtered out — it's not actionable signal).
    assert "system_prompt_delta" in out
    assert "context_manager" in out
    assert "improvement_summary" not in out
    assert "divergence" in out.lower()


def test_widget_truncates_giant_diff():
    """Diffs with many +/- lines must still fit the ribbon."""
    big_diff_lines = ["--- a/system_prompt", "+++ b/system_prompt", "@@ -1,20 +1,40 @@"]
    big_diff_lines.extend(f"+new line {i}" for i in range(20))
    big_diff_lines.extend(f"-old line {i}" for i in range(15))
    diff = "\n".join(big_diff_lines) + "\n"
    s = PulseState()
    s.apply(_delta_diff_event(iteration=4, system_prompt_diff=diff))
    out = _render(s.latest_divergence_event)
    # Hint about more lines must appear when we truncated.
    assert "more diff lines" in out
    # And we must not have splatted all 35 lines into the ribbon.
    rendered_change_lines = [
        ln for ln in out.splitlines() if ln.startswith("[green]") or ln.startswith("[red]")
    ]
    assert len(rendered_change_lines) <= 3


# ---------------------------------------------------------------------------- #
# Headless mount smoke test                                                    #
# ---------------------------------------------------------------------------- #


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


@pytest.mark.asyncio
async def test_divergence_highlights_mounts_and_updates():
    """Headless: feed events through state → reactive → widget renders."""
    ribbon = DivergenceHighlights()
    app = _SingleWidgetApp(ribbon)
    state = PulseState()
    async with app.run_test() as pilot:
        # Empty state mounted
        await pilot.pause()
        assert ribbon.event is None

        # delta.diff with system_prompt change
        state.apply(
            _delta_diff_event(
                iteration=2,
                system_prompt_diff=(
                    "--- a/system_prompt\n"
                    "+++ b/system_prompt\n"
                    "@@ -1,1 +1,2 @@\n"
                    "+Begin response with `import` or `def`.\n"
                ),
            )
        )
        ribbon.event = state.latest_divergence_event
        await pilot.pause()
        assert ribbon.event is not None
        assert ribbon.event.kind == DIVERGENCE_KIND_DELTA_DIFF

        # Newer divergence event displaces it
        state.apply(_divergence_event(iteration=3, divergent=True))
        ribbon.event = state.latest_divergence_event
        await pilot.pause()
        assert ribbon.event.kind == DIVERGENCE_KIND_DIVERGENCE
        assert ribbon.event.iteration == 3
