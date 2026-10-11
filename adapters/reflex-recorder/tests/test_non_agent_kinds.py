"""Non-LLM producers on bus.agent.activity.v1 never enter the run store.

tachyonac-engine publishes every cron/ingestor job tick as agent activity;
recorded as runs they swamped label.py and synthesis.py past their nightly
timeouts (2026-10-11).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REC_DIR = Path(__file__).parent.parent
_TESTS_DIR = Path(__file__).parent
for _p in (_REC_DIR, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import recorder as recorder_mod  # noqa: E402
from test_recorder_shutdown import _read_db  # noqa: E402


def _activity(conv: str, kind: str, event: str, ts: str) -> str:
    data = {
        "conversation_id": conv, "session_id": conv, "agent_id": conv,
        "project": "p", "agent_kind": kind,
        "event": event, "tool_name": "job", "ts": ts,
    }
    return json.dumps({"type": recorder_mod.ACTIVITY_TYPE, "data": data})


class TestNonAgentKinds(unittest.TestCase):
    def test_cron_and_ingestor_are_dropped_agents_are_kept(self):
        original_publish = recorder_mod._publish_run
        recorder_mod._publish_run = lambda payload, timeout=0: True
        self.addCleanup(setattr, recorder_mod, "_publish_run", original_publish)

        with tempfile.TemporaryDirectory() as td:
            cfg = {"idle_timeout_s": 900.0, "db_path": Path(td) / "runs.db",
                   "shutdown_budget_s": 10.0}
            rec = recorder_mod.Recorder(cfg)
            rec._ingest_batch([
                ("1-0", _activity("c1", "cron", "started", "2026-10-11T00:00:00Z")),
                ("1-1", _activity("c1", "cron", "ended", "2026-10-11T00:00:01Z")),
                ("1-2", _activity("i1", "ingestor", "ended", "2026-10-11T00:00:02Z")),
                ("1-3", _activity("a1", "host_claude_code", "tool_call", "2026-10-11T00:00:03Z")),
                ("1-4", _activity("a1", "host_claude_code", "ended", "2026-10-11T00:00:04Z")),
            ])
            rec.shutdown()
            db = _read_db(cfg["db_path"])
            self.assertEqual(len(db["runs"]), 1)
            self.assertEqual(db["runs"][0][4], 2)
            self.assertEqual(db["pending"], [])


if __name__ == "__main__":
    unittest.main()


class TestRetire(unittest.TestCase):
    def test_moves_non_agent_rows_to_archive_and_keeps_agents(self):
        import sqlite3
        from retire_non_agent_runs import retire
        from store import SQLiteStore

        with tempfile.TemporaryDirectory() as td:
            db, arch = Path(td) / "runs.db", Path(td) / "arch.db"
            SQLiteStore(db).close()
            conn = sqlite3.connect(db)
            for rid, kind in (("r1", "cron"), ("r2", "ingestor"), ("r3", "host_claude_code")):
                conn.execute(
                    "INSERT INTO runs (run_id, run_key, run_key_kind, project, agent_kind,"
                    " started, ended, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                    (rid, rid, "session", "p", kind, "t", "t", "t"))
                conn.execute("INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)"
                             " VALUES (?,1,'t','x','{}')", (rid,))
            conn.execute("INSERT INTO pending_events (run_key, stream_id, event_ts, event_type, raw_json)"
                         " VALUES ('k','1-0','t','x',?)", (json.dumps({"data": {"agent_kind": "cron"}}),))
            conn.commit()
            conn.close()

            moved = retire(db, arch, batch=1)
            self.assertEqual(moved, {"runs": 2, "run_events": 2, "detector_hits": 0, "pending_events": 1})
            self.assertEqual(retire(db, arch)["runs"], 0)

            live = sqlite3.connect(db)
            self.assertEqual([r[0] for r in live.execute("SELECT run_id FROM runs")], ["r3"])
            self.assertEqual(live.execute("SELECT COUNT(*) FROM run_events").fetchone()[0], 1)
            archived = sqlite3.connect(arch)
            self.assertEqual(sorted(r[0] for r in archived.execute("SELECT run_id FROM runs")), ["r1", "r2"])
