"""live_check: transcript detectors on one session's runs, each hit printed once."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import live_check
from detectors.base import ensure_detector_schema
from detectors.foreground_wait import ForegroundWaitDetector
from transcript_steps import parse_transcript

FIX = Path(__file__).parent / "fixtures" / "transcripts"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, session_id TEXT,
                           started TEXT, ended TEXT, event_count INTEGER);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    ensure_detector_schema(conn)
    for rid, sess, name in (("RW", "lead-1", "start_grid_waits"), ("RX", "other", "start_grid_waits"),
                            ("RN", "lead-1", "podbot_sweep")):
        fx = FIX / f"{name}.jsonl"
        st = parse_transcript(fx)
        conn.execute("INSERT INTO runs VALUES (?, 'demo', ?, ?, ?, ?)",
                     (rid, sess, _iso(st[0].t0), _iso(st[-1].t0), len(st)))
        conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES (?, 1, ?, ?)",
                     (rid, _iso(st[0].t0), json.dumps({"data": {"transcript_path": str(fx)}})))
    conn.commit()
    conn.close()
    return path


def test_scoped_to_the_given_runs_and_deduped(tmp_path):
    conn = sqlite3.connect(_db(tmp_path / "runs.db"))
    seen: set[str] = set()
    hits = live_check.live_hits(conn, ["RW", "RN"], seen, detectors=(ForegroundWaitDetector,), roots=[])
    assert [(h["run_id"], h["detector"]) for h in hits] == [("RW", "foreground_wait")]
    assert live_check.live_hits(conn, ["RW", "RN"], seen, detectors=(ForegroundWaitDetector,),
                                roots=[]) == []
    assert conn.execute("SELECT COUNT(*) FROM detector_hits").fetchone()[0] == 0


def test_session_selects_its_runs_inside_the_window(tmp_path):
    conn = sqlite3.connect(_db(tmp_path / "runs.db"))
    assert live_check.session_runs(conn, "lead-1", since_hours=24 * 365 * 30) == ["RN", "RW"]  # by start
    assert live_check.session_runs(conn, "lead-1", since_hours=1) == []


def test_text_output_groups_runs_per_signature():
    hits = [{"run_id": r, "project": "p", "detector": "foreground_wait", "signature": "p:fw",
             "remediation": None} for r in ("A", "B", "C")]
    assert live_check.format_hits(hits) == ["[reflex] foreground_wait on run C and 2 more run(s) (p): p:fw"]


def test_cli_prints_a_hit_once_across_calls(tmp_path, capsys, monkeypatch):
    db = _db(tmp_path / "runs.db")
    monkeypatch.setattr(live_check, "LIVE_DETECTORS", (ForegroundWaitDetector,))
    monkeypatch.setattr(ForegroundWaitDetector, "transcript_roots", [])
    argv = ["--db", str(db), "--state", str(tmp_path / "seen.json"), "--run-id", "RW"]
    assert live_check.main(argv) == 0
    first = capsys.readouterr().out
    assert first.startswith("[reflex] foreground_wait on run RW (demo):")
    assert live_check.main(argv) == 0
    assert capsys.readouterr().out == ""
