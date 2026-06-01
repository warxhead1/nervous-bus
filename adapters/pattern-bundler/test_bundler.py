# adapters/pattern-bundler/test_bundler.py
import math, sys, time, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from baseline import welford_empty, welford_update, welford_stddev, welford_deviation
from window import Window

class TestWelford(unittest.TestCase):
    def test_empty_state(self):
        s = welford_empty()
        self.assertEqual(s["n"], 0)
        self.assertEqual(s["mean"], 0.0)
        self.assertEqual(s["M2"], 0.0)
        self.assertEqual(s["min"], float("inf"))
        self.assertEqual(s["max"], float("-inf"))

    def test_single_value_mean(self):
        s = welford_update(welford_empty(), 5.0)
        self.assertEqual(s["n"], 1)
        self.assertAlmostEqual(s["mean"], 5.0)

    def test_two_values_mean(self):
        s = welford_update(welford_update(welford_empty(), 2.0), 4.0)
        self.assertAlmostEqual(s["mean"], 3.0)

    def test_stddev_known_sequence(self):
        s = welford_empty()
        for v in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]:
            s = welford_update(s, v)
        self.assertAlmostEqual(welford_stddev(s), 2.0, places=5)

    def test_deviation_returns_none_below_100(self):
        s = welford_empty()
        for _ in range(99):
            s = welford_update(s, 1.0)
        self.assertIsNone(welford_deviation(s, 5.0))

    def test_deviation_nonzero_stddev(self):
        s = welford_empty()
        for _ in range(100):
            s = welford_update(s, 1.0)
        for _ in range(100):
            s = welford_update(s, 3.0)
        deviation = welford_deviation(s, 10.0)
        self.assertIsNotNone(deviation)
        self.assertGreater(abs(deviation), 1.0)

    def test_min_max_tracked(self):
        s = welford_empty()
        for v in [1.0, 5.0, 3.0]:
            s = welford_update(s, v)
        self.assertEqual(s["min"], 1.0)
        self.assertEqual(s["max"], 5.0)

class TestWindow(unittest.TestCase):
    def _window(self, count=50, time_s=900):
        return Window("loom.lifecycle.v1", "bus", count, time_s)

    def test_should_not_close_fresh(self):
        w = self._window()
        self.assertFalse(w.should_close())

    def test_closes_on_count(self):
        w = self._window(count=3)
        for _ in range(3):
            w.ingest("{}", {})
        self.assertTrue(w.should_close())

    def test_low_interest_when_no_baseline(self):
        w = self._window()
        self.assertTrue(w.is_low_interest(None))

    def test_not_low_interest_high_deviation(self):
        w = self._window()
        self.assertFalse(w.is_low_interest(2.5))

    def test_not_low_interest_with_errors(self):
        w = self._window()
        w.ingest_log({"level": "error", "message": "oops"})
        self.assertFalse(w.is_low_interest(0.3))

    def test_stats_rate_per_min(self):
        w = Window("ch", "bus", 50, 900)
        w.opened_at = time.time() - 60
        for _ in range(10):
            w.ingest("{}", {})
        stats = w.compute_stats(None)
        self.assertAlmostEqual(stats["rate_per_min"], 10.0, delta=0.5)

    def test_is_low_interest_exact_boundary(self):
        # abs(1.0) >= 1.0 → not low interest
        w = self._window()
        self.assertFalse(w.is_low_interest(1.0))

    def test_ingest_tracks_numeric_fields(self):
        w = self._window()
        w.ingest('{"type":"x"}', {"elapsed_ms": 142.5, "active": True})
        stats = w.compute_stats(None)
        # numeric field tracked; bool "active" excluded
        self.assertIn("data.elapsed_ms", stats["field_stats"])
        self.assertNotIn("data.active", stats["field_stats"])

    def test_ingest_log_critical_counts_as_error(self):
        w = self._window()
        w.ingest_log({"level": "critical", "message": "oom"})
        self.assertFalse(w.is_low_interest(0.3))

    def test_closes_on_time(self):
        w = Window("ch", "bus", 50, 0.01)  # 10ms trigger
        import time; time.sleep(0.02)
        self.assertTrue(w.should_close())

class TestWindowErrorEvents(unittest.TestCase):
    def test_error_log_captured_in_error_events(self):
        w = Window("test:chan", "logs", count_trigger=100, time_trigger_s=300)
        entry = {
            "log_source": "journal", "service": "myapp",
            "level": "error", "message": "connection refused", "raw": "raw"
        }
        w.ingest_log(entry)
        self.assertEqual(len(w.error_events), 1)
        self.assertIn("connection refused", w.error_events[0])

    def test_critical_log_captured_in_error_events(self):
        w = Window("test:chan", "logs", count_trigger=100, time_trigger_s=300)
        entry = {
            "log_source": "kernel", "service": "kernel",
            "level": "critical", "message": "OOM kill process 123", "raw": "raw"
        }
        w.ingest_log(entry)
        self.assertEqual(len(w.error_events), 1)

    def test_info_log_not_captured_in_error_events(self):
        w = Window("test:chan", "logs", count_trigger=100, time_trigger_s=300)
        entry = {
            "log_source": "journal", "service": "myapp",
            "level": "info", "message": "started", "raw": "raw"
        }
        w.ingest_log(entry)
        self.assertEqual(len(w.error_events), 0)

    def test_error_events_capped_at_20(self):
        w = Window("test:chan", "logs", count_trigger=10000, time_trigger_s=300)
        for i in range(30):
            w.ingest_log({"log_source": "journal", "service": "s", "level": "error",
                          "message": f"error {i}", "raw": "r"})
        self.assertEqual(len(w.error_events), 20)
        # Most recent 20 kept
        self.assertIn("error 29", w.error_events[-1])

    def test_error_events_empty_initially(self):
        w = Window("test:chan", "bus", count_trigger=50, time_trigger_s=900)
        self.assertEqual(w.error_events, [])

class TestWindowPatternDist(unittest.TestCase):
    def test_fp_counts_accumulate(self):
        w = Window("test:chan", "logs", count_trigger=1000, time_trigger_s=300)
        for i in range(5):
            w.ingest_log({"log_source": "journal", "service": "s",
                          "level": "info", "message": "go build ./cmd/server", "raw": "r"})
        for i in range(2):
            w.ingest_log({"log_source": "journal", "service": "s",
                          "level": "info", "message": "go build ./cmd/worker", "raw": "r"})
        # Both normalize to same fingerprint (only differ in path)
        self.assertGreater(len(w.fp_counts), 0)
        total = sum(w.fp_counts.values())
        self.assertEqual(total, 7)

    def test_binary_data_not_fingerprinted(self):
        w = Window("test:chan", "logs", count_trigger=1000, time_trigger_s=300)
        w.ingest_log({"log_source": "journal", "service": "s",
                      "level": "info", "message": "[binary data]", "raw": "r"})
        self.assertEqual(len(w.fp_counts), 0)

    def test_fp_counts_empty_initially(self):
        w = Window("test:chan", "logs", count_trigger=1000, time_trigger_s=300)
        self.assertEqual(w.fp_counts, {})


class TestFingerprint(unittest.TestCase):
    def test_strips_timestamps(self):
        from bundler import _fingerprint
        fp = _fingerprint("2026-05-09T19:23:01Z connection refused to 127.0.0.1:5432")
        self.assertNotIn("2026", fp)
        self.assertIn("connection refused", fp)

    def test_strips_pids(self):
        from bundler import _fingerprint
        fp = _fingerprint("Killed process 98765 (python3) total-vm:1048576kB")
        self.assertNotIn("98765", fp)
        self.assertIn("killed", fp)

    def test_strips_hex(self):
        from bundler import _fingerprint
        fp = _fingerprint("segfault at 0xdeadbeef in libssl.so")
        self.assertNotIn("0xdeadbeef", fp)
        self.assertIn("segfault", fp)

    def test_strips_uuids(self):
        from bundler import _fingerprint
        fp = _fingerprint("task 550e8400-e29b-41d4-a716-446655440000 failed")
        self.assertNotIn("550e8400", fp)
        self.assertIn("task", fp)
        self.assertIn("failed", fp)

    def test_same_structure_same_fingerprint(self):
        from bundler import _fingerprint
        fp1 = _fingerprint("Killed process 111 (python3) total-vm:999kB")
        fp2 = _fingerprint("Killed process 222 (python3) total-vm:888kB")
        self.assertEqual(fp1, fp2)

    def test_different_structure_different_fingerprint(self):
        from bundler import _fingerprint
        fp1 = _fingerprint("connection refused to database")
        fp2 = _fingerprint("out of memory, killing process")
        self.assertNotEqual(fp1, fp2)


class TestFingerprintDensityGate(unittest.TestCase):
    def _make_redis_with_fps(self, channel: str, fp_data: list[dict]):
        """Return a mock redis that returns fp_data for hgetall on pattern:err-fp:{channel}."""
        import json
        from unittest.mock import MagicMock
        mock_r = MagicMock()
        mock_r.hgetall.return_value = {
            f"hash{i}": json.dumps(fp) for i, fp in enumerate(fp_data)
        }
        return mock_r

    def test_dense_returns_true_when_two_fps_each_seen_three_times(self):
        from bundler import _has_dense_fingerprints
        r = self._make_redis_with_fps("test:chan", [
            {"fp": "conn refused", "count": 5, "last_seen": 1e10},
            {"fp": "sqlite locked", "count": 3, "last_seen": 1e10},
        ])
        self.assertTrue(_has_dense_fingerprints(r, "test:chan"))

    def test_dense_returns_false_when_only_one_qualifies(self):
        from bundler import _has_dense_fingerprints
        r = self._make_redis_with_fps("test:chan", [
            {"fp": "conn refused", "count": 5, "last_seen": 1e10},
            {"fp": "another error", "count": 1, "last_seen": 1e10},
        ])
        self.assertFalse(_has_dense_fingerprints(r, "test:chan"))

    def test_dense_returns_false_when_no_fingerprints(self):
        from bundler import _has_dense_fingerprints
        from unittest.mock import MagicMock
        r = MagicMock()
        r.hgetall.return_value = {}
        self.assertFalse(_has_dense_fingerprints(r, "test:chan"))

    def test_dense_ignores_stale_fingerprints(self):
        from bundler import _has_dense_fingerprints
        import time
        # last_seen 48 hours ago — outside the 24h window
        old_ts = time.time() - 86400 * 2
        r = self._make_redis_with_fps("test:chan", [
            {"fp": "conn refused", "count": 10, "last_seen": old_ts},
            {"fp": "sqlite locked", "count": 10, "last_seen": old_ts},
        ])
        self.assertFalse(_has_dense_fingerprints(r, "test:chan"))

    def test_dense_returns_false_on_redis_error(self):
        from bundler import _has_dense_fingerprints
        from unittest.mock import MagicMock
        r = MagicMock()
        r.hgetall.side_effect = Exception("redis down")
        self.assertFalse(_has_dense_fingerprints(r, "test:chan"))


if __name__ == "__main__":
    unittest.main()
