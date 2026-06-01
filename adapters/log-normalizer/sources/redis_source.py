from __future__ import annotations
from queue import Queue
from threading import Event

def format_slowlog_entry(entry: dict) -> dict:
    duration_us = entry.get("duration", 0)
    cmd_parts = entry.get("command", [])
    cmd = " ".join(str(a) for a in cmd_parts)[:100]
    message = f"slow command {duration_us/1000:.1f}ms: {cmd}"
    return {
        "log_source": "redis",
        "service": "redis-slowlog",
        "level": "warn",
        "message": message,
        "raw": message[:1000],
        "parsed_fields": {
            "duration_us": duration_us,
            "command": cmd,
            "slowlog_id": entry.get("id", 0),
        },
    }

def redis_source(q: Queue, stop: Event, redis_client, threshold_ms: int = 10) -> None:
    last_id = -1
    while not stop.is_set():
        try:
            entries = redis_client.slowlog_get(128)
            for entry in reversed(entries):
                if entry["id"] <= last_id:
                    continue
                last_id = entry["id"]
                if entry.get("duration", 0) < threshold_ms * 1000:
                    continue
                try:
                    q.put_nowait(format_slowlog_entry(entry))
                except Exception:
                    pass
        except Exception:
            pass
        stop.wait(60)
