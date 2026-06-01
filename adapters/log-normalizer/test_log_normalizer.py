import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from sources.helpers import infer_level
from sources.docker_source import normalize_docker_line
from sources.journal_source import parse_journal_entry
from sources.kernel_source import parse_kmsg_line
from sources.redis_source import format_slowlog_entry

class TestInferLevel(unittest.TestCase):
    def test_panic_is_critical(self):
        self.assertEqual(infer_level("panic: runtime error"), "critical")

    def test_oom_is_critical(self):
        self.assertEqual(infer_level("Killed process 123 (python3) total-vm:1048576kB"), "critical")

    def test_error_keyword(self):
        self.assertEqual(infer_level("error: connection refused"), "error")

    def test_warn_keyword(self):
        self.assertEqual(infer_level("WARNING: deprecated API"), "warn")

    def test_plain_line_is_info(self):
        self.assertEqual(infer_level("started successfully"), "info")

from sources.docker_source import _relevant

class TestDockerRelevantFilter(unittest.TestCase):
    def test_hearth_loom_container_is_relevant(self):
        self.assertTrue(_relevant("hearth-loom-agent-abc123"))

    def test_tengine_container_is_relevant(self):
        self.assertTrue(_relevant("tengine-silo-runner"))

    def test_unrelated_container_is_not_relevant(self):
        self.assertFalse(_relevant("postgres-db"))

    def test_empty_prefix_list_allows_all(self):
        # Monkey-patch _NAME_PREFIXES to empty for this test
        import sources.docker_source as ds
        orig = ds._NAME_PREFIXES
        ds._NAME_PREFIXES = ()
        try:
            self.assertTrue(ds._relevant("anything-at-all"))
        finally:
            ds._NAME_PREFIXES = orig

class TestDockerChannelNormalization(unittest.TestCase):
    def test_strips_long_decimal_suffix(self):
        from sources.docker_source import _channel_name
        self.assertEqual(_channel_name("hearth-loom-agent-17783819408"), "hearth-loom-agent")

    def test_strips_long_hex_suffix(self):
        from sources.docker_source import _channel_name
        self.assertEqual(_channel_name("tengine-silo-runner-abc12345def"), "tengine-silo-runner")

    def test_keeps_stable_service_name(self):
        from sources.docker_source import _channel_name
        self.assertEqual(_channel_name("deer-flow-gateway"), "deer-flow-gateway")

    def test_keeps_short_numeric_suffix(self):
        from sources.docker_source import _channel_name
        # short suffix like "-1" or "-2" is part of the name, not an instance ID
        self.assertEqual(_channel_name("tengine-runner-1"), "tengine-runner-1")


class TestNormalizeDockerLine(unittest.TestCase):
    def test_parses_timestamp_prefix(self):
        line = "2026-05-09T22:10:46.123Z error: disk full"
        result = normalize_docker_line(line, "my-container")
        self.assertEqual(result["log_source"], "docker")
        self.assertEqual(result["service"], "my-container")
        self.assertEqual(result["level"], "error")
        self.assertIn("disk full", result["message"])

    def test_no_timestamp(self):
        result = normalize_docker_line("plain log line", "svc")
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "info")

    def test_raw_capped_at_1000(self):
        result = normalize_docker_line("x" * 2000, "svc")
        self.assertLessEqual(len(result["raw"]), 1000)

class TestJournalSource(unittest.TestCase):
    def test_maps_priority_3_to_error(self):
        j = {"MESSAGE": "disk error", "PRIORITY": "3", "_SYSTEMD_UNIT": "foo.service", "_PID": "123"}
        result = parse_journal_entry(j)
        self.assertEqual(result["level"], "error")
        self.assertEqual(result["log_source"], "journal")

    def test_maps_priority_4_to_warn(self):
        j = {"MESSAGE": "low memory", "PRIORITY": "4", "_SYSTEMD_UNIT": "bar.service"}
        result = parse_journal_entry(j)
        self.assertEqual(result["level"], "warn")

    def test_missing_priority_defaults_info(self):
        j = {"MESSAGE": "started", "_SYSTEMD_UNIT": "baz.service"}
        result = parse_journal_entry(j)
        self.assertEqual(result["level"], "info")

    def test_redis_service_name_not_corrupted(self):
        j = {"MESSAGE": "ready", "PRIORITY": "6", "_SYSTEMD_UNIT": "redis.service"}
        result = parse_journal_entry(j)
        self.assertEqual(result["service"], "redis")  # not "red"

    def test_strips_ansi_escape_codes(self):
        j = {"MESSAGE": "\x1b[32m INFO\x1b[0m started", "PRIORITY": "6", "_SYSTEMD_UNIT": "foo.service"}
        result = parse_journal_entry(j)
        self.assertNotIn("\x1b", result["message"])
        self.assertIn("INFO", result["message"])
        self.assertIn("started", result["message"])

    def test_replaces_decimal_byte_sequence(self):
        # 11+ space-separated decimal numbers = binary data
        j = {"MESSAGE": "27 91 50 109 50 48 50 54 45 48 53 45 48 57", "PRIORITY": "6", "_SYSTEMD_UNIT": "foo.service"}
        result = parse_journal_entry(j)
        self.assertEqual(result["message"], "[binary data]")

    def test_normal_message_unchanged(self):
        j = {"MESSAGE": "server started on port 8080", "PRIORITY": "6", "_SYSTEMD_UNIT": "foo.service"}
        result = parse_journal_entry(j)
        self.assertEqual(result["message"], "server started on port 8080")

    def test_short_number_sequence_not_replaced(self):
        # "pid 1234" — short, not decimal-byte-encoded
        j = {"MESSAGE": "process 1234 started", "PRIORITY": "6", "_SYSTEMD_UNIT": "foo.service"}
        result = parse_journal_entry(j)
        self.assertEqual(result["message"], "process 1234 started")

class TestKernelSource(unittest.TestCase):
    def test_priority_3_is_error(self):
        line = "3,1234,5678,-;EXT4-fs error on sda1\n"
        result = parse_kmsg_line(line)
        self.assertEqual(result["level"], "error")
        self.assertEqual(result["log_source"], "kernel")

    def test_no_semicolon_falls_back(self):
        result = parse_kmsg_line("plain kernel line\n")
        self.assertIsNotNone(result)

class TestRedisSource(unittest.TestCase):
    def test_formats_slowlog_entry(self):
        entry = {"id": 5, "duration": 25000, "command": ["GET", "mykey"]}
        result = format_slowlog_entry(entry)
        self.assertEqual(result["log_source"], "redis")
        self.assertIn("25.0ms", result["message"])

if __name__ == "__main__":
    unittest.main()
