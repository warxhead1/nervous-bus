"""Shared pytest fixtures for pulse_app tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the adapter dir is on sys.path so `import pulse_app` works without install.
HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@pytest.fixture
def sample_events() -> list[dict]:
    """A canonical sequence of autobench events for one session.

    Mirrors the shape of records in ~/.cache/nervous-bus/debug.jsonl.
    """
    sid = "01KRQMD4M20RYCDS8X5CHWTPMP"
    return [
        {
            "specversion": "1.0", "id": "e1", "source": "/autobench",
            "type": "autobench.iteration.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:34.722Z",
            "data": {"iteration": 0, "harness_version": "v0", "status": "start", "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e2", "source": "/autobench",
            "type": "autobench.phase.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:36.725Z",
            "data": {"phase": "benchmark", "status": "start",
                     "extra": {"num_cases": 2, "bench_name": "shader_tier1"},
                     "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e3", "source": "/autobench",
            "type": "autobench.sandbox.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:38.728Z",
            "data": {"case_id": "probe-1", "status": "complete",
                     "verdict": "OK", "language": "python", "sandbox_type": "subprocess",
                     "latency_ms": 12.4, "exit_code": 0, "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e4", "source": "/autobench",
            "type": "autobench.sandbox.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:39.728Z",
            "data": {"case_id": "probe-2", "status": "complete",
                     "verdict": "OK", "language": "python", "sandbox_type": "subprocess",
                     "latency_ms": 11.0, "exit_code": 0, "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e5", "source": "/autobench",
            "type": "autobench.improver.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:40.5Z",
            "data": {"status": "start", "model": "claude-sonnet",
                     "prompt_tokens": 500, "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e6", "source": "/autobench",
            "type": "autobench.improver.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:41.5Z",
            "data": {"status": "complete", "model": "claude-sonnet",
                     "prompt_tokens": 500, "completion_tokens": 200,
                     "delta_summary": "tweak", "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e7", "source": "/autobench",
            "type": "autobench.iteration.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:33:52.851Z",
            "data": {"iteration": 0, "harness_version": "v0", "status": "complete",
                     "aggregate_score": 0.50, "verdict_counts": {"OK": 2},
                     "improvement_delta": {"improvement_summary": "ok"},
                     "session_id": sid},
        },
        {
            "specversion": "1.0", "id": "e8", "source": "/autobench",
            "type": "autobench.iteration.v1",
            "datacontenttype": "application/json",
            "time": "2026-05-16T05:34:00Z",
            "data": {"iteration": 1, "harness_version": "v1", "status": "complete",
                     "aggregate_score": 0.75, "verdict_counts": {"OK": 2},
                     "improvement_delta": {"improvement_summary": "better"},
                     "session_id": sid},
        },
    ]


@pytest.fixture
def sample_jsonl(tmp_path, sample_events) -> Path:
    """Write the sample events to a temp jsonl file."""
    p = tmp_path / "debug.jsonl"
    with open(p, "w") as fh:
        for evt in sample_events:
            fh.write(json.dumps(evt) + "\n")
    return p
