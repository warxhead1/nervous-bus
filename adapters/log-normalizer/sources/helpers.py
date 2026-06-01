from __future__ import annotations

def infer_level(message: str) -> str:
    m = message.lower()
    if any(w in m for w in ("panic", "fatal", "oom killed", "out of memory", "killed process")):
        return "critical"
    if any(w in m for w in ("error", "err:", "failed", "failure", "exception", "traceback", "sigkill")):
        return "error"
    if any(w in m for w in ("warn", "warning", "deprecated", "retrying")):
        return "warn"
    if any(w in m for w in ("debug", "trace", "verbose")):
        return "debug"
    return "info"
