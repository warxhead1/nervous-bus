"""tests/test_skill_opportunity.py — Unit tests for SkillOpportunityDetector.

All fixture commands/paths are SYNTHETIC. Covers:

  - Positive: a run's Bash command matches a skill_map.toml mapping's
    pattern, and runs.features.skills does not contain that skill -> fires.
  - Negative: mapped CLI used AND the skill IS in features.skills -> no fire.
  - Negative: CLI never invoked -> no fire, and no denominator entry either.
  - context_pattern: a mapping with a secondary required regex (e.g.
    "cargo" needs "hearth" nearby) only fires when BOTH match the same
    command string.
  - tool_summary shapes: a JSON-object tool_summary with a "command" key,
    and a bare string tool_summary, are both extracted correctly (mirrors
    detectors/rebuild_cache_miss.py's _extract_command contract).
  - Non-Bash / non-tool_call events are ignored even if their tool_summary
    text happens to match a pattern.
  - Signature is the bare skill name (no project prefix, no run_id).
  - bypass_rate in extra/evidence is bypass_run_count / cli_used_run_count.
  - Missing/malformed skill_map.toml -> load_skill_map returns [] (never
    raises), detect() then returns [] rather than breaking synthesis.
  - since_ts bounds the runs scanned (issue #32 API).
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema
from detectors.skill_opportunity import SkillOpportunityDetector, load_skill_map


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    project    TEXT NOT NULL,
    started    TEXT NOT NULL,
    ended      TEXT NOT NULL,
    features   TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);
"""

_SKILL_MAP_TOML = r"""
[[map]]
skill = "hearth-loom-dispatch"
pattern = '\bhearth-loom\b'

[[map]]
skill = "beads"
pattern = '(?:^|[\s;&|`])bd\s+(?:create|update|show|ready|close|claim)\b'

[[map]]
skill = "hearth-dev"
pattern = '\b(?:hearth-build|cargo)\b'
context_pattern = '\bhearth\b'
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project, started="2026-09-20T00:00:00Z",
                 ended="2026-09-20T01:00:00Z", features=None):
    conn.execute(
        "INSERT INTO runs (run_id, project, started, ended, features) VALUES (?,?,?,?,?)",
        (run_id, project, started, ended, json.dumps(features or {})),
    )


def _insert_bash_event(conn, run_id, seq, command, as_json_object=False):
    tool_summary = json.dumps({"command": command}) if as_json_object else command
    data = {"event": "tool_call", "tool_name": "Bash", "tool_summary": tool_summary}
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, "2026-09-20T00:10:00Z", "bus.agent.activity.v1",
         json.dumps({"data": data})),
    )


def _insert_non_bash_event(conn, run_id, seq, tool_name, tool_summary):
    data = {"event": "tool_call", "tool_name": tool_name, "tool_summary": tool_summary}
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, "2026-09-20T00:10:00Z", "bus.agent.activity.v1",
         json.dumps({"data": data})),
    )


class TestLoadSkillMap(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_skill_map(Path("/no/such/skill_map.toml")), [])

    def test_malformed_toml_returns_empty(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w")
        tmp.write("not [ valid toml")
        tmp.close()
        self.assertEqual(load_skill_map(Path(tmp.name)), [])

    def test_valid_toml_parses_mappings(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w")
        tmp.write(_SKILL_MAP_TOML)
        tmp.close()
        mappings = load_skill_map(Path(tmp.name))
        self.assertEqual({m.skill for m in mappings},
                          {"hearth-loom-dispatch", "beads", "hearth-dev"})


class TestSkillOpportunityDetector(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        tmp = tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="w")
        tmp.write(_SKILL_MAP_TOML)
        tmp.close()
        self.skill_map_path = Path(tmp.name)

    def _detector(self):
        det = SkillOpportunityDetector(self.conn)
        det.skill_map_path = self.skill_map_path
        return det

    def test_bypass_fires_when_skill_not_loaded(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_bash_event(self.conn, "run-1", 1, "hearth-loom status")
        det = self._detector()
        candidates = det.run()
        by_sig = {c.signature: c for c in candidates}
        self.assertIn("hearth-loom-dispatch", by_sig)
        cand = by_sig["hearth-loom-dispatch"]
        self.assertEqual(cand.run_ids, ["run-1"])
        self.assertEqual(cand.project, "synthetic")
        self.assertEqual(cand.extra["bypass_rate"], 1.0)

    def test_no_fire_when_skill_already_loaded(self):
        _insert_run(self.conn, "run-1", "synthetic",
                     features={"skills": {"hearth-loom-dispatch": {"skill_tool": 1}}})
        _insert_bash_event(self.conn, "run-1", 1, "hearth-loom status")
        det = self._detector()
        self.assertEqual(det.run(), [])

    def test_no_fire_when_cli_never_invoked(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_bash_event(self.conn, "run-1", 1, "ls -la")
        det = self._detector()
        self.assertEqual(det.run(), [])

    def test_json_object_tool_summary_extracted(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_bash_event(self.conn, "run-1", 1, "hearth-loom status", as_json_object=True)
        det = self._detector()
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].signature, "hearth-loom-dispatch")

    def test_context_pattern_requires_both_regexes(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_run(self.conn, "run-2", "synthetic", features={"skills": {}})
        # cargo alone, no "hearth" context -> must NOT fire hearth-dev.
        _insert_bash_event(self.conn, "run-1", 1, "cargo build --release")
        # cargo AND hearth in the same command -> fires.
        _insert_bash_event(self.conn, "run-2", 1, "cargo build -p hearth-api")
        det = self._detector()
        candidates = det.run()
        by_sig = {c.signature: c for c in candidates}
        self.assertIn("hearth-dev", by_sig)
        self.assertEqual(by_sig["hearth-dev"].run_ids, ["run-2"])

    def test_non_bash_tool_ignored_even_if_text_matches(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_non_bash_event(self.conn, "run-1", 1, "Read", "hearth-loom status")
        det = self._detector()
        self.assertEqual(det.run(), [])

    def test_signature_is_bare_skill_name(self):
        _insert_run(self.conn, "run-1", "proj-a", features={"skills": {}})
        _insert_bash_event(self.conn, "run-1", 1, "hearth-loom status")
        det = self._detector()
        candidates = det.run()
        self.assertEqual(candidates[0].signature, "hearth-loom-dispatch")

    def test_bypass_rate_partial(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_run(self.conn, "run-2", "synthetic",
                     features={"skills": {"hearth-loom-dispatch": {"skill_tool": 1}}})
        _insert_bash_event(self.conn, "run-1", 1, "hearth-loom status")
        _insert_bash_event(self.conn, "run-2", 1, "hearth-loom status")
        det = self._detector()
        candidates = det.run()
        by_sig = {c.signature: c for c in candidates}
        cand = by_sig["hearth-loom-dispatch"]
        self.assertEqual(cand.run_ids, ["run-1"])
        self.assertEqual(cand.extra["cli_used_runs"], 2)
        self.assertEqual(cand.extra["bypass_rate"], 0.5)

    def test_since_ts_excludes_earlier_runs(self):
        _insert_run(self.conn, "run-old", "synthetic",
                     started="2026-01-01T00:00:00Z", ended="2026-01-01T01:00:00Z",
                     features={"skills": {}})
        _insert_bash_event(self.conn, "run-old", 1, "hearth-loom status")
        det = self._detector()
        self.assertEqual(det.run(since_ts="2026-06-01T00:00:00Z"), [])

    def test_missing_skill_map_yields_no_candidates(self):
        _insert_run(self.conn, "run-1", "synthetic", features={"skills": {}})
        _insert_bash_event(self.conn, "run-1", 1, "hearth-loom status")
        det = SkillOpportunityDetector(self.conn)
        det.skill_map_path = Path("/no/such/skill_map.toml")
        self.assertEqual(det.run(), [])


if __name__ == "__main__":
    unittest.main()
