"""Measure the intrinsic score variance of the autobench harness.

Motivation (sibx): cycle 5 (2026-05-16, session 01KRS6CWS6JDE946S2RJMT57WX)
showed a score "regression" of 0.647 → 0.628 between iter 0 and iter 1 that
turned out to be a NO-OP transition — the delta-diff event recorded
``no_change: true`` because the improver's malformed JSON fell back to
rule-based with an empty delta. The harness ran with IDENTICAL config both
iterations and scored 0.019 apart purely from generation stochasticity.

Until we know the variance floor, any single-iter score delta is
indistinguishable from noise. This script runs the benchmark N times on
the same harness config (no improver, no delta application) and reports
mean / stdev / min / max so future cycles can put error bars on every
claimed improvement.

Usage::

    # Default — 3 repeats on the CodeForces tier-1 benchmark
    python3 tools/variance_floor.py

    # Larger sample for tighter bounds
    python3 tools/variance_floor.py --n 5

    # Specify output destination (JSON record for the regression file)
    python3 tools/variance_floor.py --n 3 --output tools/variance_floor_2026-05-16.json

Cost note: each run executes the FULL benchmark (20 cases × ~30s/case ≈
10 min) with worker LLM calls. Default n=3 is ~30 min and ~$0.20 spend.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from autobench.budget_guard import BudgetExceeded, BudgetGuard  # noqa: E402
from autobench.evaluator import BenchmarkEvaluator  # noqa: E402
from autobench.observability import AutobenchObservability  # noqa: E402
from autobench.worker_agent import MiniMaxWorker  # noqa: E402
from autobench.benchmarks.codeforces_tier1.run_first import (  # noqa: E402
    CASES_FILE,
    _initial_harness,
)
from autobench.evaluator import BenchmarkCase  # noqa: E402


def _load_cases() -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for line in CASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        cases.append(BenchmarkCase(**json.loads(line)))
    return cases


def _run_once(run_idx: int, total: int) -> dict:
    """Run the benchmark once. Returns a record dict.

    No improver, no delta application — just evaluator.run on the baseline
    harness config. The interesting quantity is the aggregate_score.
    """
    cases = _load_cases()
    harness = _initial_harness()
    obs = AutobenchObservability()

    if not os.environ.get("MINIMAX_API_KEY"):
        print(
            "[variance_floor] FATAL: MINIMAX_API_KEY unset — empty-string "
            "worker produces a deterministic score of 0.0 and would give a "
            "misleading variance floor of 0. Set the key or use a non-empty "
            "worker stub.",
            file=sys.stderr,
        )
        return {"run_idx": run_idx, "error": "no_api_key"}

    worker = MiniMaxWorker(obs=obs)
    worker_totals = {"calls": 0, "cost_usd": 0.0}

    def _worker_callable(prompt: str, cfg) -> str:
        code = worker(prompt, cfg)
        usage = worker._last_usage
        worker_totals["calls"] += 1
        worker_totals["cost_usd"] += float(usage.get("cost_usd", 0.0))
        return code

    evaluator = BenchmarkEvaluator(generate_fn=_worker_callable, obs=obs)
    # nervous-bus-dq7l: $ guard disabled; wall-time only.
    guard = BudgetGuard(
        max_cost_dollars=0,
        max_wall_time_seconds=1800,
        session_id=obs.session_id,
    )

    print(
        f"[variance_floor] run {run_idx}/{total} starting "
        f"(session={obs.session_id})..."
    )
    t0 = time.monotonic()
    try:
        result = evaluator.run(harness, cases, iteration=0)
    except BudgetExceeded as exc:
        elapsed = time.monotonic() - t0
        return {
            "run_idx": run_idx,
            "session_id": obs.session_id,
            "error": f"budget_exceeded: {exc}",
            "elapsed_s": elapsed,
            "cost_usd": worker_totals["cost_usd"],
        }

    elapsed = time.monotonic() - t0
    record = {
        "run_idx": run_idx,
        "session_id": obs.session_id,
        "aggregate_score": result.aggregate_score,
        "verdict_counts": dict(result.verdict_counts),
        "num_cases": len(result.case_results),
        "elapsed_s": elapsed,
        "cost_usd": worker_totals["cost_usd"],
        "worker_calls": worker_totals["calls"],
    }
    print(
        f"[variance_floor] run {run_idx}/{total} done "
        f"score={record['aggregate_score']:.4f} "
        f"verdicts={record['verdict_counts']} "
        f"cost=${record['cost_usd']:.4f} elapsed={elapsed:.1f}s"
    )
    return record


def _summarize(runs: list[dict]) -> dict:
    scored = [r for r in runs if "aggregate_score" in r]
    if not scored:
        return {"error": "no successful runs"}
    scores = [r["aggregate_score"] for r in scored]
    mean = statistics.fmean(scores)
    stdev = statistics.stdev(scores) if len(scores) > 1 else 0.0
    return {
        "n": len(scores),
        "mean": mean,
        "stdev": stdev,
        "min": min(scores),
        "max": max(scores),
        "range": max(scores) - min(scores),
        "two_sigma": 2 * stdev,
        "total_cost_usd": sum(r.get("cost_usd", 0.0) for r in scored),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure intrinsic score variance of the autobench harness.",
    )
    parser.add_argument(
        "--n", type=int, default=3,
        help="Number of identical-config repeats (default: 3).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Output JSON file path. Defaults to "
             "tools/variance_floor_<UTC-date>.json",
    )
    args = parser.parse_args(argv)

    if args.n < 2:
        print("[variance_floor] --n must be >= 2 for a meaningful stdev.",
              file=sys.stderr)
        return 2

    runs: list[dict] = []
    for i in range(1, args.n + 1):
        runs.append(_run_once(i, args.n))

    summary = _summarize(runs)

    output_path = args.output
    if output_path is None:
        today = datetime.now(timezone.utc).date().isoformat()
        output_path = REPO_ROOT / "tools" / f"variance_floor_{today}.json"

    record = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "runs": runs,
    }
    output_path.write_text(json.dumps(record, indent=2))

    print()
    print("=" * 60)
    print("variance floor summary")
    print("=" * 60)
    if "error" in summary:
        print(f"  ERROR: {summary['error']}")
        return 1
    print(f"  n            = {summary['n']}")
    print(f"  mean         = {summary['mean']:.4f}")
    print(f"  stdev        = {summary['stdev']:.4f}")
    print(f"  range        = [{summary['min']:.4f}, {summary['max']:.4f}]"
          f" (Δ={summary['range']:.4f})")
    print(f"  2σ           = {summary['two_sigma']:.4f}")
    print(f"  total cost   = ${summary['total_cost_usd']:.4f}")
    print()
    print(f"  → any single-iter delta < {summary['two_sigma']:.3f} "
          "is within 2σ noise")
    print()
    print(f"  record written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
