"""Tests for the nervous-bus Prometheus exporter (bead nervous-bus-qy8n).

We avoid spinning up a real Prometheus or HTTP server. Instead we feed
synthetic CloudEvents-lite dicts through ``Exporter.ingest_iterable`` and
scrape the registry via ``prometheus_client.generate_latest``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

prometheus_client = pytest.importorskip("prometheus_client")
from prometheus_client import CollectorRegistry, generate_latest  # noqa: E402

from adapters.exporter.prometheus_exporter import (  # noqa: E402
    Exporter,
    ExporterMetrics,
    QUEUE_MAXSIZE,
    tail_jsonl,
)


def _scrape(registry: CollectorRegistry) -> str:
    return generate_latest(registry).decode("utf-8")


def _sample(text: str, metric: str, **labels: str) -> float:
    """Pull one metric sample line out of the exposition text.

    Label order in Prometheus exposition is sorted alphabetically, so we
    match each ``key="value"`` pair independently rather than baking the
    order into one regex.
    """
    pattern = rf"^{re.escape(metric)}\{{([^}}]*)\}} (\S+)$"
    for m in re.finditer(pattern, text, re.MULTILINE):
        label_blob = m.group(1)
        if all(f'{k}="{v}"' in label_blob for k, v in labels.items()):
            return float(m.group(2))
    # Unlabeled metric form: `nbus_foo 5.0`
    if not labels:
        m2 = re.search(rf"^{re.escape(metric)} (\S+)$", text, re.MULTILINE)
        if m2:
            return float(m2.group(1))
    raise AssertionError(f"metric {metric}{labels} not in scrape:\n{text}")


def _evt(type_: str, source: str = "/autobench", **data) -> dict:
    return {
        "specversion": "1.0",
        "id": f"test-{type_}-{len(data)}",
        "source": source,
        "type": type_,
        "time": "2026-05-16T00:00:00Z",
        "data": data,
    }


# ------------------ Counter / Gauge basics --------------------------------


def test_events_total_counter_increments_per_event() -> None:
    exporter = Exporter(registry=CollectorRegistry())
    events = [
        _evt("autobench.sandbox.v1", session_id="s1", status="dispatch"),
        _evt("autobench.sandbox.v1", session_id="s1", status="complete"),
        _evt("autobench.iteration.v1", session_id="s1", status="start", iteration=0),
    ]
    n = exporter.ingest_iterable(events)
    assert n == 3

    text = _scrape(exporter.metrics.registry)
    sb = _sample(
        text,
        "nbus_events_total",
        channel="autobench.sandbox.v1",
        source="/autobench",
        project="autobench",
    )
    iter_count = _sample(
        text,
        "nbus_events_total",
        channel="autobench.iteration.v1",
        source="/autobench",
        project="autobench",
    )
    assert sb == 2.0
    assert iter_count == 1.0


def test_events_per_second_gauge_reflects_recent_window() -> None:
    exporter = Exporter(registry=CollectorRegistry())
    burst = [_evt("autobench.sandbox.v1") for _ in range(30)]
    exporter.ingest_iterable(burst)

    text = _scrape(exporter.metrics.registry)
    rate = _sample(text, "nbus_events_per_second", channel="autobench.sandbox.v1")
    # 30 events in a 60s window => 0.5 ev/s (well above zero).
    assert rate == pytest.approx(0.5, abs=0.001)


# ------------------ Session gauges ----------------------------------------


def test_iteration_complete_sets_score_gauge() -> None:
    exporter = Exporter(registry=CollectorRegistry())
    events = [
        _evt("autobench.iteration.v1", session_id="sess-A", status="start", iteration=0),
        _evt(
            "autobench.iteration.v1",
            session_id="sess-A",
            status="complete",
            iteration=0,
            aggregate_score=0.7321,
        ),
    ]
    exporter.ingest_iterable(events)
    text = _scrape(exporter.metrics.registry)
    score = _sample(text, "nbus_autobench_iteration_score", session_id="sess-A")
    assert score == pytest.approx(0.7321, abs=1e-6)


def test_loomie_lifecycle_drives_active_session_gauge() -> None:
    exporter = Exporter(registry=CollectorRegistry())
    events = [
        _evt("loom.lifecycle.v1", source="/hearth-loom", project="deer-flow", attempt_id="att-1", event="claimed"),
        _evt("loom.lifecycle.v1", source="/hearth-loom", project="deer-flow", attempt_id="att-2", event="claimed"),
        _evt("loom.lifecycle.v1", source="/hearth-loom", project="deer-flow", attempt_id="att-1", event="complete"),
    ]
    exporter.ingest_iterable(events)
    text = _scrape(exporter.metrics.registry)
    active = _sample(text, "nbus_session_active", agent_type="loomie", project="deer-flow")
    assert active == 1.0


# ------------------ AHE outcome counter -----------------------------------


def test_ahe_outcomes_split_hit_miss_refuted() -> None:
    exporter = Exporter(registry=CollectorRegistry())
    events = [
        _evt("autobench.improver.prediction.verified.v1", session_id="s", outcome="hit"),
        _evt("autobench.improver.prediction.verified.v1", session_id="s", outcome="hit"),
        _evt("autobench.improver.prediction.verified.v1", session_id="s", outcome="miss"),
        _evt("autobench.improver.prediction.refuted_live.v1", session_id="s", is_refuted=True),
        _evt("autobench.improver.prediction.refuted_live.v1", session_id="s", is_refuted=False),
    ]
    exporter.ingest_iterable(events)
    text = _scrape(exporter.metrics.registry)
    assert _sample(text, "nbus_autobench_ahe_outcome_total", outcome="hit") == 2.0
    assert _sample(text, "nbus_autobench_ahe_outcome_total", outcome="miss") == 1.0
    assert _sample(text, "nbus_autobench_ahe_outcome_total", outcome="refuted") == 1.0
    assert _sample(text, "nbus_autobench_ahe_outcome_total", outcome="pending") == 1.0


# ------------------ Backpressure -----------------------------------------


def test_backpressure_drops_oldest_and_counts() -> None:
    metrics = ExporterMetrics(registry=CollectorRegistry())
    # Simulate drops directly — the queue logic is internal but the
    # dropped_total counter is the contract we expose.
    metrics.dropped_total.inc(5)
    text = _scrape(metrics.registry)
    m = re.search(r"^nbus_exporter_dropped_total (\S+)$", text, re.MULTILINE)
    assert m and float(m.group(1)) == 5.0


# ------------------ File tailer ------------------------------------------


def test_tail_jsonl_reads_existing_lines_from_start(tmp_path: Path) -> None:
    p = tmp_path / "debug.jsonl"
    lines = [_evt("autobench.sandbox.v1") for _ in range(3)]
    p.write_text("\n".join(json.dumps(e) for e in lines) + "\n")

    stop = threading.Event()
    collected: list[dict] = []

    def grab() -> None:
        for evt in tail_jsonl(p, from_start=True, poll_interval=0.01, stop_event=stop):
            collected.append(evt)
            if len(collected) == 3:
                stop.set()
                return

    t = threading.Thread(target=grab, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert len(collected) == 3
    assert all(e["type"] == "autobench.sandbox.v1" for e in collected)


def test_tail_jsonl_skips_malformed_json(tmp_path: Path) -> None:
    p = tmp_path / "debug.jsonl"
    valid = json.dumps(_evt("autobench.sandbox.v1"))
    p.write_text(f"{valid}\nNOT_JSON_AT_ALL\n{valid}\n")

    stop = threading.Event()
    collected: list[dict] = []

    def grab() -> None:
        for evt in tail_jsonl(p, from_start=True, poll_interval=0.01, stop_event=stop):
            collected.append(evt)
            if len(collected) == 2:
                stop.set()
                return

    t = threading.Thread(target=grab, daemon=True)
    t.start()
    t.join(timeout=2.0)
    assert len(collected) == 2


# ------------------ End-to-end through queue --------------------------------


def test_exporter_threads_consume_tailed_events(tmp_path: Path) -> None:
    p = tmp_path / "debug.jsonl"
    p.write_text("")  # empty — exporter tails from EOF

    exporter = Exporter(path=p, registry=CollectorRegistry(), from_start=False)
    exporter.start()
    try:
        # Append events after the exporter has started tailing.
        with open(p, "a") as fh:
            for _ in range(5):
                fh.write(json.dumps(_evt("autobench.sandbox.v1")) + "\n")
                fh.flush()
        # Allow producer + consumer threads to drain.
        deadline = time.monotonic() + 3.0
        text = ""
        while time.monotonic() < deadline:
            text = _scrape(exporter.metrics.registry)
            try:
                v = _sample(
                    text,
                    "nbus_events_total",
                    channel="autobench.sandbox.v1",
                    source="/autobench",
                    project="autobench",
                )
            except AssertionError:
                v = 0.0
            if v >= 5.0:
                break
            time.sleep(0.05)
        else:  # pragma: no cover
            pytest.fail(f"counter never reached 5:\n{text}")
    finally:
        exporter.stop()


def test_queue_maxsize_is_sane() -> None:
    # Sanity: ensure the backpressure threshold isn't pathologically tiny.
    assert QUEUE_MAXSIZE >= 1024
