#!/usr/bin/env python3
"""Diagnose whether MINIMAX_API_KEY is hitting the coding-plan quota or pay-per-token billing.

Makes one minimal chat-completions call and dumps every response header plus the
usage block. Coding-plan routing usually surfaces in one of three places:
  1. A header like ``X-RateLimit-*`` or ``X-Plan-*``
  2. A ``plan``/``subscription``/``quota`` key in the response body
  3. An auth error if the endpoint rejects the key family

Run::

    python3 tools/check_minimax_routing.py

Exit 0 on a 2xx response; non-zero otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-M2")


def main() -> int:
    api_key = os.environ.get("MINIMAX_API_KEY", "")
    if not api_key:
        print("MINIMAX_API_KEY unset", file=sys.stderr)
        return 2

    key_prefix = api_key[:6]
    print(f"key prefix : {key_prefix}...  (sk-cp- = coding plan, sk- = pay-per-token)")
    print(f"base url   : {BASE_URL}")
    print(f"model      : {MODEL}")
    print()

    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply with one word: ping"}],
            "max_tokens": 8,
            "temperature": 0.0,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        status = e.code
        headers = dict(e.headers.items()) if e.headers else {}
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"_raw_error": str(e)}
    except Exception as e:
        print(f"request failed: {e}", file=sys.stderr)
        return 3

    print(f"http status: {status}")
    print()
    print("=== response headers ===")
    for k in sorted(headers):
        print(f"  {k}: {headers[k]}")
    print()
    print("=== response body (usage + plan-like keys only) ===")
    usage = payload.get("usage") or {}
    if usage:
        print("usage:")
        for k, v in usage.items():
            print(f"  {k}: {v}")
    plan_keys = [k for k in payload if any(s in k.lower() for s in ("plan", "quota", "subscrib", "rate"))]
    for k in plan_keys:
        print(f"{k}: {payload[k]}")
    print()
    print("=== routing verdict ===")
    routing_signals = [
        (k, v) for k, v in headers.items()
        if any(s in k.lower() for s in ("plan", "quota", "ratelimit", "subscrib", "remaining"))
    ]
    if routing_signals:
        for k, v in routing_signals:
            print(f"  signal: {k}={v}")
    else:
        print("  no plan/quota/rate-limit headers in response — endpoint does not surface routing info.")
        print("  Verify externally by checking platform.minimax.io dashboard for quota tick-down.")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
