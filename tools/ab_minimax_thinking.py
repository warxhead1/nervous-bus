#!/usr/bin/env python3
"""A/B benchmark: thinking_budget on/off × connection-reuse on/off.

Four configs × N calls each = 4N total API calls. We measure mean/p50/p95
latency, mean cost, success rate per config. The cross-product isolates two
independent latency contributions:

  - ``thinking_budget``: does capping MiniMax-M2.7's reasoning trace at 1024
    tokens reduce wall-clock latency? Easy problems already only use ~800
    thinking chars, so for cf-1A this may be a no-op — that's a finding too.

  - ``conn_reuse``: does reusing one ``httpx.Client`` across calls (sharing
    the TLS session) shave a meaningful slice off per-call latency vs
    constructing a fresh client each time?

Reuses the cf-1A "Theatre Square" prompt loaded from the tier-1 benchmark
so the comparison stays apples-to-apples with the existing endpoint A/B
(``tools/ab_minimax_endpoints.py``).

Usage::

    MINIMAX_API_KEY=... python3 tools/ab_minimax_thinking.py [--n=5]

Exit 0 if every config has >= 60% success rate; 1 otherwise (looser bound
than the endpoint A/B because a thinking-budget rejection would zero a
whole arm and we still want to record it).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx


BASE_URL = "https://api.minimax.io"
ANTHROPIC_PATH = "/anthropic/v1/messages"

MODEL = "MiniMax-M2.7"
TEMPERATURE = 0.3
MAX_TOKENS = 2048
TIMEOUT_SECONDS = 90.0

CASES_FILE = (
    Path(__file__).resolve().parents[1]
    / "autobench" / "benchmarks" / "codeforces_tier1" / "cases.jsonl"
)
TARGET_CASE_ID = "cf-1A"

SYSTEM_PROMPT = """You are a competitive programming agent.

You will receive a problem statement. Return ONLY a complete, self-contained
Python 3 program that reads from stdin and writes to stdout. No markdown
fences, no commentary, no explanation."""

PRICING = {
    "MiniMax-M2.7": {"input": 0.30, "output": 1.20},
    "MiniMax-M2.5": {"input": 0.10, "output": 0.40},
}


def _load_prompt() -> str:
    for line in CASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        if case.get("id") == TARGET_CASE_ID:
            return case["prompt"]
    raise SystemExit(f"case {TARGET_CASE_ID!r} not found in {CASES_FILE}")


def _estimate_cost(input_tokens: int, output_tokens: int,
                   model: str = MODEL) -> float:
    rates = PRICING.get(model, PRICING[MODEL])
    return (
        (input_tokens / 1_000_000) * rates["input"]
        + (output_tokens / 1_000_000) * rates["output"]
    )


def _build_payload(user_prompt: str, thinking_budget: int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    if thinking_budget is not None:
        payload["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget,
        }
    return payload


def _one_call(client: httpx.Client, api_key: str,
              payload: dict[str, Any]) -> dict[str, Any]:
    """Make one POST. Returns a per-call record dict; never raises."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    rec: dict[str, Any] = {
        "ok": False,
        "status": 0,
        "latency_s": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "error": None,
    }
    start = time.monotonic()
    try:
        resp = client.post(
            f"{BASE_URL}{ANTHROPIC_PATH}",
            json=payload, headers=headers,
        )
        rec["latency_s"] = time.monotonic() - start
        rec["status"] = resp.status_code
        if resp.status_code >= 400:
            rec["error"] = f"http_{resp.status_code}: {resp.text[:200]}"
            return rec
        body = resp.json()
        usage = body.get("usage", {}) or {}
        rec["input_tokens"] = int(usage.get("input_tokens", 0) or 0)
        rec["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
        rec["cost_usd"] = _estimate_cost(rec["input_tokens"], rec["output_tokens"])
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001
        rec["latency_s"] = time.monotonic() - start
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r["ok"]]
    n = len(records)
    if not ok:
        return {
            "n": n, "n_ok": 0, "success_rate": 0.0,
            "mean_latency_s": float("nan"),
            "p50_latency_s": float("nan"),
            "p95_latency_s": float("nan"),
            "mean_cost_usd": float("nan"),
            "mean_input_tokens": float("nan"),
            "mean_output_tokens": float("nan"),
            "errors": [r["error"] for r in records if r["error"]],
        }
    lats = sorted(r["latency_s"] for r in ok)
    p50 = statistics.median(lats)
    p95_idx = max(0, min(len(lats) - 1, int(0.95 * len(lats) + 0.5) - 1))
    p95 = lats[p95_idx]
    return {
        "n": n,
        "n_ok": len(ok),
        "success_rate": len(ok) / n,
        "mean_latency_s": statistics.mean(lats),
        "p50_latency_s": p50,
        "p95_latency_s": p95,
        "mean_cost_usd": statistics.mean(r["cost_usd"] for r in ok),
        "mean_input_tokens": statistics.mean(r["input_tokens"] for r in ok),
        "mean_output_tokens": statistics.mean(r["output_tokens"] for r in ok),
        "errors": [r["error"] for r in records if r["error"]],
    }


def _run_config(api_key: str, user_prompt: str, thinking_budget: int | None,
                conn_reuse: bool, n: int, label: str) -> list[dict[str, Any]]:
    """Run one config. If conn_reuse, one client across n calls; else one per call."""
    records: list[dict[str, Any]] = []
    payload = _build_payload(user_prompt, thinking_budget)
    if conn_reuse:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            for i in range(n):
                print(f"  [{label}] {i+1}/{n}...", end="", flush=True)
                r = _one_call(client, api_key, payload)
                print(f" {r['latency_s']:.2f}s "
                      f"({'ok' if r['ok'] else r['error'][:40]})")
                records.append(r)
    else:
        for i in range(n):
            print(f"  [{label}] {i+1}/{n}...", end="", flush=True)
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                r = _one_call(client, api_key, payload)
            print(f" {r['latency_s']:.2f}s "
                  f"({'ok' if r['ok'] else r['error'][:40]})")
            records.append(r)
    return records


def _fmt_row(label: str, s: dict[str, Any]) -> str:
    return (
        f"  {label:36s}  "
        f"n={s['n_ok']}/{s['n']}  "
        f"mean={s['mean_latency_s']:6.2f}s  "
        f"p50={s['p50_latency_s']:6.2f}s  "
        f"p95={s['p95_latency_s']:6.2f}s  "
        f"${s['mean_cost_usd']:.5f}/call  "
        f"in={s['mean_input_tokens']:.0f}/out={s['mean_output_tokens']:.0f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=5,
                        help="calls per config (default 5)")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent
                                / "ab_minimax_thinking.json",
                        help="results JSON path")
    args = parser.parse_args()

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        print("SKIP: MINIMAX_API_KEY not set.", file=sys.stderr)
        return 2

    user_prompt = _load_prompt()
    print(f"Prompt: {TARGET_CASE_ID} (Theatre Square) — {len(user_prompt)} chars")
    print(f"Model={MODEL} temperature={TEMPERATURE} max_tokens={MAX_TOKENS}")
    print(f"N={args.n} per config, 4 configs = {4 * args.n} total API calls\n")

    configs = [
        # (label, thinking_budget, conn_reuse)
        ("budget=None,reuse=off", None, False),
        ("budget=1024,reuse=off", 1024, False),
        ("budget=None,reuse=on",  None, True),
        ("budget=1024,reuse=on",  1024, True),
    ]

    all_results: dict[str, Any] = {}
    for label, budget, reuse in configs:
        print(f"=== Config: {label} ===")
        records = _run_config(api_key, user_prompt, budget, reuse, args.n, label)
        all_results[label] = {
            "thinking_budget": budget,
            "conn_reuse": reuse,
            "records": records,
            "summary": _summarise(records),
        }
        print()

    print("=" * 130)
    print("A/B Results: thinking_budget × connection-reuse")
    print("=" * 130)
    for label, _, _ in configs:
        print(_fmt_row(label, all_results[label]["summary"]))

    # Pairwise deltas (interesting cuts).
    def _delta(a: str, b: str, field: str) -> tuple[float, float]:
        sa = all_results[a]["summary"][field]
        sb = all_results[b]["summary"][field]
        if not (isinstance(sa, float) and isinstance(sb, float)):
            return (float("nan"), float("nan"))
        return (sb - sa, ((sb - sa) / sa * 100) if sa else float("nan"))

    print()
    print("  Pairwise mean-latency deltas (positive = the second config is slower):")
    print(f"    thinking impact (reuse=off):   "
          f"+{_delta('budget=None,reuse=off', 'budget=1024,reuse=off', 'mean_latency_s')[0]:+.2f}s "
          f"({_delta('budget=None,reuse=off', 'budget=1024,reuse=off', 'mean_latency_s')[1]:+.1f}%)")
    print(f"    thinking impact (reuse=on):    "
          f"+{_delta('budget=None,reuse=on', 'budget=1024,reuse=on', 'mean_latency_s')[0]:+.2f}s "
          f"({_delta('budget=None,reuse=on', 'budget=1024,reuse=on', 'mean_latency_s')[1]:+.1f}%)")
    print(f"    conn-reuse impact (budget=None): "
          f"+{_delta('budget=None,reuse=off', 'budget=None,reuse=on', 'mean_latency_s')[0]:+.2f}s "
          f"({_delta('budget=None,reuse=off', 'budget=None,reuse=on', 'mean_latency_s')[1]:+.1f}%)")
    print(f"    conn-reuse impact (budget=1024): "
          f"+{_delta('budget=1024,reuse=off', 'budget=1024,reuse=on', 'mean_latency_s')[0]:+.2f}s "
          f"({_delta('budget=1024,reuse=off', 'budget=1024,reuse=on', 'mean_latency_s')[1]:+.1f}%)")

    args.out.write_text(json.dumps({
        "config": {
            "model": MODEL,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "n_per_config": args.n,
            "case_id": TARGET_CASE_ID,
            "prompt_chars": len(user_prompt),
        },
        "results": all_results,
    }, indent=2))
    print(f"\nWrote {args.out}")

    # Exit policy: every config >= 60% success.
    failing = [
        label for label, _, _ in configs
        if all_results[label]["summary"]["success_rate"] < 0.6
    ]
    if failing:
        print(f"\nFAIL: configs below 60% success: {failing}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
