"""tests/test_recorder_once.py — nervous-bus#23: `--once` drains and exits.

Redis reads `XREADGROUP ... BLOCK 0` as "wait forever", not "do not wait", so
the old `--once` path hung on a quiet stream, could not be interrupted by the
shutdown latch, and (when it did return) read a single batch. The stand-in
below models exactly those semantics: a `block=0` read raises instead of
hanging the test run, a read with any BLOCK would wait, an id of `0` serves
this consumer's own pending entries, and `>` serves new ones. No fakeredis is
available here, so only the primitives the loop calls are stood in for.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REC_DIR = Path(__file__).parent.parent
if str(_REC_DIR) not in sys.path:
    sys.path.insert(0, str(_REC_DIR))

import recorder as recorder_mod  # noqa: E402


class _WouldBlockForever(Exception):
    """What `XREADGROUP ... BLOCK 0` is: a read that never returns."""


def _envelope(conv: str, seq: int) -> str:
    data = {
        "conversation_id": conv, "session_id": conv, "agent_id": conv,
        "project": "nervous-bus", "agent_kind": "host_claude_code",
        "event": "tool_call", "tool_name": "Bash",
        "ts": f"2026-09-06T02:{(seq // 60) % 60:02d}:{seq % 60:02d}Z",
        "cwd": "/home/eric/projects/nervous-bus",
    }
    return json.dumps({"type": recorder_mod.ACTIVITY_TYPE, "data": data})


def _entries(n: int, convs: int = 3, prefix: str = "1757116800000", start: int = 0):
    return [
        (f"{prefix}-{i}", {"_raw": _envelope(f"conv-{i % convs}", i)})
        for i in range(start, start + n)
    ]


class _StreamRedis:
    def __init__(self, new=(), own_pending=(), endless_batch: int = 0, on_new_read=None):
        self.new = list(new)
        self.own_pending = list(own_pending)
        self.endless_batch = endless_batch
        self.on_new_read = on_new_read
        self.acked: list[str] = []
        self.new_reads = 0
        self.blocking_reads = 0
        self._minted = 0

    def xreadgroup(self, groupname, consumername, streams, count, block):
        if block == 0:
            raise _WouldBlockForever()
        if block is not None:
            self.blocking_reads += 1
        (read_id,) = streams.values()
        if read_id == "0":
            acked = set(self.acked)
            return [(recorder_mod.STREAM_NAME,
                     [e for e in self.own_pending if e[0] not in acked][:count])]
        self.new_reads += 1
        if self.on_new_read is not None:
            self.on_new_read(self)
        if self.endless_batch:
            batch = _entries(self.endless_batch, prefix="1757200000000", start=self._minted)
            self._minted += self.endless_batch
        else:
            batch, self.new = self.new[:count], self.new[count:]
        self.own_pending.extend(batch)
        return [(recorder_mod.STREAM_NAME, batch)] if batch else []

    def xack(self, stream, group, stream_id):
        self.acked.append(stream_id)
        return 1

    # main() startup primitives: nothing stranded, nothing to prune.
    def ping(self):
        return True

    def xgroup_create(self, *a, **kw):
        return True

    def xautoclaim(self, *a, **kw):
        return ("0-0", [], [])

    def xinfo_consumers(self, *a, **kw):
        return []

    def xgroup_delconsumer(self, *a, **kw):
        return 0


def _cfg(tmp: Path) -> dict:
    return {
        "idle_timeout_s": 900.0,
        "db_path": tmp / "runs.db",
        "shutdown_budget_s": 5.0,
        "stream_read_count": 200,
        "stream_block_ms": 50,
        "metrics_interval_s": 3600.0,
        "tick_interval_s": 3600.0,
    }


class _OnceBase(unittest.TestCase):
    def setUp(self):
        p = patch.object(recorder_mod, "_publish_run", lambda payload, timeout=0: True)
        p.start()
        self.addCleanup(p.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(td.name)
        self.cfg = _cfg(self.tmp)

    def _runs(self):
        con = sqlite3.connect(f"file:{self.cfg['db_path']}?mode=ro", uri=True)
        try:
            return con.execute("SELECT run_key, event_count FROM runs").fetchall()
        finally:
            con.close()


class TestOnceDrain(_OnceBase):
    def test_drains_every_batch_without_blocking_then_returns(self):
        entries = _entries(450)
        fake = _StreamRedis(new=entries)
        rec = recorder_mod.Recorder(self.cfg)
        try:
            clean = recorder_mod._run_xreadgroup(fake, rec, self.cfg, once=True)
            self.assertTrue(clean)
            self.assertEqual(set(fake.acked), {sid for sid, _ in entries})
            self.assertEqual(rec._events_ingested, 450)
            self.assertEqual(fake.blocking_reads, 0)
        finally:
            rec.shutdown()
        self.assertEqual(sum(n for _k, n in self._runs()), 450)

    def test_returns_on_an_empty_stream(self):
        fake = _StreamRedis()
        rec = recorder_mod.Recorder(self.cfg)
        try:
            self.assertTrue(recorder_mod._run_xreadgroup(fake, rec, self.cfg, once=True))
            self.assertEqual(fake.acked, [])
        finally:
            rec.shutdown()

    def test_redelivers_this_consumers_own_pending_entries_first(self):
        stuck = _entries(3, convs=1, prefix="1757000000000")
        fresh = _entries(2, convs=1, prefix="1757116800000")
        fake = _StreamRedis(new=fresh, own_pending=stuck)
        rec = recorder_mod.Recorder(self.cfg)
        try:
            recorder_mod._run_xreadgroup(fake, rec, self.cfg, once=True)
            self.assertEqual(set(fake.acked), {sid for sid, _ in stuck + fresh})
            self.assertEqual(rec._events_ingested, 5)
        finally:
            rec.shutdown()


class TestOnceStops(_OnceBase):
    def test_sigterm_stops_a_drain_of_an_endless_stream(self):
        flag = recorder_mod.ShutdownSignal()

        def _term_on_third_batch(fake):
            if fake.new_reads == 3:
                flag.request(15)

        fake = _StreamRedis(endless_batch=5, on_new_read=_term_on_third_batch)
        rec = recorder_mod.Recorder(self.cfg)
        started = time.time()
        try:
            recorder_mod._run_xreadgroup(fake, rec, self.cfg, once=True, shutdown=flag)
        finally:
            rec.shutdown()
        self.assertLess(time.time() - started, 5.0)
        self.assertEqual(fake.new_reads, 3)
        # Everything delivered before the stop was journaled and acked.
        self.assertEqual(len(fake.acked), 15)
        self.assertEqual(sum(n for _k, n in self._runs()), 15)

    def test_a_failed_batch_aborts_the_drain_and_is_not_acked(self):
        fake = _StreamRedis(new=_entries(10))
        rec = recorder_mod.Recorder(self.cfg)
        try:
            with patch.object(rec, "_ingest_batch", side_effect=RuntimeError("journal write failed")):
                clean = recorder_mod._run_xreadgroup(fake, rec, self.cfg, once=True)
            self.assertFalse(clean)
            self.assertEqual(fake.new_reads, 1)
            self.assertEqual(fake.acked, [])
        finally:
            rec.shutdown()


class TestMainOnce(_OnceBase):
    def _main(self, fake) -> int:
        argv = ["recorder.py", "--once", "--config", str(self.tmp / "absent.toml"),
                "--db-path", str(self.cfg["db_path"])]
        with patch.object(sys, "argv", argv), \
             patch.object(recorder_mod.redis.Redis, "from_url", return_value=fake), \
             patch.object(recorder_mod, "install_shutdown_handlers",
                          return_value=recorder_mod.ShutdownSignal()):
            return recorder_mod.main()

    def test_main_once_persists_what_it_drained_and_exits_zero(self):
        entries = _entries(30, convs=2)
        fake = _StreamRedis(new=entries)
        self.assertEqual(self._main(fake), 0)
        self.assertEqual(set(fake.acked), {sid for sid, _ in entries})
        self.assertEqual(sorted(n for _k, n in self._runs()), [15, 15])

    def test_main_once_exits_nonzero_when_redis_drops_mid_drain(self):
        class _Dropping(_StreamRedis):
            def xreadgroup(self, **kw):
                raise recorder_mod.redis.ConnectionError("connection reset")

        started = time.time()
        self.assertEqual(self._main(_Dropping()), 1)
        self.assertLess(time.time() - started, 3.0)


if __name__ == "__main__":
    unittest.main()
