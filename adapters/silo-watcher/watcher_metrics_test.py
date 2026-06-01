"""Tests for silo-watcher frame metrics functionality."""

import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from watcher import build_frame_metrics_event


class TestBuildFrameMetricsEvent(unittest.TestCase):
    def test_returns_none_when_no_frames(self):
        with tempfile.TemporaryDirectory() as td:
            report = {"silo": "test", "frames": []}
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("test", "silo_test", Path(td))
            self.assertIsNone(result)

    def test_returns_none_when_frames_missing(self):
        with tempfile.TemporaryDirectory() as td:
            report = {}
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("test", "silo_test", Path(td))
            self.assertIsNone(result)

    def test_returns_none_on_parse_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "verification_report.json"
            p.write_text("not json{")
            result = build_frame_metrics_event("test", "silo_test", Path(td))
            self.assertIsNone(result)

    def test_single_frame_event(self):
        with tempfile.TemporaryDirectory() as td:
            report = {
                "silo": "racing",
                "frames": [
                    {
                        "time_ms": 16.5,
                        "gpu_util_pct": 78.2,
                        "mem_bandwidth_gbps": 45.6,
                        "top_shader": "water",
                        "anomaly_codes": ["HIGH_LAT"],
                    }
                ],
            }
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("racing", "silo_racing", Path(td))
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["silo"], "racing")
            self.assertEqual(result[0]["session_id"], "silo_racing")
            self.assertEqual(result[0]["frame_index"], 0)
            self.assertEqual(result[0]["frame_time_ms"], 16.5)
            self.assertEqual(result[0]["gpu_utilization_pct"], 78.2)
            self.assertEqual(result[0]["memory_bandwidth_gbps"], 45.6)
            self.assertEqual(result[0]["top_shader"], "water")
            self.assertEqual(result[0]["anomaly_codes"], ["HIGH_LAT"])

    def test_multiple_frame_events(self):
        with tempfile.TemporaryDirectory() as td:
            report = {
                "silo": "exploration",
                "frames": [
                    {"time_ms": 15.0, "gpu_util_pct": 70.0, "mem_bandwidth_gbps": 40.0, "top_shader": "terrain", "anomaly_codes": []},
                    {"time_ms": 16.0, "gpu_util_pct": 75.0, "mem_bandwidth_gbps": 42.0, "top_shader": "water", "anomaly_codes": ["HIGH_LAT"]},
                    {"time_ms": 14.5, "gpu_util_pct": 72.0, "mem_bandwidth_gbps": 41.0, "top_shader": "sky", "anomaly_codes": []},
                ],
            }
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("exploration", "silo_exploration", Path(td))
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0]["frame_index"], 0)
            self.assertEqual(result[1]["frame_index"], 1)
            self.assertEqual(result[2]["frame_index"], 2)
            self.assertEqual(result[1]["anomaly_codes"], ["HIGH_LAT"])

    def test_skips_non_dict_frame_entries(self):
        with tempfile.TemporaryDirectory() as td:
            report = {"silo": "test", "frames": [1, "foo", None, {"time_ms": 10}]}
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("test", "silo_test", Path(td))
            self.assertIsNotNone(result)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["frame_time_ms"], 10.0)

    def test_handles_missing_silo_in_report(self):
        with tempfile.TemporaryDirectory() as td:
            report = {"frames": [{"time_ms": 20.0}]}
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("fallback_silo", "silo_test", Path(td))
            self.assertEqual(result[0]["silo"], "fallback_silo")

    def test_handles_missing_anomaly_codes_field(self):
        with tempfile.TemporaryDirectory() as td:
            report = {"silo": "test", "frames": [{"time_ms": 20.0}]}
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("test", "silo_test", Path(td))
            self.assertEqual(result[0]["anomaly_codes"], [])

    def test_handles_non_list_anomaly_codes(self):
        with tempfile.TemporaryDirectory() as td:
            report = {"silo": "test", "frames": [{"time_ms": 20.0, "anomaly_codes": "not-a-list"}]}
            p = Path(td) / "verification_report.json"
            p.write_text(json.dumps(report))
            result = build_frame_metrics_event("test", "silo_test", Path(td))
            self.assertEqual(result[0]["anomaly_codes"], [])


if __name__ == "__main__":
    unittest.main()
