"""tests/test_rebuild_cache_miss.py — Unit tests for RebuildCacheMissDetector.

Covers:
  - Positive: worktree cargo build with gap above threshold fires.
  - Negative: fast build (below floor) does not fire.
  - Negative: main-tree cwd (not a worktree) does not fire.
  - Negative: non-cargo Bash command does not fire.
  - Signature stability: signature must NOT contain run_id or timestamp.
  - Signature format: must be <project>:<DETECTOR_NAME>:<anchor>.
  - remediation_rung = "eliminate" is set in extra.
  - Recurrence / dedup: second run for same (project, crate) increments
    issues.recurrence_count, not a second issue row.
  - Prevalence integration with the base layer.
  - Multiple projects / crates produce separate PatternCandidates.
  - Threshold logic: 3× baseline OR absolute floor (60s), whichever is larger.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.rebuild_cache_miss import (
    RebuildCacheMissDetector,
    SLOW_BUILD_MULTIPLIER,
    ABSOLUTE_SLOW_BUILD_FLOOR_S,
    _extract_command,
    _extract_cwd,
    _is_cargo_build_command,
    _is_worktree_cwd,
    _extract_crate_target,
)


# ── Minimal schema ────────────────────────────────────────────────────────────

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
"""

_RUN_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_re_run_id ON run_events(run_id, seq);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    conn.executescript(_RUN_EVENTS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _ts(offset_s: float = 0.0) -> str:
    """Return an RFC3339 UTC timestamp offset_s seconds from a fixed epoch."""
    base = datetime(2026, 6, 13, 10, 0, 0, tzinfo=timezone.utc)
    t = base + timedelta(seconds=offset_s)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    project: str,
    worktree: str | None = None,
    outcome: str | None = "clean",
    labeled_at: str | None = None,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, project, worktree, worktree_slug, outcome, labeled_at, ended)
           VALUES (?,?,?,?,?,?,?)""",
        (
            run_id,
            project,
            worktree,
            worktree.split("/")[-1] if worktree else None,
            outcome,
            labeled_at or _now_utc(),
            _now_utc(),
        ),
    )


def _make_bash_event(
    cmd: str,
    cwd: str = "/home/eric/projects/tengine",
    project: str = "tengine",
) -> str:
    """Build a minimal bus.agent.activity.v1 raw_json blob for a Bash tool call."""
    return json.dumps(
        {
            "specversion": "1.0",
            "type": "bus.agent.activity.v1",
            "data": {
                "project": project,
                "cwd": cwd,
                "tool_name": "Bash",
                "event": "tool_call",
                "tool_summary": json.dumps({"command": cmd, "description": "test"}),
                "tool_response_summary": json.dumps(
                    {"interrupted": False, "isImage": False, "stdout": "", "stderr": ""}
                ),
            },
        }
    )


def _insert_events(
    conn: sqlite3.Connection,
    run_id: str,
    events: list[tuple[int, float, str]],  # (seq, ts_offset_s, raw_json)
) -> None:
    """Insert a sequence of run_events rows."""
    for seq, ts_offset, raw in events:
        conn.execute(
            """INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
               VALUES (?,?,?,?,?)""",
            (run_id, seq, _ts(ts_offset), "bus.agent.activity.v1", raw),
        )


# ── Helper constants ──────────────────────────────────────────────────────────

_WORKTREE_CWD = "/home/eric/projects/tengine/.claude/worktrees/agent-abc123"
_MAIN_TREE_CWD = "/home/eric/projects/tengine"
_SLOW_GAP = float(ABSOLUTE_SLOW_BUILD_FLOOR_S + 60)  # 120s — clearly above floor
_FAST_GAP = 5.0  # 5s — clearly below floor


# ── Unit tests for helpers ────────────────────────────────────────────────────


class TestHelpers(unittest.TestCase):
    def test_is_cargo_build_command(self):
        self.assertTrue(_is_cargo_build_command("cargo build -p foo"))
        self.assertTrue(_is_cargo_build_command("cargo test --lib -p foo"))
        self.assertTrue(_is_cargo_build_command("SKIP=1 cargo check -p bar 2>&1"))
        self.assertTrue(_is_cargo_build_command("echo cargo build"))  # substring match is intentional
        self.assertFalse(_is_cargo_build_command("ls -la"))
        self.assertFalse(_is_cargo_build_command("git status"))
        self.assertFalse(_is_cargo_build_command("echo done"))  # no cargo token at all

    def test_is_worktree_cwd(self):
        self.assertTrue(_is_worktree_cwd("/proj/.claude/worktrees/agent-abc"))
        self.assertTrue(_is_worktree_cwd("/proj/.worktrees/wt-123"))
        self.assertFalse(_is_worktree_cwd("/home/eric/projects/tengine"))
        self.assertFalse(_is_worktree_cwd(None))
        self.assertFalse(_is_worktree_cwd(""))

    def test_extract_crate_target(self):
        self.assertEqual(_extract_crate_target("cargo build -p my-crate"), "my-crate")
        self.assertEqual(_extract_crate_target("cargo test --package foo 2>&1"), "foo")
        self.assertEqual(_extract_crate_target("cargo build"), "workspace")

    def test_extract_command_plain_string(self):
        raw = json.dumps(
            {"data": {"tool_summary": "cargo build -p foo", "tool_name": "Bash"}}
        )
        cmd = _extract_command(raw)
        self.assertIsNotNone(cmd)
        self.assertIn("cargo build", cmd)

    def test_extract_command_nested_json(self):
        raw = _make_bash_event("cargo build -p foo", cwd=_WORKTREE_CWD)
        cmd = _extract_command(raw)
        self.assertIsNotNone(cmd)
        self.assertIn("cargo build", cmd)

    def test_extract_cwd(self):
        raw = _make_bash_event("ls", cwd=_WORKTREE_CWD)
        cwd = _extract_cwd(raw)
        self.assertEqual(cwd, _WORKTREE_CWD)


# ── Positive detection ────────────────────────────────────────────────────────


class TestRebuildCacheMissPositive(unittest.TestCase):
    """A slow cargo build in a worktree cwd fires the detector."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-slow-1", "tengine", worktree=_WORKTREE_CWD)
        # Cargo build at t=0, next event at t=_SLOW_GAP (120s) → fire
        _insert_events(
            self.conn,
            "run-slow-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p tengine-dgc-hal", cwd=_WORKTREE_CWD)),
                (2, _SLOW_GAP, _make_bash_event("git status", cwd=_WORKTREE_CWD)),
            ],
        )

    def test_fires_on_slow_build(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1, f"Expected 1 candidate, got {len(candidates)}")
        c = candidates[0]
        self.assertEqual(c.project, "tengine")
        self.assertEqual(c.pattern_name, "rebuild_cache_miss")
        self.assertEqual(c.detector, "rebuild_cache_miss")
        self.assertGreater(c.occurrences, 0)

    def test_run_id_in_run_ids(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertIn("run-slow-1", candidates[0].run_ids)

    def test_evidence_contains_useful_fields(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        ev = "\n".join(candidates[0].evidence)
        self.assertIn("tengine", ev)
        self.assertIn("slow_builds", ev)
        self.assertIn("max_gap_s", ev)

    def test_proposed_remediation_mentions_cargo_target_dir(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        rem = candidates[0].proposed_remediation
        self.assertIsNotNone(rem)
        self.assertIn("CARGO_TARGET_DIR", rem)
        self.assertIn("eliminate", rem.lower())

    def test_remediation_rung_is_eliminate(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].extra["remediation_rung"], "eliminate")

    def test_remediation_rung_justification_present(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        justification = candidates[0].extra.get("remediation_rung_justification", "")
        self.assertIn("eliminate", justification.lower())


# ── Negative: fast build (below floor) does not fire ─────────────────────────


class TestRebuildCacheMissNegativeFastBuild(unittest.TestCase):
    """A fast build (below absolute floor) should NOT fire."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-fast-1", "tengine", worktree=_WORKTREE_CWD)
        # Cargo build at t=0, next event at t=5s → fast incremental, no fire
        _insert_events(
            self.conn,
            "run-fast-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p foo", cwd=_WORKTREE_CWD)),
                (2, _FAST_GAP, _make_bash_event("git status", cwd=_WORKTREE_CWD)),
            ],
        )

    def test_no_fire_on_fast_build(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [], "Fast build should not fire")


# ── Negative: main-tree cwd does not fire ─────────────────────────────────────


class TestRebuildCacheMissNegativeMainTree(unittest.TestCase):
    """A slow cargo build in the main project tree (not a worktree) must NOT fire."""

    def setUp(self):
        self.conn = _make_db()
        # No worktree column — main-tree agent session
        _insert_run(self.conn, "run-main-1", "tengine", worktree=None)
        _insert_events(
            self.conn,
            "run-main-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p foo", cwd=_MAIN_TREE_CWD)),
                (2, _SLOW_GAP, _make_bash_event("echo done", cwd=_MAIN_TREE_CWD)),
            ],
        )

    def test_no_fire_on_main_tree_build(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [], "Main-tree build should not fire")


# ── Negative: non-cargo command does not fire ─────────────────────────────────


class TestRebuildCacheMissNegativeNonCargo(unittest.TestCase):
    """A slow non-cargo bash command must NOT fire."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-noncargo-1", "tengine", worktree=_WORKTREE_CWD)
        _insert_events(
            self.conn,
            "run-noncargo-1",
            [
                (1, 0.0, _make_bash_event("sleep 300", cwd=_WORKTREE_CWD)),
                (2, _SLOW_GAP, _make_bash_event("echo done", cwd=_WORKTREE_CWD)),
            ],
        )

    def test_no_fire_on_non_cargo_command(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates, [], "Non-cargo slow command should not fire")


# ── Signature stability and format ────────────────────────────────────────────


class TestSignatureStability(unittest.TestCase):
    """Signature must be stable (no run_id, no timestamp) and match the contract."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-sig-1", "tengine", worktree=_WORKTREE_CWD)
        _insert_events(
            self.conn,
            "run-sig-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p tengine-dgc-hal", cwd=_WORKTREE_CWD)),
                (2, _SLOW_GAP, _make_bash_event("ls", cwd=_WORKTREE_CWD)),
            ],
        )

    def test_signature_does_not_contain_run_id(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        self.assertNotIn("run-sig-1", sig)

    def test_signature_does_not_contain_timestamp(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        # No year/date substring (YYYY- pattern)
        import re
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", sig), f"Timestamp found in sig: {sig}")

    def test_signature_format_project_detector_anchor(self):
        """Signature must match <project>:<DETECTOR_NAME>:<anchor> with no run_id."""
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        sig = candidates[0].signature
        # Must have exactly 2 colons
        parts = sig.split(":")
        self.assertEqual(len(parts), 3, f"Signature has wrong number of ':' parts: {sig}")
        project, detector_name, anchor = parts
        self.assertEqual(project, "tengine")
        self.assertEqual(detector_name, "rebuild_cache_miss")
        # anchor = "<project>/<crate>"
        self.assertIn("/", anchor)
        self.assertIn("tengine", anchor)

    def test_signature_stable_across_two_runs(self):
        """Two runs with the same project/crate produce the same signature."""
        _insert_run(self.conn, "run-sig-2", "tengine", worktree=_WORKTREE_CWD)
        _insert_events(
            self.conn,
            "run-sig-2",
            [
                (1, 1000.0, _make_bash_event("cargo build -p tengine-dgc-hal", cwd=_WORKTREE_CWD)),
                (2, 1000.0 + _SLOW_GAP, _make_bash_event("ls", cwd=_WORKTREE_CWD)),
            ],
        )
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        # Both runs map to the same (project, crate) → same signature, one candidate
        sigs = {c.signature for c in candidates}
        self.assertEqual(len(sigs), 1, f"Expected one unique signature, got: {sigs}")


# ── Recurrence / dedup ────────────────────────────────────────────────────────


class TestRecurrenceAndDedup(unittest.TestCase):
    """Running the detector twice on the same leak increments recurrence_count."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-rec-1", "tengine", worktree=_WORKTREE_CWD)
        _insert_events(
            self.conn,
            "run-rec-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p tengine-dgc-hal", cwd=_WORKTREE_CWD)),
                (2, _SLOW_GAP, _make_bash_event("ls", cwd=_WORKTREE_CWD)),
            ],
        )

    def _signature(self):
        return "tengine:rebuild_cache_miss:tengine/tengine-dgc-hal"

    def test_first_run_creates_issue(self):
        det = RebuildCacheMissDetector(self.conn)
        det.run()
        issue = det.get_issue(self._signature())
        self.assertIsNotNone(issue)
        self.assertEqual(issue["recurrence_count"], 1)

    def test_second_run_increments_recurrence(self):
        det1 = RebuildCacheMissDetector(self.conn)
        det1.run()
        det2 = RebuildCacheMissDetector(self.conn)
        det2.run()
        issue = det2.get_issue(self._signature())
        self.assertEqual(issue["recurrence_count"], 2)


# ── Ladder rung in extra ──────────────────────────────────────────────────────


class TestRemediationLadder(unittest.TestCase):
    """remediation_rung MUST be 'eliminate' and justification must be present."""

    def setUp(self):
        self.conn = _make_db()
        _insert_run(self.conn, "run-lad-1", "tengine", worktree=_WORKTREE_CWD)
        _insert_events(
            self.conn,
            "run-lad-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p foo", cwd=_WORKTREE_CWD)),
                (2, _SLOW_GAP, _make_bash_event("ls", cwd=_WORKTREE_CWD)),
            ],
        )

    def test_remediation_rung_is_eliminate(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(candidates[0].extra["remediation_rung"], "eliminate")

    def test_justification_explains_eliminate_choice(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        j = candidates[0].extra.get("remediation_rung_justification", "")
        self.assertIn("eliminate", j.lower())
        self.assertIn("CARGO_TARGET_DIR", candidates[0].proposed_remediation)

    def test_not_inform_not_automate(self):
        """The rung must not be 'inform' or 'automate' — those are lower rungs."""
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        rung = candidates[0].extra["remediation_rung"]
        self.assertNotEqual(rung, "inform")
        self.assertNotEqual(rung, "automate")


# ── Multiple projects / crates produce separate candidates ────────────────────


class TestMultipleProjects(unittest.TestCase):
    """Slow builds for different (project, crate) pairs produce separate candidates."""

    def setUp(self):
        self.conn = _make_db()

        # Project A, crate alpha
        wt_a = "/home/eric/projects/proj-a/.claude/worktrees/agent-001"
        _insert_run(self.conn, "run-mp-1", "proj-a", worktree=wt_a)
        _insert_events(
            self.conn,
            "run-mp-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p alpha", cwd=wt_a, project="proj-a")),
                (2, _SLOW_GAP, _make_bash_event("echo", cwd=wt_a, project="proj-a")),
            ],
        )

        # Project B, crate beta
        wt_b = "/home/eric/projects/proj-b/.claude/worktrees/agent-002"
        _insert_run(self.conn, "run-mp-2", "proj-b", worktree=wt_b)
        _insert_events(
            self.conn,
            "run-mp-2",
            [
                (1, 500.0, _make_bash_event("cargo build -p beta", cwd=wt_b, project="proj-b")),
                (2, 500.0 + _SLOW_GAP, _make_bash_event("echo", cwd=wt_b, project="proj-b")),
            ],
        )

    def test_two_candidates_for_two_projects(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 2, f"Got: {[c.signature for c in candidates]}")
        projects = {c.project for c in candidates}
        self.assertIn("proj-a", projects)
        self.assertIn("proj-b", projects)

    def test_signatures_are_distinct(self):
        det = RebuildCacheMissDetector(self.conn)
        candidates = det.run()
        sigs = {c.signature for c in candidates}
        self.assertEqual(len(sigs), 2)


# ── Threshold boundary tests ──────────────────────────────────────────────────


class TestThresholdBoundary(unittest.TestCase):
    """Builds exactly at the threshold boundary should NOT fire; just above should."""

    def _build_conn_with_gap(self, gap_s: float) -> sqlite3.Connection:
        conn = _make_db()
        _insert_run(conn, "run-thresh-1", "tengine", worktree=_WORKTREE_CWD)
        _insert_events(
            conn,
            "run-thresh-1",
            [
                (1, 0.0, _make_bash_event("cargo build -p foo", cwd=_WORKTREE_CWD)),
                (2, gap_s, _make_bash_event("ls", cwd=_WORKTREE_CWD)),
            ],
        )
        return conn

    def test_at_floor_does_not_fire(self):
        # Exactly at the absolute floor (not exceeding it)
        conn = self._build_conn_with_gap(float(ABSOLUTE_SLOW_BUILD_FLOOR_S))
        det = RebuildCacheMissDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_one_second_above_floor_fires(self):
        conn = self._build_conn_with_gap(float(ABSOLUTE_SLOW_BUILD_FLOOR_S) + 1.0)
        det = RebuildCacheMissDetector(conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)

    def test_multiplier_threshold_respected(self):
        """With fast builds (10s), a 70s build fires via absolute floor (>60s).
        Also fires via multiplier: p50=10s, 3×p50=30s < 70s."""
        conn = _make_db()
        wt = "/home/eric/projects/tengine/.claude/worktrees/agent-thr"
        _insert_run(conn, "run-thr-base", "tengine", worktree=wt)
        # 3 fast builds of 10s each, then one slow build of 70s.
        # 70s > 60s floor → fires.  Also 70s > 3×10s=30s → fires via multiplier.
        events = [
            (1, 0.0, _make_bash_event("cargo build -p foo", cwd=wt)),
            (2, 10.0, _make_bash_event("ls", cwd=wt)),
            (3, 20.0, _make_bash_event("cargo build -p foo", cwd=wt)),
            (4, 30.0, _make_bash_event("ls", cwd=wt)),
            (5, 40.0, _make_bash_event("cargo build -p foo", cwd=wt)),
            (6, 50.0, _make_bash_event("ls", cwd=wt)),
            # Slow build: 70s gap > absolute floor (60s)
            (7, 60.0, _make_bash_event("cargo build -p foo", cwd=wt)),
            (8, 130.0, _make_bash_event("ls", cwd=wt)),
        ]
        _insert_events(conn, "run-thr-base", events)
        det = RebuildCacheMissDetector(conn)
        candidates = det.run()
        self.assertEqual(len(candidates), 1)


# ── Empty DB / no cargo events ────────────────────────────────────────────────


class TestEmptyDB(unittest.TestCase):
    def test_empty_db_returns_no_candidates(self):
        conn = _make_db()
        det = RebuildCacheMissDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])

    def test_runs_without_events_returns_no_candidates(self):
        conn = _make_db()
        _insert_run(conn, "run-empty-1", "tengine", worktree=_WORKTREE_CWD)
        det = RebuildCacheMissDetector(conn)
        candidates = det.run()
        self.assertEqual(candidates, [])


# ── Prevalence integration ────────────────────────────────────────────────────


class TestPrevalenceIntegration(unittest.TestCase):
    """detector.prevalence() reports the correct rate after slow builds fire."""

    def setUp(self):
        self.conn = _make_db()

    def test_prevalence_rate_after_hits(self):
        # 4 runs for tengine; 2 have slow worktree builds
        for i in range(4):
            _insert_run(self.conn, f"run-prv{i}", "tengine")

        det = RebuildCacheMissDetector(self.conn)
        sig = "tengine:rebuild_cache_miss:tengine/foo"
        det.record_hit("run-prv0", sig, "tengine")
        det.record_hit("run-prv1", sig, "tengine")

        rate = det.prevalence("tengine", window_days=7)
        self.assertAlmostEqual(rate, 0.5)


if __name__ == "__main__":
    unittest.main()
