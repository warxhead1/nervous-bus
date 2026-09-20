"""Bounded historical queries and evidence-only sustained-pressure diagnostics."""
import hashlib
import json
import time

from store import METRICS, PRESSURES, pressure_value


def diagnose(store, event, expected_interval_s=60, cooldown_s=3600):
    findings = []
    for entity in event["entities"]:
        rows = store.db.execute("SELECT payload FROM samples WHERE host=? AND entity=? ORDER BY id DESC LIMIT 3",
                                (event["host"], entity["entity"])).fetchall()
        points = [json.loads(row[0]) for row in reversed(rows)]
        if len(points) != 3 or any(p["entity_data"]["state"] != "ok" for p in points):
            continue
        identities = {(p["boot_id"], p["entity_data"]["identity"]) for p in points}
        if len(identities) != 1 or not all(next(iter(identities))):
            continue
        gaps = [b["monotonic_s"] - a["monotonic_s"] for a, b in zip(points, points[1:])]
        if any(gap <= 0 or gap > expected_interval_s * 2.5 for gap in gaps):
            continue
        if points[-1]["monotonic_s"] - points[0]["monotonic_s"] < expected_interval_s * 2:
            continue
        if any(abs((b["ts"]-a["ts"]) - (b["monotonic_s"]-a["monotonic_s"])) > 5 for a,b in zip(points, points[1:])):
            continue
        for kind, threshold in (("cpu", 50), ("memory", 10), ("io", 20)):
            values = [pressure_value(p["entity_data"], kind) for p in points]
            if any(value is None or value < threshold for value in values):
                continue
            identity = [event["host"], *next(iter(identities)), entity["entity"], kind]
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
            prior = store.db.execute("SELECT ts FROM findings WHERE key=?", (key,)).fetchone()
            if prior and event["ts"] - prior[0] < cooldown_s:
                continue
            finding = {"key": key, "entity": entity["entity"], "resource": kind,
                       "threshold_pct": threshold, "observed_pct": values,
                       "from_ts": points[0]["ts"], "to_ts": event["ts"],
                       "duration_s": sum(gaps), "cooldown_s": cooldown_s,
                       "interpretation": "sustained observed pressure; cause unproven"}
            store.db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?)", (key, event["ts"], json.dumps(finding)))
            findings.append(finding)
    return findings


def _gpu_row(row):
    value = dict(row)
    if value.get("driver_version") == "":
        value["driver_version"] = None
    return value


def gpu_report(store, since, now, limit):
    """Return bounded GPU inventory evidence separately from host/cgroup rankings."""
    keys = "window_ts,host,boot_id,collector_version,collector_digest"
    status_cte = f"""
        WITH raw_status AS (
          SELECT CAST(b.ts/300 AS INTEGER)*300 AS window_ts,b.host,b.boot_id,
            b.collector_version,b.collector_digest,COUNT(*) AS status_count,
            COALESCE(SUM(b.attempted),0) AS attempt_count,
            SUM(CASE WHEN b.state='ok' THEN 1 ELSE 0 END) AS ok_count,
            SUM(CASE WHEN b.state='partial' THEN 1 ELSE 0 END) AS partial_count,
            SUM(CASE WHEN b.state='unavailable' THEN 1 ELSE 0 END) AS unavailable_count,
            SUM(CASE WHEN b.state='empty' THEN 1 ELSE 0 END) AS empty_count
          FROM gpu_batches b
          WHERE b.rolled_up=0 AND b.ts>=? AND b.ts<=?
          GROUP BY CAST(b.ts/300 AS INTEGER),b.host,b.boot_id,b.collector_version,b.collector_digest),
        raw_devices AS (
          SELECT CAST(b.ts/300 AS INTEGER)*300 AS window_ts,b.host,b.boot_id,
            b.collector_version,b.collector_digest,
            SUM(CASE WHEN g.state!='unavailable' THEN 1 ELSE 0 END) AS device_sample_count
          FROM gpu_batches b JOIN gpu_gauges g ON g.batch_id=b.batch_id
          WHERE b.rolled_up=0 AND b.ts>=? AND b.ts<=?
          GROUP BY CAST(b.ts/300 AS INTEGER),b.host,b.boot_id,b.collector_version,b.collector_digest),
        points AS (
          SELECT {keys},status_count,attempt_count,ok_count,partial_count,
            unavailable_count,empty_count,device_sample_count
          FROM gpu_status_rollups WHERE window_ts+300>? AND window_ts<=?
          UNION ALL
          SELECT s.window_ts,s.host,s.boot_id,s.collector_version,s.collector_digest,
            s.status_count,s.attempt_count,s.ok_count,s.partial_count,s.unavailable_count,
            s.empty_count,COALESCE(d.device_sample_count,0)
          FROM raw_status s LEFT JOIN raw_devices d USING ({keys})),
        combined AS (
          SELECT {keys},SUM(status_count) AS status_count,SUM(attempt_count) AS attempt_count,
            SUM(ok_count) AS ok_count,SUM(partial_count) AS partial_count,
            SUM(unavailable_count) AS unavailable_count,SUM(empty_count) AS empty_count,
            SUM(device_sample_count) AS device_sample_count
          FROM points GROUP BY {keys})
        """
    status_rows = store.db.execute(status_cte + "SELECT * FROM combined ORDER BY window_ts DESC,host LIMIT ?",
                                   (since, now, since, now, since, now, limit + 1)).fetchall()
    coverage = store.db.execute(status_cte + """
        SELECT COALESCE(SUM(status_count),0) AS status_samples,
          COALESCE(SUM(attempt_count),0) AS query_attempts,
          COALESCE(SUM(ok_count),0) AS ok_queries,
          COALESCE(SUM(partial_count),0) AS partial_queries,
          COALESCE(SUM(unavailable_count),0) AS unavailable_queries,
          COALESCE(SUM(empty_count),0) AS empty_queries,
          COALESCE(SUM(device_sample_count),0) AS observed_device_samples
        FROM combined""", (since, now, since, now, since, now)).fetchone()
    sampled = store.db.execute("""
        SELECT * FROM gpu_rollups WHERE window_ts+300>? AND window_ts<=?
        ORDER BY window_ts DESC,uuid LIMIT ?""", (since, now, limit + 1)).fetchall()
    raw = store.db.execute("""
        SELECT g.ts,g.host,g.boot_id,g.collector_version,g.collector_digest,g.uuid,g.driver_version,
          g.state,g.reason,g.memory_total_bytes,g.memory_used_bytes,g.memory_free_bytes,g.utilization_pct,
          b.state AS query_state,b.reason AS query_reason,b.attempted,b.query_ms
        FROM gpu_gauges g JOIN gpu_batches b ON b.batch_id=g.batch_id
        WHERE b.rolled_up=0 AND g.ts>=? AND g.ts<=?
        ORDER BY g.ts DESC,g.uuid LIMIT ?""", (since, now, limit + 1)).fetchall()
    latest_status = store.db.execute("""
        SELECT batch_id,ts,monotonic_s,host,boot_id,collector_version,collector_digest,state,reason,
          attempted,query_ms,delivery FROM gpu_batches ORDER BY ts DESC,id DESC LIMIT 1""").fetchone()
    latest_devices = []
    if latest_status:
        latest_devices = [_gpu_row(row) for row in store.db.execute("""
            SELECT uuid,driver_version,state,reason,memory_total_bytes,memory_used_bytes,
              memory_free_bytes,utilization_pct FROM gpu_gauges WHERE batch_id=? ORDER BY uuid""",
            (latest_status["batch_id"],)).fetchall()]
    return {
        "rollup_resolution_s": 300,
        "sample_semantics": "sampled min/max/sum/count; sampled maxima are not instantaneous peaks and counts are not durations",
        "coverage": dict(coverage),
        "status_history": [dict(row) for row in status_rows[:limit]],
        "status_history_truncated": len(status_rows) > limit,
        "sampled_history": [_gpu_row(row) for row in sampled[:limit]],
        "sampled_history_truncated": len(sampled) > limit,
        "raw_samples": [_gpu_row(row) for row in raw[:limit]],
        "raw_samples_truncated": len(raw) > limit,
        "latest_status": dict(latest_status) if latest_status else None,
        "latest_devices": latest_devices,
    }


def report(store, since, limit=120, now=None):
    now = time.time() if now is None else now
    since = max(float(since), now - 30 * 86400)
    limit = max(1, min(int(limit), 500))
    sums = ["elapsed_s", *(column for name in METRICS for column in (name, name + "_coverage_s"))]
    columns = ["entity", "sample_count", "valid_samples", "invalid_samples", *sums, "memory_bytes", *PRESSURES]
    raw_columns = ["entity", "1", "interval_state='ok'", "interval_state!='ok'", *sums, "memory_bytes", *PRESSURES]
    source = f"""WITH points AS (
        SELECT {','.join(columns)} FROM rollups WHERE window_ts+300>? AND window_ts<=?
        UNION ALL SELECT {','.join(raw_columns)} FROM samples WHERE rolled_up=0 AND ts>=? AND ts<=?)
        SELECT entity,SUM(sample_count) AS sample_count,SUM(valid_samples) AS valid_samples,
        SUM(invalid_samples) AS invalid_samples,{','.join(f'SUM({c}) AS {c}' for c in sums)},
        MAX(memory_bytes) AS memory_peak_bytes,{','.join(f'MAX({c}) AS {c}' for c in PRESSURES)}
        FROM points GROUP BY entity"""
    args = (since, now, since, now)
    top = {}
    for name, ordering in (("cpu", "cpu_usec/NULLIF(cpu_usec_coverage_s,0)"),
                           ("io", "(COALESCE(read_bytes,0)+COALESCE(write_bytes,0))"),
                           ("memory", "memory_peak_bytes")):
        rows = store.db.execute(f"SELECT * FROM ({source}) WHERE entity!='@host' ORDER BY {ordering} DESC LIMIT 20", args).fetchall()
        top[name] = [dict(row) for row in rows]
        for item in top[name]:
            for metric, label, scale in (("cpu_usec", "cpu_cores", 1e6),
                                         ("read_bytes", "read_bytes_per_s", 1),
                                         ("write_bytes", "write_bytes_per_s", 1)):
                coverage = item[metric + "_coverage_s"]
                item[label] = item[metric] / coverage / scale if coverage else None
    recent = store.db.execute("SELECT * FROM rollups WHERE window_ts+300>? AND window_ts<=? ORDER BY window_ts DESC,entity LIMIT ?",
                              (since, now, limit + 1)).fetchall()
    latest_ts = store.db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    latest = store.db.execute("SELECT entity,state,interval_state,delivery,payload FROM samples WHERE ts=? LIMIT 9", (latest_ts,)).fetchall()
    delivery = store.db.execute("SELECT delivery,COUNT(*) AS count FROM samples WHERE ts>=? AND ts<=? GROUP BY delivery", (since, now)).fetchall()
    metadata = dict(store.db.execute("SELECT key,value FROM metadata"))
    return {"requested_since": since, "as_of": now, "rollup_resolution_s": 300,
            "rollup_assignment": "interval end; first intersecting bucket may precede requested_since",
            "query_limit": limit, "top_consumers": top,
            "pressure_over_time": [dict(row) for row in recent[:limit]],
            "pressure_series_truncated": len(recent) > limit,
            "latest": [{**dict(row), "payload": json.loads(row["payload"])} for row in latest],
            "gpu": gpu_report(store, since, now, limit),
            "health": {"latest_ts": latest_ts, "freshness_s": None if latest_ts is None else now-latest_ts,
                       "stale": latest_ts is None or now-latest_ts > 180 or latest_ts > now+5,
                       "unavailable_entities": sum(row["state"] != "ok" for row in latest),
                       "delivery": [dict(row) for row in delivery], **metadata}}
