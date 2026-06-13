"""tests/test_base.py — Unit tests for detectors/base.py.

Covers:
  - ensure_detector_schema creates tables
  - record_hit writes to detector_hits
  - prevalence query (hits/total, rolling window, zero-division)
  - find_or_create_issue dedup + recurrence_count increment
  - recurrence_count_at_apply not modified by find_or_create_issue
  - emit_candidate shape
  - BaseDetector.run() orchestrates hit recording + issue dedup
"""
import sqlite3
import sys
import time
import unittest
from pathlib import Path

# Ensure the adapter root is on sys.path so we can import store + detectors.
_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import (
    BaseDetector,
    PatternCandidate,
    ensure_detector_schema,
    _now_utc,
    _days_ago_utc,
)


# ── Minimal in-memory DB with the full runs + detector schema ─────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    worktree    TEXT,
    worktree_slug TEXT,
    git_branch  TEXT,
    bead_id     TEXT,
    outcome     TEXT,
    ended       TEXT NOT NULL,
    close_reason TEXT
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project, ended, worktree=None, outcome=None):
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, project, ended, worktree, outcome) VALUES (?,?,?,?,?)",
        (run_id, project, ended, worktree, outcome),
    )


# ── Concrete test detector ────────────────────────────────────────────────────

class _NullDetector(BaseDetector):
    """Always returns one candidate with a fixed signature."""
    DETECTOR_NAME = "null_test_detector"
    _candidates: list[PatternCandidate] = []

    def detect(self, conn):
        return self._candidates


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEnsureSchema(unittest.TestCase):
    def test_tables_created(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        ensure_detector_schema(conn)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("detector_hits", tables)
        self.assertIn("issues", tables)

    def test_idempotent(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        ensure_detector_schema(conn)
        ensure_detector_schema(conn)  # must not raise
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("detector_hits", tables)


class TestRecordHit(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.det = _NullDetector(self.conn)

    def test_record_hit_inserts_row(self):
        self.det.record_hit("run-001", "sig-abc", "myproject")
        rows = self.conn.execute("SELECT * FROM detector_hits").fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # columns: id, run_id, detector, signature, project, ts
        self.assertEqual(row[1], "run-001")
        self.assertEqual(row[2], "null_test_detector")
        self.assertEqual(row[3], "sig-abc")
        self.assertEqual(row[4], "myproject")

    def test_multiple_hits(self):
        self.det.record_hit("run-001", "sig-abc", "p1")
        self.det.record_hit("run-002", "sig-abc", "p1")
        self.det.record_hit("run-003", "sig-xyz", "p2")
        count = self.conn.execute("SELECT COUNT(*) FROM detector_hits").fetchone()[0]
        self.assertEqual(count, 3)


class TestPrevalence(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.det = _NullDetector(self.conn)

    def test_zero_runs_returns_zero(self):
        rate = self.det.prevalence("ghost-project", window_days=7)
        self.assertEqual(rate, 0.0)

    def test_all_runs_hit(self):
        # 3 runs, 3 hits → 100%
        for i in range(3):
            _insert_run(self.conn, f"r{i}", "proj", _now_utc())
            self.det.record_hit(f"r{i}", "sig", "proj")
        rate = self.det.prevalence("proj", window_days=7)
        self.assertAlmostEqual(rate, 1.0)

    def test_partial_hit_rate(self):
        # 4 runs, 2 hits → 50%
        for i in range(4):
            _insert_run(self.conn, f"r{i}", "proj2", _now_utc())
        for i in range(2):
            self.det.record_hit(f"r{i}", "sig", "proj2")
        rate = self.det.prevalence("proj2", window_days=7)
        self.assertAlmostEqual(rate, 0.5)

    def test_window_excludes_old_runs(self):
        # Insert a run dated 30 days ago — should be excluded from 7-day window.
        old_ts = _days_ago_utc(30)
        _insert_run(self.conn, "old-run", "projX", old_ts)
        self.det.record_hit("old-run", "sig", "projX", ts=old_ts)
        # Recent run with no hit
        _insert_run(self.conn, "new-run", "projX", _now_utc())
        rate = self.det.prevalence("projX", window_days=7)
        # Window = last 7 days: 1 run total, 0 hits in window → 0%
        self.assertAlmostEqual(rate, 0.0)


class TestFindOrCreateIssue(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.det = _NullDetector(self.conn)

    def test_creates_on_first_call(self):
        issue = self.det.find_or_create_issue("sig-1", "proj", ["ev-a"])
        self.assertEqual(issue["recurrence_count"], 1)
        self.assertEqual(issue["project"], "proj")
        self.assertIsNone(issue["recurrence_count_at_apply"])

    def test_increments_on_second_call(self):
        self.det.find_or_create_issue("sig-2", "proj", ["ev-a"])
        issue = self.det.find_or_create_issue("sig-2", "proj", ["ev-a", "ev-b"])
        self.assertEqual(issue["recurrence_count"], 2)

    def test_recurrence_count_at_apply_not_touched(self):
        self.det.find_or_create_issue("sig-3", "proj", [])
        # Manually set recurrence_count_at_apply (simulating a fix applied)
        self.conn.execute(
            "UPDATE issues SET recurrence_count_at_apply = 1 WHERE signature = 'sig-3'"
        )
        # Fire again — recurrence_count_at_apply must not change
        issue = self.det.find_or_create_issue("sig-3", "proj", ["post-fix-hit"])
        self.assertEqual(issue["recurrence_count_at_apply"], 1)
        self.assertEqual(issue["recurrence_count"], 2)  # incremented

    def test_get_issue_none_for_missing(self):
        result = self.det.get_issue("nonexistent-sig")
        self.assertIsNone(result)

    def test_get_issue_returns_row(self):
        self.det.find_or_create_issue("sig-4", "proj", ["ev"])
        issue = self.det.get_issue("sig-4")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["signature"], "sig-4")

    def test_different_signatures_independent(self):
        self.det.find_or_create_issue("sig-A", "proj", [])
        self.det.find_or_create_issue("sig-B", "proj", [])
        self.det.find_or_create_issue("sig-A", "proj", [])
        count_a = self.conn.execute(
            "SELECT recurrence_count FROM issues WHERE signature='sig-A'"
        ).fetchone()[0]
        count_b = self.conn.execute(
            "SELECT recurrence_count FROM issues WHERE signature='sig-B'"
        ).fetchone()[0]
        self.assertEqual(count_a, 2)
        self.assertEqual(count_b, 1)


class TestEmitCandidate(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()
        self.det = _NullDetector(self.conn)

    def test_emit_required_fields(self):
        c = PatternCandidate(
            project="proj",
            pattern_name="test_pattern",
            signature="sig-emit",
            detector="null_test_detector",
            occurrences=3,
            evidence=["ev-1", "ev-2"],
        )
        payload = self.det.emit_candidate(c)
        self.assertEqual(payload["project"], "proj")
        self.assertEqual(payload["pattern_name"], "test_pattern")
        self.assertEqual(payload["occurrences"], 3)
        self.assertEqual(payload["evidence"], ["ev-1", "ev-2"])
        self.assertEqual(payload["signature"], "sig-emit")

    def test_emit_with_remediation(self):
        c = PatternCandidate(
            project="proj",
            pattern_name="tp",
            signature="sig",
            detector="null_test_detector",
            occurrences=1,
            evidence=[],
            proposed_remediation="run git worktree remove ...",
        )
        payload = self.det.emit_candidate(c)
        self.assertIn("proposed_remediation", payload)

    def test_emit_extra_fields_merged(self):
        c = PatternCandidate(
            project="proj",
            pattern_name="tp",
            signature="sig",
            detector="null_test_detector",
            occurrences=1,
            evidence=[],
            extra={"custom_field": "hello"},
        )
        payload = self.det.emit_candidate(c)
        self.assertEqual(payload["custom_field"], "hello")


class TestRunOrchestration(unittest.TestCase):
    """BaseDetector.run() wires detect() → record_hit() → find_or_create_issue()."""

    def setUp(self):
        self.conn = _make_db()

    def test_run_records_hits_and_creates_issue(self):
        class _OneHitDetector(BaseDetector):
            DETECTOR_NAME = "one_hit"
            def detect(self, conn):
                return [PatternCandidate(
                    project="p",
                    pattern_name="pat",
                    signature="sig-run",
                    detector="one_hit",
                    occurrences=1,
                    evidence=["ev"],
                    run_ids=["run-r1"],
                )]

        det = _OneHitDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)

        hits = self.conn.execute("SELECT * FROM detector_hits").fetchall()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], "run-r1")  # run_id

        issue = det.get_issue("sig-run")
        self.assertIsNotNone(issue)
        self.assertEqual(issue["recurrence_count"], 1)

    def test_run_increments_recurrence_on_repeat(self):
        class _RepeatDetector(BaseDetector):
            DETECTOR_NAME = "repeat_det"
            def detect(self, conn):
                return [PatternCandidate(
                    project="p",
                    pattern_name="pat",
                    signature="sig-repeat",
                    detector="repeat_det",
                    occurrences=1,
                    evidence=["ev"],
                    run_ids=["run-x"],
                )]

        det = _RepeatDetector(self.conn)
        det.run()
        det.run()
        issue = det.get_issue("sig-repeat")
        self.assertEqual(issue["recurrence_count"], 2)


if __name__ == "__main__":
    unittest.main()
