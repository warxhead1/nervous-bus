"""unverified_completion reads the transcript when it resolves: the full Bash
command tells `python3 -m pytest` from `python3 script.py`, which the verb-only
bus summary cannot."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ENGINE))

from detectors.base import ensure_detector_schema
from detectors.unverified_completion import (
    UnverifiedCompletionDetector,
    transcript_verdict,
)
from detectors.verification import build_verifier
from transcript_steps import parse_transcript


def _line(t, role, block):
    return json.dumps({"type": role, "timestamp": f"2026-11-20T10:00:{t:02d}Z",
                       "message": {"content": [block]}})


def _use(t, bid, name, inp):
    return [
        _line(t, "assistant", {"type": "tool_use", "id": bid, "name": name, "input": inp}),
        _line(t + 1, "user", {"type": "tool_result", "tool_use_id": bid, "content": "ok"}),
    ]


def _edits(start):
    out = []
    for i in range(3):
        out += _use(start + 2 * i, f"e{i}", "Edit", {"file_path": f"/r/src/mod{i}.py"})
    return out


def _detect(tmp_path, lines):
    tx = tmp_path / "t.jsonl"
    tx.write_text("\n".join(lines) + "\n")
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, started TEXT, ended TEXT,
                           close_reason TEXT);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    ensure_detector_schema(conn)
    conn.execute("INSERT INTO runs VALUES ('R1', 'demo', '2026-11-20T09:59:00Z', "
                 "'2026-11-20T10:30:00Z', 'idle_timeout')")
    conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES "
                 "('R1', 1, '2026-11-20T10:00:00Z', ?)",
                 (json.dumps({"data": {"transcript_path": str(tx)}}),))
    det = UnverifiedCompletionDetector(conn)
    det.transcript_roots = []
    return det.detect(conn), tx


def test_script_run_after_the_edits_is_not_a_verification(tmp_path):
    lines = _edits(0) + _use(6, "b0", "Bash", {"command": "python3 script.py --fast"})
    out, _ = _detect(tmp_path, lines)
    assert len(out) == 1 and out[0].extra["tier"] == "transcript"
    assert out[0].run_ids == ["R1"]


def test_pytest_after_the_last_edit_verifies(tmp_path):
    lines = _edits(0) + _use(6, "b0", "Bash", {"command": "python3 -m pytest -q tests"})
    out, _ = _detect(tmp_path, lines)
    assert out == []


def test_a_verification_before_the_last_edit_does_not_count(tmp_path):
    lines = _use(0, "v", "Bash", {"command": "cargo test"}) + _edits(2)
    _, tx = _detect(tmp_path, lines)
    n, verified = transcript_verdict(parse_transcript(tx), "demo", build_verifier())
    assert (n, verified) == (3, False)
