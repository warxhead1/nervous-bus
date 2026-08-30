"""Tests for adapters/staleness/monitor.py against real nbus:test.* streams.

Requires a reachable Redis (redis://localhost:6379 by default, override with
NERVOUS_REDIS_URL). Uses a dedicated 'nbus:test.staleness.*' namespace and
cleans up every key it creates, both before and after the run, so it never
collides with real channel data or leaves test streams behind.

Run:
    python3 test_monitor.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import redis

sys.path.insert(0, str(Path(__file__).resolve().parent))
import monitor  # noqa: E402

REDIS_URL = os.environ.get("NERVOUS_REDIS_URL", "redis://localhost:6379")
TEST_PREFIX = "nbus:test.staleness."


def _mkstream(client: redis.Redis, name: str, timestamps_ms: list) -> str:
    """Create a stream with entries at explicit Redis-Stream IDs (ms-seq)."""
    stream = f"{TEST_PREFIX}{name}"
    client.delete(stream)
    for i, ts in enumerate(timestamps_ms):
        client.xadd(stream, {"data.v": str(i)}, id=f"{ts}-0")
    return stream


class StalenessMonitorTest(unittest.TestCase):
    def setUp(self):
        self.client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
        try:
            self.client.ping()
        except redis.RedisError as e:
            self.skipTest(f"Redis unreachable at {REDIS_URL}: {e}")
        self._cleanup()
        self.tmpdir = tempfile.mkdtemp(prefix="staleness-test-")
        self.baseline_path = Path(self.tmpdir) / "baselines.json"
        self.report_path = Path(self.tmpdir) / "report.md"

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        for key in self.client.scan_iter(match=f"{TEST_PREFIX}*", count=200):
            self.client.delete(key)

    def test_sparse_channel_never_alerts(self):
        now_ms = int(time.time() * 1000)
        # Only 3 lifetime events (< SPARSE_MIN_EVENTS=5).
        stream = _mkstream(self.client, "sparse", [now_ms - 3000, now_ms - 2000, now_ms - 1000])
        result = monitor.evaluate_stream(self.client, stream, now=time.time())
        self.assertEqual(result["verdict"], "sparse")
        self.assertEqual(result["lifetime_count"], 3)

    def test_healthy_regular_channel_is_ok(self):
        now = time.time()
        now_ms = int(now * 1000)
        # 20 events, 10s apart, most recent 5s ago: well within a P95-derived threshold.
        timestamps = [now_ms - (20 - i) * 10_000 for i in range(20)]
        timestamps[-1] = now_ms - 5_000
        stream = _mkstream(self.client, "healthy", timestamps)
        result = monitor.evaluate_stream(self.client, stream, now=now)
        self.assertEqual(result["verdict"], "ok")
        self.assertGreaterEqual(result["baseline_sample_size"], 5)

    def test_gone_silent_channel_is_stale(self):
        now = time.time()
        now_ms = int(now * 1000)
        # 20 events at a tight ~10s cadence, but the LAST one was 2 hours ago —
        # far beyond any 10s-cadence-derived threshold (even generous floor).
        timestamps = [now_ms - 7200_000 - (20 - i) * 10_000 for i in range(20)]
        stream = _mkstream(self.client, "gone-silent", timestamps)
        result = monitor.evaluate_stream(self.client, stream, now=now)
        self.assertEqual(result["verdict"], "stale")
        self.assertGreater(result["observed_gap_seconds"], result["expected_gap_seconds"])

    def test_floor_protects_bursty_then_normal_channel(self):
        now = time.time()
        now_ms = int(now * 1000)
        # A burst of heartbeats a second apart, then last event 20 minutes ago.
        # p95 of a 1s-cadence burst would be ~1s; multiplier alone (3s) must not
        # trigger a false stale alert at 20 minutes silence — the floor should.
        timestamps = [now_ms - 20 * 60_000 - (20 - i) * 1_000 for i in range(20)]
        stream = _mkstream(self.client, "bursty", timestamps)
        result = monitor.evaluate_stream(self.client, stream, now=now, floor_seconds=1800.0)
        # 20 minutes (1200s) < 1800s floor -> not stale despite p95*multiplier being tiny.
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["expected_gap_seconds"], 1800.0)

    def test_run_end_to_end_writes_report_and_baseline(self):
        now = time.time()
        now_ms = int(now * 1000)
        _mkstream(self.client, "e2e-ok", [now_ms - (10 - i) * 10_000 for i in range(10)])
        timestamps = [now_ms - 5000_000 - (10 - i) * 10_000 for i in range(10)]
        _mkstream(self.client, "e2e-stale", timestamps)
        _mkstream(self.client, "e2e-sparse", [now_ms - 1000])

        results = monitor.run(
            redis_url=REDIS_URL,
            pattern=f"{TEST_PREFIX}*",
            dry_run=True,
            baseline_path=self.baseline_path,
            report_path=self.report_path,
        )

        verdicts = {r["channel"]: r["verdict"] for r in results}
        self.assertEqual(verdicts[f"test.staleness.e2e-ok"], "ok")
        self.assertEqual(verdicts[f"test.staleness.e2e-stale"], "stale")
        self.assertEqual(verdicts[f"test.staleness.e2e-sparse"], "sparse")

        self.assertTrue(self.baseline_path.exists())
        self.assertTrue(self.report_path.exists())
        report_text = self.report_path.read_text()
        self.assertIn("STALE", report_text)
        self.assertIn("e2e-stale", report_text)

    def test_alert_cooldown_suppresses_repeat_publish(self):
        now = time.time()
        now_ms = int(now * 1000)
        timestamps = [now_ms - 5000_000 - (10 - i) * 10_000 for i in range(10)]
        _mkstream(self.client, "cooldown", timestamps)

        published = []
        orig = monitor.publish_stale_event
        monitor.publish_stale_event = lambda result, dry_run=False: (published.append(result["channel"]) or True)
        try:
            monitor.run(
                redis_url=REDIS_URL,
                pattern=f"{TEST_PREFIX}cooldown",
                dry_run=False,
                baseline_path=self.baseline_path,
                report_path=self.report_path,
                alert_cooldown_s=3600.0,
            )
            monitor.run(
                redis_url=REDIS_URL,
                pattern=f"{TEST_PREFIX}cooldown",
                dry_run=False,
                baseline_path=self.baseline_path,
                report_path=self.report_path,
                alert_cooldown_s=3600.0,
            )
        finally:
            monitor.publish_stale_event = orig

        self.assertEqual(published.count("test.staleness.cooldown"), 1)


if __name__ == "__main__":
    unittest.main()
