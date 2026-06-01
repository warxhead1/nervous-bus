#!/usr/bin/env python3
"""Snapshot the autobench-pulse state from a JSONL debug file.

Demonstrates the post-beads (9l69, sm8n, wutr, zynw, yn9v) state queries:

  * ``cycle_outcome_payload()``           — single-sentence banner payload
  * ``pareto_classified()``               — frontier vs dominated split
  * ``summary_text()``                    — header line (now with
                                            "all-sessions $" label so it
                                            disambiguates from the banner's
                                            "this cycle" cost)
  * ``iteration_progress()``              — IterationProgressPanel snapshot,
                                            now carrying ``history_snapshot``
                                            + ``aggregate_score`` so the
                                            complete-state panel collapses
                                            to a one-line "Iter N: X/Y ·
                                            score · {OK:.., WA:.., CE:..}"
                                            summary (yn9v fix 1+2)
  * ``ahe_prediction_panel_payload()``    — now session-scoped history dots
                                            + ``history_dots_scope`` so a
                                            fresh cycle's first prediction
                                            doesn't inherit prior-session
                                            dots (yn9v fix 6)
  * ``latest_completed_iteration_summary()`` — feeds the CEPatternPanel
                                            empty-state verdict roll-up
                                            (yn9v fix 3)
  * ``REPLAY_STATE``                      — global replay-mode flag (toggled
                                            when invoked with --replay-demo
                                            to prove the badge wiring)

Usage:

    python tools/pulse_snapshot.py [path/to/debug.jsonl]
    python tools/pulse_snapshot.py --replay-demo

Default path: ~/.cache/nervous-bus/debug.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the pulse_app package importable without an editable install.
HERE = Path(__file__).resolve().parent
PULSE_DIR = HERE.parent / "adapters" / "dashboard" / "autobench-pulse"
if str(PULSE_DIR) not in sys.path:
    sys.path.insert(0, str(PULSE_DIR))

from pulse_app.source import (  # noqa: E402
    DEFAULT_DEBUG_FILE,
    FileSource,
    REPLAY_STATE,
    set_replay_state,
)
from pulse_app.state import PulseState  # noqa: E402
from pulse_app.widgets import CycleOutcomeBanner  # noqa: E402


def dump(state: PulseState) -> None:
    print("=" * 70)
    print("pulse snapshot — header / banner / pareto")
    print("=" * 70)
    print(f"summary_text()      = {state.summary_text()!r}")
    payload = state.cycle_outcome_payload()
    if payload is None:
        print("cycle_outcome      = (no sessions)")
    else:
        print(f"cycle_outcome      = {payload}")
        markup = CycleOutcomeBanner.render_markup(payload)
        print(f"  banner markup   = {markup}")
    classified = state.pareto_classified()
    print(f"pareto frontier    = {classified['frontier']}")
    print(f"pareto dominated   = {classified['dominated']}")
    print(f"REPLAY_STATE       = {dict(REPLAY_STATE)}")
    # ---- nervous-bus-yn9v post-fix surfaces -------------------------------
    print()
    print("-" * 70)
    print("yn9v post-fix queries")
    print("-" * 70)
    progress = state.iteration_progress()
    print(f"iteration_progress = {progress}")
    ahe = state.ahe_prediction_panel_payload()
    if ahe is None:
        print("ahe_panel_payload  = (no predictions)")
    else:
        # Compact dump: prediction → its session/iter/status, plus the
        # session-scoped dots and cross-session annotation.
        rec = ahe["prediction"]
        print(
            "ahe_panel_payload  = "
            f"sid={getattr(rec, 'session_id', '?')[-12:]} "
            f"iter={getattr(rec, 'iteration', '?')} "
            f"status={getattr(rec, 'status', '?')} "
            f"watermark={ahe.get('watermark')} "
            f"dots={ahe.get('history_dots')} "
            f"scope={ahe.get('history_dots_scope')}"
        )
    latest_iter = state.latest_completed_iteration_summary()
    print(f"latest_iter_summary = {latest_iter}")


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if "--replay-demo" in argv:
        set_replay_state(True, speed=50.0)
        argv.remove("--replay-demo")
    path = Path(argv[0]) if argv else DEFAULT_DEBUG_FILE
    state = PulseState()
    if path.exists():
        src = FileSource(path, follow=False, from_start=True)
        for evt in src.iter_events():
            state.apply(evt)
    else:
        # Synthesize a tiny session so the demo always shows something.
        sid = "01PULSE_SNAPSHOT_DEMO_ABCD"
        for i, sc in enumerate([0.40, 0.65]):
            state.apply({
                "type": "autobench.worker.v1",
                "data": {"session_id": sid, "cost_usd": 0.05, "latency_ms": 100.0},
            })
            state.apply({
                "type": "autobench.iteration.v1",
                "data": {"session_id": sid, "iteration": i, "harness_version": "v0",
                         "status": "complete", "aggregate_score": sc,
                         "verdict_counts": {"OK": 2}},
            })
            state.apply({
                "type": "autobench.iteration.summary.v1",
                "data": {"session_id": sid, "iteration": i, "num_cases": 2,
                         "aggregate_score": sc, "verdict_distribution": {"OK": 2}},
            })
    dump(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
