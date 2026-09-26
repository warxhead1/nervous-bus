"""tests/test_write_ahead.py — nervous-bus#51: write-ahead journal durability.

Covers what test_recorder_shutdown.py's SIGTERM suite structurally cannot:
the 2026-09-26 incident was a SIGKILL (systemd's TimeoutStopSec escalation),
which skips every `finally` block — there is no graceful path to exercise.
The fix moves the durability boundary to `store.journal_events`'s commit,
which happens unconditionally before the batch's XACK, so a SIGKILL is
survivable regardless of whether any signal handler ever runs.

(i)   SIGKILL mid-run, then a fresh Recorder against the same DB recovers
      every journaled event and eventually closes the same runs a graceful
      stop would have produced.
(ii)  store.close_run is one transaction: a failure partway through leaves
      neither a partial `runs` row nor drained `pending_events`.
(iii) Recovery is idempotent across repeated crashes.
(iv)  Shutdown of N runs x 500 events completes well under 1s.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REC_DIR = Path(__file__).parent.parent
_TESTS_DIR = Path(__file__).parent
for _p in (_REC_DIR, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import recorder as recorder_mod  # noqa: E402
from store import SQLiteStore  # noqa: E402

from test_recorder_shutdown import _Child, _read_db  # noqa: E402


class TestSigkillRecovery(unittest.TestCase):
    """A REAL SIGKILL, then a fresh process recovers from the journal."""

    def test_sigkill_then_restart_recovers_all_journaled_events(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            child = _Child(tmp)
            child.start()

            # All 6 events are acked (delivered) — with the fix, that also
            # means all 6 are already durable in pending_events, because
            # journal_events() commits before _run_xreadgroup acks the batch.
            self.assertEqual(json.loads(child.ready.read_text())["acked"], 6)
            pre = _read_db(child.db)
            self.assertEqual(pre["runs"], [])
            self.assertEqual(pre["events"], [])
            self.assertEqual(len(pre["pending"]), 6)

            # No handler, no finally: exactly what escalation to SIGKILL does.
            child.proc.send_signal(signal.SIGKILL)
            child.proc.wait(timeout=10)
            self.assertNotEqual(child.proc.returncode, 0,
                                 "a killed process must not report a clean exit")

            # Nothing closed by the kill itself.
            killed = _read_db(child.db)
            self.assertEqual(killed["runs"], [])
            self.assertEqual(killed["events"], [])
            self.assertEqual(len(killed["pending"]), 6)

            # Fresh recorder against the same DB: recovery re-folds the
            # journaled events into a live Segmenter before anything else.
            cfg = {
                "idle_timeout_s": 900.0,
                "db_path": child.db,
                "shutdown_budget_s": 5.0,
            }
            recovered = recorder_mod.Recorder(cfg)
            try:
                self.assertEqual(recovered.segmenter.open_run_count, 3,
                                  "conv-a#wt-one, conv-a#wt-two, conv-b must reopen")
                self.assertEqual(recovered._events_ingested, 6)
                # The journal itself is untouched by recovery — only
                # close_run() ever drains it.
                self.assertEqual(recovered.store.pending_event_count(), 6)
            finally:
                recovered.shutdown()

            post = _read_db(child.db)
            self.assertEqual(
                sorted(r[1] for r in post["runs"]),
                ["conv-a#wt-one", "conv-a#wt-two", "conv-b"],
            )
            for run in post["runs"]:
                self.assertEqual(run[3], "recorder_shutdown")
                self.assertEqual(run[4], 2, "event_count must reflect the recovered events")
            self.assertEqual(len(post["events"]), 6)
            self.assertEqual(post["pending"], [],
                              "close_run must drain the journal once the runs close")

    def test_recovery_is_idempotent_across_repeated_crashes(self):
        """Recovering twice (e.g. crash-during-recovery-window) must not
        duplicate events or reopen runs with inflated event counts."""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "runs.db"
            cfg = {"idle_timeout_s": 900.0, "db_path": db_path, "shutdown_budget_s": 5.0}

            # Seed the journal directly, as a crashed live process would have
            # left it (bypassing the redis/subprocess machinery entirely).
            store = SQLiteStore(db_path)
            store.journal_events([
                ("conv-x", "1-0", "2026-09-26T00:00:00Z", recorder_mod.ACTIVITY_TYPE,
                 json.dumps({"type": recorder_mod.ACTIVITY_TYPE,
                             "data": {"conversation_id": "conv-x", "session_id": "conv-x",
                                      "agent_id": "conv-x", "project": "p",
                                      "agent_kind": "host_claude_code", "event": "tool_call",
                                      "tool_name": "Bash", "ts": "2026-09-26T00:00:00Z"}})),
                ("conv-x", "1-1", "2026-09-26T00:00:01Z", recorder_mod.ACTIVITY_TYPE,
                 json.dumps({"type": recorder_mod.ACTIVITY_TYPE,
                             "data": {"conversation_id": "conv-x", "session_id": "conv-x",
                                      "agent_id": "conv-x", "project": "p",
                                      "agent_kind": "host_claude_code", "event": "tool_call",
                                      "tool_name": "Read", "ts": "2026-09-26T00:00:01Z"}})),
            ])
            store.close()

            first = recorder_mod.Recorder(cfg)
            self.assertEqual(first.segmenter.open_run_count, 1)
            self.assertEqual(first.store.pending_event_count(), 2)
            # Simulate a second crash before this recorder ever closes the
            # run: construct ANOTHER Recorder on the same DB without closing
            # the first (mirrors two restarts with no clean shutdown between
            # them).
            second = recorder_mod.Recorder(cfg)
            try:
                self.assertEqual(second.segmenter.open_run_count, 1,
                                  "must not double-open the run")
                self.assertEqual(second.store.pending_event_count(), 2,
                                  "recovery must not re-journal what it recovers")
                self.assertEqual(second._events_ingested, 2)
            finally:
                second.shutdown()
            first.store.close()

            post = _read_db(db_path)
            self.assertEqual(len(post["runs"]), 1)
            self.assertEqual(post["runs"][0][4], 2, "event_count must not double")
            self.assertEqual(len(post["events"]), 2)
            self.assertEqual(post["pending"], [])


class TestCloseRunAtomicity(unittest.TestCase):
    """store.close_run must be one transaction: all or nothing."""

    def test_failure_after_runs_upsert_leaves_journal_intact(self):
        with tempfile.TemporaryDirectory() as td:
            store = SQLiteStore(Path(td) / "runs.db")
            try:
                store.journal_events([
                    ("key-1", "1-0", "2026-09-26T00:00:00Z", "bus.agent.activity.v1", "{}"),
                    ("key-1", "1-1", "2026-09-26T00:00:01Z", "bus.agent.activity.v1", "{}"),
                ])
                self.assertEqual(store.pending_event_count(), 2)

                # sqlite3.Connection attributes are read-only, so intercept
                # via a thin proxy on the STORE's own `_conn` attribute
                # instead (that attribute is an ordinary mutable reference).
                real_conn = store._conn

                class _FailOnceExecutemany:
                    def __init__(self, real):
                        self._real = real
                        self.failed = False

                    def __getattr__(self, name):
                        return getattr(self._real, name)

                    def executemany(self, sql, params):
                        if (sql.strip().startswith("INSERT INTO run_events")
                                and not self.failed):
                            self.failed = True
                            raise sqlite3.OperationalError("simulated failure mid-transaction")
                        return self._real.executemany(sql, params)

                store._conn = _FailOnceExecutemany(real_conn)

                payload = {
                    "run_id": "run-1", "run_key": "key-1", "run_key_kind": "session",
                    "host_conversation_id": None, "project": "p", "agent_kind": "host_claude_code",
                    "session_id": None, "agent_id": None,
                    "started": "2026-09-26T00:00:00Z", "ended": "2026-09-26T00:00:01Z",
                    "close_reason": "ended", "continues_run_id": None,
                    "event_count": 2, "tool_histogram": {}, "worktree": None,
                    "worktree_slug": None, "git_branch": None, "bead_id": None,
                    "outcome": None, "labeled_at": None, "label_version": None,
                    "label_history": [], "features": {}, "schema_version": "1",
                }
                with self.assertRaises(sqlite3.OperationalError):
                    store.close_run(payload)

                store._conn = real_conn
                # Neither half of the transaction landed: no run row, and
                # the journal rows this attempt would have drained are still
                # there — a retry (e.g. the next process restart) can still
                # find and close them.
                self.assertEqual(store.run_count(), 0)
                self.assertEqual(store.pending_event_count(), 2)
            finally:
                store.close()

    def test_success_drains_journal_and_writes_run_in_one_shot(self):
        with tempfile.TemporaryDirectory() as td:
            store = SQLiteStore(Path(td) / "runs.db")
            try:
                store.journal_events([
                    ("key-2", "2-0", "2026-09-26T00:00:00Z", "bus.agent.activity.v1", "{}"),
                ])
                payload = {
                    "run_id": "run-2", "run_key": "key-2", "run_key_kind": "session",
                    "host_conversation_id": None, "project": "p", "agent_kind": "host_claude_code",
                    "session_id": None, "agent_id": None,
                    "started": "2026-09-26T00:00:00Z", "ended": "2026-09-26T00:00:01Z",
                    "close_reason": "ended", "continues_run_id": None,
                    "event_count": 1, "tool_histogram": {}, "worktree": None,
                    "worktree_slug": None, "git_branch": None, "bead_id": None,
                    "outcome": None, "labeled_at": None, "label_version": None,
                    "label_history": [], "features": {}, "schema_version": "1",
                }
                store.close_run(payload)
                self.assertEqual(store.run_count(), 1)
                self.assertEqual(store.event_count(), 1)
                self.assertEqual(store.pending_event_count(), 0)
            finally:
                store.close()


class TestShutdownScale(unittest.TestCase):
    """Shutdown of many runs/events must complete well under 1s."""

    def test_shutdown_500_events_across_many_runs_is_fast(self):
        # Stub `nervous publish` — a real subprocess per run (25 of them)
        # would swamp the 1s budget with process-spawn overhead unrelated to
        # what this test measures (SQLite persistence).
        original_publish = recorder_mod._publish_run
        recorder_mod._publish_run = lambda payload, timeout=0: True
        self.addCleanup(setattr, recorder_mod, "_publish_run", original_publish)

        with tempfile.TemporaryDirectory() as td:
            cfg = {
                "idle_timeout_s": 900.0,
                "db_path": Path(td) / "runs.db",
                "shutdown_budget_s": 10.0,
            }
            rec = recorder_mod.Recorder(cfg)
            n_runs = 25
            events_per_run = 20  # 25 * 20 = 500
            entries = []
            for r in range(n_runs):
                for e in range(events_per_run):
                    conv = f"conv-{r}"
                    raw = json.dumps({
                        "type": recorder_mod.ACTIVITY_TYPE,
                        "data": {
                            "conversation_id": conv, "session_id": conv, "agent_id": conv,
                            "project": "p", "agent_kind": "host_claude_code",
                            "event": "tool_call", "tool_name": "Bash",
                            "ts": f"2026-09-26T00:{r % 60:02d}:{e % 60:02d}Z",
                        },
                    })
                    entries.append((f"{r}-{e}", raw))
            rec._ingest_batch(entries)
            self.assertEqual(rec.segmenter.open_run_count, n_runs)
            self.assertEqual(rec.store.pending_event_count(), n_runs * events_per_run)

            db_path = rec.store.db_path
            started = time.time()
            rec.shutdown(budget_s=10.0)
            elapsed = time.time() - started
            self.assertLess(elapsed, 1.0,
                             f"shutdown of {n_runs} runs / {n_runs * events_per_run} "
                             f"events took {elapsed:.3f}s — expected well under 1s")

            # rec.store is closed by shutdown(); reopen to verify the drain.
            verify = SQLiteStore(db_path)
            try:
                self.assertEqual(verify.pending_event_count(), 0)
                self.assertEqual(verify.run_count(), n_runs)
                self.assertEqual(verify.event_count(), n_runs * events_per_run)
            finally:
                verify.close()


if __name__ == "__main__":
    unittest.main()
