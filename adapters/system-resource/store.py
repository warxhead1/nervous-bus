"""Typed, bounded SQLite history for system-resource samples and findings."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, host TEXT NOT NULL, boot_id TEXT,
          cgroup TEXT NOT NULL, cgroup_id TEXT, state TEXT NOT NULL, interval_state TEXT NOT NULL,
          elapsed_s REAL, cpu_usage_usec INTEGER, cpu_throttled_usec INTEGER, cpu_quota_usec INTEGER,
          cpu_period_usec INTEGER, host_memory_total_bytes INTEGER, host_memory_available_bytes INTEGER,
          host_swap_free_bytes INTEGER, host_disk_read_sectors INTEGER, host_disk_write_sectors INTEGER,
          memory_current_bytes INTEGER, memory_anon_bytes INTEGER, memory_file_bytes INTEGER,
          memory_swap_bytes INTEGER, memory_high_bytes INTEGER, memory_max_bytes INTEGER,
          io_read_bytes INTEGER, io_write_bytes INTEGER, cpu_usage_usec_delta INTEGER,
          cpu_throttled_usec_delta INTEGER, io_read_bytes_delta INTEGER, io_write_bytes_delta INTEGER,
          pressure_json TEXT NOT NULL, cgroup_pressure_json TEXT NOT NULL, events_json TEXT NOT NULL, collector_version TEXT NOT NULL,
          collector_digest TEXT NOT NULL, duration_ms REAL NOT NULL, delivery TEXT NOT NULL DEFAULT 'pending');
        CREATE INDEX IF NOT EXISTS samples_time ON samples(ts DESC);
        CREATE INDEX IF NOT EXISTS samples_consumer ON samples(ts DESC, cgroup);
        CREATE TABLE IF NOT EXISTS rollups (
          window_ts INTEGER NOT NULL, host TEXT NOT NULL, boot_id TEXT, cgroup TEXT NOT NULL, samples INTEGER NOT NULL,
          duration_s REAL, cpu_usage_usec_delta INTEGER, cpu_throttled_usec_delta INTEGER,
          io_read_bytes_delta INTEGER, io_write_bytes_delta INTEGER, memory_max_bytes INTEGER,
          pressure_max REAL, missing_samples INTEGER NOT NULL, PRIMARY KEY(window_ts, host, boot_id, cgroup));
        CREATE TABLE IF NOT EXISTS findings (key TEXT PRIMARY KEY, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
          count INTEGER NOT NULL, last_delivery TEXT NOT NULL, evidence_json TEXT NOT NULL);
        """)

    def previous(self, host, cgroup):
        row = self.db.execute("SELECT * FROM samples WHERE host=? AND cgroup=? ORDER BY id DESC LIMIT 1", (host, cgroup)).fetchone()
        return dict(row) if row else None

    def insert(self, sample):
        fields = [k for k in sample if k in {r[1] for r in self.db.execute("PRAGMA table_info(samples)")}]
        self.db.execute("INSERT INTO samples (%s) VALUES (%s)" % (",".join(fields), ",".join("?" * len(fields))), [sample[k] for k in fields])

    def mark_delivery(self, ts, host, cgroup, delivery):
        self.db.execute("UPDATE samples SET delivery=? WHERE id=(SELECT id FROM samples WHERE ts=? AND host=? AND cgroup=? ORDER BY id DESC LIMIT 1)", (delivery, ts, host, cgroup))

    def retain(self, now, raw_hours=24, rollup_days=30):
        self.db.execute("DELETE FROM samples WHERE ts < ?", (now - raw_hours * 3600,))
        self.db.execute("DELETE FROM rollups WHERE window_ts < ?", (int(now - rollup_days * 86400),))

    def commit(self):
        self.db.commit()

    def rollup(self, now):
        current_window = int(now // 300) * 300
        windows = self.db.execute("SELECT DISTINCT CAST(ts / 300 AS INTEGER) * 300 AS start FROM samples WHERE ts < ?", (current_window,)).fetchall()
        for item in windows:
            start = item["start"]
            rows = self.db.execute("SELECT * FROM samples WHERE ts>=? AND ts<?", (start, start + 300)).fetchall()
            for key in {(r["host"], r["boot_id"], r["cgroup"]) for r in rows if r["boot_id"]}:
                group = [r for r in rows if (r["host"], r["boot_id"], r["cgroup"]) == key]
                def numeric(name):
                    values = [r[name] for r in group]
                    return sum(values) if all(value is not None for value in values) else None
                maxmem = max((r["memory_current_bytes"] or 0) for r in group) or None
                pressures = [json.loads(r["cgroup_pressure_json"]).get("memory", {}).get("some", {}).get("avg10") for r in group]
                psi = max((value for value in pressures if value is not None), default=None)
                elapsed = numeric("elapsed_s")
                self.db.execute("INSERT OR REPLACE INTO rollups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (start, *key, len(group), elapsed, numeric("cpu_usage_usec_delta"), numeric("cpu_throttled_usec_delta"), numeric("io_read_bytes_delta"), numeric("io_write_bytes_delta"), maxmem, psi, sum(r["state"] != "ok" for r in group)))

    def finding(self, key, now, evidence, cooldown_s):
        old = self.db.execute("SELECT * FROM findings WHERE key=?", (key,)).fetchone()
        if old and now - old["last_ts"] < cooldown_s:
            return False
        first = old["first_ts"] if old else now
        count = (old["count"] if old else 0) + 1
        self.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?)", (key, first, now, count, "pending", json.dumps(evidence, sort_keys=True)))
        return True

    def finding_delivery(self, key, delivery):
        self.db.execute("UPDATE findings SET last_delivery=? WHERE key=?", (delivery, key))

    def sustained_pressure(self, host, cgroup, threshold=3):
        rows = self.db.execute("SELECT ts,boot_id,pressure_json FROM samples WHERE host=? AND cgroup=? AND state='ok' ORDER BY id DESC LIMIT ?", (host, cgroup, threshold)).fetchall()
        if len(rows) != threshold:
            return False
        return len({row["ts"] for row in rows}) == threshold and len({row["boot_id"] for row in rows}) == 1 and rows[0]["boot_id"] is not None and all(json.loads(row["pressure_json"]).get("memory", {}).get("some", {}).get("avg10", 0) >= 10 for row in rows)

    def report(self, since, limit=120, now=None):
        now = now or time.time(); since = max(since, now - 30 * 86400); limit = max(1, min(limit, 500))
        rows = self.db.execute("SELECT * FROM samples WHERE ts>=? ORDER BY ts DESC LIMIT ?", (since, limit)).fetchall()
        rollups = self.db.execute("SELECT * FROM rollups WHERE window_ts>=? ORDER BY window_ts DESC LIMIT ?", (since, limit)).fetchall()
        top = self.db.execute("SELECT host,cgroup,MAX(memory_current_bytes) AS memory_peak_bytes,SUM(cpu_usage_usec_delta) AS cpu_usage_usec_delta,SUM(io_read_bytes_delta) AS io_read_bytes_delta,SUM(io_write_bytes_delta) AS io_write_bytes_delta,COUNT(*) AS sample_count FROM samples WHERE ts>=? GROUP BY host,cgroup ORDER BY memory_peak_bytes DESC,cpu_usage_usec_delta DESC LIMIT 20", (since,)).fetchall()
        deliveries = self.db.execute("SELECT delivery,COUNT(*) AS count FROM samples WHERE ts>=? GROUP BY delivery", (since,)).fetchall()
        latest = max((r["ts"] for r in rows), default=None)
        serial = lambda row: dict(row)
        return {"since": since, "query_limit": limit, "freshness_s": None if latest is None else now - latest,
                "top_consumers": [serial(r) for r in top], "pressure_over_time": [serial(r) for r in rollups],
                "health": {"sample_count": len(rows), "failed_samples": sum(r["state"] != "ok" for r in rows),
                           "delivery": [serial(r) for r in deliveries], "stale": latest is None or now - latest > 180}}
