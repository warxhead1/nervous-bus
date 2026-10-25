"""run_time + `reflex time`: model vs tool time per run, from transcripts."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_time  # noqa: E402
from adapter_api import CommandTaxonomy  # noqa: E402
from query import main  # noqa: E402
from transcript_steps import Step  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "transcripts"


def test_union_counts_parallel_calls_once():
    assert run_time._union([(0, 10), (5, 12), (20, 25)]) == 17
    assert run_time._union([]) == 0


def test_breakdown_splits_model_and_tool_time():
    steps = [
        Step(id="1", tool="Bash", input={"command": "cargo build --release"}, t0=0, t1=60),
        Step(id="2", tool="Read", input={"file_path": "/a"}, t0=30, t1=40),   # parallel
        Step(id="3", tool="Bash", input={"command": "cargo test"}, t0=100, t1=130),
    ]
    r = run_time.breakdown(steps, CommandTaxonomy(), top_k=1)
    assert (r["wall_s"], r["tool_s"], r["model_s"], r["calls"]) == (130, 90, 40, 3)
    assert r["by_class"]["build"]["seconds"] == 60
    assert r["top"][0]["what"] == "cargo build --release"


def test_known_answer_on_the_podbot_lane():
    r = run_time.transcript_time(FIX / "podbot_sweep.jsonl", CommandTaxonomy(), top_k=3)
    assert round(r["wall_s"] / 60, 1) == 193.7
    assert round(r["tool_s"] / 60, 1) == 146.1
    assert round(r["model_s"] / 60, 1) == 47.6
    assert len(r["top"]) == 3


def test_cli_time_on_a_transcript(tmp_path, capsys):
    db = tmp_path / "runs.db"
    sqlite3.connect(str(db)).close()
    rc = main(["--db", str(db), "time", "--transcript", str(FIX / "podbot_sweep.jsonl"),
               "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["agent"] == "podbot_sweep"
    assert rows[0]["calls"] == 284


def test_cli_time_through_the_store(tmp_path, capsys):
    db = tmp_path / "runs.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, started TEXT, ended TEXT,
                           event_count INTEGER);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER,
                                 event_ts TEXT, raw_json TEXT);
    """)
    fx = FIX / "rocks_off_line.jsonl"
    first = json.loads(fx.open().readline())["timestamp"]
    conn.execute("INSERT INTO runs VALUES ('R1', 'demo', ?, NULL, 5)", (first,))
    conn.execute("INSERT INTO runs VALUES ('R2', 'demo', ?, NULL, 5)", (first,))
    conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES ('R1', 1, ?, ?)",
                 (first, json.dumps({"data": {"transcript_path": str(fx)}})))
    conn.execute("INSERT INTO run_events (run_id, seq, event_ts, raw_json) VALUES ('R2', 1, ?, ?)",
                 (first, json.dumps({"data": {"transcript_path": "/nonexistent.jsonl"}})))
    conn.commit()
    conn.close()
    rc = main(["--db", str(db), "time", "--project", "demo", "--min-wall", "0", "--json"])
    assert rc == 0
    rows = {r["run_id"]: r for r in json.loads(capsys.readouterr().out)}
    assert round(rows["R1"]["wall_s"] / 60, 1) == 32.5
    assert rows["R2"]["transcript"] is None and rows["R2"]["calls"] == 0
