#!/usr/bin/env python3
"""Launch the steering judge when it is due, at most once a week.

The nightly timer runs this. It reads the judge gate that steering_verdicts.py
writes (no model). It starts one headless Claude session only when all hold:

  - the gate says the judge is due,
  - no launch was attempted in the last MIN_DAYS_BETWEEN days, whether or not
    it worked, so a failing run cannot repeat every night,
  - no Claude Code transcript changed in the last QUIET_MINUTES, so a run never
    competes with a person who is working.

Kill switch: STEERING_JUDGE_DISABLE=1. --dry-run prints the decision only.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DIGEST_DIR = Path.home() / ".cache" / "nervous-bus" / "reflex" / "steering-digest"
MIN_DAYS_BETWEEN = 7
QUIET_MINUTES = 30
TIMEOUT_S = 3000
# The lead session only runs the workflow script. The judge is opus and the audits are sonnet.
LEAD_MODEL = "sonnet"
WORKFLOW_ARGS = {"publish": True, "judge_model": "opus", "audit_model": "sonnet", "max_proposals": 3}
PROMPT = (
    "Run the saved workflow named steering-audit with these args: {args}. "
    "This is an explicit request to run a workflow. Wait for it to finish. "
    "Then print its returned result as JSON and stop. Do nothing else."
)


def decide(gate, state, newest_transcript_age_s, now, disabled=False):
    """(launch, reason). Pure: every input is passed in."""
    if disabled:
        return False, "disabled by STEERING_JUDGE_DISABLE"
    if not (gate or {}).get("due"):
        return False, "judge not due"
    last = (state or {}).get("last_launch", 0)
    if now - last < MIN_DAYS_BETWEEN * 86400:
        return False, f"last launch {int((now - last) // 86400)} d ago, under {MIN_DAYS_BETWEEN} d"
    if newest_transcript_age_s is not None and newest_transcript_age_s < QUIET_MINUTES * 60:
        return False, f"a Claude session was active {int(newest_transcript_age_s // 60)} min ago"
    return True, "due: " + ", ".join(gate.get("reasons") or ["no reason given"])


def newest_transcript_age(root, now):
    newest = 0.0
    for p in Path(root).glob("*/*.jsonl"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    return (now - newest) if newest else None


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--digest-dir", type=Path, default=DIGEST_DIR)
    ap.add_argument("--transcripts", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--cwd", type=Path, default=Path(__file__).resolve().parents[3])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    now = time.time()
    state_file = a.digest_dir / "judge-launch.json"
    gate = read_json(a.digest_dir / "verdicts-latest.json").get("judge")
    launch, reason = decide(gate, read_json(state_file), newest_transcript_age(a.transcripts, now), now,
                            disabled=os.environ.get("STEERING_JUDGE_DISABLE") == "1")
    print(f"steering-judge: {'launch' if launch else 'skip'} ({reason})")
    if not launch or a.dry_run:
        return 0

    # Record the attempt first. A crash or a timeout still counts against the weekly cap.
    state_file.write_text(json.dumps({"last_launch": now, "reason": reason}), encoding="utf-8")
    log = a.digest_dir / f"judge-run-{time.strftime('%Y-%m-%d', time.gmtime(now))}.log"
    cmd = ["claude", "-p", PROMPT.format(args=json.dumps(WORKFLOW_ARGS)), "--model", LEAD_MODEL]
    try:
        with log.open("w", encoding="utf-8") as out:
            rc = subprocess.run(cmd, cwd=a.cwd, stdout=out, stderr=subprocess.STDOUT, timeout=TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as e:
        print(f"steering-judge: could not start claude: {e}", file=sys.stderr)
        rc = 127
    print(f"steering-judge: claude exited {rc}, log {log}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
