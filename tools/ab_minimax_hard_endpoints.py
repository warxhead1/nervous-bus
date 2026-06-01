#!/usr/bin/env python3
"""Endpoint A/B on a HARD MiniMax case (cf-7A).

Earlier endpoint A/B on cf-1A (EASY) showed Anthropic-compat ~5.7% mean /
8.7% p95 faster than OpenAI-compat. cf-1A finishes in 12-20s — the gap is
small relative to the noise floor. cf-7A (observed 22-244s) may diverge
materially. n=5 per endpoint, no thinking_budget (OpenAI-compat doesn't
support it; fair comparison), max_tokens=12000, conn_reuse=on, 300s read.

Queues behind tools/ab_minimax_hard_case.py to avoid M2.7 pool contention.
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
OPENAI_PATH = "/v1/chat/completions"
ANTHROPIC_PATH = "/anthropic/v1/messages"
MODEL = "MiniMax-M2.7"
TEMPERATURE = 0.3
MAX_TOKENS = 12000
N_PER_ENDPOINT = 5
TEST_READ_TIMEOUT_S = 300.0
TEST_CONNECT_TIMEOUT_S = 10.0
WORKER_TIMEOUT_S = 60.0
HARD_SLOW_S = 180.0

CASES_FILE = (
    Path(__file__).resolve().parents[1]
    / "autobench" / "benchmarks" / "codeforces_tier1" / "cases.jsonl"
)
TARGET_CASE_ID = "cf-7A"
PRIOR_AB_PID = 3011292
PRIOR_AB_JSON = Path(__file__).resolve().parent / "ab_minimax_hard_case.json"

SYSTEM_PROMPT = (
    "You are a competitive programming agent.\n\n"
    "You will receive a problem statement. Return ONLY a complete, self-contained\n"
    "Python 3 program that reads from stdin and writes to stdout. No markdown\n"
    "fences, no commentary, no explanation."
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_prior(pid: int, json_path: Path) -> float:
    start = time.monotonic()
    announced = False
    while _pid_alive(pid) and not json_path.exists():
        if not announced:
            print(f"[waiting for prior A/B PID={pid}...]", flush=True)
            announced = True
        time.sleep(5.0)
    waited = time.monotonic() - start
    print(f"[prior A/B clear after {waited:.0f}s — proceeding]", flush=True)
    return waited


def _load_prompt() -> str:
    for line in CASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        if case.get("id") == TARGET_CASE_ID:
            return case["prompt"]
    raise SystemExit(f"case {TARGET_CASE_ID!r} not found in {CASES_FILE}")


def _build_payload(user_prompt: str, anthropic: bool) -> dict[str, Any]:
    if anthropic:
        return {
            "model": MODEL,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
        }
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }


def _parse_usage(body: dict[str, Any], anthropic: bool) -> tuple[int, int]:
    usage = body.get("usage", {}) or {}
    if anthropic:
        return (int(usage.get("input_tokens", 0) or 0),
                int(usage.get("output_tokens", 0) or 0))
    return (int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0))


def _one_call(client: httpx.Client, api_key: str, url: str,
              payload: dict[str, Any], anthropic: bool) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    rec: dict[str, Any] = {"url": url, "ok": False, "status": 0,
                           "latency_s": 0.0, "input_tokens": 0,
                           "output_tokens": 0, "error": None}
    start = time.monotonic()
    try:
        resp = client.post(url, json=payload, headers=headers)
        rec["latency_s"] = time.monotonic() - start
        rec["status"] = resp.status_code
        if resp.status_code >= 400:
            rec["error"] = f"http_{resp.status_code}: {resp.text[:200]}"
            return rec
        rec["input_tokens"], rec["output_tokens"] = _parse_usage(resp.json(), anthropic)
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001
        rec["latency_s"] = time.monotonic() - start
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    lats = sorted(r["latency_s"] for r in records)
    ok = [r for r in records if r["ok"]]
    p95_idx = max(0, min(len(lats) - 1, int(0.95 * len(lats) + 0.5) - 1))
    nan = float("nan")
    return {
        "n": n, "n_ok": len(ok),
        "success_rate": len(ok) / n if n else 0.0,
        "mean_latency_s": statistics.mean(lats) if lats else nan,
        "p50_latency_s": statistics.median(lats) if lats else nan,
        "p95_latency_s": lats[p95_idx] if lats else nan,
        "min_latency_s": lats[0] if lats else nan,
        "max_latency_s": lats[-1] if lats else nan,
        "timeouts_over_60s": sum(1 for r in records if r["latency_s"] > WORKER_TIMEOUT_S),
        "timeouts_over_180s": sum(1 for r in records if r["latency_s"] > HARD_SLOW_S),
        "mean_output_tokens": statistics.mean(r["output_tokens"] for r in ok) if ok else nan,
        "errors": [r["error"] for r in records if r["error"]],
    }


def _run_endpoint(api_key: str, user_prompt: str, url: str, anthropic: bool,
                  n: int, label: str) -> list[dict[str, Any]]:
    payload = _build_payload(user_prompt, anthropic)
    timeout = httpx.Timeout(TEST_READ_TIMEOUT_S, connect=TEST_CONNECT_TIMEOUT_S)
    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout) as client:  # conn_reuse=on
        for i in range(n):
            print(f"  [{label}] {i+1}/{n}...", end="", flush=True)
            r = _one_call(client, api_key, url, payload, anthropic)
            tag = "ok" if r["ok"] else (r["error"] or "")[:40]
            print(f" {r['latency_s']:6.2f}s  ({tag})")
            records.append(r)
    return records


def _delta_pct(a: float, b: float) -> float:
    if b == 0 or b != b:
        return 0.0
    return (a - b) / b * 100.0


def main() -> int:
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        print("SKIP: MINIMAX_API_KEY not set.", file=sys.stderr)
        return 2

    user_prompt = _load_prompt()
    out_path = Path(__file__).resolve().parent / "ab_minimax_hard_endpoints.json"
    print(f"Case: {TARGET_CASE_ID} — {len(user_prompt)} chars")
    print(f"Model={MODEL} temp={TEMPERATURE} max_tokens={MAX_TOKENS} budget=None")
    print(f"Read timeout={TEST_READ_TIMEOUT_S}s | N={N_PER_ENDPOINT}/endpoint = {2*N_PER_ENDPOINT} calls\n")

    waited_s = _wait_for_prior(PRIOR_AB_PID, PRIOR_AB_JSON)

    print(f"\n=== Endpoint: OpenAI-compat ({OPENAI_PATH}) ===")
    openai_records = _run_endpoint(
        api_key, user_prompt, f"{BASE_URL}{OPENAI_PATH}",
        anthropic=False, n=N_PER_ENDPOINT, label="openai",
    )
    print(f"\n=== Endpoint: Anthropic-compat ({ANTHROPIC_PATH}) ===")
    anthropic_records = _run_endpoint(
        api_key, user_prompt, f"{BASE_URL}{ANTHROPIC_PATH}",
        anthropic=True, n=N_PER_ENDPOINT, label="anthropic",
    )

    openai_s = _summarise(openai_records)
    anth_s = _summarise(anthropic_records)

    print("\n" + "=" * 90)
    print(f"=== OpenAI vs Anthropic on {TARGET_CASE_ID} "
          f"(n={N_PER_ENDPOINT}/endpoint, budget=None, max_tokens={MAX_TOKENS}) ===")
    print("=" * 90)
    print(f"  OpenAI:    mean={openai_s['mean_latency_s']:.1f}s "
          f"p50={openai_s['p50_latency_s']:.1f}s p95={openai_s['p95_latency_s']:.1f}s "
          f"min..max={openai_s['min_latency_s']:.0f}..{openai_s['max_latency_s']:.0f} "
          f"timeouts(>60s)={openai_s['timeouts_over_60s']}/{N_PER_ENDPOINT}")
    print(f"  Anthropic: mean={anth_s['mean_latency_s']:.1f}s "
          f"p50={anth_s['p50_latency_s']:.1f}s p95={anth_s['p95_latency_s']:.1f}s "
          f"min..max={anth_s['min_latency_s']:.0f}..{anth_s['max_latency_s']:.0f} "
          f"timeouts(>60s)={anth_s['timeouts_over_60s']}/{N_PER_ENDPOINT}")

    mean_delta = _delta_pct(openai_s["mean_latency_s"], anth_s["mean_latency_s"])
    p95_delta = _delta_pct(openai_s["p95_latency_s"], anth_s["p95_latency_s"])
    significant = abs(mean_delta) >= 15.0 and abs(p95_delta) >= 15.0
    verdict = "significant" if significant else "noise"
    sign = lambda x: ("+" if x >= 0 else "")  # noqa: E731
    print(f"  delta:     mean={sign(mean_delta)}{mean_delta:.1f}% "
          f"p95={sign(p95_delta)}{p95_delta:.1f}% — statistically {verdict}")
    print("  (positive delta = OpenAI slower than Anthropic)")

    out_path.write_text(json.dumps({
        "config": {
            "model": MODEL, "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
            "thinking_budget": None, "n_per_endpoint": N_PER_ENDPOINT,
            "case_id": TARGET_CASE_ID, "prompt_chars": len(user_prompt),
            "test_read_timeout_s": TEST_READ_TIMEOUT_S,
            "worker_timeout_s": WORKER_TIMEOUT_S, "hard_slow_s": HARD_SLOW_S,
            "conn_reuse": True, "waited_for_prior_s": waited_s,
            "prior_ab_pid": PRIOR_AB_PID,
        },
        "openai": {"url": f"{BASE_URL}{OPENAI_PATH}", "records": openai_records, "summary": openai_s},
        "anthropic": {"url": f"{BASE_URL}{ANTHROPIC_PATH}", "records": anthropic_records, "summary": anth_s},
        "delta": {"mean_pct": mean_delta, "p95_pct": p95_delta, "verdict": verdict},
    }, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
