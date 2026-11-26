"""detectors/steering_hits.py — trend the steering plugins' hit records.

Source records are agent.steering.hit.v1 (schemas/agent.steering.hit.v1.json),
written by the steering plugin hooks to a local hits.jsonl and mirrored into the
bus by adapters/steering-hits. This detector reads them from the bus stream
``nbus:agent.steering.hit.v1`` and falls back to the file (and its ``.1``
rotation) when the bus is unreachable or holds none.

It writes AGGREGATES to runs.db and returns no PatternCandidates; the output is
a trend line, not a per-run pathology:

  steering_hit_daily  (day, project, session_id, plugin, check_name, surface):
                      records, findings, blocks, scored records, sum of
                      avg_sentence over scored records, max sentence, words,
                      and the prose-compare lost counts. avg_sentence for any
                      group is ``avg_sentence_sum / scored``.
  steering_hit_rules  (day, project, session_id, plugin, check_name, rule):
                      finding count per rule.

Rows for every day found in the source are replaced, so a rerun is idempotent
and days that rotated out of the source stay as they were.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterator, Optional

from detectors.base import BaseDetector, PatternCandidate
from detectors.stretch_opens import project_of

STREAM = "nbus:agent.steering.hit.v1"
LOST_KEYS = ("numbers", "urls", "code", "flags", "quotes", "hedges")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS steering_hit_daily (
    day              TEXT NOT NULL,
    project          TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    plugin           TEXT NOT NULL,
    check_name       TEXT NOT NULL,
    surface          TEXT NOT NULL,
    records          INTEGER NOT NULL,
    findings         INTEGER NOT NULL,
    blocks           INTEGER NOT NULL,
    scored           INTEGER NOT NULL,
    avg_sentence_sum REAL NOT NULL,
    max_sentence     INTEGER NOT NULL,
    words            INTEGER NOT NULL,
    lost_numbers     INTEGER NOT NULL,
    lost_urls        INTEGER NOT NULL,
    lost_code        INTEGER NOT NULL,
    lost_flags       INTEGER NOT NULL,
    lost_quotes      INTEGER NOT NULL,
    lost_hedges      INTEGER NOT NULL,
    PRIMARY KEY (day, project, session_id, plugin, check_name, surface)
);
CREATE TABLE IF NOT EXISTS steering_hit_rules (
    day        TEXT NOT NULL,
    project    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    plugin     TEXT NOT NULL,
    check_name TEXT NOT NULL,
    rule       TEXT NOT NULL,
    n          INTEGER NOT NULL,
    PRIMARY KEY (day, project, session_id, plugin, check_name, rule)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def default_hits_path() -> Path:
    env = os.environ.get("AGENT_STEERING_HITS")
    if env:
        return Path(env)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "agent-steering" / "hits.jsonl"


def iter_file(path: Path) -> Iterator[dict]:
    for p in (path.with_name(path.name + ".1"), path):
        try:
            f = open(p, "r", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec


def iter_bus(redis_url: str) -> Optional[list[dict]]:
    """Records from the Redis stream, or None when the bus is unreachable."""
    try:
        import redis
        r = redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=10,
                                 decode_responses=True)
        entries = r.xrange(STREAM)
    except Exception:
        return None
    out, seen = [], set()
    for _id, fields in entries:
        raw = fields.get("_raw")
        try:
            data = json.loads(raw)["data"] if raw else None
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        key = json.dumps(data, sort_keys=True)
        if key in seen:  # the tailer is at-least-once
            continue
        seen.add(key)
        out.append(data)
    return out


def aggregate(records, since_day: Optional[str] = None) -> dict:
    daily: dict[tuple, dict] = {}
    rules: dict[tuple, int] = defaultdict(int)
    for rec in records:
        ts = rec.get("ts")
        if not isinstance(ts, str) or len(ts) < 10:
            continue
        day = ts[:10]
        if since_day and day < since_day:
            continue
        key = (day, project_of(rec.get("cwd") or ""), rec.get("session_id") or "",
               rec.get("plugin") or "", rec.get("check") or "", rec.get("surface") or "")
        row = daily.setdefault(key, dict(
            records=0, findings=0, blocks=0, scored=0, avg_sum=0.0, max_sentence=0,
            words=0, **{f"lost_{k}": 0 for k in LOST_KEYS}))
        row["records"] += 1
        row["findings"] += int(rec.get("findings") or 0)
        row["blocks"] += 1 if rec.get("action") == "block" else 0
        if isinstance(rec.get("avg_sentence"), (int, float)):
            row["scored"] += 1
            row["avg_sum"] += float(rec["avg_sentence"])
        row["max_sentence"] = max(row["max_sentence"], int(rec.get("max_sentence") or 0))
        row["words"] += int(rec.get("words") or 0)
        lost = rec.get("lost") if isinstance(rec.get("lost"), dict) else {}
        for k in LOST_KEYS:
            row[f"lost_{k}"] += int(lost.get(k) or 0)
        for rule, n in (rec.get("rules") or {}).items():
            if isinstance(n, int):
                rules[key[:5] + (rule,)] += n
    return {"daily": daily, "rules": rules}


def store(conn: sqlite3.Connection, agg: dict) -> None:
    ensure_schema(conn)
    days = sorted({k[0] for k in agg["daily"]})
    conn.execute("SAVEPOINT agg")
    try:
        for day in days:
            conn.execute("DELETE FROM steering_hit_daily WHERE day = ?", (day,))
            conn.execute("DELETE FROM steering_hit_rules WHERE day = ?", (day,))
        conn.executemany(
            "INSERT INTO steering_hit_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(*k, r["records"], r["findings"], r["blocks"], r["scored"], r["avg_sum"],
              r["max_sentence"], r["words"], *(r[f"lost_{x}"] for x in LOST_KEYS))
             for k, r in agg["daily"].items()],
        )
        conn.executemany(
            "INSERT INTO steering_hit_rules VALUES (?,?,?,?,?,?,?)",
            [(*k, n) for k, n in agg["rules"].items()],
        )
        conn.execute("RELEASE agg")
    except Exception:
        conn.execute("ROLLBACK TO agg")
        conn.execute("RELEASE agg")
        raise


class SteeringHitsDetector(BaseDetector):
    """Aggregates agent.steering.hit records into runs.db."""

    DETECTOR_NAME = "steering_hits"

    redis_url: str = "redis://localhost:6379"
    use_bus: bool = True
    hits_path: Optional[Path] = None

    def records(self) -> list[dict]:
        if self.use_bus:
            bus = iter_bus(self.redis_url)
            if bus:
                return bus
        return list(iter_file(self.hits_path or default_hits_path()))

    def detect(self, conn: sqlite3.Connection, since_ts: Optional[str] = None) -> list[PatternCandidate]:
        since_day = since_ts[:10] if since_ts and since_ts[:4].isdigit() else None
        store(conn, aggregate(self.records(), since_day))
        return []
