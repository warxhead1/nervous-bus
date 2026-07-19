"""tests/test_harness_change_watch.py — Unit tests for HarnessChangeWatchDetector
(A1 in the harness-engineering-adoption-map, Part 2 Tier 1).

Covers:
  - Positive: an Edit touching CLAUDE.md fires with label "CLAUDE.md".
  - Positive: a git Bash call whose stdout mentions a harness path (the
    "diff surface" signal) fires with via="git_diff_surface".
  - Negative: ordinary file edits + non-git Bash calls never fire
    (the "no-harness-change" acceptance case).
  - Cross-run aggregation: the same harness artifact touched in two different
    runs shares ONE signature and both run_ids are recorded.
  - Signature stability: no run_id/timestamp embedded.
  - Remediation rung is "inform" (purely observational, no gate).
  - A run touching TWO distinct harness artifacts produces two candidates.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.harness_change_watch import (
    HarnessChangeWatchDetector,
    classify_harness_text,
)


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    session_id   TEXT,
    started      TEXT NOT NULL,
    ended        TEXT NOT NULL,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_run_events_seq ON run_events(run_id, seq);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project="proj", session_id="sess-1", close_reason="idle_timeout"):
    now = _now_utc()
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, project, session_id, started, ended, close_reason)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, project, session_id, now, now, close_reason),
    )


def _insert_event(conn, run_id, seq, tool_name, tool_summary=None,
                   tool_response_summary=None, project="proj", cwd=""):
    envelope = {
        "specversion": "1.0",
        "id": f"evt-{run_id}-{seq}",
        "source": "/test/source",
        "type": "bus.agent.activity.v1",
        "data": {
            "agent_kind": "host_claude_code",
            "session_id": "sess-1",
            "project": project,
            "tool_name": tool_name,
            "tool_summary": tool_summary or "",
            "tool_response_summary": tool_response_summary or "",
            "cwd": cwd,
            "event": "tool_call",
            "ts": _now_utc(),
        },
    }
    conn.execute(
        """INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, seq, _now_utc(), "bus.agent.activity.v1", json.dumps(envelope)),
    )


class TestClassifyHarnessText(unittest.TestCase):
    def test_claude_md(self):
        self.assertEqual(classify_harness_text("/home/eric/projects/foo/CLAUDE.md"), "CLAUDE.md")

    def test_agents_md(self):
        self.assertEqual(classify_harness_text("/repo/AGENTS.md"), "AGENTS.md")

    def test_hooks(self):
        self.assertEqual(
            classify_harness_text("/repo/.claude/hooks/pre_commit.py"), ".claude/hooks"
        )

    def test_skills(self):
        self.assertEqual(
            classify_harness_text("/repo/.claude/skills/foo/SKILL.md"), ".claude/skills"
        )

    def test_settings_json(self):
        self.assertEqual(
            classify_harness_text("/repo/.claude/settings.json"), "settings.json"
        )
        self.assertEqual(
            classify_harness_text("/repo/.claude/settings.local.json"), "settings.json"
        )

    def test_hermes_routing(self):
        self.assertEqual(
            classify_harness_text("/home/eric/.config/hermes/routing.toml"),
            "hermes/routing.toml",
        )

    def test_no_match(self):
        self.assertIsNone(classify_harness_text("/repo/src/main.py"))
        self.assertIsNone(classify_harness_text(""))
        self.assertIsNone(classify_harness_text(None))

    def test_unrelated_settings_json_not_matched(self):
        # A project's OWN settings.json (not under .claude/) should not fire —
        # narrows the harness-artifact match to the claude-config path.
        self.assertIsNone(classify_harness_text("/repo/config/settings.json"))


class TestHarnessChangeWatchDetector(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()

    def test_edit_claude_md_fires(self):
        _insert_run(self.conn, "run-1", project="proj", session_id="sess-A")
        _insert_event(
            self.conn, "run-1", 1, "Edit",
            tool_summary=json.dumps({"file_path": "/repo/CLAUDE.md", "old_string": "x", "new_string": "y"}),
        )
        detector = HarnessChangeWatchDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.extra["harness_artifact"], "CLAUDE.md")
        self.assertIn("run-1", c.run_ids)
        self.assertEqual(c.extra["remediation_rung"], "inform")
        self.assertNotIn("run-1", c.signature)  # signature must not embed run_id

    def test_bash_git_diff_surface_fires(self):
        _insert_run(self.conn, "run-2", project="proj", session_id="sess-B")
        _insert_event(
            self.conn, "run-2", 1, "Bash",
            tool_summary=json.dumps({"command": "git status", "description": "check status"}),
            tool_response_summary="M  AGENTS.md\n?? foo.py",
        )
        detector = HarnessChangeWatchDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.extra["harness_artifact"], "AGENTS.md")

    def test_no_harness_change_no_fire(self):
        """Acceptance gate: ordinary edits + non-git bash never fire."""
        _insert_run(self.conn, "run-3", project="proj")
        _insert_event(
            self.conn, "run-3", 1, "Edit",
            tool_summary=json.dumps({"file_path": "/repo/src/main.py"}),
        )
        _insert_event(
            self.conn, "run-3", 2, "Bash",
            tool_summary=json.dumps({"command": "npm test", "description": "run tests"}),
            tool_response_summary="12 passing",
        )
        detector = HarnessChangeWatchDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(candidates, [])

    def test_non_git_bash_mentioning_claude_md_does_not_fire(self):
        """A non-git command that happens to print 'CLAUDE.md' should NOT fire —
        the Bash path is gated on the command itself being a git operation."""
        _insert_run(self.conn, "run-4", project="proj")
        _insert_event(
            self.conn, "run-4", 1, "Bash",
            tool_summary=json.dumps({"command": "ls", "description": "list files"}),
            tool_response_summary="CLAUDE.md\nsrc/",
        )
        detector = HarnessChangeWatchDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(candidates, [])

    def test_cross_run_aggregation_same_signature(self):
        _insert_run(self.conn, "run-5", project="proj", session_id="sess-A")
        _insert_run(self.conn, "run-6", project="proj", session_id="sess-B")
        for run_id in ("run-5", "run-6"):
            _insert_event(
                self.conn, run_id, 1, "Edit",
                tool_summary=json.dumps({"file_path": "/repo/CLAUDE.md"}),
            )
        detector = HarnessChangeWatchDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(sorted(c.run_ids), ["run-5", "run-6"])
        self.assertEqual(c.occurrences, 2)

    def test_two_distinct_artifacts_two_candidates(self):
        _insert_run(self.conn, "run-7", project="proj")
        _insert_event(
            self.conn, "run-7", 1, "Edit",
            tool_summary=json.dumps({"file_path": "/repo/CLAUDE.md"}),
        )
        _insert_event(
            self.conn, "run-7", 2, "Edit",
            tool_summary=json.dumps({"file_path": "/repo/.claude/hooks/pre.py"}),
        )
        detector = HarnessChangeWatchDetector(self.conn)
        candidates = detector.run()
        labels = sorted(c.extra["harness_artifact"] for c in candidates)
        self.assertEqual(labels, [".claude/hooks", "CLAUDE.md"])

    def test_recurrence_persisted_in_issues_table(self):
        _insert_run(self.conn, "run-8", project="proj")
        _insert_event(
            self.conn, "run-8", 1, "Edit",
            tool_summary=json.dumps({"file_path": "/repo/CLAUDE.md"}),
        )
        detector = HarnessChangeWatchDetector(self.conn)
        detector.run()
        issue = detector.get_issue("proj:harness_change_watch:CLAUDE.md")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["recurrence_count"], 1)


if __name__ == "__main__":
    unittest.main()
