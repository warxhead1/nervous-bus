#!/usr/bin/env python3
"""Collect bounded cgroup-v2/PSI measurements and retain local history."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from collector import collect, intervalize
from store import Store

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(os.environ.get("NERVOUS_SYSTEM_RESOURCE_CACHE", str(Path.home() / ".cache/nervous-bus/system-resource")))
VERSION = "1.0.0"


def iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def publish(channel, payload, dry_run):
    if dry_run:
        return "dry_run"
    command = [os.environ.get("NERVOUS_BIN", str(ROOT / "sdk/shell/nervous")), "publish", channel, json.dumps(payload, separators=(",", ":"))]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, cwd=ROOT)
        if result.returncode != 0:
            return "failed"
        if "Redis delivery failed" in result.stderr:
            return "bus_unavailable_file_retained"
        return "bus_delivered"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def pressure_level(pressure):
    value = pressure.get("memory", {}).get("some", {}).get("avg10")
    return value is not None and value >= 10.0


def sample(args, now=None, measure=collect):
    now = time.time() if now is None else now; started = time.monotonic()
    host, cglist = socket.gethostname(), tuple(x for x in args.cgroups.split(",") if x)[:args.max_cgroups]
    data = measure(cgroups=cglist); db = Store(args.db)
    events = []
    for current in data["cgroups"]:
        previous = db.previous(host, current["cgroup"])
        previous = previous or {}; previous["memory_events"] = json.loads(previous.get("events_json", "{}"))
        previous["boot_id"] = previous.get("boot_id")
        interval = intervalize({**current, "boot_id": data["boot_id"]}, previous, now - previous.get("ts", now))
        row = {**current, **data["host"], **interval, "ts": now, "host": host, "boot_id": data["boot_id"],
               "pressure_json": json.dumps(data["pressure"], sort_keys=True), "cgroup_pressure_json": json.dumps(current["pressure"], sort_keys=True), "events_json": json.dumps(current.get("memory_events", {}), sort_keys=True),
               "collector_version": VERSION, "collector_digest": digest(), "duration_ms": (time.monotonic() - started) * 1000, "delivery": "pending"}
        db.insert(row)
        event = {"host": host, "boot_id": data["boot_id"], "sampled_at": iso(now), "measurement_window_s": interval.get("elapsed_s"),
                 "collector_version": VERSION, "collector_digest": digest(), "cgroup": current, "interval": interval,
                 "host_resources": data["host"],
                 "pressure": {"host": data["pressure"], "cgroup": current["pressure"]}, "health": {"duration_ms": row["duration_ms"], "state": current["state"],
                 "configured_cgroups": len(cglist), "observed_cgroups": len(data["cgroups"]),
                 "unavailable_cgroups": sum(item["state"] != "ok" for item in data["cgroups"])}}
        events.append(event)
    db.rollup(now); db.retain(now, args.raw_hours, args.rollup_days)
    for current in data["cgroups"]:
        finding_key = "memory-psi-sustained:%s:%s" % (host, current["cgroup"])
        sustained = current["state"] == "ok" and pressure_level(data["pressure"]) and db.sustained_pressure(host, current["cgroup"])
        if sustained and db.finding(finding_key, now, {"pressure": data["pressure"], "required_consecutive": 3, "cgroup": current["cgroup"]}, args.cooldown_s):
            events.append({"finding": finding_key, "sampled_at": iso(now), "pressure": {"host": data["pressure"], "cgroup": current["pressure"]}, "cooldown_s": args.cooldown_s})
    db.commit()
    for event in events:
        delivery = publish("bus.system.resource.sample.v1", {"kind": "finding" if "finding" in event else "sample", **event}, args.dry_run)
        if "finding" in event: db.finding_delivery(event["finding"], delivery)
        else: db.mark_delivery(now, host, event["cgroup"]["cgroup"], delivery)
    db.commit()
    return {"events": events, "delivery": ["dry_run" if args.dry_run else "attempted"], "report": db.report(now - 3600, now=now)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(CACHE / "history.sqlite3")); parser.add_argument("--cgroups", default=os.environ.get("NERVOUS_SYSTEM_RESOURCE_CGROUPS", "user.slice"))
    parser.add_argument("--max-cgroups", type=int, default=8); parser.add_argument("--raw-hours", type=int, default=24); parser.add_argument("--rollup-days", type=int, default=30)
    parser.add_argument("--cooldown-s", type=int, default=3600); parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--report", action="store_true")
    parser.add_argument("--since", type=float); parser.add_argument("--limit", type=int, default=120); args = parser.parse_args()
    if args.since is not None or args.report:
        print(json.dumps(Store(args.db).report(args.since or time.time() - 3600, args.limit), sort_keys=True)); return 0
    print(json.dumps(sample(args), sort_keys=True)); return 0


if __name__ == "__main__":
    sys.exit(main())
