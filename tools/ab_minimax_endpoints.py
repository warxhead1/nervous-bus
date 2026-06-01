#!/usr/bin/env python3
"""Real A/B benchmark: MiniMax OpenAI vs Anthropic endpoint latency.

Why this exists: the autobench RSI loop now calls MiniMax via two paths
(``worker_agent`` for code generation, ``minimax_improver`` for harness
deltas). Both default to ``endpoint_mode="anthropic"`` so the M2.7 reasoning
trace lands in a separate ``thinking`` block rather than inline
``<think>...</think>`` markers. The Anthropic-default switch was motivated
by a latent JSON-collision risk in the improver's permissive regex, NOT by
any measured latency edge.

This script measures the actual edge. We fire N identical requests at each
endpoint with the same model, same temperature, same max_tokens, and the
same prompt (CF 1A "Theatre Square" from the autobench tier-1 benchmark
suite — chosen because it's the realistic latency profile the RSI loop
actually pays per case). We record wall-clock latency, token counts, cost,
HTTP status, and response size per call, then print per-endpoint summary
stats plus a clean winner declaration.

Usage::

    MINIMAX_API_KEY=... python3 tools/ab_minimax_endpoints.py [--n=10]

Exit 0 if both endpoints return >= 80% success; exit 1 otherwise.
Writes durable results to ``tools/ab_minimax_endpoints.json``.
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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://api.minimax.io"
OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/anthropic/v1/messages"

MODEL = "MiniMax-M2.7"
TEMPERATURE = 0.3
MAX_TOKENS = 1024
TIMEOUT_SECONDS = 90.0

# MiniMax public list, May 2026 ($ per 1M tokens).
PRICING = {
    "MiniMax-M2.7": {"input": 0.30, "output": 1.20},
    "MiniMax-M2.5": {"input": 0.10, "output": 0.40},
}

# autobench tier-1 benchmark — load cf-1A prompt verbatim so the latency
# profile matches what the RSI loop pays per case.
CASES_FILE = (
    Path(__file__).resolve().parents[1]
    / "autobench" / "benchmarks" / "codeforces_tier1" / "cases.jsonl"
)
TARGET_CASE_ID = "cf-1A"

# Mirrors worker_agent.DEFAULT_SYSTEM_PROMPT so we measure the actual
# system prompt the RSI loop sends.
SYSTEM_PROMPT = """You are a competitive programming agent.

You will receive a problem statement. Return ONLY a complete, self-contained
Python 3 program that reads from stdin and writes to stdout. No markdown
fences, no commentary, no explanation."""


# ---------------------------------------------------------------------------
# Cost / parse helpers (mirror worker_agent + minimax_improver)
# ---------------------------------------------------------------------------

def _estimate_cost(input_tokens: int, output_tokens: int,
                   model: str = MODEL) -> float:
    rates = PRICING.get(model, PRICING[MODEL])
    return (
        (input_tokens / 1_000_000) * rates["input"]
        + (output_tokens / 1_000_000) * rates["output"]
    )


def _parse_openai(body: dict[str, Any]) -> tuple[int, int]:
    usage = body.get("usage", {}) or {}
    return (
        int(usage.get("prompt_tokens", 0) or 0),
        int(usage.get("completion_tokens", 0) or 0),
    )


def _parse_anthropic(body: dict[str, Any]) -> tuple[int, int]:
    usage = body.get("usage", {}) or {}
    return (
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
    )


# ---------------------------------------------------------------------------
# Single-call shapes
# ---------------------------------------------------------------------------

def _call_openai(client: httpx.Client, api_key: str,
                 user_prompt: str) -> dict[str, Any]:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    return _post_and_record(
        client=client,
        url=f"{BASE_URL}{OPENAI_PATH}",
        api_key=api_key,
        payload=payload,
        parser=_parse_openai,
    )


def _call_anthropic(client: httpx.Client, api_key: str,
                    user_prompt: str) -> dict[str, Any]:
    payload = {
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    return _post_and_record(
        client=client,
        url=f"{BASE_URL}{ANTHROPIC_PATH}",
        api_key=api_key,
        payload=payload,
        parser=_parse_anthropic,
    )


def _post_and_record(client: httpx.Client, url: str, api_key: str,
                     payload: dict[str, Any], parser) -> dict[str, Any]:
    """One POST. Returns a record dict; never raises (errors recorded inline)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    record: dict[str, Any] = {
        "url": url,
        "ok": False,
        "status": 0,
        "latency_s": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "body_bytes": 0,
        "error": None,
    }
    start = time.monotonic()
    try:
        resp = client.post(url, json=payload, headers=headers)
        record["latency_s"] = time.monotonic() - start
        record["status"] = resp.status_code
        record["body_bytes"] = len(resp.content)
        if resp.status_code >= 400:
            record["error"] = f"http_{resp.status_code}: {resp.text[:200]}"
            return record
        body = resp.json()
        inp, outp = parser(body)
        record["prompt_tokens"] = inp
        record["completion_tokens"] = outp
        record["cost_usd"] = _estimate_cost(inp, outp)
        record["ok"] = True
    except Exception as exc:  # noqa: BLE001 — fully recorded, never raised
        record["latency_s"] = time.monotonic() - start
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok_records = [r for r in records if r["ok"]]
    n = len(records)
    n_ok = len(ok_records)
    success_rate = (n_ok / n) if n else 0.0

    if not ok_records:
        return {
            "n": n,
            "n_ok": 0,
            "success_rate": 0.0,
            "mean_latency_s": float("nan"),
            "p50_latency_s": float("nan"),
            "p95_latency_s": float("nan"),
            "mean_cost_usd": float("nan"),
            "mean_prompt_tokens": float("nan"),
            "mean_completion_tokens": float("nan"),
            "errors": [r["error"] for r in records if r["error"]],
        }

    latencies = sorted(r["latency_s"] for r in ok_records)
    p50 = statistics.median(latencies)
    # p95: index = ceil(0.95 * n) - 1, clamped.
    p95_idx = max(0, min(len(latencies) - 1, int(0.95 * len(latencies) + 0.5) - 1))
    p95 = latencies[p95_idx]
    return {
        "n": n,
        "n_ok": n_ok,
        "success_rate": success_rate,
        "mean_latency_s": statistics.mean(latencies),
        "p50_latency_s": p50,
        "p95_latency_s": p95,
        "mean_cost_usd": statistics.mean(r["cost_usd"] for r in ok_records),
        "mean_prompt_tokens": statistics.mean(r["prompt_tokens"] for r in ok_records),
        "mean_completion_tokens": statistics.mean(r["completion_tokens"] for r in ok_records),
        "errors": [r["error"] for r in records if r["error"]],
    }


def _load_prompt() -> str:
    """Load the cf-1A 'Theatre Square' prompt from the tier-1 benchmark."""
    if not CASES_FILE.exists():
        raise SystemExit(f"benchmark cases file not found: {CASES_FILE}")
    for line in CASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        if case.get("id") == TARGET_CASE_ID:
            return case["prompt"]
    raise SystemExit(
        f"case {TARGET_CASE_ID!r} not found in {CASES_FILE}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _format_summary_row(label: str, s: dict[str, Any]) -> str:
    return (
        f"  {label:30s} n={s['n_ok']}/{s['n']}  "
        f"mean={s['mean_latency_s']:.2f}s  "
        f"p50={s['p50_latency_s']:.2f}s  "
        f"p95={s['p95_latency_s']:.2f}s  "
        f"${s['mean_cost_usd']:.5f}/call  "
        f"tokens_in={s['mean_prompt_tokens']:.0f}/out={s['mean_completion_tokens']:.0f}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10,
                        help="calls per endpoint (default 10)")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent / "ab_minimax_endpoints.json",
                        help="results JSON path")
    args = parser.parse_args()

    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        print("SKIP: MINIMAX_API_KEY not set in environment. "
              "Set it to run the A/B benchmark.", file=sys.stderr)
        return 2  # distinct from pass/fail so CI can detect skip

    user_prompt = _load_prompt()
    print(f"Prompt: cf-1A (Theatre Square) — {len(user_prompt)} chars")
    print(f"Model={MODEL} temperature={TEMPERATURE} max_tokens={MAX_TOKENS}")
    print(f"N={args.n} per endpoint, interleaved\n")

    openai_records: list[dict[str, Any]] = []
    anthropic_records: list[dict[str, Any]] = []

    # Interleave openai/anthropic calls so transient network conditions
    # affect both endpoints roughly equally.
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for i in range(args.n):
            print(f"[{i+1}/{args.n}] openai...", end="", flush=True)
            r1 = _call_openai(client, api_key, user_prompt)
            print(f" {r1['latency_s']:.2f}s ({'ok' if r1['ok'] else r1['error'][:50]})")
            openai_records.append(r1)

            print(f"[{i+1}/{args.n}] anthropic...", end="", flush=True)
            r2 = _call_anthropic(client, api_key, user_prompt)
            print(f" {r2['latency_s']:.2f}s ({'ok' if r2['ok'] else r2['error'][:50]})")
            anthropic_records.append(r2)

    openai_summary = _summarise(openai_records)
    anthropic_summary = _summarise(anthropic_records)

    print("\n" + "=" * 100)
    print("A/B Benchmark Results")
    print("=" * 100)
    print(_format_summary_row("/v1/chat/completions (openai)", openai_summary))
    print(_format_summary_row("/anthropic/v1/messages",       anthropic_summary))

    # Winner declaration on mean latency.
    winner = None
    delta_pct = 0.0
    if openai_summary["n_ok"] and anthropic_summary["n_ok"]:
        oa = openai_summary["mean_latency_s"]
        an = anthropic_summary["mean_latency_s"]
        if oa < an:
            winner = "openai"
            delta_pct = (an - oa) / an * 100
        else:
            winner = "anthropic"
            delta_pct = (oa - an) / oa * 100

        # Cost diff (mean cost per successful call).
        oc = openai_summary["mean_cost_usd"]
        ac = anthropic_summary["mean_cost_usd"]
        cheaper = "openai" if oc < ac else "anthropic"
        cheaper_pct = abs(oc - ac) / max(oc, ac) * 100 if max(oc, ac) > 0 else 0.0

        print()
        print(f"  WINNER (mean latency): {winner}  "
              f"({delta_pct:.1f}% faster)")
        print(f"  CHEAPER (mean cost):  {cheaper}  "
              f"({cheaper_pct:.1f}% lower)")
    else:
        print("\n  WINNER: undetermined (one or both endpoints had no successes)")

    # Durable record.
    out_data = {
        "config": {
            "model": MODEL,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "n_per_endpoint": args.n,
            "case_id": TARGET_CASE_ID,
            "prompt_chars": len(user_prompt),
        },
        "openai": {"summary": openai_summary, "records": openai_records},
        "anthropic": {"summary": anthropic_summary, "records": anthropic_records},
        "winner": winner,
        "latency_delta_pct": delta_pct,
    }
    args.out.write_text(json.dumps(out_data, indent=2))
    print(f"\nWrote {args.out}")

    # Exit policy: fail if either endpoint had < 80% success.
    if openai_summary["success_rate"] < 0.8 or anthropic_summary["success_rate"] < 0.8:
        print("\nFAIL: one or both endpoints below 80% success rate.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
