#!/usr/bin/env python3
"""redis-mirror — opt-in Redis Streams transport for selected bus channels.

Subscribes to nervous-bus events via JSONL-tail (same pattern as cc-bus-dashboard).
For each event whose 'type' is in the configured channel prefixes, XADD to stream
'nbus:<type>' with MAXLEN ~N (configurable, default 10000).

Behavior:
- Subscribes to nervous-bus events via JSONL-tail with inode-rotation awareness
- For each event whose 'type' matches configured prefixes, XADD to 'nbus:<type>'
- Optional MINID-based time-window trim instead of MAXLEN
- Logs metrics (events/sec mirrored, dropped, redis errors) to stderr
- Survives redis restarts via reconnect; offset file protects against log rotation
- No-op when redis not reachable or config empty — fail-soft

Usage:
    python mirror.py                          # uses config.toml in same dir
    python mirror.py --config /path/to.toml   # explicit config
    python mirror.py --once                   # one-shot tail and exit (test mode)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import jsonschema
import redis

# ── Schema registry ──────────────────────────────────────────────────────────
# Loaded once at startup; reloaded every SCHEMA_RELOAD_INTERVAL_S seconds so
# new schemas added to disk don't require a process restart.

SCHEMA_RELOAD_INTERVAL_S = 300
_NBUS_ROOT = Path(__file__).parent.parent.parent  # adapters/redis-mirror/../../ == nervous-bus root
# User-home dir for private/custom schemas and config. Overridable via NERVOUS_HOME env var.
# Schemas in NERVOUS_HOME/schemas/ are loaded in addition to the repo schemas directory and
# take precedence over repo schemas for the same channel name (user layer wins on conflict).
NERVOUS_HOME = Path(os.environ.get("NERVOUS_HOME", Path.home() / ".config" / "nervous-bus"))
SCHEMA_DIRS: List[Path] = [
    _NBUS_ROOT / "schemas",   # repo schemas (public, versioned)
    NERVOUS_HOME / "schemas", # user-home schemas (private/custom, override repo)
]


class SchemaRegistry:
    """Caches JSON schemas keyed by channel type string (e.g. 'bus.notify.v1').

    Loads from multiple directories in order; later directories override earlier ones
    for the same channel name. This allows a user-home overlay (NERVOUS_HOME/schemas/)
    to extend or override the repo schemas without modifying the repo.

    Envelope-style schemas (those with 'specversion' in their required[] array)
    are validated against the full CloudEvents envelope. All other schemas are
    treated as data-payload schemas and validated against event['data'].
    """

    def __init__(self, schema_dirs: List[Path]) -> None:
        self.schema_dirs = schema_dirs
        self._registry: Dict[str, dict] = {}
        self._envelope_types: set = set()
        self._last_load: float = 0.0
        self._load()

    def _load(self) -> None:
        registry: Dict[str, dict] = {}
        envelope_types: set = set()
        total = 0
        for schema_dir in self.schema_dirs:
            if not schema_dir.exists():
                continue
            count = 0
            for path in schema_dir.glob("*.json"):
                channel = path.stem  # filename without .json == channel name
                try:
                    with path.open() as f:
                        schema = json.load(f)
                    registry[channel] = schema
                    if "specversion" in schema.get("required", []):
                        envelope_types.add(channel)
                    count += 1
                except Exception as e:
                    sys.stderr.write(f"[nbus schema] failed to load {path.name}: {e}\n")
                    sys.stderr.flush()
            if count:
                sys.stderr.write(f"[nbus schema] loaded {count} schemas from {schema_dir}\n")
                sys.stderr.flush()
            total += count

        self._registry = registry
        self._envelope_types = envelope_types
        self._last_load = time.time()

    def maybe_reload(self) -> None:
        if time.time() - self._last_load >= SCHEMA_RELOAD_INTERVAL_S:
            self._load()

    def validate(self, event_type: str, envelope: dict) -> Optional[str]:
        """Validate envelope against the registered schema for event_type.

        Returns None on success, or a short error message string on failure.
        If no schema is registered for event_type, logs a warning and returns None
        (unknown channels are allowed through).
        """
        schema = self._registry.get(event_type)
        if schema is None:
            sys.stderr.write(f"[nbus warn] no schema for type: {event_type}\n")
            sys.stderr.flush()
            return None

        # Decide what to validate: full envelope or just the data payload.
        subject = envelope if event_type in self._envelope_types else envelope.get("data", {})
        try:
            jsonschema.validate(subject, schema)
            return None
        except jsonschema.ValidationError as e:
            return str(e.message)[:500]
        except Exception as e:
            return f"validator error: {e}"


# Module-level singleton — initialised lazily on first mirror_event call.
_schema_registry: Optional[SchemaRegistry] = None


def get_schema_registry() -> SchemaRegistry:
    global _schema_registry
    if _schema_registry is None:
        _schema_registry = SchemaRegistry(SCHEMA_DIRS)
    return _schema_registry

DEFAULT_CONFIG = Path(__file__).parent / "config.toml"
DEFAULT_LOG = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"
DEFAULT_OFFSET_FILE = Path("~/.cache/nervous-bus/redis-mirror-offset.json").expanduser()


def load_config(config_path: Path) -> dict:
    """Load and parse TOML config file."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    cfg = {
        "channels": [],
        "redis_url": "redis://localhost:6379",
        "redis_password": None,
        "redis_db": 0,
        "connect_timeout_s": 5.0,
        "maxlen": 10000,
        "trim_strategy": "MAXLEN",
        "min_idle_ms": 0,
        "offset_file": DEFAULT_OFFSET_FILE,
        "metrics_interval_s": 60.0,
    }

    if "channels" in raw:
        channel_cfg = raw["channels"]
        if isinstance(channel_cfg, dict) and "types" in channel_cfg:
            cfg["channels"] = channel_cfg["types"]
        elif isinstance(channel_cfg, list):
            cfg["channels"] = channel_cfg

    redis_cfg = raw.get("redis", {})
    if "url" in redis_cfg:
        cfg["redis_url"] = redis_cfg["url"]
    if "password" in redis_cfg:
        cfg["redis_password"] = redis_cfg["password"]
    if "db" in redis_cfg:
        cfg["redis_db"] = redis_cfg["db"]
    if "connect_timeout_s" in redis_cfg:
        cfg["connect_timeout_s"] = redis_cfg["connect_timeout_s"]

    streams_cfg = raw.get("streams", {})
    if "maxlen" in streams_cfg:
        cfg["maxlen"] = streams_cfg["maxlen"]
    if "trim_strategy" in streams_cfg:
        cfg["trim_strategy"] = streams_cfg["trim_strategy"]
    if "min_idle_ms" in streams_cfg:
        cfg["min_idle_ms"] = streams_cfg["min_idle_ms"]

    channel_cfg_raw = raw.get("channels", {})
    if isinstance(channel_cfg_raw, dict):
        cfg["mirror_all"] = bool(channel_cfg_raw.get("mirror_all", False))
    else:
        cfg["mirror_all"] = False

    if "universal_stream" in streams_cfg:
        cfg["universal_stream"] = streams_cfg["universal_stream"]
        cfg["universal_stream_maxlen"] = int(streams_cfg.get("universal_stream_maxlen", 50000))
    else:
        cfg["universal_stream"] = ""
        cfg["universal_stream_maxlen"] = 50000

    offset_cfg = raw.get("offset_file", {})
    if isinstance(offset_cfg, dict) and "path" in offset_cfg:
        cfg["offset_file"] = Path(offset_cfg["path"]).expanduser()

    metrics_cfg = raw.get("metrics", {})
    if "interval_s" in metrics_cfg:
        cfg["metrics_interval_s"] = metrics_cfg["interval_s"]

    return cfg


def load_offset(offset_file: Path) -> dict:
    """Load persisted tail position from offset file."""
    if not offset_file.exists():
        return {"inode": None, "offset": 0}
    try:
        with open(offset_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"inode": None, "offset": 0}


def save_offset(offset_file: Path, offset_data: dict) -> None:
    """Persist tail position to offset file."""
    try:
        offset_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = offset_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(offset_data, f)
        tmp.rename(offset_file)
    except Exception:
        pass


class State:
    def __init__(
        self,
        log_path: Path,
        channel_prefixes: List[str],
        redis_url: str,
        redis_password: Optional[str],
        redis_db: int,
        connect_timeout_s: float,
        maxlen: int,
        trim_strategy: str,
        min_idle_ms: int,
        offset_file: Path,
        metrics_interval_s: float,
        mirror_all: bool,
        universal_stream: str,
        universal_stream_maxlen: int,
    ):
        self.log_path = log_path
        self.channel_prefixes = channel_prefixes
        self.redis_url = redis_url
        self.redis_password = redis_password
        self.redis_db = redis_db
        self.connect_timeout_s = connect_timeout_s
        self.maxlen = maxlen
        self.trim_strategy = trim_strategy
        self.min_idle_ms = min_idle_ms
        self.offset_file = offset_file
        self.metrics_interval_s = metrics_interval_s
        self.mirror_all: bool = mirror_all
        self.universal_stream: str = universal_stream
        self.universal_stream_maxlen: int = universal_stream_maxlen

        self.fp: Optional[object] = None
        self.inode: Optional[int] = None

        self.redis_client: Optional[redis.Redis] = None
        self.redis_connected = False
        self.redis_last_connect_attempt = 0.0
        self.redis_reconnect_interval_s = 5.0

        self.offset_data = load_offset(offset_file)
        self.inode = self.offset_data.get("inode")
        self.offset = self.offset_data.get("offset", 0)

        self.events_mirrored = 0
        self.events_dropped = 0
        self.redis_errors = 0
        self.last_metrics_log = time.time()
        self.started_at = time.time()

    def connect_redis(self) -> bool:
        now = time.time()
        if self.redis_connected:
            return True
        if now - self.redis_last_connect_attempt < self.redis_reconnect_interval_s:
            return False

        self.redis_last_connect_attempt = now
        try:
            self.redis_client = redis.Redis.from_url(
                self.redis_url,
                password=self.redis_password,
                db=self.redis_db,
                socket_timeout=self.connect_timeout_s,
                socket_connect_timeout=self.connect_timeout_s,
                decode_responses=True,
            )
            self.redis_client.ping()
            self.redis_connected = True
            return True
        except Exception as e:
            self.redis_connected = False
            self.redis_client = None
            sys.stderr.write(f"redis-mirror connect failed ({self.redis_url!r}): {e}\n")
            sys.stderr.flush()
            return False

    def close(self) -> None:
        if self.fp:
            try:
                self.fp.close()
            except Exception:
                pass
        if self.redis_client:
            try:
                self.redis_client.close()
            except Exception:
                pass


class TailReader:
    def __init__(self, state: State):
        self.state = state

    def open_log(self) -> None:
        if self.state.fp is not None:
            try:
                self.state.fp.close()
            except Exception:
                pass

        if not self.state.log_path.exists():
            self.state.fp = None
            self.state.inode = None
            return

        self.state.fp = self.state.log_path.open("r", encoding="utf-8", errors="replace")
        seek_target = self.state.offset if self.state.inode == self.state.log_path.stat().st_ino else 0
        self.state.fp.seek(seek_target)
        self.state.inode = self.state.log_path.stat().st_ino

    def check_rotation(self) -> None:
        if not self.state.log_path.exists():
            if self.state.fp is not None:
                self.state.fp.close()
                self.state.fp = None
            return

        st = self.state.log_path.stat()
        if self.state.fp is None or st.st_ino != self.state.inode:
            if self.state.fp is not None:
                try:
                    self.state.fp.close()
                except Exception:
                    pass
            self.state.fp = self.state.log_path.open("r", encoding="utf-8", errors="replace")
            self.state.inode = st.st_ino
            self.state.offset = 0
            return

        if self.state.fp.tell() > st.st_size:
            self.state.fp.seek(0)

    def read_new_lines(self) -> List[str]:
        if self.state.fp is None:
            self.open_log()
            return []

        self.check_rotation()
        if self.state.fp is None:
            return []

        out: List[str] = []
        while True:
            line = self.state.fp.readline()
            if not line:
                break
            line = line.strip()
            if line:
                out.append(line)

        if out:
            self.state.offset = self.state.fp.tell()
            save_offset(
                self.state.offset_file,
                {"inode": self.state.inode, "offset": self.state.offset},
            )

        return out


def matches_channel(event_type: str, prefixes: List[str]) -> bool:
    for prefix in prefixes:
        if event_type.startswith(prefix):
            return True
    return False


def xadd_args(maxlen: int, trim_strategy: str, min_idle_ms: int) -> List[str]:
    if trim_strategy == "MINID":
        return ["MINID", f"{min_idle_ms}"]
    return ["MAXLEN", str(maxlen)]


def _emit_dead_letter(state: State, event_type: str, raw: str, violation_detail: str) -> None:
    """Emit a bus.dead_letter.v1 event to Redis for a schema-violating event.

    This is best-effort — if Redis is unreachable the dead-letter itself is
    silently dropped (we don't recurse or block).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import uuid
    dl_id = str(uuid.uuid4())
    dl_data = {
        "failure_reason": "schema_violation",
        "original_type": event_type,
        "original_payload_excerpt": raw[:500],
        "schema_violation_detail": violation_detail[:500],
    }
    dl_envelope = {
        "specversion": "1.0",
        "id": dl_id,
        "source": "/redis-mirror",
        "type": "bus.dead_letter",
        "time": now,
        "datacontenttype": "application/json",
        "data": dl_data,
    }
    dl_raw = json.dumps(dl_envelope)
    dl_fields: Dict[str, str] = {
        "_raw": dl_raw[:2000],
        "type": "bus.dead_letter",
        "source": "/redis-mirror",
        "timestamp": now,
        "event_id": dl_id,
    }
    try:
        if state.redis_client and state.redis_connected:
            state.redis_client.xadd("nbus:bus.dead_letter", dl_fields, maxlen=10000, approximate=True)
            state.redis_client.xadd("nbus:all", dl_fields, maxlen=50000, approximate=True)
    except Exception:
        pass  # dead-letter emission is never fatal


def mirror_event(state: State, raw: str) -> bool:
    try:
        event = json.loads(raw)
    except Exception:
        return False

    event_type = event.get("type") or ""
    if not state.mirror_all and not matches_channel(event_type, state.channel_prefixes):
        return False

    if not state.connect_redis():
        state.events_dropped += 1
        return False

    # ── Schema validation ────────────────────────────────────────────────────
    registry = get_schema_registry()
    registry.maybe_reload()
    violation = registry.validate(event_type, event)
    if violation is not None:
        sys.stderr.write(
            f"[nbus schema] validation failed for {event_type}: {violation}\n"
        )
        sys.stderr.flush()
        _emit_dead_letter(state, event_type, raw, violation)
        state.events_dropped += 1
        return False
    # ────────────────────────────────────────────────────────────────────────

    stream_name = f"nbus:{event_type}"

    try:
        fields: Dict[str, str] = {}
        for key, value in event.items():
            if key == "data":
                data = value if isinstance(value, dict) else {}
                for dk, dv in data.items():
                    fields[f"data.{dk}"] = json.dumps(dv) if not isinstance(dv, str) else str(dv)
            elif key == "id":
                fields["event_id"] = str(value)
            elif key == "time":
                fields["timestamp"] = str(value)
            elif key == "type":
                pass
            else:
                fields[key] = json.dumps(value) if not isinstance(value, str) else str(value)

        fields["_raw"] = raw[:2000]

        if state.trim_strategy == "MINID":
            state.redis_client.xadd(stream_name, fields, minid=str(state.min_idle_ms), approximate=True)
        else:
            state.redis_client.xadd(stream_name, fields, maxlen=state.maxlen, approximate=True)
        state.events_mirrored += 1

        if state.universal_stream:
            try:
                state.redis_client.xadd(
                    state.universal_stream, fields,
                    maxlen=state.universal_stream_maxlen, approximate=True,
                )
            except redis.RedisError:
                pass  # universal stream failure is non-fatal

        return True

    except redis.RedisError as e:
        state.redis_connected = False
        state.redis_client = None
        state.redis_errors += 1
        state.events_dropped += 1
        sys.stderr.write(f"redis-mirror xadd failed (stream={stream_name}): {e}\n")
        sys.stderr.flush()
        return False


def log_metrics(state: State) -> None:
    elapsed = time.time() - state.started_at
    events_per_sec = state.events_mirrored / max(1, elapsed)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.stderr.write(
        f"[{now_str}] redis-mirror metrics: "
        f"mirrored={state.events_mirrored} dropped={state.events_dropped} "
        f"redis_errors={state.redis_errors} rate={events_per_sec:.2f}/s\n"
    )
    sys.stderr.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="redis-mirror — Redis Streams adapter for nervous-bus")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to config.toml (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG,
        help=f"Path to debug.jsonl (default: {DEFAULT_LOG})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Tail existing log, process, and exit (test mode)",
    )
    args = parser.parse_args()

    if not args.config.exists():
        sys.stderr.write(f"config not found: {args.config}\n")
        return 1

    cfg = load_config(args.config)

    channel_prefixes = cfg["channels"]
    if not channel_prefixes and not cfg.get("mirror_all", False):
        sys.stderr.write("redis-mirror: no channels configured, exiting quietly\n")
        return 0

    state = State(
        log_path=args.log,
        channel_prefixes=channel_prefixes,
        redis_url=cfg["redis_url"],
        redis_password=cfg["redis_password"],
        redis_db=cfg["redis_db"],
        connect_timeout_s=cfg["connect_timeout_s"],
        maxlen=cfg["maxlen"],
        trim_strategy=cfg["trim_strategy"],
        min_idle_ms=cfg["min_idle_ms"],
        offset_file=cfg["offset_file"],
        metrics_interval_s=cfg["metrics_interval_s"],
        mirror_all=cfg.get("mirror_all", False),
        universal_stream=cfg.get("universal_stream", ""),
        universal_stream_maxlen=cfg.get("universal_stream_maxlen", 50000),
    )

    reader = TailReader(state)
    reader.open_log()

    if args.once:
        for line in reader.read_new_lines():
            mirror_event(state, line)
        log_metrics(state)
        state.close()
        return 0

    last_metrics = time.time()

    while True:
        try:
            for line in reader.read_new_lines():
                mirror_event(state, line)

            now = time.time()
            if now - last_metrics >= state.metrics_interval_s:
                log_metrics(state)
                last_metrics = now

            time.sleep(0.1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stderr.write(f"redis-mirror error: {e}\n")
            time.sleep(1)

    state.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())