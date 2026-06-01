"""Single source of color truth for the autobench-pulse dashboard.

Background: pulse v2 grew organically — each new widget hand-picked colors
from rich/textual's named palette. Verdict-anchored signals (FailureCodeSidebar
verdict text, VerdictHistogram bars, IterationProgressPanel dots) collided
with auxiliary signals (the budget-warning ribbon used the same yellow as
WA verdicts). With 5+ accent colors and no semantic grouping, the dashboard
read as noise.

This module fixes that by partitioning color into two layers:

1. **VERDICT_PALETTE** — the authoritative verdict → color map. Every widget
   that renders a verdict (or anything verdict-derived) MUST go through this
   dict. Keys are the string values from ``autobench.core.Verdict``.

2. **Auxiliary semantic colors** — ``COLOR_*`` constants for signals that
   are *not* verdict-related: budget warnings, queue-pressure tiers, AHE
   prediction outcomes, neutral muted text. These deliberately use colors
   that do NOT collide with the verdict palette (e.g. budget warnings are
   blue, not yellow, so they don't read as "WA verdict").

All colors are textual/rich-compatible color names — strings usable in
``[name]…[/]`` markup tags. No hex literals, no ``Color()`` objects, no
RGB tuples — so the palette degrades gracefully on 256-color and 16-color
terminals.

Naming/family decisions, briefly:

  * OK   → green (universal "pass")
  * WA   → yellow (wrong-but-ran; visible, not alarming)
  * CE   → orange1 (compile error; harder fail than WA, distinct from RE)
  * RE   → orange1 (runtime error; sibling of CE — both are "broken code")
  * TLE  → red (resource exhaustion; user-facing pain)
  * MLE  → red (resource exhaustion; sibling of TLE)
  * VF   → magenta (visual-fail — shader-specific; doesn't fit the OK/WA scale)
  * RV   → magenta (refactor verified; the "weird success" family)
  * RD   → magenta (refactor drift; AST changes beyond declared refactor)
  * RT   → magenta (refactor test fail; test suite regression)

The CE/RE choice of ``orange1`` is the only non-primary-rich-color name —
``orange1`` is in the 256-color palette, supported by rich/textual since 9.x.
On a true-color terminal it renders as ~#ff8700 — distinctly warmer than the
yellow WA tag and distinctly cooler than the red TLE/MLE pair, so the failure
spectrum reads (left → right):

    OK → WA (yellow) → CE/RE (orange) → TLE/MLE (red)

Refactor verdicts (RV/RD/RT) cluster in magenta — visually orthogonal to
the OK/WA/CE/RE/TLE/MLE pipeline because they answer a different question
("did the refactor preserve semantics?" vs "did the code run?").
"""

from __future__ import annotations

from typing import Final

# ─────────────────────────────────────────────────────────────────────────── #
# Verdict-anchored palette                                                    #
# ─────────────────────────────────────────────────────────────────────────── #

# Every widget that renders a verdict, a verdict-derived state, or a status
# tied to verdict semantics MUST go through this dict. Keys match the string
# values of ``autobench.core.Verdict``.
VERDICT_PALETTE: Final[dict[str, str]] = {
    "OK":  "green",
    "WA":  "yellow",
    "CE":  "orange1",
    "RE":  "orange1",
    "TLE": "red",
    "MLE": "red",
    "VF":  "magenta",
    "RV":  "magenta",
    "RD":  "magenta",
    "RT":  "magenta",
}


def verdict_color(verdict: str, default: str = "white") -> str:
    """Return the rich-compatible color name for ``verdict``.

    Centralised so widgets never need to know about the dict's name or the
    fallback color. Unknown verdicts return ``default`` (white = neutral on
    any background).
    """
    return VERDICT_PALETTE.get(verdict, default)


# ─────────────────────────────────────────────────────────────────────────── #
# Auxiliary semantic colors (NOT verdict-related)                             #
# ─────────────────────────────────────────────────────────────────────────── #

# Budget / cost warnings. Deliberately *blue*, not yellow — yellow is reserved
# for WA verdicts and queue-pressure watch. Budget warnings are an
# operator-facing signal about spend, not about code quality.
COLOR_BUDGET_WARN: Final[str] = "blue"

# Queue-pressure tiers (worker.queue_pressure.v1 deviation_factor).
#   dev < 0.25  → normal (cyan via COLOR_QUEUE_PRESSURE_OK below)
#   0.25 ≤ dev < 0.50 → watch (yellow — semantically "this needs attention")
#   dev ≥ 0.50  → critical (red — "do something now")
#
# Yellow doubles as WA verdict color, but the contexts are spatially
# disjoint (top-bar QueuePressureBar vs in-tree verdict tags) so the
# collision doesn't degrade scanability. Documented for future readers.
COLOR_QUEUE_PRESSURE_OK: Final[str] = "cyan"
COLOR_QUEUE_PRESSURE_WATCH: Final[str] = "yellow"
COLOR_QUEUE_PRESSURE_CRIT: Final[str] = "red"

# AHE prediction lifecycle outcome colors.
#   hit      → green   (confirmed prediction; aligns with OK verdict semantics)
#   miss     → red     (refuted on verify; aligns with TLE/MLE pain semantics)
#   refuted  → magenta (live-refuted via watermark crash; the "weird" outcome)
#   pending  → cyan    (in-flight; matches QUEUE_PRESSURE_OK — neutral attention)
COLOR_AHE_HIT: Final[str] = "green"
COLOR_AHE_MISS: Final[str] = "red"
COLOR_AHE_REFUTED: Final[str] = "magenta"
COLOR_AHE_PENDING: Final[str] = "cyan"

# Worker-latency tiers. The 60s+ timeout bucket should read as the same kind
# of pain as TLE — they're the same underlying failure mode at different
# observation points. So COLOR_LATENCY_TIMEOUT == VERDICT_PALETTE["TLE"].
COLOR_LATENCY_NORMAL: Final[str] = "cyan"
COLOR_LATENCY_LONG_TAIL: Final[str] = "yellow"
COLOR_LATENCY_TIMEOUT: Final[str] = "red"

# Neutral muted color. Used everywhere we want text to recede (placeholders,
# secondary annotations, empty-state messages).
COLOR_DIM: Final[str] = "dim"

# Accent / headline color for the dashboard's primary identifiers (session
# IDs, iteration headers). Distinct from any verdict so it never collides
# with verdict semantics.
COLOR_ACCENT: Final[str] = "cyan"


__all__ = [
    "VERDICT_PALETTE",
    "verdict_color",
    "COLOR_BUDGET_WARN",
    "COLOR_QUEUE_PRESSURE_OK",
    "COLOR_QUEUE_PRESSURE_WATCH",
    "COLOR_QUEUE_PRESSURE_CRIT",
    "COLOR_AHE_HIT",
    "COLOR_AHE_MISS",
    "COLOR_AHE_REFUTED",
    "COLOR_AHE_PENDING",
    "COLOR_LATENCY_NORMAL",
    "COLOR_LATENCY_LONG_TAIL",
    "COLOR_LATENCY_TIMEOUT",
    "COLOR_DIM",
    "COLOR_ACCENT",
]
