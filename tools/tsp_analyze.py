#!/usr/bin/env python3
"""tsp_analyze.py — digest + understand TSP auto-kernel runs.

Reads a results JSON written by ``TSPKernel.save_results`` (and, optionally, the
nervous-bus ``tsp.*`` event stream) and produces:

  - a printed summary (generations, MiniMax requests, phase timings, best),
  - a fitness trajectory plot (best + mean population fitness per generation),
  - a phase-timing plot (generation vs evaluation wall-clock per generation),
  - a source breakdown (how llm / mutated / baseline / migrated programs fare).

This is the "Python for plotting/understanding/aggregation" layer of the auto-
kernel vision: turn the fire-hose of tsp.* events + run artifacts into a few
legible pictures and numbers.

Usage:
    python3 tools/tsp_analyze.py /tmp/tsp_run/results_gen10.json
    python3 tools/tsp_analyze.py results.json --outdir /tmp/plots
    python3 tools/tsp_analyze.py results.json --bus   # also fold in tsp.* events
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402

BUS_LOG = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"


def load_results(path: Path) -> dict:
    data = json.loads(path.read_text())
    if "history" not in data:
        raise ValueError(f"{path} does not look like a TSP results file (no 'history')")
    return data


def load_bus_events(run_id: str | None) -> list[dict]:
    """Load tsp.candidate.evaluated events from the bus debug log (best-effort)."""
    if not BUS_LOG.exists():
        return []
    events = []
    for line in BUS_LOG.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or "tsp.candidate.evaluated" not in line:
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            continue
        # The CLI may double-envelope; unwrap to the inner data payload.
        data = env.get("data", {})
        if isinstance(data, dict) and data.get("type", "").startswith("tsp."):
            data = data.get("data", {})
        if not isinstance(data, dict) or "fitness" not in data:
            continue
        if run_id and data.get("run_id") != run_id:
            continue
        events.append(data)
    return events


def print_summary(data: dict, bus_events: list[dict]) -> None:
    history = data["history"]
    cfg = data.get("config", {})
    best = data.get("best_program") or {}
    print("=" * 64)
    print("TSP run summary")
    print("=" * 64)
    print(f"  instances:       {cfg.get('instances')}")
    print(f"  islands x pop:   {cfg.get('n_islands')} x {cfg.get('population_per_island')}")
    print(f"  generations:     {len(history)}")
    if history and "llm_requests" in history[-1]:
        print(f"  MiniMax requests: {history[-1]['llm_requests']} (billing unit: requests)")
    if history and "gen_seconds" in history[0]:
        gen_t = sum(h.get("gen_seconds", 0) for h in history)
        eval_t = sum(h.get("eval_seconds", 0) for h in history)
        print(f"  phase wall-clock: generation={gen_t:.1f}s  evaluation={eval_t:.1f}s")
    if best:
        print(f"  best program:    {best.get('id')}  fitness={best.get('fitness', 0):.4f}")
    if history:
        first_b = history[0]["best_fitness"]
        last_b = history[-1]["best_fitness"]
        delta = (last_b - first_b) / first_b * 100 if first_b else 0.0
        print(f"  best fitness:    {first_b:.4f} -> {last_b:.4f}  ({delta:+.1f}%)")
    if bus_events:
        print(f"  bus candidate events: {len(bus_events)}")


def source_breakdown(data: dict, bus_events: list[dict]) -> dict[str, dict]:
    """Aggregate count + best/mean fitness per program source."""
    rows = list(data.get("top_programs", []))
    # Prefer the (larger) bus candidate set when available.
    if bus_events:
        rows = bus_events
    agg: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        agg[r.get("source", "unknown")].append(float(r.get("fitness", 0.0)))
    out = {}
    for src, fits in sorted(agg.items()):
        out[src] = {
            "count": len(fits),
            "best": max(fits) if fits else 0.0,
            "mean": sum(fits) / len(fits) if fits else 0.0,
        }
    return out


def plot_fitness(history: list[dict], outdir: Path) -> Path:
    gens = [h["generation"] for h in history]
    best = [h["best_fitness"] for h in history]
    mean = [h.get("mean_pop_fitness", h["best_fitness"]) for h in history]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(gens, best, "o-", label="best fitness", color="#2b8cbe")
    ax.plot(gens, mean, "s--", label="mean island best", color="#a6bddb")
    ax.axhline(1.0, color="grey", lw=0.8, ls=":", label="optimal (1.0)")
    ax.set_xlabel("generation")
    ax.set_ylabel("approximation ratio (higher = better)")
    ax.set_title("TSP heuristic evolution — fitness trajectory")
    ax.legend()
    ax.grid(alpha=0.3)
    path = outdir / "tsp_fitness.png"
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_timings(history: list[dict], outdir: Path) -> Path | None:
    if not history or "gen_seconds" not in history[0]:
        return None
    gens = [h["generation"] for h in history]
    gen_s = [h.get("gen_seconds", 0) for h in history]
    eval_s = [h.get("eval_seconds", 0) for h in history]
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(gens, gen_s, label="generation (LLM, concurrent)", color="#fdae6b")
    ax.bar(gens, eval_s, bottom=gen_s, label="evaluation (gVisor, serial)", color="#e6550d")
    ax.set_xlabel("generation")
    ax.set_ylabel("wall-clock seconds")
    ax.set_title("TSP per-generation phase timing")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    path = outdir / "tsp_timings.png"
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_sources(breakdown: dict[str, dict], outdir: Path) -> Path | None:
    if not breakdown:
        return None
    srcs = list(breakdown.keys())
    best = [breakdown[s]["best"] for s in srcs]
    mean = [breakdown[s]["mean"] for s in srcs]
    x = range(len(srcs))
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.bar([i - 0.2 for i in x], best, width=0.4, label="best", color="#31a354")
    ax.bar([i + 0.2 for i in x], mean, width=0.4, label="mean", color="#a1d99b")
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{s}\n(n={breakdown[s]['count']})" for s in srcs])
    ax.set_ylabel("approximation ratio")
    ax.set_title("TSP fitness by program source")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    path = outdir / "tsp_sources.png"
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze a TSP auto-kernel run")
    ap.add_argument("results", help="path to results_genN.json")
    ap.add_argument("--outdir", default=None, help="where to write PNGs (default: alongside results)")
    ap.add_argument("--bus", action="store_true", help="fold in tsp.* events from the bus debug log")
    args = ap.parse_args(argv)

    results_path = Path(args.results)
    if not results_path.exists():
        print(f"error: {results_path} not found", file=sys.stderr)
        return 1
    data = load_results(results_path)
    outdir = Path(args.outdir) if args.outdir else results_path.parent
    outdir.mkdir(parents=True, exist_ok=True)

    run_id = (data.get("best_program") or {}).get("run_id")
    bus_events = load_bus_events(run_id) if args.bus else []

    print_summary(data, bus_events)

    breakdown = source_breakdown(data, bus_events)
    if breakdown:
        print("\n  source breakdown:")
        for src, stats in breakdown.items():
            print(f"    {src:10s} n={stats['count']:3d}  best={stats['best']:.4f}  mean={stats['mean']:.4f}")

    written = [plot_fitness(data["history"], outdir)]
    written.append(plot_timings(data["history"], outdir))
    written.append(plot_sources(breakdown, outdir))
    print("\n  plots written:")
    for p in written:
        if p:
            print(f"    {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
