from __future__ import annotations
from dataclasses import dataclass, field
import json
import redis as redis_lib

# Global defaults (used as fallback for unknown types)
AUTO_ENABLE_VERDICTS          = 15    # was 20 — tuned for single-developer workstation
AUTO_ENABLE_ACCEPT_RATE       = 0.70  # was 0.75
AUTO_ENABLE_NO_RECENT_REJECTS = 3     # was 5

# Per-signal-type overrides
_TYPE_THRESHOLDS: dict[str, dict] = {
    "anomaly": {
        "verdicts":    15,
        "accept_rate": 0.70,
        "clean_gap":   3,
    },
    "pattern": {
        "verdicts":    15,
        "accept_rate": 0.70,
        "clean_gap":   3,
    },
    "correlation": {
        "verdicts":    15,
        "accept_rate": 0.70,
        "clean_gap":   3,
    },
    "recovery": {
        "verdicts":    8,
        "accept_rate": 0.65,
        "clean_gap":   2,
    },
    "silence": {
        "verdicts":    999,   # effectively never auto-file
        "accept_rate": 0.99,
        "clean_gap":   999,
    },
}

@dataclass
class CalibrationState:
    signal_type: str
    channel: str
    accept_count: int = 0
    reject_count: int = 0
    last_reject_idx: int = 0
    auto_enabled: bool = False

def _key(signal_type: str, channel: str = "") -> str:
    if channel:
        return f"pattern:calibration:{signal_type}:{channel}"
    return f"pattern:calibration:{signal_type}"

def load_calibration(r: redis_lib.Redis, signal_type: str, channel: str = "") -> CalibrationState:
    raw = r.hgetall(_key(signal_type, channel))
    if raw:
        return CalibrationState(
            signal_type=signal_type,
            channel=channel,
            accept_count=int(raw.get("accept_count", 0)),
            reject_count=int(raw.get("reject_count", 0)),
            last_reject_idx=int(raw.get("last_reject_idx", 0)),
            auto_enabled=raw.get("auto_enabled", "false") == "true",
        )
    # Fall back to global (no-channel) calibration if per-channel not found
    if channel:
        raw = r.hgetall(_key(signal_type, ""))
        if raw:
            return CalibrationState(
                signal_type=signal_type,
                channel=channel,
                accept_count=int(raw.get("accept_count", 0)),
                reject_count=int(raw.get("reject_count", 0)),
                last_reject_idx=int(raw.get("last_reject_idx", 0)),
                auto_enabled=raw.get("auto_enabled", "false") == "true",
            )
    return CalibrationState(signal_type=signal_type, channel=channel)

def save_calibration(r: redis_lib.Redis, state: CalibrationState) -> None:
    r.hset(_key(state.signal_type, state.channel), mapping={
        "accept_count":    str(state.accept_count),
        "reject_count":    str(state.reject_count),
        "last_reject_idx": str(state.last_reject_idx),
        "auto_enabled":    "true" if state.auto_enabled else "false",
    })

def should_auto_file(state: CalibrationState) -> bool:
    t = _TYPE_THRESHOLDS.get(state.signal_type, {
        "verdicts":    AUTO_ENABLE_VERDICTS,
        "accept_rate": AUTO_ENABLE_ACCEPT_RATE,
        "clean_gap":   AUTO_ENABLE_NO_RECENT_REJECTS,
    })
    total = state.accept_count + state.reject_count
    if total < t["verdicts"]:
        return False
    if state.accept_count / total < t["accept_rate"]:
        return False
    if total - state.last_reject_idx < t["clean_gap"]:
        return False
    return True

def record_verdict(state: CalibrationState, verdict: str) -> None:
    total = state.accept_count + state.reject_count
    if verdict == "accept":
        state.accept_count += 1
    elif verdict in ("reject", "modify"):
        state.reject_count += 1
        state.last_reject_idx = total + 1
    state.auto_enabled = should_auto_file(state)
    # Re-disable on bad streak
    if verdict == "reject" and state.auto_enabled:
        state.auto_enabled = False
        state.accept_count = max(10, state.accept_count // 2)
        state.reject_count = 0
