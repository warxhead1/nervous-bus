"""Tests for adapters/pattern-watchdog/watchdog.py.

The watchdog talks to Valkey and shells out to systemctl / nervous; both are
replaced here by an in-memory client and a recording subprocess stub.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
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
    """Just enough of redis-py for the watchdog's checks."""

    def __init__(self, *, signal_age_s: float | None, now: float = NOW,
                 bundle_age_s: float = 60.0):
        self.hashes: dict[str, dict[str, str]] = {}
        self.signal_ids: list[str] = []
        if signal_age_s is not None:
            self.signal_ids.append(f"{int((now - signal_age_s) * 1000)}-0")
        self.bundle_id = f"{int((now - bundle_age_s) * 1000)}-0"

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
        if name == "nbus:bus.pattern.signal.v1":
            return [(i, {}) for i in reversed(self.signal_ids)][:count]
        return []

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
        fake.signal_ids.append(f"{int((NOW - 30) * 1000)}-0")
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


class TestExistingChecksUnchanged(_Base):

    def test_plumbing_check_is_still_clean_on_a_healthy_fake(self):
        self.assertEqual(watchdog._check(FakeRedis(signal_age_s=60, now=time.time())), [])

    def test_stale_bundles_still_flagged(self):
        fake = FakeRedis(signal_age_s=60, now=time.time(), bundle_age_s=2 * 3600)
        issues = watchdog._check(fake)
        self.assertTrue(any("nbus:bundles stale" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()
