"""Publish one event through `nervous publish`, the only sanctioned bus writer.

Best effort: returns False when the bus is unreachable so a reflex job never
fails because publishing did.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_NERVOUS = Path(__file__).resolve().parents[2] / "sdk" / "shell" / "nervous"
TIMEOUT_S = 20


def find_nervous() -> str:
    env = os.environ.get("NERVOUS_BIN")
    if env:
        return env
    if REPO_NERVOUS.is_file():
        return str(REPO_NERVOUS)
    return shutil.which("nervous") or "nervous"


def publish(channel: str, payload: dict, source: str = "/reflex-recorder",
            nervous: str | None = None) -> bool:
    env = dict(os.environ, NERVOUS_SOURCE=source)
    try:
        r = subprocess.run([nervous or find_nervous(), "publish", channel, json.dumps(payload)],
                           capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0
