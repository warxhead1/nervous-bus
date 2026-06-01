"""Tests for the FileSource event source."""

from __future__ import annotations

import json
import time

from pulse_app.source import (
    FileSource,
    REPLAY_SPEED_CAP,
    REPLAY_STATE,
    ReplaySource,
    replay_from_file,
    set_replay_state,
)


def test_file_source_reads_existing_events(sample_jsonl):
    src = FileSource(sample_jsonl, follow=False, from_start=True)
    events = list(src.iter_events())
    assert len(events) == 8
    types = {e["type"] for e in events}
    assert "autobench.iteration.v1" in types
    assert "autobench.sandbox.v1" in types


def test_file_source_handles_missing_file(tmp_path):
    src = FileSource(tmp_path / "nope.jsonl", follow=False, from_start=True)
    events = list(src.iter_events())
    assert events == []


def test_replay_source_honors_speed_multiplier(tmp_path):
    """nervous-bus-zynw — 3 events 100ms apart, speed=100x → <1s total.

    Real-time (1x) the sleeps would total ~200ms; at 100x they collapse
    to ~2ms. Generous 1s budget accommodates GC pauses and CI jitter.
    """
    path = tmp_path / "replay.jsonl"
    # Three events spaced 100ms apart in their RFC3339 timestamps.
    events = [
        {
            "specversion": "1.0", "id": f"e{i}", "source": "/autobench",
            "type": "autobench.iteration.v1",
            "time": ts,
            "data": {"session_id": "01REPLAY_TEST_SESSION_ZYNW",
                     "iteration": i, "harness_version": "v0", "status": "start"},
        }
        for i, ts in enumerate([
            "2026-05-16T10:00:00.000Z",
            "2026-05-16T10:00:00.100Z",
            "2026-05-16T10:00:00.200Z",
        ])
    ]
    with open(path, "w") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")

    t0 = time.monotonic()
    out = list(replay_from_file(path, speed=100.0))
    elapsed = time.monotonic() - t0
    assert len(out) == 3
    assert elapsed < 1.0, f"replay@100x took {elapsed:.3f}s — should be <1s"


def test_replay_source_session_id_filter(tmp_path):
    """ReplaySource only yields events for the requested session_id."""
    path = tmp_path / "multi.jsonl"
    with open(path, "w") as fh:
        for sid in ("AAA", "BBB", "AAA"):
            fh.write(json.dumps({
                "type": "autobench.iteration.v1",
                "time": "2026-05-16T10:00:00.000Z",
                "data": {"session_id": sid, "iteration": 0,
                         "harness_version": "v0", "status": "start"},
            }) + "\n")
    out = list(replay_from_file(path, session_id="AAA", speed=100.0))
    assert len(out) == 2
    assert all(e["data"]["session_id"] == "AAA" for e in out)


def test_replay_speed_clamped_to_cap(tmp_path):
    """speed=1e9 is silently clamped to REPLAY_SPEED_CAP (100x)."""
    src = ReplaySource(tmp_path / "x.jsonl", speed=1e9)
    assert src.speed == REPLAY_SPEED_CAP


def test_set_replay_state_global_flag():
    """set_replay_state mutates the module-level dict the badge reads."""
    set_replay_state(True, speed=42.0)
    assert REPLAY_STATE["active"] is True
    assert REPLAY_STATE["speed"] == 42.0
    set_replay_state(False, speed=1.0)
    assert REPLAY_STATE["active"] is False
