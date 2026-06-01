#!/usr/bin/env python3
"""Focused A/B: thinking_budget on a known-slow tier-1 case (cf-7A).

Background: cycles 3 and 4 of the autobench RSI loop see constant 60s
``ReadTimeout`` on MiniMax-M2.7 with the worker's default
``thinking_budget=1024``. The earlier A/B on cf-1A (Theatre Square, EASY)
already showed ``p95=54.78s`` with budget=1024 — 91% of the 60s read timeout.
The hypothesis: harder problems blow through the 60s wall reliably at
budget=1024, but a higher budget (or no budget cap at all) might let the
model finish faster by *not* truncating its plan mid-stream.

This script measures real latency above the worker's 60s wall by setting a
generous 300s read timeout on the test client. It tests three configs on
cf-7A ("Kalevitch and Chess", a known cycle-3 171s case):

  - ``thinking_budget=None`` (no budget cap — model decides)
  - ``thinking_budget=512``
  - ``thinking_budget=1024``

All three use the Anthropic-compat endpoint and ``conn_reuse=on`` (the
default we already shipped). n=5 per config = 15 total calls.

NOTE: cycle 4 may still be running against MiniMax. If you see large
variance across same-config calls, that's likely contention — not the
thinking_budget itself.
"""

from __future__ import annotations

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
MAX_TOKENS = 12000  # match the current improver-set worker default
N_PER_CONFIG = 5

# Generous read timeout so we can measure real latency above the worker's
# 60s wall. The worker uses 60s — we want to SEE how far past 60s the call
# goes when not artificially cut off.
TEST_READ_TIMEOUT_S = 300.0
TEST_CONNECT_TIMEOUT_S = 10.0

CASES_FILE = (
    Path(__file__).resolve().parents[1]
    / "autobench" / "benchmarks" / "codeforces_tier1" / "cases.jsonl"
)
TARGET_CASE_ID = "cf-7A"

# Two latency buckets to report against:
WORKER_TIMEOUT_S = 60.0     # the autobench worker's actual read timeout
HARD_SLOW_S = 180.0         # "this would have been a 3x retry stall"

SYSTEM_PROMPT = """You are a competitive programming agent.

You will receive a problem statement. Return ONLY a complete, self-contained
Python 3 program that reads from stdin and writes to stdout. No markdown
fences, no commentary, no explanation."""


def _load_prompt() -> str:
    for line in CASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        if case.get("id") == TARGET_CASE_ID:
            return case["prompt"]
    raise SystemExit(f"case {TARGET_CASE_ID!r} not found in {CASES_FILE}")


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
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    rec: dict[str, Any] = {
        "ok": False,
        "status": 0,
        "latency_s": 0.0,
        "output_tokens": 0,
        "input_tokens": 0,
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
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001
        rec["latency_s"] = time.monotonic() - start
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    # For latency stats, count ALL calls (timeouts have real latency too).
    lats = sorted(r["latency_s"] for r in records)
    ok = [r for r in records if r["ok"]]
    mean_lat = statistics.mean(lats) if lats else float("nan")
    p50 = statistics.median(lats) if lats else float("nan")
    p95_idx = max(0, min(len(lats) - 1, int(0.95 * len(lats) + 0.5) - 1))
    p95 = lats[p95_idx] if lats else float("nan")
    over_worker = sum(1 for r in records if r["latency_s"] > WORKER_TIMEOUT_S)
    over_hard = sum(1 for r in records if r["latency_s"] > HARD_SLOW_S)
    return {
        "n": n,
        "n_ok": len(ok),
        "success_rate": len(ok) / n if n else 0.0,
        "mean_latency_s": mean_lat,
        "p50_latency_s": p50,
        "p95_latency_s": p95,
        "min_latency_s": lats[0] if lats else float("nan"),
        "max_latency_s": lats[-1] if lats else float("nan"),
        "timeouts_over_60s": over_worker,
        "timeouts_over_180s": over_hard,
        "mean_output_tokens": (
            statistics.mean(r["output_tokens"] for r in ok) if ok else float("nan")
        ),
        "errors": [r["error"] for r in records if r["error"]],
    }


def _run_config(api_key: str, user_prompt: str, thinking_budget: int | None,
                n: int, label: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    payload = _build_payload(user_prompt, thinking_budget)
    timeout = httpx.Timeout(
        TEST_READ_TIMEOUT_S,
        connect=TEST_CONNECT_TIMEOUT_S,
    )
    with httpx.Client(timeout=timeout) as client:  # conn_reuse=on
        for i in range(n):
            print(f"  [{label}] {i+1}/{n}...", end="", flush=True)
            r = _one_call(client, api_key, payload)
            tag = "ok" if r["ok"] else (r["error"] or "")[:40]
            print(f" {r['latency_s']:6.2f}s  ({tag})")
            records.append(r)
    return records


def _fmt_row(label: str, s: dict[str, Any]) -> str:
    return (
        f"  budget={label:>5s}: "
        f"mean={s['mean_latency_s']:6.2f}s "
        f"p50={s['p50_latency_s']:6.2f}s "
        f"p95={s['p95_latency_s']:6.2f}s "
        f"min={s['min_latency_s']:5.1f}s max={s['max_latency_s']:6.1f}s "
        f"ok={s['n_ok']}/{s['n']} "
        f"timeouts(>60s)={s['timeouts_over_60s']}/{s['n']} "
        f"timeouts(>180s)={s['timeouts_over_180s']}/{s['n']}"
    )


def main() -> int:
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        print("SKIP: MINIMAX_API_KEY not set.", file=sys.stderr)
        return 2

    user_prompt = _load_prompt()
    out_path = Path(__file__).resolve().parent / "ab_minimax_hard_case.json"
    print(f"Case: {TARGET_CASE_ID} (Kalevitch and Chess) — {len(user_prompt)} chars")
    print(f"Model={MODEL} temp={TEMPERATURE} max_tokens={MAX_TOKENS}")
    print(f"Test read timeout={TEST_READ_TIMEOUT_S}s "
          f"(worker prod uses {WORKER_TIMEOUT_S}s)")
    print(f"N={N_PER_CONFIG} per config, 3 configs = {3 * N_PER_CONFIG} calls\n")

    configs: list[tuple[str, int | None]] = [
        ("None", None),
        ("512", 512),
        ("1024", 1024),
    ]

    all_results: dict[str, Any] = {}
    for label, budget in configs:
        print(f"=== Config: thinking_budget={label} ===")
        records = _run_config(api_key, user_prompt, budget, N_PER_CONFIG, label)
        all_results[label] = {
            "thinking_budget": budget,
            "records": records,
            "summary": _summarise(records),
        }
        print()

    print("=" * 110)
    print(f"=== thinking_budget A/B on {TARGET_CASE_ID} (harder case) ===")
    print("=" * 110)
    for label, _ in configs:
        print(_fmt_row(label, all_results[label]["summary"]))

    out_path.write_text(json.dumps({
        "config": {
            "model": MODEL,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "n_per_config": N_PER_CONFIG,
            "case_id": TARGET_CASE_ID,
            "prompt_chars": len(user_prompt),
            "test_read_timeout_s": TEST_READ_TIMEOUT_S,
            "worker_timeout_s": WORKER_TIMEOUT_S,
            "hard_slow_s": HARD_SLOW_S,
            "endpoint": ANTHROPIC_PATH,
            "conn_reuse": True,
        },
        "results": all_results,
    }, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
