#!/usr/bin/env python3
"""Replay recorded shell calls through the verify-pipe pipefail rewrite.

Same corpus as backtest.py (adapters/transcripts, every agent it can read).
Pure: nothing is executed and no state is read or written.

  python3 backtest_pipefail.py --days 30 --out <scratch dir> [--agent claude] [--sample 30] [--seed 1]

Prints the rewrite count, the count by consumer shape, and how the recorded
result relates to a failure marker in the output:
  hidden_red   recorded is_error=false and the output shows a failure marker (the rewrite would surface it)
  visible      recorded is_error=true already (the rewrite changes nothing the agent sees)
  quiet        no failure marker in the output (status should stay 0 by construction)
  no_output    no recorded result
Writes <out>/rewrites.jsonl (every rewrite) and <out>/sample.json (random sample for hand review).
"""
import argparse
import collections
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "transcripts"))
import pipefail  # noqa: E402
import transcripts  # noqa: E402

FAIL_MARK = re.compile(
    r"\bFAILED\b|\bFAIL\b|\d+ failed|test result: FAILED|error\[E\d+\]|\berror:|panicked at|Traceback \(most recent|"
    r"AssertionError|npm ERR!|\bERROR\b|make: \*\*\*|exit status [1-9]|✗|✘|❌|Build FAILED|cannot find|"
    r"undefined:|SyntaxError|ModuleNotFoundError|Cannot find module|TS\d{4}:", re.I)
CONSUMER = re.compile(r"\|&?\s*(?:command\s+)?(?:\S*/)?([A-Za-z0-9_.+-]+)")


def shape(command: str) -> str:
    names = []
    for m in CONSUMER.finditer(command):
        n = m.group(1)
        if n in ("tail", "tee", "wc", "sort", "cat", "grep", "rg", "egrep", "fgrep", "ugrep", "ug"):
            names.append(n)
    return "|".join(sorted(set(names))) or "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--out", required=True)
    ap.add_argument("--agent", action="append")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--strict-window", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    cutoff = time.time() - a.days * 86400
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    seen, calls, dupes, piped = set(), 0, 0, 0
    by_shape, by_agent = collections.Counter(), collections.Counter()
    result = collections.Counter()
    rows = []
    for tc in transcripts.iter_calls(a.agent, cutoff - (86400 if a.strict_window else 0), shell_only=True):
        key = (tc.agent, tc.session, tc.call_id)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        if a.strict_window and tc.ts and tc.ts[:19] < cutoff_iso:
            continue
        calls += 1
        cmd = tc.command or ""
        if "|" in cmd:
            piped += 1
        new = pipefail.rewrite(cmd)
        if new is None:
            continue
        by_shape[shape(cmd)] += 1
        by_agent[tc.agent] += 1
        if tc.output is None:
            kind = "no_output"
        elif tc.is_error:
            kind = "visible"
        elif FAIL_MARK.search(tc.output[-4000:]):
            kind = "hidden_red"
        else:
            kind = "quiet"
        result[kind] += 1
        rows.append({"agent": tc.agent, "session": tc.session, "cmd": cmd[:6000], "kind": kind,
                     "output_tail": (tc.output or "")[-300:]})
    with open(os.path.join(a.out, "rewrites.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    random.Random(a.seed).shuffle(rows)
    with open(os.path.join(a.out, "sample.json"), "w") as f:
        json.dump(rows[:a.sample], f, indent=1)
    print(f"calls={calls} dupes_skipped={dupes} piped={piped} rewrites={len(rows)} by_agent={dict(by_agent)}")
    print("by_consumer_shape:")
    for k, v in by_shape.most_common():
        print(f"  {v:6d}  {k}")
    print(f"recorded_result: {dict(result)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
