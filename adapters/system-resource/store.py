"""Typed, bounded history. One transaction records each complete measurement batch."""
import json
import math
import sqlite3
from pathlib import Path

METRICS = {"cpu_usec": "cpu_usage_usec", "throttled_usec": "cpu_throttled_usec",
           "read_bytes": "io_read_bytes", "write_bytes": "io_write_bytes",
           "high_events": "memory_high_events"}
PRESSURES = ("cpu_psi", "memory_psi", "io_psi")
KEYS = "window_ts,host,boot_id,entity,identity,digest"
RAW_CAP = 50_000
ROLLUP_CAP = 100_000
GPU_METRICS = ("memory_total_bytes", "memory_used_bytes", "memory_free_bytes", "utilization_pct")
GPU_STATUS_STATES = ("ok", "partial", "unavailable", "empty")


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
        gpu_gauge_sql = ",".join(("memory_total_bytes INTEGER", "memory_used_bytes INTEGER",
                                  "memory_free_bytes INTEGER", "utilization_pct REAL"))
        gpu_rollup_sql = ",".join(
            f"{name}_min REAL,{name}_max REAL,{name}_sum REAL NOT NULL,{name}_count INTEGER NOT NULL"
            for name in GPU_METRICS)
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
        CREATE TABLE IF NOT EXISTS gpu_batches (
          id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE, ts REAL NOT NULL,
          monotonic_s REAL NOT NULL, host TEXT NOT NULL, boot_id TEXT NOT NULL,
          collector_version TEXT NOT NULL, collector_digest TEXT NOT NULL,
          state TEXT NOT NULL, reason TEXT, attempted INTEGER NOT NULL,
          query_ms REAL NOT NULL, delivery TEXT NOT NULL,
          rolled_up INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS gpu_batch_time ON gpu_batches(ts);
        CREATE INDEX IF NOT EXISTS pending_gpu_rollup ON gpu_batches(rolled_up,ts);
        CREATE TABLE IF NOT EXISTS gpu_gauges (
          id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, ts REAL NOT NULL,
          host TEXT NOT NULL, boot_id TEXT NOT NULL, collector_version TEXT NOT NULL,
          collector_digest TEXT NOT NULL, uuid TEXT NOT NULL, driver_version TEXT NOT NULL,
          state TEXT NOT NULL, reason TEXT, {gpu_gauge_sql},
          UNIQUE(batch_id,uuid));
        CREATE INDEX IF NOT EXISTS gpu_gauge_batch ON gpu_gauges(batch_id);
        CREATE INDEX IF NOT EXISTS gpu_gauge_time ON gpu_gauges(ts);
        CREATE TABLE IF NOT EXISTS gpu_status_rollups (
          window_ts INTEGER NOT NULL, host TEXT NOT NULL, boot_id TEXT NOT NULL,
          collector_version TEXT NOT NULL, collector_digest TEXT NOT NULL,
          status_count INTEGER NOT NULL, attempt_count INTEGER NOT NULL,
          ok_count INTEGER NOT NULL, partial_count INTEGER NOT NULL,
          unavailable_count INTEGER NOT NULL, empty_count INTEGER NOT NULL,
          device_sample_count INTEGER NOT NULL,
          PRIMARY KEY(window_ts,host,boot_id,collector_version,collector_digest));
        CREATE TABLE IF NOT EXISTS gpu_rollups (
          window_ts INTEGER NOT NULL, host TEXT NOT NULL, boot_id TEXT NOT NULL,
          collector_version TEXT NOT NULL, collector_digest TEXT NOT NULL,
          uuid TEXT NOT NULL, driver_version TEXT NOT NULL,
          last_observed_ts REAL, attempt_count INTEGER NOT NULL,
          observed_sample_count INTEGER NOT NULL, missing_sample_count INTEGER NOT NULL,
          {gpu_rollup_sql},
          PRIMARY KEY(window_ts,host,boot_id,collector_version,collector_digest,uuid,driver_version));
        CREATE INDEX IF NOT EXISTS gpu_rollup_time ON gpu_rollups(window_ts);
        """)

    def close(self):
        self.db.close()

    def previous(self, host, entity):
        row = self.db.execute("SELECT payload FROM samples WHERE host=? AND entity=? ORDER BY id DESC LIMIT 1",
                              (host, entity)).fetchone()
        return json.loads(row[0]) if row else None

    def insert_batch(self, event, gpu_max_gap_s=None):
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
        self.insert_gpu_batch(event, gpu_max_gap_s)

    @staticmethod
    def _gpu_number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
        return value

    def insert_gpu_batch(self, event, gpu_max_gap_s=None):
        """Persist query status and typed GPU gauges without touching entity rows."""
        gpu = event.get("gpu") or {}
        state = gpu.get("state") if gpu.get("state") in GPU_STATUS_STATES else "unavailable"
        reason = gpu.get("reason") if isinstance(gpu.get("reason"), str) else None
        attempted = bool(gpu.get("attempted"))
        query_ms = self._gpu_number(gpu.get("query_ms"))
        collector = event["collector"]
        identity = {"batch_id": event["id"], "ts": event["ts"], "monotonic_s": event["monotonic_s"],
                    "host": event["host"], "boot_id": event["boot_id"] or "",
                    "collector_version": collector["version"], "collector_digest": collector["digest"]}
        prior_devices = []
        if attempted and type(gpu_max_gap_s) in (int, float) and gpu_max_gap_s > 0 and identity["boot_id"]:
            previous = self.db.execute("""
                SELECT batch_id,monotonic_s,boot_id,collector_version,collector_digest
                FROM gpu_batches WHERE host=? ORDER BY id DESC LIMIT 1""", (identity["host"],)).fetchone()
            if (previous and previous["boot_id"] == identity["boot_id"]
                    and previous["collector_version"] == identity["collector_version"]
                    and previous["collector_digest"] == identity["collector_digest"]):
                elapsed = identity["monotonic_s"] - previous["monotonic_s"]
                if 0 < elapsed <= gpu_max_gap_s:
                    prior_devices = self.db.execute("""
                        SELECT uuid,driver_version FROM gpu_gauges
                        WHERE batch_id=? AND state!='unavailable' ORDER BY uuid""",
                                                   (previous["batch_id"],)).fetchall()
        batch = {**identity, "state": state, "reason": reason, "attempted": int(attempted),
                 "query_ms": 0 if query_ms is None else query_ms, "delivery": "pending"}
        self.db.execute(f"INSERT INTO gpu_batches ({','.join(batch)}) VALUES ({','.join('?' for _ in batch)})",
                        tuple(batch.values()))
        devices = gpu.get("devices") if isinstance(gpu.get("devices"), list) else []
        seen = set()
        for device in devices[:8]:
            if not isinstance(device, dict):
                continue
            uuid = device.get("uuid")
            if not isinstance(uuid, str) or not uuid or uuid in seen:
                continue
            seen.add(uuid)
            driver = device.get("driver_version")
            gauges = {name: self._gpu_number(device.get(name)) for name in GPU_METRICS}
            device_state = device.get("state") if device.get("state") in ("ok", "partial") else "partial"
            row = {**{key: identity[key] for key in ("batch_id", "ts", "host", "boot_id", "collector_version", "collector_digest")},
                   "uuid": uuid, "driver_version": driver if isinstance(driver, str) else "",
                   "state": device_state,
                   "reason": device.get("reason") if isinstance(device.get("reason"), str) else None,
                   **gauges}
            self.db.execute(f"INSERT INTO gpu_gauges ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                            tuple(row.values()))
        if prior_devices:
            missing_reason = "not_observed" if state in ("ok", "empty") else "query_" + state
            for prior in prior_devices:
                if prior["uuid"] in seen or len(seen) >= 8:
                    continue
                seen.add(prior["uuid"])
                row = {**{key: identity[key] for key in ("batch_id", "ts", "host", "boot_id", "collector_version", "collector_digest")},
                       "uuid": prior["uuid"], "driver_version": prior["driver_version"],
                       "state": "unavailable", "reason": missing_reason,
                       **{name: None for name in GPU_METRICS}}
                self.db.execute(f"INSERT INTO gpu_gauges ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                                tuple(row.values()))

    def mark_delivery(self, batch_id, delivery):
        self.db.execute("UPDATE samples SET delivery=? WHERE batch_id=?", (delivery, batch_id))
        self.db.execute("UPDATE gpu_batches SET delivery=? WHERE batch_id=?", (delivery, batch_id))
        self.db.commit()

    def rollup_gpu(self, now):
        """Fold completed GPU batches once, with status and gauge markers together."""
        boundary = int(now // 300) * 300
        status_keys = "window_ts,host,boot_id,collector_version,collector_digest"
        status_updates = ",".join(
            f"{column}=gpu_status_rollups.{column}+excluded.{column}"
            for column in ("status_count", "attempt_count", "ok_count", "partial_count",
                           "unavailable_count", "empty_count", "device_sample_count"))
        self.db.execute(f"""
            WITH pending AS (
              SELECT *,CAST(ts/300 AS INTEGER)*300 AS window_ts
              FROM gpu_batches WHERE rolled_up=0 AND ts<?),
            status_groups AS (
              SELECT window_ts,host,boot_id,collector_version,collector_digest,
                COUNT(*) AS status_count,COALESCE(SUM(attempted),0) AS attempt_count,
                SUM(CASE WHEN state='ok' THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN state='partial' THEN 1 ELSE 0 END) AS partial_count,
                SUM(CASE WHEN state='unavailable' THEN 1 ELSE 0 END) AS unavailable_count,
                SUM(CASE WHEN state='empty' THEN 1 ELSE 0 END) AS empty_count
              FROM pending GROUP BY {status_keys}),
            device_groups AS (
              SELECT p.window_ts,p.host,p.boot_id,p.collector_version,p.collector_digest,
                SUM(CASE WHEN g.state!='unavailable' THEN 1 ELSE 0 END) AS device_sample_count
              FROM pending p JOIN gpu_gauges g ON g.batch_id=p.batch_id
              GROUP BY p.window_ts,p.host,p.boot_id,p.collector_version,p.collector_digest)
            INSERT INTO gpu_status_rollups
              ({status_keys},status_count,attempt_count,ok_count,partial_count,
               unavailable_count,empty_count,device_sample_count)
            SELECT s.window_ts,s.host,s.boot_id,s.collector_version,s.collector_digest,
              s.status_count,s.attempt_count,s.ok_count,s.partial_count,s.unavailable_count,
              s.empty_count,COALESCE(d.device_sample_count,0)
            FROM status_groups s LEFT JOIN device_groups d USING ({status_keys})
            WHERE 1
            ON CONFLICT({status_keys}) DO UPDATE SET {status_updates}
            """, (boundary,))

        metric_values = []
        for name in GPU_METRICS:
            metric_values.extend((f"MIN(g.{name}) AS {name}_min", f"MAX(g.{name}) AS {name}_max",
                                  f"COALESCE(SUM(g.{name}),0) AS {name}_sum",
                                  f"COUNT(g.{name}) AS {name}_count"))
        metric_updates = []
        for name in GPU_METRICS:
            for suffix, aggregate in (("min", "MIN"), ("max", "MAX")):
                column = f"{name}_{suffix}"
                metric_updates.append(
                    f"{column}=CASE WHEN gpu_rollups.{column} IS NULL THEN excluded.{column} "
                    f"WHEN excluded.{column} IS NULL THEN gpu_rollups.{column} "
                    f"ELSE {aggregate}(gpu_rollups.{column},excluded.{column}) END")
            for suffix in ("sum", "count"):
                column = f"{name}_{suffix}"
                metric_updates.append(f"{column}=gpu_rollups.{column}+excluded.{column}")
        gauge_keys = "window_ts,host,boot_id,collector_version,collector_digest,uuid,driver_version"
        gauge_group = ",".join("d." + key for key in gauge_keys.split(","))
        gauge_updates = [
            "last_observed_ts=CASE WHEN gpu_rollups.last_observed_ts IS NULL THEN excluded.last_observed_ts "
            "WHEN excluded.last_observed_ts IS NULL THEN gpu_rollups.last_observed_ts "
            "ELSE MAX(gpu_rollups.last_observed_ts,excluded.last_observed_ts) END",
            # The status rollup is the complete window denominator, including
            # batches folded before this UUID or driver was first observed.
            "attempt_count=excluded.attempt_count",
            "observed_sample_count=gpu_rollups.observed_sample_count+excluded.observed_sample_count",
            "missing_sample_count=gpu_rollups.missing_sample_count+excluded.missing_sample_count-gpu_rollups.attempt_count",
            *metric_updates,
        ]
        gauge_columns = ("last_observed_ts,attempt_count,observed_sample_count,missing_sample_count," +
                         ",".join(f"{name}_{suffix}" for name in GPU_METRICS
                                  for suffix in ("min", "max", "sum", "count")))
        self.db.execute(f"""
            WITH pending AS (
              SELECT *,CAST(ts/300 AS INTEGER)*300 AS window_ts
              FROM gpu_batches WHERE rolled_up=0 AND ts<?),
            segments AS (
              SELECT DISTINCT window_ts,host,boot_id,collector_version,collector_digest FROM pending),
            device_keys AS (
              SELECT DISTINCT p.window_ts,p.host,p.boot_id,p.collector_version,p.collector_digest,
                g.uuid,g.driver_version
              FROM pending p JOIN gpu_gauges g ON g.batch_id=p.batch_id
              UNION
              SELECT r.window_ts,r.host,r.boot_id,r.collector_version,r.collector_digest,
                r.uuid,r.driver_version
              FROM gpu_rollups r JOIN segments s
                ON s.window_ts=r.window_ts AND s.host=r.host AND s.boot_id=r.boot_id
                AND s.collector_version=r.collector_version AND s.collector_digest=r.collector_digest),
            aggregates AS (
              SELECT d.window_ts,d.host,d.boot_id,d.collector_version,d.collector_digest,
                d.uuid,d.driver_version,
                MAX(CASE WHEN g.id IS NOT NULL AND g.state!='unavailable' THEN p.ts END) AS last_observed_ts,
                MAX(s.attempt_count) AS attempt_count,
                SUM(CASE WHEN g.id IS NOT NULL AND g.state!='unavailable' THEN 1 ELSE 0 END) AS observed_sample_count,
                MAX(s.attempt_count)-SUM(CASE WHEN p.attempted=1 AND g.id IS NOT NULL AND g.state!='unavailable' THEN 1 ELSE 0 END) AS missing_sample_count,
                {','.join(metric_values)}
              FROM device_keys d JOIN pending p
                ON p.window_ts=d.window_ts AND p.host=d.host AND p.boot_id=d.boot_id
                AND p.collector_version=d.collector_version AND p.collector_digest=d.collector_digest
              JOIN gpu_status_rollups s
                ON s.window_ts=d.window_ts AND s.host=d.host AND s.boot_id=d.boot_id
                AND s.collector_version=d.collector_version AND s.collector_digest=d.collector_digest
              LEFT JOIN gpu_gauges g ON g.batch_id=p.batch_id AND g.uuid=d.uuid
                AND g.driver_version=d.driver_version
              GROUP BY {gauge_group})
            INSERT INTO gpu_rollups ({gauge_keys},{gauge_columns})
            SELECT {gauge_keys},{gauge_columns} FROM aggregates WHERE 1
            ON CONFLICT({gauge_keys}) DO UPDATE SET {','.join(gauge_updates)}
            """, (boundary,))
        self.db.execute("UPDATE gpu_batches SET rolled_up=1 WHERE rolled_up=0 AND ts<?", (boundary,))

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
        self.rollup_gpu(now)

    def _increment_metadata(self, key, amount):
        if amount:
            self.db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
                            (key, amount))

    def retain_gpu(self, now, raw_hours, rollup_days, raw_cap, rollup_cap):
        """Bound GPU status/gauge rows independently from host and cgroup rows."""
        raw_cutoff = now - raw_hours * 3600
        self.db.execute("DELETE FROM gpu_gauges WHERE batch_id IN (SELECT batch_id FROM gpu_batches WHERE ts<?)",
                        (raw_cutoff,))
        self.db.execute("DELETE FROM gpu_batches WHERE ts<?", (raw_cutoff,))
        status_offset = min(RAW_CAP, raw_cap)
        status_ids = "SELECT id FROM gpu_batches ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?"
        status_evictions = self.db.execute(f"SELECT COUNT(*) FROM gpu_batches WHERE id IN ({status_ids})",
                                           (status_offset,)).fetchone()[0]
        if status_evictions:
            self.db.execute(f"DELETE FROM gpu_gauges WHERE batch_id IN (SELECT batch_id FROM gpu_batches WHERE id IN ({status_ids}))",
                            (status_offset,))
            self.db.execute(f"DELETE FROM gpu_batches WHERE id IN ({status_ids})", (status_offset,))
        self._increment_metadata("gpu_raw_status_cap_evictions", status_evictions)
        gauge_offset = min(RAW_CAP, raw_cap)
        gauge_evictions = self.db.execute(
            "DELETE FROM gpu_gauges WHERE id IN (SELECT id FROM gpu_gauges ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?)",
            (gauge_offset,)).rowcount
        self._increment_metadata("gpu_raw_gauge_cap_evictions", gauge_evictions)
        rollup_cutoff = now - rollup_days * 86400
        self.db.execute("DELETE FROM gpu_status_rollups WHERE window_ts<?", (rollup_cutoff,))
        self.db.execute("DELETE FROM gpu_rollups WHERE window_ts<?", (rollup_cutoff,))
        rollup_offset = min(ROLLUP_CAP, rollup_cap)
        self.db.execute("DELETE FROM gpu_status_rollups WHERE rowid IN "
                        "(SELECT rowid FROM gpu_status_rollups ORDER BY window_ts DESC LIMIT -1 OFFSET ?)",
                        (rollup_offset,))
        self.db.execute("DELETE FROM gpu_rollups WHERE rowid IN "
                        "(SELECT rowid FROM gpu_rollups ORDER BY window_ts DESC LIMIT -1 OFFSET ?)",
                        (rollup_offset,))

    def retain(self, now, raw_hours=24, rollup_days=30, raw_cap=RAW_CAP, rollup_cap=ROLLUP_CAP):
        self.db.execute("DELETE FROM samples WHERE ts<?", (now - raw_hours * 3600,))
        self.db.execute("DELETE FROM rollups WHERE window_ts<?", (now - rollup_days * 86400,))
        count = self.db.execute("DELETE FROM samples WHERE id IN (SELECT id FROM samples ORDER BY ts DESC,id DESC LIMIT -1 OFFSET ?)",
                                (min(RAW_CAP, raw_cap),)).rowcount
        if count:
            self.db.execute("INSERT INTO metadata VALUES ('raw_cap_evictions',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (count,))
        self.db.execute("DELETE FROM rollups WHERE rowid IN (SELECT rowid FROM rollups ORDER BY window_ts DESC LIMIT -1 OFFSET ?)",
                        (min(ROLLUP_CAP, rollup_cap),))
        self.retain_gpu(now, raw_hours, rollup_days, raw_cap, rollup_cap)
        self.db.execute("DELETE FROM findings WHERE ts<?", (now - 30 * 86400,))
        self.db.execute("DELETE FROM findings WHERE key IN (SELECT key FROM findings ORDER BY ts DESC LIMIT -1 OFFSET 1000)")

    def commit(self):
        self.db.commit()
