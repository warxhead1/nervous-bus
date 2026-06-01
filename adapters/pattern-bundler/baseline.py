# adapters/pattern-bundler/baseline.py
from __future__ import annotations
import math
from typing import Optional
import redis as redis_lib

BASELINE_TTL = 86400 * 30

def welford_empty() -> dict:
    return {"n": 0, "mean": 0.0, "M2": 0.0, "min": float("inf"), "max": float("-inf")}

def welford_update(state: dict, value: float) -> dict:
    n = state["n"] + 1
    delta = value - state["mean"]
    mean = state["mean"] + delta / n
    M2 = state["M2"] + delta * (value - mean)
    return {"n": n, "mean": mean, "M2": M2,
            "min": min(state["min"], value), "max": max(state["max"], value)}

def welford_stddev(state: dict) -> float:
    if state["n"] < 2:
        return 0.0
    return math.sqrt(state["M2"] / state["n"])

def welford_deviation(state: dict, current: float) -> Optional[float]:
    if state["n"] < 100:
        return None
    sd = welford_stddev(state)
    if sd < 0.001:
        return None
    return (current - state["mean"]) / sd

# Redis I/O

def _bl_key(channel: str) -> str:
    return f"pattern:baseline:{channel}"

def _th_key(channel: str) -> str:
    return f"pattern:thresholds:{channel}"

def load_baseline_redis(r: redis_lib.Redis, channel: str) -> dict:
    raw = r.hgetall(_bl_key(channel))
    if not raw:
        return welford_empty()
    return {
        "n": int(raw.get("n", 0)),
        "mean": float(raw.get("mean", 0.0)),
        "M2": float(raw.get("M2", 0.0)),
        "min": float(raw.get("min", float("inf"))),
        "max": float(raw.get("max", float("-inf"))),
    }

def save_baseline_redis(r: redis_lib.Redis, channel: str, state: dict) -> None:
    pipe = r.pipeline()
    pipe.hset(_bl_key(channel), mapping={k: str(v) for k, v in state.items()})
    pipe.expire(_bl_key(channel), BASELINE_TTL)
    pipe.execute()

def init_thresholds(r: redis_lib.Redis, channel: str) -> None:
    key = _th_key(channel)
    if not r.exists(key):
        r.hset(key, mapping={"warn_sigma": "2.0", "alert_sigma": "3.0"})

def read_thresholds(r: redis_lib.Redis, channel: str) -> dict:
    raw = r.hgetall(_th_key(channel))
    return {
        "warn_sigma": float(raw.get("warn_sigma", 2.0)),
        "alert_sigma": float(raw.get("alert_sigma", 3.0)),
    }
