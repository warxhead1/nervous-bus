# adapters/pattern-bundler/window.py
from __future__ import annotations
import json, math, time
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.dirname(__file__))
from fingerprint import fingerprint as _fp
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

@dataclass
class Window:
    channel: str
    source_stream: str  # "bus" or "logs"
    count_trigger: int
    time_trigger_s: float

    opened_at: float = field(default_factory=time.time)
    events: Deque[str] = field(default_factory=lambda: deque(maxlen=20))
    count: int = 0
    error_count: int = 0
    _num_fields: Dict[str, list] = field(default_factory=lambda: defaultdict(list))
    _str_fields: Dict[str, list] = field(default_factory=lambda: defaultdict(list))
    error_events: list = field(default_factory=list)
    fp_counts: dict = field(default_factory=dict)

    def ingest(self, raw: str, data: dict) -> None:
        self.count += 1
        self.events.append(raw[:500])
        for k, v in data.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                self._num_fields[f"data.{k}"].append(float(v))
            elif isinstance(v, str) and len(v) < 64:
                self._str_fields[f"data.{k}"].append(v)

    def ingest_log(self, entry: dict) -> None:
        self.count += 1
        self.events.append(json.dumps(entry)[:500])
        if entry.get("level") in ("error", "critical"):
            self.error_count += 1
            snapshot = json.dumps({
                "service": entry.get("service", ""),
                "level":   entry.get("level", ""),
                "message": entry.get("message", "")[:300],
                "log_source": entry.get("log_source", ""),
            })
            self.error_events.append(snapshot)
            if len(self.error_events) > 20:
                self.error_events = self.error_events[-20:]
        msg = entry.get("message", "")
        if msg and msg != "[binary data]":
            fp = _fp(msg)
            self.fp_counts[fp] = self.fp_counts.get(fp, 0) + 1

    def should_close(self) -> bool:
        return self.count >= self.count_trigger or (time.time() - self.opened_at) >= self.time_trigger_s

    def is_low_interest(self, deviation: Optional[float]) -> bool:
        if deviation is None:
            return True
        if abs(deviation) >= 1.0:
            return False
        if self.error_count > 0:
            return False
        return True

    def compute_stats(self, deviation: Optional[float]) -> dict:
        elapsed = max(1.0, time.time() - self.opened_at)
        rate = self.count / elapsed * 60.0
        field_stats: dict = {}
        for k, vals in self._num_fields.items():
            if vals:
                mean = sum(vals) / len(vals)
                var = sum((v - mean) ** 2 for v in vals) / len(vals) if len(vals) > 1 else 0.0
                # population variance (÷N) — intentional; within-window stats, not cross-window baseline
                field_stats[k] = {"mean": round(mean, 3), "stddev": round(math.sqrt(var), 3),
                                   "min": min(vals), "max": max(vals)}
        for k, vals in self._str_fields.items():
            field_stats[k] = {"top_values": dict(Counter(vals).most_common(5))}
        error_rate = self.error_count / max(1, self.count)
        return {
            "rate_per_min": round(rate, 3),
            "baseline_deviation": round(deviation, 3) if deviation is not None else None,
            "baseline_n": 0,
            "field_stats": field_stats,
            "error_rate": round(error_rate, 4),
        }
