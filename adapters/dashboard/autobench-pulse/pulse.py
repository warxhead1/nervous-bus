#!/usr/bin/env python3
"""autobench-pulse — live tree-view of one (or many) autobench runs.

Subscribes to the four autobench observability channels:
    autobench.phase.v1
    autobench.iteration.v1
    autobench.sandbox.v1
    autobench.improver.v1

The script prefers ``deer obs bus --json`` when available, falling back to
tailing ``~/.cache/nervous-bus/debug.jsonl``.  On every event the screen is
cleared and a tree of active sessions is redrawn.

Examples:
    # live tail of all sessions
    pulse.py

    # offline replay from a captured debug file
    pulse.py --debug-file ~/.cache/nervous-bus/debug.jsonl --tail

    # only show one channel
    pulse.py --channel autobench.iteration.v1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Iterator

DEFAULT_DEBUG_FILE = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"

VALID_CHANNELS = {
    "autobench.phase.v1",
    "autobench.iteration.v1",
    "autobench.sandbox.v1",
    "autobench.improver.v1",
}

# ANSI ---------------------------------------------------------------------- #

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    GREY = "\033[90m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _color(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"{code}{text}{C.RESET}"


def _clear_screen() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")
    else:
        sys.stdout.write("\n" + "-" * 60 + "\n")


# --------------------------------------------------------------------------- #
# Session state — keyed by session_id
# --------------------------------------------------------------------------- #

class SessionState:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.started_at: float = time.time()
        # iteration_num → dict
        self.iterations: dict[int, dict[str, Any]] = {}
        # most recent activity time
        self.last_event: float = time.time()
        # active phases (phase_label → started_at)
        self.active_phases: dict[str, float] = {}
        # sandbox cases for the latest iteration
        self.recent_sandbox: deque[dict[str, Any]] = deque(maxlen=200)
        self.improver: dict[str, Any] | None = None

    def touch(self) -> None:
        self.last_event = time.time()


def _short_id(s: str, n: int = 8) -> str:
    return s[:n] if s else "?"


# --------------------------------------------------------------------------- #
# Renderer
# --------------------------------------------------------------------------- #

def _fmt_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def _verdict_glyph(verdict: str) -> str:
    if verdict == "OK":
        return _color("✓", C.GREEN)
    return _color("✗", C.RED)


def _verdict_color(verdict: str) -> str:
    if verdict == "OK":
        return _color(verdict, C.GREEN)
    if verdict in ("TLE", "MLE"):
        return _color(verdict, C.YELLOW)
    return _color(verdict, C.RED)


def render(sessions: dict[str, SessionState]) -> str:
    lines: list[str] = []
    if not sessions:
        lines.append(_color("(no autobench sessions seen yet — waiting...)", C.DIM))
        return "\n".join(lines)

    # Most recent session first
    ordered = sorted(sessions.values(), key=lambda s: s.last_event, reverse=True)
    for sess in ordered:
        age = time.time() - sess.started_at
        header = (
            f"┌─ autobench session: {_color(_short_id(sess.session_id, 12), C.BOLD + C.CYAN)}"
            f"  ({_color('started ' + _fmt_age(age) + ' ago', C.DIM)})"
        )
        lines.append(header)

        iter_keys = sorted(sess.iterations.keys())
        for idx, iter_num in enumerate(iter_keys):
            iter_info = sess.iterations[iter_num]
            last_in_session = idx == len(iter_keys) - 1
            elbow_top = "└─" if last_in_session else "├─"
            cont = "   " if last_in_session else "│  "

            status = iter_info.get("status", "?")
            score = iter_info.get("aggregate_score")
            prev = iter_info.get("prev_score")
            delta_str = ""
            if score is not None and prev is not None:
                delta = score - prev
                arrow = "→"
                delta_color = C.GREEN if delta >= 0 else C.RED
                delta_str = (
                    f"  {prev:.2f} {arrow} {score:.2f}  "
                    f"{_color('Δ' + ('+' if delta >= 0 else '') + f'{delta:.2f}', delta_color)}"
                )
            elif score is not None:
                delta_str = f"  score={score:.2f}"

            limit = iter_info.get("limit", "?")
            tag = ""
            if status == "start":
                tag = _color("  (in progress)", C.YELLOW)

            lines.append(
                f"│  {elbow_top} {_color(f'iteration {iter_num} / {limit}', C.BOLD)}"
                f"{delta_str}{tag}"
            )

            # Improver
            improver = iter_info.get("improver")
            if improver:
                model = improver.get("model", "?")
                pt = improver.get("prompt_tokens")
                ct = improver.get("completion_tokens")
                bits = [f"improver: {_color(model, C.MAGENTA)}"]
                if pt is not None:
                    bits.append(f"prompt={pt}t")
                if ct is not None:
                    bits.append(f"completion={ct}t")
                lines.append(f"│  {cont}├─ " + "  ".join(bits))

            # Benchmark / phases
            for ph_label, ph_started in iter_info.get("active_phases", {}).items():
                lines.append(
                    f"│  {cont}├─ phase: {_color(ph_label, C.BLUE)}  "
                    f"{_color(_fmt_age(time.time() - ph_started) + ' elapsed', C.DIM)}"
                )

            # Sandbox cases
            cases = iter_info.get("cases", [])
            if cases:
                bench_name = iter_info.get("bench_name", "")
                header_label = "benchmark"
                if bench_name:
                    header_label += f": {bench_name}"
                lines.append(f"│  {cont}├─ {header_label}")
                ok_count = sum(1 for c in cases if c.get("verdict") == "OK")
                fail_count = sum(1 for c in cases if c.get("verdict") and c.get("verdict") != "OK")
                pending = [c for c in cases if not c.get("verdict")]

                shown = cases[-20:]
                for j, case in enumerate(shown):
                    is_last_case = (j == len(shown) - 1) and not pending
                    case_elbow = "└─" if is_last_case else "├─"
                    cid = case.get("case_id", "?")
                    v = case.get("verdict") or "..."
                    ms = case.get("latency_ms")
                    sb = case.get("sandbox_type", "")
                    ms_s = f"{ms:.0f}ms" if isinstance(ms, (int, float)) else "    "
                    glyph = _verdict_glyph(v) if v != "..." else _color("·", C.DIM)
                    v_col = _verdict_color(v) if v != "..." else _color(v, C.DIM)
                    lines.append(
                        f"│  {cont}│   {case_elbow} {glyph} {cid:<14} {v_col:<5} {ms_s:>7} {_color(sb, C.DIM)}"
                    )
                # Pending / running line
                if pending:
                    last = pending[-1]
                    lines.append(
                        f"│  {cont}│   └─ {_color('· running', C.DIM)} "
                        f"{last.get('case_id', '?')} "
                        f"{_color(last.get('language', ''), C.DIM)} "
                        f"{_color(last.get('sandbox_type', ''), C.DIM)}"
                    )
                if len(cases) > len(shown):
                    extra = len(cases) - len(shown)
                    lines.append(
                        f"│  {cont}│   {_color(f'... ({extra} more, {ok_count} OK / {fail_count} fail)', C.DIM)}"
                    )
                else:
                    lines.append(
                        f"│  {cont}│   {_color(f'{ok_count} OK / {fail_count} fail', C.DIM)}"
                    )

            # Pareto
            pareto = iter_info.get("pareto_configs")
            if pareto is not None:
                lines.append(f"│  {cont}└─ pareto: {pareto} frontier configs")

        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Event ingestion
# --------------------------------------------------------------------------- #

def _ensure_session(sessions: dict[str, SessionState], sid: str) -> SessionState:
    if sid not in sessions:
        sessions[sid] = SessionState(sid)
    return sessions[sid]


def _ensure_iteration(sess: SessionState, num: int) -> dict[str, Any]:
    if num not in sess.iterations:
        sess.iterations[num] = {
            "status": "start",
            "cases": [],
            "active_phases": {},
        }
    return sess.iterations[num]


def _last_iter(sess: SessionState) -> dict[str, Any] | None:
    if not sess.iterations:
        return None
    return sess.iterations[max(sess.iterations.keys())]


def ingest_event(sessions: dict[str, SessionState], event: dict[str, Any]) -> None:
    """Update session state from one CloudEvents envelope."""
    ev_type = event.get("type")
    data = event.get("data") or {}
    sid = data.get("session_id")
    if not sid or ev_type not in VALID_CHANNELS:
        return

    sess = _ensure_session(sessions, sid)
    sess.touch()

    if ev_type == "autobench.iteration.v1":
        num = data.get("iteration", 0)
        info = _ensure_iteration(sess, num)
        info["status"] = data.get("status", info["status"])
        info["harness_version"] = data.get("harness_version", info.get("harness_version", ""))
        if data.get("status") == "complete":
            new_score = data.get("aggregate_score")
            # find previous score
            prev_num = max((k for k in sess.iterations if k < num), default=None)
            if prev_num is not None:
                prev_score = sess.iterations[prev_num].get("aggregate_score")
                info["prev_score"] = prev_score
            info["aggregate_score"] = new_score
            info["verdict_counts"] = data.get("verdict_counts", {})
            info["improvement_delta"] = data.get("improvement_delta")
    elif ev_type == "autobench.phase.v1":
        phase = data.get("phase", "?")
        status = data.get("status")
        # phase events don't carry iteration; attach to latest active iteration
        info = _last_iter(sess)
        if info is None:
            info = _ensure_iteration(sess, 0)
        if status == "start":
            info["active_phases"][phase] = time.time()
        else:
            info["active_phases"].pop(phase, None)
    elif ev_type == "autobench.sandbox.v1":
        case_id = data.get("case_id", "?")
        status = data.get("status")
        info = _last_iter(sess) or _ensure_iteration(sess, 0)
        cases = info["cases"]
        # find or append
        existing = next((c for c in cases if c.get("case_id") == case_id), None)
        if existing is None:
            existing = {"case_id": case_id}
            cases.append(existing)
        existing["language"] = data.get("language", existing.get("language", ""))
        existing["sandbox_type"] = data.get("sandbox_type", existing.get("sandbox_type", ""))
        if status == "complete":
            existing["verdict"] = data.get("verdict")
            existing["latency_ms"] = data.get("latency_ms")
            existing["exit_code"] = data.get("exit_code")
    elif ev_type == "autobench.improver.v1":
        info = _last_iter(sess) or _ensure_iteration(sess, 0)
        improver = info.get("improver") or {}
        improver["model"] = data.get("model", improver.get("model"))
        if data.get("status") == "start":
            improver["prompt_tokens"] = data.get("prompt_tokens")
        else:
            improver["completion_tokens"] = data.get("completion_tokens")
            improver["delta_summary"] = data.get("delta_summary")
        info["improver"] = improver


# --------------------------------------------------------------------------- #
# Event sources
# --------------------------------------------------------------------------- #

def _iter_file_tail(path: Path, follow: bool, from_start: bool) -> Iterator[dict[str, Any]]:
    """Yield events from a JSONL file. If follow=True, keep tailing."""
    if not path.exists():
        if not follow:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    # Read everything first
    with open(path, "r") as fh:
        if not from_start and follow:
            fh.seek(0, os.SEEK_END)
        while True:
            line = fh.readline()
            if not line:
                if not follow:
                    return
                time.sleep(0.25)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _iter_deer_obs_bus() -> Iterator[dict[str, Any]] | None:
    """Try to spawn ``deer obs bus --json`` as a source. Returns None if unavailable."""
    if not shutil.which("deer"):
        return None
    import subprocess
    try:
        proc = subprocess.Popen(
            ["deer", "obs", "bus", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=1,
            text=True,
        )
    except Exception:
        return None

    def gen() -> Iterator[dict[str, Any]]:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    return gen()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulse.py",
        description="Live tree view of autobench observability events.",
    )
    p.add_argument(
        "--debug-file",
        type=Path,
        default=DEFAULT_DEBUG_FILE,
        help=f"Path to debug JSONL file (default: {DEFAULT_DEBUG_FILE})",
    )
    p.add_argument(
        "--tail",
        action="store_true",
        help="Tail the debug file from the start (offline replay).",
    )
    p.add_argument(
        "--follow",
        action="store_true",
        help="Follow the source forever (default in live mode).",
    )
    p.add_argument(
        "--channel",
        action="append",
        default=None,
        help="Only show events on this channel (repeatable). Default: all four.",
    )
    p.add_argument(
        "--no-deer",
        action="store_true",
        help="Skip the deer-obs-bus source even when available.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Read existing events, render once, exit (used in tests).",
    )
    return p


def _legacy_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Channel filter
    filter_channels: set[str] | None = None
    if args.channel:
        filter_channels = set(args.channel)
        invalid = filter_channels - VALID_CHANNELS
        if invalid:
            print(f"warning: unknown channels: {sorted(invalid)}", file=sys.stderr)

    sessions: dict[str, SessionState] = {}

    # Pick source
    if args.tail or args.once:
        from_start = True
        follow = args.follow and not args.once
        source = _iter_file_tail(args.debug_file, follow=follow, from_start=from_start)
    else:
        deer_source = None if args.no_deer else _iter_deer_obs_bus()
        if deer_source is not None:
            source = deer_source
        else:
            source = _iter_file_tail(args.debug_file, follow=True, from_start=False)

    last_render = 0.0
    RENDER_MIN_INTERVAL = 0.05  # seconds — debounce
    rendered_once = False

    try:
        for event in source:
            ev_type = event.get("type")
            if filter_channels and ev_type not in filter_channels:
                continue
            if ev_type not in VALID_CHANNELS:
                continue
            ingest_event(sessions, event)

            now = time.time()
            if now - last_render >= RENDER_MIN_INTERVAL:
                _clear_screen()
                sys.stdout.write(render(sessions) + "\n")
                sys.stdout.flush()
                last_render = now
                rendered_once = True

            if args.once:
                # consume any remaining buffered events but don't block
                continue
    except KeyboardInterrupt:
        pass

    # Final render (so --once always prints something)
    if args.once or not rendered_once:
        _clear_screen()
        sys.stdout.write(render(sessions) + "\n")
        sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------- #
# v2 cutover shim                                                              #
# --------------------------------------------------------------------------- #
#
# Per terminal_rendering_2026.md §7.10: `pulse.py` becomes a thin shim. Default
# routes into `pulse_app`; `--legacy` keeps the v1 renderer for one release.
#
# `--once` is preserved on the v1 path because the orchestrator uses it for
# smoke-tests with deterministic ANSI output. New callers should prefer
# `python -m pulse_app --once` (synchronous summary) — but the legacy path is
# always available behind `--legacy --once`.


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    use_legacy = False
    if "--legacy" in argv:
        use_legacy = True
        argv.remove("--legacy")

    if use_legacy:
        return _legacy_main(argv)

    # Route to pulse_app v2 — import lazily so the legacy path keeps working
    # even if Textual isn't installed.
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    try:
        from pulse_app.cli import main as v2_main  # type: ignore
    except ImportError as e:
        print(
            f"autobench-pulse v2 (pulse_app) is unavailable: {e}\n"
            f"Falling back to the legacy renderer. Install with:\n"
            f"    pip install -r {here / 'requirements.txt'}\n"
            f"Or invoke explicitly:  python pulse.py --legacy",
            file=sys.stderr,
        )
        return _legacy_main(argv)
    return v2_main(argv)


if __name__ == "__main__":
    sys.exit(main())
