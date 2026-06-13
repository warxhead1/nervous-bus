"""tests/test_reread_same_file.py — Unit tests for RereadSameFileDetector.

Covers:
  - Positive: a run where the same file is Read > REREAD_THRESHOLD times → fires.
  - Negative: read count at or below threshold → no fire.
  - Negative: no false-fire when no Read events exist.
  - Signature stability: no run_id, no timestamp in signature.
  - Signature format: project:reread_same_file:norm_path.
  - Recurrence/dedup: same path re-read in multiple runs increments
    issues.recurrence_count (Kyoko #5).
  - Ladder rung: extra["remediation_rung"] == "automate" with justification.
  - Path extraction: JSON tool_summary, truncated JSON (regex fallback),
    and raw_json envelope fallback all work.
  - Path normalization: worktree-slug paths collapse to the same anchor.
  - Cross-run aggregation: two runs firing on the same file share one signature.
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
from detectors.reread_same_file import (
    RereadSameFileDetector,
    REREAD_THRESHOLD,
    _extract_file_path,
    _normalize_path,
)


# ── Fixture helpers ───────────────────────────────────────────────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    worktree      TEXT,
    worktree_slug TEXT,
    git_branch    TEXT,
    bead_id       TEXT,
    outcome       TEXT,
    labeled_at    TEXT,
    ended         TEXT NOT NULL,
    close_reason  TEXT
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
    """Create a fresh in-memory SQLite DB with the minimal schema."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project, outcome=None):
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, project, outcome, ended)
           VALUES (?, ?, ?, ?)""",
        (run_id, project, outcome, _now_utc()),
    )


def _make_read_event_json(project: str, file_path: str) -> str:
    """Build a minimal bus.agent.activity.v1 envelope for a Read tool call."""
    return json.dumps({
        "specversion": "1.0",
        "id": "test-id",
        "source": "/test/source",
        "type": "bus.agent.activity.v1",
        "data": {
            "agent_kind": "host_claude_code",
            "session_id": "test-session",
            "project": project,
            "tool_name": "Read",
            "tool_summary": json.dumps({"file_path": file_path, "limit": 100}),
            "event": "tool_call",
            "ts": _now_utc(),
        },
    })


def _make_read_event_json_truncated(project: str, file_path: str) -> str:
    """Build an envelope where tool_summary is truncated (simulates real data)."""
    # Truncate after file_path value, before closing quote
    truncated = f'{{"file_path":"{file_path}","content":"this is a long file c'
    return json.dumps({
        "specversion": "1.0",
        "id": "test-id-trunc",
        "source": "/test/source",
        "type": "bus.agent.activity.v1",
        "data": {
            "agent_kind": "host_claude_code",
            "session_id": "test-session",
            "project": project,
            "tool_name": "Read",
            "tool_summary": truncated,
            "event": "tool_call",
            "ts": _now_utc(),
        },
    })


def _insert_read_events(conn, run_id, project, file_path, count, tool_summary_style="json"):
    """Insert *count* Read-tool events for *file_path* into run_events."""
    for i in range(count):
        if tool_summary_style == "json":
            raw = _make_read_event_json(project, file_path)
        elif tool_summary_style == "truncated":
            raw = _make_read_event_json_truncated(project, file_path)
        else:
            raw = _make_read_event_json(project, file_path)
        conn.execute(
            """INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, i + 1, _now_utc(), "bus.agent.activity.v1", raw),
        )


# ── Tests: file_path extraction helpers ──────────────────────────────────────

class TestExtractFilePath(unittest.TestCase):
    """Unit tests for _extract_file_path()."""

    def test_well_formed_json_tool_summary(self):
        ts = json.dumps({"file_path": "/home/eric/CLAUDE.md", "limit": 50})
        result = _extract_file_path(ts, None)
        self.assertEqual(result, "/home/eric/CLAUDE.md")

    def test_truncated_json_fallback_to_regex(self):
        # Simulates the real truncated summaries in production DB
        ts = '{"file_path":"/home/eric/projects/foo/bar.py","content":"some trun'
        result = _extract_file_path(ts, None)
        self.assertEqual(result, "/home/eric/projects/foo/bar.py")

    def test_null_tool_summary_falls_back_to_raw_json(self):
        raw = json.dumps({
            "data": {
                "tool_summary": json.dumps({"file_path": "/from/raw.py"}),
            }
        })
        result = _extract_file_path(None, raw)
        self.assertEqual(result, "/from/raw.py")

    def test_returns_none_when_no_path(self):
        result = _extract_file_path('{"command": "ls"}', '{"data": {}}')
        self.assertIsNone(result)

    def test_returns_none_on_empty_inputs(self):
        self.assertIsNone(_extract_file_path(None, None))
        self.assertIsNone(_extract_file_path("", ""))


class TestNormalizePath(unittest.TestCase):
    """Unit tests for _normalize_path()."""

    def test_worktree_slug_stripped(self):
        path = "/home/eric/projects/foo/.claude/worktrees/wf_abc123/adapters/bar.py"
        result = _normalize_path(path)
        self.assertEqual(result, "adapters/bar.py")

    def test_different_slug_same_anchor(self):
        path1 = "/home/eric/projects/foo/.claude/worktrees/wf_abc/adapters/bar.py"
        path2 = "/home/eric/projects/foo/.claude/worktrees/wf_xyz/adapters/bar.py"
        self.assertEqual(_normalize_path(path1), _normalize_path(path2))

    def test_direct_repo_path_collapses_to_repo_relative(self):
        # A directly-accessed repo path strips the .../projects/<proj>/ prefix to
        # the repo-relative anchor, so it shares ONE signature with the same file
        # accessed via a worktree (which strips to the same suffix).
        path = "/home/eric/projects/foo/CLAUDE.md"
        self.assertEqual(_normalize_path(path), "CLAUDE.md")

    def test_direct_and_worktree_access_share_anchor(self):
        direct = "/home/eric/projects/foo/adapters/store.py"
        worktree = "/home/eric/projects/foo/.claude/worktrees/wf_abc/adapters/store.py"
        self.assertEqual(_normalize_path(direct), _normalize_path(worktree))

    def test_dotworktrees_variant_also_stripped(self):
        path = "/home/eric/projects/foo/.worktrees/wf_abc/some/file.py"
        result = _normalize_path(path)
        self.assertEqual(result, "some/file.py")


# ── Tests: positive detection ─────────────────────────────────────────────────

class TestRereadSameFilePositive(unittest.TestCase):
    """A run where a file is Read > REREAD_THRESHOLD times → fires."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-001", "myproject", outcome="clean")
        _insert_read_events(
            self.conn, "run-001", "myproject",
            "/home/eric/projects/myproject/CLAUDE.md",
            count=REREAD_THRESHOLD + 1,  # just above threshold
        )

    def test_fires_above_threshold(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.project, "myproject")
        self.assertEqual(c.detector, "reread_same_file")
        self.assertEqual(c.pattern_name, "reread_same_file")

    def test_occurrences_reflects_read_count(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].occurrences, REREAD_THRESHOLD + 1)

    def test_evidence_contains_file_and_count(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        ev = "\n".join(candidates[0].evidence)
        self.assertIn("CLAUDE.md", ev)
        self.assertIn("myproject", ev)

    def test_run_ids_populated(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertIn("run-001", candidates[0].run_ids)

    def test_proposed_remediation_present(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        rem = candidates[0].proposed_remediation
        self.assertIsNotNone(rem)
        self.assertIn("CLAUDE.md", rem)
        self.assertIn("Automate", rem)


class TestRereadSameFileTruncatedSummary(unittest.TestCase):
    """Regex fallback handles truncated tool_summary strings."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-trunc", "proj", outcome="clean")
        _insert_read_events(
            self.conn, "run-trunc", "proj",
            "/home/eric/projects/proj/store.py",
            count=REREAD_THRESHOLD + 2,
            tool_summary_style="truncated",
        )

    def test_fires_with_truncated_summary(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)
        self.assertIn("store.py", candidates[0].evidence[0])


# ── Tests: negative (no false-fire) ──────────────────────────────────────────

class TestRereadSameFileNegativeAtThreshold(unittest.TestCase):
    """read count == threshold (not above) → does NOT fire."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-eq", "proj")
        _insert_read_events(
            self.conn, "run-eq", "proj",
            "/home/eric/file.py",
            count=REREAD_THRESHOLD,  # exactly at threshold, not above
        )

    def test_no_fire_at_threshold(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [], "Must not fire when count == threshold")


class TestRereadSameFileNegativeBelowThreshold(unittest.TestCase):
    """read count < threshold → does NOT fire."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-low", "proj")
        _insert_read_events(
            self.conn, "run-low", "proj",
            "/home/eric/file.py",
            count=max(1, REREAD_THRESHOLD - 1),
        )

    def test_no_fire_below_threshold(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


class TestRereadSameFileNegativeNoReadEvents(unittest.TestCase):
    """No Read events at all → no fire."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-noread", "proj")
        # Insert only Bash events (not Read)
        for i in range(10):
            self.conn.execute(
                """INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    "run-noread",
                    i + 1,
                    _now_utc(),
                    "bus.agent.activity.v1",
                    json.dumps({
                        "data": {
                            "tool_name": "Bash",
                            "tool_summary": json.dumps({"command": "ls", "description": "list files"}),
                            "project": "proj",
                        }
                    }),
                ),
            )

    def test_no_fire_with_no_reads(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


class TestRereadSameFileNegativeDifferentFiles(unittest.TestCase):
    """Multiple files each read once → no fire (each below threshold)."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-multi", "proj")
        for i in range(REREAD_THRESHOLD + 2):
            _insert_read_events(
                self.conn, "run-multi", "proj",
                f"/home/eric/projects/proj/file_{i}.py",
                count=1,
            )

    def test_no_fire_with_many_distinct_files(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


# ── Tests: signature stability ────────────────────────────────────────────────

class TestSignatureFormat(unittest.TestCase):
    """Signature must be <project>:<DETECTOR_NAME>:<anchor> — no run_id, no ts."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-sig-001", "myproj")
        _insert_read_events(
            self.conn, "run-sig-001", "myproj",
            "/home/eric/projects/myproj/important.py",
            count=REREAD_THRESHOLD + 1,
        )

    def test_signature_starts_with_project(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        self.assertTrue(sig.startswith("myproj:"), f"Signature must start with project: {sig}")

    def test_signature_contains_detector_name(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        self.assertIn("reread_same_file", sig, f"Signature must contain detector name: {sig}")

    def test_signature_does_not_contain_run_id(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        self.assertNotIn("run-sig-001", sig, f"Signature must NOT contain run_id: {sig}")

    def test_signature_does_not_contain_timestamp(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        # No 'T' followed by time pattern (ISO 8601 heuristic)
        import re
        self.assertIsNone(
            re.search(r"\d{4}-\d{2}-\d{2}T", sig),
            f"Signature must NOT contain timestamp: {sig}",
        )

    def test_signature_three_part_colon_format(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        # Must match <project>:<detector>:<anchor> (exactly 2 colons at structural boundaries)
        parts = sig.split(":", 2)
        self.assertEqual(len(parts), 3, f"Signature must have 3 colon-separated parts: {sig}")
        self.assertEqual(parts[0], "myproj")
        self.assertEqual(parts[1], "reread_same_file")
        self.assertGreater(len(parts[2]), 0, "Anchor must be non-empty")

    def test_same_file_same_signature_across_runs(self):
        """Two runs re-reading the same file must produce the SAME signature."""
        _insert_run(self.conn, "run-sig-002", "myproj")
        _insert_read_events(
            self.conn, "run-sig-002", "myproj",
            "/home/eric/projects/myproj/important.py",
            count=REREAD_THRESHOLD + 1,
        )
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        # Both runs fire on the same file → same signature, one candidate
        self.assertEqual(len(candidates), 1, "Same file across runs → one candidate")
        self.assertIn("run-sig-001", candidates[0].run_ids)
        self.assertIn("run-sig-002", candidates[0].run_ids)


# ── Tests: recurrence/dedup (Kyoko #5) ───────────────────────────────────────

class TestRecurrenceAndDedup(unittest.TestCase):
    """Cross-run recurrence increments issues.recurrence_count on repeated scans."""

    def setUp(self):
        self.conn = _make_db()
        self.file_path = "/home/eric/projects/myproj/CLAUDE.md"
        _insert_run(self.conn, "run-rec-001", "myproj")
        _insert_read_events(
            self.conn, "run-rec-001", "myproj", self.file_path, count=REREAD_THRESHOLD + 1
        )

    def _expected_sig(self):
        from detectors.reread_same_file import _normalize_path
        return f"myproj:reread_same_file:{_normalize_path(self.file_path)}"

    def test_first_scan_creates_issue_with_count_1(self):
        det = RereadSameFileDetector(self.conn)
        det.run()
        issue = det.get_issue(self._expected_sig())
        self.assertIsNotNone(issue)
        self.assertEqual(issue["recurrence_count"], 1)

    def test_second_scan_increments_recurrence(self):
        det1 = RereadSameFileDetector(self.conn)
        det1.run()

        # Second scan (same DB, same run still present)
        det2 = RereadSameFileDetector(self.conn)
        det2.run()

        issue = det2.get_issue(self._expected_sig())
        self.assertEqual(issue["recurrence_count"], 2)

    def test_second_run_same_file_also_deduped(self):
        """Adding a second run with the same file → same signature, higher recurrence."""
        _insert_run(self.conn, "run-rec-002", "myproj")
        _insert_read_events(
            self.conn, "run-rec-002", "myproj", self.file_path, count=REREAD_THRESHOLD + 1
        )
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()

        # Both runs → one candidate with combined run_ids
        self.assertEqual(len(candidates), 1)
        sig = candidates[0].signature
        self.assertNotIn("run-rec-001", sig)
        self.assertNotIn("run-rec-002", sig)

        # The issue table has it with recurrence_count=1 (first scan)
        issue = det.get_issue(sig)
        self.assertEqual(issue["recurrence_count"], 1)

    def test_project_detector_stored_in_issues(self):
        det = RereadSameFileDetector(self.conn)
        det.run()
        issue = det.get_issue(self._expected_sig())
        self.assertEqual(issue["project"], "myproj")
        self.assertEqual(issue["detector"], "reread_same_file")


# ── Tests: remediation ladder rung ────────────────────────────────────────────

class TestRemediationLadder(unittest.TestCase):
    """extra["remediation_rung"] must be "automate" with justification."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-lad-001", "proj")
        _insert_read_events(
            self.conn, "run-lad-001", "proj",
            "/home/eric/projects/proj/CLAUDE.md",
            count=REREAD_THRESHOLD + 1,
        )

    def test_remediation_rung_is_automate(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        rung = candidates[0].extra.get("remediation_rung")
        self.assertEqual(rung, "automate", f"Expected 'automate' rung, got: {rung}")

    def test_remediation_rung_justification_present(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        justification = candidates[0].extra.get("remediation_rung_justification", "")
        self.assertGreater(len(justification), 20, "Justification must be non-trivial")
        self.assertIn("Eliminate", justification, "Must explain why Eliminate is not applicable")

    def test_remediation_not_inform_only(self):
        """proposed_remediation must NOT just say 'warn the user'."""
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        rem = candidates[0].proposed_remediation or ""
        # The remediation proposes a concrete action (CLAUDE.md append), not just inform
        self.assertIn("CLAUDE.md", rem)
        self.assertNotIn("warn the user", rem.lower())


# ── Tests: cross-run aggregation ──────────────────────────────────────────────

class TestCrossRunAggregation(unittest.TestCase):
    """Two runs re-reading the same file collapse into one candidate."""

    def setUp(self):
        self.conn = _make_db()
        self.path = "/home/eric/projects/proj/store.py"
        for run_id in ("run-xr-001", "run-xr-002"):
            _insert_run(self.conn, run_id, "proj")
            _insert_read_events(
                self.conn, run_id, "proj", self.path, count=REREAD_THRESHOLD + 1
            )

    def test_one_candidate_for_two_runs(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)

    def test_both_run_ids_in_candidate(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertIn("run-xr-001", candidates[0].run_ids)
        self.assertIn("run-xr-002", candidates[0].run_ids)

    def test_occurrences_summed_across_runs(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        # Each run has REREAD_THRESHOLD + 1 reads; total = 2 * (REREAD_THRESHOLD + 1)
        expected = 2 * (REREAD_THRESHOLD + 1)
        self.assertEqual(candidates[0].occurrences, expected)


# ── Tests: worktree path dedup via normalization ──────────────────────────────

class TestWorktreePathDedup(unittest.TestCase):
    """Files from different worktrees that are logically the same path share a signature."""

    def setUp(self):
        self.conn = _make_db()
        self.base_path = "adapters/reflex-recorder/store.py"
        self.path1 = f"/home/eric/projects/foo/.claude/worktrees/wf_abc/adapters/reflex-recorder/store.py"
        self.path2 = f"/home/eric/projects/foo/.claude/worktrees/wf_xyz/adapters/reflex-recorder/store.py"
        _insert_run(self.conn, "run-wt-001", "foo")
        _insert_run(self.conn, "run-wt-002", "foo")
        _insert_read_events(
            self.conn, "run-wt-001", "foo", self.path1, count=REREAD_THRESHOLD + 1
        )
        _insert_read_events(
            self.conn, "run-wt-002", "foo", self.path2, count=REREAD_THRESHOLD + 1
        )

    def test_same_logical_file_same_signature(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1, "Different worktree slugs, same logical file → 1 candidate")

    def test_signature_uses_normalized_path(self):
        det = RereadSameFileDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        # Signature must use the normalized path (stripped of worktree prefix)
        self.assertIn("adapters/reflex-recorder/store.py", sig)
        # No worktree slug in signature
        self.assertNotIn("wf_abc", sig)
        self.assertNotIn("wf_xyz", sig)


if __name__ == "__main__":
    unittest.main()
