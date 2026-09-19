#!/usr/bin/env python3
"""Low-frequency, bounded host history; publishing never determines local retention."""
import argparse
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from collector import collect
from history import diagnose, report
from intervals import intervalize
from store import Store

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CACHE = Path(os.environ.get("NERVOUS_SYSTEM_RESOURCE_CACHE", str(Path.home() / ".cache/nervous-bus/system-resource")))
CHANNEL = "bus.system.resource.sample.v2"
VERSION = "2.0.0"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    temporary.replace(path)


def configuration(path):
    value = json.loads(Path(path).read_text())
    selectors = value.get("selectors")
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= 8:
        raise ValueError("configure one to eight cgroup selectors")
    if any(not isinstance(s, str) or not s or len(s) > 512 for s in selectors):
        raise ValueError("invalid cgroup selector")
    for key, low, high in (("raw_hours", 1, 24), ("rollup_days", 1, 30),
                           ("cooldown_s", 60, 86400), ("expected_interval_s", 30, 300)):
        if type(value.get(key)) is not int or not low <= value[key] <= high:
            raise ValueError("invalid bounded configuration: " + key)
    return value


def identity(config):
    digest = hashlib.sha256()
    sources = [path for path in sorted(HERE.glob("*.py")) if not path.name.startswith("test_")]
    sources += [ROOT / "schemas" / (CHANNEL + ".json")]
    for path in sources:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return {"version": VERSION, "digest": digest.hexdigest(),
            "config_digest": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()}


def publish(event, dry_run=False):
    if dry_run:
        return "dry_run"
    command = [os.environ.get("NERVOUS_BIN", str(ROOT / "sdk/shell/nervous")),
               "publish", CHANNEL, json.dumps(event, separators=(",", ":"), allow_nan=False)]
    try:
        with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, start_new_session=True) as process:
            try:
                _, stderr = process.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return "timed_out"
            if process.returncode:
                return "failed"
            if b"Redis delivery failed" in stderr:
                return "receipt_only"
            return "accepted_by_cli"  # acknowledgement, not independent consumer proof
    except OSError:
        return "unavailable"


def sample(db_path, config, dry_run=False, measure=collect, publisher=publish, now=None, mono=None):
    started, cpu_started = time.monotonic(), time.process_time()
    now = time.time() if now is None else now
    mono = started if mono is None else mono
    data = measure(selectors=config["selectors"])
    event = {"id": str(uuid.uuid4()), "host": socket.gethostname(), "ts": now,
             "monotonic_s": mono, "boot_id": data["boot_id"], "collector": identity(config),
             "entities": data["entities"], "findings": [], "health": {}}
    store = Store(db_path)
    try:
        for entity in event["entities"]:
            entity["interval"] = intervalize(entity, store.previous(event["host"], entity["entity"]),
                                              event["boot_id"], mono, now, config["expected_interval_s"] * 2.5)
        store.insert_batch(event)
        event["findings"] = diagnose(store, event, config["expected_interval_s"], config["cooldown_s"])
        store.rollup(now)
        store.retain(now, config["raw_hours"], config["rollup_days"])
        store.commit()
        event["health"] = {"collection_ms": (time.monotonic() - started) * 1000,
                            "collector_cpu_ms": (time.process_time() - cpu_started) * 1000,
                            "entities": len(event["entities"]),
                            "unavailable_entities": sum(e["state"] != "ok" for e in event["entities"]),
                            "warnings": data["warnings"]}
        delivery = publisher(event, dry_run)
        store.mark_delivery(event["id"], delivery)
        receipt = {"state": "ok" if not event["health"]["unavailable_entities"] else "partial",
                   "id": event["id"], "ts": now, "collector": event["collector"],
                   "delivery": delivery, "findings": len(event["findings"]), **event["health"],
                   "total_ms": (time.monotonic() - started) * 1000}
        atomic_json(Path(db_path).parent / "health.json", receipt)
        if event["findings"]:
            atomic_json(Path(db_path).parent / "latest-diagnostic.json", {
                "sample_id": event["id"], "findings": event["findings"],
                "report": report(store, now - 3600, now=now)})
        return event, receipt
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=os.environ.get("NERVOUS_SYSTEM_RESOURCE_CONFIG", str(HERE / "default-config.json")))
    parser.add_argument("--db", default=str(CACHE / "history-v2.sqlite3"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--since", type=float)
    parser.add_argument("--limit", type=int, default=120)
    args = parser.parse_args()
    if args.report or args.since is not None:
        store = Store(args.db)
        try:
            result = report(store, args.since if args.since is not None else time.time() - 3600, args.limit)
            try:
                result["collector_health"] = json.loads((Path(args.db).parent / "health.json").read_text())
            except (OSError, ValueError):
                result["collector_health"] = {"state": "unavailable"}
            print(json.dumps(result, allow_nan=False))
        finally:
            store.close()
        return 0
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with open(args.db + ".lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"state": "skipped", "reason": "collector_already_running"}))
            return 0
        try:
            _, receipt = sample(args.db, configuration(args.config), args.dry_run)
            print(json.dumps(receipt, allow_nan=False))
            return 0
        except Exception as error:
            receipt = {"state": "failed", "ts": time.time(), "error_class": type(error).__name__}
            atomic_json(Path(args.db).parent / "health.json", receipt)
            print(json.dumps(receipt), file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
