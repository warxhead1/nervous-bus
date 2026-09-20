import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

import gpu
import watch
from history import report
from store import Store


UUID_A = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
UUID_B = "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def host_entity(counter=0):
    return {
        "entity": "@host", "kind": "host", "identity": "boot-1", "device_identity": "a" * 64,
        "state": "ok", "reason": None,
        "counters": {"cpu_usage_usec": counter, "io_read_bytes": counter * 2,
                     "io_write_bytes": counter, "memory_high_events": 0},
        "gauges": {"memory_current_bytes": 1024}, "limits": {}, "devices": {},
        "pressure": {kind: {"some": {"avg10": 0, "avg60": 0, "avg300": 0, "total_usec": 0}}
                     for kind in ("cpu", "memory", "io")},
    }


def gpu_measurement(uuid=UUID_A, used_mib=1100, driver="615.71.09"):
    return {
        "state": "ok", "reason": None, "attempted": True, "query_ms": 4.5,
        "devices": [{"uuid": uuid, "driver_version": driver, "state": "ok", "reason": None,
                     "memory_total_bytes": 8192 * gpu.MIB, "memory_used_bytes": used_mib * gpu.MIB,
                     # Deliberately not total-used: NVIDIA reserved memory can make this differ.
                     "memory_free_bytes": 6518 * gpu.MIB, "utilization_pct": 0}],
    }


class ClosedStdoutProcess:
    def __init__(self, clock):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        os.close(write_fd)
        self.clock = clock
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        if self.killed:
            return -9
        self.clock[0] = gpu.PROCESS_TIMEOUT_S + 0.01
        raise subprocess.TimeoutExpired(gpu.COMMAND, timeout)


class StuckProcess:
    stdout = None

    def __init__(self, clock):
        self.clock = clock
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.clock[0] += 1
        raise subprocess.TimeoutExpired(gpu.COMMAND, timeout)


class BufferedPipeProcess:
    """A real readable pipe with an owned-child lifecycle, without GPU hardware."""
    def __init__(self, data):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        os.write(write_fd, data)
        os.close(write_fd)
        self.killed = False
        self.reaped = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.reaped = True
        return -9 if self.killed else 0


class GpuReadTests(unittest.TestCase):
    def test_real_pipe_reader_caps_output_and_cleans_up_read_errors(self):
        for failure in ("overflow", "read_error", "success"):
            with self.subTest(failure=failure):
                process = BufferedPipeProcess(b"x" * 65)
                self.addCleanup(process.stdout.close)
                if failure == "overflow":
                    with patch("gpu.MAX_STDOUT_BYTES", 64):
                        result = gpu.run_query(popen=lambda *_a, **_k: process)
                    self.assertEqual(result["reason"], "output_limit")
                    self.assertEqual(result["stdout"], b"")
                elif failure == "read_error":
                    with patch("gpu.os.read", side_effect=OSError("fixture read failure")):
                        result = gpu.run_query(popen=lambda *_a, **_k: process)
                    self.assertEqual(result["reason"], "query_failed")
                else:
                    result = gpu.run_query(popen=lambda *_a, **_k: process)
                    self.assertEqual((result["state"], result["stdout"]), ("ok", b"x" * 65))
                self.assertEqual(process.killed, failure != "success")
                self.assertTrue(process.reaped)
                self.assertTrue(process.stdout.closed)

    def test_absent_timeout_and_overflow_are_typed_without_gpu_hardware(self):
        with patch("gpu.subprocess.Popen", side_effect=FileNotFoundError):
            absent = gpu.collect_gpu()
        self.assertEqual((absent["state"], absent["reason"], absent["devices"]),
                         ("unavailable", "unsupported", []))
        for reason in ("timeout", "output_limit"):
            with patch("gpu.run_query", return_value={"state": "unavailable", "reason": reason,
                                                       "attempted": True, "query_ms": 2000}):
                result = gpu.collect_gpu()
            self.assertEqual((result["state"], result["reason"]), ("unavailable", reason))
        self.assertEqual(gpu.parse_output(b"x" * (gpu.MAX_STDOUT_BYTES + 1))["reason"], "output_limit")

    def test_stdout_close_does_not_bypass_deadline_and_cleanup_is_bounded(self):
        ticks = [0.0]
        process = ClosedStdoutProcess(ticks)
        state, output, code = gpu._bounded_stdout(process, 0.0, lambda: ticks[0])
        self.assertEqual((state, output, code), ("timeout", b"", None))
        self.assertTrue(process.killed)
        ticks = [0.0]
        stuck = StuckProcess(ticks)
        self.assertFalse(gpu._terminate_and_reap(stuck, lambda: ticks[0]))
        self.assertTrue(stuck.killed)
        with patch("gpu._bounded_stdout", return_value=("cleanup_failed", b"", None)):
            result = gpu.run_query(popen=lambda *_args, **_kwargs: object())
        self.assertEqual((result["state"], result["reason"]), ("unavailable", "cleanup_failed"))

    def test_parser_preserves_zero_and_unknown_and_rejects_bad_identity(self):
        partial = gpu.parse_output((
            f"{UUID_A}, 615.71.09, 8192, 0, 6518, 0\n"
            f"{UUID_B}, N/A, N/A, 1100, N/A, N/A\n").encode(), 7)
        self.assertEqual(partial["reason"], "partial_reading")
        first, second = partial["devices"]
        self.assertEqual(first["state"], "ok")  # Used + free need not equal total VRAM.
        self.assertEqual((first["memory_used_bytes"], first["utilization_pct"]), (0, 0))
        self.assertIsNone(second["driver_version"])
        self.assertIsNone(second["memory_total_bytes"])
        duplicate = gpu.parse_output((
            f"{UUID_A}, 615.71.09, 8192, 1, 6518, 1\n"
            f"{UUID_A.upper()}, 615.71.09, 8192, 2, 6518, 2\n").encode())
        self.assertEqual((duplicate["state"], duplicate["reason"], duplicate["devices"]),
                         ("partial", "duplicate_uuid", []))
        malformed = gpu.parse_output(f"{UUID_A}, 615.71.09, 8, 9, 0, 1\n".encode())
        self.assertEqual((malformed["state"], malformed["reason"]), ("partial", "malformed_output"))
        many = b"\n".join(
            f"GPU-{i:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa, 615.71.09, 8, 1, 6, 1".encode()
            for i in range(9))
        limited = gpu.parse_output(many)
        self.assertEqual((limited["reason"], len(limited["devices"])), ("device_limit", 8))

    def test_command_is_fixed_inventory_only_and_uses_no_shell(self):
        captured = {}

        def popen(*args, **kwargs):
            captured["args"], captured["kwargs"] = args, kwargs
            return object()

        with patch("gpu._bounded_stdout", return_value=("finished", b"", 0)):
            gpu.run_query(popen=popen)
        self.assertEqual(captured["args"], (gpu.COMMAND,))
        self.assertNotIn("shell", captured["kwargs"])
        self.assertEqual(captured["kwargs"]["stderr"], subprocess.DEVNULL)
        self.assertIn("--query-gpu=uuid,driver_version,memory.total,memory.used,memory.free,utilization.gpu",
                      gpu.COMMAND)


class GpuStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "history.sqlite3"
        self.config = {"selectors": ["work"], "raw_hours": 24, "rollup_days": 30,
                       "cooldown_s": 3600, "expected_interval_s": 60}

    def sample(self, ts, measurement=None, boot="boot-1", publisher=None):
        data = {"boot_id": boot, "entities": [host_entity(ts)], "warnings": []}
        return watch.sample(self.db_path, self.config, True, measure=lambda **_: copy.deepcopy(data),
                            gpu_measure=lambda: copy.deepcopy(measurement or gpu_measurement()), now=ts, mono=ts,
                            publisher=publisher or (lambda *_: "dry_run"))

    def store(self):
        store = Store(self.db_path)
        self.addCleanup(store.close)
        return store

    def validate(self, event):
        schema = json.loads((watch.ROOT / "schemas" / (watch.CHANNEL + ".json")).read_text())
        jsonschema.Draft202012Validator(schema).validate(event)

    def test_late_first_uuid_and_driver_have_same_coverage_as_single_fold(self):
        def rows(incremental):
            self.db_path = Path(self.temp.name) / ("incremental.db" if incremental else "single.db")
            self.sample(600, gpu_measurement(UUID_A))
            store = self.store()
            if incremental:
                store.rollup(900)
                store.commit()
            self.sample(660, gpu_measurement(UUID_B))
            if incremental:
                store.rollup(900)
                store.commit()
            self.sample(720, gpu_measurement(UUID_B, driver="616.0"))
            store.rollup(900)
            store.commit()
            return [tuple(row) for row in store.db.execute(
                "SELECT uuid,driver_version,attempt_count,observed_sample_count,missing_sample_count "
                "FROM gpu_rollups ORDER BY uuid,driver_version")]

        expected = rows(False)
        self.assertTrue(all(row[2] == 3 for row in expected))
        self.assertEqual(rows(True), expected)

    def test_v3_contract_and_frozen_v1_v2(self):
        event, receipt = self.sample(1000)
        self.assertEqual((watch.CHANNEL, event["collector"]["version"], receipt["state"]),
                         ("bus.system.resource.sample.v3", "3.0.0", "ok"))
        self.assertEqual(event["entities"][0]["gauges"]["memory_current_bytes"], 1024)
        self.assertEqual(event["gpu"]["devices"][0]["memory_used_bytes"], 1100 * gpu.MIB)
        self.validate(event)
        invalid = copy.deepcopy(event)
        invalid["gpu"]["devices"][0]["memory_used_bytes"] = "unknown"
        with self.assertRaises(jsonschema.ValidationError):
            self.validate(invalid)
        expected = {
            "bus.system.resource.sample.v1.json": "f0fb52a08a6462846a961542573a2b434cee235da704270da0ea4dddc1a79eea",
            "bus.system.resource.sample.v2.json": "fbb99b43793d660e6709c42187eec22e913058e4ad31c3e07f6a13d5dec7c937",
        }
        for name, digest in expected.items():
            self.assertEqual(hashlib.sha256((watch.ROOT / "schemas" / name).read_bytes()).hexdigest(), digest)

    def test_publisher_failure_keeps_gpu_status_and_gauges(self):
        event, receipt = self.sample(1000, publisher=lambda *_: "failed")
        store = self.store()
        status = store.db.execute("SELECT state,delivery,attempted FROM gpu_batches WHERE batch_id=?", (event["id"],)).fetchone()
        gauge = store.db.execute("SELECT memory_total_bytes,memory_used_bytes,memory_free_bytes,utilization_pct "
                                 "FROM gpu_gauges WHERE batch_id=?", (event["id"],)).fetchone()
        self.assertEqual(tuple(status), ("ok", "failed", 1))
        self.assertEqual(tuple(gauge), (8192 * gpu.MIB, 1100 * gpu.MIB, 6518 * gpu.MIB, 0))
        self.assertEqual(receipt["delivery"], "failed")

    def test_absent_timeout_overflow_and_partial_gpu_do_not_block_host_rows(self):
        partial = gpu.parse_output((
            f"{UUID_A}, 615.71.09, 8192, 1, 6518, 1\n"
            "malformed row\n").encode())
        multi = gpu.parse_output((
            f"{UUID_A}, 615.71.09, 8192, 1, 6518, 1\n"
            f"{UUID_B}, 615.71.09, 8192, 2, 6518, 2\n").encode())
        measurements = [
            gpu.unavailable_gpu("unsupported", attempted=True),
            gpu.unavailable_gpu("timeout", 2000, attempted=True),
            gpu.unavailable_gpu("output_limit", 2, attempted=True),
            partial,
            multi,
        ]
        for index, measurement in enumerate(measurements):
            event, _ = self.sample(1000 + index * 60, measurement)
            self.assertEqual(event["entities"][0]["gauges"]["memory_current_bytes"], 1024)
            self.validate(event)
        store = self.store()
        states = store.db.execute("SELECT state,reason FROM gpu_batches ORDER BY ts").fetchall()
        self.assertEqual([tuple(row) for row in states[:4]], [
            ("unavailable", "unsupported"), ("unavailable", "timeout"),
            ("unavailable", "output_limit"), ("partial", "malformed_output"),
        ])
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], len(measurements))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM gpu_gauges").fetchone()[0], 3)

    def test_rollup_retention_restart_and_report_are_idempotent(self):
        for ts in (1000, 1060, 1120, 1180, 1240):
            self.sample(ts)
        first = self.store()
        first.rollup(1500)
        first.commit()
        row = first.db.execute("SELECT SUM(attempt_count),SUM(observed_sample_count),SUM(missing_sample_count),"
                               "MIN(memory_used_bytes_min),MAX(memory_used_bytes_max),SUM(memory_used_bytes_sum),"
                               "SUM(memory_used_bytes_count) FROM gpu_rollups WHERE uuid=?", (UUID_A,)).fetchone()
        self.assertEqual(tuple(row), (5, 5, 0, 1100 * gpu.MIB, 1100 * gpu.MIB, 5 * 1100 * gpu.MIB, 5))
        first.rollup(1500)
        first.commit()
        self.assertEqual(first.db.execute("SELECT SUM(observed_sample_count) FROM gpu_rollups").fetchone()[0], 5)
        first.close()
        reopened = Store(self.db_path)
        self.addCleanup(reopened.close)
        reopened.rollup(1500)
        reopened.commit()
        self.assertEqual(reopened.db.execute("SELECT SUM(observed_sample_count) FROM gpu_rollups").fetchone()[0], 5)
        result = report(reopened, 900, now=1500)
        self.assertNotIn("gpu", result["top_consumers"])
        self.assertEqual(result["gpu"]["coverage"]["query_attempts"], 5)
        self.assertEqual(sum(row["memory_used_bytes_count"] for row in result["gpu"]["sampled_history"]), 5)
        reopened.retain(5000, raw_hours=1)
        reopened.commit()
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM gpu_batches").fetchone()[0], 0)
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM gpu_gauges").fetchone()[0], 0)
        self.assertEqual(sum(row["observed_sample_count"] for row in
                             report(reopened, 900, now=5000)["gpu"]["sampled_history"]), 5)

    def test_replacement_reboot_and_observed_loss_stay_on_identity_boundaries(self):
        self.sample(1120, gpu_measurement(UUID_A), boot="boot-1")
        self.sample(1240, gpu_measurement(UUID_B), boot="boot-1")
        self.sample(1300, gpu_measurement(UUID_B), boot="boot-2")
        store = self.store()
        store.rollup(1500)
        store.commit()
        rows = store.db.execute("SELECT boot_id,uuid,last_observed_ts,attempt_count,observed_sample_count,missing_sample_count "
                                "FROM gpu_rollups ORDER BY boot_id,uuid").fetchall()
        self.assertEqual({(row["boot_id"], row["uuid"]) for row in rows},
                         {("boot-1", UUID_A.lower().replace("gpu-", "GPU-")),
                          ("boot-1", UUID_B.lower().replace("gpu-", "GPU-")),
                          ("boot-2", UUID_B.lower().replace("gpu-", "GPU-"))})
        old_loss = next(row for row in rows if row["boot_id"] == "boot-1" and row["uuid"].endswith("aaaa")
                        and row["missing_sample_count"])
        self.assertIsNone(old_loss["last_observed_ts"])
        self.assertEqual((old_loss["attempt_count"], old_loss["observed_sample_count"], old_loss["missing_sample_count"]),
                         (1, 0, 1))
        self.assertTrue(all(row["attempt_count"] >= row["observed_sample_count"] for row in rows))

    def test_gpu_raw_and_rollup_caps_are_independent_and_bounded(self):
        for ts in (1000, 1060, 1120, 1180, 1240):
            self.sample(ts)
        store = self.store()
        store.rollup(1500)
        store.retain(1500, raw_cap=2, rollup_cap=1)
        store.commit()
        self.assertLessEqual(store.db.execute("SELECT COUNT(*) FROM gpu_batches").fetchone()[0], 2)
        self.assertLessEqual(store.db.execute("SELECT COUNT(*) FROM gpu_gauges").fetchone()[0], 2)
        self.assertLessEqual(store.db.execute("SELECT COUNT(*) FROM gpu_status_rollups").fetchone()[0], 1)
        self.assertLessEqual(store.db.execute("SELECT COUNT(*) FROM gpu_rollups").fetchone()[0], 1)
        metadata = dict(store.db.execute("SELECT key,value FROM metadata"))
        self.assertGreater(metadata["gpu_raw_status_cap_evictions"], 0)

    def test_replacement_placeholders_never_raise_stored_device_count_over_eight(self):
        def eight(start):
            result = gpu_measurement()
            result["devices"] = [
                {"uuid": f"GPU-{index:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "driver_version": "615.71.09",
                 "state": "ok", "reason": None, "memory_total_bytes": 8192 * gpu.MIB,
                 "memory_used_bytes": 1 * gpu.MIB, "memory_free_bytes": 6500 * gpu.MIB,
                 "utilization_pct": 1}
                for index in range(start, start + 8)
            ]
            return result

        _, first_receipt = self.sample(1000, eight(0))
        second, _ = self.sample(1060, eight(16))
        store = self.store()
        self.assertEqual(first_receipt["state"], "ok")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM gpu_gauges WHERE batch_id=?", (second["id"],)).fetchone()[0], 8)


if __name__ == "__main__":
    unittest.main()
