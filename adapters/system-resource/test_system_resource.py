import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collector
from store import Store
import watch


class CollectorTests(unittest.TestCase):
    def test_psi_and_io_preserve_missing(self):
        self.assertEqual(collector.psi("some avg10=1.00 avg60=2.00 avg300=3.00 total=4\n")["some"]["total_usec"], 4)
        self.assertEqual(collector.io_totals("8:0 rbytes=3 wbytes=5 rios=1\n"), (3, 5))
        self.assertEqual(collector.io_totals(None), (None, None))
        self.assertEqual(collector.meminfo("MemTotal: 2 kB\nMemAvailable: 1 kB\n")["host_memory_total_bytes"], 2048)
        self.assertEqual(collector.diskstats("8 0 sda 1 0 4 0 2 0 6 0\n")["host_disk_write_sectors"], 6)

    def test_identity_and_counter_fences(self):
        now = {"state": "ok", "boot_id": "a", "cgroup_id": "1", "cpu_usage_usec": 20}
        old = {"state": "ok", "boot_id": "a", "cgroup_id": "1", "cpu_usage_usec": 10}
        self.assertEqual(collector.intervalize(now, old, 2)["cpu_usage_usec_per_s"], 5)
        self.assertEqual(collector.intervalize(now, {**old, "cgroup_id": "2"}, 2)["interval_state"], "reset")
        self.assertEqual(collector.intervalize({**now, "cpu_usage_usec": 1}, old, 2)["interval_state"], "reset")
        self.assertEqual(collector.intervalize({**now, "boot_id": None}, old, 2)["interval_state"], "unavailable")

    def test_partial_cgroup_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "user.slice"; path.mkdir()
            (path / "cpu.stat").write_text("usage_usec 1\n")
            (path / "cpu.max").write_text("max 100000\n")
            (path / "memory.current").write_text("2\n")
            self.assertEqual(collector.cgroup_sample(Path(td), "user.slice")["state"], "unavailable")


class StoreTests(unittest.TestCase):
    def test_retention_rollup_query_and_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "x.sqlite3")
            base = {"ts": 600.0, "host": "h", "boot_id": "b", "cgroup": "user.slice", "cgroup_id": "1", "state": "ok", "interval_state": "ok", "elapsed_s": 60, "cpu_usage_usec": 1, "memory_current_bytes": 8, "pressure_json": '{"memory":{"some":{"avg10":12}}}', "cgroup_pressure_json": '{"memory":{"some":{"avg10":12}}}', "events_json": "{}", "collector_version": "v", "collector_digest": "d", "duration_ms": 1, "delivery": "pending"}
            store.insert(base); store.rollup(900); store.commit()
            self.assertTrue(store.sustained_pressure("h", "user.slice", 1)); self.assertTrue(store.finding("x", 700, {}, 3600)); self.assertFalse(store.finding("x", 701, {}, 3600))
            result = store.report(0, 999, now=700)
            self.assertEqual(result["query_limit"], 500); self.assertEqual(len(result["pressure_over_time"]), 1)
            other_boot = {**base, "ts": 601.0, "boot_id": "new"}
            store.insert(other_boot); store.rollup(900); store.commit()
            self.assertEqual(len(store.report(0, now=900)["pressure_over_time"]), 2)
            store.retain(90000, raw_hours=1, rollup_days=30)
            self.assertEqual(store.report(0, now=90000)["health"]["sample_count"], 0)


class WatchTests(unittest.TestCase):
    def test_sample_is_schema_valid_and_finding_requires_three(self):
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(db=str(Path(td) / "x.sqlite3"), cgroups="user.slice", max_cgroups=8, raw_hours=24, rollup_days=30, cooldown_s=3600, dry_run=True)
            def measure(**_):
                return {"boot_id": "b", "host": {}, "pressure": {"memory": {"some": {"avg10": 11}}}, "cgroups": [{"cgroup": "user.slice", "cgroup_id": "1", "state": "ok", "cpu_usage_usec": 1, "cpu_throttled_usec": 0, "cpu_period_usec": 100000, "memory_current_bytes": 2, "io_read_bytes": 0, "io_write_bytes": 0, "memory_events": {}, "pressure": {}}]}
            events = []
            for now in (1000, 1060, 1120): events = watch.sample(args, now, measure)["events"]
            finding = [e for e in events if "finding" in e][0]
            self.assertTrue(finding["finding"].endswith(":user.slice"))
            import jsonschema
            schema = json.loads((Path(__file__).parents[2] / "schemas/bus.system.resource.sample.v1.json").read_text())
            jsonschema.validate({"kind": "sample", **events[0]}, schema)


if __name__ == "__main__":
    unittest.main()
