"""Custom widgets for autobench-pulse v2.

All widgets are pure-display. They consume reactive props set by the App from
state computed in ``pulse_app.state``. They never mutate state.

If ``textual-plotext`` is missing, the chart widgets degrade to ASCII-text
placeholders instead of crashing — see §7.11 / pragmatic guardrails.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

from textual.app import ComposeResult
from textual.containers import Container
from textual.reactive import reactive
from textual.widgets import ProgressBar, Sparkline, Static, Tree

# Single source of color truth — every verdict / semantic state color goes
# through palette.py. See palette.py for the rationale & color choices.
from pulse_app.palette import (
    COLOR_ACCENT,
    COLOR_AHE_HIT,
    COLOR_AHE_MISS,
    COLOR_AHE_PENDING,
    COLOR_AHE_REFUTED,
    COLOR_BUDGET_WARN,
    COLOR_DIM,
    COLOR_LATENCY_LONG_TAIL,
    COLOR_LATENCY_NORMAL,
    COLOR_LATENCY_TIMEOUT,
    COLOR_QUEUE_PRESSURE_CRIT,
    COLOR_QUEUE_PRESSURE_OK,
    COLOR_QUEUE_PRESSURE_WATCH,
    VERDICT_PALETTE,
    verdict_color,
)

# Optional dep — degrade gracefully -------------------------------------------
try:
    from textual_plotext import PlotextPlot  # type: ignore

    HAS_PLOTEXT = True
except Exception:  # pragma: no cover - import guard
    HAS_PLOTEXT = False

    class PlotextPlot(Static):  # type: ignore[no-redef]
        """Fallback placeholder when textual-plotext isn't installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__("[textual-plotext not installed]", *args, **kwargs)

        # mimic the bit of the real API we use
        @property
        def plt(self):  # noqa: D401
            return _NullPlt()

        def refresh(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            return super().refresh(*args, **kwargs)


class _NullPlt:
    def __getattr__(self, _name: str):
        def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        return _noop


# ---------------------------------------------------------------------------- #
# SessionTree                                                                  #
# ---------------------------------------------------------------------------- #


# Session-level lifecycle states (NOT the autobench case Verdict — those go
# through VERDICT_PALETTE in palette.py). These are dashboard-internal session
# labels: a session is "improved" or "regressed" relative to its prior score.
# We anchor them to palette colors where the semantic match is exact:
#   improved → green (OK family)
#   regressed → red  (TLE/MLE family — "things got worse")
#   running   → COLOR_QUEUE_PRESSURE_WATCH yellow (in-flight, watch)
#   pending   → COLOR_DIM (placeholder)
#   complete  → COLOR_ACCENT cyan (terminal-but-neutral)
_VERDICT_COLOR = {
    "running": COLOR_QUEUE_PRESSURE_WATCH,
    "pending": COLOR_DIM,
    "improved": f"bold {VERDICT_PALETTE['OK']}",
    "regressed": f"bold {VERDICT_PALETTE['TLE']}",
    "flat": "white",
    "complete": COLOR_ACCENT,
}

_VERDICT_GLYPH = {
    "running": "·",
    "pending": "?",
    "improved": "↑",
    "regressed": "↓",
    "flat": "→",
    "complete": "✓",
}


class SessionTree(Tree):
    """Reactive tree of autobench sessions, color-coded by verdict.

    Per §7.5: implement a basic but functional view — one root per top-level
    session, child nodes per iteration. Lineage via ``parent_id`` is supported
    when present, otherwise sessions render flat.
    """

    BINDINGS = [
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("space", "toggle_node", "Toggle"),
        ("enter", "select_cursor", "Select"),
    ]

    def __init__(self, label: str = "autobench sessions", **kwargs: Any) -> None:
        super().__init__(label, **kwargs)
        self.show_root = True
        self._session_nodes: dict[str, Any] = {}

    def rebuild(self, sessions: list[Any]) -> None:
        """Wholesale rebuild — cheap for ~100 sessions, simpler than diffing.

        At >10k sessions we should swap to incremental updates; see §8.1.
        """
        self.clear()
        self._session_nodes.clear()
        # First pass: index by id for parent lookups
        by_id = {s.session_id: s for s in sessions}
        roots = [s for s in sessions if not s.parent_id or s.parent_id not in by_id]

        def add(parent_node, sess) -> None:
            label = self._format_session_label(sess)
            node = parent_node.add(label, data=sess, expand=True)
            self._session_nodes[sess.session_id] = node
            # iterations as leaves
            for iter_num in sorted(sess.iterations):
                it = sess.iterations[iter_num]
                node.add_leaf(self._format_iter_label(it), data=it)
            # nested children
            for child in sessions:
                if child.parent_id == sess.session_id:
                    add(node, child)

        for r in roots:
            add(self.root, r)
        self.root.expand_all()

    # ---- label formatting -------------------------------------------------
    @staticmethod
    def _format_session_label(sess) -> str:
        verdict = sess.verdict
        color = _VERDICT_COLOR.get(verdict, "white")
        glyph = _VERDICT_GLYPH.get(verdict, "·")
        score = sess.latest_score
        score_str = f"  score={score:.2f}" if score is not None else ""
        # nervous-bus-dq7l: $ removed — MiniMax bills by requests, not cost.
        # cost_usd is now always 0.0 from the producer side; we just stop
        # rendering it so the dashboard doesn't lie.
        cost_str = ""
        return (
            f"[{color}]{glyph}[/] "
            f"[bold cyan]{sess.session_id[:12]}[/] "
            f"[{color}]{verdict}[/]"
            f"{score_str}{cost_str}"
        )

    @staticmethod
    def _format_iter_label(it) -> str:
        score_str = ""
        if it.aggregate_score is not None:
            if it.prev_score is not None and it.score_delta is not None:
                # Score delta is verdict-derived semantics: positive delta = OK
                # family (improvement), negative = TLE/MLE family (regression).
                d_color = (
                    VERDICT_PALETTE["OK"]
                    if it.score_delta >= 0
                    else VERDICT_PALETTE["TLE"]
                )
                sign = "+" if it.score_delta >= 0 else ""
                score_str = (
                    f"  {it.prev_score:.2f}→{it.aggregate_score:.2f} "
                    f"[{d_color}]Δ{sign}{it.score_delta:.3f}[/]"
                )
            else:
                score_str = f"  score={it.aggregate_score:.2f}"
        cases_done = sum(1 for c in it.cases if c.get("verdict"))
        cases_total = len(it.cases)
        cases_str = f"  cases={cases_done}/{cases_total}" if cases_total else ""
        # "start" state == in-flight iteration → watch color (same family as
        # SessionTree's running label). Idle states are muted.
        status_color = (
            COLOR_QUEUE_PRESSURE_WATCH if it.status == "start" else COLOR_DIM
        )
        return (
            f"iter [{status_color}]{it.iteration}[/] "
            f"[dim]{it.status}[/]{score_str}{cases_str}"
        )


# ---------------------------------------------------------------------------- #
# Sparkline / gauge                                                            #
# ---------------------------------------------------------------------------- #


class ScoreSpark(Sparkline):
    """Score-over-iterations sparkline driven by ``data`` reactive."""

    DEFAULT_CSS = """
    ScoreSpark {
        height: 5;
        margin: 0 1;
    }
    """


class BurnGauge(ProgressBar):
    """$/min burn-rate gauge, gradient when supported.

    Reactive ``burn`` (float, $/min) and ``budget`` (float). ``watch_burn``
    converts to percentage and updates the progress bar.
    """

    burn: reactive[float] = reactive(0.0)
    budget: reactive[float] = reactive(1.0)

    def __init__(self, budget: float = 1.0, **kwargs: Any) -> None:
        super().__init__(total=100, show_eta=False, **kwargs)
        self.budget = budget

    def watch_burn(self, value: float) -> None:
        if self.budget <= 0:
            self.update(progress=0)
            return
        pct = max(0.0, min(100.0, (value / self.budget) * 100.0))
        self.update(progress=pct)


# ---------------------------------------------------------------------------- #
# IterationProgressPanel — nervous-bus-4cw9                                    #
# ---------------------------------------------------------------------------- #


# IterationProgressPanel per-verdict dots. Derived from VERDICT_PALETTE — OK
# and WA bolded to make the most-common verdicts pop, everything else inherits
# the palette color directly. Single source of truth: palette.py.
_PROGRESS_VERDICT_COLOR = {
    "OK": f"bold {VERDICT_PALETTE['OK']}",
    "WA": f"bold {VERDICT_PALETTE['WA']}",
    "TLE": VERDICT_PALETTE["TLE"],
    "RE": VERDICT_PALETTE["RE"],
    "CE": VERDICT_PALETTE["CE"],
    "MLE": VERDICT_PALETTE["MLE"],
    "VF": VERDICT_PALETTE["VF"],
    "RV": VERDICT_PALETTE["RV"],
    "RD": VERDICT_PALETTE["RD"],
    "RT": VERDICT_PALETTE["RT"],
}
_PROGRESS_BAR_WIDTH = 20


def _format_duration(seconds: float) -> str:
    """Human-friendly duration: ``8m24s`` / ``42s`` / ``1h03m`` / ``-``."""
    if seconds is None:
        return "-"
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "-"
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    h = s // 3600
    rem = s % 3600
    return f"{h}h{rem // 60:02d}m"


def _format_abs_clock(epoch_seconds: float) -> str:
    """HH:MM in 24-hour local time."""
    try:
        return time.strftime("%H:%M", time.localtime(float(epoch_seconds)))
    except (TypeError, ValueError, OSError):
        return "--:--"


def _build_unicode_bar(done: int, total: int, width: int = _PROGRESS_BAR_WIDTH) -> str:
    """Block-character progress bar — matches the §7.5.5 unicode aesthetic.

    Uses the same ``█`` filled / ``░`` empty pattern as Textual's
    ProgressBar's text fallback so the panel looks at home next to BurnGauge.
    """
    if total <= 0:
        return "░" * width
    frac = max(0.0, min(1.0, done / float(total)))
    filled = int(round(frac * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


class IterationProgressPanel(Static):
    """Top-of-pulse "where are we" panel for the current RSI iteration.

    Tracks the in-flight iteration: cases done / total, rolling per-case
    average latency, ETA. Resets on each ``autobench.iteration.v1`` start
    event (state layer drives the reset; the widget just re-renders).

    Reactive ``progress`` is a snapshot dict produced by
    ``PulseState.iteration_progress`` — never holds a reference to mutating
    state, per §7.5.5 / the "render-tick fans out reactives" pattern.
    """

    DEFAULT_CSS = """
    IterationProgressPanel {
        height: auto;
        min-height: 3;
        max-height: 7;
        padding: 0 1;
        border: tall $primary 70%;
        background: $surface;
    }
    """

    progress: reactive[Optional[dict[str, Any]]] = reactive(None)
    iter_overhead_s: reactive[float] = reactive(15.0)

    def __init__(
        self,
        *args: Any,
        iter_overhead_s: float = 15.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.iter_overhead_s = iter_overhead_s
        self.update(self._render_idle("Awaiting iteration data…"))

    # ------------------------------------------------------------------ #
    def watch_progress(self, value: Optional[dict[str, Any]]) -> None:
        if value is None:
            self.update(self._render_idle("Awaiting iteration data…"))
            return
        self.update(self._render_panel(value))

    # ------------------------------------------------------------------ #
    def _render_idle(self, msg: str) -> str:
        return f"[dim]{msg}[/]"

    def _render_panel(self, p: dict[str, Any]) -> str:
        iter_num = int(p.get("iteration", 0))
        total_iters = p.get("total_iterations")
        iter_label = f"{iter_num}" if iter_num is not None else "?"
        if total_iters:
            header = f"Iteration {iter_label} / {int(total_iters)}"
        else:
            header = f"Iteration {iter_label}"

        status = p.get("status") or "start"
        if status == "complete":
            # Idle between iterations: muted message until next iter_start.
            return self._render_complete(p, iter_num)

        cases_done = int(p.get("cases_done", 0) or 0)
        cases_total = max(1, int(p.get("cases_total", 1) or 1))
        bar = _build_unicode_bar(cases_done, cases_total)
        pct = int(round((cases_done / cases_total) * 100))

        verdicts = p.get("verdict_counts") or {}
        verdict_line = self._format_verdict_dots(verdicts)

        elapsed = float(p.get("elapsed_s", 0.0) or 0.0)
        avg_ms = p.get("avg_case_latency_ms")
        if avg_ms:
            per_case_str = _format_duration(avg_ms / 1000.0)
        else:
            per_case_str = "—"

        eta_s = p.get("eta_s")
        if eta_s is None:
            eta_line = "[dim]ETA: computing…[/]"
        else:
            eta_clock = _format_abs_clock(time.time() + float(eta_s))
            eta_line = (
                f"[dim]ETA:[/] [italic]~{_format_duration(eta_s)} remaining[/] "
                f"[dim](~{eta_clock} absolute)[/]"
            )

        # ── Compose ────────────────────────────────────────────────────
        lines = [
            f"[bold cyan]{header}[/]",
            (
                f"[bold]Cases:[/]   [{VERDICT_PALETTE['OK']}]{bar}[/]  "
                f"[bold]{cases_done}[/] / {cases_total}    "
                f"[dim]{pct}%[/]"
            ),
            (
                f"[bold]Verdicts:[/] {verdict_line}  "
                f"[dim]({cases_done} total)[/]"
            ),
            (
                f"[bold]Elapsed:[/] {_format_duration(elapsed)}   "
                f"[bold]Per-case avg:[/] {per_case_str}"
            ),
            eta_line,
        ]
        return "\n".join(lines)

    def _render_complete(self, p: dict[str, Any], iter_num: int) -> str:
        """Collapse a completed iteration to a single summary row.

        nervous-bus-yn9v fix 1+2: pre-fix this widget wasted 6 rows showing
        "Iteration N complete / Cases: 0/20 100% / Awaiting iter N+1…".
        The 0/20 is wrong when case.result events are filed under a sibling
        iteration index (a known producer convention quirk). Prefer the
        authoritative ``iteration.summary.v1`` rollup carried in
        ``history_snapshot`` so the panel shows real numbers.
        """
        # Prefer the iteration_history rollup — it's authoritative.
        snap = p.get("history_snapshot") or {}
        live_verdicts = p.get("verdict_counts") or {}
        hist_verdicts = snap.get("verdict_counts") or {}
        verdict_counts = hist_verdicts if hist_verdicts else live_verdicts
        num_cases = int(snap.get("num_cases") or 0)
        if num_cases <= 0:
            cases_done = int(p.get("cases_done", 0) or 0)
            cases_total = int(p.get("cases_total", 0) or 0) or cases_done
            num_cases = cases_total or cases_done
        score = snap.get("aggregate_score")
        if score is None:
            score = p.get("aggregate_score")
        try:
            score_str = f"{float(score):.3f}" if score is not None else "—"
        except (TypeError, ValueError):
            score_str = "—"
        # Compact verdict line: {OK:16, WA:2, CE:2} — palette-coloured.
        if verdict_counts:
            verdict_parts: list[str] = []
            for k, v in sorted(
                verdict_counts.items(), key=lambda kv: (-int(kv[1] or 0), kv[0])
            ):
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                color = VERDICT_PALETTE.get(k, "white")
                verdict_parts.append(f"[{color}]{k}:{n}[/]")
            verdict_str = "{" + ", ".join(verdict_parts) + "}" if verdict_parts else "[dim]—[/]"
        else:
            verdict_str = "[dim]—[/]"
        return (
            f"[bold cyan]Iter {iter_num}[/]  "
            f"[{VERDICT_PALETTE['OK']}]complete[/]  "
            f"[dim]·[/] [bold]{num_cases}/{num_cases}[/]  "
            f"[dim]·[/] score [bold]{score_str}[/]  "
            f"[dim]·[/] {verdict_str}"
        )

    def _format_verdict_dots(self, verdicts: dict[str, int]) -> str:
        """Tiny coloured ● dots, one per case, grouped by verdict.

        Keeps the line short by collapsing >12 of a verdict into ``●×N``.
        """
        if not verdicts:
            return "[dim]—[/]"
        parts: list[str] = []
        for verdict, count in sorted(verdicts.items(), key=lambda kv: (-kv[1], kv[0])):
            if count <= 0:
                continue
            color = _PROGRESS_VERDICT_COLOR.get(verdict, "white")
            if count <= 12:
                dots = "●" * count
                parts.append(f"[bold]{verdict}[/] [{color}]{dots}[/]")
            else:
                parts.append(f"[bold]{verdict}[/] [{color}]●×{count}[/]")
        return "  ".join(parts) if parts else "[dim]—[/]"


# ---------------------------------------------------------------------------- #
# PlotextPlot-based widgets                                                    #
# ---------------------------------------------------------------------------- #


class VerdictHistogram(PlotextPlot):
    """Bar chart of verdict counts. Refreshed at ≤2 Hz per §8.2.

    Bars are colored per-verdict via ``VERDICT_PALETTE`` so the chart shares
    the same color language as every other verdict-anchored widget in the
    dashboard. plotext's ``bar`` API doesn't natively per-bar-color a single
    series, so we render each bar as its own one-element series — this is
    cheap (≤10 verdicts) and gives us exact palette control.
    """

    counts: reactive[dict[str, int]] = reactive(dict)

    @staticmethod
    def color_for(verdict: str) -> str:
        """Return the palette color name a histogram bar would use for
        ``verdict``. Exposed for tests + sibling widgets that want to keep
        the chart-color and dot-color in sync."""
        return verdict_color(verdict)

    def watch_counts(self, value: dict[str, int]) -> None:
        if not HAS_PLOTEXT:
            return
        plt = self.plt
        plt.clear_figure()
        plt.clear_data()
        if not value:
            plt.title("verdicts (no data)")
            self.refresh()
            return
        keys = list(value.keys())
        vals = [value[k] for k in keys]
        # Per-bar color: each bar is its own one-element series, colored
        # from VERDICT_PALETTE. Falls back to plt.bar(...) if plotext doesn't
        # accept the color kwarg in this version.
        try:
            for k, v in zip(keys, vals):
                color = self.color_for(k)
                plt.bar([k], [v], orientation="vertical", color=color)
        except Exception:
            plt.bar(keys, vals, orientation="vertical")
        plt.title(f"verdicts (total={sum(vals)})")
        plt.xlabel("verdict")
        plt.ylabel("count")
        self.refresh()


class ParetoScatter(PlotextPlot):
    """Cost-vs-score scatter — multi-iter accumulator.

    nervous-bus-sm8n: ``points`` now carries one tuple per
    iteration.summary boundary (not per session), so a multi-iter RSI
    run renders as a real cloud. The frontier (non-dominated points)
    paints in COLOR_AHE_HIT — green = "this is the win line" — while
    dominated points render dimmed so the operator can scan past them.
    """

    points: reactive[list[tuple[float, float]]] = reactive(list)

    def watch_points(self, value: list[tuple[float, float]]) -> None:
        if not HAS_PLOTEXT:
            return
        plt = self.plt
        plt.clear_figure()
        plt.clear_data()
        if not value:
            plt.title("pareto: cost vs score (no data)")
            self.refresh()
            return
        frontier_set = {(round(p[0], 9), round(p[1], 9)) for p in _pareto_frontier(value)}
        front: list[tuple[float, float]] = []
        dim: list[tuple[float, float]] = []
        for p in value:
            key = (round(p[0], 9), round(p[1], 9))
            (front if key in frontier_set else dim).append(p)
        # Dominated points first so the frontier overlays on top.
        if dim:
            plt.scatter(
                [p[0] for p in dim],
                [p[1] for p in dim],
                marker="braille",
                color=COLOR_DIM,
            )
        if front:
            plt.scatter(
                [p[0] for p in front],
                [p[1] for p in front],
                marker="braille",
                color=COLOR_AHE_HIT,
            )
        plt.title(f"pareto: cost ($) vs score  ({len(front)} frontier / {len(dim)} dominated)")
        plt.xlabel("cost ($)")
        plt.ylabel("score")
        self.refresh()


def _pareto_frontier(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Maximize score, minimize cost. Returns the non-dominated subset."""
    if not points:
        return []
    sorted_by_cost = sorted(points, key=lambda p: (p[0], -p[1]))
    frontier: list[tuple[float, float]] = []
    best_score = float("-inf")
    for cost, score in sorted_by_cost:
        if score > best_score:
            frontier.append((cost, score))
            best_score = score
    return frontier


# ---------------------------------------------------------------------------- #
# CostRatePanel — running total cost + budget threshold lines                  #
# (bead nervous-bus-cewj)                                                      #
# ---------------------------------------------------------------------------- #


class CostTrajectoryPlot(PlotextPlot):
    """Cost-over-time line chart with 50/80/100% threshold horizontal lines.

    Reactive ``payload`` carries the full cost-state dict from
    ``PulseState.cost_summary()``. The plot uses hlines for the threshold cap
    references and a single line for the cumulative cost trajectory. When a
    threshold has fired, we overlay a colored marker at that x-coord.
    """

    payload: reactive[dict[str, Any]] = reactive(dict)

    def watch_payload(self, value: dict[str, Any]) -> None:
        if not HAS_PLOTEXT:
            return
        plt = self.plt
        plt.clear_figure()
        plt.clear_data()
        cap = float(value.get("max_cost_usd") or 1.0)
        xs, ys = value.get("trajectory") or ([], [])
        thresholds = value.get("thresholds_fired") or {}
        # Threshold horizontal lines — dashed, muted. plotext doesn't expose a
        # literal dashed-line marker, so we draw a sparse-marker horizontal
        # line which renders as a dashed visual against most palettes.
        # Threshold ladder: 50% = budget warn (blue), 80% = queue-pressure
        # watch tier (yellow), 100% = critical (red). The 50% line uses
        # COLOR_BUDGET_WARN deliberately — budget warnings live in their own
        # color family so they don't collide with verdict colors. The 80%
        # tier promotes to "watch" yellow, mirroring queue-pressure semantics.
        for frac, color in (
            (0.5, COLOR_BUDGET_WARN),
            (0.8, COLOR_QUEUE_PRESSURE_WATCH),
            (1.0, COLOR_QUEUE_PRESSURE_CRIT),
        ):
            cap_y = cap * frac
            # Need at least 2 x-points for a horizontal reference; fall back
            # to a synthetic [0, 1] domain when the trajectory is empty.
            ref_xs = xs if len(xs) >= 2 else [0.0, 1.0]
            ref_ys = [cap_y, cap_y]
            ref_xs2 = [ref_xs[0], ref_xs[-1]]
            try:
                plt.plot(ref_xs2, ref_ys, color=color, marker="braille")
            except Exception:
                pass
        # Cumulative cost trajectory — bright, prominent. Only plotted when
        # there's at least 2 samples; a single point isn't a line.
        if len(xs) >= 2:
            try:
                # Trajectory line: green == OK family, "we're still healthy".
                plt.plot(xs, ys, color=VERDICT_PALETTE["OK"], marker="braille")
            except Exception:
                pass
        # Fired-threshold markers — pulse via a single scatter point at the
        # x-coord where the threshold breach landed.
        if xs:
            x_last = xs[-1]
            for frac, (_ts, _iter_hint) in thresholds.items():
                # Fired-threshold scatter marker — mirrors the ladder above.
                if frac >= 1.0:
                    color = COLOR_QUEUE_PRESSURE_CRIT
                elif frac >= 0.8:
                    color = COLOR_QUEUE_PRESSURE_WATCH
                else:
                    color = COLOR_BUDGET_WARN
                try:
                    plt.scatter([x_last], [cap * float(frac)], color=color, marker="braille")
                except Exception:
                    pass
        plt.title(f"cost ($) — cap ${cap:.2f}")
        plt.xlabel("elapsed (s)")
        plt.ylabel("$")
        self.refresh()


class CostRatePanel(Container):
    """Composite cost + rate panel. Hosts a header line, a trajectory chart,
    and a warning ticker.

    Reactive ``payload`` is the dict from ``PulseState.cost_summary()``. We
    deliberately pass the entire snapshot in one go (rather than separate
    reactives) so the panel renders coherently — no half-updated state.
    """

    DEFAULT_CSS = """
    CostRatePanel {
        height: 17;
        border: tall $secondary;
        layout: vertical;
        padding: 0 1;
    }
    CostRatePanel #cost-header {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    CostRatePanel #cost-notional {
        height: 1;
    }
    CostRatePanel CostTrajectoryPlot {
        height: 1fr;
    }
    CostRatePanel #cost-warning {
        height: 1;
        color: $warning;
    }
    """

    payload: reactive[dict[str, Any]] = reactive(dict)

    def compose(self) -> ComposeResult:
        yield Static("Rate: -- / -- requests", id="cost-header")
        yield Static("[dim]notional cost: $0.0000 / $1.00[/]", id="cost-notional")
        yield CostTrajectoryPlot(id="cost-chart")
        yield Static("", id="cost-warning")

    def watch_payload(self, value: dict[str, Any]) -> None:
        """Fan the snapshot out to header / chart / warning sub-widgets."""
        if not value:
            return
        try:
            header = self.query_one("#cost-header", Static)
            header.update(self._format_header(value))
        except Exception:
            pass
        try:
            notional = self.query_one("#cost-notional", Static)
            notional.update(self._format_notional(value))
        except Exception:
            pass
        try:
            chart = self.query_one(CostTrajectoryPlot)
            chart.payload = value
        except Exception:
            pass
        try:
            warn = self.query_one("#cost-warning", Static)
            warn.update(self._format_warning(value))
        except Exception:
            pass

    # ---- formatting -------------------------------------------------------
    @staticmethod
    def _format_header(value: dict[str, Any]) -> str:
        """Primary readout: REQUESTS / cap. The MiniMax coding plan bills by
        requests-per-5h, so this is the real billable axis. Color tracks the
        request-utilization fraction (mirrors VerdictHistogram tinting)."""
        rate = value.get("rate") or {}
        mx = int(rate.get("max_requests") or 0)
        if mx > 0:
            cur = int(rate.get("current_count") or 0)
            win_s = float(rate.get("window_seconds") or 0.0)
            win_h = win_s / 3600.0 if win_s else 0.0
            if win_h >= 1.0:
                win_str = f"{win_h:.0f}h"
            else:
                win_str = f"{win_s:.0f}s"
            frac = cur / mx if mx > 0 else 0.0
            # Rate-utilization color ladder. Matches the cost-threshold ladder
            # so the operator sees a coherent "headroom" signal across the
            # whole panel: green (lots of headroom) → cyan (steady) →
            # yellow (watch) → red (over budget).
            if frac >= 1.0:
                color = f"bold {COLOR_QUEUE_PRESSURE_CRIT}"
            elif frac >= 0.8:
                color = f"bold {COLOR_QUEUE_PRESSURE_WATCH}"
            elif frac >= 0.5:
                color = f"bold {COLOR_QUEUE_PRESSURE_OK}"
            else:
                color = f"bold {VERDICT_PALETTE['OK']}"
            return f"[{color}]Rate: {cur} / {mx} requests ({win_str})[/]"
        return "[dim]Rate: -- / -- requests[/]"

    @staticmethod
    def _format_notional(value: dict[str, Any]) -> str:
        """Secondary readout: dollar total, labeled "notional". The MiniMax
        coding plan bills flat-rate per requests-per-5h, NOT per token — so the
        dollar figure is a derived estimate, not real money. Rendered dim so it
        never competes with the requests headline."""
        total = float(value.get("total_usd") or 0.0)
        cap = float(value.get("max_cost_usd") or 1.0)
        return f"[dim]notional cost: ${total:.4f} / ${cap:.2f}[/]"

    @staticmethod
    def _format_warning(value: dict[str, Any]) -> str:
        fired = value.get("thresholds_fired") or {}
        if not fired:
            return ""
        # Highest threshold fired wins the message.
        top = max(fired)
        _ts, iter_hint = fired[top]
        pct = int(round(top * 100))
        # Budget-warning ribbon. The hard-cap (≥100%) tier escalates to red
        # — that's verdict-TLE territory (resource exhaustion). The soft
        # tiers (50%/80%) live in COLOR_BUDGET_WARN (blue), DELIBERATELY
        # NOT yellow: yellow is reserved for WA verdicts + queue-pressure
        # watch. A blue budget warning never collides with a verdict tag.
        if top >= 1.0:
            icon = f"[bold {COLOR_QUEUE_PRESSURE_CRIT}]⚠[/]"
            tag = f"[bold {COLOR_QUEUE_PRESSURE_CRIT}]HARD CAP[/]"
        else:
            icon = f"[{COLOR_BUDGET_WARN}]⚠[/]"
            tag = f"[{COLOR_BUDGET_WARN}]{pct}% threshold[/]"
        return f"{icon} budget warning fired at {tag} (chart-x {iter_hint})"
# AHE Prediction Tracker                                                       #
# ---------------------------------------------------------------------------- #


# Status → (glyph, rich style). Mirrors _VERDICT_COLOR / _VERDICT_GLYPH above so
# the dashboard is visually coherent: confirmed reads like an "improved" verdict,
# refuted/refuted_live like "regressed", partial like "flat".
_PREDICTION_GLYPH = {
    "pending": "◯",
    "refuted_live": "⚠",
    "confirmed": "✓",
    "partial": "◐",
    "refuted": "✗",
}

# AHE prediction lifecycle colors — routed through palette.py COLOR_AHE_*.
#   pending      → muted (waiting on cases)
#   refuted_live → bold watch yellow (refutation came in *during* the run —
#                  loud "look at me" without escalating to verdict-fail red)
#   confirmed    → COLOR_AHE_HIT (verified true; OK family)
#   partial      → COLOR_AHE_REFUTED magenta (weird success — partial credit)
#   refuted      → COLOR_AHE_MISS (verified false; TLE/MLE pain family)
_PREDICTION_COLOR = {
    "pending": COLOR_DIM,
    "refuted_live": f"bold {COLOR_QUEUE_PRESSURE_WATCH}",
    "confirmed": f"bold {COLOR_AHE_HIT}",
    "partial": COLOR_AHE_REFUTED,
    "refuted": f"bold {COLOR_AHE_MISS}",
}

_PREDICTION_LABEL = {
    "pending": "pending",
    "refuted_live": "REFUTED (live)",
    "confirmed": "confirmed",
    "partial": "partial",
    "refuted": "refuted",
}

# Maximum prediction rows displayed; older predictions still live in state.
PREDICTION_TRACKER_LIMIT = 5

# Truncate long rationale / refutation reason text to keep rows scannable.
_MAX_REASON_LEN = 60


def _format_delta(value: Optional[float], places: int = 3) -> str:
    """Format a signed delta to ``places`` decimal places. Includes sign."""
    if value is None:
        return "  n/a  "
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{places}f}"


def _truncate(text: str, limit: int = _MAX_REASON_LEN) -> str:
    """Trim long text, append an ellipsis when chopped. Preserves newlines as spaces."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


class AHEPredictionTracker(Static):
    """Live lifecycle viewer for AHE predictions.

    Reactive ``records`` is a list of ``PredictionRecord`` dataclasses (or any
    object exposing the same attribute surface) ordered NEWEST-FIRST. The
    widget caps display at ``PREDICTION_TRACKER_LIMIT`` and renders each
    prediction as a two-line block: header (iter, confidence, predicted delta)
    + status line coloured by lifecycle stage.

    State machine reflected in the rendering:

    ===================  =====  ====================================
    PredictionRecord     glyph  meaning
    -------------------  -----  ------------------------------------
    pending              ◯     emitted, not yet verified or refuted
    refuted_live         ⚠     live partial-refutation arrived
    confirmed            ✓     verified — outcome_label="confirmed"
    partial              ◐     verified — outcome_label="partial"
    refuted              ✗     verified — outcome_label="refuted"
    ===================  =====  ====================================

    The widget intentionally re-renders on every reactive assignment — it's
    cheap (≤5 rows, plain markup) and avoids the 10 Hz tick reaching in.
    """

    records: reactive[list[Any]] = reactive(list, always_update=True)
    case_progress: reactive[dict[tuple[str, int], tuple[int, int]]] = reactive(
        dict, always_update=True
    )

    DEFAULT_CSS = """
    AHEPredictionTracker {
        height: auto;
        min-height: 5;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._empty_text = "[dim]no predictions yet — waiting for improver to commit a falsifiable claim…[/]"

    # ----------------------------------------------------- reactive watchers
    def watch_records(self, _value: list[Any]) -> None:
        self.update(self._render_content())

    def watch_case_progress(
        self, _value: dict[tuple[str, int], tuple[int, int]]
    ) -> None:
        self.update(self._render_content())

    # ------------------------------------------------------------- render --
    def _render_content(self) -> str:
        rows = list(self.records or [])[:PREDICTION_TRACKER_LIMIT]
        if not rows:
            return self._empty_text
        lines: list[str] = []
        for rec in rows:
            lines.extend(self._render_one(rec))
            # blank spacer between predictions (mirrors the spec mock)
            lines.append("")
        # drop the final spacer for visual tightness
        if lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    def _render_one(self, rec: Any) -> list[str]:
        status = getattr(rec, "status", "pending")
        glyph = _PREDICTION_GLYPH.get(status, "·")
        color = _PREDICTION_COLOR.get(status, "white")
        label = _PREDICTION_LABEL.get(status, status)

        iteration = int(getattr(rec, "iteration", 0))
        confidence = float(getattr(rec, "confidence", 0.0))
        predicted = _format_delta(float(getattr(rec, "predicted_score_delta", 0.0)))

        # Header line — bold cyan iteration arrow, dim confidence, predicted delta
        # right-padded for vertical alignment across rows.
        header = (
            f"[bold cyan]iter {iteration} → {iteration + 1}[/]   "
            f"[dim]confidence[/] [bold]{confidence:.2f}[/]  "
            f"[dim]predicted[/] [bold]{predicted}[/]"
        )

        # Status line varies per state.
        if status == "pending":
            sid = str(getattr(rec, "session_id", ""))
            done, total = self.case_progress.get((sid, iteration), (0, 0))
            if total > 0:
                progress = f"{done}/{total} cases done"
            else:
                progress = "waiting for iter cases"
            status_line = (
                f"  [{color}]{glyph}[/] [{color}]{label}[/] [dim]— {progress}[/]"
            )
        elif status == "refuted_live":
            reason = _truncate(getattr(rec, "refutation_reason", ""))
            # Refutation reason inherits the watch tier — same yellow family
            # as the status glyph, so the eye reads the row as one signal.
            status_line = (
                f"  [{color}]{glyph} {label}[/][dim]:[/] "
                f"[{COLOR_QUEUE_PRESSURE_WATCH}]{reason}[/]"
            )
        else:
            actual = getattr(rec, "actual_score_delta", None)
            error = getattr(rec, "score_delta_error", None)
            calibration = getattr(rec, "confidence_calibration", None)
            actual_s = _format_delta(actual)
            error_s = (
                f"{error:.3f}" if isinstance(error, (int, float)) else "n/a"
            )
            calib_s = (
                f"{calibration:.2f}"
                if isinstance(calibration, (int, float))
                else "n/a"
            )
            status_line = (
                f"  [{color}]{glyph} {label}[/][dim]:[/] "
                f"[dim]actual[/] [bold]{actual_s}[/]  "
                f"[dim]error[/] [bold]{error_s}[/]  "
                f"[dim]calibration[/] [bold]{calib_s}[/]"
            )

        return [header, status_line]


# ---------------------------------------------------------------------------- #
# Header subtitle + help                                                       #
# ---------------------------------------------------------------------------- #


# ---------------------------------------------------------------------------- #
# WorkerLatencyHistogram                                                       #
# ---------------------------------------------------------------------------- #


# Bucket edges in milliseconds. Designed to make the long tail visible —
# fine-grained at the typical-latency end (≤20s) and coarser approaching the
# 60s worker timeout. The final 60s+ bucket captures timeout territory.
WORKER_LATENCY_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-5s", 0.0, 5_000.0),
    ("5-10s", 5_000.0, 10_000.0),
    ("10-15s", 10_000.0, 15_000.0),
    ("15-20s", 15_000.0, 20_000.0),
    ("20-30s", 20_000.0, 30_000.0),
    ("30-45s", 30_000.0, 45_000.0),
    ("45-60s", 45_000.0, 60_000.0),
    ("60s+", 60_000.0, float("inf")),
)

# Latency-bucket colors — derived from the auxiliary latency-tier palette.
# The 60s+ timeout bucket reads as the same kind of pain as a TLE verdict
# (it IS a TLE in the making), so COLOR_LATENCY_TIMEOUT == VERDICT_PALETTE["TLE"].
_WORKER_BAR_COLOR = {
    "0-5s": COLOR_LATENCY_NORMAL,
    "5-10s": COLOR_LATENCY_NORMAL,
    "10-15s": COLOR_LATENCY_NORMAL,
    "15-20s": COLOR_LATENCY_NORMAL,
    "20-30s": COLOR_LATENCY_NORMAL,
    "30-45s": COLOR_LATENCY_NORMAL,
    "45-60s": COLOR_LATENCY_LONG_TAIL,
    "60s+": f"bold {COLOR_LATENCY_TIMEOUT}",
}


def _bucket_for_latency_ms(latency_ms: float) -> str:
    """Return the label of the bucket containing ``latency_ms``.

    Buckets are right-open ``[lo, hi)`` so a value sitting exactly on the 60s
    edge lands in the timeout bucket — which is the conservative read.
    """
    for label, lo, hi in WORKER_LATENCY_BUCKETS:
        if lo <= latency_ms < hi:
            return label
    return WORKER_LATENCY_BUCKETS[-1][0]


def compute_worker_latency_buckets(
    latencies_ms: list[float],
) -> dict[str, int]:
    """Bucket a list of latency_ms values. Pure function — easy to unit-test."""
    out: dict[str, int] = {label: 0 for label, _, _ in WORKER_LATENCY_BUCKETS}
    for v in latencies_ms:
        out[_bucket_for_latency_ms(float(v))] += 1
    return out


def compute_worker_latency_stats(
    latencies_ms: list[float],
) -> dict[str, float]:
    """Return ``{count, mean, p50, p95}`` over ``latencies_ms`` (in ms).

    ``count == 0`` ⇒ all stats are 0. ``count == 1`` ⇒ p50 == p95 == mean ==
    the single value. We use ``statistics.quantiles`` with ``n=20`` so p95 maps
    to the 19th cutpoint, which matches the textbook nearest-rank definition
    closely enough for dashboard purposes.
    """
    import statistics

    n = len(latencies_ms)
    if n == 0:
        return {"count": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0}
    vals = sorted(float(v) for v in latencies_ms)
    mean = statistics.fmean(vals)
    p50 = statistics.median(vals)
    if n == 1:
        p95 = vals[0]
    else:
        # nearest-rank p95: ceil(0.95 * n) - 1
        import math
        idx = max(0, min(n - 1, int(math.ceil(0.95 * n)) - 1))
        p95 = vals[idx]
    return {"count": float(n), "mean": mean, "p50": p50, "p95": p95}


class WorkerLatencyHistogram(Static):
    """Bucketed histogram of worker-agent call latencies.

    Subscribes (via the App's render tick) to ``autobench.worker.v1`` derived
    state. Renders a unicode bar chart with explicit timeout-tail buckets so
    operators can spot tail-pathology at a glance. Stats line above the chart
    summarises count / mean / p50 / p95.

    Why unicode and not textual-plotext? plotext's horizontal-bar API doesn't
    give us per-bar colour control (which is the whole point of this widget —
    the 45-60s and 60s+ bars must read as "warning" and "danger" respectively).
    Solid block bars give us pixel-tight visual control AND scale cleanly to
    arbitrary widget widths.
    """

    DEFAULT_CSS = """
    WorkerLatencyHistogram {
        height: auto;
        min-height: 13;
        border: tall $secondary;
        padding: 0 1;
    }
    """

    latencies_ms: reactive[list[float]] = reactive(list)
    bar_width: reactive[int] = reactive(20)

    # Max bar fill width in characters. Kept modest so the label + count
    # annotation still fit on a 60-col-ish layout. Bumpable from the App.
    BAR_FILL_CHARS: int = 24

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=True, **kwargs)
        self.border_title = "Worker Latency"

    def watch_latencies_ms(self, value: list[float]) -> None:
        self.update(self._render_histogram_markup(value))

    # ------------------------------------------------------------------ #
    def _render_histogram_markup(self, latencies: list[float]) -> str:
        n = len(latencies)
        if n == 0:
            self.border_title = "Worker Latency"
            return "[dim]no worker calls yet[/]"

        buckets = compute_worker_latency_buckets(latencies)
        stats = compute_worker_latency_stats(latencies)
        self.border_title = f"Worker Latency (n={n})"

        # Stats line — muted compared to the bars. One decimal place in
        # seconds: consistent with how mean/p50/p95 read in autobench logs.
        stats_line = (
            f"[dim]mean {stats['mean']/1000:.1f}s  "
            f"p50 {stats['p50']/1000:.1f}s  "
            f"p95 {stats['p95']/1000:.1f}s[/]"
        )

        # Degrade rule: single data point — show the stats line but suppress
        # the bar chart, which would otherwise plot a single 100%-full bar
        # and visually mislead.
        if n < 2:
            sole = latencies[0]
            bucket = _bucket_for_latency_ms(float(sole))
            return (
                f"{stats_line}\n\n"
                f"[dim]only 1 sample ({sole/1000:.1f}s, {bucket}); "
                f"chart unlocks at n>=2[/]"
            )

        # Bar chart — scale each bar to ``BAR_FILL_CHARS`` based on the max
        # bucket count so the largest bucket fills the row.
        max_count = max(buckets.values()) or 1
        rows: list[str] = []
        for label, _lo, _hi in WORKER_LATENCY_BUCKETS:
            count = buckets[label]
            fill = int(round(count / max_count * self.BAR_FILL_CHARS))
            bar = "█" * fill + "░" * (self.BAR_FILL_CHARS - fill)
            color = _WORKER_BAR_COLOR[label]
            annotation = ""
            if label == "45-60s":
                annotation = f"  [{COLOR_LATENCY_LONG_TAIL}]← long tail[/]"
            elif label == "60s+":
                annotation = f"  [bold {COLOR_LATENCY_TIMEOUT}]← timeout[/]"
            rows.append(
                f"[bold]{label:6s}[/] [{color}]{bar}[/] "
                f"[dim]{count:>3d}[/]{annotation}"
            )
        return f"{stats_line}\n\n" + "\n".join(rows)
# DivergenceHighlights                                                         #
# ---------------------------------------------------------------------------- #


# Max diff lines to render in the ribbon. Ribbon is at most 4 rows tall
# (border + headline + diff excerpt) — anything longer is truncated.
_DIFF_EXCERPT_MAX_LINES = 3
# Cap a single rendered diff line so a 200-char system_prompt line doesn't blow
# the layout. Truncation is purely visual; full diff lives on the bus.
_DIFF_LINE_MAX_CHARS = 96


def _summarise_unified_diff(diff_text: str) -> tuple[int, int, list[str]]:
    """Count +/- lines and return up to ``_DIFF_EXCERPT_MAX_LINES`` body lines.

    Skips unified-diff metadata (``---``, ``+++``, ``@@``). Returns
    ``(plus_count, minus_count, sample_lines)`` where ``sample_lines`` is the
    first N change lines preserved verbatim (still prefixed with +/-).
    """
    if not diff_text:
        return (0, 0, [])
    plus = 0
    minus = 0
    sample: list[str] = []
    for raw in diff_text.splitlines():
        if not raw:
            continue
        if raw.startswith("---") or raw.startswith("+++") or raw.startswith("@@"):
            continue
        if raw.startswith("+"):
            plus += 1
        elif raw.startswith("-"):
            minus += 1
        if len(sample) < _DIFF_EXCERPT_MAX_LINES and (
            raw.startswith("+") or raw.startswith("-")
        ):
            sample.append(raw)
    return (plus, minus, sample)


def _truncate_diff_line(line: str) -> str:
    if len(line) <= _DIFF_LINE_MAX_CHARS:
        return line
    return line[: _DIFF_LINE_MAX_CHARS - 1] + "…"


def _render_diff_line(line: str) -> str:
    """Colourise one diff line for Textual markup.

    + lines → success (green), - lines → danger (red), context → dim. We
    escape ``[`` so Rich/Textual markup in the diff body doesn't break the
    render (a system_prompt with ``[INST]`` would otherwise be interpreted
    as a style tag).
    """
    truncated = _truncate_diff_line(line)
    escaped = truncated.replace("[", r"\[")
    # Diff +/- lines are conceptually OK/TLE — additions read as "good",
    # deletions as "removed/breaking". Routed through VERDICT_PALETTE so the
    # diff colors stay coherent with the rest of the dashboard.
    if truncated.startswith("+"):
        return f"[{VERDICT_PALETTE['OK']}]{escaped}[/]"
    if truncated.startswith("-"):
        return f"[{VERDICT_PALETTE['TLE']}]{escaped}[/]"
    return f"[{COLOR_DIM}]{escaped}[/]"


def _format_budget_changes(budget: dict[str, dict[str, Any]]) -> str:
    """Render ``budget_changes`` as ``key before → after, key2 ...``."""
    if not budget:
        return ""
    parts: list[str] = []
    for key, ba in budget.items():
        if not isinstance(ba, dict):
            continue
        b = ba.get("before")
        a = ba.get("after")
        parts.append(f"{key} {b} → {a}")
    return ", ".join(parts)


def _delta_diff_headline(evt: Any) -> str:
    """One-line headline summarising a delta.diff event.

    ``evt`` is a ``DivergenceEvent`` (kind=delta_diff). Caller has already
    decided this is a non-trivial change (``no_change=false``).
    """
    bits: list[str] = []
    sp_p, sp_m, _ = _summarise_unified_diff(evt.system_prompt_diff)
    if sp_p or sp_m:
        bits.append(f"system_prompt +{sp_p} -{sp_m} lines")
    ts_p, ts_m, _ = _summarise_unified_diff(evt.tool_surface_diff)
    if ts_p or ts_m:
        bits.append(f"tool_surface +{ts_p} -{ts_m} lines")
    if evt.rollout_protocol_change:
        b = evt.rollout_protocol_change.get("before")
        a = evt.rollout_protocol_change.get("after")
        bits.append(f"rollout {b} → {a}")
    if evt.context_manager_change:
        b = evt.context_manager_change.get("before")
        a = evt.context_manager_change.get("after")
        bits.append(f"context_manager {b} → {a}")
    bud = _format_budget_changes(evt.budget_changes)
    if bud:
        bits.append(bud)
    return ", ".join(bits) if bits else "harness updated"


def _divergence_headline(evt: Any) -> str:
    """One-line headline for an improver.divergence event.

    Shows which top-level fields each proposal touched — operators care
    about "where do they disagree", not the full nested payload.
    """
    llm_keys = sorted(
        k for k in (evt.llm_delta or {}).keys() if k != "improvement_summary"
    )
    heur_keys = sorted(
        k for k in (evt.heuristic_delta or {}).keys() if k != "improvement_summary"
    )
    llm_str = ", ".join(llm_keys) if llm_keys else "(no change)"
    heur_str = ", ".join(heur_keys) if heur_keys else "(no change)"
    return f"LLM proposed [{llm_str}]; rule-based [{heur_str}]"


class DivergenceHighlights(Static):
    """Thin ribbon surfacing the most-recent improver event.

    Priority order (newer always wins regardless of kind):
      1. delta.diff with ``no_change=false`` → headline + truncated diff
      2. improver.divergence with ``divergent=true`` → headline + summary
      3. delta.diff with ``no_change=true`` → muted "no change" message
      4. nothing fired yet → empty state

    The ribbon is COMPACT — max 4 rendered rows (plus its 1-row border).
    Diff excerpts truncate to ``_DIFF_EXCERPT_MAX_LINES`` so the ribbon
    never competes with the primary panels below it.

    Mirrors HeaderStats / VerdictHistogram's Static-backed reactive
    pattern — a single ``event`` reactive drives ``update()`` in
    ``watch_event``. Colour palette reuses the existing widgets.py
    convention (success green / danger red / dim context / accent border).
    """

    DEFAULT_CSS = """
    DivergenceHighlights {
        height: auto;
        max-height: 6;
        padding: 0 1;
        color: $text;
        background: $boost;
        border: tall $accent 40%;
    }
    """

    event: reactive[Optional[Any]] = reactive(None, layout=True)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__("", *args, **kwargs)
        self._empty_text = (
            "[dim]Δ ribbon[/]  [dim]Awaiting iteration boundary…[/]"
        )
        self.update(self._empty_text)

    def watch_event(self, value: Any) -> None:
        if value is None:
            self.update(self._empty_text)
            return
        kind = getattr(value, "kind", "")
        if kind == "delta_diff":
            self.update(self._render_delta_diff(value))
        elif kind == "divergence":
            self.update(self._render_divergence(value))
        else:
            # nervous-bus-yn9v fix 4: any non-None event with an unknown
            # kind still renders SOMETHING (iteration header) so the
            # ribbon never re-empties when an improver event lands —
            # the "Awaiting iteration boundary…" placeholder is reserved
            # for the genuinely-no-events state.
            it = int(getattr(value, "iteration", 0) or 0)
            self.update(
                f"[bold cyan]Δ iter {it}[/]  "
                f"[dim]improver event recorded ({kind or 'unspecified'})[/]"
            )

    # ---- renderers ---------------------------------------------------------
    def _render_delta_diff(self, evt: Any) -> str:
        it = int(getattr(evt, "iteration", 0))
        prev = max(0, it - 1)
        if getattr(evt, "no_change", False):
            return (
                f"[bold cyan]Δ iter {prev} → {it}[/]  "
                f"[dim]no change since last apply[/]"
            )
        headline = _delta_diff_headline(evt)
        lines: list[str] = [
            f"[bold cyan]Δ iter {prev} → {it}[/]  [bold]{headline}[/]"
        ]
        # Prefer the system_prompt sample; otherwise the tool_surface sample.
        # Past _DIFF_EXCERPT_MAX_LINES we drop to a "see full diff" hint —
        # the operator can `deer obs bus` for the rest.
        excerpt_source = evt.system_prompt_diff or evt.tool_surface_diff
        sp_p, sp_m, excerpt = _summarise_unified_diff(excerpt_source)
        for raw in excerpt[:_DIFF_EXCERPT_MAX_LINES]:
            lines.append(_render_diff_line(raw))
        total_changes = sp_p + sp_m
        if total_changes > len(excerpt) and excerpt:
            lines.append(
                f"[dim]… {total_changes - len(excerpt)} more diff lines — "
                f"see full diff[/]"
            )
        return "\n".join(lines)

    def _render_divergence(self, evt: Any) -> str:
        it = int(getattr(evt, "iteration", 0))
        if not getattr(evt, "divergent", False):
            return (
                f"[bold cyan]Δ iter {it}[/]  "
                f"[dim]improver agrees with rule-based heuristic[/]"
            )
        headline = _divergence_headline(evt)
        summary = (getattr(evt, "divergence_summary", "") or "").strip()
        # Divergence == the LLM improver disagreed with the rule-based
        # heuristic. This belongs to the refactor-family "weird" signal set,
        # so it uses VERDICT_PALETTE['RV'] (magenta) — same family as RV/RD/RT.
        lines: list[str] = [
            f"[bold {VERDICT_PALETTE['RV']}]Δ iter {it} divergence[/]  "
            f"[bold]{headline}[/]"
        ]
        if summary:
            first = next((s for s in summary.splitlines() if s.strip()), "")
            if first:
                escaped = _truncate_diff_line(first).replace("[", r"\[")
                lines.append(f"[dim]{escaped}[/]")
        return "\n".join(lines)


class CycleOutcomeBanner(Static):
    """Single-sentence headline summarizing the most-recent session.

    nervous-bus-wutr. Mounted at the very top of the dashboard, above
    HeaderStats — the moment the operator's eyes land on the screen they
    see the verdict for the cycle. Format:

      Cycle <session_short>: <verdict> <score_initial>→<score_final>
      across <N> iters · AHE: <hits>/<total> · cost $<X>

    Verdict color is anchored to the existing AHE palette: ``improved``
    → COLOR_AHE_HIT (green), ``regressed`` → COLOR_AHE_MISS (red), and
    ``flat`` → COLOR_DIM. ``running`` and ``pending`` use neutral cyan/dim
    so an in-flight session reads as ongoing rather than win/loss.

    The widget reads a single reactive ``payload`` dict produced by
    ``PulseState.cycle_outcome_payload()``. ``None`` payload renders the
    empty state.
    """

    payload: reactive[Optional[dict[str, Any]]] = reactive(None, layout=True)

    DEFAULT_CSS = """
    CycleOutcomeBanner {
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("[dim]Cycle: waiting for events…[/]", **kwargs)

    def watch_payload(self, value: Optional[dict[str, Any]]) -> None:
        self.update(self.render_markup(value))

    @staticmethod
    def render_markup(value: Optional[dict[str, Any]]) -> str:
        if not value:
            return "[dim]Cycle: waiting for events…[/]"
        verdict = str(value.get("verdict") or "pending")
        # Verdict → color anchor. Improvement/regression map to the AHE
        # win/miss palette (green/red); flat is muted; running/complete/
        # pending get neutral colors (cyan accent / dim).
        if verdict == "improved":
            v_color = COLOR_AHE_HIT
        elif verdict == "regressed":
            v_color = COLOR_AHE_MISS
        elif verdict == "flat":
            v_color = COLOR_DIM
        elif verdict == "running":
            v_color = COLOR_QUEUE_PRESSURE_WATCH
        else:
            v_color = COLOR_ACCENT
        short = str(value.get("session_short") or "?")
        si = value.get("score_initial")
        sf = value.get("score_final")
        score_str: str
        if si is not None and sf is not None:
            score_str = f"{float(si):.2f}→{float(sf):.2f}"
        elif sf is not None:
            score_str = f"·→{float(sf):.2f}"
        else:
            score_str = "·→·"
        iters = int(value.get("iters_count") or 0)
        hits = int(value.get("ahe_hits") or 0)
        total = int(value.get("ahe_total") or 0)
        # nervous-bus-dq7l: $ removed from the cycle banner. The pricing
        # tables that produced this number were either MiniMax list prices
        # (which don't apply on the coding plan) or hardcoded model-name
        # estimates (which drift). Request-count telemetry will replace
        # this in Phase 2.
        return (
            f"[bold]Cycle[/] [{COLOR_ACCENT}]{short}[/]: "
            f"[bold {v_color}]{verdict}[/] "
            f"[bold]{score_str}[/] across [bold]{iters}[/] iters"
            f" · AHE: [bold]{hits}/{total}[/]"
        )


class HeaderStats(Static):
    """A reactive ``text`` line displayed under the header."""

    text: reactive[str] = reactive("autobench-pulse v2 — waiting for events…")

    def watch_text(self, value: str) -> None:
        self.update(value)


# ---------------------------------------------------------------------------- #
# FailureCodeSidebar                                                           #
# ---------------------------------------------------------------------------- #


# Initial FailureCodeSidebar palette. NOTE: this dict is later overwritten
# at module scope by ``_FAILURE_VERDICT_COLOR = _CASE_VERDICT_COLOR`` (see
# below) so both sidebars share one source. The assignment exists for
# back-compat with sibling modules that imported the name early.
_FAILURE_VERDICT_COLOR: dict[str, str] = {
    "CE": VERDICT_PALETTE["CE"],
    "RE": VERDICT_PALETTE["RE"],
    "TLE": VERDICT_PALETTE["TLE"],
    "MLE": VERDICT_PALETTE["MLE"],
}


class FailureCodeSidebar(Static):
    """Last N CE/RE/TLE/MLE cases with a short generated_code preview.

    Per §7.5.5 / §8.2: this widget never refreshes on every event. The App's
    10 Hz render tick calls ``set_cases`` with the latest pre-aggregated deque
    snapshot from ``PulseState.failure_cases``; the widget short-circuits if
    the snapshot hasn't changed since the last paint.

    All string work (truncation, preview slicing) happens upstream in
    ``state.py`` so ``render()`` stays well under the 50 ms budget even with
    the worst-case ring of N entries.
    """

    # ``revision`` is bumped by PulseState every time a failure is appended;
    # the App writes it via the render tick. Watching a single integer (rather
    # than the deque) lets Textual's reactive machinery do the change-detect
    # cheaply without us re-hashing the case list.
    revision: reactive[int] = reactive(0)

    DEFAULT_CSS = """
    FailureCodeSidebar {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._cases: list[Any] = []

    def set_cases(self, cases: list[Any], revision: int) -> None:
        if revision == self.revision and len(cases) == len(self._cases):
            return
        self._cases = list(cases)
        self.revision = revision

    def watch_revision(self, _value: int) -> None:
        self.update(self._build_markup())

    def _build_markup(self) -> str:
        if not self._cases:
            return "[dim]waiting for failures…[/]"
        lines: list[str] = []
        for fc in reversed(self._cases):
            # Verdict color via palette; unknown verdicts inherit the
            # TLE tone (failures default to "resource exhaustion" red).
            color = _CASE_VERDICT_COLOR.get(fc.verdict, VERDICT_PALETTE["TLE"])
            case_id = _truncate_label(fc.case_id, 32)
            iter_tag = f"  [dim]iter {fc.iteration}[/]"
            lang_tag = f"  [dim]{fc.language}[/]" if fc.language else ""
            header = (
                f"[bold cyan]{case_id}[/]"
                f"  [{color}]{fc.verdict}[/]"
                f"{iter_tag}{lang_tag}"
            )
            lines.append(header)
            preview = _escape_markup(fc.code_preview)
            if not preview:
                lines.append("  [dim](no code)[/]")
            else:
                for raw in preview.splitlines() or [preview]:
                    clipped = _truncate_label(raw, 78)
                    lines.append(f"  [white]{clipped}[/]")
                if fc.code_truncated:
                    lines.append("  [dim]…[/]")
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)


# ---------------------------------------------------------------------------- #
# CEPatternPanel — top-N failing-case shared-prefix clusters                   #
# ---------------------------------------------------------------------------- #

# Case-level verdict palette — derived directly from VERDICT_PALETTE (the
# single source of color truth). Used by FailureCodeSidebar and CEPatternPanel.
# Previously hand-coded with red-or-yellow heuristics; now every verdict
# inherits its canonical palette color. RV is included for completeness but
# never rendered here (refactor-verified successes don't surface as failures).
_CASE_VERDICT_COLOR = {
    "OK": VERDICT_PALETTE["OK"],
    "TLE": VERDICT_PALETTE["TLE"],
    "MLE": VERDICT_PALETTE["MLE"],
    "CE": VERDICT_PALETTE["CE"],
    "RE": VERDICT_PALETTE["RE"],
    "WA": VERDICT_PALETTE["WA"],
    "VF": VERDICT_PALETTE["VF"],
    "RV": VERDICT_PALETTE["RV"],
    "RD": VERDICT_PALETTE["RD"],
    "RT": VERDICT_PALETTE["RT"],
}

# Glyph cells for the bar — solid block / shaded / empty. Matches the broader
# "unicode-only" graphics policy in app.py compose() (no extra charting lib).
_BAR_FULL = "█"   # █
_BAR_EMPTY = "░"  # ░
_BAR_WIDTH = 6


def _case_verdict_color(verdict: str) -> str:
    return _CASE_VERDICT_COLOR.get(verdict, "white")


def _bar_for(sample_count: int, total_in_class: int, color: str) -> str:
    """Render a ``_BAR_WIDTH``-cell bar for ``sample_count / total_in_class``."""
    if total_in_class <= 0:
        filled = 0
    else:
        ratio = max(0.0, min(1.0, sample_count / float(total_in_class)))
        filled = int(round(ratio * _BAR_WIDTH))
        # Guarantee a visible sliver for any non-zero share.
        if sample_count > 0 and filled == 0:
            filled = 1
    empty = _BAR_WIDTH - filled
    return f"[{color}]{_BAR_FULL * filled}[/][dim]{_BAR_EMPTY * empty}[/]"


def _format_prefix(prefix: str, width: int) -> str:
    """Repr-escape then truncate. Keeps the table aligned for noisy prefixes."""
    if prefix is None:
        prefix = ""
    # repr() gives us escaped \n/\t/quotes; trim the surrounding quotes so we
    # can re-wrap them ourselves with a consistent style.
    escaped = repr(prefix)
    if len(escaped) >= 2 and escaped[0] in ("'", '"') and escaped[-1] in ("'", '"'):
        escaped = escaped[1:-1]
    if width > 1 and len(escaped) > width:
        escaped = escaped[: max(1, width - 1)] + "…"  # …
    return escaped


def _truncate_label(s: str, n: int) -> str:
    """Truncate to n chars with an ellipsis suffix; preserve short strings."""
    if len(s) <= n:
        return s
    if n <= 1:
        return s[:n]
    return s[: n - 1] + "…"


def _escape_markup(s: str) -> str:
    """Defang Rich markup characters so user code can't inject styling."""
    return s.replace("[", "\\[")


# Compat alias: some sibling agents referenced _FAILURE_VERDICT_COLOR; we use
# the unified _CASE_VERDICT_COLOR table (defined above) since both panels
# colour-code the same verdicts the same way.
_FAILURE_VERDICT_COLOR = _CASE_VERDICT_COLOR


class CEPatternPanel(Static):
    """Top-3 shared-prefix clusters of failing cases, live.

    Primary data source: ``autobench.failure_pattern.v1`` events accumulated
    in ``PulseState.failure_patterns``. Fallback: when the detector hasn't
    fired yet, falls back to ``PulseState._infer_failure_patterns`` which
    mirrors the detector's algorithm with a relaxed threshold so the panel is
    responsive in the first minute of a run.

    nervous-bus-yn9v fix 3: when no patterns exist, the panel now surfaces
    the most-recent completed iteration's verdict breakdown via the
    ``latest_summary`` reactive — so the bottom-of-column slot reads as
    useful "post-iter verdict roll-up" data rather than an empty box.
    """

    DEFAULT_CSS = """
    CEPatternPanel {
        height: auto;
        min-height: 6;
        border: tall $secondary;
        padding: 0 1;
    }
    """

    patterns: reactive[list[dict[str, Any]]] = reactive(list)
    latest_summary: reactive[Optional[dict[str, Any]]] = reactive(None)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.border_title = "CE Pattern Panel"

    def watch_patterns(self, value: list[dict[str, Any]]) -> None:
        self.update(self._format(value))

    def watch_latest_summary(self, _value: Optional[dict[str, Any]]) -> None:
        # When patterns are empty, the fallback rendering depends on the
        # iteration summary — re-emit on summary changes so the empty state
        # keeps pace with each completed iteration.
        if not self.patterns:
            self.update(self._format(self.patterns))

    def render_text(self) -> str:
        return self._format(self.patterns)

    def _format(self, value: list[dict[str, Any]]) -> str:
        if not value:
            return self._format_empty()
        prefix_width = max(
            20,
            getattr(self, "size", None).width - 18
            if getattr(self, "size", None) and self.size.width
            else 36,
        )
        lines: list[str] = []
        for p in value[:3]:
            verdict = str(p.get("verdict") or "?")
            color = _case_verdict_color(verdict)
            sample_count = int(p.get("sample_count") or 0)
            total = int(p.get("total_in_class") or sample_count or 1)
            bar = _bar_for(sample_count, total, color)
            prefix = _format_prefix(p.get("prefix") or "", prefix_width)
            tag = "" if p.get("source") != "inferred" else " [dim](inferred)[/]"
            lines.append(
                f"[bold {color}]{verdict:<3}[/] {bar} "
                f"[dim]{sample_count}/{total}[/] "
                f"'{prefix}'{tag}"
            )
        return "\n".join(lines)

    def _format_empty(self) -> str:
        """Empty-state rendering — fall back to latest iter verdict breakdown.

        nervous-bus-yn9v fix 3: surfaces verdict data for the most recently
        completed iteration so the bottom-of-column slot is never visually
        blank just because no failure_pattern.v1 event has fired yet.
        """
        snap = self.latest_summary or {}
        verdicts = snap.get("verdict_counts") or {}
        num_cases = int(snap.get("num_cases") or 0)
        iter_num = snap.get("iteration")
        if not verdicts or num_cases <= 0:
            return "[dim]no failure patterns detected yet[/]"
        # Compact one-line verdict roll-up with palette colours.
        parts: list[str] = []
        for k, v in sorted(
            verdicts.items(), key=lambda kv: (-int(kv[1] or 0), kv[0])
        ):
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if n <= 0:
                continue
            color = VERDICT_PALETTE.get(k, "white")
            parts.append(f"[{color}]{k}:{n}[/]")
        verdict_line = "  ".join(parts) if parts else "[dim]—[/]"
        header = (
            f"[dim]no failure patterns yet · iter[/] "
            f"[bold cyan]{iter_num}[/] [dim]verdict roll-up:[/]"
            if iter_num is not None
            else "[dim]no failure patterns yet · latest verdict roll-up:[/]"
        )
        return f"{header}\n{verdict_line}  [dim]({num_cases} cases)[/]"


# ---------------------------------------------------------------------------- #
# AHEPredictionPanel (nervous-bus-a5mx)                                        #
# ---------------------------------------------------------------------------- #


_AHE_HISTORY_LIMIT = 5

# Status → coloured dot for the history row at the bottom of the panel.
# Each glyph inherits a color from the AHE auxiliary palette so the history
# strip + the main status line + the lineage strip all speak the same color.
_AHE_HISTORY_DOT = {
    "confirmed": f"[{COLOR_AHE_HIT}]●[/]",
    "partial": f"[{COLOR_QUEUE_PRESSURE_WATCH}]◐[/]",
    "refuted": f"[{COLOR_AHE_MISS}]✗[/]",
    "refuted_live": f"[{COLOR_AHE_MISS}]✗[/]",
    "pending": f"[{COLOR_DIM}]·[/]",
}


_PARSE_STATUS_BADGE = {
    "ok": (f"[{COLOR_AHE_HIT}]●[/]", "parsed"),
    "ok_after_repair": (f"[{COLOR_QUEUE_PRESSURE_WATCH}]●[/]", "parsed (repaired)"),
    "no_change": (f"[{COLOR_DIM}]○[/]", "LLM: no change"),
    "fell_back_to_rule_based": (f"[{COLOR_AHE_MISS}]✗[/]", "parser fallback"),
    "parse_failed": (f"[{COLOR_AHE_MISS}]✗[/]", "parse failed"),
}


def _format_parse_status_badge(status: Optional[str]) -> str:
    """Render the improver parse_status as a colored badge for the AHE panel.

    ane0: critical to distinguish 'LLM said no change' (intentional no-op,
    green dot) from 'parser silently failed' (latent observability gap, red).
    Empty string when no status has been observed yet.
    """
    if not status:
        return ""
    glyph, label = _PARSE_STATUS_BADGE.get(
        status, (f"[{COLOR_DIM}]?[/]", str(status)),
    )
    return f"{glyph} [dim]{label}[/]"


def _format_verdict_class_changes(changes: dict[str, int]) -> str:
    """Format ``{"OK": +3, "CE": -3}`` → ``+3 OK / -3 CE``.

    Sorted by magnitude descending so the biggest predicted shift leads.
    """
    if not changes:
        return "[dim]—[/]"
    parts: list[str] = []
    for k, v in sorted(changes.items(), key=lambda kv: (-abs(int(kv[1])), kv[0])):
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        sign = "+" if n >= 0 else ""
        parts.append(f"{sign}{n} {k}")
    return " / ".join(parts) if parts else "[dim]—[/]"


# ---------------------------------------------------------------------------- #
# Watermark thermometer renderer (FIX 2)                                       #
# ---------------------------------------------------------------------------- #

# Width of the thermometer bar in glyphs. Kept tight so it shares a line
# with the numeric counter without wrapping in narrow terminals.
_WATERMARK_BAR_WIDTH: int = 10
# Filled / empty glyphs for the bar. Block-half glyphs read as a meter at
# small widths better than full blocks (which look chunky next to text).
_WATERMARK_FILL_GLYPH: str = "█"
_WATERMARK_EMPTY_GLYPH: str = "░"


def _watermark_thermometer(value: int, initial: int, color: str) -> str:
    """Render a fixed-width thermometer bar showing ``value / initial``.

    When ``value`` hits zero the bar flips to bold red (regardless of the
    requested ``color``) so the eye reads "this prediction is one bad
    case away from death." When ``initial`` is zero (idle / no slack
    computed yet) we render an all-empty bar in dim grey.
    """
    if initial <= 0:
        return f"[{COLOR_DIM}]{_WATERMARK_EMPTY_GLYPH * _WATERMARK_BAR_WIDTH}[/]"
    safe_value = max(0, min(int(value), int(initial)))
    filled = int(round((safe_value / float(initial)) * _WATERMARK_BAR_WIDTH))
    filled = max(0, min(_WATERMARK_BAR_WIDTH, filled))
    fill = _WATERMARK_FILL_GLYPH * filled
    empty = _WATERMARK_EMPTY_GLYPH * (_WATERMARK_BAR_WIDTH - filled)
    if safe_value <= 0:
        # Bold red pulses when fully drained — visceral "about to die".
        return f"[bold {COLOR_AHE_MISS}]{_WATERMARK_EMPTY_GLYPH * _WATERMARK_BAR_WIDTH}[/]"
    return f"[{color}]{fill}[/][{COLOR_DIM}]{empty}[/]"


# ---------------------------------------------------------------------------- #
# Refutation flash sequence (FIX 1)                                            #
# ---------------------------------------------------------------------------- #

# Ordered colour cycle the AHE panel border steps through when a
# refuted_live transition lands. Each frame holds for ~166 ms so the
# whole flash completes inside ~500 ms.
_REFUTATION_FLASH_STEPS: tuple[str, ...] = ("red", "orange3", "yellow")
_REFUTATION_FLASH_STEP_S: float = 0.166
# Default "normal" border colour to restore after the flash completes.
# Mirrors the warning hue used by DEFAULT_CSS at idle.
_REFUTATION_BORDER_RESTORE: str = "$warning 60%"


class AHEPredictionPanel(Static):
    """Live AHE prediction panel — shows the staked prediction + watermark.

    Surfaces the differentiator the existing ``AHEPredictionTracker`` only
    half-shows: this panel renders the IN-FLIGHT prediction with its full
    contract (predicted score delta, per-verdict class changes, confidence,
    watermark of remaining slack) plus a history strip of recent outcomes.

    The widget is fed via reactive ``payload`` — a plain dict produced by
    ``PulseState.ahe_prediction_panel_payload`` (computed each render tick).
    Empty payload renders an idle placeholder.

    FIX 1 (live-refutation flash + bell): when ``payload['prediction'].status``
    transitions to ``refuted_live`` the border cycles through red → orange →
    yellow over ~500 ms and a terminal bell (``\\a``) is written to stdout.

    FIX 2 (watermark thermometer drain): the watermark line is rendered as a
    text-glyph progress bar with ``watermark`` / ``watermark_initial`` as the
    numerator / denominator. The reactive ``watermark_value`` /
    ``watermark_initial`` / ``zero_pulse`` properties are exposed for tests
    and for downstream widgets that want to mirror the drain state.
    """

    DEFAULT_CSS = """
    AHEPredictionPanel {
        height: auto;
        min-height: 6;
        padding: 0 1;
        border: round $warning 60%;
    }
    """

    payload: reactive[Optional[dict[str, Any]]] = reactive(None, always_update=True)
    # FIX 2: numerator / denominator of the thermometer. Tests assert these
    # drop monotonically as cases land.
    watermark_value: reactive[int] = reactive(0)
    watermark_initial: reactive[int] = reactive(0)
    # FIX 2: True once the bar has drained to 0 — the rendered bar flips to
    # bold red and the widget toggles a CSS class so a stylesheet pulse rule
    # can pick it up. Tests assert this transitions from False → True.
    zero_pulse: reactive[bool] = reactive(False)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=True, **kwargs)
        self.border_title = "AHE Prediction"
        # FIX 1: track the previous status so we only fire the flash on the
        # transition INTO refuted_live (not on every payload tick while
        # already refuted).
        self._prev_status: Optional[str] = None
        # FIX 1: cache active flash timers so successive refutations replace
        # rather than stack.
        self._flash_timers: list[Any] = []
        # FIX 1: bumped each time a refutation flash is triggered. Tests
        # subscribe by polling this counter instead of mocking the timer API.
        self.flash_count: int = 0
        self.update(self._render_idle())

    def watch_payload(self, value: Optional[dict[str, Any]]) -> None:
        if not value or not value.get("prediction"):
            self.watermark_value = 0
            self.watermark_initial = 0
            self.zero_pulse = False
            self._prev_status = None
            self.update(self._render_idle())
            return
        rec = value["prediction"]
        status = str(getattr(rec, "status", "pending"))
        # FIX 2: sync thermometer reactives from the payload before rendering.
        wm = int(value.get("watermark") or 0)
        wm_init = int(value.get("watermark_initial") or 0)
        # Guard against denominator < numerator (defensive — state pins
        # initial to the high-water mark, but tests may bypass that path).
        if wm_init < wm:
            wm_init = wm
        self.watermark_value = wm
        self.watermark_initial = wm_init
        self.zero_pulse = bool(wm_init > 0 and wm <= 0)
        # FIX 1: fire flash + bell on transition into refuted_live.
        if (
            status == "refuted_live"
            and self._prev_status != "refuted_live"
        ):
            self.trigger_refutation_flash()
        self._prev_status = status
        self.update(self._render_payload(value))

    # -------------------------------------------------- FIX 1 helpers ----
    def trigger_refutation_flash(self) -> None:
        """Step the border through the refutation colour cycle and ring the bell.

        Implementation note: each frame is scheduled via ``set_timer`` so the
        animation runs on the Textual event loop without blocking renders.
        Tests that don't have a mounted app simply check that ``flash_count``
        was bumped — the timers are best-effort and harmless when unmounted.
        """
        self.flash_count += 1
        # Bell: a single \a byte. Best-effort — stdout may be closed in
        # headless test environments, so wrap in try.
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
        # Apply the first frame synchronously and schedule the remaining
        # frames + restore on the Textual event loop. ``set_timer(0, ...)``
        # divides by zero inside Textual's timer-stop path during teardown,
        # so the immediate frame must be applied directly rather than via a
        # zero-delay timer.
        first_color = _REFUTATION_FLASH_STEPS[0] if _REFUTATION_FLASH_STEPS else "red"
        self._apply_flash_color(first_color)
        # Only schedule timers if the widget is actually attached to a running
        # Textual app — calling ``set_timer`` without a loop spawns an
        # un-awaited coroutine and warns. ``is_mounted`` is the cheapest
        # idempotent guard.
        if not getattr(self, "is_mounted", False):
            return
        try:
            for i, color in enumerate(_REFUTATION_FLASH_STEPS[1:], start=1):
                delay = i * _REFUTATION_FLASH_STEP_S
                timer = self.set_timer(
                    delay, lambda c=color: self._apply_flash_color(c)
                )
                self._flash_timers.append(timer)
            # Final frame: restore the resting border colour just past the
            # last flash step.
            restore_delay = len(_REFUTATION_FLASH_STEPS) * _REFUTATION_FLASH_STEP_S
            timer = self.set_timer(restore_delay, self._restore_flash_color)
            self._flash_timers.append(timer)
        except Exception:
            # Best-effort — no Textual loop available.
            pass

    def _apply_flash_color(self, color: str) -> None:
        """Apply one frame of the refutation border cycle."""
        try:
            self.styles.border = ("round", color)
        except Exception:
            pass

    def _restore_flash_color(self) -> None:
        """Return the border to its idle warning hue once the flash ends."""
        try:
            # Restore by clearing the inline override and falling back to
            # DEFAULT_CSS. ``None`` removes the style on Textual >= 0.45.
            self.styles.border = None  # type: ignore[assignment]
        except Exception:
            pass

    # -------------------------------------------------- render helpers ---
    def _render_idle(self) -> str:
        return (
            "[bold cyan]AHE Prediction[/]\n"
            "[dim]no falsifiable prediction staked yet — "
            "awaiting improver…[/]"
        )

    def _render_payload(self, payload: dict[str, Any]) -> str:
        rec = payload["prediction"]
        status = str(getattr(rec, "status", "pending"))
        glyph = _AHE_HISTORY_DOT.get(status, "·")
        delta = float(getattr(rec, "predicted_score_delta", 0.0) or 0.0)
        delta_sign = "+" if delta >= 0 else ""
        confidence = float(getattr(rec, "confidence", 0.0) or 0.0)
        watermark = int(payload.get("watermark") or 0)
        iter_n = int(getattr(rec, "iteration", 0))

        # Map watermark → colour, same ladder as queue-pressure tiers.
        # 0 slack means refutation is one bad case away → escalate to crit.
        if watermark <= 0:
            wm_color = f"bold {COLOR_AHE_MISS}"
        elif watermark <= 2:
            wm_color = COLOR_QUEUE_PRESSURE_WATCH
        else:
            wm_color = COLOR_AHE_PENDING

        verdict_changes = _format_verdict_class_changes(
            dict(getattr(rec, "predicted_verdict_class_changes", {}) or {})
        )

        # FIX 2: render the watermark as a draining thermometer bar.
        # Denominator is the initial watermark observed when the prediction
        # was first staked; the bar drains as cases land and consume slack.
        wm_initial = int(payload.get("watermark_initial") or 0)
        bar = _watermark_thermometer(watermark, wm_initial, wm_color)

        lines: list[str] = [
            (
                f"[bold cyan]iter {iter_n} → {iter_n + 1}[/]   "
                f"[dim]predicted_score_delta:[/] "
                f"[bold]{delta_sign}{delta:.3f}[/]"
            ),
            (
                f"[dim]verdict_class_changes:[/] {verdict_changes}   "
                f"[dim]confidence:[/] [bold]{confidence:.2f}[/]"
            ),
            (
                f"[dim]watermark:[/] {bar} "
                f"[{wm_color}]{watermark}/{max(wm_initial, watermark)}[/] "
                f"[dim]before refutation[/]"
            ),
        ]

        # Status line — refuted_live foregrounds the refutation_reason in bold red.
        label_color = _PREDICTION_COLOR.get(status, "white")
        label_text = _PREDICTION_LABEL.get(status, status)
        parse_badge = _format_parse_status_badge(payload.get("parse_status"))
        if status == "refuted_live":
            reason = _truncate(str(getattr(rec, "refutation_reason", "") or ""), 80)
            # Live-refutation is bold COLOR_AHE_MISS (red) — the eye reads
            # "the prediction just died" without needing to scan the label.
            lines.append(
                f"[dim]status:[/] {glyph} [bold {COLOR_AHE_MISS}]{label_text}[/]  "
                f"[bold {COLOR_AHE_MISS}]{reason}[/]   {parse_badge}"
            )
        else:
            lines.append(
                f"[dim]status:[/] {glyph} [{label_color}]{label_text}[/]   {parse_badge}"
            )

        # History row — last N predictions as dots, oldest-first.
        # nervous-bus-yn9v fix 6: dots are now session-scoped (so a fresh
        # cycle's first prediction shows ONE dot, not 5 inherited from
        # prior sessions). If state knows about older predictions in other
        # sessions it surfaces them via ``history_dots_scope`` so we can
        # render a subtle annotation explaining the scope.
        dots = payload.get("history_dots") or []
        scope = payload.get("history_dots_scope") or {}
        cross = int(scope.get("cross_session_count") or 0)
        if dots:
            line = "[dim]history (this session):[/] " + " ".join(dots)
            if cross > 0:
                line += f"  [dim]({cross} more across sessions)[/]"
            lines.append(line)
        return "\n".join(lines)


# ---------------------------------------------------------------------------- #
# QueuePressureBar (nervous-bus-m3so)                                          #
# ---------------------------------------------------------------------------- #


# Sparkline glyph ramp (8 levels). Standard block-character ladder — same
# family used by IterationProgressPanel for visual cohesion.
_SPARK_GLYPHS = " ▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], width: int = 30) -> str:
    """Render ``values`` as a unicode sparkline of ``width`` cells.

    Each cell is one glyph from ``_SPARK_GLYPHS`` sized by the value's
    fraction of the window's maximum. Empty / all-zero inputs render as
    space padding so the bar keeps a constant width.
    """
    if not values:
        return " " * width
    # Trim/pad to ``width`` from the right (we want the freshest cells on the right).
    tail = values[-width:]
    pad = width - len(tail)
    vmax = max(tail) if tail else 0.0
    if vmax <= 0:
        return " " * width
    glyphs: list[str] = [" "] * pad
    for v in tail:
        if v <= 0:
            glyphs.append(_SPARK_GLYPHS[0])
            continue
        ratio = min(1.0, v / vmax)
        idx = int(round(ratio * (len(_SPARK_GLYPHS) - 1)))
        glyphs.append(_SPARK_GLYPHS[max(1, idx)])
    return "".join(glyphs)


def _queue_pressure_color(deviation: float) -> str:
    """Map ``deviation_factor`` (smaller=worse) → rich colour name.

    Per spec: dev < 0.25 → cyan (normal); 0.25 ≤ dev < 0.50 → yellow (watch);
    dev ≥ 0.50 → red (critical). Higher deviation_factor means the rate has
    drifted further from baseline; see the schema description.
    """
    if deviation >= 0.50:
        return COLOR_QUEUE_PRESSURE_CRIT
    if deviation >= 0.25:
        return COLOR_QUEUE_PRESSURE_WATCH
    return COLOR_QUEUE_PRESSURE_OK


class QueuePressureBar(Static):
    """Slim top-bar widget for MiniMax queue-pressure surfacing.

    Renders one row (with optional second-row sparkline) of:
    ``tps: X / Y baseline [sparkline] dev: 0.NN ↓``. Colour-coded by the
    deviation factor: cyan (normal), yellow (watch), red (critical).

    Reactive ``payload`` is the dict from ``PulseState.queue_pressure_summary``;
    set to ``None`` until a queue_pressure event arrives.
    """

    DEFAULT_CSS = """
    QueuePressureBar {
        height: 2;
        padding: 0 1;
    }
    """

    payload: reactive[Optional[dict[str, Any]]] = reactive(None, always_update=True)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=True, **kwargs)
        self.update(self._render_idle())

    def watch_payload(self, value: Optional[dict[str, Any]]) -> None:
        if value is None or value.get("latest") is None:
            self.update(self._render_idle())
            return
        self.update(self._render_payload(value))

    def _render_idle(self) -> str:
        return "[dim]queue pressure: awaiting worker.queue_pressure.v1…[/]"

    def _render_payload(self, payload: dict[str, Any]) -> str:
        latest = payload["latest"]
        current = float(latest.get("current_rate_tps") or 0.0)
        baseline = float(latest.get("baseline_tps") or 0.0)
        deviation = float(latest.get("deviation_factor") or 0.0)
        window: list[float] = list(payload.get("tps_window") or [])
        spark = _sparkline(window, width=30)

        color = _queue_pressure_color(deviation)
        # Trend arrow — coarse heuristic. Below 1.0 baseline-ratio = falling.
        # If the rolling window's last value is below its midpoint mean we
        # mark a down-trend, otherwise neutral or up.
        if len(window) >= 2:
            mid = sum(window) / len(window)
            arrow = "↓" if window[-1] < mid else ("↑" if window[-1] > mid else "→")
        else:
            arrow = "→"

        line = (
            f"[bold cyan]tps:[/] [{color}]{current:.1f}[/] / "
            f"[dim]{baseline:.1f} baseline[/]  "
            f"[{color}]{spark}[/]  "
            f"[dim]dev:[/] [{color}]{deviation:.2f} {arrow}[/]"
        )
        return line


# ---------------------------------------------------------------------------- #
# IterationLineageStrip (nervous-bus-4fz3)                                     #
# ---------------------------------------------------------------------------- #


# AHE outcome → dot glyph + style for the strip. Identical to
# AHEPredictionPanel's _AHE_HISTORY_DOT — same source palette.
_LINEAGE_AHE_DOT = {
    "confirmed": f"[{COLOR_AHE_HIT}]●[/]",
    "partial": f"[{COLOR_QUEUE_PRESSURE_WATCH}]◐[/]",
    "refuted": f"[{COLOR_AHE_MISS}]✗[/]",
    "refuted_live": f"[{COLOR_AHE_MISS}]✗[/]",
    "pending": f"[{COLOR_DIM}]·[/]",
}

# Verdict family palette for the mini bar (3 family buckets — not the full
# 10-verdict palette, because a 5-char bar can't render that many).
# Each family inherits its representative verdict's palette color:
#   OK family → VERDICT_PALETTE["OK"]   (just OK)
#   CE family → VERDICT_PALETTE["CE"]   (just CE — distinct orange)
#   WA family → VERDICT_PALETTE["WA"]   (catches all other fails: WA/TLE/...)
_LINEAGE_VERDICT_FAMILIES = (
    ("OK", VERDICT_PALETTE["OK"], {"OK"}),
    ("CE", VERDICT_PALETTE["CE"], {"CE"}),
    ("WA", VERDICT_PALETTE["WA"], {"WA", "TLE", "RE", "MLE", "VF", "RV", "RD", "RT"}),
)


def _lineage_mini_bar(verdicts: dict[str, int], total_width: int = 5) -> str:
    """Render a 5-cell mini bar of OK / CE / WA proportions.

    Coloured per family. ``total_width`` characters are partitioned by the
    fraction each family represents of the total verdict count; any leftover
    cells from rounding land on the largest family.
    """
    total = sum(int(v or 0) for v in verdicts.values())
    if total <= 0:
        return "[dim]" + "·" * total_width + "[/]"
    cells: list[tuple[int, str]] = []  # (count, color)
    consumed = 0
    family_counts: list[tuple[str, str, int]] = []
    for label, color, members in _LINEAGE_VERDICT_FAMILIES:
        n = sum(int(verdicts.get(v) or 0) for v in members)
        family_counts.append((label, color, n))
    # Allocate cells proportional to family count.
    allocations: list[int] = []
    for _, _, n in family_counts:
        share = int(round((n / total) * total_width)) if total > 0 else 0
        allocations.append(share)
        consumed += share
    # Adjust to exactly total_width — drop/add to the largest family.
    if consumed != total_width and family_counts:
        biggest_idx = max(
            range(len(family_counts)), key=lambda i: family_counts[i][2]
        )
        allocations[biggest_idx] += total_width - consumed
        allocations[biggest_idx] = max(0, allocations[biggest_idx])
    out: list[str] = []
    for (label, color, _), share in zip(family_counts, allocations):
        if share <= 0:
            continue
        out.append(f"[{color}]" + "█" * share + "[/]")
    rendered = "".join(out)
    # If rounding dropped everything, pad with dim dots so width is stable.
    if not rendered:
        return "[dim]" + "·" * total_width + "[/]"
    return rendered


class IterationLineageStrip(Static):
    """Horizontal 4-column strip of N-3 / N-2 / N-1 / N (pending) iterations.

    Each cell renders:
      * iteration index (header)
      * aggregate_score
      * mini verdict bar (OK / CE / WA)
      * AHE outcome dot (● hit / ○ miss / ✗ refuted / · pending)

    The rightmost column shows the in-flight iteration whose prediction is
    still pending; ``aggregate_score`` is replaced by the *predicted* score
    delta in muted colour, since no actual score exists yet.

    Reactive ``cells`` is the list returned by ``PulseState.iteration_lineage``.
    """

    DEFAULT_CSS = """
    IterationLineageStrip {
        height: 6;
        padding: 0 1;
        border: round $primary 50%;
    }
    """

    cells: reactive[list[dict[str, Any]]] = reactive(list, always_update=True)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=True, **kwargs)
        self.border_title = "Iteration Lineage"
        self.update(self._render_idle())

    def watch_cells(self, value: list[dict[str, Any]]) -> None:
        if not value:
            self.update(self._render_idle())
            return
        self.update(self._render_cells(value))

    def _render_idle(self) -> str:
        return "[dim]iteration lineage: awaiting iteration.summary.v1…[/]"

    def _render_cells(self, cells: list[dict[str, Any]]) -> str:
        # Render four parallel row-strings (header, score, bar, ahe-dot)
        # then join with column separators.
        headers: list[str] = []
        scores: list[str] = []
        bars: list[str] = []
        dots: list[str] = []
        for cell in cells:
            iter_n = int(cell.get("iteration") or 0)
            kind = str(cell.get("kind") or "completed")
            ahe_status = str(cell.get("ahe_status") or "pending")
            verdicts = dict(cell.get("verdict_distribution") or {})
            if kind == "pending":
                headers.append(f"[bold dim]iter {iter_n}[/]")
                pred = cell.get("predicted_score_delta")
                if isinstance(pred, (int, float)):
                    sign = "+" if pred >= 0 else ""
                    scores.append(f"[dim italic]{sign}{pred:.3f}[/]")
                else:
                    scores.append("[dim]—[/]")
                bars.append("[dim]·····[/]")
            else:
                headers.append(f"[bold cyan]iter {iter_n}[/]")
                agg = cell.get("aggregate_score")
                if isinstance(agg, (int, float)):
                    scores.append(f"[bold]{float(agg):.3f}[/]")
                else:
                    scores.append("[dim]—[/]")
                bars.append(_lineage_mini_bar(verdicts))
            dot = _LINEAGE_AHE_DOT.get(ahe_status, "[dim]·[/]")
            dots.append(dot)

        sep = "  [dim]│[/]  "
        return "\n".join(
            [
                sep.join(headers),
                sep.join(scores),
                sep.join(bars),
                sep.join(dots),
            ]
        )


# ---------------------------------------------------------------------------- #
# StderrFaultPanel (FIX 3 — pulse_visual_richness_exploration_2026-05-16)      #
# ---------------------------------------------------------------------------- #

# Verdict → display colour for the panel rows. Routes through VERDICT_PALETTE
# so the panel speaks the same colour language as FailureCodeSidebar +
# IterationProgressPanel. OK / WA are absent because the channel only fires
# on error-class verdicts (CE/RE/TLE/MLE).
_STDERR_VERDICT_COLOR = {
    "CE": VERDICT_PALETTE["CE"],
    "RE": VERDICT_PALETTE["RE"],
    "TLE": VERDICT_PALETTE["TLE"],
    "MLE": VERDICT_PALETTE["MLE"],
}


class StderrFaultPanel(Static):
    """Rolling ring of the last N sandbox stderr excerpts.

    Subscribes to ``autobench.sandbox.stderr.v1`` (via PulseState.recent_stderr)
    and renders one row per excerpt:

        [verdict_color]CE[/] case_0042  SyntaxError: invalid syntax (line 17)

    The render-tick pattern matches FailureCodeSidebar: the App pushes the
    state's deque snapshot via ``set_entries(entries, revision)`` once per
    tick; the widget short-circuits if the revision matches the last paint.
    """

    revision: reactive[int] = reactive(0)

    DEFAULT_CSS = """
    StderrFaultPanel {
        height: auto;
        min-height: 5;
        padding: 0 1;
        border: round $error 60%;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=True, **kwargs)
        self.border_title = "Sandbox stderr"
        self._entries: list[dict[str, Any]] = []
        self.update(self._build_markup())

    def set_entries(self, entries: list[dict[str, Any]], revision: int) -> None:
        """Push a fresh snapshot of recent stderr entries.

        Called from the App's render tick. The widget only repaints when the
        revision counter changes — this keeps the panel inside the §7.5.5
        50 ms / render budget even with frequent stderr emissions.
        """
        if revision == self.revision and len(entries) == len(self._entries):
            return
        self._entries = list(entries)
        self.revision = revision

    def watch_revision(self, _value: int) -> None:
        self.update(self._build_markup())

    def _build_markup(self) -> str:
        if not self._entries:
            return "[dim]no stderr captured yet — waiting for CE/RE/TLE/MLE…[/]"
        # Render newest at the top so the most recent fault is the first
        # thing the operator's eye lands on.
        lines: list[str] = []
        for entry in reversed(self._entries):
            verdict = str(entry.get("verdict") or "?")
            color = _STDERR_VERDICT_COLOR.get(verdict, VERDICT_PALETTE.get("TLE", "red"))
            case_id = _truncate_label(str(entry.get("case_id") or "?"), 24)
            excerpt = str(entry.get("stderr_excerpt") or "").strip()
            # Flatten newlines into a single readable line — the excerpt is
            # already 200 chars max upstream, so we just collapse whitespace.
            flat = " ".join(excerpt.split()) if excerpt else "(no stderr)"
            flat = _truncate_label(flat, 78)
            language = str(entry.get("language") or "")
            lang_tag = f"  [dim]{language}[/]" if language else ""
            lines.append(
                f"[bold {color}]{verdict}[/] [bold cyan]{case_id}[/]{lang_tag}  "
                f"[white]{_escape_markup(flat)}[/]"
            )
        return "\n".join(lines)


HELP_TEXT = """\
[bold]autobench-pulse keybindings[/]

  q     quit
  p     toggle pause (events still buffered)
  /     focus filter input (filter sessions by id substring)
  g     jump to top of session tree
  G     jump to bottom
  j/k   move cursor down/up in tree
  ?     toggle this help
  space toggle tree node
  enter focus tree node

Press [bold]?[/] again to dismiss.
"""
