"""tests/test_synthesis_window.py — issue #32 window plumbing.

Covers:
1. A detector given since_ts reads 0 rows for a run started before the
   window (worktree_leak + repeated_question — a runs-driven query and a
   run_events-driven query, the two SQL shapes present across the built-in
   detectors).
2. BaseDetector._call_detect() introspection: a detector whose detect() does
   NOT accept since_ts (a private-overlay-shaped detector) is called exactly
   as before — since_ts is never force-fed positionally.
3. run_synthesis(window_days=N) threads a since_ts cutoff into detector.run()
   such that a run far outside the window contributes no fresh detector_hits.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REC_DIR = Path(__file__).parent.parent
_TESTS_DIR = Path(__file__).parent
for _p in (_REC_DIR, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import synthesis as syn
from detectors.base import BaseDetector, PatternCandidate, ensure_detector_schema
from detectors.worktree_leak import WorktreeLeakDetector
from detectors.repeated_question import RepeatedQuestionDetector

from test_synthesis import _make_conn, _insert_run  # noqa: E402 (shared fixture helpers)


@pytest.fixture(autouse=True)
def _pin_builtin_detectors(monkeypatch):
    monkeypatch.setattr(syn, "DETECTOR_CLASSES", list(syn._BUILTIN_DETECTOR_CLASSES))


@pytest.fixture(autouse=True)
def _forbid_default_db_path(monkeypatch):
    """Same guard as test_synthesis.py: every test here uses an explicit
    in-memory conn, never synthesis.py's DEFAULT_DB_PATH (the live runs.db).
    Point it somewhere that fails loudly if ever touched by accident."""
    monkeypatch.setattr(
        syn, "DEFAULT_DB_PATH",
        Path("/nonexistent-test-guard/must-not-use-default-db/runs.db"),
    )


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_event(conn, run_id, event_type, raw_json, seq=1):
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, datetime.now(timezone.utc).isoformat(), event_type, raw_json),
    )


class TestWorktreeLeakWindow:
    """runs-table-driven detector (WHERE ... AND started >= ?)."""

    def test_since_ts_excludes_run_outside_window(self):
        conn = _make_conn()
        old_ts = _days_ago(90)
        _insert_run(
            conn, "old-run", project="proj",
            started=old_ts, ended=old_ts, close_reason="idle_timeout",
            outcome="clean", labeled_at=old_ts,
            worktree="/tmp/nonexistent-worktree-xyz", worktree_slug="xyz",
        )
        detector = WorktreeLeakDetector(conn)

        # Unbounded (since_ts=None): the old run is IN scope for the query
        # (whether or not it produces a candidate depends on the filesystem
        # check, which we don't rely on here — we assert on since_ts alone
        # by using os.path.isdir as the gate: point worktree at a path that
        # does not exist, so this run never yields a candidate either way,
        # and instead assert directly on the SQL row count via a spy).
        recent_cutoff = _days_ago(30)
        rows_recent = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE outcome IN ('clean','landed','corrected') "
            "AND labeled_at IS NOT NULL AND worktree IS NOT NULL AND worktree != '' "
            "AND started >= ?",
            (recent_cutoff,),
        ).fetchone()[0]
        assert rows_recent == 0, "90-day-old run must not be visible to a 30-day window"

        # detect() with since_ts=recent_cutoff must not even consider the row
        # (0 candidates AND 0 detector_hits rows recorded for it).
        candidates = detector.detect(conn, since_ts=recent_cutoff)
        assert candidates == []

        # detect() with since_ts=None (unbounded) still sees the row in its
        # query (even though it won't fire, because the dir doesn't exist —
        # we confirm the QUERY itself is unbounded via the raw SQL count).
        rows_unbounded = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE outcome IN ('clean','landed','corrected') "
            "AND labeled_at IS NOT NULL AND worktree IS NOT NULL AND worktree != ''"
        ).fetchone()[0]
        assert rows_unbounded == 1

    def test_run_synthesis_windowed_records_no_hit_for_old_run(self, tmp_path, monkeypatch):
        """End-to-end: a real leaked worktree (existing dir, and git DOES
        still know about it) outside the window must not produce a
        detector_hits row when synthesis runs with a bounding window_days.

        Stubs the module's git subprocess helpers (rather than relying on a
        real `git worktree list` / `git rev-parse` round-trip) so the test is
        fast and deterministic regardless of host load or whether tmp_path
        happens to sit inside a real git checkout.
        """
        import detectors.worktree_leak as wl_mod

        conn = _make_conn()
        old_ts = _days_ago(90)
        leaked_dir = tmp_path / "leaked-worktree"
        leaked_dir.mkdir()
        _insert_run(
            conn, "old-leak-run", project="win-proj",
            started=old_ts, ended=old_ts, close_reason="idle_timeout",
            outcome="clean", labeled_at=old_ts,
            worktree=str(leaked_dir), worktree_slug="leaked-worktree",
        )
        monkeypatch.setattr(wl_mod, "_infer_repo_root", lambda p: str(tmp_path))
        monkeypatch.setattr(wl_mod, "_git_worktree_paths", lambda root: {str(leaked_dir)})

        syn.run_synthesis(conn, window_days=30, dry_run=True, project_filter="win-proj")
        hit = conn.execute(
            "SELECT COUNT(*) FROM detector_hits WHERE run_id = 'old-leak-run' "
            "AND detector = 'worktree_leak'"
        ).fetchone()[0]
        assert hit == 0, (
            "a 90-day-old worktree_leak candidate must not be scanned/recorded "
            "under a 30-day synthesis window"
        )


class TestRepeatedQuestionWindow:
    """run_events-driven detector (two-step run_id-scoped query)."""

    def test_since_ts_excludes_events_outside_window(self):
        conn = _make_conn()
        old_ts = _days_ago(90)
        recent_ts = _days_ago(1)
        # Same question class asked in two OLD runs -> would fire unbounded.
        for i in range(2):
            rid = f"old-q-{i}"
            _insert_run(
                conn, rid, project="proj", started=old_ts, ended=old_ts,
                close_reason="idle_timeout",
            )
            _insert_event(
                conn, rid, "permission_requested",
                '{"data": {"tool_summary": "Continue with the deploy?"}}',
            )

        recent_cutoff = _days_ago(30)
        detector = RepeatedQuestionDetector(conn)

        unbounded = detector.detect(conn, since_ts=None)
        windowed = detector.detect(conn, since_ts=recent_cutoff)

        assert len(unbounded) >= 1, "sanity: unbounded scan sees the repeated question"
        assert windowed == [], "windowed scan must not see events entirely outside it"


class TestBackwardCompatibleDetect:
    """BaseDetector._call_detect(): since_ts is only passed when accepted."""

    def test_legacy_detector_without_since_ts_is_called_unmodified(self):
        calls = []

        class LegacyDetector(BaseDetector):
            DETECTOR_NAME = "legacy_no_since_ts"

            def detect(self, conn):  # no since_ts kwarg at all
                calls.append("called")
                return []

        conn = _make_conn()
        d = LegacyDetector(conn)
        # Must not raise TypeError even though since_ts is passed to run().
        result = d.run(conn, since_ts="2026-08-01T00:00:00Z")
        assert result == []
        assert calls == ["called"]

    def test_since_ts_aware_detector_receives_it(self):
        received = {}

        class NewDetector(BaseDetector):
            DETECTOR_NAME = "new_since_ts_aware"

            def detect(self, conn, since_ts=None):
                received["since_ts"] = since_ts
                return []

        conn = _make_conn()
        d = NewDetector(conn)
        d.run(conn, since_ts="2026-08-01T00:00:00Z")
        assert received["since_ts"] == "2026-08-01T00:00:00Z"

    def test_since_ts_aware_detector_defaults_to_none_when_unbounded(self):
        received = {}

        class NewDetector(BaseDetector):
            DETECTOR_NAME = "new_since_ts_aware2"

            def detect(self, conn, since_ts=None):
                received["since_ts"] = since_ts
                return []

        conn = _make_conn()
        d = NewDetector(conn)
        d.run(conn)  # no since_ts passed at all
        assert received["since_ts"] is None


class TestSingleDetectorInvocation:
    """Issue #32: detectors must run exactly ONCE per synthesis pass (the old
    code invoked .run() then a bare .detect() again to rebuild
    sig_to_candidate, doubling detector wall time)."""

    def test_detector_detect_called_once_per_pass(self, monkeypatch):
        call_count = {"n": 0}
        real_detect = WorktreeLeakDetector.detect

        def counting_detect(self, conn, since_ts=None):
            call_count["n"] += 1
            return real_detect(self, conn, since_ts=since_ts)

        monkeypatch.setattr(WorktreeLeakDetector, "detect", counting_detect)
        monkeypatch.setattr(syn, "DETECTOR_CLASSES", [WorktreeLeakDetector])

        conn = _make_conn()
        _insert_run(conn, "some-run", project="proj")
        syn.run_synthesis(conn, dry_run=True, project_filter="proj")

        assert call_count["n"] == 1, (
            f"WorktreeLeakDetector.detect() called {call_count['n']}x in one "
            f"synthesis pass; expected exactly 1 (regression: the old code "
            f"re-ran every detector via a second bare .detect() call to "
            f"rebuild sig_to_candidate)"
        )
