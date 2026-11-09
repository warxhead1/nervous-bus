"""tests/test_query_digest.py — query.py prevalence / issues / classifications.

Covers the digest-reader contract:
  - prevalence numerator and denominator share one window and one project
    attribution (the run's), so hit_runs can never exceed total_runs;
  - classifier-kind detectors (detectors.base.KIND_CLASSIFIER) tag every run
    and are neither prevalence nor issue recurrence;
  - an implausibly high hit rate is flagged suspect_calibration, not ranked.

In-memory fixtures only — never the live runs.db.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_REC_DIR = Path(__file__).parent.parent
for _p in (_REC_DIR, Path(__file__).parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from detectors.base import KIND_CLASSIFIER, BaseDetector
from detectors.failure_taxonomy import (
    CONTEXT_FAILURE,
    PLANNING_FAILURE,
    PLANNING_FAILURE_UNCONFIRMED_CADENCE,
    UNCLASSIFIED,
)
from query import (
    DEFAULT_MAX_PLAUSIBLE_RATE,
    DEFAULT_MIN_SUSPECT_RUNS,
    main,
    query_classifications,
    query_issues,
    query_prevalence,
)
from test_query import _RUNS_DDL, _insert_hit, _insert_issue, _insert_run, _make_db, _ts


class _ToyClassifier(BaseDetector):
    """A classifier that is NOT failure_taxonomy: proves exclusion is driven by
    the declared kind, not by a detector name."""

    DETECTOR_NAME = "toy_classifier_for_query_tests"
    KIND = KIND_CLASSIFIER

    def detect(self, conn):  # pragma: no cover - never run
        return []


def _runs(conn: sqlite3.Connection, project: str, n: int, prefix: str,
          ended: str | None = None) -> list[str]:
    ids = [f"{prefix}-{i}" for i in range(n)]
    for rid in ids:
        _insert_run(conn, rid, project, ended=ended or _ts(0, 1), started=ended or _ts(0, 2))
    return ids


class TestPrevalenceWindowAlignment(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def test_late_recorded_hit_for_run_outside_window_is_not_a_zero_denominator_row(self) -> None:
        # The run ended 20 days ago; synthesis only RECORDED its hit 3 days ago.
        # With a 7-day window the run is outside it, so there is nothing to rate.
        _insert_run(self.conn, "old-run", "app", started=_ts(20, 1), ended=_ts(20))
        _insert_hit(self.conn, "old-run", "unverified_completion", "app:unverified_completion:x",
                    "app", ts=_ts(3))
        self.assertEqual(query_prevalence(self.conn, days=7), [])

    def test_hit_runs_never_exceed_total_runs(self) -> None:
        in_window = _runs(self.conn, "alpha", 4, "a")
        # Late-recorded hits on in-window runs, a hit whose detector-side project
        # disagrees with the run's, and an orphan hit with no runs row at all.
        for rid in in_window[:3]:
            _insert_hit(self.conn, rid, "reread_same_file", f"alpha:reread_same_file:{rid}",
                        "alpha", ts=_ts(0))
        _insert_hit(self.conn, in_window[3], "reread_same_file", "beta:reread_same_file:z",
                    "beta", ts=_ts(0))
        _insert_hit(self.conn, "no-such-run", "reread_same_file", "alpha:reread_same_file:o",
                    "alpha", ts=_ts(0))
        rows = query_prevalence(self.conn, days=7)
        self.assertTrue(rows)
        for r in rows:
            self.assertGreater(r["total_runs"], 0, r)
            self.assertLessEqual(r["hit_runs"], r["total_runs"], r)
            self.assertLessEqual(r["rate"], 1.0, r)
        by_key = {(r["project"], r["detector"]): r for r in rows}
        # The mismatched-project hit is attributed to the project whose runs it is.
        self.assertEqual(by_key[("alpha", "reread_same_file")]["hit_runs"], 4)
        self.assertNotIn("beta", {r["project"] for r in rows})

    def test_project_filter_uses_the_runs_project(self) -> None:
        rid = _runs(self.conn, "alpha", 2, "a")[0]
        _insert_hit(self.conn, rid, "reread_same_file", "beta:reread_same_file:z", "beta")
        rows = query_prevalence(self.conn, project="alpha", days=7)
        self.assertEqual([(r["project"], r["hit_runs"], r["total_runs"]) for r in rows],
                         [("alpha", 1, 2)])


class TestPrevalenceClassifiers(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()
        self.ids = _runs(self.conn, "alpha", 6, "a")
        for rid in self.ids[:2]:
            _insert_hit(self.conn, rid, "worktree_leak", "alpha:worktree_leak:x", "alpha")
        for rid in self.ids:  # a classifier tags every run
            _insert_hit(self.conn, rid, "failure_taxonomy",
                        f"alpha:failure_taxonomy:{UNCLASSIFIED}", "alpha")
            _insert_hit(self.conn, rid, _ToyClassifier.DETECTOR_NAME,
                        "alpha:toy_classifier_for_query_tests:t", "alpha")

    def tearDown(self) -> None:
        self.conn.close()

    def test_classifier_rows_are_not_prevalence(self) -> None:
        rows = query_prevalence(self.conn, days=7)
        self.assertEqual([r["detector"] for r in rows], ["worktree_leak"])
        self.assertEqual(rows[0]["kind"], "detector")

    def test_include_classifiers_lists_them_with_kind_and_never_suspect(self) -> None:
        rows = query_prevalence(self.conn, days=7, include_classifiers=True,
                                min_suspect_runs=1)
        by_det = {r["detector"]: r for r in rows}
        self.assertEqual(by_det["failure_taxonomy"]["kind"], "classifier")
        self.assertEqual(by_det[_ToyClassifier.DETECTOR_NAME]["kind"], "classifier")
        self.assertEqual(by_det["failure_taxonomy"]["rate"], 1.0)
        self.assertFalse(by_det["failure_taxonomy"]["suspect_calibration"])


class TestSuspectCalibration(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def _seed(self, project: str, n_runs: int, n_hit: int, detector: str = "unverified_completion") -> None:
        ids = _runs(self.conn, project, n_runs, project)
        for rid in ids[:n_hit]:
            _insert_hit(self.conn, rid, detector, f"{project}:{detector}:s", project)

    def _flag(self, project: str, **kw) -> bool:
        rows = [r for r in query_prevalence(self.conn, days=7, **kw) if r["project"] == project]
        self.assertEqual(len(rows), 1)
        return rows[0]["suspect_calibration"]

    def test_rate_above_bound_over_enough_runs_is_flagged(self) -> None:
        n = DEFAULT_MIN_SUSPECT_RUNS
        hits = int(n * DEFAULT_MAX_PLAUSIBLE_RATE) + 1
        self._seed("big", n, hits)
        self.assertTrue(self._flag("big"))

    def test_rate_at_or_below_bound_is_not_flagged(self) -> None:
        n = DEFAULT_MIN_SUSPECT_RUNS
        self._seed("ok", n, int(n * DEFAULT_MAX_PLAUSIBLE_RATE))
        self.assertFalse(self._flag("ok"))

    def test_tiny_sample_at_100_percent_is_not_flagged(self) -> None:
        self._seed("tiny", 1, 1)
        self.assertFalse(self._flag("tiny"))

    def test_bound_and_sample_floor_are_configurable(self) -> None:
        self._seed("cfg", 10, 6)
        self.assertFalse(self._flag("cfg"))  # under the default sample floor
        self.assertTrue(self._flag("cfg", min_suspect_runs=10, max_plausible_rate=0.5))
        self.assertFalse(self._flag("cfg", min_suspect_runs=10, max_plausible_rate=0.9))

    def test_cli_json_carries_the_flag_and_honours_the_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "runs.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.executescript(_RUNS_DDL)
            for rid in (f"c-{i}" for i in range(10)):
                _insert_run(conn, rid, "cli", ended=_ts(0, 1))
            for i in range(8):
                _insert_hit(conn, f"c-{i}", "unverified_completion", "cli:unverified_completion:s", "cli")
            conn.commit()
            conn.close()

            def run(*extra: str) -> list[dict]:
                buf = StringIO()
                with patch("sys.stdout", buf):
                    rc = main(["--db", str(db_path), "prevalence", "--days", "7", "--json", *extra])
                self.assertEqual(rc, 0)
                return json.loads(buf.getvalue())

            self.assertFalse(run()[0]["suspect_calibration"])  # 10 runs < default sample floor
            self.assertTrue(run("--min-suspect-runs", "10", "--max-plausible-rate", "0.5")[0]["suspect_calibration"])
            self.assertFalse(run("--min-suspect-runs", "10", "--max-plausible-rate", "0.9")[0]["suspect_calibration"])


class TestIssuesExcludeClassifiers(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()
        _insert_issue(self.conn, "alpha:worktree_leak:x", "alpha", "worktree_leak", recurrence_count=3)
        # Legacy rows written before classifiers stopped creating issues; the
        # inflated counter must not top the recurrence-ordered list.
        _insert_issue(self.conn, f"alpha:failure_taxonomy:{UNCLASSIFIED}", "alpha",
                      "failure_taxonomy", recurrence_count=182767)
        _insert_issue(self.conn, "alpha:toy_classifier_for_query_tests:t", "alpha",
                      _ToyClassifier.DETECTOR_NAME, recurrence_count=500)

    def tearDown(self) -> None:
        self.conn.close()

    def test_classifier_signatures_are_not_issues(self) -> None:
        rows = query_issues(self.conn)
        self.assertEqual([r["detector"] for r in rows], ["worktree_leak"])

    def test_include_classifiers_restores_legacy_rows(self) -> None:
        rows = query_issues(self.conn, include_classifiers=True)
        self.assertEqual(rows[0]["detector"], "failure_taxonomy")
        self.assertEqual(len(rows), 3)


class TestQueryClassifications(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _make_db()

    def tearDown(self) -> None:
        self.conn.close()

    def _tag(self, rid: str, project: str, label: str) -> None:
        _insert_hit(self.conn, rid, "failure_taxonomy", f"{project}:failure_taxonomy:{label}", project,
                    ts=_ts(0))

    def test_labels_are_split_into_classified_and_not(self) -> None:
        ids = _runs(self.conn, "alpha", 6, "a")
        for rid in ids[:3]:
            self._tag(rid, "alpha", PLANNING_FAILURE)
        self._tag(ids[0], "alpha", CONTEXT_FAILURE)  # multi-label run
        self._tag(ids[3], "alpha", PLANNING_FAILURE_UNCONFIRMED_CADENCE)
        for rid in ids[4:]:
            self._tag(rid, "alpha", UNCLASSIFIED)
        rows = query_classifications(self.conn, days=7)
        got = {r["label"]: (r["runs"], r["classified"]) for r in rows}
        self.assertEqual(got[PLANNING_FAILURE], (3, True))
        self.assertEqual(got[CONTEXT_FAILURE], (1, True))
        self.assertEqual(got[PLANNING_FAILURE_UNCONFIRMED_CADENCE], (1, False))
        self.assertEqual(got[UNCLASSIFIED], (2, False))
        self.assertEqual({r["total_runs"] for r in rows}, {6})

    def test_window_follows_run_end_not_recording_time(self) -> None:
        _insert_run(self.conn, "old", "alpha", started=_ts(20, 1), ended=_ts(20))
        self._tag("old", "alpha", UNCLASSIFIED)  # recorded just now, run is old
        self.assertEqual(query_classifications(self.conn, days=7), [])

    def test_non_classifier_hits_are_not_listed(self) -> None:
        rid = _runs(self.conn, "alpha", 1, "a")[0]
        _insert_hit(self.conn, rid, "worktree_leak", "alpha:worktree_leak:x", "alpha")
        self.assertEqual(query_classifications(self.conn, days=7), [])


if __name__ == "__main__":
    unittest.main()
