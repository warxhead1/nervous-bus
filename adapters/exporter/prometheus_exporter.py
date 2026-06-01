"""nervous-bus -> Prometheus exporter (bead nervous-bus-qy8n).

Hybrid exporter:
  1. Tails ``~/.cache/nervous-bus/debug.jsonl`` and translates CloudEvents-lite
     JSONL into per-channel counters/rates (original behaviour).
  2. Polls Valkey every 15 s for stream-level metrics:
     - nbus_stream_length{stream}
     - nbus_consumer_group_lag{stream,group}
     - nbus_consumer_group_pending{stream,group}
     - nbus_events_total{event_type,project}  (sampled, last 100 events/tick)
     - nbus_events_per_second  (rate over last 60 s window)
     - nbus_dead_letters_total
     - nbus_stream_first_entry_age_seconds

Exposes /metrics on HTTP (default :9418).  Uses ``prometheus_client`` when
available; falls back to hand-rolled Prometheus text format so no extra deps
are required.

Run::

    python adapters/exporter/prometheus_exporter.py [--port 9418]

Backpressure: events flow through a bounded queue; oldest is dropped on
overflow and ``nbus_exporter_dropped_total`` increments.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import redis

# ── Valkey defaults ───────────────────────────────────────────────────────────
VALKEY_URL = "redis://localhost:6379"
UNIVERSAL_STREAM = "nbus:all"
POLL_INTERVAL_S = 15.0
SAMPLE_COUNT = 100        # events sampled per tick for nbus_events_total
RATE_WINDOW_SECS = 60.0
QUEUE_MAXSIZE = 10_000

DEFAULT_DEBUG_FILE = Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"
DEFAULT_PORT = 9418


# ── Prometheus text format helpers ────────────────────────────────────────────
# Used when prometheus_client is not installed.

def _labels_str(labels: Dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in labels.items()]
    return "{" + ",".join(parts) + "}"


class _TextMetric:
    """Minimal prometheus text format metric (HELP + TYPE + lines)."""

    def __init__(self, name: str, help_text: str, mtype: str) -> None:
        self.name = name
        self.help_text = help_text
        self.mtype = mtype  # "counter", "gauge", "untyped"
        self._values: Dict[str, float] = {}   # labels_str -> value
        self._lock = threading.Lock()

    def set(self, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_str(labels or {})
        with self._lock:
            self._values[key] = value

    def inc(self, delta: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        key = _labels_str(labels or {})
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + delta

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.mtype}"]
        ts_ms = int(time.time() * 1000)
        with self._lock:
            snapshot = dict(self._values)
        for key, val in sorted(snapshot.items()):
            lines.append(f"{self.name}{key} {val} {ts_ms}")
        return "\n".join(lines)


class _TextRegistry:
    def __init__(self) -> None:
        self._metrics: List[_TextMetric] = []
        self._lock = threading.Lock()

    def register(self, m: _TextMetric) -> _TextMetric:
        with self._lock:
            self._metrics.append(m)
        return m

    def render_all(self) -> str:
        with self._lock:
            metrics = list(self._metrics)
        return "\n".join(m.render() for m in metrics) + "\n"


# ── Try importing prometheus_client; fall back to text format ─────────────────
try:
    from prometheus_client import (
        CollectorRegistry,
        Counter as _Counter,
        Gauge as _Gauge,
        generate_latest,
        start_http_server as _start_http_server,
    )
    _HAS_PROM = True
except ImportError:
    _HAS_PROM = False


# ── Metric façade — same API regardless of backend ────────────────────────────

class MetricRegistry:
    """Thin wrapper so the rest of the code is backend-agnostic."""

    def __init__(self) -> None:
        if _HAS_PROM:
            self._prom_registry = CollectorRegistry()
            self._backend = "prometheus_client"
        else:
            self._text_registry = _TextRegistry()
            self._backend = "text"

    def _new_counter(self, name: str, help_text: str, labelnames: List[str]):
        if _HAS_PROM:
            return _Counter(name, help_text, labelnames, registry=self._prom_registry)
        m = _TextMetric(name, help_text, "counter")
        self._text_registry.register(m)
        return _TextCounterAdapter(m, labelnames)

    def _new_gauge(self, name: str, help_text: str, labelnames: List[str]):
        if _HAS_PROM:
            return _Gauge(name, help_text, labelnames, registry=self._prom_registry)
        m = _TextMetric(name, help_text, "gauge")
        self._text_registry.register(m)
        return _TextGaugeAdapter(m, labelnames)

    def generate(self) -> str:
        if _HAS_PROM:
            return generate_latest(self._prom_registry).decode("utf-8")
        return self._text_registry.render_all()


class _TextCounterAdapter:
    """Adapts _TextMetric to prometheus_client Counter interface."""

    def __init__(self, metric: _TextMetric, labelnames: List[str]) -> None:
        self._m = metric
        self._labelnames = labelnames

    def labels(self, *args, **kwargs) -> "_TextCounterAdapter":
        clone = _TextCounterLabelSet(self._m, self._labelnames, args, kwargs)
        return clone

    def inc(self, amount: float = 1.0) -> None:
        self._m.inc(amount, {})


class _TextCounterLabelSet:
    def __init__(self, metric: _TextMetric, labelnames: List[str], args, kwargs) -> None:
        self._m = metric
        if args:
            self._labels = dict(zip(labelnames, args))
        else:
            self._labels = {k: str(v) for k, v in kwargs.items()}

    def inc(self, amount: float = 1.0) -> None:
        self._m.inc(amount, self._labels)


class _TextGaugeAdapter:
    """Adapts _TextMetric to prometheus_client Gauge interface."""

    def __init__(self, metric: _TextMetric, labelnames: List[str]) -> None:
        self._m = metric
        self._labelnames = labelnames

    def labels(self, *args, **kwargs) -> "_TextGaugeLabelSet":
        return _TextGaugeLabelSet(self._m, self._labelnames, args, kwargs)

    def set(self, value: float) -> None:
        self._m.set(value, {})

    def inc(self, amount: float = 1.0) -> None:
        self._m.inc(amount, {})


class _TextGaugeLabelSet:
    def __init__(self, metric: _TextMetric, labelnames: List[str], args, kwargs) -> None:
        self._m = metric
        if args:
            self._labels = dict(zip(labelnames, args))
        else:
            self._labels = {k: str(v) for k, v in kwargs.items()}

    def set(self, value: float) -> None:
        self._m.set(value, self._labels)

    def inc(self, amount: float = 1.0) -> None:
        self._m.inc(amount, self._labels)


# ── All metrics ───────────────────────────────────────────────────────────────

class ExporterMetrics:
    """All metric objects + per-channel rate state + Valkey polling."""

    def __init__(self, registry: MetricRegistry, valkey_url: str = VALKEY_URL) -> None:
        self.registry = registry
        self.valkey_url = valkey_url

        # ── Valkey stream metrics ──────────────────────────────────────────
        self.stream_length = registry._new_gauge(
            "nbus_stream_length",
            "Current XLEN of an nbus stream.",
            ["stream"],
        )
        self.group_lag = registry._new_gauge(
            "nbus_consumer_group_lag",
            "Consumer group lag (unread entries) for an nbus stream.",
            ["stream", "group"],
        )
        self.group_pending = registry._new_gauge(
            "nbus_consumer_group_pending",
            "Consumer group PEL size (unacked entries) for an nbus stream.",
            ["stream", "group"],
        )
        self.events_total = registry._new_counter(
            "nbus_events_total",
            "Approximate event count by type and project, sampled from nbus:all.",
            ["event_type", "project"],
        )
        self.events_per_second = registry._new_gauge(
            "nbus_events_per_second",
            "Approximate events/s over the last 60 s window, from nbus:all stream IDs.",
            [],
        )
        self.dead_letters_total = registry._new_gauge(
            "nbus_dead_letters_total",
            "Total dead-letter entries in nbus:bus.dead_letter.",
            [],
        )
        self.first_entry_age = registry._new_gauge(
            "nbus_stream_first_entry_age_seconds",
            "Age in seconds of the oldest entry in nbus:all (retention freshness).",
            [],
        )

        # ── JSONL-tail metrics (kept from original exporter) ──────────────
        self.jsonl_events_total = registry._new_counter(
            "nbus_jsonl_events_total",
            "Total nervous-bus events seen via JSONL tail, by channel/project.",
            ["channel", "project"],
        )
        self.jsonl_events_per_second = registry._new_gauge(
            "nbus_jsonl_events_per_second",
            "Rolling 60s events-per-second by channel (JSONL tail).",
            ["channel"],
        )
        self.dropped_total = registry._new_counter(
            "nbus_exporter_dropped_total",
            "Events dropped due to backpressure (queue full).",
            [],
        )

        # Internal state
        self._rate_window: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._redis: Optional[redis.Redis] = None
        self._redis_lock = threading.Lock()

        # Track last seen stream position for rate calculation
        self._last_stream_id: Optional[str] = None
        self._last_stream_ts: Optional[float] = None
        self._last_stream_count: Optional[int] = None

        # Track already-counted event type/project pairs to approximate increments
        self._counted_ids: deque = deque(maxlen=SAMPLE_COUNT * 10)

    # ── Valkey connection management ──────────────────────────────────────────

    def _get_redis(self) -> Optional[redis.Redis]:
        with self._redis_lock:
            if self._redis is not None:
                try:
                    self._redis.ping()
                    return self._redis
                except Exception:
                    self._redis = None
            try:
                r = redis.Redis.from_url(
                    self.valkey_url,
                    decode_responses=True,
                    socket_timeout=3,
                    socket_connect_timeout=3,
                )
                r.ping()
                self._redis = r
                return r
            except Exception as e:
                sys.stderr.write(f"[prometheus_exporter] Valkey connect failed: {e}\n")
                return None

    # ── Valkey poll ───────────────────────────────────────────────────────────

    def poll_valkey(self) -> None:
        """Refresh all Valkey-sourced metrics. Called every POLL_INTERVAL_S seconds."""
        r = self._get_redis()
        if r is None:
            return

        now = time.time()

        # 1. Stream lengths for nbus:all and nbus:bus.dead_letter
        try:
            all_len = r.xlen(UNIVERSAL_STREAM)
            self.stream_length.labels(UNIVERSAL_STREAM).set(float(all_len))
        except Exception as e:
            sys.stderr.write(f"[prometheus_exporter] xlen nbus:all failed: {e}\n")
            all_len = None

        try:
            dl_len = r.xlen("nbus:bus.dead_letter")
            self.stream_length.labels("nbus:bus.dead_letter").set(float(dl_len))
            self.dead_letters_total.set(float(dl_len))
        except Exception:
            dl_len = 0
            self.dead_letters_total.set(0.0)

        # 2. Consumer group lag + pending for nbus:all
        try:
            groups = r.xinfo_groups(UNIVERSAL_STREAM)
            for g in groups:
                group_name = g.get("name", "unknown")
                lag = int(g.get("lag", 0))
                pending = int(g.get("pel-count", 0))
                self.group_lag.labels(UNIVERSAL_STREAM, group_name).set(float(lag))
                self.group_pending.labels(UNIVERSAL_STREAM, group_name).set(float(pending))
        except Exception as e:
            sys.stderr.write(f"[prometheus_exporter] xinfo_groups nbus:all failed: {e}\n")

        # 3. First entry age in nbus:all
        try:
            first = r.xrange(UNIVERSAL_STREAM, count=1)
            if first:
                entry_id, _ = first[0]
                ts_ms = int(entry_id.split("-")[0])
                age_s = now - ts_ms / 1000.0
                self.first_entry_age.set(float(age_s))
        except Exception as e:
            sys.stderr.write(f"[prometheus_exporter] xrange first entry failed: {e}\n")

        # 4. Sample last SAMPLE_COUNT events for event_type/project counters
        try:
            entries = r.xrevrange(UNIVERSAL_STREAM, count=SAMPLE_COUNT)
            new_ids = set()
            for entry_id, fields in entries:
                if entry_id in self._counted_ids:
                    continue
                new_ids.add(entry_id)
                raw = fields.get("_raw", "")
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                event_type = str(evt.get("type") or "unknown")
                data = evt.get("data") or {}
                source = str(evt.get("source") or "")
                project = str(data.get("project") or _project_from_source(source))
                self.events_total.labels(event_type, project).inc(1.0)
            for eid in new_ids:
                self._counted_ids.append(eid)
        except Exception as e:
            sys.stderr.write(f"[prometheus_exporter] xrevrange sample failed: {e}\n")

        # 5. Events per second — compare stream length now vs last poll
        if all_len is not None:
            if self._last_stream_count is not None and self._last_stream_ts is not None:
                elapsed = now - self._last_stream_ts
                delta = max(0, all_len - self._last_stream_count)
                if elapsed > 0:
                    rate = delta / elapsed
                    self.events_per_second.set(round(rate, 4))
            self._last_stream_count = all_len
            self._last_stream_ts = now

    # ── JSONL-tail ingest ─────────────────────────────────────────────────────

    def ingest_jsonl(self, evt: dict) -> None:
        """Process one event from the JSONL tail."""
        channel = str(evt.get("type") or "unknown")
        source = str(evt.get("source") or "unknown")
        data = evt.get("data") or {}
        project = str(data.get("project") or _project_from_source(source))

        self.jsonl_events_total.labels(channel, project).inc()

        now = time.monotonic()
        with self._lock:
            window = self._rate_window[channel]
            window.append(now)
            cutoff = now - RATE_WINDOW_SECS
            while window and window[0] < cutoff:
                window.popleft()

    def refresh_jsonl_rates(self) -> None:
        now = time.monotonic()
        cutoff = now - RATE_WINDOW_SECS
        with self._lock:
            channels = list(self._rate_window.keys())
        for ch in channels:
            with self._lock:
                window = self._rate_window[ch]
                while window and window[0] < cutoff:
                    window.popleft()
                count = len(window)
            self.jsonl_events_per_second.labels(ch).set(count / RATE_WINDOW_SECS)


def _project_from_source(source: str) -> str:
    s = source.strip("/")
    return s.split("/", 1)[0] if s else "unknown"


# ── JSONL tailer ──────────────────────────────────────────────────────────────

def _tail_jsonl(path: Path, stop_event: threading.Event):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    with open(path, "r") as fh:
        fh.seek(0, 2)
        while not stop_event.is_set():
            line = fh.readline()
            if not line:
                stop_event.wait(0.25)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ── HTTP server ───────────────────────────────────────────────────────────────

def _make_handler(metrics_registry: MetricRegistry):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/metrics", "/metrics/"):
                body = metrics_registry.generate().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # suppress request logs
            pass

    return Handler


# ── Main exporter class ───────────────────────────────────────────────────────

class Exporter:
    def __init__(
        self,
        port: int = DEFAULT_PORT,
        path: Path = DEFAULT_DEBUG_FILE,
        valkey_url: str = VALKEY_URL,
    ) -> None:
        self.port = port
        self.path = path
        self._registry = MetricRegistry()
        self.metrics = ExporterMetrics(self._registry, valkey_url=valkey_url)
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _producer(self) -> None:
        for evt in _tail_jsonl(self.path, self._stop):
            try:
                self._q.put_nowait(evt)
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
                self.metrics.dropped_total.inc()
                try:
                    self._q.put_nowait(evt)
                except queue.Full:
                    self.metrics.dropped_total.inc()

    def _consumer(self) -> None:
        while not self._stop.is_set():
            try:
                evt = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.metrics.ingest_jsonl(evt)
            except Exception:
                pass

    def _rate_ticker(self) -> None:
        while not self._stop.is_set():
            self.metrics.refresh_jsonl_rates()
            self._stop.wait(1.0)

    def _valkey_poller(self) -> None:
        while not self._stop.is_set():
            try:
                self.metrics.poll_valkey()
            except Exception as e:
                sys.stderr.write(f"[prometheus_exporter] poll error: {e}\n")
            self._stop.wait(POLL_INTERVAL_S)

    def start(self) -> None:
        for target in (self._producer, self._consumer, self._rate_ticker, self._valkey_poller):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port for /metrics (default: {DEFAULT_PORT})")
    parser.add_argument("--path", type=Path, default=DEFAULT_DEBUG_FILE,
                        help="Path to debug.jsonl (JSONL tail source)")
    parser.add_argument("--valkey-url", default=VALKEY_URL,
                        help=f"Valkey/Redis URL (default: {VALKEY_URL})")
    parser.add_argument("--once", action="store_true",
                        help="Poll Valkey once, print metrics text, exit (no HTTP)")
    args = parser.parse_args(argv)

    exporter = Exporter(port=args.port, path=args.path, valkey_url=args.valkey_url)

    if args.once:
        exporter.metrics.poll_valkey()
        print(exporter._registry.generate())
        return 0

    # Do an initial Valkey poll before serving so metrics aren't empty
    exporter.metrics.poll_valkey()

    if _HAS_PROM:
        # prometheus_client has its own threaded HTTP server
        _start_http_server(args.port, registry=exporter._registry._prom_registry)
    else:
        handler = _make_handler(exporter._registry)
        httpd = HTTPServer(("0.0.0.0", args.port), handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True, name="http_server")
        t.start()

    exporter.start()
    backend = "prometheus_client" if _HAS_PROM else "text-format"
    print(
        f"nbus Prometheus exporter ({backend}) listening on "
        f":{args.port}/metrics  (JSONL: {args.path}  Valkey: {args.valkey_url})"
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        exporter.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
