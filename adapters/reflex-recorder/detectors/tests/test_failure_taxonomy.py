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
    PLANNING_FAILURE_UNCONFIRMED_CADENCE,
    UNCLASSIFIED,
    CONFIRMED_TIER,
    UNCONFIRMED_CADENCE_TIER,
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

    def test_cadence_heuristic_alone_lands_in_unconfirmed_bucket_not_confirmed(self):
        # 2026-08-28 confidence-tier split: a run whose ONLY planning evidence
        # is the cadence heuristic (no confirmed thrashed/abandoned outcome,
        # no confirmed planning-detector hit) is NOT tagged PLANNING_FAILURE
        # (the confirmed tier) — it lands in its own, separately-scored
        # unconfirmed bucket.
        buckets = classify_run(set(), None, None, 0, THRASH_EVENT_COUNT_FLOOR)
        self.assertNotIn(PLANNING_FAILURE, buckets)
        self.assertIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, buckets)

    def test_cadence_heuristic_fires_for_labeled_non_planning_outcome_too(self):
        # A labeled-but-not-thrashed/abandoned outcome (e.g. 'reverted') is
        # not confirmed CLEAN, so the cadence heuristic still fires — and
        # since 'reverted' is not itself a confirmed planning-failure outcome,
        # it still lands in the unconfirmed-cadence bucket for the PLANNING
        # taxonomy (it separately drives verification_failure via
        # outcome=='reverted', tested elsewhere). Never gated on labeled_at.
        buckets = classify_run(set(), "reverted", "2026-01-01T00:00:00Z", 0, THRASH_EVENT_COUNT_FLOOR)
        self.assertNotIn(PLANNING_FAILURE, buckets)
        self.assertIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, buckets)

    def test_cadence_heuristic_suppressed_when_confirmed_clean(self):
        buckets = classify_run(set(), "clean", "2026-01-01T00:00:00Z", 0, THRASH_EVENT_COUNT_FLOOR)
        self.assertNotIn(PLANNING_FAILURE, buckets)
        self.assertNotIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, buckets)

    def test_below_cadence_floor_no_planning_failure(self):
        buckets = classify_run(set(), None, None, 0, THRASH_EVENT_COUNT_FLOOR - 1)
        self.assertNotIn(PLANNING_FAILURE, buckets)
        self.assertNotIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, buckets)

    def test_cadence_heuristic_folds_into_confirmed_bucket_when_backed_by_confirmed_reason(self):
        # A run that is BOTH confirmed-thrashed AND long-running lands only in
        # the confirmed PLANNING_FAILURE bucket — the cadence heuristic is
        # corroborating evidence there, never a separate/duplicate bucket.
        buckets = classify_run(
            set(), "thrashed", "2026-01-01T00:00:00Z", 0, THRASH_EVENT_COUNT_FLOOR
        )
        self.assertIn(PLANNING_FAILURE, buckets)
        self.assertNotIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, buckets)
        # cadence reason appended as corroborating evidence
        self.assertTrue(
            any("cadence heuristic" in r for r in buckets[PLANNING_FAILURE])
        )

    def test_confirmed_planning_reason_without_cadence_still_confirmed(self):
        buckets = classify_run({"red_baseline_dispatch"}, None, None, 0, 10)
        self.assertIn(PLANNING_FAILURE, buckets)
        self.assertNotIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, buckets)

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


class TestConfidenceTierSplitEndToEnd(unittest.TestCase):
    """2026-08-28 confidence-tier split — verifier-narrowed remediation.

    N unlabeled long runs (cadence-only) + M labeled-thrashed runs must
    produce TWO SEPARATE signatures/candidates for the planning bucket, each
    independently scored/prevalent, so the confirmed tier's score is never
    diluted by unconfirmed backlog and the unconfirmed tier's mass is
    reported (never dropped) but stamped as such.
    """

    def setUp(self):
        self.conn = _make_db()

    def _seed_mixed_backlog(self, n_unlabeled_long=6, m_labeled_thrashed=2):
        for i in range(n_unlabeled_long):
            _insert_run(
                self.conn, f"run-unlabeled-{i}", project="proj",
                outcome=None, labeled_at=None,
                event_count=THRASH_EVENT_COUNT_FLOOR + i,
            )
        for i in range(m_labeled_thrashed):
            _insert_run(
                self.conn, f"run-thrashed-{i}", project="proj",
                outcome="thrashed", labeled_at="2026-08-28T00:00:00Z",
                event_count=10,  # short run — confirmed via outcome alone
            )
        return n_unlabeled_long, m_labeled_thrashed

    def test_two_separate_candidates_confirmed_vs_unconfirmed(self):
        n, m = self._seed_mixed_backlog()
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()

        by_bucket = {c.extra["bucket"]: c for c in candidates}
        self.assertIn(PLANNING_FAILURE, by_bucket)
        self.assertIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, by_bucket)

        confirmed = by_bucket[PLANNING_FAILURE]
        unconfirmed = by_bucket[PLANNING_FAILURE_UNCONFIRMED_CADENCE]

        # Distinct signatures — never merged.
        self.assertNotEqual(confirmed.signature, unconfirmed.signature)

        # Confirmed tier holds exactly the M labeled-thrashed runs.
        self.assertEqual(sorted(confirmed.run_ids), sorted(f"run-thrashed-{i}" for i in range(m)))
        self.assertEqual(confirmed.extra["confidence_tier"], CONFIRMED_TIER)

        # Unconfirmed tier holds exactly the N unlabeled-long runs — the
        # backlog mass is present, not dropped.
        self.assertEqual(
            sorted(unconfirmed.run_ids),
            sorted(f"run-unlabeled-{i}" for i in range(n)),
        )
        self.assertEqual(unconfirmed.extra["confidence_tier"], UNCONFIRMED_CADENCE_TIER)

    def test_confirmed_tier_prevalence_not_diluted_by_unconfirmed_backlog(self):
        # 6 unlabeled-long + 2 labeled-thrashed = 8 runs total; without the
        # split, planning_failure's naive prevalence over ALL 8 would be
        # 100%. With the split, the CONFIRMED signature's prevalence is
        # computed over its own detector_hits (2 runs), independent of the
        # unconfirmed backlog's mass.
        self._seed_mixed_backlog(n_unlabeled_long=6, m_labeled_thrashed=2)
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        by_bucket = {c.extra["bucket"]: c for c in candidates}
        self.assertEqual(by_bucket[PLANNING_FAILURE].occurrences, 2)
        self.assertEqual(
            by_bucket[PLANNING_FAILURE_UNCONFIRMED_CADENCE].occurrences, 6
        )

    def test_run_confirmed_by_both_cadence_and_outcome_appears_only_in_confirmed(self):
        # A long-running run that is ALSO labeled thrashed must not appear in
        # the unconfirmed bucket — the cadence heuristic is corroborating
        # evidence for the confirmed bucket, not a second, duplicate hit.
        _insert_run(
            self.conn, "run-both", project="proj", outcome="thrashed",
            labeled_at="2026-08-28T00:00:00Z", event_count=THRASH_EVENT_COUNT_FLOOR,
        )
        detector = FailureTaxonomyDetector(self.conn)
        candidates = detector.run()
        by_bucket = {c.extra["bucket"]: c for c in candidates}
        self.assertIn("run-both", by_bucket[PLANNING_FAILURE].run_ids)
        self.assertNotIn(PLANNING_FAILURE_UNCONFIRMED_CADENCE, by_bucket)


if __name__ == "__main__":
    unittest.main()
