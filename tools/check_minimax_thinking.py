#!/usr/bin/env python3
"""One-shot diagnostic: does MiniMax's Anthropic-compat endpoint honour ``thinking``?

Background:
    MiniMax-M2.7 on ``/anthropic/v1/messages`` generates a separate
    ``"thinking"`` content block when reasoning. Anthropic's native API takes
    a ``thinking={"type": "enabled", "budget_tokens": N}`` parameter that caps
    *reasoning* tokens (not total output). MiniMax's docs claim full
    Anthropic-compat, but we have to verify empirically because the autobench
    RSI loop is bleeding 60s timeouts on M2.7 reasoning storms.

What this does:
    1. Loads the cf-1A "Theatre Square" prompt verbatim from the tier-1
       benchmark (so the latency profile matches what the RSI loop pays).
    2. Makes TWO calls against ``https://api.minimax.io/anthropic/v1/messages``:
       (a) control — no ``thinking`` field
       (b) experiment — ``thinking={"type": "enabled", "budget_tokens": 1024}``
    3. Records: latency, HTTP status, prompt/output tokens (split between
       thinking and text content blocks when present), response shape, error.
    4. Prints a comparison table; writes JSON to
       ``tools/check_minimax_thinking.json``.

Exit codes:
    0 — both calls succeeded (200 OK + parseable response)
    1 — either call failed; the API may have rejected the ``thinking`` field
    2 — MINIMAX_API_KEY not set in environment
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx


BASE_URL = "https://api.minimax.io"
ANTHROPIC_PATH = "/anthropic/v1/messages"

MODEL = "MiniMax-M2.7"
MAX_TOKENS = 2048
TEMPERATURE = 0.3
TIMEOUT_SECONDS = 90.0
THINKING_BUDGET = 1024

CASES_FILE = (
    Path(__file__).resolve().parents[1]
    / "autobench" / "benchmarks" / "codeforces_tier1" / "cases.jsonl"
)
TARGET_CASE_ID = "cf-1A"

SYSTEM_PROMPT = """You are a competitive programming agent.

You will receive a problem statement. Return ONLY a complete, self-contained
Python 3 program that reads from stdin and writes to stdout. No markdown
fences, no commentary, no explanation."""


def _load_prompt() -> str:
    if not CASES_FILE.exists():
        raise SystemExit(f"benchmark cases file not found: {CASES_FILE}")
    for line in CASES_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        case = json.loads(line)
        if case.get("id") == TARGET_CASE_ID:
            return case["prompt"]
    raise SystemExit(f"case {TARGET_CASE_ID!r} not found in {CASES_FILE}")


def _summarise_content(content: Any) -> dict[str, Any]:
    """Return per-block-type token-ish summary of a content list."""
    summary: dict[str, Any] = {
        "block_types": [],
        "thinking_chars": 0,
        "text_chars": 0,
        "n_blocks": 0,
    }
    if not isinstance(content, list):
        summary["raw_repr"] = repr(content)[:200]
        return summary
    summary["n_blocks"] = len(content)
    for block in content:
        if not isinstance(block, dict):
            summary["block_types"].append(f"non-dict:{type(block).__name__}")
            continue
        btype = block.get("type", "?")
        summary["block_types"].append(btype)
        if btype == "thinking":
            summary["thinking_chars"] += len(str(block.get("thinking", "")))
        elif btype == "text":
            summary["text_chars"] += len(str(block.get("text", "")))
    return summary


def _one_call(
    client: httpx.Client,
    api_key: str,
    user_prompt: str,
    label: str,
    include_thinking: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
    }
    if include_thinking:
        payload["thinking"] = {"type": "enabled", "budget_tokens": THINKING_BUDGET}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    rec: dict[str, Any] = {
        "label": label,
        "include_thinking": include_thinking,
        "thinking_budget": THINKING_BUDGET if include_thinking else None,
        "url": f"{BASE_URL}{ANTHROPIC_PATH}",
        "ok": False,
        "status": 0,
        "latency_s": 0.0,
        "prompt_tokens": 0,
        "output_tokens": 0,
        "body_bytes": 0,
        "content_summary": {},
        "error": None,
        "error_body": None,
        "usage_keys": [],
    }
    start = time.monotonic()
    try:
        resp = client.post(rec["url"], json=payload, headers=headers)
        rec["latency_s"] = time.monotonic() - start
        rec["status"] = resp.status_code
        rec["body_bytes"] = len(resp.content)
        if resp.status_code >= 400:
            try:
                rec["error_body"] = resp.json()
            except Exception:  # noqa: BLE001
                rec["error_body"] = resp.text[:500]
            rec["error"] = f"http_{resp.status_code}"
            return rec
        body = resp.json()
        usage = body.get("usage", {}) or {}
        rec["usage_keys"] = sorted(usage.keys())
        rec["prompt_tokens"] = int(usage.get("input_tokens", 0) or 0)
        rec["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
        rec["content_summary"] = _summarise_content(body.get("content"))
        rec["ok"] = True
    except Exception as exc:  # noqa: BLE001
        rec["latency_s"] = time.monotonic() - start
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def _fmt_row(rec: dict[str, Any]) -> str:
    cs = rec.get("content_summary") or {}
    block_types = ",".join(cs.get("block_types", [])) or "-"
    return (
        f"  {rec['label']:34s}  "
        f"status={rec['status']:>3d}  "
        f"latency={rec['latency_s']:6.2f}s  "
        f"in={rec['prompt_tokens']:>5d}  "
        f"out={rec['output_tokens']:>5d}  "
        f"blocks=[{block_types}]  "
        f"think_chars={cs.get('thinking_chars',0):>5d}  "
        f"text_chars={cs.get('text_chars',0):>5d}"
    )


def main() -> int:
    api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        print("SKIP: MINIMAX_API_KEY not set.", file=sys.stderr)
        return 2

    user_prompt = _load_prompt()
    print(f"Prompt: {TARGET_CASE_ID} (Theatre Square) — {len(user_prompt)} chars")
    print(f"Model={MODEL} temperature={TEMPERATURE} max_tokens={MAX_TOKENS} "
          f"thinking_budget={THINKING_BUDGET}\n")

    records: list[dict[str, Any]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        # CONTROL: no thinking field
        print("[1/2] control (no thinking field)...", end="", flush=True)
        r1 = _one_call(client, api_key, user_prompt,
                       "control (no thinking)", include_thinking=False)
        print(f" {r1['latency_s']:.2f}s status={r1['status']} "
              f"{'OK' if r1['ok'] else r1['error']}")
        records.append(r1)

        # EXPERIMENT: thinking enabled, budget_tokens=1024
        print("[2/2] experiment (thinking=enabled,budget=1024)...",
              end="", flush=True)
        r2 = _one_call(client, api_key, user_prompt,
                       "experiment (thinking budget=1024)",
                       include_thinking=True)
        print(f" {r2['latency_s']:.2f}s status={r2['status']} "
              f"{'OK' if r2['ok'] else r2['error']}")
        records.append(r2)

    print("\n" + "=" * 120)
    print("MiniMax Anthropic-compat thinking-parameter diagnostic")
    print("=" * 120)
    for rec in records:
        print(_fmt_row(rec))
    print()

    # Honour-check: did the thinking parameter actually constrain output?
    if records[0]["ok"] and records[1]["ok"]:
        c_out = records[0]["output_tokens"]
        e_out = records[1]["output_tokens"]
        c_think_chars = records[0]["content_summary"].get("thinking_chars", 0)
        e_think_chars = records[1]["content_summary"].get("thinking_chars", 0)
        print(f"  control output_tokens:    {c_out}  (thinking_chars={c_think_chars})")
        print(f"  experiment output_tokens: {e_out}  (thinking_chars={e_think_chars})")
        if e_out < c_out or e_think_chars < c_think_chars:
            print("  -> appears HONOURED (experiment produced less reasoning/output)")
        else:
            print("  -> NOT obviously honoured — may need second sample or "
                  "thinking field silently ignored")

    if records[1].get("error_body"):
        print()
        print("  experiment error_body:")
        print(f"    {json.dumps(records[1]['error_body'], indent=2)[:1000]}")

    out_path = Path(__file__).resolve().parent / "check_minimax_thinking.json"
    out_path.write_text(json.dumps({
        "config": {
            "model": MODEL,
            "temperature": TEMPERATURE,
            "max_tokens": MAX_TOKENS,
            "thinking_budget": THINKING_BUDGET,
            "case_id": TARGET_CASE_ID,
            "prompt_chars": len(user_prompt),
        },
        "control": records[0],
        "experiment": records[1],
    }, indent=2))
    print(f"\nWrote {out_path}")

    if records[0]["ok"] and records[1]["ok"]:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
