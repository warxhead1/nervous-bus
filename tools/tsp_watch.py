#!/usr/bin/env python3
"""tsp_watch.py — live tail / batch summary for TSP auto-kernel event stream.

Reads ``~/.cache/nervous-bus/debug.jsonl`` (or a custom path via ``--log``) and
presents either a compact historical summary of all runs (default / ``--once``)
or a live-tail that prints new events as they land (``--follow``).

Event source: CloudEvents-lite JSONL emitted by ``/autobench/tsp_kernel``.
Handles both direct envelopes (outer type = ``tsp.*``) and double-enveloped
events where ``envelope["data"]`` is itself a full CloudEvents dict.

Usage::

    python3 tools/tsp_watch.py                         # all runs, compact table
    python3 tools/tsp_watch.py --run-id 01KSVNCW...    # single run
    python3 tools/tsp_watch.py --follow                 # live tail, Ctrl-C to exit
    python3 tools/tsp_watch.py --log /tmp/alt.jsonl     # override log path
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_LOG = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"
POLL_INTERVAL = 2  # seconds, for --follow

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


# During the kernel-unification merge window we accept BOTH the legacy
# per-domain ``tsp.*`` channels and the new unified ``kernel.*`` channels
# (carrying ``data.domain == "tsp"``). Event-segment suffixes are shared:
# ``kernel.started`` / ``kernel.completed`` / ``generation.completed`` /
# ``candidate.evaluated`` (and the others). We normalise a raw event type to
# its canonical legacy ``tsp.<suffix>`` form so the rest of the watcher is
# unchanged.

# Suffix (event segment) -> canonical legacy tsp.* type used internally.
_TSP_EVENT_SUFFIXES = {
    "kernel.started": "tsp.kernel.started.v1",
    "kernel.completed": "tsp.kernel.completed.v1",
    "generation.completed": "tsp.generation.completed.v1",
    "candidate.evaluated": "tsp.candidate.evaluated.v1",
    "best_fitness_improved": "tsp.best_fitness_improved.v1",
    "island_reset": "tsp.island_reset.v1",
    "plateau_hint": "tsp.plateau_hint.v1",
    "prior.loaded": "tsp.prior.loaded.v1",
    "prior.updated": "tsp.prior.updated.v1",
}


def _normalize_kernel_type(event_type: str, data: dict) -> str | None:
    """Return a canonical legacy ``tsp.<suffix>.v1`` type for a tsp-domain
    kernel event, or ``None`` if the event is not a tsp kernel event.

    Accepts both legacy ``tsp.*`` and unified ``kernel.*`` channels. For the
    unified channels we only treat them as tsp events when ``data.domain`` is
    absent (legacy double-envelope) or equals ``"tsp"``.
    """
    if event_type.startswith("tsp."):
        return event_type
    if event_type.startswith("kernel.") and event_type.endswith(".v1"):
        domain = data.get("domain")
        if domain not in (None, "tsp"):
            return None
        suffix = event_type[len("kernel."):-len(".v1")]
        return _TSP_EVENT_SUFFIXES.get(suffix)
    return None


def _unwrap(envelope: dict) -> tuple[str, dict]:
    """Return (event_type, data_dict) for a raw envelope line.

    Handles double-enveloped events where data itself is a CloudEvents dict
    with a ``type`` field starting with ``tsp.`` or ``kernel.``.
    """
    t = envelope.get("type", "")
    d = envelope.get("data", {})
    if isinstance(d, dict) and (
        d.get("type", "").startswith("tsp.")
        or d.get("type", "").startswith("kernel.")
    ):
        # double-envelope: inner CloudEvents payload
        t = d.get("type", t)
        d = d.get("data", {})
    return t, d if isinstance(d, dict) else {}


def iter_tsp_events(lines: list[str]) -> Iterator[tuple[str, dict]]:
    """Yield (canonical_event_type, data_dict) for every parseable tsp kernel
    line, accepting both legacy ``tsp.*`` and unified ``kernel.*`` channels."""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        t, d = _unwrap(ev)
        canonical = _normalize_kernel_type(t, d)
        if canonical is not None:
            yield canonical, d


# ---------------------------------------------------------------------------
# Data model — one RunState per run_id
# ---------------------------------------------------------------------------


class RunState:
    """Accumulates events for a single TSP run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.started: dict | None = None          # data from kernel.started
        self.completed: dict | None = None        # data from kernel.completed
        # generation rows keyed by generation number (deduped, keep-last)
        self._gen_rows: dict[int, dict] = {}
        # candidate count per generation (deduped by program_id)
        self._candidates: dict[str, dict] = {}

    # -- ingestion -----------------------------------------------------------

    def ingest(self, event_type: str, data: dict) -> None:
        if event_type == "tsp.kernel.started.v1":
            self.started = data
        elif event_type == "tsp.kernel.completed.v1":
            self.completed = data
        elif event_type == "tsp.generation.completed.v1":
            gen = data.get("generation")
            if gen is not None:
                self._gen_rows[gen] = data
        elif event_type == "tsp.candidate.evaluated.v1":
            pid = data.get("program_id")
            if pid:
                self._candidates[pid] = data

    # -- derived accessors ---------------------------------------------------

    def generation_table(self) -> list[dict]:
        """One row per generation, most complete data available.

        For completed runs: prefer ``history[]`` from the completed event
        (has gen_seconds, eval_seconds, llm_requests).
        Fall back to generation.completed events for in-progress runs
        (those columns will be None).
        """
        if self.completed:
            rows = []
            for h in self.completed.get("history", []):
                gen = h.get("generation")
                rows.append({
                    "generation": gen,
                    "best_fitness": h.get("best_fitness"),
                    "mean_pop_fitness": h.get("mean_pop_fitness"),
                    "llm_requests": h.get("llm_requests"),
                    "gen_seconds": h.get("gen_seconds"),
                    "eval_seconds": h.get("eval_seconds"),
                    "best_program_id": h.get("best_id") or h.get("best_program_id"),
                    "best_island": h.get("best_island"),
                })
            # If history is empty (some completed events omit it), fall back
            if not rows:
                rows = self._gen_rows_sorted()
            return rows
        return self._gen_rows_sorted()

    def _gen_rows_sorted(self) -> list[dict]:
        rows = []
        for gen_num in sorted(self._gen_rows):
            d = self._gen_rows[gen_num]
            rows.append({
                "generation": gen_num,
                "best_fitness": d.get("best_fitness"),
                "mean_pop_fitness": d.get("mean_pop_fitness"),
                "llm_requests": d.get("llm_requests"),
                "gen_seconds": d.get("gen_seconds"),
                "eval_seconds": d.get("eval_seconds"),
                "best_program_id": d.get("best_program_id"),
                "best_island": d.get("best_island"),
            })
        return rows

    @property
    def config(self) -> dict:
        return self.started or {}

    @property
    def is_complete(self) -> bool:
        return self.completed is not None


# ---------------------------------------------------------------------------
# Batch summary (--once)
# ---------------------------------------------------------------------------


def _fmt(val: object, fmt: str = "", width: int = 0) -> str:
    if val is None:
        s = "-"
    elif fmt:
        try:
            s = format(float(val), fmt)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            s = str(val)
    else:
        s = str(val)
    return s.rjust(width) if width else s


def print_run_summary(rs: RunState) -> None:
    cfg = rs.config
    run_id = rs.run_id

    # -- header --------------------------------------------------------------
    print()
    print("=" * 72)
    print(f"run  {run_id}")
    print("=" * 72)

    instances = cfg.get("instances", [])
    n_islands = cfg.get("n_islands", "-")
    pop = cfg.get("population_per_island", "-")
    gens_cfg = cfg.get("generations", "-")
    sandbox = cfg.get("sandbox_type", "-")
    print(
        f"  instances={','.join(instances) or '-'}  "
        f"islands={n_islands}  pop/island={pop}  "
        f"max_gens={gens_cfg}  sandbox={sandbox}"
    )

    rows = rs.generation_table()
    cand_count = len(rs._candidates)
    if cand_count:
        print(f"  candidates evaluated: {cand_count}")

    if not rows:
        if rs.is_complete:
            print("  (completed — no generation history available)")
        else:
            print("  (no generation events yet)")
    else:
        # -- generation table -----------------------------------------------
        col_w = {"gen": 4, "best": 8, "mean": 8, "reqs": 5, "gen_s": 7, "eval_s": 7}
        hdr = (
            f"  {'gen':>{col_w['gen']}} | "
            f"{'best':>{col_w['best']}} | "
            f"{'mean':>{col_w['mean']}} | "
            f"{'reqs':>{col_w['reqs']}} | "
            f"{'gen_s':>{col_w['gen_s']}} | "
            f"{'eval_s':>{col_w['eval_s']}}"
        )
        sep = "  " + "-" * (len(hdr) - 2)
        print(hdr)
        print(sep)
        for row in rows:
            print(
                f"  {_fmt(row['generation'], width=col_w['gen'])} | "
                f"{_fmt(row['best_fitness'], '.4f', col_w['best'])} | "
                f"{_fmt(row['mean_pop_fitness'], '.4f', col_w['mean'])} | "
                f"{_fmt(row['llm_requests'], width=col_w['reqs'])} | "
                f"{_fmt(row['gen_seconds'], '.1f', col_w['gen_s'])} | "
                f"{_fmt(row['eval_seconds'], '.1f', col_w['eval_s'])}"
            )

    # -- completion block ---------------------------------------------------
    if rs.completed:
        c = rs.completed
        bp = c.get("best_program") or {}
        total = c.get("total_generations", "-")
        stop = c.get("stop_reason", "-")
        total_reqs = c.get("llm_requests", "-")
        print(f"  stop_reason : {stop}")
        print(f"  total_gens  : {total}  llm_requests={total_reqs}")
        if bp:
            print(
                f"  best        : id={bp.get('id','-')}  "
                f"fitness={bp.get('fitness',0):.4f}  "
                f"island={bp.get('island','-')}  "
                f"gen={bp.get('generation','-')}  "
                f"source={bp.get('source','-')}"
            )


def load_all_runs(log_path: Path, run_id_filter: str | None) -> dict[str, RunState]:
    """Parse entire file; return ordered dict run_id -> RunState."""
    runs: dict[str, RunState] = {}
    if not log_path.exists():
        return runs
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return runs

    for t, d in iter_tsp_events(lines):
        rid = d.get("run_id")
        if not rid:
            continue
        if run_id_filter and rid != run_id_filter:
            continue
        if rid not in runs:
            runs[rid] = RunState(rid)
        runs[rid].ingest(t, d)
    return runs


def cmd_once(args: argparse.Namespace) -> int:
    log_path = Path(args.log).expanduser()
    runs = load_all_runs(log_path, args.run_id)

    if not runs:
        if not log_path.exists():
            print(f"no log file found at {log_path}", file=sys.stderr)
        else:
            print("no tsp.* events found in log.")
        return 0

    for rs in runs.values():
        print_run_summary(rs)

    print()
    completed = sum(1 for rs in runs.values() if rs.is_complete)
    print(f"--- {len(runs)} run(s), {completed} completed ---")
    return 0


# ---------------------------------------------------------------------------
# Follow mode (--follow)
# ---------------------------------------------------------------------------


def print_gen_event(run_id: str, data: dict) -> None:
    gen = data.get("generation", "?")
    best = data.get("best_fitness")
    mean = data.get("mean_pop_fitness")
    island = data.get("best_island", "?")
    best_s = f"{best:.4f}" if best is not None else "-"
    mean_s = f"{mean:.4f}" if mean is not None else "-"
    # truncate run_id for display
    rid_short = run_id[-16:] if len(run_id) > 16 else run_id
    print(
        f"[gen] {rid_short}  gen={gen:>3}  best={best_s}  mean={mean_s}  island={island}"
    )


def print_completed_event(run_id: str, data: dict) -> None:
    bp = data.get("best_program") or {}
    stop = data.get("stop_reason", "-")
    total = data.get("total_generations", "?")
    reqs = data.get("llm_requests", "-")
    fit = bp.get("fitness", 0)
    rid_short = run_id[-16:] if len(run_id) > 16 else run_id
    print(
        f"[done] {rid_short}  total_gens={total}  llm_reqs={reqs}"
        f"  best_fitness={fit:.4f}  stop={stop}"
    )


def cmd_follow(args: argparse.Namespace) -> int:
    log_path = Path(args.log).expanduser()
    run_id_filter: str | None = args.run_id

    # Start at current EOF
    offset = log_path.stat().st_size if log_path.exists() else 0
    print(f"Following {log_path} (offset={offset})  Ctrl-C to exit", file=sys.stderr)

    try:
        while True:
            if not log_path.exists():
                time.sleep(POLL_INTERVAL)
                continue

            size = log_path.stat().st_size
            if size < offset:
                # file truncated / rotated — reset
                offset = 0

            if size == offset:
                time.sleep(POLL_INTERVAL)
                continue

            with log_path.open(errors="replace") as fh:
                fh.seek(offset)
                chunk = fh.read(size - offset)
                offset = fh.tell()

            lines = chunk.splitlines()
            for t, d in iter_tsp_events(lines):
                rid = d.get("run_id")
                if not rid:
                    continue
                if run_id_filter and rid != run_id_filter:
                    continue
                if t == "tsp.generation.completed.v1":
                    print_gen_event(rid, d)
                elif t == "tsp.kernel.completed.v1":
                    print_completed_event(rid, d)
                elif t == "tsp.kernel.started.v1":
                    cfg_parts = (
                        f"islands={d.get('n_islands','-')}"
                        f"  pop={d.get('population_per_island','-')}"
                        f"  sandbox={d.get('sandbox_type','-')}"
                    )
                    rid_short = rid[-16:] if len(rid) > 16 else rid
                    print(f"[start] {rid_short}  {cfg_parts}")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Watch the nervous-bus TSP auto-kernel event stream."
    )
    ap.add_argument(
        "--follow",
        action="store_true",
        help="Poll the log file every 2s and print new events as they arrive.",
    )
    ap.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="(default) Parse existing events and print a per-run summary table.",
    )
    ap.add_argument(
        "--run-id",
        metavar="ULID",
        default=None,
        help="Filter output to a single run_id.",
    )
    ap.add_argument(
        "--log",
        metavar="PATH",
        default=str(DEFAULT_LOG),
        help=f"Path to the bus debug log (default: {DEFAULT_LOG}).",
    )
    args = ap.parse_args(argv)

    if args.follow:
        return cmd_follow(args)
    return cmd_once(args)


if __name__ == "__main__":
    sys.exit(main())
