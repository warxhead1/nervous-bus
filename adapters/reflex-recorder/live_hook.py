#!/usr/bin/env python3
"""live_hook.py — deliver live reflex hits to the lead session as hook context.

Install as a PostToolUse command hook with matcher `Agent` (older builds name
the tool `Task`) in the lead's settings. Claude Code passes the hook input as
JSON on stdin. This reads `session_id`, runs live_check.py for that session
with --publish, and prints one hookSpecificOutput JSON whose additionalContext
holds the new hit lines. It prints nothing when there are no new hits, and it
always exits 0 so a failed check never blocks the lead.

Kill switch: REFLEX_LIVE_HOOK_DISABLE=1.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().parent / "live_check.py"
TIMEOUT_S = 90
TOOLS = {"Agent", "Task"}


def context_for(event: dict, run=subprocess.run) -> str:
    """The additionalContext text for one hook event, or '' when there is none."""
    if event.get("tool_name") not in TOOLS:
        return ""
    session = event.get("session_id")
    if not session:
        return ""
    try:
        r = run([sys.executable, str(CHECK), "--session", session, "--publish"],
                capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


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
