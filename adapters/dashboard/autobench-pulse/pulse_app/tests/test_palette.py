"""Contract tests for ``pulse_app.palette`` — the single source of color truth.

The palette is what every widget refers to for verdict-anchored coloring;
these tests pin that contract so a future drive-by edit (e.g. "rename WA
to amber") fails loudly here instead of silently desynchronising widget
colors from autobench's Verdict enum.

What's locked in:

1. **VERDICT_PALETTE covers every Verdict enum value.** The palette must
   be exhaustive over ``autobench.core.Verdict``. If autobench adds a new
   verdict, this test forces an explicit palette decision rather than
   letting widgets silently render an unknown verdict in fallback white.

2. **No collision between WA-yellow and the budget-warning ribbon.** The
   pulse-v2 critique was specifically about a yellow budget ribbon
   competing with WA verdict tags. We assert COLOR_BUDGET_WARN != WA color
   AND that the CostRatePanel warning-text formatter doesn't emit ``yellow``
   for non-hard-cap thresholds.

3. **VerdictHistogram per-verdict color contract.** For each verdict the
   histogram is asked to render, ``VerdictHistogram.color_for(v)`` returns
   the palette entry — i.e. the chart's colors match the dot colors and
   the sidebar colors. Snapshot-style: one assertion per verdict.
"""

from __future__ import annotations

import pytest

from autobench.core import Verdict
from pulse_app.palette import (
    COLOR_AHE_HIT,
    COLOR_AHE_MISS,
    COLOR_AHE_PENDING,
    COLOR_AHE_REFUTED,
    COLOR_BUDGET_WARN,
    COLOR_DIM,
    COLOR_QUEUE_PRESSURE_CRIT,
    COLOR_QUEUE_PRESSURE_OK,
    COLOR_QUEUE_PRESSURE_WATCH,
    VERDICT_PALETTE,
    verdict_color,
)
from pulse_app.widgets import CostRatePanel, VerdictHistogram


# ─────────────────────────────────────────────────────────────────────────── #
# (1) VERDICT_PALETTE covers every Verdict in autobench.core                  #
# ─────────────────────────────────────────────────────────────────────────── #


def test_palette_covers_every_verdict_enum_value():
    """Every ``autobench.core.Verdict`` member must have a palette entry.

    This locks the contract that adding a new verdict to autobench forces
    a palette decision — there is no silent fallback for verdict-anchored
    colors.
    """
    enum_values = {v.value for v in Verdict}
    palette_keys = set(VERDICT_PALETTE.keys())
    missing = enum_values - palette_keys
    extra = palette_keys - enum_values
    assert not missing, (
        f"VERDICT_PALETTE missing entries for: {sorted(missing)} — "
        "every autobench.core.Verdict value needs a palette color."
    )
    assert not extra, (
        f"VERDICT_PALETTE has stale keys not in Verdict: {sorted(extra)} — "
        "remove them or sync with autobench.core.Verdict."
    )


def test_verdict_color_function_returns_palette_value_for_known_verdicts():
    """``verdict_color`` is the canonical accessor — verify it matches the dict."""
    for v in Verdict:
        assert verdict_color(v.value) == VERDICT_PALETTE[v.value]


def test_verdict_color_returns_default_for_unknown_verdict():
    """Unknown verdicts fall back to the supplied default (white by default)."""
    assert verdict_color("ZZZ_UNKNOWN") == "white"
    assert verdict_color("ZZZ_UNKNOWN", default="dim") == "dim"


# ─────────────────────────────────────────────────────────────────────────── #
# (2) No yellow / WA collision in the budget warning ribbon                   #
# ─────────────────────────────────────────────────────────────────────────── #


def test_budget_warn_color_does_not_equal_wa_verdict_color():
    """COLOR_BUDGET_WARN must NOT equal VERDICT_PALETTE['WA'].

    This is the literal collision the v2-pulse critique flagged:
    yellow ribbon competing with yellow WA verdict tags. The two signals
    live in different semantic domains (spend-rate vs code-quality) and
    must be visually distinguishable.
    """
    assert COLOR_BUDGET_WARN != VERDICT_PALETTE["WA"], (
        "Budget warning color collides with WA verdict color — pick a "
        "non-yellow color for COLOR_BUDGET_WARN."
    )


def test_cost_rate_panel_warning_does_not_emit_yellow_for_soft_thresholds():
    """CostRatePanel._format_warning at 50%/80% must NOT render ``[yellow]``.

    The 50%/80% (soft) budget thresholds use COLOR_BUDGET_WARN; only the
    hard-cap (≥100%) tier escalates to red — never yellow at any tier.
    Yellow is reserved for WA verdicts and queue-pressure watch.
    """
    # Soft threshold at 50%
    payload_50 = {
        "thresholds_fired": {0.5: (1234.5, 7)},
        "max_cost_usd": 1.0,
    }
    out_50 = CostRatePanel._format_warning(payload_50)
    assert "yellow" not in out_50, (
        f"Budget warning at 50% emitted 'yellow' tag: {out_50!r}"
    )
    assert "50%" in out_50  # sanity — formatter still produces the threshold tag

    # Soft threshold at 80%
    payload_80 = {
        "thresholds_fired": {0.8: (1234.5, 7)},
        "max_cost_usd": 1.0,
    }
    out_80 = CostRatePanel._format_warning(payload_80)
    assert "yellow" not in out_80, (
        f"Budget warning at 80% emitted 'yellow' tag: {out_80!r}"
    )
    assert "80%" in out_80


def test_cost_rate_panel_warning_hard_cap_escalates_to_crit_color():
    """The hard-cap (≥100%) tier uses the queue-pressure critical color
    (red). This is the one tier where the budget warning legitimately
    crosses into verdict-fail territory."""
    payload_hard = {
        "thresholds_fired": {1.0: (1234.5, 7)},
        "max_cost_usd": 1.0,
    }
    out = CostRatePanel._format_warning(payload_hard)
    assert COLOR_QUEUE_PRESSURE_CRIT in out
    assert "HARD CAP" in out


# ─────────────────────────────────────────────────────────────────────────── #
# (3) VerdictHistogram per-verdict color snapshot                             #
# ─────────────────────────────────────────────────────────────────────────── #


@pytest.mark.parametrize(
    "verdict,expected_color",
    [
        ("OK", "green"),
        ("WA", "yellow"),
        ("CE", "orange1"),
        ("RE", "orange1"),
        ("TLE", "red"),
        ("MLE", "red"),
        ("VF", "magenta"),
        ("RV", "magenta"),
        ("RD", "magenta"),
        ("RT", "magenta"),
    ],
)
def test_verdict_histogram_color_for_each_verdict(verdict, expected_color):
    """``VerdictHistogram.color_for`` returns the palette color for each
    verdict. This is the "color contract" the AC pinned — assert every
    OK/WA/CE/RE/TLE/MLE plus the full enum tail matches palette.py.
    """
    assert VerdictHistogram.color_for(verdict) == expected_color
    assert VerdictHistogram.color_for(verdict) == VERDICT_PALETTE[verdict]


# ─────────────────────────────────────────────────────────────────────────── #
# (4) Auxiliary semantic constants are non-empty rich color names             #
# ─────────────────────────────────────────────────────────────────────────── #


def test_auxiliary_colors_are_non_empty_strings():
    """Defensive — all aux constants must be string color names usable in
    ``[name]…[/]`` markup tags."""
    for name, val in [
        ("COLOR_BUDGET_WARN", COLOR_BUDGET_WARN),
        ("COLOR_QUEUE_PRESSURE_OK", COLOR_QUEUE_PRESSURE_OK),
        ("COLOR_QUEUE_PRESSURE_WATCH", COLOR_QUEUE_PRESSURE_WATCH),
        ("COLOR_QUEUE_PRESSURE_CRIT", COLOR_QUEUE_PRESSURE_CRIT),
        ("COLOR_AHE_HIT", COLOR_AHE_HIT),
        ("COLOR_AHE_MISS", COLOR_AHE_MISS),
        ("COLOR_AHE_REFUTED", COLOR_AHE_REFUTED),
        ("COLOR_AHE_PENDING", COLOR_AHE_PENDING),
        ("COLOR_DIM", COLOR_DIM),
    ]:
        assert isinstance(val, str) and val, f"{name} must be a non-empty string"


def test_ahe_palette_matches_verdict_semantics():
    """AHE outcome colors borrow from the verdict palette where the semantics
    match (hit↔OK, miss↔TLE). Locks that mapping so the dashboard's color
    language stays internally consistent.
    """
    assert COLOR_AHE_HIT == VERDICT_PALETTE["OK"], (
        "AHE-hit should reuse the OK verdict color so the eye reads the "
        "same green for 'pass' across the dashboard."
    )
    assert COLOR_AHE_MISS == VERDICT_PALETTE["TLE"], (
        "AHE-miss should reuse the TLE verdict color so the eye reads the "
        "same red for 'pain' across the dashboard."
    )
    assert COLOR_AHE_REFUTED == VERDICT_PALETTE["VF"], (
        "AHE-refuted is a 'weird-success' family signal — pin it to the "
        "magenta family that VF/RV/RD/RT also live in."
    )


# ─────────────────────────────────────────────────────────────────────────── #
# (5) Queue-pressure thresholds map correctly                                  #
# ─────────────────────────────────────────────────────────────────────────── #


def test_queue_pressure_palette_tiers():
    """OK/watch/crit tiers should be distinct color names. Symmetric with
    the deviation-factor thresholds enforced in ``_queue_pressure_color``.
    """
    assert COLOR_QUEUE_PRESSURE_OK != COLOR_QUEUE_PRESSURE_WATCH
    assert COLOR_QUEUE_PRESSURE_WATCH != COLOR_QUEUE_PRESSURE_CRIT
    assert COLOR_QUEUE_PRESSURE_OK != COLOR_QUEUE_PRESSURE_CRIT
