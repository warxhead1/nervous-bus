"""detectors/tests/test_detector_kind.py — the detector-vs-classifier split in
detectors/base.py and its application to failure_taxonomy.

A classifier labels EVERY run, so it must not become prevalence or issue
recurrence. The contract lives on the class (KIND), not in any reader's list of
detector names.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import (
    KIND_CLASSIFIER,
    KIND_DETECTOR,
    BaseDetector,
    PatternCandidate,
    classifier_detector_names,
    detector_kind,
    ensure_detector_schema,
)
from detectors.failure_taxonomy import (
    BUCKETS,
    PLANNING_FAILURE_UNCONFIRMED_CADENCE,
    THRASH_EVENT_COUNT_FLOOR,
    UNCLASSIFIED,
    FailureTaxonomyDetector,
)

_RUNS_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, project TEXT NOT NULL, outcome TEXT, labeled_at TEXT,
    event_count INTEGER NOT NULL DEFAULT 0, started TEXT NOT NULL, ended TEXT NOT NULL,
    close_reason TEXT
);
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    event_ts TEXT NOT NULL, event_type TEXT NOT NULL, raw_json TEXT NOT NULL
);
"""
_T = "2026-11-01T00:00:00Z"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _run(conn, run_id, project="proj", outcome=None, labeled_at=None, event_count=5):
    conn.execute(
        "INSERT INTO runs (run_id, project, outcome, labeled_at, event_count, started, ended, close_reason) "
        "VALUES (?,?,?,?,?,?,?,'idle_timeout')",
        (run_id, project, outcome, labeled_at, event_count, _T, _T),
    )


class _EveryRunDetector(BaseDetector):
    """Emits one candidate covering every run; the kind is set per subclass."""

    def detect(self, conn):
        ids = [r[0] for r in conn.execute("SELECT run_id FROM runs ORDER BY run_id")]
        return [PatternCandidate(
            project="proj", pattern_name=self.DETECTOR_NAME,
            signature=f"proj:{self.DETECTOR_NAME}:all", detector=self.DETECTOR_NAME,
            occurrences=len(ids), evidence=ids, run_ids=ids,
        )]


class _PlainEveryRun(_EveryRunDetector):
    DETECTOR_NAME = "kind_test_plain"


class _ClassifyEveryRun(_EveryRunDetector):
    DETECTOR_NAME = "kind_test_classifier"
    KIND = KIND_CLASSIFIER


class TestKindDeclaration(unittest.TestCase):
    def test_default_kind_is_detector_and_registered(self):
        self.assertEqual(_PlainEveryRun.KIND, KIND_DETECTOR)
        self.assertEqual(detector_kind(_PlainEveryRun.DETECTOR_NAME), KIND_DETECTOR)

    def test_classifier_subclass_registers_by_its_declared_kind(self):
        self.assertEqual(detector_kind(_ClassifyEveryRun.DETECTOR_NAME), KIND_CLASSIFIER)
        self.assertIn(_ClassifyEveryRun.DETECTOR_NAME, classifier_detector_names())
        self.assertNotIn(_PlainEveryRun.DETECTOR_NAME, classifier_detector_names())

    def test_unregistered_name_is_a_detector_never_hidden(self):
        self.assertEqual(detector_kind("never_seen_overlay_detector"), KIND_DETECTOR)

    def test_invalid_kind_is_rejected_when_the_class_is_defined(self):
        with self.assertRaises(ValueError):
            class _Bad(_EveryRunDetector):  # noqa: F841
                DETECTOR_NAME = "kind_test_bad"
                KIND = "analyzer"

    def test_db_only_reader_can_resolve_failure_taxonomy_without_importing_it(self):
        # query.py never imports a detector module; load_detector_kinds() must
        # be enough for a fresh interpreter to know failure_taxonomy's kind.
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from detectors.base import load_detector_kinds, detector_kind;"
            "before = detector_kind('failure_taxonomy');"
            "load_detector_kinds();"
            "print(before, detector_kind('failure_taxonomy'))"
        )
        out = subprocess.run(
            [sys.executable, "-I", "-c", code, str(_ADAPTER_ROOT)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [KIND_DETECTOR, KIND_CLASSIFIER])


class TestRunBookkeeping(unittest.TestCase):
    def setUp(self):
        self.conn = _db()
        for i in range(3):
            _run(self.conn, f"r{i}")

    def _counts(self, detector):
        hits = self.conn.execute(
            "SELECT COUNT(*) FROM detector_hits WHERE detector=?", (detector,)).fetchone()[0]
        issues = self.conn.execute(
            "SELECT COUNT(*) FROM issues WHERE detector=?", (detector,)).fetchone()[0]
        return hits, issues

    def test_detector_records_hits_and_an_issue(self):
        _PlainEveryRun(self.conn).run()
        self.assertEqual(self._counts(_PlainEveryRun.DETECTOR_NAME), (3, 1))

    def test_classifier_records_hits_but_never_an_issue(self):
        _ClassifyEveryRun(self.conn).run()
        _ClassifyEveryRun(self.conn).run()  # a second pass changes nothing
        self.assertEqual(self._counts(_ClassifyEveryRun.DETECTOR_NAME), (3, 0))


class TestBasePrevalence(unittest.TestCase):
    """BaseDetector.prevalence(): hits and runs share one window and one project."""

    def setUp(self):
        self.conn = _db()
        self.det = _PlainEveryRun(self.conn)

    def _run_ended(self, run_id, days_ago, project="proj"):
        ended = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.conn.execute(
            "INSERT INTO runs (run_id, project, event_count, started, ended, close_reason) "
            "VALUES (?,?,5,?,?,'idle_timeout')",
            (run_id, project, ended, ended),
        )

    def test_hit_recorded_late_for_an_old_run_does_not_count(self):
        # The run ended 30 days ago; a synthesis pass recorded its hit just now.
        self._run_ended("old", 30)
        self.det.record_hit("old", "sig", "proj")
        self._run_ended("new-a", 1)
        self._run_ended("new-b", 1)
        self.assertEqual(self.det.prevalence("proj", window_days=7), 0.0)

    def test_rate_counts_only_hits_of_runs_inside_the_window(self):
        self._run_ended("old", 30)
        self.det.record_hit("old", "sig", "proj")
        self._run_ended("new-hit", 1)
        self.det.record_hit("new-hit", "sig", "proj")
        self._run_ended("new-clean", 1)
        self.assertAlmostEqual(self.det.prevalence("proj", window_days=7), 0.5)

    def test_a_hit_with_no_run_row_never_counts(self):
        self._run_ended("real", 1)
        self.det.record_hit("ghost-run", "sig", "proj")
        self.assertEqual(self.det.prevalence("proj", window_days=7), 0.0)

    def test_classifier_rate_is_zero_where_a_detector_over_the_same_runs_is_not(self):
        self._run_ended("r1", 1)
        self._run_ended("r2", 1)
        _PlainEveryRun(self.conn).run()
        _ClassifyEveryRun(self.conn).run()
        self.assertEqual(_PlainEveryRun(self.conn).prevalence("proj", window_days=7), 1.0)
        self.assertEqual(_ClassifyEveryRun(self.conn).prevalence("proj", window_days=7), 0.0)


class TestFailureTaxonomyIsAClassifier(unittest.TestCase):
    def setUp(self):
        self.conn = _db()

    def test_declared_kind(self):
        self.assertEqual(FailureTaxonomyDetector.KIND, KIND_CLASSIFIER)
        self.assertIn("failure_taxonomy", classifier_detector_names())

    def test_tags_every_run_without_creating_issues(self):
        _run(self.conn, "plain")                                           # no signal
        _run(self.conn, "thrash", outcome="thrashed", labeled_at=_T)       # planning_failure
        FailureTaxonomyDetector(self.conn).run()
        tagged = {r[0] for r in self.conn.execute(
            "SELECT run_id FROM detector_hits WHERE detector='failure_taxonomy'")}
        self.assertEqual(tagged, {"plain", "thrash"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 0)

    def test_classified_flag_is_true_only_for_canonical_buckets(self):
        _run(self.conn, "plain")
        _run(self.conn, "thrash", outcome="thrashed", labeled_at=_T)
        _run(self.conn, "long", event_count=THRASH_EVENT_COUNT_FLOOR)     # cadence only
        by_bucket = {c.extra["bucket"]: c for c in FailureTaxonomyDetector(self.conn).run()}
        self.assertTrue(by_bucket["planning_failure"].extra["classified"])
        self.assertFalse(by_bucket[UNCLASSIFIED].extra["classified"])
        self.assertFalse(by_bucket[PLANNING_FAILURE_UNCONFIRMED_CADENCE].extra["classified"])
        for bucket, cand in by_bucket.items():
            self.assertEqual(cand.extra["classified"], bucket in BUCKETS)


if __name__ == "__main__":
    unittest.main()
