"""Append one JSON line per finding to the local steering hit log.

Contract: counts, rule names and paths only, never prose. Logging is
best-effort: no failure here may change a hook's output or exit code.
Off: AGENT_STEERING_LOG=0. File: $AGENT_STEERING_HITS, else
${XDG_STATE_HOME:-~/.local/state}/agent-steering/hits.jsonl.
"""
import json
import os
import time
from pathlib import Path

PLUGIN = "plain-prose"
MAX_BYTES = 20 * 1024 * 1024


def hits_path():
    explicit = os.environ.get("AGENT_STEERING_HITS")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "agent-steering" / "hits.jsonl"


def log_hit(check, surface, ev, action, findings, rules=None, path="", stats=None, lost=None):
    try:
        if os.environ.get("AGENT_STEERING_LOG") == "0":
            return
        stats = stats or {}
        rec = {
            "v": 1,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "plugin": PLUGIN,
            "check": check,
            "surface": surface,
            "session_id": str(ev.get("session_id") or ""),
            "cwd": str(ev.get("cwd") or os.getcwd()),
            "path": str(path or ""),
            "action": action,
            "findings": int(findings),
            "rules": {str(k): int(v) for k, v in (rules or {}).items()},
        }
        for key in ("words", "avg_sentence", "max_sentence"):
            if key in stats:
                rec[key] = stats[key]
        if lost is not None:
            rec["lost"] = {k: int(v) for k, v in lost.items()}
        target = hits_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.stat().st_size > MAX_BYTES:
                os.replace(target, target.with_name(target.name + ".1"))
        except FileNotFoundError:
            pass
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass
