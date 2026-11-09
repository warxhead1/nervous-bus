"""tests/test_recorder_ended.py — nervous-bus#94 item 1: no phantom `ended` runs.

A dispatched subagent's terminal `ended` is published with the PARENT session's
ids and no worktree, so it used to key on the bare conversation id: it minted a
one-event run when none was open and closed the parent session's own run when
one was. These tests drive the real Recorder (journal, fold, close_run) with
batches shaped like the live traffic and check the SQLite end state, including
the write-ahead journal: an event the segmenter will not fold must never be
journaled, or no run close could ever drain it.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REC_DIR = Path(__file__).parent.parent
if str(_REC_DIR) not in sys.path:
    sys.path.insert(0, str(_REC_DIR))

import recorder as recorder_mod  # noqa: E402

PARENT = "parent-session-ulid"
SUB = "a1b2c3d4e5f60718"


def _env(data: dict) -> str:
    return json.dumps({"type": recorder_mod.ACTIVITY_TYPE, "data": data})


def _parent_call(ts="2026-11-06T11:59:00Z") -> str:
    return _env({
        "agent_kind": "host_claude_code", "event": "tool_call", "tool_name": "Bash",
        "conversation_id": PARENT, "session_id": PARENT, "agent_id": PARENT,
        "project": "myproject", "ts": ts, "cwd": "/home/eric/projects/myproject",
    })


def _sub_call(ts="2026-11-06T12:00:00Z", sub=SUB) -> str:
    return _env({
        "agent_kind": "host_subagent", "event": "tool_call", "tool_name": "Bash",
        "conversation_id": PARENT, "session_id": PARENT, "agent_id": PARENT,
        "project": "myproject", "ts": ts,
        "cwd": f"/home/eric/data2/worktrees/myproject/agent-{sub}",
        "worktree": f"agent-{sub}", "subagent_id": sub,
        "assignment_id": "toolu_d1", "parent_tool_use_id": "toolu_d1",
    })


def _sub_end(ts="2026-11-06T12:05:00Z", sub=SUB) -> str:
    return _env({
        "agent_kind": "host_subagent", "event": "ended", "status": "completed",
        "conversation_id": PARENT, "session_id": PARENT, "agent_id": PARENT,
        "project": "myproject", "ts": ts, "subagent_id": sub,
        "assignment_id": "toolu_d1", "parent_tool_use_id": "toolu_d1",
    })


def _batch(*raws, start=0):
    return [(f"1762430000000-{start + i}", raw) for i, raw in enumerate(raws)]


class _Base(unittest.TestCase):
    def setUp(self):
        p = patch.object(recorder_mod, "_publish_run", lambda payload, timeout=0: True)
        p.start()
        self.addCleanup(p.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.cfg = {
            "idle_timeout_s": 900.0,
            "db_path": Path(td.name) / "runs.db",
            "shutdown_budget_s": 5.0,
        }
        self.rec = recorder_mod.Recorder(self.cfg)
        self.addCleanup(self.rec.store.close)

    def runs(self):
        return sorted(self.rec.store.recent_runs(50), key=lambda r: r["started"])


class TestSubagentEndInTheJournaledPath(_Base):
    def test_end_in_the_same_batch_closes_the_subagent_run_only(self):
        self.rec._ingest_batch(_batch(_sub_call("2026-11-06T12:00:00Z"),
                                      _sub_call("2026-11-06T12:00:01Z"), _sub_end()))
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["close_reason"], runs[0]["event_count"]), ("ended", 3))
        self.assertEqual(runs[0]["run_key_kind"], "worktree")
        self.assertEqual(self.rec.store.event_count(), 3)
        self.assertEqual(self.rec.store.pending_event_count(), 0)
        self.assertEqual(self.rec.segmenter.open_run_count, 0)

    def test_end_in_a_later_batch_drains_the_journal_the_same_way(self):
        self.rec._ingest_batch(_batch(_sub_call(), _sub_call("2026-11-06T12:00:01Z")))
        self.assertEqual(self.rec.store.pending_event_count(), 2)
        self.rec._ingest_batch(_batch(_sub_end(), start=2))
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.rec.store.event_count(), 3)
        self.assertEqual(self.rec.store.pending_event_count(), 0)

    def test_end_with_no_run_is_neither_journaled_nor_folded(self):
        self.rec._ingest_batch(_batch(_sub_end()))
        self.assertEqual(self.rec.store.pending_event_count(), 0)
        self.assertEqual(self.rec.store.run_count(), 0)
        self.assertEqual(self.rec.segmenter.open_run_count, 0)
        self.assertEqual(self.rec._events_absorbed, 1)
        self.assertEqual(self.rec._events_ingested, 0)

    def test_end_leaves_the_parent_sessions_run_and_journal_alone(self):
        self.rec._ingest_batch(_batch(_parent_call(), _sub_call(), _sub_end()))
        self.assertEqual(self.rec.segmenter.open_run_count, 1)
        self.assertEqual(self.rec.store.pending_event_count(), 1)
        self.rec.shutdown()
        con = recorder_mod.SQLiteStore(self.cfg["db_path"])
        try:
            by_kind = {r["run_key_kind"]: r for r in con.recent_runs(10)}
        finally:
            con.close()
        self.assertEqual(by_kind["worktree"]["close_reason"], "ended")
        self.assertEqual(by_kind["session"]["close_reason"], "recorder_shutdown")
        self.assertEqual(by_kind["session"]["event_count"], 1)

    def test_second_end_for_one_dispatch_is_not_journaled(self):
        self.rec._ingest_batch(_batch(_sub_call(), _sub_end(), _sub_end("2026-11-06T12:05:01Z")))
        self.assertEqual(self.rec.store.run_count(), 1)
        self.assertEqual(self.rec.store.pending_event_count(), 0)
        self.assertEqual(self.rec._events_absorbed, 1)


class TestSubagentEndAcrossARestart(_Base):
    def test_recovered_run_is_closed_by_an_end_arriving_after_the_restart(self):
        self.rec._ingest_batch(_batch(_sub_call(), _sub_call("2026-11-06T12:00:01Z")))
        self.rec.store.close()  # the process dies: nothing is flushed or closed

        reborn = recorder_mod.Recorder(self.cfg)
        self.addCleanup(reborn.store.close)
        self.assertEqual(reborn.segmenter.open_run_count, 1)
        reborn._ingest_batch(_batch(_sub_end(), start=2))
        runs = reborn.store.recent_runs(10)
        self.assertEqual([(r["close_reason"], r["event_count"]) for r in runs], [("ended", 3)])
        self.assertEqual(reborn.store.pending_event_count(), 0)

    def test_journal_row_from_before_the_fix_does_not_mint_a_run_on_recovery(self):
        legacy_end = _env(json.loads(_sub_end())["data"])
        self.rec.store.journal_events(
            [(PARENT, "legacy-1", "2026-11-06T12:05:00Z", recorder_mod.ACTIVITY_TYPE, legacy_end)]
        )
        self.rec.store.close()

        reborn = recorder_mod.Recorder(self.cfg)
        self.addCleanup(reborn.store.close)
        self.assertEqual(reborn.segmenter.open_run_count, 0)
        self.assertEqual(reborn._events_absorbed, 1)
        reborn.shutdown()
        check = recorder_mod.SQLiteStore(self.cfg["db_path"])
        try:
            self.assertEqual(check.run_count(), 0)
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
