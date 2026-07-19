"""tests/test_failure_taxonomy.py — Unit tests for FailureTaxonomyDetector
(A2 in the harness-engineering-adoption-map, Part 2 Tier 1).

Covers:
  - classify_run() pure-function unit tests for each of the four buckets.
  - The `unclassified` acceptance case: a run with none of the mapped signals
    is tagged unclassified, not forced into a bucket.
  - Multi-label: a run can match more than one bucket at once.
  - End-to-end detect(): reads OTHER detectors' hits from detector_hits (this
    detector is registered last in synthesis.py for exactly that reason),
    permission_requested events from run_events, and outcome/event_count from
    runs — all pre-existing signals, no new capture.
  - Cross-run aggregation per (project, bucket); signature has no run_id.
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
from detectors.failure_taxonomy import (
    FailureTaxonomyDetector,
    classify_run,
    CONTEXT_FAILURE,
    CONSTRAINT_FAILURE,
    VERIFICATION_FAILURE,
    PLANNING_FAILURE,
    UNCLASSIFIED,
    THRASH_EVENT_COUNT_FLOOR,
)


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    outcome      TEXT,
    labeled_at   TEXT,
    event_count  INTEGER NOT NULL DEFAULT 0,
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
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project="proj", outcome=None, labeled_at=None,
                 event_count=10, close_reason="idle_timeout"):
    now = _now_utc()
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, project, outcome, labeled_at, event_count, started, ended, close_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, project, outcome, labeled_at, event_count, now, now, close_reason),
    )


def _seed_detector_hit(conn, run_id, detector, project="proj", signature=None):
    """Directly seed a detector_hits row, simulating an upstream detector
    (reread_same_file, unverified_completion, etc.) having already run this pass."""
    sig = signature or f"{project}:{detector}:seed"
    conn.execute(
        "INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?, ?, ?, ?, ?)",
        (run_id, detector, sig, project, _now_utc()),
    )


def _insert_permission_event(conn, run_id, seq=1):
    envelope = {"tool_summary": "allow Bash(npm test)"}
    conn.execute(
        """INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
           VALUES (?, ?, ?, ?, ?)""",
        (run_id, seq, _now_utc(), "permission_requested", json.dumps(envelope)),
    )


class TestClassifyRunPure(unittest.TestCase):
    def test_context_failure_from_reread(self):
        buckets = classify_run({"reread_same_file"}, None, None, 0, 10)
        self.assertIn(CONTEXT_FAILURE, buckets)
        self.assertEqual(len(buckets), 1)

    def test_context_failure_from_directive_mismatch(self):
        buckets = classify_run({"directive_ground_truth_mismatch"}, None, None, 0, 10)
        self.assertIn(CONTEXT_FAILURE, buckets)

    def test_context_failure_from_repeated_question(self):
        buckets = classify_run({"repeated_question"}, None, None, 0, 10)
        self.assertIn(CONTEXT_FAILURE, buckets)

    def test_constraint_failure_from_permission_count(self):
        buckets = classify_run(set(), None, None, 3, 10)
        self.assertIn(CONSTRAINT_FAILURE, buckets)
        self.assertIn("3 permission_requested event(s)", buckets[CONSTRAINT_FAILURE][0])

    def test_verification_failure_from_unverified_completion(self):
        buckets = classify_run({"unverified_completion"}, None, None, 0, 10)
        self.assertIn(VERIFICATION_FAILURE, buckets)

    def test_verification_failure_from_edit_build_fail_revert(self):
        buckets = classify_run({"edit_build_fail_revert"}, None, None, 0, 10)
        self.assertIn(VERIFICATION_FAILURE, buckets)

    def test_verification_failure_from_reverted_outcome(self):
        buckets = classify_run(set(), "reverted", "2026-01-01T00:00:00Z", 0, 10)
        self.assertIn(VERIFICATION_FAILURE, buckets)

    def test_unlabeled_reverted_like_outcome_does_not_count(self):
        # labeled_at is None -> NOT-YET-LABELED, must never be trusted as reverted.
        buckets = classify_run(set(), "reverted", None, 0, 10)
        self.assertNotIn(VERIFICATION_FAILURE, buckets)

    def test_planning_failure_from_thrashed_outcome(self):
        buckets = classify_run(set(), "thrashed", "2026-01-01T00:00:00Z", 0, 10)
        self.assertIn(PLANNING_FAILURE, buckets)

    def test_planning_failure_from_abandoned_outcome(self):
        buckets = classify_run(set(), "abandoned", "2026-01-01T00:00:00Z", 0, 10)
        self.assertIn(PLANNING_FAILURE, buckets)

    def test_planning_failure_from_red_baseline_dispatch(self):
        buckets = classify_run({"red_baseline_dispatch"}, None, None, 0, 10)
        self.assertIn(PLANNING_FAILURE, buckets)

    def test_planning_failure_from_inherited_rationalization(self):
        buckets = classify_run({"inherited_rationalization"}, None, None, 0, 10)
        self.assertIn(PLANNING_FAILURE, buckets)

    def test_planning_failure_from_cadence_heuristic_when_not_confirmed_clean(self):
        buckets = classify_run(set(), None, None, 0, THRASH_EVENT_COUNT_FLOOR)
        self.assertIn(PLANNING_FAILURE, buckets)

    def test_cadence_heuristic_suppressed_when_confirmed_clean(self):
        buckets = classify_run(set(), "clean", "2026-01-01T00:00:00Z", 0, THRASH_EVENT_COUNT_FLOOR)
        self.assertNotIn(PLANNING_FAILURE, buckets)

    def test_below_cadence_floor_no_planning_failure(self):
        buckets = classify_run(set(), None, None, 0, THRASH_EVENT_COUNT_FLOOR - 1)
        self.assertNotIn(PLANNING_FAILURE, buckets)

    def test_unclassified_when_nothing_matches(self):
        buckets = classify_run(set(), "clean", "2026-01-01T00:00:00Z", 0, 10)
        self.assertEqual(buckets, {})

    def test_multi_label(self):
        buckets = classify_run({"reread_same_file"}, "reverted", "2026-01-01T00:00:00Z", 0, 10)
        self.assertIn(CONTEXT_FAILURE, buckets)
        self.assertIn(VERIFICATION_FAILURE, buckets)
        self.assertEqual(len(buckets), 2)


class TestFailureTaxonomyDetectorEndToEnd(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()

    def test_unclassified_case(self):
        """Acceptance gate: a run with no mapped signal is tagged unclassified,
        never forced into one of the four buckets."""
        _insert_run(self.conn, "run-u1", project="proj", outcome="clean",
                    labeled_at="2026-01-01T00:00:00Z", event_count=5)
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["bucket"], UNCLASSIFIED)
        self.assertIn("run-u1", candidates[0].run_ids)

    def test_verification_failure_end_to_end(self):
        _insert_run(self.conn, "run-v1", project="proj", outcome="reverted",
                    labeled_at="2026-01-01T00:00:00Z", event_count=5)
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["bucket"], VERIFICATION_FAILURE)

    def test_constraint_failure_reads_permission_requested_events(self):
        _insert_run(self.conn, "run-c1", project="proj", event_count=5)
        _insert_permission_event(self.conn, "run-c1")
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["bucket"], CONSTRAINT_FAILURE)

    def test_context_failure_reads_upstream_detector_hits(self):
        _insert_run(self.conn, "run-x1", project="proj", event_count=5)
        _seed_detector_hit(self.conn, "run-x1", "reread_same_file", project="proj")
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["bucket"], CONTEXT_FAILURE)

    def test_multi_label_produces_two_candidates_same_run(self):
        _insert_run(self.conn, "run-m1", project="proj", outcome="thrashed",
                    labeled_at="2026-01-01T00:00:00Z", event_count=5)
        _seed_detector_hit(self.conn, "run-m1", "repeated_question", project="proj")
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        buckets = sorted(c.extra["bucket"] for c in candidates)
        self.assertEqual(buckets, [CONTEXT_FAILURE, PLANNING_FAILURE])
        for c in candidates:
            self.assertIn("run-m1", c.run_ids)

    def test_signature_has_no_run_id(self):
        _insert_run(self.conn, "run-s1", project="proj", outcome="thrashed",
                    labeled_at="2026-01-01T00:00:00Z")
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(candidates[0].signature, "proj:failure_taxonomy:planning_failure")

    def test_cross_run_aggregation_same_bucket(self):
        _insert_run(self.conn, "run-a1", project="proj", outcome="abandoned",
                    labeled_at="2026-01-01T00:00:00Z")
        _insert_run(self.conn, "run-a2", project="proj", outcome="thrashed",
                    labeled_at="2026-01-01T00:00:00Z")
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].extra["bucket"], PLANNING_FAILURE)
        self.assertEqual(sorted(candidates[0].run_ids), ["run-a1", "run-a2"])

    def test_remediation_rung_is_inform(self):
        _insert_run(self.conn, "run-i1", project="proj")
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        self.assertEqual(candidates[0].extra["remediation_rung"], "inform")


if __name__ == "__main__":
    unittest.main()
