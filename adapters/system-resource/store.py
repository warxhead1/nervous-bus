"""Typed, bounded history. One transaction records each complete measurement batch."""
import json
import sqlite3
from pathlib import Path

METRICS = {"cpu_usec": "cpu_usage_usec", "throttled_usec": "cpu_throttled_usec",
           "read_bytes": "io_read_bytes", "write_bytes": "io_write_bytes",
           "high_events": "memory_high_events"}
PRESSURES = ("cpu_psi", "memory_psi", "io_psi")
KEYS = "window_ts,host,boot_id,entity,identity,digest"
RAW_CAP = 50_000
ROLLUP_CAP = 100_000


def pressure_value(entity, kind):
    value = entity.get("pressure", {}).get(kind)
    return (value or {}).get("some", {}).get("avg10")


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        metric_sql = ",".join(f"{name} REAL,{name}_coverage_s REAL NOT NULL" for name in METRICS)
        pressure_sql = ",".join(f"{name} REAL" for name in PRESSURES)
        self.db.executescript(f"""
        CREATE TABLE IF NOT EXISTS samples (
          id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, ts REAL NOT NULL,
          monotonic_s REAL NOT NULL, host TEXT NOT NULL, boot_id TEXT NOT NULL,
          entity TEXT NOT NULL, identity TEXT NOT NULL, digest TEXT NOT NULL,
          state TEXT NOT NULL, interval_state TEXT NOT NULL, elapsed_s REAL,
          {metric_sql}, memory_bytes REAL, {pressure_sql}, payload TEXT NOT NULL,
          delivery TEXT NOT NULL, rolled_up INTEGER NOT NULL DEFAULT 0, UNIQUE(batch_id,entity));
        CREATE INDEX IF NOT EXISTS sample_time ON samples(ts);
        CREATE INDEX IF NOT EXISTS sample_entity ON samples(host,entity,id DESC);
        CREATE INDEX IF NOT EXISTS pending_rollup ON samples(rolled_up,ts);
        CREATE TABLE IF NOT EXISTS rollups (
          window_ts INTEGER NOT NULL, host TEXT NOT NULL, boot_id TEXT NOT NULL,
          entity TEXT NOT NULL, identity TEXT NOT NULL, digest TEXT NOT NULL,
          last_id INTEGER NOT NULL, sample_count INTEGER NOT NULL,
          valid_samples INTEGER NOT NULL, invalid_samples INTEGER NOT NULL,
          elapsed_s REAL, {metric_sql}, memory_bytes REAL, {pressure_sql},
          PRIMARY KEY({KEYS}));
        CREATE TABLE IF NOT EXISTS findings (
          key TEXT PRIMARY KEY, ts REAL NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value INTEGER NOT NULL);
        """)

    def close(self):
        self.db.close()

    def previous(self, host, entity):
        row = self.db.execute("SELECT payload FROM samples WHERE host=? AND entity=? ORDER BY id DESC LIMIT 1",
                              (host, entity)).fetchone()
        return json.loads(row[0]) if row else None

    def insert_batch(self, event):
        for entity in event["entities"]:
            interval = entity["interval"]
            payload = {"boot_id": event["boot_id"], "ts": event["ts"],
                       "monotonic_s": event["monotonic_s"], "collector": event["collector"], "entity_data": entity}
            row = {"batch_id": event["id"], "ts": event["ts"], "monotonic_s": event["monotonic_s"],
                   "host": event["host"], "boot_id": event["boot_id"] or "", "entity": entity["entity"],
                   "identity": entity["identity"] or "", "digest": event["collector"]["digest"],
                   "state": entity["state"], "interval_state": interval["state"],
                   "elapsed_s": interval["elapsed_s"], "memory_bytes": entity["gauges"].get("memory_current_bytes"),
                   "payload": json.dumps(payload, separators=(",", ":")), "delivery": "pending"}
            for column, counter in METRICS.items():
                row[column] = interval["deltas"].get(counter)
                row[column + "_coverage_s"] = interval["elapsed_s"] if row[column] is not None else 0
            for kind in ("cpu", "memory", "io"):
                row[kind + "_psi"] = pressure_value(entity, kind)
            self.db.execute(f"INSERT INTO samples ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))

    def mark_delivery(self, batch_id, delivery):
        self.db.execute("UPDATE samples SET delivery=? WHERE batch_id=?", (delivery, batch_id))
        self.db.commit()

    def rollup(self, now):
        # Each completed raw row is folded once, atomically with its marker. This
        # also handles late timestamps without re-aggregating pruned history.
        columns = [*KEYS.split(","), "last_id", "sample_count", "valid_samples", "invalid_samples", "elapsed_s"]
        values = ["CAST(ts/300 AS INTEGER)*300", "host", "boot_id", "entity", "identity", "digest",
                  "MAX(id)", "COUNT(*)", "SUM(interval_state='ok')", "SUM(interval_state!='ok')", "SUM(elapsed_s)"]
        for metric in METRICS:
            columns.extend([metric, metric + "_coverage_s"])
            values.extend([f"SUM({metric})", f"SUM({metric}_coverage_s)"])
        columns.extend(["memory_bytes", *PRESSURES])
        values.extend(["MAX(memory_bytes)", *(f"MAX({p})" for p in PRESSURES)])
        updates = []
        for column in columns[6:]:
            aggregate = "MAX" if column in ("last_id", "memory_bytes", *PRESSURES) else None
            left, right = f"rollups.{column}", f"excluded.{column}"
            expression = f"{aggregate}({left},{right})" if aggregate else f"{left}+{right}"
            updates.append(f"{column}=CASE WHEN {left} IS NULL THEN {right} WHEN {right} IS NULL THEN {left} ELSE {expression} END")
        boundary = int(now // 300) * 300
        self.db.execute(f"""INSERT INTO rollups ({','.join(columns)})
            SELECT {','.join(values)} FROM samples WHERE rolled_up=0 AND ts<?
            GROUP BY CAST(ts/300 AS INTEGER),host,boot_id,entity,identity,digest
            ON CONFLICT({KEYS}) DO UPDATE SET {','.join(updates)}""", (boundary,))
        self.db.execute("UPDATE samples SET rolled_up=1 WHERE rolled_up=0 AND ts<?", (boundary,))

    def retain(self, now, raw_hours=24, rollup_days=30, raw_cap=RAW_CAP, rollup_cap=ROLLUP_CAP):
        self.db.execute("DELETE FROM samples WHERE ts<?", (now - raw_hours * 3600,))
        self.db.execute("DELETE FROM rollups WHERE window_ts<?", (now - rollup_days * 86400,))
        count = self.db.execute("DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?)",
                                (min(RAW_CAP, raw_cap),)).rowcount
        if count:
            self.db.execute("INSERT INTO metadata VALUES ('raw_cap_evictions',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (count,))
        self.db.execute("DELETE FROM rollups WHERE rowid IN (SELECT rowid FROM rollups ORDER BY window_ts DESC LIMIT -1 OFFSET ?)",
                        (min(ROLLUP_CAP, rollup_cap),))
        self.db.execute("DELETE FROM findings WHERE ts<?", (now - 30 * 86400,))
        self.db.execute("DELETE FROM findings WHERE key IN (SELECT key FROM findings ORDER BY ts DESC LIMIT -1 OFFSET 1000)")

    def commit(self):
        self.db.commit()
