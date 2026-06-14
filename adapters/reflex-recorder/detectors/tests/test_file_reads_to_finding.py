"""tests/test_file_reads_to_finding.py — Unit tests for FileReadsToFindingDetector.

Covers:
  - Positive detection: project with multiple runs exceeding READ_THRESHOLD fires.
  - Negative / no false-fire:
      - Single run above threshold alone does not fire (MIN_FLAGGED_RUNS=2).
      - Project with all runs below threshold does not fire.
      - Runs with no mutations (read-only tasks) are excluded from distribution.
  - Signature stability: no run_id or timestamp in signature.
  - Signature format: exactly "<project>:file_reads_to_finding:<project>".
  - Recurrence / dedup path: second scan increments recurrence_count.
  - Remediation ladder: extra["remediation_rung"] == "automate" and
    justification present.
  - Distribution stats (p50, p90, max) are carried in extra.
  - READ_THRESHOLD and MIN_FLAGGED_RUNS are respected as named constants.
  - Unlabeled runs are excluded from detection.
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
from detectors.file_reads_to_finding import (
    FileReadsToFindingDetector,
    READ_THRESHOLD,
    MIN_FLAGGED_RUNS,
    _nav_events_before_first_mutation,
    _percentile,
)


# ── Minimal schema (mirrors store.py but in-memory) ───────────────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    outcome       TEXT,
    labeled_at    TEXT,
    ended         TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z'
);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
    event_type  TEXT NOT NULL DEFAULT 'bus.agent.activity.v1',
    raw_json    TEXT NOT NULL
);
"""


def _make_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with runs + run_events + detector tables."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    project: str,
    outcome: str = "clean",
    labeled_at: str | None = "AUTO",
) -> None:
    # "AUTO" sentinel means "set to now"; None means "not labeled" (labeled_at IS NULL).
    la = _now_utc() if labeled_at == "AUTO" else labeled_at
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, project, outcome, labeled_at) VALUES (?,?,?,?)",
        (run_id, project, outcome, la),
    )


def _insert_event(
    conn: sqlite3.Connection,
    run_id: str,
    seq: int,
    tool_name: str,
) -> None:
    """Insert a bus.agent.activity.v1 event with the given tool_name."""
    raw = json.dumps({
        "specversion": "1.0",
        "id": f"test-evt-{run_id}-{seq}",
        "source": "/test/project",
        "type": "bus.agent.activity.v1",
        "time": "2026-01-01T00:00:00Z",
        "datacontenttype": "application/json",
        "data": {
            "tool_name": tool_name,
            "event": "tool_call",
            "project": "test",
        },
    })
    conn.execute(
        "INSERT INTO run_events (run_id, seq, raw_json) VALUES (?,?,?)",
        (run_id, seq, raw),
    )


def _build_run_with_tools(
    conn: sqlite3.Connection,
    run_id: str,
    project: str,
    tools: list[str],
    labeled: bool = True,
) -> None:
    """Helper: insert a run and its tool sequence."""
    # Pass "AUTO" for labeled (sets now), None for unlabeled (NULL in DB).
    _insert_run(conn, run_id, project, labeled_at="AUTO" if labeled else None)
    for seq, tool in enumerate(tools, start=1):
        _insert_event(conn, run_id, seq, tool)


# ── Helper: build a tool list with N nav tools then one mutation ──────────────

def _nav_then_edit(nav_count: int, nav_tool: str = "Read") -> list[str]:
    """Return a tool list of nav_count nav tools followed by one Edit."""
    return [nav_tool] * nav_count + ["Edit"]


# ── Tests: _nav_events_before_first_mutation helper ──────────────────────────

class TestNavEventsHelper(unittest.TestCase):
    def setUp(self):
        self.conn = _make_db()

    def test_counts_reads_before_edit(self):
        _build_run_with_tools(
            self.conn, "r1", "p",
            ["Read", "Read", "Read", "Edit", "Edit"],
        )
        count, had_mut = _nav_events_before_first_mutation(self.conn, "r1")
        self.assertEqual(count, 3)
        self.assertTrue(had_mut)

    def test_bash_not_counted_as_navigation(self):
        # Bash is excluded from nav-tools (it conflates lookup with execution),
        # so two Bash calls before a Write yield a nav count of 0.
        _build_run_with_tools(
            self.conn, "r2", "p",
            ["Bash", "Bash", "Write"],
        )
        count, had_mut = _nav_events_before_first_mutation(self.conn, "r2")
        self.assertEqual(count, 0)
        self.assertTrue(had_mut)

    def test_no_mutation_returns_false(self):
        _build_run_with_tools(
            self.conn, "r3", "p",
            ["Read", "Bash", "Read"],
        )
        count, had_mut = _nav_events_before_first_mutation(self.conn, "r3")
        self.assertFalse(had_mut)

    def test_mutation_first_returns_zero_nav(self):
        _build_run_with_tools(
            self.conn, "r4", "p",
            ["Edit", "Read", "Read"],
        )
        count, had_mut = _nav_events_before_first_mutation(self.conn, "r4")
        self.assertEqual(count, 0)
        self.assertTrue(had_mut)

    def test_mixed_nav_tools(self):
        # Read + Grep + Glob count (3); Bash is excluded; Edit is the mutation.
        _build_run_with_tools(
            self.conn, "r5", "p",
            ["Read", "Bash", "Grep", "Glob", "Edit"],
        )
        count, had_mut = _nav_events_before_first_mutation(self.conn, "r5")
        self.assertEqual(count, 3)
        self.assertTrue(had_mut)


# ── Tests: _percentile helper ─────────────────────────────────────────────────

class TestPercentileHelper(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(_percentile([], 90), 0.0)

    def test_single_value(self):
        self.assertEqual(_percentile([42], 90), 42.0)

    def test_p50(self):
        vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # median of 10 values by nearest-rank: 5th element = 5
        result = _percentile(vals, 50)
        self.assertGreaterEqual(result, 4.0)
        self.assertLessEqual(result, 6.0)

    def test_p90_larger_list(self):
        vals = list(range(1, 11))  # 1..10
        result = _percentile(vals, 90)
        self.assertGreaterEqual(result, 8.0)
        self.assertLessEqual(result, 10.0)


# ── Tests: positive detection ─────────────────────────────────────────────────

class TestPositiveDetection(unittest.TestCase):
    """Project with multiple runs > READ_THRESHOLD fires."""

    def setUp(self):
        self.conn = _make_db()
        # Two runs for "myproject" each with READ_THRESHOLD+5 nav events.
        nav = READ_THRESHOLD + 5
        _build_run_with_tools(
            self.conn, "run-pos-01", "myproject",
            _nav_then_edit(nav),
        )
        _build_run_with_tools(
            self.conn, "run-pos-02", "myproject",
            _nav_then_edit(nav),
        )

    def test_fires(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)

    def test_project_matches(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].project, "myproject")

    def test_pattern_name(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].pattern_name, "file_reads_to_finding")

    def test_detector_name(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].detector, "file_reads_to_finding")

    def test_occurrences_equals_flagged_runs(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].occurrences, 2)

    def test_run_ids_populated(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertIn("run-pos-01", candidates[0].run_ids)
        self.assertIn("run-pos-02", candidates[0].run_ids)


# ── Tests: signature format ───────────────────────────────────────────────────

class TestSignatureFormat(unittest.TestCase):
    """Signature must be stable and free of run_id / timestamp."""

    def setUp(self):
        self.conn = _make_db()
        nav = READ_THRESHOLD + 1
        for i in range(MIN_FLAGGED_RUNS):
            _build_run_with_tools(
                self.conn, f"sig-run-{i:03d}", "sigproject",
                _nav_then_edit(nav),
            )

    def _get_signature(self) -> str:
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertGreater(len(candidates), 0, "Expected at least one candidate")
        return candidates[0].signature

    def test_signature_format_is_project_detector_project(self):
        sig = self._get_signature()
        parts = sig.split(":")
        self.assertEqual(len(parts), 3, f"Signature should have 3 colon-separated parts: {sig!r}")
        self.assertEqual(parts[0], "sigproject")
        self.assertEqual(parts[1], "file_reads_to_finding")
        self.assertEqual(parts[2], "sigproject")

    def test_signature_does_not_contain_run_id(self):
        sig = self._get_signature()
        for i in range(MIN_FLAGGED_RUNS):
            self.assertNotIn(f"sig-run-{i:03d}", sig, f"run_id leaked into signature: {sig!r}")

    def test_signature_does_not_contain_timestamp(self):
        sig = self._get_signature()
        # No year, no Z suffix, no T-separated datetime
        self.assertNotRegex(sig, r"\d{4}-\d{2}-\d{2}")
        self.assertNotRegex(sig, r"\d{4}T\d{2}")

    def test_signature_stable_across_two_scans(self):
        """Same signature on two consecutive scans."""
        det1 = FileReadsToFindingDetector(self.conn)
        sig1 = det1.run()[0].signature
        det2 = FileReadsToFindingDetector(self.conn)
        sig2 = det2.run()[0].signature
        self.assertEqual(sig1, sig2)


# ── Tests: no false-fire (negative cases) ────────────────────────────────────

class TestNoFalseFire(unittest.TestCase):

    def test_single_run_above_threshold_does_not_fire(self):
        """Only one run above threshold → below MIN_FLAGGED_RUNS; no candidate."""
        conn = _make_db()
        _build_run_with_tools(
            conn, "solo-run", "soloproject",
            _nav_then_edit(READ_THRESHOLD + 10),
        )
        det = FileReadsToFindingDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_all_runs_below_threshold_does_not_fire(self):
        """All runs well below threshold → no candidate."""
        conn = _make_db()
        nav_below = max(0, READ_THRESHOLD - 5)
        for i in range(5):
            _build_run_with_tools(
                conn, f"below-{i}", "belowproject",
                _nav_then_edit(nav_below),
            )
        det = FileReadsToFindingDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_read_only_runs_excluded(self):
        """Runs with no mutations are excluded; they don't contribute to distribution."""
        conn = _make_db()
        # These runs have many nav events but no mutation — they're read-only tasks.
        for i in range(5):
            _build_run_with_tools(
                conn, f"readonly-{i}", "roproject",
                ["Read"] * (READ_THRESHOLD + 20),  # No Edit/Write
            )
        det = FileReadsToFindingDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_unlabeled_runs_excluded(self):
        """Runs without labeled_at must not be considered."""
        conn = _make_db()
        nav = READ_THRESHOLD + 10
        for i in range(MIN_FLAGGED_RUNS + 1):
            # Insert run with labeled_at=None (not labeled yet)
            _build_run_with_tools(
                conn, f"unlabeled-{i}", "unlabeledproject",
                _nav_then_edit(nav),
                labeled=False,
            )
        det = FileReadsToFindingDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_empty_db_returns_empty(self):
        """No runs at all → no candidate."""
        conn = _make_db()
        det = FileReadsToFindingDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


# ── Tests: recurrence / dedup ─────────────────────────────────────────────────

class TestRecurrenceAndDedup(unittest.TestCase):
    """Verify Kyoko recurrence increments on successive scans."""

    def setUp(self):
        self.conn = _make_db()
        nav = READ_THRESHOLD + 1
        for i in range(MIN_FLAGGED_RUNS):
            _build_run_with_tools(
                self.conn, f"recur-{i}", "recurproject",
                _nav_then_edit(nav),
            )

    def test_first_scan_creates_issue(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        sig = candidates[0].signature
        issue = det.get_issue(sig)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["recurrence_count"], 1)

    def test_second_scan_stable_recurrence(self):
        """CONTRACT (2026-06): second pass over the SAME run_ids must NOT inflate
        recurrence_count.  The hit for each (run_id, detector, signature) is already
        recorded; re-running detect() on identical data leaves the count at 1.
        """
        det1 = FileReadsToFindingDetector(self.conn)
        cands1 = det1.run()
        self.assertEqual(len(cands1), 1)
        sig = cands1[0].signature
        issue1 = det1.get_issue(sig)
        self.assertEqual(issue1["recurrence_count"], 1, "first scan → recurrence_count=1")

        # Second scan over SAME data — count must stay at 1
        det2 = FileReadsToFindingDetector(self.conn)
        cands2 = det2.run()
        self.assertEqual(len(cands2), 1)
        issue2 = det2.get_issue(cands2[0].signature)
        self.assertEqual(
            issue2["recurrence_count"], 1,
            "second pass over identical data must NOT inflate recurrence_count",
        )

    def test_new_run_increments_recurrence(self):
        """A genuinely new run_id firing the same signature DOES increment recurrence_count."""
        nav = READ_THRESHOLD + 1
        det1 = FileReadsToFindingDetector(self.conn)
        cands1 = det1.run()
        sig = cands1[0].signature

        # Add a new run that also exceeds the threshold
        _build_run_with_tools(
            self.conn, "recur-extra", "recurproject",
            _nav_then_edit(nav),
        )

        det2 = FileReadsToFindingDetector(self.conn)
        det2.run()
        issue = det2.get_issue(sig)
        self.assertEqual(
            issue["recurrence_count"], 2,
            "a new distinct run_id must increment recurrence_count",
        )

    def test_signature_dedup_same_project_same_detector(self):
        """Two scans produce the same signature → same issue row."""
        det1 = FileReadsToFindingDetector(self.conn)
        det1.run()
        sig1 = det1.run()[0].signature

        det2 = FileReadsToFindingDetector(self.conn)
        det2.run()
        sig2 = det2.run()[0].signature

        self.assertEqual(sig1, sig2)


# ── Tests: remediation ladder ─────────────────────────────────────────────────

class TestRemediationLadder(unittest.TestCase):
    """Verify AUTOMATE rung is encoded correctly in extra."""

    def setUp(self):
        self.conn = _make_db()
        nav = READ_THRESHOLD + 3
        for i in range(MIN_FLAGGED_RUNS):
            _build_run_with_tools(
                self.conn, f"ladder-{i}", "ladderproject",
                _nav_then_edit(nav),
            )

    def test_remediation_rung_is_automate(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].extra["remediation_rung"], "automate")

    def test_remediation_rung_justification_present(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        justification = candidates[0].extra.get("remediation_rung_justification", "")
        self.assertGreater(len(justification), 20, "Justification should be non-empty")

    def test_proposed_remediation_mentions_automate(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        rem = candidates[0].proposed_remediation or ""
        self.assertIn("AUTOMATE", rem)

    def test_proposed_remediation_not_none(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertIsNotNone(candidates[0].proposed_remediation)


# ── Tests: distribution stats in extra ───────────────────────────────────────

class TestDistributionStats(unittest.TestCase):
    """extra must carry p50, p90, max and threshold constants."""

    def setUp(self):
        self.conn = _make_db()
        # Three runs: 25, 30, 35 nav events before mutation.
        for count, rid in [(25, "dist-a"), (30, "dist-b"), (35, "dist-c")]:
            _build_run_with_tools(
                self.conn, rid, "distproject",
                _nav_then_edit(count),
            )

    def test_extra_has_p50_p90_max(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        extra = candidates[0].extra
        self.assertIn("p50", extra)
        self.assertIn("p90", extra)
        self.assertIn("max", extra)

    def test_max_is_35(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].extra["max"], 35)

    def test_extra_has_threshold_constant(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].extra["read_threshold"], READ_THRESHOLD)

    def test_extra_has_min_flagged_runs_constant(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].extra["min_flagged_runs"], MIN_FLAGGED_RUNS)

    def test_flagged_run_ids_in_extra(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        flagged = candidates[0].extra["flagged_run_ids"]
        self.assertIn("dist-a", flagged)
        self.assertIn("dist-b", flagged)
        self.assertIn("dist-c", flagged)


# ── Tests: multi-project isolation ────────────────────────────────────────────

class TestMultiProjectIsolation(unittest.TestCase):
    """Only the project with recurring high-nav runs fires; clean project does not."""

    def setUp(self):
        self.conn = _make_db()
        # "highproject" — 3 runs above threshold
        for i in range(3):
            _build_run_with_tools(
                self.conn, f"high-{i}", "highproject",
                _nav_then_edit(READ_THRESHOLD + 5),
            )
        # "cleanproject" — 5 runs well below threshold
        for i in range(5):
            _build_run_with_tools(
                self.conn, f"clean-{i}", "cleanproject",
                _nav_then_edit(3),
            )

    def test_only_highproject_fires(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        projects = {c.project for c in candidates}
        self.assertIn("highproject", projects)
        self.assertNotIn("cleanproject", projects)

    def test_exactly_one_candidate(self):
        det = FileReadsToFindingDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
