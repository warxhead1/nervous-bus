"""tests/test_backfill_dispatch_lineage.py -- dry-run default and apply mode of the
dispatch-lineage back-fill (issue #119). Synthetic store only."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import backfill_dispatch_lineage as bf


def _db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE run_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
                 "seq INTEGER, event_ts TEXT, event_type TEXT, raw_json TEXT)")
    rows = [
        ("2026-09-01T00:00:00Z", {"tool_name": "Agent", "tool_use_id": "t0",
                                  "tool_response_summary": '{"agentId":"old"}'}),
        ("2026-11-01T00:00:00Z", {"tool_name": "Agent", "tool_use_id": "t1",
                                  "tool_response_summary": '{"bytes":5,"is_error":false}'}),
        ("2026-11-01T00:00:01Z", {"tool_name": "Bash", "tool_use_id": "t9",
                                  "parent_tool_use_id": "t1", "subagent_id": "abc123"}),
        ("2026-11-02T00:00:00Z", {"tool_name": "Agent", "tool_use_id": "t2",
                                  "tool_response_summary": '{"bytes":5}'}),
    ]
    for ts, data in rows:
        conn.execute("INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) "
                     "VALUES ('r',1,?,?,?)", (ts, "activity", json.dumps({"data": data})))
    conn.commit()
    conn.close()


def _responses(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [json.loads(r[0])["data"].get("tool_response_summary", "")
                for r in conn.execute("SELECT raw_json FROM run_events WHERE id IN (2,4)")]
    finally:
        conn.close()


def test_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    db = tmp_path / "runs.db"
    _db(db)
    before = _responses(db)
    assert bf.main(["--db", str(db), "--since", "2026-10-03"]) == 0
    out = capsys.readouterr().out
    assert "2026-11: total=2 summary=0 envelope=1 transcript=0 unresolved=1" in out
    assert "2026-09" not in out and "dry run" in out
    assert _responses(db) == before


def test_apply_writes_agent_id_only_where_resolved(tmp_path):
    db = tmp_path / "runs.db"
    _db(db)
    assert bf.main(["--db", str(db), "--since", "2026-10-03", "--apply"]) == 0
    resolved, unresolved = _responses(db)
    assert json.loads(resolved)["agentId"] == "abc123"
    assert "agentId" not in unresolved
