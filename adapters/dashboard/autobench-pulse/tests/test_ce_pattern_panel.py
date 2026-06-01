"""Tests for CEPatternPanel — top-3 failing-case shared-prefix clusters.

Covers:
  * ``PulseState`` ingestion of ``autobench.failure_pattern.v1`` events
  * Top-3 sorting / truncation
  * Fallback path that infers patterns from ``autobench.case.result.v1`` events
    when no detector events have arrived yet
  * Headless Pilot render: prefix appears in widget output and ``<think>``
    style payloads are repr-escaped so newlines don't break alignment
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from pulse_app.state import PulseState
from pulse_app.widgets import CEPatternPanel


SID = "01KRQMD4M20RYCDS8X5CHWTPMP"


def _fp_event(verdict: str, prefix: str, sample_count: int, total_in_class: int = None,
              case_ids: list[str] = None, iteration: int = 0) -> dict:
    if total_in_class is None:
        total_in_class = sample_count
    if case_ids is None:
        case_ids = [f"c{i}" for i in range(min(5, sample_count))]
    return {
        "specversion": "1.0", "id": f"fp-{prefix[:4]}-{verdict}", "source": "/autobench",
        "type": "autobench.failure_pattern.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": SID,
            "iteration": iteration,
            "verdict": verdict,
            "prefix": prefix,
            "sample_count": sample_count,
            "total_in_class": total_in_class,
            "sample_case_ids": case_ids,
            "prefix_len_chars": 20,
        },
    }


def _case_event(verdict: str, case_id: str, generated_code: str, iteration: int = 0) -> dict:
    return {
        "specversion": "1.0", "id": f"cr-{case_id}", "source": "/autobench",
        "type": "autobench.case.result.v1",
        "datacontenttype": "application/json",
        "time": "2026-05-16T05:33:34.722Z",
        "data": {
            "session_id": SID,
            "case_id": case_id,
            "iteration": iteration,
            "language": "python",
            "verdict": verdict,
            "p_score": 0.0,
            "latency_ms": 10.0,
            "generated_code": generated_code,
            "generated_code_length": len(generated_code),
        },
    }


# ---------------------------------------------------------------------------- #
# State-level tests                                                            #
# ---------------------------------------------------------------------------- #


def test_top3_sorted_by_sample_count_desc():
    s = PulseState()
    s.apply(_fp_event("CE", "<think>I should ", 5))
    s.apply(_fp_event("CE", "*****|* Sample te", 3))
    s.apply(_fp_event("RE", "i=0|j=n-1|cntA=1", 7))
    top = s.top_failure_patterns(n=3)
    assert [p["sample_count"] for p in top] == [7, 5, 3]
    assert top[0]["verdict"] == "RE"
    assert top[1]["verdict"] == "CE"


def test_top3_caps_at_three_even_with_more():
    s = PulseState()
    for i in range(6):
        # distinct prefixes so they don't collapse onto one bucket
        s.apply(_fp_event("CE", f"prefix-{i}-xxxxxxxx", 10 - i))
    top = s.top_failure_patterns(n=3)
    assert len(top) == 3
    assert [p["sample_count"] for p in top] == [10, 9, 8]


def test_fallback_infers_pattern_from_case_results():
    """No failure_pattern events but 5 CE case.results with shared prefix."""
    s = PulseState()
    shared = "<think>I should consider edge cases"
    for i in range(5):
        s.apply(_case_event("CE", f"case-{i}", shared + f"\nx={i}"))
    # No detector events yet — top_failure_patterns falls back.
    top = s.top_failure_patterns(n=3)
    assert len(top) >= 1
    assert top[0]["verdict"] == "CE"
    assert top[0]["sample_count"] == 5
    assert top[0]["source"] == "inferred"
    # prefix is normalised: leading whitespace stripped, newlines→|
    assert top[0]["prefix"].startswith("<think>I should con")


def test_fallback_skips_ok_cases():
    s = PulseState()
    for i in range(5):
        s.apply(_case_event("OK", f"ok-{i}", "print(42)"))
    assert s.top_failure_patterns(n=3) == []


def test_event_overrides_fallback():
    """Once a failure_pattern event arrives, it takes precedence over inference."""
    s = PulseState()
    for i in range(3):
        s.apply(_case_event("CE", f"c{i}", "abc\ndef"))
    s.apply(_fp_event("CE", "official-prefix", 99))
    top = s.top_failure_patterns(n=3)
    assert top[0]["sample_count"] == 99
    assert top[0]["source"] == "event"


def test_failure_pattern_overwrites_same_bucket():
    """Re-emitted pattern with updated count overwrites the older record."""
    s = PulseState()
    s.apply(_fp_event("CE", "same-prefix", 3, iteration=0))
    s.apply(_fp_event("CE", "same-prefix", 7, iteration=1))
    top = s.top_failure_patterns(n=3)
    assert len(top) == 1
    assert top[0]["sample_count"] == 7


def test_empty_state_returns_empty_list():
    s = PulseState()
    assert s.top_failure_patterns(n=3) == []


# ---------------------------------------------------------------------------- #
# Widget render tests                                                          #
# ---------------------------------------------------------------------------- #


class _SingleWidgetApp(App):
    def __init__(self, widget):
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def test_panel_render_text_empty_state():
    panel = CEPatternPanel()
    text = panel.render_text()
    assert "no failure patterns" in text


def test_panel_render_text_includes_prefix_and_verdict():
    panel = CEPatternPanel()
    panel.patterns = [
        {"verdict": "CE", "prefix": "<think>I should",
         "sample_count": 4, "total_in_class": 5,
         "sample_case_ids": ["a", "b"], "iteration": 0, "source": "event"},
        {"verdict": "RE", "prefix": "i=0|j=n-1|cntA",
         "sample_count": 2, "total_in_class": 6,
         "sample_case_ids": ["x"], "iteration": 0, "source": "event"},
    ]
    text = panel.render_text()
    # verdict badges visible
    assert "CE" in text
    assert "RE" in text
    # prefix shown (repr-escaped — angle brackets survive intact, newlines
    # already collapsed to | upstream)
    assert "<think>I should" in text
    assert "i=0|j=n-1|cntA" in text
    # ratio is rendered
    assert "4/5" in text
    assert "2/6" in text


def test_panel_render_escapes_newlines_and_tabs():
    """Even if a raw '\\n' / '\\t' sneaks through, repr() escapes them."""
    panel = CEPatternPanel()
    panel.patterns = [
        {"verdict": "CE", "prefix": "line1\nline2\ttab",
         "sample_count": 3, "total_in_class": 3,
         "sample_case_ids": ["a"], "iteration": 0, "source": "event"},
    ]
    text = panel.render_text()
    # raw newline / tab characters must NOT appear in the rendered prefix
    # (the literal "\\n" / "\\t" escape sequences are OK)
    assert "\\n" in text
    assert "\\t" in text


def test_panel_render_truncates_long_prefix():
    panel = CEPatternPanel()
    long_prefix = "x" * 200
    panel.patterns = [
        {"verdict": "WA", "prefix": long_prefix,
         "sample_count": 3, "total_in_class": 3,
         "sample_case_ids": ["a"], "iteration": 0, "source": "event"},
    ]
    text = panel.render_text()
    # Ellipsis character indicates truncation happened.
    assert "…" in text


def test_panel_inferred_tag_visible():
    panel = CEPatternPanel()
    panel.patterns = [
        {"verdict": "CE", "prefix": "abc",
         "sample_count": 2, "total_in_class": 2,
         "sample_case_ids": ["a"], "iteration": 0, "source": "inferred"},
    ]
    text = panel.render_text()
    assert "inferred" in text


# ── nervous-bus-yn9v fix 3: empty-state verdict roll-up ────────────────────


def test_empty_state_renders_verdict_roll_up_when_summary_present():
    """Empty CEPatternPanel falls back to the latest iter verdict breakdown.

    Before yn9v fix 3 the panel rendered just "no failure patterns detected
    yet" — a near-empty box at the bottom of the right column. Now it
    surfaces a verdict roll-up so the slot stays useful.
    """
    panel = CEPatternPanel()
    panel.latest_summary = {
        "session_id": "01KSESSION0000000000000000",
        "iteration": 3,
        "num_cases": 20,
        "verdict_counts": {"OK": 16, "WA": 2, "CE": 2},
        "aggregate_score": 0.628,
    }
    text = panel.render_text()
    # Roll-up header + per-verdict tags
    assert "verdict roll-up" in text or "roll-up" in text
    assert "OK:16" in text
    assert "WA:2" in text
    assert "CE:2" in text
    assert "20 cases" in text


def test_empty_state_falls_back_to_legacy_when_no_summary():
    """Without a latest_summary we keep the legacy "no failure patterns" line."""
    panel = CEPatternPanel()
    text = panel.render_text()
    assert "no failure patterns" in text


@pytest.mark.asyncio
async def test_panel_mounts_and_updates():
    panel = CEPatternPanel()
    app = _SingleWidgetApp(panel)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel.patterns = [
            {"verdict": "CE", "prefix": "<think>",
             "sample_count": 5, "total_in_class": 5,
             "sample_case_ids": ["a"], "iteration": 0, "source": "event"},
        ]
        await pilot.pause()
        # The Static renderable contains the formatted text.
        rendered = panel.render()
        # Textual Static.render returns a Rich renderable; str() flattens it.
        flat = str(rendered)
        assert "CE" in flat
