#!/usr/bin/env python3
"""live_hook.py — deliver live reflex hits to the lead session as hook context.

Install as a PostToolUse command hook with matcher `Agent` (older builds name
the tool `Task`) in the lead's settings. Claude Code passes the hook input as
JSON on stdin. This reads `session_id`, runs live_check.py for that session
with --publish, and prints one hookSpecificOutput JSON whose additionalContext
holds the new hit lines. It prints nothing when there are no new hits, and it
always exits 0 so a failed check never blocks the lead. The scan itself runs
detached; see context_for.

Kill switch: REFLEX_LIVE_HOOK_DISABLE=1.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CHECK = Path(__file__).resolve().parent / "live_check.py"
PENDING = Path.home() / ".cache" / "nervous-bus" / "reflex" / "live-pending"
TIMEOUT_S = 90
TOOLS = {"Agent", "Task"}


def _safe(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:80]


def _collect(pending: Path, tag: str) -> str:
    """Lines earlier background scans left for this session; consumed once."""
    lines = []
    for f in sorted(pending.glob(f"{tag}.*.out")):
        try:
            lines.append(f.read_text().strip())
            f.unlink()
        except OSError:
            pass
    return "\n".join(x for x in lines if x)


def _scan_running(pending: Path, tag: str, now: float) -> bool:
    for f in pending.glob(f"{tag}.*.out.part"):
        try:
            if now - f.stat().st_mtime < TIMEOUT_S:
                return True
            f.unlink()
        except OSError:
            pass
    return False


def context_for(event: dict, spawn=subprocess.Popen, pending: Path = PENDING,
                now=time.time) -> str:
    """The additionalContext text for one hook event, or '' when there is none.

    The detectors need seconds on a long session, so the hook never runs them:
    it hands back what the previous background scan found and starts the next
    scan detached. A hit reaches the lead one Agent call after the scan sees it."""
    if event.get("tool_name") not in TOOLS:
        return ""
    session = event.get("session_id")
    if not session:
        return ""
    tag = _safe(session)
    try:
        pending.mkdir(parents=True, exist_ok=True)
        text = _collect(pending, tag)
        t = now()
        if not _scan_running(pending, tag, t):
            out = pending / f"{tag}.{int(t * 1000)}.out"
            out.with_name(out.name + ".part").touch()
            spawn([sys.executable, str(CHECK), "--session", session, "--publish",
                   "--out", str(out)],
                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, start_new_session=True)
        return text
    except OSError:
        return ""


def main() -> int:
    if os.environ.get("REFLEX_LIVE_HOOK_DISABLE"):
        return 0
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    text = context_for(event)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
