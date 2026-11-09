"""Tests for adapters/pattern-watchdog/watchdog.py.

The watchdog talks to Valkey and shells out to systemctl / nervous; both are
replaced here by an in-memory client and a recording subprocess stub.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
NOW = 1_800_000_000.0   # fixed "current time" for the pure checks
DAY = 86400.0


class FakeRedis:
    """Just enough of redis-py for the watchdog's checks.

    ``signal_age_s`` is the age of the newest REAL signal (None = none at all);
    ``degraded_ages_s`` are consumer_degraded entries, which the live stream
    carries in bulk. Entries come in the two shapes redis-mirror writes.
    """

    def __init__(self, *, signal_age_s: float | None = None, now: float = NOW,
                 bundle_age_s: float = 60.0, degraded_ages_s=()):
        self.hashes: dict[str, dict[str, str]] = {}
        self.signal_entries: list[tuple[str, dict[str, str]]] = []
        self.signal_calls = 0
        self._seq = 0
        self._now = now
        if signal_age_s is not None:
            self.add_signal(signal_age_s, "anomaly")
        for age in degraded_ages_s:
            self.add_signal(age, "consumer_degraded")
        self.bundle_id = f"{int((now - bundle_age_s) * 1000)}-0"

    def add_signal(self, age_s: float, signal_type: str | None, shape: str = "raw") -> str:
        self._seq += 1
        eid = f"{int((self._now - age_s) * 1000)}-{self._seq}"
        payload = {"signal_id": f"s{self._seq}", "signal_type": signal_type,
                   "severity": "warn", "confidence": 1.0, "channels": ["c"],
                   "description": "x"}
        raw = json.dumps({"specversion": "1.0", "type": "bus.pattern.signal.v1", "data": payload})
        if shape == "raw":                  # _raw only (the newest degraded entries)
            fields = {"_raw": raw, "type": "bus.pattern.signal.v1"}
        elif shape == "flat":               # flattened data.*, _raw cut mid-string
            fields = {"_raw": raw[:40], "data.signal_type": signal_type}
        elif shape == "unreadable":         # neither field gives a signal_type
            fields = {"_raw": "not json", "type": "bus.pattern.signal.v1"}
        else:
            raise ValueError(shape)
        self.signal_entries.append((eid, fields))
        return eid

    def ping(self):
        return True

    def xinfo_groups(self, name):
        if name == "nbus:bundles":
            return [{"name": "deer-flow-patterns", "pel-count": 0, "lag": 0}]
        return []

    def xinfo_stream(self, name):
        return {"entries-added": 0}

    def xrevrange(self, name, max="+", min="-", count=None):
        if name == "nbus:bundles":
            return [(self.bundle_id, {})]
        if name != "nbus:bus.pattern.signal.v1":
            return []
        self.signal_calls += 1
        key = lambda eid: tuple(int(p) for p in eid.split("-"))
        rows = sorted(self.signal_entries, key=lambda e: key(e[0]), reverse=True)
        if max != "+":
            if max.startswith("("):
                rows = [e for e in rows if key(e[0]) < key(max[1:])]
            else:
                rows = [e for e in rows if key(e[0]) <= key(max)]
        return [(i, dict(f)) for i, f in rows][:count]

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, mapping=None, **_kw):
        self.hashes.setdefault(key, {}).update(mapping or {})

    def expire(self, *_a, **_kw):
        return True


class _Subprocess:
    """Records every command the watchdog runs; systemctl says 'active'."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, cmd, **_kw):
        self.calls.append(list(cmd))
        return types.SimpleNamespace(stdout="active\n", stderr="", returncode=0)

    def published(self, channel: str) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["nervous", "publish"] and c[2] == channel]

    def restarted(self) -> bool:
        return any("restart" in c for c in self.calls)


def _load_watchdog():
    # Loaded under a stub `redis` so the module imports on a box without the
    # package, and on the pre-change module that imports it at top level.
    stub = types.ModuleType("redis")
    stub.Redis = type("Redis", (), {})
    spec = importlib.util.spec_from_file_location("pattern_watchdog_under_test", HERE / "watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"redis": stub}):
        spec.loader.exec_module(mod)
    return mod


watchdog = _load_watchdog()


class _Base(unittest.TestCase):

    def setUp(self):
        self.sp = _Subprocess()
        patcher = patch.object(watchdog.subprocess, "run", self.sp.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PATTERN_SIGNAL_MAX_AGE_S", None)

    def run_main(self, fake: FakeRedis) -> tuple[int, str]:
        redis_stub = types.ModuleType("redis")

        class _R:
            @staticmethod
            def from_url(*_a, **_kw):
                return fake

        redis_stub.Redis = _R
        err = io.StringIO()
        with patch.dict(sys.modules, {"redis": redis_stub}), \
                patch.object(watchdog, "redis", redis_stub, create=True), \
                contextlib.redirect_stderr(err):
            rc = watchdog.main()
        return rc, err.getvalue()


class TestMainReportsAStaleOutput(_Base):
    """The failure the old watchdog hid: plumbing healthy, nothing produced."""

    def test_old_signal_stream_makes_main_fail_with_a_clear_line(self):
        rc, err = self.run_main(FakeRedis(signal_age_s=40 * DAY, now=time.time()))
        self.assertEqual(rc, 1)
        self.assertIn("no bus.pattern.signal.v1 for", err)
        self.assertNotIn("pipeline ok", err)

    def test_fresh_signal_stream_keeps_main_green(self):
        rc, err = self.run_main(FakeRedis(signal_age_s=600, now=time.time()))
        self.assertEqual(rc, 0)
        self.assertIn("pipeline ok", err)

    def test_stale_output_does_not_restart_the_consumer(self):
        self.run_main(FakeRedis(signal_age_s=40 * DAY, now=time.time()))
        self.assertFalse(self.sp.restarted())

    def test_empty_stream_makes_main_fail(self):
        rc, err = self.run_main(FakeRedis(signal_age_s=None, now=time.time()))
        self.assertEqual(rc, 1)
        self.assertIn("empty or missing", err)

    def test_stale_output_does_not_warn_on_the_bus_every_run(self):
        fake = FakeRedis(signal_age_s=40 * DAY, now=time.time())
        for _ in range(4):
            rc, _err = self.run_main(fake)
            self.assertEqual(rc, 1)
        self.assertEqual(len(self.sp.published("sys.log.entry.v1")), 1)
        self.assertEqual(len(self.sp.published("bus.intrinsic.marker.v1")), 1)


class TestSignalAgeCheck(_Base):

    def check(self, fake: FakeRedis, now: float = NOW) -> list[str]:
        with contextlib.redirect_stderr(io.StringIO()):
            return watchdog._check_signal_age(fake, now=now)

    def test_bound_is_configurable_both_ways(self):
        fake = FakeRedis(signal_age_s=5 * 3600)
        os.environ["PATTERN_SIGNAL_MAX_AGE_S"] = "3600"
        self.assertEqual(len(self.check(fake)), 1)
        os.environ["PATTERN_SIGNAL_MAX_AGE_S"] = str(DAY)
        self.assertEqual(self.check(fake), [])

    def test_invalid_bound_falls_back_to_a_working_default(self):
        for bad in ("soon", "-5", "0"):
            os.environ["PATTERN_SIGNAL_MAX_AGE_S"] = bad
            self.assertEqual(self.check(FakeRedis(signal_age_s=400 * DAY)) != [], True, bad)
            self.assertEqual(self.check(FakeRedis(signal_age_s=60)), [], bad)

    def test_issue_names_the_age_and_the_last_signal_time(self):
        issue = self.check(FakeRedis(signal_age_s=40 * DAY))[0]
        self.assertIn("40.0 days", issue)
        self.assertRegex(issue, r"last signal \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

    def test_issue_text_never_triggers_the_service_restart_match(self):
        self.assertNotIn("service is", self.check(FakeRedis(signal_age_s=40 * DAY))[0])
        self.assertNotIn("service is", self.check(FakeRedis(signal_age_s=None))[0])

    def test_last_signal_age_is_tracked_in_valkey(self):
        fake = FakeRedis(signal_age_s=7200)
        self.check(fake)
        state = fake.hashes[watchdog.SIGNAL_HEALTH_KEY]
        self.assertAlmostEqual(float(state["last_signal_age_s"]), 7200, delta=1)

    def test_recovery_publishes_once_when_a_signal_arrives(self):
        fake = FakeRedis(signal_age_s=40 * DAY)
        self.assertEqual(len(self.check(fake)), 1)
        fake.add_signal(30, "anomaly")
        self.assertEqual(self.check(fake), [])
        self.assertEqual(self.check(fake), [])
        markers = self.sp.published("bus.intrinsic.marker.v1")
        self.assertEqual(len(markers), 2)   # one error.spike, one quality.signal
        self.assertIn("quality.signal", markers[-1][3])

    def test_redis_failure_is_reported_not_swallowed(self):
        class Broken(FakeRedis):
            def xrevrange(self, *_a, **_kw):
                raise ConnectionError("boom")
        issues = self.check(Broken(signal_age_s=60))
        self.assertEqual(len(issues), 1)
        self.assertIn("boom", issues[0])


class TestConsumerDegradedIsNotOutput(_Base):
    """consumer_degraded is the consumer reporting itself broken (F13/#131).

    The live stream holds 3,602 real signals, all from 2026-05-10/11, followed
    by 1,842 consumer_degraded entries. Counting those as freshness made the
    watchdog green for about a third of the days since the pipeline died.
    """

    def check(self, fake: FakeRedis, now: float = NOW) -> list[str]:
        with contextlib.redirect_stderr(io.StringIO()):
            return watchdog._check_signal_age(fake, now=now)

    def test_recent_degraded_only_stream_fails_main(self):
        fake = FakeRedis(degraded_ages_s=[20, 1200, 2400], now=time.time())
        rc, err = self.run_main(fake)
        self.assertEqual(rc, 1)
        self.assertIn("consumer_degraded", err)
        self.assertNotIn("pipeline ok", err)

    def test_recent_degraded_with_a_long_dead_real_signal_fails(self):
        fake = FakeRedis(signal_age_s=182 * DAY, degraded_ages_s=[60, 1800, 3600])
        issue = self.check(fake)[0]
        self.assertIn("182.0 days", issue)
        self.assertRegex(issue, r"last signal \d{4}-\d{2}-\d{2}T")
        self.assertIn("consumer_degraded", issue)

    def test_recent_real_signal_passes_even_with_newer_degraded_entries(self):
        fake = FakeRedis(signal_age_s=600, degraded_ages_s=[30, 90])
        self.assertEqual(self.check(fake), [])

    def test_stale_by_age_even_if_degraded_entries_are_seconds_old(self):
        # The pre-fix rule (newest entry of any type) was green here.
        fake = FakeRedis(signal_age_s=5 * DAY, degraded_ages_s=[5])
        self.assertEqual(len(self.check(fake)), 1)

    def test_degraded_only_stream_reports_no_real_output_not_empty(self):
        issue = self.check(FakeRedis(degraded_ages_s=[60, 120]))[0]
        self.assertIn("no non-degraded bus.pattern.signal.v1", issue)
        self.assertIn("all 2 entries", issue)
        self.assertNotIn("empty or missing", issue)
        self.assertNotIn("service is", issue)

    def test_real_signal_is_found_beneath_many_pages_of_degraded_entries(self):
        fake = FakeRedis(signal_age_s=2 * 3600,
                         degraded_ages_s=[i + 1 for i in range(1300)])
        self.assertEqual(self.check(fake), [])
        self.assertGreater(fake.signal_calls, 2)   # 50 + 500 + 500 + ...

    def test_healthy_stream_costs_one_read(self):
        fake = FakeRedis(signal_age_s=60)
        self.assertEqual(self.check(fake), [])
        self.assertEqual(fake.signal_calls, 1)

    def test_scan_bound_caps_the_read_and_says_so(self):
        fake = FakeRedis(signal_age_s=DAY, degraded_ages_s=[i + 1 for i in range(120)])
        with patch.object(watchdog, "SIGNAL_SCAN_MAX", 80):
            issue = self.check(fake)[0]
        self.assertIn("newest 80 entries (scan bound)", issue)
        self.assertEqual(fake.signal_calls, 2)   # 50, then the 30 left under the bound

    def test_flat_field_entries_are_classified_without_valid_raw(self):
        flat_real = FakeRedis()
        flat_real.add_signal(120, "correlation", shape="flat")
        self.assertEqual(self.check(flat_real), [])
        flat_degraded = FakeRedis()
        flat_degraded.add_signal(120, "consumer_degraded", shape="flat")
        self.assertEqual(len(self.check(flat_degraded)), 1)

    def test_unreadable_entries_are_not_proof_of_output(self):
        fake = FakeRedis()
        fake.add_signal(60, None, shape="unreadable")
        issue = self.check(fake)[0]
        self.assertIn("1 entries had no readable signal_type", issue)

    def test_valkey_tracks_the_real_signal_not_the_degraded_one(self):
        fake = FakeRedis(signal_age_s=7200, degraded_ages_s=[10])
        self.check(fake)
        state = fake.hashes[watchdog.SIGNAL_HEALTH_KEY]
        self.assertAlmostEqual(float(state["last_signal_age_s"]), 7200, delta=1)
        self.assertAlmostEqual(NOW - float(state["newest_degraded_ts"]), 10, delta=1)

    def test_degraded_only_outage_publishes_once_then_recovers_on_real_output(self):
        fake = FakeRedis(degraded_ages_s=[60])
        for _ in range(3):
            self.assertEqual(len(self.check(fake)), 1)
        fake.add_signal(5, "anomaly")
        self.assertEqual(self.check(fake), [])
        markers = self.sp.published("bus.intrinsic.marker.v1")
        self.assertEqual(len(markers), 2)           # one error.spike, one quality.signal
        self.assertEqual(len(self.sp.published("sys.log.entry.v1")), 1)

    def test_bad_entry_id_is_reported(self):
        fake = FakeRedis()
        fake.signal_entries.append(("not-an-id", {"_raw": "{}"}))
        fake.xrevrange = lambda *a, **k: fake.signal_entries[:1]
        issues = self.check(fake)
        self.assertEqual(len(issues), 1)
        self.assertIn("bad entry id", issues[0])


class TestExistingChecksUnchanged(_Base):

    def test_plumbing_check_is_still_clean_on_a_healthy_fake(self):
        self.assertEqual(watchdog._check(FakeRedis(signal_age_s=60, now=time.time())), [])

    def test_stale_bundles_still_flagged(self):
        fake = FakeRedis(signal_age_s=60, now=time.time(), bundle_age_s=2 * 3600)
        issues = watchdog._check(fake)
        self.assertTrue(any("nbus:bundles stale" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
