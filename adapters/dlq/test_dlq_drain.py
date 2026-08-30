#!/usr/bin/env python3
"""Tests for the DLQ consumer-group drain (adapters/dlq/dlq.py).

Exercises against a real local Redis/Valkey instance using a throwaway
stream+group namespace (nbus:test.dlq.*) so it never touches the live
nbus:bus.dead_letter stream or its dlq-drain consumer group. Skips cleanly
if no Redis is reachable on localhost:6379.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dlq  # noqa: E402

try:
    import redis
    _r = redis.Redis(decode_responses=True, socket_connect_timeout=1)
    _r.ping()
    REDIS_AVAILABLE = True
except Exception:
    REDIS_AVAILABLE = False

TEST_STREAM = "nbus:test.dlq.drain"
TEST_GROUP = "dlq-drain-test"


def _fake_dead_letter_fields(event_type: str, reason: str, excerpt: str) -> dict:
    envelope = {
        "specversion": "1.0",
        "id": f"test-{time.time_ns()}",
        "source": "/test",
        "type": "bus.dead_letter",
        "time": "2026-08-30T00:00:00Z",
        "datacontenttype": "application/json",
        "data": {
            "failure_reason": reason,
            "original_type": event_type,
            "original_payload_excerpt": excerpt,
        },
    }
    raw = json.dumps(envelope)
    return {
        "_raw": raw,
        "type": "bus.dead_letter",
        "source": "/test",
        "timestamp": "2026-08-30T00:00:00Z",
        "event_id": envelope["id"],
    }


@unittest.skipUnless(REDIS_AVAILABLE, "no local Redis/Valkey reachable")
class TestConsumerGroupDrain(unittest.TestCase):
    def setUp(self):
        self.r = redis.Redis(decode_responses=True)
        self.r.delete(TEST_STREAM)
        try:
            self.r.xgroup_destroy(TEST_STREAM, TEST_GROUP)
        except redis.ResponseError:
            pass
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dlq-test-"))
        self.db_path = self.tmpdir / "dlq.db"
        self.jsonl_dir = self.tmpdir / "jsonl"
        self.quarantine_path = self.tmpdir / "quarantine.jsonl"
        self.conn = dlq.open_db(self.db_path)
        self.tracker = dlq.SummaryTracker(self.jsonl_dir / "summary.json")

    def tearDown(self):
        self.conn.close()
        self.r.delete(TEST_STREAM)
        try:
            self.r.xgroup_destroy(TEST_STREAM, TEST_GROUP)
        except redis.ResponseError:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ensure_consumer_group_idempotent(self):
        dlq.ensure_consumer_group(self.r, TEST_STREAM, TEST_GROUP)
        # Second call must not raise (BUSYGROUP swallowed).
        dlq.ensure_consumer_group(self.r, TEST_STREAM, TEST_GROUP)
        groups = self.r.xinfo_groups(TEST_STREAM)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], TEST_GROUP)

    def test_group_created_at_zero_drains_existing_backlog(self):
        # Push entries BEFORE the group exists.
        self.r.xadd(TEST_STREAM, _fake_dead_letter_fields(
            "hearth.ember.insight.v1", "schema_violation", "too long excerpt"))
        dlq.ensure_consumer_group(self.r, TEST_STREAM, TEST_GROUP)
        results = self.r.xreadgroup(TEST_GROUP, "c1", {TEST_STREAM: ">"}, count=10, block=1000)
        self.assertTrue(results, "backlog entry should be delivered to a group created at id=0")

    def test_process_entry_writes_sqlite_and_jsonl_and_summary(self):
        fields = _fake_dead_letter_fields("hearth.ember.insight.v1", "schema_violation", "x" * 50)
        merged = dlq.process_entry(self.conn, "1-1", fields, self.quarantine_path,
                                    self.jsonl_dir, self.tracker)
        self.assertEqual(merged["original_type"], "hearth.ember.insight.v1")

        # SQLite row present (quarantined since schema_violation is non-retryable).
        row = self.conn.execute("SELECT * FROM dead_letters").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["failure_reason"], "schema_violation")

        # JSONL sink has exactly one line, day-stamped file.
        files = list(self.jsonl_dir.glob("dead_letter-*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = files[0].read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["failure_reason"], "schema_violation")

        # Summary tracker recorded the (channel, reason) pair.
        self.tracker.flush()
        summary = json.loads((self.jsonl_dir / "summary.json").read_text())
        self.assertEqual(summary["current_window_total"], 1)
        self.assertEqual(summary["by_channel"][0]["channel"], "hearth.ember.insight.v1")
        self.assertEqual(summary["by_channel"][0]["failure_reason"], "schema_violation")

    def test_jsonl_never_contains_more_than_written_excerpt_field_shape(self):
        # Redaction check: the sink stores original_payload_excerpt (as designed —
        # it's private ~/.cache, matching the existing quarantine archive), but
        # process_entry/tracker must never leak it into the SUMMARY structure.
        fields = _fake_dead_letter_fields(
            "tachyonos.trading.secret.v1", "schema_violation", "SECRET_PAYLOAD_MARKER")
        dlq.process_entry(self.conn, "2-1", fields, self.quarantine_path, self.jsonl_dir, self.tracker)
        self.tracker.flush()
        summary_text = (self.jsonl_dir / "summary.json").read_text()
        self.assertNotIn("SECRET_PAYLOAD_MARKER", summary_text)

    def test_end_to_end_consumer_group_worker_acks_and_stops(self):
        import threading
        self.r.xadd(TEST_STREAM, _fake_dead_letter_fields(
            "hearth.ember.insight.v1", "schema_violation", "y" * 40))

        stop_event = threading.Event()
        orig_stream = dlq.DLQ_STREAM
        dlq.DLQ_STREAM = TEST_STREAM
        try:
            t = threading.Thread(
                target=dlq.consumer_group_worker,
                args=(self.conn, "redis://localhost:6379", stop_event,
                      self.quarantine_path, self.jsonl_dir, self.tracker, TEST_GROUP, "worker-1"),
                daemon=True,
            )
            t.start()
            deadline = time.time() + 5
            while time.time() < deadline:
                if self.conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0] >= 1:
                    break
                time.sleep(0.1)
            stop_event.set()
            t.join(timeout=3)
        finally:
            dlq.DLQ_STREAM = orig_stream

        count = self.conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        self.assertEqual(count, 1)

        # Entry must be ACKed — pending list should be empty.
        pending = self.r.xpending(TEST_STREAM, TEST_GROUP)
        self.assertEqual(pending["pending"], 0)


class TestJsonlRotationAndPrune(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dlq-jsonl-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rotates_when_over_max_bytes(self):
        day = dlq._day_stamp()
        base = self.tmpdir / f"dead_letter-{day}.jsonl"
        base.write_text("x" * 100)
        path = dlq.append_jsonl(self.tmpdir, {"a": 1}, max_bytes=50)
        self.assertNotEqual(path, base)
        self.assertTrue(path.name.endswith(".1.jsonl"))

    def test_prune_removes_old_files_only(self):
        old = self.tmpdir / "dead_letter-20200101.jsonl"
        old.write_text("{}\n")
        import os
        old_time = time.time() - 30 * 86400
        os.utime(old, (old_time, old_time))

        new = self.tmpdir / f"dead_letter-{dlq._day_stamp()}.jsonl"
        new.write_text("{}\n")

        removed = dlq.prune_old_jsonl(self.tmpdir, retention_days=14)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class TestSummaryTracker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="dlq-summary-test-"))
        self.path = self.tmpdir / "summary.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_and_snapshot_reset(self):
        t = dlq.SummaryTracker(self.path)
        t.record("hearth.ember.insight.v1", "schema_violation")
        t.record("hearth.ember.insight.v1", "schema_violation")
        t.record("other.channel", "malformed_json")
        rollup = t.snapshot_and_reset_window()
        self.assertEqual(rollup["total_count"], 3)
        self.assertEqual(rollup["since_start_total"], 3)
        by = {(c["channel"], c["failure_reason"]): c["count"] for c in rollup["by_channel"]}
        self.assertEqual(by[("hearth.ember.insight.v1", "schema_violation")], 2)

        # Window reset: a second snapshot immediately after has zero new counts.
        rollup2 = t.snapshot_and_reset_window()
        self.assertEqual(rollup2["total_count"], 0)
        # since_start_total is cumulative, never resets.
        self.assertEqual(rollup2["since_start_total"], 3)

    def test_flush_is_atomic_write(self):
        t = dlq.SummaryTracker(self.path)
        t.record("a.b", "reason1")
        t.flush()
        self.assertTrue(self.path.exists())
        data = json.loads(self.path.read_text())
        self.assertEqual(data["current_window_total"], 1)
        # No leftover .tmp file after rename.
        self.assertFalse(self.path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
