"""tests/test_recorder_shutdown.py — graceful SIGTERM handling for the recorder.

The systemd unit restarts the recorder with SIGTERM (TimeoutStopSec=15). Python's
default disposition for SIGTERM kills the process inside the kernel, so the
`finally` block in `main()` that flushes open runs never ran: every run still
open in the Segmenter, and every activity envelope buffered in
`Recorder._pending_events`, was lost on each restart. Those envelopes had
already been XACKed, so Redis would not redeliver them either.

The subprocess tests below send a REAL SIGTERM to a REAL Recorder driving the
REAL `_run_xreadgroup` loop (redis and `nervous publish` are stubbed in
`_sigterm_child.py`; nothing else is), and assert against the resulting SQLite
file. See `_sigterm_child.py` for exactly what is stood in for.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import recorder as recorder_mod
from segment import Segmenter
from store import SQLiteStore

_CHILD = Path(__file__).parent / "_sigterm_child.py"

# The child blocks in 500ms slices, so a graceful stop should land well inside
# this. It is an upper bound on "bounded", not a performance target.
STOP_DEADLINE_S = 10.0
READY_TIMEOUT_S = 30.0


def _read_db(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        runs = conn.execute(
            "SELECT run_id, run_key, run_key_kind, close_reason, event_count "
            "FROM runs ORDER BY run_key"
        ).fetchall()
        events = conn.execute(
            "SELECT run_id, seq, raw_json FROM run_events ORDER BY run_id, seq"
        ).fetchall()
        return {"runs": runs, "events": events}
    finally:
        conn.close()


class _Child:
    """Spawn `_sigterm_child.py`, wait for readiness, SIGTERM it, collect state."""

    def __init__(self, tmp: Path, mode: str = "fast", budget_s: float = 10.0):
        self.tmp = tmp
        self.db = tmp / "runs.db"
        self.ready = tmp / "ready.json"
        self.summary = tmp / "summary.json"
        self.mode = mode
        self.budget_s = budget_s
        self.proc = None
        self.stop_latency = None
        self.stderr = ""

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(_CHILD),
             "--db", str(self.db),
             "--ready-file", str(self.ready),
             "--summary-file", str(self.summary),
             "--mode", self.mode,
             "--budget-s", str(self.budget_s)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + READY_TIMEOUT_S
        while time.time() < deadline:
            if self.ready.exists():
                return
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise AssertionError(f"child exited before ready: rc={self.proc.returncode}\n{err}")
            time.sleep(0.05)
        self.proc.kill()
        raise AssertionError("child never became ready")

    def sigterm(self, times: int = 1):
        sent = time.time()
        for _ in range(times):
            self.proc.send_signal(signal.SIGTERM)
        try:
            _, self.stderr = self.proc.communicate(timeout=STOP_DEADLINE_S)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
            raise AssertionError(
                f"child did not exit within {STOP_DEADLINE_S}s of SIGTERM "
                f"(mode={self.mode}) — shutdown is not bounded"
            )
        self.stop_latency = time.time() - sent

    def summary_json(self) -> dict:
        return json.loads(self.summary.read_text())


class TestSigtermFlush(unittest.TestCase):
    """A real SIGTERM must persist what a kernel-default kill would have lost."""

    def test_sigterm_persists_buffered_runs_and_events_once(self):
        with tempfile.TemporaryDirectory() as td:
            child = _Child(Path(td))
            child.start()

            # Ack timing, grounded: every entry has been XACKed off the PEL, and
            # NOTHING is durable yet. This is the exact state a default-SIGTERM
            # kill used to discard with no possibility of Redis replay.
            self.assertEqual(json.loads(child.ready.read_text())["acked"], 6)
            pre = _read_db(child.db)
            self.assertEqual(pre["runs"], [], "runs must not be durable before close")
            self.assertEqual(pre["events"], [], "events must not be durable before close")

            child.sigterm()
            self.assertEqual(child.proc.returncode, 0,
                             f"unclean exit; stderr:\n{child.stderr}")
            self.assertLess(child.stop_latency, STOP_DEADLINE_S)

            summary = child.summary_json()
            self.assertEqual(summary["signals"], 1)
            self.assertEqual(summary["acked"], 6)

            post = _read_db(child.db)

            # Three open runs flushed: two worktree runs off one conversation
            # plus one session run. Run keys and segmentation are preserved.
            self.assertEqual(
                sorted(r[1] for r in post["runs"]),
                ["conv-a#wt-one", "conv-a#wt-two", "conv-b"],
            )
            kinds = {r[1]: r[2] for r in post["runs"]}
            self.assertEqual(kinds["conv-a#wt-one"], "worktree")
            self.assertEqual(kinds["conv-a#wt-two"], "worktree")
            self.assertEqual(kinds["conv-b"], "session")

            for run in post["runs"]:
                self.assertEqual(run[3], "recorder_shutdown")
                self.assertEqual(run[4], 2)

            # Every buffered envelope landed, exactly once, and only once —
            # `_pending_events` is popped as it drains, so the repeated
            # shutdown in the child cannot double-append.
            self.assertEqual(len(post["events"]), 6)
            seqs = {}
            for run_id, seq, _raw in post["events"]:
                seqs.setdefault(run_id, []).append(seq)
            self.assertEqual(sorted(seqs.values()), [[1, 2], [1, 2], [1, 2]])
            self.assertEqual(len({(r, s) for r, s, _ in post["events"]}), 6)

            self.assertEqual(summary["runs_closed"], 3)
            self.assertEqual(summary["runs_published"], 3)
            self.assertEqual(len(set(summary["published_run_ids"])), 3,
                             "each run must be published exactly once")

    def test_repeated_shutdown_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            child = _Child(Path(td))
            child.start()
            # Two SIGTERMs, the second racing the flush the first started.
            child.sigterm(times=2)
            self.assertEqual(child.proc.returncode, 0, child.stderr)

            summary = child.summary_json()
            self.assertGreaterEqual(summary["signals"], 1)
            self.assertTrue(summary["first_shutdown"], "first shutdown must do work")
            self.assertFalse(summary["second_shutdown"],
                             "second shutdown must be a no-op")
            self.assertEqual(summary["runs_closed"], 3,
                             "runs must be closed once, not once per call")

            post = _read_db(child.db)
            self.assertEqual(len(post["runs"]), 3)
            self.assertEqual(len(post["events"]), 6)

    def test_shutdown_budget_bounds_publish_not_persistence(self):
        """Publishing is dropped when the budget is spent; records are not."""
        with tempfile.TemporaryDirectory() as td:
            child = _Child(Path(td), mode="slow_publish", budget_s=0.4)
            child.start()
            child.sigterm()
            self.assertEqual(child.proc.returncode, 0, child.stderr)

            summary = child.summary_json()
            self.assertGreaterEqual(
                summary["publishes_skipped"], 1,
                "an exhausted budget must skip publishing rather than overrun",
            )
            self.assertEqual(summary["runs_published"], 0)

            # ...and every run and event is still durable.
            post = _read_db(child.db)
            self.assertEqual(len(post["runs"]), 3)
            self.assertEqual(len(post["events"]), 6)

    def test_one_failing_run_does_not_abort_the_flush(self):
        """A store failure mid-flush must not cost the runs behind it."""
        with tempfile.TemporaryDirectory() as td:
            child = _Child(Path(td), mode="flush_error")
            child.start()
            child.sigterm()
            self.assertEqual(child.proc.returncode, 0, child.stderr)

            summary = child.summary_json()
            self.assertEqual(summary["flush_errors"], 1)

            post = _read_db(child.db)
            self.assertEqual(len(post["runs"]), 2,
                             "the two runs after the failing one must persist")
            self.assertEqual(len(post["events"]), 4)


class TestShutdownUnit(unittest.TestCase):
    """In-process checks that do not need a subprocess."""

    def _recorder(self, td: Path, **over) -> "recorder_mod.Recorder":
        cfg = {
            "idle_timeout_s": 900.0,
            "db_path": td / "runs.db",
            "shutdown_budget_s": 10.0,
        }
        cfg.update(over)
        return recorder_mod.Recorder(cfg)

    def test_install_shutdown_handlers_replaces_default_disposition(self):
        previous = signal.getsignal(signal.SIGTERM)
        try:
            self.assertEqual(previous, signal.SIG_DFL,
                             "test precondition: SIGTERM starts at the default")
            flag = recorder_mod.install_shutdown_handlers()
            # Bound methods compare equal but are not identical objects.
            self.assertEqual(signal.getsignal(signal.SIGTERM), flag.request)
            self.assertFalse(flag.is_set())
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(flag.is_set())
            self.assertEqual(flag.count, 1)
            self.assertEqual(flag.signum, signal.SIGTERM)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_shutdown_flag_latches_across_repeats(self):
        flag = recorder_mod.ShutdownSignal()
        flag.request(signal.SIGTERM, None)
        flag.request(signal.SIGTERM, None)
        self.assertTrue(flag.is_set())
        self.assertEqual(flag.count, 2)

    def test_sleep_interruptible_returns_early_on_shutdown(self):
        flag = recorder_mod.ShutdownSignal()
        flag.request()
        started = time.time()
        recorder_mod._sleep_interruptible(5.0, flag)
        self.assertLess(time.time() - started, 1.0)

    def test_sleep_interruptible_sleeps_when_not_shutting_down(self):
        started = time.time()
        recorder_mod._sleep_interruptible(0.3, recorder_mod.ShutdownSignal())
        self.assertGreaterEqual(time.time() - started, 0.25)

    def test_empty_run_key_events_are_not_buffered(self):
        """An event the segmenter refuses must not accumulate un-flushable."""
        with tempfile.TemporaryDirectory() as td:
            rec = self._recorder(Path(td))
            try:
                raw = json.dumps({
                    "type": recorder_mod.ACTIVITY_TYPE,
                    "data": {"project": "p", "event": "tool_call"},
                })
                rec._ingest_activity(raw, "1-0")
                self.assertEqual(rec._pending_events, {})
                self.assertEqual(rec.segmenter.open_run_count, 0)
            finally:
                rec.shutdown()

    def test_shutdown_returns_false_when_already_shut_down(self):
        with tempfile.TemporaryDirectory() as td:
            rec = self._recorder(Path(td))
            self.assertTrue(rec.shutdown())
            self.assertFalse(rec.shutdown())

    def test_publish_budget_none_once_deadline_passes(self):
        with tempfile.TemporaryDirectory() as td:
            rec = self._recorder(Path(td))
            try:
                self.assertEqual(rec._publish_budget(),
                                 recorder_mod.DEFAULT_PUBLISH_TIMEOUT_S)
                rec._shutdown_deadline = time.time() + 100.0
                self.assertLessEqual(rec._publish_budget(),
                                     recorder_mod.DEFAULT_PUBLISH_TIMEOUT_S)
                rec._shutdown_deadline = time.time() - 1.0
                self.assertIsNone(rec._publish_budget())
            finally:
                rec.shutdown()


class TestReadLoopStopsOnFlag(unittest.TestCase):
    """`_run_xreadgroup` must poll the latch, including while redis is down."""

    class _Boom:
        def __init__(self, exc):
            self.exc = exc
            self.calls = 0

        def xreadgroup(self, **kw):
            self.calls += 1
            raise self.exc

        def xack(self, *a):
            return 1

    def test_loop_exits_when_flag_set_before_first_read(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = {
                "idle_timeout_s": 900.0, "db_path": Path(td) / "runs.db",
                "stream_read_count": 10, "stream_block_ms": 100,
                "metrics_interval_s": 60.0, "tick_interval_s": 60.0,
            }
            rec = recorder_mod.Recorder(cfg)
            try:
                flag = recorder_mod.ShutdownSignal()
                flag.request(signal.SIGTERM, None)
                fake = self._Boom(RuntimeError("must not be called"))
                recorder_mod._run_xreadgroup(fake, rec, cfg, shutdown=flag)
                self.assertEqual(fake.calls, 0)
            finally:
                rec.shutdown()

    def test_connection_error_backoff_does_not_outlast_shutdown(self):
        """A redis outage during a restart must still stop inside the budget."""
        import redis as _redis
        with tempfile.TemporaryDirectory() as td:
            cfg = {
                "idle_timeout_s": 900.0, "db_path": Path(td) / "runs.db",
                "stream_read_count": 10, "stream_block_ms": 100,
                "metrics_interval_s": 60.0, "tick_interval_s": 60.0,
            }
            rec = recorder_mod.Recorder(cfg)
            try:
                flag = recorder_mod.ShutdownSignal()
                fake = self._Boom(_redis.ConnectionError("down"))

                # Set the latch from inside the first failed read, i.e. exactly
                # when the loop is about to enter its 5s backoff.
                original = fake.xreadgroup

                def _raise_then_signal(**kw):
                    flag.request(signal.SIGTERM, None)
                    return original(**kw)
                fake.xreadgroup = _raise_then_signal

                started = time.time()
                recorder_mod._run_xreadgroup(fake, rec, cfg, shutdown=flag)
                elapsed = time.time() - started
                self.assertEqual(fake.calls, 1)
                self.assertLess(elapsed, 2.0,
                                "the 5s reconnect backoff must yield to shutdown")
            finally:
                rec.shutdown()


class TestSegmenterShutdownContract(unittest.TestCase):
    """The properties Recorder.shutdown relies on, asserted directly."""

    def test_segmenter_shutdown_closes_open_runs_once(self):
        closed = []
        seg = Segmenter(idle_timeout_s=900.0, on_run_closed=closed.append)
        seg.ingest({"conversation_id": "c1", "event": "tool_call",
                    "ts": "2026-09-06T02:00:00Z"}, now=time.time())
        self.assertEqual(seg.open_run_count, 1)
        seg.shutdown()
        seg.shutdown()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["close_reason"], "recorder_shutdown")
        self.assertEqual(seg.open_run_count, 0)

    def test_save_run_is_idempotent_on_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            store = SQLiteStore(Path(td) / "runs.db")
            try:
                closed = []
                seg = Segmenter(idle_timeout_s=900.0, on_run_closed=closed.append)
                seg.ingest({"conversation_id": "c1", "event": "tool_call",
                            "ts": "2026-09-06T02:00:00Z"}, now=time.time())
                seg.shutdown()
                store.save_run(closed[0])
                store.save_run(closed[0])
                self.assertEqual(store.run_count(), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
