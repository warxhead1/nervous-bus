"""tests/test_pel_reclaim.py — nervous-bus#54: reclaim stranded PEL entries.

A crash strictly between a batch's XREADGROUP delivery and its
`journal_events` commit leaves that batch's stream ids parked in Redis's
per-consumer pending-entries list (PEL) forever: ordinary `XREADGROUP ... >`
reads only ever deliver NEW stream entries, never re-deliver a consumer's own
pending ones, and each recorder process uses a pid-suffixed consumer name so
a dead pid's PEL entries are never revisited by a future restart under that
same name. `_reclaim_stranded_pel` (recorder.py) closes this on every
startup via XAUTOCLAIM; `_cleanup_dead_consumers` then prunes XINFO
CONSUMERS rows for names with nothing left pending.

fakeredis is unavailable in this environment (`python3 -c "import
fakeredis"` raises ModuleNotFoundError), so `_FakeReclaimRedis` below stands
in for exactly the four primitives these two functions call — XAUTOCLAIM,
XACK, XINFO CONSUMERS, XGROUP DELCONSUMER — not a general Redis stream
emulation. `store.SQLiteStore` and `recorder.Recorder`/`_ingest_batch` are
real; only the network client is stood in for.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REC_DIR = Path(__file__).parent.parent
if str(_REC_DIR) not in sys.path:
    sys.path.insert(0, str(_REC_DIR))

import recorder as recorder_mod  # noqa: E402


def _envelope(conversation_id: str, seq: int) -> str:
    data = {
        "conversation_id": conversation_id,
        "session_id": conversation_id,
        "agent_id": conversation_id,
        "project": "nervous-bus",
        "agent_kind": "host_claude_code",
        "event": "tool_call",
        "tool_name": "Bash",
        "ts": f"2026-09-06T02:00:{seq:02d}Z",
        "cwd": "/home/eric/projects/nervous-bus",
    }
    return json.dumps({"type": recorder_mod.ACTIVITY_TYPE, "data": data})


class _FakeReclaimRedis:
    """Stub for the primitives `_reclaim_stranded_pel` /
    `_cleanup_dead_consumers` call. `pages` is popped one XAUTOCLAIM call at
    a time — each element is (next_cursor, claimed_entries, deleted_ids),
    matching redis-py's 3-tuple XAUTOCLAIM return shape.
    """

    def __init__(self, pages=None, consumers=None):
        self._pages = list(pages or [])
        self._consumers = list(consumers or [])
        self.xack_calls: list[str] = []
        self.xautoclaim_calls: list[tuple] = []
        self.delconsumer_calls: list[str] = []
        self.raise_on_ingest_stream_id: str | None = None

    def xautoclaim(self, name, groupname, consumername, min_idle_time,
                    start_id="0-0", count=None):
        self.xautoclaim_calls.append((start_id, min_idle_time, count))
        if not self._pages:
            return ("0-0", [], [])
        return self._pages.pop(0)

    def xack(self, name, groupname, stream_id):
        self.xack_calls.append(stream_id)
        return 1

    def xinfo_consumers(self, name, groupname):
        return self._consumers

    def xgroup_delconsumer(self, name, groupname, consumername):
        self.delconsumer_calls.append(consumername)
        return 0


class TestReclaimStrandedPel(unittest.TestCase):
    def _recorder(self, tmp: Path) -> "recorder_mod.Recorder":
        cfg = {
            "idle_timeout_s": 900.0,
            "db_path": tmp / "runs.db",
            "reclaim_min_idle_s": 600.0,
            "reclaim_dead_consumer_idle_s": 86400.0,
            "reclaim_batch_count": 100,
        }
        return recorder_mod.Recorder(cfg)

    def test_reclaim_ingests_journals_and_acks(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rec = self._recorder(tmp)
            try:
                entries = [
                    ("1700000000000-0", {"_raw": _envelope("conv-a", 1)}),
                    ("1700000000000-1", {"_raw": _envelope("conv-a", 2)}),
                ]
                fake = _FakeReclaimRedis(pages=[("0-0", entries, [])])
                cfg = rec.cfg
                reclaimed = recorder_mod._reclaim_stranded_pel(fake, rec, cfg)

                self.assertEqual(reclaimed, 2)
                # Both stream ids acked...
                self.assertEqual(
                    set(fake.xack_calls),
                    {"1700000000000-0", "1700000000000-1"},
                )
                # ...and journaled (durable) before that ack.
                self.assertEqual(rec.store.pending_event_count(), 2)
                self.assertEqual(rec.segmenter.open_run_count, 1)
            finally:
                rec.shutdown()

    def test_ack_happens_after_journal_commit_fail_journal_no_ack(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rec = self._recorder(tmp)
            try:
                entries = [("1700000000000-0", {"_raw": _envelope("conv-a", 1)})]
                fake = _FakeReclaimRedis(pages=[("0-0", entries, [])])

                def _boom(rows):
                    raise RuntimeError("journal write failed")

                rec.store.journal_events = _boom  # type: ignore[assignment]

                with self.assertRaises(RuntimeError):
                    recorder_mod._reclaim_stranded_pel(fake, rec, rec.cfg)

                # No ack must have happened — the batch's journal never
                # committed, so XACKing would durably lose it.
                self.assertEqual(fake.xack_calls, [])
            finally:
                rec.shutdown()

    def test_dedupe_skips_reingest_but_still_acks(self):
        """A stream_id already journaled (crash before ack, prior delivery)
        must not be re-ingested, but IS acked now."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rec = self._recorder(tmp)
            try:
                stream_id = "1700000000000-0"
                raw = _envelope("conv-a", 1)
                # Simulate: a prior delivery already journaled this exact
                # stream_id (crashed before XACK).
                rec.store.journal_events([("run-key-a", stream_id, "2026-09-06T02:00:01Z",
                                            recorder_mod.ACTIVITY_TYPE, raw)])
                self.assertEqual(rec.store.pending_event_count(), 1)

                fake = _FakeReclaimRedis(pages=[("0-0", [(stream_id, {"_raw": raw})], [])])
                reclaimed = recorder_mod._reclaim_stranded_pel(fake, rec, rec.cfg)

                self.assertEqual(reclaimed, 1)
                self.assertEqual(fake.xack_calls, [stream_id])
                # Still exactly 1 row — no duplicate insert.
                self.assertEqual(rec.store.pending_event_count(), 1)
            finally:
                rec.shutdown()

    def test_reclaim_loop_handles_multi_page_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rec = self._recorder(tmp)
            try:
                page1 = [("1700000000000-0", {"_raw": _envelope("conv-a", 1)})]
                page2 = [("1700000000000-1", {"_raw": _envelope("conv-b", 2)})]
                fake = _FakeReclaimRedis(pages=[
                    ("1700000000000-1", page1, []),  # non-zero cursor -> more pages
                    ("0-0", page2, []),               # terminal cursor
                ])
                reclaimed = recorder_mod._reclaim_stranded_pel(fake, rec, rec.cfg)

                self.assertEqual(reclaimed, 2)
                self.assertEqual(len(fake.xautoclaim_calls), 2)
                self.assertEqual(
                    set(fake.xack_calls),
                    {"1700000000000-0", "1700000000000-1"},
                )
            finally:
                rec.shutdown()

    def test_empty_first_page_stops_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rec = self._recorder(tmp)
            try:
                fake = _FakeReclaimRedis(pages=[("0-0", [], [])])
                reclaimed = recorder_mod._reclaim_stranded_pel(fake, rec, rec.cfg)
                self.assertEqual(reclaimed, 0)
                self.assertEqual(fake.xack_calls, [])
                self.assertEqual(len(fake.xautoclaim_calls), 1)
            finally:
                rec.shutdown()


class TestCleanupDeadConsumers(unittest.TestCase):
    def test_dead_idle_consumer_deleted_live_and_self_kept(self):
        consumers = [
            {"name": recorder_mod.CONSUMER_NAME, "pending": 0, "idle": 999999999},
            {"name": "reflex-recorder-1111", "pending": 0, "idle": 90000000},  # dead: >1d idle, 0 pending
            {"name": "reflex-recorder-2222", "pending": 3, "idle": 90000000},  # still has pending work
            {"name": "reflex-recorder-3333", "pending": 0, "idle": 1000},      # recently active
        ]
        fake = _FakeReclaimRedis(consumers=consumers)
        cfg = {"reclaim_dead_consumer_idle_s": 86400.0}
        deleted = recorder_mod._cleanup_dead_consumers(fake, cfg)

        self.assertEqual(deleted, 1)
        self.assertEqual(fake.delconsumer_calls, ["reflex-recorder-1111"])

    def test_never_deletes_self_even_if_reported_idle(self):
        consumers = [
            {"name": recorder_mod.CONSUMER_NAME, "pending": 0, "idle": 999999999},
        ]
        fake = _FakeReclaimRedis(consumers=consumers)
        deleted = recorder_mod._cleanup_dead_consumers(
            fake, {"reclaim_dead_consumer_idle_s": 86400.0}
        )
        self.assertEqual(deleted, 0)
        self.assertEqual(fake.delconsumer_calls, [])


if __name__ == "__main__":
    unittest.main()
