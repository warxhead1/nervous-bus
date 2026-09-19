import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

import collector
import watch
from history import diagnose, report
from intervals import intervalize
from store import Store


def entity(counter=0, pressure=0, name="/work", state="ok"):
    return {"entity": name, "kind": "host" if name == "@host" else "cgroup",
            "identity": "inode-1", "device_identity": "a" * 64, "state": state,
            "reason": None if state == "ok" else "missing", "counters": {
                "cpu_usage_usec": counter, "io_read_bytes": counter * 2,
                "io_write_bytes": counter, "memory_high_events": 0},
            "gauges": {"memory_current_bytes": 1024}, "limits": {}, "devices": {},
            "pressure": {kind: {"some": {"avg10": pressure, "avg60": 0,
                                       "avg300": 0, "total_usec": 0}} for kind in ("cpu", "memory", "io")}}


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.db_path = self.path / "history.sqlite3"
        self.config = {"selectors": ["work"], "raw_hours": 24, "rollup_days": 30,
                       "cooldown_s": 3600, "expected_interval_s": 60}

    def store(self):
        result = Store(self.db_path)
        self.addCleanup(result.close)
        return result

    def sample(self, t, rows=None, boot="boot-1", mono=None, publisher=None):
        data = {"boot_id": boot, "entities": rows or [entity(t)], "warnings": []}
        return watch.sample(self.db_path, self.config, True, measure=lambda **_: copy.deepcopy(data),
                            now=t, mono=t if mono is None else mono,
                            publisher=publisher or (lambda *_: "dry_run"))

    def test_physical_disks_exclude_layers_and_partitions(self):
        fields = "1 0 2 3 4 0 5 6 0 7 8"
        rows = collector.disks("\n".join(f"{dev} {name} {fields}" for dev, name in
                                        (("259 0", "nvme0n1"), ("259 1", "nvme0n1p1"),
                                         ("253 0", "dm-0"), ("8 0", "sda"), ("8 1", "sda1"))))
        self.assertEqual(set(rows), {"259:0", "8:0"})
        self.assertEqual(rows["259:0"]["read_bytes"], 1024)
        self.assertEqual(rows["8:0"]["write_bytes"], 2560)

    def test_cgroup_io_missing_is_not_idle(self):
        physical = {"259:0": {"name": "nvme0n1"}}
        self.assertEqual(collector.cgroup_io(None, physical)[:2], (None, None))
        self.assertEqual(collector.cgroup_io("", physical)[:2], (0, 0))
        self.assertEqual(collector.cgroup_io("253:0 rbytes=12 wbytes=24", physical)[:2], (None, None))
        raw = "259:0 rbytes=12 wbytes=24\n253:0 rbytes=12 wbytes=24"
        self.assertEqual(collector.cgroup_io(raw, physical)[:2], (12, 24))
        self.assertEqual(collector.cgroup_io("259:0 rbytes=bad wbytes=24", physical)[:2], (None, None))

    def test_kernel_zero_unlimited_and_missing_remain_distinct(self):
        self.assertEqual(collector.limit("max"), {"state": "unlimited", "value": None})
        self.assertEqual(collector.limit("0"), {"state": "limited", "value": 0})
        self.assertEqual(collector.limit(None)["state"], "unavailable")
        self.assertIsNone(collector.number("nan"))
        self.assertEqual(collector.number("9007199254740993"), 9007199254740993)

    def test_absent_kernel_and_cgroup_still_produce_valid_event(self):
        data = collector.collect(self.path / "proc", self.path / "cg", ["missing"])
        event, receipt = watch.sample(self.db_path, self.config, True, measure=lambda **_: data)
        self.assertEqual(receipt["state"], "partial")
        self.assertEqual(event["entities"][1]["pressure"], {"cpu": None, "memory": None, "io": None})
        self.validate(event)

    def test_selection_is_bounded_and_recursive_patterns_rejected(self):
        for i in range(20):
            (self.path / "cg" / f"unit{i}").mkdir(parents=True)
        data = collector.collect(self.path / "proc", self.path / "cg", ["unit*"])
        self.assertEqual(len(data["entities"]), 9)
        self.assertIn("cgroup_selection_truncated", data["warnings"])
        for selector in ("../escape", "**/unit", "*/unit"):
            with self.assertRaises(ValueError):
                collector.collect(self.path, self.path, [selector])

    def test_rates_use_interval_not_lifetime(self):
        self.sample(1000, [entity(1_000_000)])
        event, _ = self.sample(1060, [entity(121_000_000)])
        value = event["entities"][0]["interval"]
        self.assertEqual(value["deltas"]["cpu_usage_usec"], 120_000_000)
        self.assertEqual(value["rates"]["cpu_usage_usec_per_s"], 2_000_000)
        self.validate(event)

    def test_reboot_inode_device_change_and_counter_decrease_reset(self):
        old = {"entity_data": entity(100), "boot_id": "b", "ts": 1000, "monotonic_s": 1000}
        cases = [(entity(99), "b"), (entity(101), "new")]
        changed = entity(101); changed["identity"] = "inode-2"; cases.append((changed, "b"))
        changed = entity(101); changed["device_identity"] = "b" * 64; cases.append((changed, "b"))
        for current, boot in cases:
            result = intervalize(current, old, boot, 1060, 1060)
            self.assertEqual(result["state"], "reset")
            self.assertEqual(result["rates"], {})

    def test_clock_and_collection_gaps_do_not_emit_rates(self):
        old = {"entity_data": entity(100), "boot_id": "b", "ts": 1000, "monotonic_s": 1000}
        for mono, wall in ((999, 1060), (1300, 1300), (1060, 10060)):
            self.assertEqual(intervalize(entity(200), old, "b", mono, wall)["state"], "unavailable")

    def test_rollup_restart_retention_query_and_late_data_are_idempotent(self):
        for t in (1000, 1060, 1120, 1180, 1240):
            self.sample(t)
        store = self.store()
        store.rollup(1500); store.commit()
        count = store.db.execute("SELECT SUM(sample_count) FROM rollups").fetchone()[0]
        self.assertEqual(count, 5)
        store.rollup(1500); store.commit()
        self.assertEqual(store.db.execute("SELECT SUM(sample_count) FROM rollups").fetchone()[0], 5)
        store.retain(5000, raw_hours=1); store.commit()
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
        result = report(store, 900, now=5000)
        row = result["top_consumers"]["cpu"][0]
        self.assertEqual(row["sample_count"], 5)
        self.assertEqual(row["cpu_usec"], 240)
        self.sample(1100, [entity(2000)])
        store.rollup(5000); store.commit()
        self.assertEqual(store.db.execute("SELECT SUM(sample_count) FROM rollups").fetchone()[0], 6)

    def test_raw_and_rollups_do_not_double_count(self):
        for t in (1000, 1060, 1120, 1240, 1300):
            self.sample(t)
        result = report(self.store(), 900, now=1350)
        self.assertEqual(result["top_consumers"]["cpu"][0]["sample_count"], 5)

    def test_caps_and_empty_history_health(self):
        store = self.store()
        self.assertTrue(report(store, 0, now=1000)["health"]["stale"])
        for t in (1000, 1060, 1120):
            self.sample(t)
        store.retain(1200, raw_cap=2, rollup_cap=1); store.commit()
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 2)
        self.assertEqual(report(store, 0, limit=9999, now=1200)["query_limit"], 500)
        self.assertEqual(report(store, 0, now=1200)["health"]["raw_cap_evictions"], 1)

    def test_pressure_requires_distinct_contiguous_observations_and_cooldown(self):
        self.sample(1000, [entity(1, 25)])
        self.assertEqual(self.sample(1001, [entity(2, 25)])[0]["findings"], [])
        self.assertEqual(self.sample(1060, [entity(3, 25)])[0]["findings"], [])
        self.assertEqual(self.sample(1120, [entity(4, 25)])[0]["findings"], [])
        event, _ = self.sample(1180, [entity(5, 25)])
        self.assertEqual({f["resource"] for f in event["findings"]}, {"memory", "io"})
        self.validate(event)
        self.assertEqual(self.sample(1240, [entity(6, 25)])[0]["findings"], [])

    def test_missing_sample_and_long_gap_break_pressure_sequence(self):
        for t, state in ((1000, "ok"), (1060, "partial"), (1120, "ok"), (2000, "ok"), (2060, "ok")):
            self.assertEqual(self.sample(t, [entity(t, 25, state=state)])[0]["findings"], [])

    def test_host_pressure_does_not_falsely_attribute_to_cgroup(self):
        for t in (1000, 1060, 1120):
            event, _ = self.sample(t, [entity(t, 25, "@host"), entity(t, 0)])
        self.assertEqual({f["entity"] for f in event["findings"]}, {"@host"})

    def test_publisher_failure_preserves_local_rows_and_honest_receipt(self):
        event, receipt = self.sample(1000, publisher=lambda *_: "failed")
        store = self.store()
        self.assertEqual(store.db.execute("SELECT delivery FROM samples").fetchone()[0], "failed")
        self.assertEqual(receipt["delivery"], "failed")
        self.assertEqual(store.previous(event["host"], "/work")["collector"], event["collector"])
        with patch.dict(watch.os.environ, {"NERVOUS_BIN": str(self.path / "absent")}):
            self.assertEqual(watch.publish(event), "unavailable")

    def validate(self, value):
        schema = json.loads((watch.ROOT / "schemas" / (watch.CHANNEL + ".json")).read_text())
        jsonschema.Draft202012Validator(schema).validate(value)

    def test_contract_rejects_untyped_measurement_and_incomplete_finding(self):
        event, _ = self.sample(1000)
        invalid = copy.deepcopy(event); invalid["entities"][0]["gauges"]["memory_current_bytes"] = "unknown"
        with self.assertRaises(jsonschema.ValidationError): self.validate(invalid)
        invalid = copy.deepcopy(event); invalid["findings"] = [{"key": "a" * 32}]
        with self.assertRaises(jsonschema.ValidationError): self.validate(invalid)


if __name__ == "__main__":
    unittest.main()
