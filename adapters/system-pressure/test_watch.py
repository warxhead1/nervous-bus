#!/usr/bin/env python3
"""Unit tests for the pure classification logic in system-pressure/watch.py."""

import unittest

import watch


class TestDiskLevel(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(watch.disk_level(50.0), "ok")
        self.assertEqual(watch.disk_level(89.9), "ok")
        self.assertEqual(watch.disk_level(90.0), "warn")
        self.assertEqual(watch.disk_level(94.9), "warn")
        self.assertEqual(watch.disk_level(95.0), "critical")
        self.assertEqual(watch.disk_level(100.0), "critical")


class TestGrowthRate(unittest.TestCase):
    def test_no_prior(self):
        self.assertIsNone(watch.growth_gb_per_hour(None, 10 * 1024**3, 300))

    def test_zero_elapsed(self):
        self.assertIsNone(watch.growth_gb_per_hour(0, 10 * 1024**3, 0))

    def test_rate(self):
        # 5G added in 30 min = 10 GB/h
        rate = watch.growth_gb_per_hour(0, 5 * 1024**3, 1800)
        self.assertAlmostEqual(rate, 10.0, places=3)

    def test_shrinking_is_negative(self):
        rate = watch.growth_gb_per_hour(10 * 1024**3, 5 * 1024**3, 3600)
        self.assertAlmostEqual(rate, -5.0, places=3)


class TestDirLevel(unittest.TestCase):
    def test_small_and_slow_is_ok(self):
        self.assertEqual(watch.dir_level(1 * 1024**3, 0.5), "ok")

    def test_absolute_size_warns(self):
        self.assertEqual(watch.dir_level(60 * 1024**3, None), "warn")

    def test_growth_warns(self):
        self.assertEqual(watch.dir_level(1 * 1024**3, 6.0), "warn")

    def test_incident_rate_is_critical(self):
        # 2026-08-30: hearth/.beads/backup measured ~40 GB/h during the burst
        self.assertEqual(watch.dir_level(100 * 1024**3, 40.0), "critical")

    def test_shrinking_large_dir_still_warns_on_size(self):
        self.assertEqual(watch.dir_level(200 * 1024**3, -30.0), "warn")


class TestRestartsLevel(unittest.TestCase):
    def test_stable_unit(self):
        self.assertEqual(watch.restarts_level(0, "active"), "ok")

    def test_first_observation_no_delta(self):
        self.assertEqual(watch.restarts_level(None, "active"), "ok")

    def test_failed_unit_warns(self):
        self.assertEqual(watch.restarts_level(0, "failed"), "warn")

    def test_storm_is_critical(self):
        # 2026-08-30: beads-dolt.service added hundreds of restarts between polls
        self.assertEqual(watch.restarts_level(5, "activating"), "critical")
        self.assertEqual(watch.restarts_level(300, "failed"), "critical")


class TestShouldEmit(unittest.TestCase):
    def test_transition_emits(self):
        self.assertTrue(watch.should_emit("ok", "warn", 0, 1000))
        self.assertTrue(watch.should_emit("warn", "critical", 0, 1000))

    def test_recovery_emits(self):
        self.assertTrue(watch.should_emit("critical", "ok", 0, 1000))

    def test_steady_ok_is_silent(self):
        self.assertFalse(watch.should_emit("ok", "ok", 0, 10**9))

    def test_sustained_pressure_reminds_after_interval(self):
        now = 10**6
        recent = now - watch.REMIND_INTERVAL_S + 60
        stale = now - watch.REMIND_INTERVAL_S - 60
        self.assertFalse(watch.should_emit("warn", "warn", recent, now))
        self.assertTrue(watch.should_emit("warn", "warn", stale, now))


if __name__ == "__main__":
    unittest.main()
