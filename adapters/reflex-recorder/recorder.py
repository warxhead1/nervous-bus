#!/usr/bin/env python3
"""reflex-recorder — Reflexarc FLYWHEEL: segment agent activity into runs.

Consumes nbus:all via XREADGROUP (at-least-once), filters to
bus.agent.activity.v1, segments into runs using the hardened composite-key
model, persists to SQLite, and emits bus.agent.run.closed.v1 via
`nervous publish` on each run close.

Usage:
    python recorder.py                  # run continuously
    python recorder.py --once           # drain current stream and exit
    python recorder.py --config foo.toml
    python recorder.py --replay /path/to/debug.jsonl   # offline fixture mode

See reflex-recorder.toml for configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import redis

# ── Repo root / sibling paths ─────────────────────────────────────────────────
_ADAPTER_DIR = Path(__file__).parent
_NBUS_ROOT = _ADAPTER_DIR.parent.parent
_NERVOUS_BIN = _NBUS_ROOT / "sdk" / "shell" / "nervous"

sys.path.insert(0, str(_ADAPTER_DIR))
from segment import Segmenter
from store import SQLiteStore, DEFAULT_DB_PATH

# ── Constants ─────────────────────────────────────────────────────────────────
CONSUMER_GROUP = "reflex-recorder"
CONSUMER_NAME = f"reflex-recorder-{os.getpid()}"
STREAM_NAME = "nbus:all"
ACTIVITY_TYPE = "bus.agent.activity.v1"
PUBLISH_CHANNEL = "bus.agent.run.closed"

DEFAULT_IDLE_TIMEOUT_S = 900.0   # 15 min
DEFAULT_METRICS_INTERVAL_S = 60.0
DEFAULT_TICK_INTERVAL_S = 30.0


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = _ADAPTER_DIR / "reflex-recorder.toml"


def _load_config(path: Path) -> dict:
    cfg = {
        "redis_url": "redis://localhost:6379",
        "redis_db": 0,
        "connect_timeout_s": 5.0,
        "idle_timeout_s": DEFAULT_IDLE_TIMEOUT_S,
        "metrics_interval_s": DEFAULT_METRICS_INTERVAL_S,
        "tick_interval_s": DEFAULT_TICK_INTERVAL_S,
        "db_path": None,  # None → use DEFAULT_DB_PATH
        "stream_read_count": 200,
        "stream_block_ms": 2000,
    }
    if not path.exists():
        return cfg
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] config parse error {path}: {e}\n")
        return cfg

    redis_cfg = raw.get("redis", {})
    if "url" in redis_cfg:
        cfg["redis_url"] = redis_cfg["url"]
    if "db" in redis_cfg:
        cfg["redis_db"] = int(redis_cfg["db"])
    if "connect_timeout_s" in redis_cfg:
        cfg["connect_timeout_s"] = float(redis_cfg["connect_timeout_s"])

    rec_cfg = raw.get("recorder", {})
    for key in ("idle_timeout_s", "metrics_interval_s", "tick_interval_s"):
        if key in rec_cfg:
            cfg[key] = float(rec_cfg[key])
    if "stream_read_count" in rec_cfg:
        cfg["stream_read_count"] = int(rec_cfg["stream_read_count"])
    if "stream_block_ms" in rec_cfg:
        cfg["stream_block_ms"] = int(rec_cfg["stream_block_ms"])

    store_cfg = raw.get("store", {})
    if "db_path" in store_cfg:
        cfg["db_path"] = Path(store_cfg["db_path"]).expanduser()

    return cfg


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _ensure_consumer_group(r: redis.Redis) -> None:
    """Create the consumer group if it doesn't exist.

    Start from '$' (new events only — backfill is a separate follow-up task).
    The MKSTREAM flag ensures the stream is created if it doesn't exist yet.
    """
    try:
        r.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="$", mkstream=True)
        sys.stderr.write(f"[reflex-recorder] created consumer group '{CONSUMER_GROUP}' on {STREAM_NAME}\n")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            # Group already exists — normal on restart
            sys.stderr.write(f"[reflex-recorder] consumer group '{CONSUMER_GROUP}' already exists\n")
        else:
            raise
    sys.stderr.flush()


# ── Publish via shell SDK ─────────────────────────────────────────────────────

def _publish_run(payload: dict) -> bool:
    """Emit a bus.agent.run.closed.v1 via `nervous publish` (shell SDK).

    Returns True on success, False on failure (non-fatal — run is already
    persisted to SQLite, publish failure just means no live bus delivery).
    """
    try:
        result = subprocess.run(
            [str(_NERVOUS_BIN), "publish", PUBLISH_CHANNEL, json.dumps(payload)],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:300]
            sys.stderr.write(f"[reflex-recorder] publish failed (rc={result.returncode}): {stderr}\n")
            sys.stderr.flush()
            return False
        return True
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] publish exception: {e}\n")
        sys.stderr.flush()
        return False


# ── Recorder state ────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        db_path = cfg.get("db_path") or DEFAULT_DB_PATH
        self.store = SQLiteStore(db_path)
        self._runs_closed = 0
        self._runs_published = 0
        self._events_ingested = 0
        self._events_skipped = 0
        self._started_at = time.time()

        # Map run_key → pending events buffer (for run_events table)
        # We buffer events until the run closes, then persist them.
        self._pending_events: dict[str, list[tuple[str, str, str]]] = {}

        def on_run_closed(payload: dict) -> None:
            self._on_run_closed(payload)

        self.segmenter = Segmenter(
            idle_timeout_s=cfg["idle_timeout_s"],
            on_run_closed=on_run_closed,
        )

    def _on_run_closed(self, payload: dict) -> None:
        """Called by the segmenter when a run is closed."""
        run_id = payload["run_id"]
        run_key = payload["run_key"]

        # 1. Persist run
        self.store.save_run(payload)

        # 2. Flush buffered events to run_events
        events = self._pending_events.pop(run_key, [])
        for (event_ts, event_type, raw_json) in events:
            self.store.append_event(run_id, event_ts, event_type, raw_json)

        # 3. Emit via nervous publish
        ok = _publish_run(payload)
        self._runs_closed += 1
        if ok:
            self._runs_published += 1

        sys.stderr.write(
            f"[reflex-recorder] closed run {run_id} "
            f"key={run_key!r} reason={payload.get('close_reason')} "
            f"events={payload['event_count']} "
            f"tools={list(payload['tool_histogram'].keys())[:5]} "
            f"published={'ok' if ok else 'FAILED'}\n"
        )
        sys.stderr.flush()

    def _ingest_activity(self, raw_json: str, stream_id: str) -> None:
        """Parse and ingest one raw CloudEvents envelope from the stream."""
        try:
            envelope = json.loads(raw_json)
        except Exception:
            return

        event_type = envelope.get("type", "")
        if event_type != ACTIVITY_TYPE:
            self._events_skipped += 1
            return

        data = envelope.get("data") or {}
        if not isinstance(data, dict):
            return

        now = time.time()
        run_key_tuple = self._get_run_key(data)
        if run_key_tuple:
            rk = run_key_tuple[0]
            ts = data.get("ts") or data.get("time") or _now_utc()
            # Buffer event for run_events persistence
            if rk not in self._pending_events:
                self._pending_events[rk] = []
            self._pending_events[rk].append((ts, event_type, raw_json))

        self.segmenter.ingest(data, now=now)
        self._events_ingested += 1

    def _get_run_key(self, activity: dict) -> Optional[tuple]:
        """Compute run key for buffering without duplicating logic."""
        from segment import compute_run_key
        try:
            return compute_run_key(activity)
        except Exception:
            return None

    def log_metrics(self) -> None:
        elapsed = time.time() - self._started_at
        rate = self._events_ingested / max(1, elapsed)
        sys.stderr.write(
            f"[{_now_utc()}] reflex-recorder metrics: "
            f"ingested={self._events_ingested} skipped={self._events_skipped} "
            f"closed={self._runs_closed} published={self._runs_published} "
            f"open_runs={self.segmenter.open_run_count} "
            f"rate={rate:.2f}/s\n"
        )
        sys.stderr.flush()

    def shutdown(self) -> None:
        self.segmenter.shutdown()
        self.store.close()


# ── Main loop ─────────────────────────────────────────────────────────────────

def _run_xreadgroup(r: redis.Redis, recorder: Recorder, cfg: dict, once: bool = False) -> None:
    last_metrics = time.time()
    last_tick = time.time()
    read_count = cfg["stream_read_count"]
    block_ms = cfg["stream_block_ms"]
    metrics_interval = cfg["metrics_interval_s"]
    tick_interval = cfg["tick_interval_s"]

    while True:
        try:
            results = r.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=read_count,
                block=block_ms if not once else 0,
            )

            if results:
                for _stream, entries in results:
                    for stream_id, fields in entries:
                        raw = fields.get("_raw", "{}")
                        recorder._ingest_activity(raw, stream_id)
                        # XACK only AFTER ingestion (at-least-once guarantee)
                        r.xack(STREAM_NAME, CONSUMER_GROUP, stream_id)

            now = time.time()

            # Idle-timeout check
            if now - last_tick >= tick_interval:
                recorder.segmenter.tick(now=now)
                last_tick = now

            # Metrics log
            if now - last_metrics >= metrics_interval:
                recorder.log_metrics()
                last_metrics = now

            if once and not results:
                break

        except KeyboardInterrupt:
            break
        except redis.ConnectionError as e:
            sys.stderr.write(f"[reflex-recorder] redis connection lost: {e}; retrying in 5s\n")
            sys.stderr.flush()
            time.sleep(5)
        except Exception as e:
            sys.stderr.write(f"[reflex-recorder] error: {e}\n")
            sys.stderr.flush()
            time.sleep(1)

        if once:
            break


def _run_replay(recorder: Recorder, replay_path: Path) -> None:
    """Offline mode: read bus.agent.activity.v1 lines from debug.jsonl fixture."""
    sys.stderr.write(f"[reflex-recorder] replay mode: {replay_path}\n")
    count = 0
    skipped = 0
    try:
        with open(replay_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except Exception:
                    continue
                if envelope.get("type") != ACTIVITY_TYPE:
                    skipped += 1
                    continue
                recorder._ingest_activity(line, "replay")
                count += 1
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] replay error: {e}\n")
        sys.stderr.flush()

    # After replay, close all open runs
    sys.stderr.write(f"[reflex-recorder] replay done: {count} activity events, {skipped} skipped; closing open runs\n")
    sys.stderr.flush()
    recorder.segmenter.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="reflex-recorder — Reflexarc run capture")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"Path to config TOML (default: {DEFAULT_CONFIG})")
    parser.add_argument("--once", action="store_true",
                        help="Drain current PEL+stream entries and exit")
    parser.add_argument("--replay", type=Path, default=None,
                        help="Offline replay mode: read activity events from debug.jsonl file")
    parser.add_argument("--db-path", type=Path, default=None,
                        help="Override SQLite DB path")
    args = parser.parse_args()

    cfg = _load_config(args.config)
    if args.db_path:
        cfg["db_path"] = args.db_path

    recorder = Recorder(cfg)

    sys.stderr.write(
        f"[reflex-recorder] starting: db={recorder.store.db_path} "
        f"idle_timeout={cfg['idle_timeout_s']}s\n"
    )
    sys.stderr.flush()

    if args.replay:
        _run_replay(recorder, args.replay)
        recorder.log_metrics()
        # Print a brief store dump
        recent = recorder.store.recent_runs(10)
        sys.stderr.write(f"[reflex-recorder] store dump ({len(recent)} runs):\n")
        for r in recent:
            sys.stderr.write(
                f"  run_id={r['run_id'][:12]}.. key_kind={r['run_key_kind']} "
                f"project={r['project']} events={r['event_count']} "
                f"close={r['close_reason']} wt={r['worktree_slug']}\n"
            )
        sys.stderr.flush()
        recorder.store.close()
        return 0

    # Live mode: XREADGROUP
    try:
        r = redis.Redis.from_url(
            cfg["redis_url"],
            db=cfg["redis_db"],
            socket_timeout=cfg["connect_timeout_s"],
            socket_connect_timeout=cfg["connect_timeout_s"],
            decode_responses=True,
        )
        r.ping()
    except Exception as e:
        sys.stderr.write(f"[reflex-recorder] redis connect failed: {e}\n")
        return 1

    _ensure_consumer_group(r)

    try:
        _run_xreadgroup(r, recorder, cfg, once=args.once)
    finally:
        recorder.segmenter.shutdown()
        recorder.log_metrics()
        recorder.store.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
