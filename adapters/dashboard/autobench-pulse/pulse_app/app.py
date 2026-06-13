"""Textual app for autobench-pulse v2.

See ``autobench/research/terminal_rendering_2026.md`` §5 for the architecture
sketch and §7 for the upgrade plan.

Dataflow:

  1. ``@work(thread=True)`` bus listener pulls events from the chosen source
     (``BusSource`` or ``FileSource``).
  2. Each event is applied to ``PulseState`` (single writer).
  3. The worker bumps ``state_version`` on the App via ``call_from_thread``.
  4. ``set_interval`` ticks (1 Hz default) push aggregate reactives to widgets.
     Charts are explicitly *not* refreshed on every event (§8.2).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Static
from textual.worker import Worker, get_current_worker

from .source import (
    BusSource,
    DEFAULT_DEBUG_FILE,
    FileSource,
    REPLAY_SPEED_CAP,
    ReplaySource,
    set_replay_state,
)
from .state import PulseState
from .widgets import (
    AHEPredictionPanel,
    AHEPredictionTracker,
    BurnGauge,
    CycleOutcomeBanner,
    FailureCodeSidebar,
    CEPatternPanel,
    CostRatePanel,
    CuriositySpikeFeed,
    DivergenceHighlights,
    HELP_TEXT,
    HeaderStats,
    IslandHeatmap,
    IterationLineageStrip,
    IterationProgressPanel,
    KernelLeaderboard,
    MultiAdvocatePanel,
    ParetoScatter,
    QueuePressureBar,
    ScoreSpark,
    SessionTree,
    StderrFaultPanel,
    VerdictHistogram,
    WorkerLatencyHistogram,
)


CSS_PATH = Path(__file__).resolve().parent.parent / "pulse.tcss"


# ---------------------------------------------------------------------------- #
# Modal help screen                                                            #
# ---------------------------------------------------------------------------- #


class HelpScreen(ModalScreen):
    BINDINGS = [("escape", "app.pop_screen"), ("?", "app.pop_screen")]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help-screen")


# ---------------------------------------------------------------------------- #
# Main app                                                                     #
# ---------------------------------------------------------------------------- #


class PulseApp(App):
    """Live 2-column Textual dashboard for autobench events."""

    CSS_PATH = str(CSS_PATH)
    TITLE = "autobench-pulse v2"
    SUB_TITLE = "RSI observability"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("/", "focus_filter", "Filter"),
        Binding("g", "jump_top", "Top"),
        Binding("G", "jump_bottom", "Bottom"),
        Binding("?", "toggle_help", "Help"),
    ]

    # Reactives the App publishes -------------------------------------------
    # state_version is bumped by the render tick — NEVER directly from the
    # bus listener. Per research §9 / probe p3, decouple event-ingest rate
    # from display-refresh rate (k9s/btop pattern). The worker thread sets a
    # plain `_dirty` flag; the 10 Hz render tick reads it.
    state_version: reactive[int] = reactive(0)
    paused: reactive[bool] = reactive(False)
    filter_text: reactive[str] = reactive("")

    def __init__(
        self,
        *,
        debug_file: Path = DEFAULT_DEBUG_FILE,
        prefer_bus: bool = False,
        once: bool = False,
        budget_per_min: float = 1.0,
        from_start: bool = True,
        follow: Optional[bool] = None,
        replay_path: Optional[Path] = None,
        replay_session_id: Optional[str] = None,
        replay_speed: float = 1.0,
    ) -> None:
        super().__init__()
        self.state = PulseState()
        self._debug_file = Path(debug_file)
        self._prefer_bus = prefer_bus
        self._once = once
        self._budget_per_min = budget_per_min
        self._from_start = from_start
        if follow is None:
            follow = not once
        self._follow = follow
        # nervous-bus-zynw: replay mode wiring. When ``replay_path`` is set,
        # the bus listener uses ReplaySource instead of BusSource/FileSource.
        # The global replay flag drives the REPLAY badge in HeaderStats.
        self._replay_path = Path(replay_path) if replay_path else None
        self._replay_session_id = replay_session_id
        self._replay_speed = replay_speed
        if self._replay_path is not None:
            set_replay_state(True, replay_speed)
        else:
            set_replay_state(False, 1.0)
        # Set by the bus worker (any thread); read by the render tick. Plain
        # bool is fine — race is benign (worst case we render one frame late).
        self._state_dirty: bool = False
        self._last_chart_tick: float = 0.0
        # Aggregate-tick gate (perf): _tick_aggregates runs ~14 query+recompute
        # +repaint ops at 2 Hz, including 3 expensive plotext Braille charts.
        # An IDLE dashboard must stop repainting them. We snapshot the
        # state_version we last painted aggregates for; if it hasn't advanced
        # (and we aren't force-painting for a pause/replay badge flip) we bail
        # before touching any widget. state_version only advances on a dirty
        # render tick, so this short-circuits a quiescent bus to ~0 work/sec.
        self._aggregates_version: int = -1

    # -------------------------------------------------------- compose -----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # nervous-bus-wutr: single-sentence cycle outcome banner sits at the
        # very top so the verdict for the most-recent session is the first
        # thing the operator sees.
        yield CycleOutcomeBanner(id="cycle-outcome")
        yield HeaderStats()
        # nervous-bus-m3so: slim queue-pressure bar lives in the header strip
        # so operators can spot MiniMax throttling without scanning a panel.
        yield QueuePressureBar(id="queue-pressure-bar")
        # nervous-bus-4cw9: "where are we" panel at the top — high prominence.
        yield IterationProgressPanel(id="iter-progress")
        # nervous-bus-4fz3: lineage strip below progress so the iter-by-iter
        # trajectory is visible without scrolling.
        yield IterationLineageStrip(id="iter-lineage")
        yield Input(placeholder="filter by session id (esc to clear)", id="filter-bar")
        # Divergence ribbon — interstitial that surfaces what changed at the
        # last iteration boundary (delta.diff) or where the LLM and rule-based
        # heuristic disagreed (improver.divergence). Thin, single-event-at-a-
        # time; the visual marker of "the system just changed itself." Sits
        # above the body so the boundary moment is the first thing the
        # operator sees once the header docks.
        yield DivergenceHighlights(id="divergence-ribbon")
        # nervous-bus-a5mx: the AHE prediction panel — our differentiator —
        # lives above the tracker so the staked claim is the headline read.
        yield AHEPredictionPanel(id="ahe-prediction-panel")
        # AHE prediction tracker — the visual win condition for "the system
        # caught itself being wrong." Sits above the body so it's the first
        # widget the operator sees once the header docks.
        yield AHEPredictionTracker(id="ahe-tracker")
        # nervous-bus-uwdq: multi-advocate population-cycle view. Hidden
        # (display:none) for single-advocate runs so the layout is unchanged;
        # surfaces N parallel advocate trajectories + a winner badge once a
        # autobench.population.summary.v1 cycle boundary lands.
        yield MultiAdvocatePanel(id="multi-advocate")
        # KernelArena (nervous-bus-tvfw): the reclaimed centre band — live
        # FunSearch kernel evolution. Leaderboard (what's evolving) + island
        # heatmap (how the island model behaves) + curiosity feed (when
        # something interesting happens).
        with Horizontal(id="kernel-arena"):
            yield KernelLeaderboard(id="kernel-leaderboard")
            yield IslandHeatmap(id="island-heatmap")
            yield CuriositySpikeFeed(id="curiosity-feed")
        with Container(id="body"):
            with Vertical(id="left"):
                yield SessionTree("autobench sessions")
                yield ScoreSpark([], summary_function=max)
                yield BurnGauge(budget=self._budget_per_min)
            with Vertical(id="right"):
                yield VerdictHistogram()
                yield WorkerLatencyHistogram()
                yield CEPatternPanel()
                yield ParetoScatter()
            with Vertical(id="failures"):
                yield FailureCodeSidebar(id="failure-sidebar")
                # FIX 3 (pulse_visual_richness_exploration_2026-05-16):
                # StderrFaultPanel slots into the right column above the cost
                # rate panel so CE/RE/TLE/MLE error excerpts surface next to
                # the generated-code preview rather than buried in the bus.
                yield StderrFaultPanel(id="stderr-fault-panel")
                # Cost & rate panel — always-visible bottom slot per bead
                # nervous-bus-cewj. Slots into the right column so it doesn't
                # crowd the session tree.
                yield CostRatePanel(id="cost-rate")
        yield Footer()

    # -------------------------------------------------------- on_mount ----
    def on_mount(self) -> None:
        # Initial render so the screen isn't blank if no events arrive
        self._tick_aggregates()
        self._tick_iter_progress()
        self._tick_divergence()
        self._tick_predictions()
        # Start the bus listener
        self.run_worker(self._bus_listener, thread=True, exclusive=True, group="bus")
        # Render tick (§9 amendment 7.5.5): single 10 Hz tick reads the dirty
        # flag set by the bus worker. Bus events never trigger refresh directly.
        self.set_interval(0.1, self._render_tick)
        # Charts coalesce to ≤2 Hz separately (§8.2 / k9s pattern).
        self.set_interval(0.5, self._tick_aggregates)

    # -------------------------------------------------------- worker ------
    def _bus_listener(self) -> None:
        """Pull events from the chosen source and apply them to state.

        Per §9 amendment: bus events ONLY mutate state and flip a dirty flag.
        They NEVER trigger widget refreshes directly — that's the render tick's
        job. This keeps the cross-thread hops at O(1)/sec regardless of how
        many events/sec arrive.
        """
        worker = get_current_worker()
        if self._replay_path is not None:
            source = ReplaySource(
                self._replay_path,
                session_id=self._replay_session_id,
                speed=self._replay_speed,
            )
        elif self._prefer_bus and BusSource.available():
            source = BusSource()
        else:
            source = FileSource(
                self._debug_file,
                follow=self._follow,
                from_start=self._from_start,
            )
        try:
            for evt in source.iter_events():
                if worker.is_cancelled:
                    break
                if self.paused:
                    continue
                self.state.apply(evt)
                self._state_dirty = True
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _render_tick(self) -> None:
        """10 Hz tick — reads the dirty flag set by the bus worker.

        The iteration-progress panel is refreshed on EVERY tick (not just
        dirty ticks) so its elapsed/ETA fields keep counting even when no
        new events have arrived — otherwise the panel would freeze between
        case completions, which feels broken.
        """
        self._tick_iter_progress()
        if not self._state_dirty:
            return
        self._state_dirty = False
        self.state_version += 1
        self._tick_tree()
        self._tick_failures()

    def _tick_failures(self) -> None:
        """Push the latest failure-ring snapshot to the sidebar.

        Cheap when nothing changed — the widget short-circuits on revision
        equality without re-rendering.
        """
        try:
            sidebar = self.query_one(FailureCodeSidebar)
            sidebar.set_cases(list(self.state.failure_cases), self.state.failure_revision)
        except Exception:
            pass

    # -------------------------------------------------------- ticks -------
    def _tick_aggregates(self, *, force: bool = False) -> None:
        """Refresh charts + header subtitle. Runs at ≤2 Hz (§8.2).

        Gated on ``state_version`` (perf): when no event has mutated state
        since the last aggregate paint we bail immediately, so an idle
        dashboard does ~0 work/sec instead of re-running ~14 query+recompute
        +repaint ops (incl. 3 plotext Braille charts) twice a second. The
        ``force`` path lets the pause toggle repaint the header badge even
        when state is otherwise quiescent.
        """
        if not force and self.state_version == self._aggregates_version:
            return
        self._aggregates_version = self.state_version
        try:
            hist = self.query_one(VerdictHistogram)
            hist.counts = dict(self.state.verdict_counts)
        except Exception:
            pass
        try:
            scatter = self.query_one(ParetoScatter)
            scatter.points = self.state.pareto_points()
        except Exception:
            pass
        try:
            gauge = self.query_one(BurnGauge)
            gauge.burn = self.state.burn_rate_per_min()
        except Exception:
            pass
        try:
            wlat = self.query_one(WorkerLatencyHistogram)
            # Snapshot the deque to a list so the reactive watcher receives a
            # stable, mutation-free view.
            wlat.latencies_ms = list(self.state.worker_latencies_ms)
            panel = self.query_one(CEPatternPanel)
            panel.patterns = self.state.top_failure_patterns(n=3)
            # nervous-bus-yn9v fix 3: feed the most-recent iter summary so the
            # empty state surfaces verdict data instead of dead space.
            panel.latest_summary = self.state.latest_completed_iteration_summary()
            cost_panel = self.query_one(CostRatePanel)
            cost_panel.payload = self.state.cost_summary()
        except Exception:
            pass
        try:
            stats = self.query_one(HeaderStats)
            pause_tag = "  [yellow][PAUSED][/]" if self.paused else ""
            # nervous-bus-zynw: REPLAY: <speed>x badge — only painted when
            # the global flag is set so live runs stay clean. Cyan accent
            # keeps it visually distinct from PAUSED (yellow).
            from .source import REPLAY_STATE
            if REPLAY_STATE.get("active"):
                replay_tag = (
                    f"  [bold cyan][REPLAY: "
                    f"{REPLAY_STATE.get('speed', 1.0):.1f}x][/]"
                )
            else:
                replay_tag = ""
            stats.text = self.state.summary_text() + pause_tag + replay_tag
        except Exception:
            pass
        # New panels — cheap snapshot pushes. Each is wrapped so a single
        # missing widget doesn't suppress the others.
        try:
            ahe_panel = self.query_one(AHEPredictionPanel)
            ahe_panel.payload = self.state.ahe_prediction_panel_payload()
        except Exception:
            pass
        # nervous-bus-uwdq: push the multi-advocate population-cycle snapshot.
        # Cheap (groups N advocate sessions); the panel hides itself when the
        # view is None / single-advocate so single-session runs pay nothing.
        try:
            multi = self.query_one(MultiAdvocatePanel)
            multi.payload = self.state.multi_advocate_view()
        except Exception:
            pass
        try:
            qbar = self.query_one(QueuePressureBar)
            qbar.payload = self.state.queue_pressure_summary()
        except Exception:
            pass
        try:
            strip = self.query_one(IterationLineageStrip)
            strip.cells = self.state.iteration_lineage()
        except Exception:
            pass
        # KernelArena fanout (nervous-bus-tvfw) — three cheap snapshot pushes.
        try:
            self.query_one(KernelLeaderboard).runs = self.state.kernel_leaderboard()
            self.query_one(IslandHeatmap).run = self.state.focused_kernel_run()
            self.query_one(CuriositySpikeFeed).spikes = self.state.curiosity_feed()
        except Exception:
            pass
        # nervous-bus-wutr: push the cycle-outcome banner payload. Cheap
        # (single dict construction); pushed on every aggregate tick so
        # the banner stays fresh as iterations land.
        try:
            banner = self.query_one(CycleOutcomeBanner)
            banner.payload = self.state.cycle_outcome_payload()
        except Exception:
            pass
        # FIX 3: push the rolling stderr ring into StderrFaultPanel. Snapshot
        # the deque so the widget renders from a stable list (the bus worker
        # may append concurrently). Revision is the monotonic counter the
        # state bumps on every append.
        try:
            panel = self.query_one(StderrFaultPanel)
            panel.set_entries(
                list(self.state.recent_stderr),
                self.state.stderr_revision,
            )
        except Exception:
            pass

    def _tick_iter_progress(self) -> None:
        """Push a fresh progress snapshot into IterationProgressPanel.

        Runs every render tick — cheap (dict copy + a few floats).
        """
        try:
            panel = self.query_one(IterationProgressPanel)
        except Exception:
            return
        snap = self.state.iteration_progress(iter_overhead_s=panel.iter_overhead_s)
        panel.progress = snap
    def _tick_divergence(self) -> None:
        """Push the latest divergence event into the ribbon widget.

        Cheap (one assignment); driven by the 10 Hz render tick so a
        delta.diff or divergence event lands on-screen within one frame.
        """
        try:
            ribbon = self.query_one(DivergenceHighlights)
        except Exception:
            return
        ribbon.event = self.state.latest_divergence_event

    def _tick_tree(self) -> None:
        """Refresh tree + sparkline (cheap)."""
        self._tick_divergence()
    def _tick_predictions(self) -> None:
        """Push the most recent predictions into the AHE tracker.

        Cheap (≤5 records); we run it from the 10 Hz render tick so a
        prediction emission is reflected next frame.
        """
        try:
            tracker = self.query_one(AHEPredictionTracker)
        except Exception:
            return
        records = self.state.recent_predictions(limit=5)
        progress: dict[tuple[str, int], tuple[int, int]] = {}
        for rec in records:
            try:
                progress[(rec.session_id, rec.iteration)] = (
                    self.state.prediction_case_progress(rec)
                )
            except Exception:
                continue
        tracker.case_progress = progress
        tracker.records = records

    def _tick_tree(self) -> None:
        """Refresh tree + sparkline (cheap)."""
        self._tick_predictions()
        try:
            tree = self.query_one(SessionTree)
            sessions = self.state.sessions_by_recency()
            if self.filter_text:
                needle = self.filter_text.lower()
                sessions = [s for s in sessions if needle in s.session_id.lower()]
            tree.rebuild(sessions)
        except Exception:
            pass
        # Score sparkline reflects most-recent session for now
        try:
            spark = self.query_one(ScoreSpark)
            recent = self.state.sessions_by_recency()
            if recent:
                scores = list(recent[0].scores)
                spark.data = scores or [0.0]
        except Exception:
            pass

    # -------------------------------------------------------- actions -----
    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        # Force header refresh immediately so the [PAUSED] tag shows up even
        # when the bus is idle (state_version hasn't advanced).
        self._tick_aggregates(force=True)

    def action_focus_filter(self) -> None:
        bar = self.query_one("#filter-bar", Input)
        bar.add_class("shown")
        bar.focus()

    def action_jump_top(self) -> None:
        try:
            self.query_one(SessionTree).action_cursor_top()
        except Exception:
            pass

    def action_jump_bottom(self) -> None:
        try:
            self.query_one(SessionTree).action_cursor_bottom()
        except Exception:
            pass

    def action_toggle_help(self) -> None:
        if self.screen_stack and isinstance(self.screen, HelpScreen):
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.filter_text = event.value
        bar = self.query_one("#filter-bar", Input)
        bar.remove_class("shown")
        self.set_focus(None)
        self._tick_tree()


# ---------------------------------------------------------------------------- #
# CLI helpers (re-exported by pulse_app.cli)                                   #
# ---------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulse-app",
        description="autobench-pulse v2 — live Textual dashboard.",
    )
    p.add_argument(
        "--debug-file",
        type=Path,
        default=DEFAULT_DEBUG_FILE,
        help=f"Path to debug JSONL (default: {DEFAULT_DEBUG_FILE})",
    )
    p.add_argument("--prefer-bus", action="store_true", help="Prefer `deer obs bus --json` over file tail")
    p.add_argument("--once", action="store_true", help="Read existing events once and exit (smoke test)")
    p.add_argument("--no-follow", action="store_true", help="Don't keep tailing after EOF")
    p.add_argument(
        "--budget-per-min",
        type=float,
        default=float(os.environ.get("PULSE_BUDGET_PER_MIN", "1.0")),
        help="Budget cap for the burn gauge ($/min)",
    )
    # nervous-bus-zynw: replay flags. --replay accepts either a JSONL path
    # OR a bare session_id (resolved against --debug-file). --speed clamps
    # to REPLAY_SPEED_CAP (100x) inside ReplaySource.
    p.add_argument(
        "--replay",
        type=str,
        default=None,
        help=(
            "Replay a finished session. Argument is either a JSONL file path "
            "OR a bare session_id (resolved against --debug-file)."
        ),
    )
    p.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help=(
            f"Replay speed multiplier (1x = real-time; capped at "
            f"{REPLAY_SPEED_CAP:.0f}x). Ignored when --replay is not set."
        ),
    )
    p.add_argument(
        "--graphics",
        choices=["auto", "unicode"],
        default="unicode",
        help=(
            "Graphics mode. Only 'unicode' is implemented in v2 (Braille via "
            "textual-plotext). 'auto' is reserved for the §7.11 stretch goal "
            "(textual-image TGP/Sixel). Per research §9 / probe p5, always-"
            "working escape hatch: --graphics=unicode."
        ),
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    follow = not args.no_follow and not args.once
    # --replay resolution: bare session_id → debug_file; literal path → as-is.
    replay_path: Optional[Path] = None
    replay_session_id: Optional[str] = None
    if args.replay:
        candidate = Path(args.replay)
        if candidate.exists() and candidate.is_file():
            replay_path = candidate
        else:
            # Treat as session_id filter against debug_file.
            replay_path = Path(args.debug_file)
            replay_session_id = args.replay
    app = PulseApp(
        debug_file=args.debug_file,
        prefer_bus=args.prefer_bus,
        once=args.once,
        budget_per_min=args.budget_per_min,
        from_start=True,
        follow=follow,
        replay_path=replay_path,
        replay_session_id=replay_session_id,
        replay_speed=args.speed,
    )
    if args.once:
        # Read events synchronously and dump a one-shot summary — no Textual UI.
        # This keeps the orchestrator's smoke-test path snappy and headless.
        return _run_once(args.debug_file)
    app.run()
    return 0


def _run_once(debug_file: Path) -> int:
    """Synchronous one-shot summary for ``--once`` mode (no TUI)."""
    state = PulseState()
    src = FileSource(Path(debug_file), follow=False, from_start=True)
    for evt in src.iter_events():
        state.apply(evt)
    print("autobench-pulse v2 — one-shot summary")
    print("=" * 60)
    print(state.summary_text())
    print()
    if not state.sessions:
        print("(no autobench sessions seen)")
        return 0
    for s in state.sessions_by_recency():
        print(f"  {s.session_id[:12]}  verdict={s.verdict}  "
              f"iters={len(s.iterations)}  cost=${s.cost_usd:.4f}")
        it = s.latest_iter
        if it is not None:
            verdict_str = ", ".join(f"{k}={v}" for k, v in sorted(it.verdict_counts.items()))
            print(f"    iter {it.iteration} {it.status}  "
                  f"score={it.aggregate_score}  cases={len(it.cases)}  "
                  f"verdicts=[{verdict_str}]")
    print()
    pts = state.pareto_points()
    if pts:
        print(f"pareto points: {len(pts)}  (cost, score) = {pts}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
