"""tests/test_label.py — Unit tests for label.py (PART B, outcome labeling).

Covers:
- _infer_from_behavior: thrash detection with positive AND negative fixtures
- _infer_from_behavior: abandoned detection
- _infer_from_behavior: clean detection
- Explicit source precedence (bead > pr > git_revert > behavior)
- label_history append (label transitions)
- compute_features_signals: thrash_score, revert_count, bash signals
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from label import (
    _count_bash_failures,
    _count_edit_fail_loops,
    _has_resolving_commit,
    _has_resolving_edit,
    _infer_from_behavior,
    apply_label,
    backfill,
    compute_features_signals,
    compute_label,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _event(tool_name="Bash", event="tool_call", tool_is_error=False,
           tool_summary="", tool_response_summary=""):
    return {
        "event": event,
        "tool_name": tool_name,
        "tool_is_error": tool_is_error,
        "tool_summary": tool_summary,
        "tool_response_summary": tool_response_summary,
    }


def _edit(fail=False):
    return _event(tool_name="Edit", tool_is_error=fail)


def _write():
    return _event(tool_name="Write")


def _bash(fail=False, cmd="ls"):
    resp = '{"exitCode":1,"stderr":"error: build failed"}' if fail else '{"stdout":"ok"}'
    return _event(
        tool_name="Bash",
        tool_is_error=fail,
        tool_summary=json.dumps({"command": cmd}),
        tool_response_summary=resp,
    )


def _read():
    return _event(tool_name="Read")


def _commit():
    return _event(
        tool_name="Bash",
        tool_summary='{"command":"git commit -m foo"}',
    )


def _make_run(close_reason="ended", git_branch=None, bead_id=None, event_count=10):
    return {
        "run_id": "TESTRUNID0001",
        "project": "test",
        "run_key_kind": "worktree",
        "close_reason": close_reason,
        "git_branch": git_branch,
        "bead_id": bead_id,
        "worktree": None,
        "event_count": event_count,
    }


def _wrap(events):
    """Wrap activity dicts as run_events rows (with raw_json encoding)."""
    rows = []
    for ev in events:
        rows.append({"raw_json": json.dumps({"data": ev})})
    return rows


# ── _count_bash_failures ──────────────────────────────────────────────────────

class TestCountBashFailures(unittest.TestCase):
    def test_no_failures(self):
        events = [_bash(fail=False) for _ in range(5)]
        total, fails = _count_bash_failures(events)
        self.assertEqual(total, 5)
        self.assertEqual(fails, 0)

    def test_with_failures(self):
        events = [_bash(fail=True), _bash(fail=False), _bash(fail=True)]
        total, fails = _count_bash_failures(events)
        self.assertEqual(total, 3)
        self.assertEqual(fails, 2)

    def test_non_bash_ignored(self):
        events = [_read(), _edit(), _bash(fail=True)]
        total, fails = _count_bash_failures(events)
        self.assertEqual(total, 1)
        self.assertEqual(fails, 1)


# ── _count_edit_fail_loops ────────────────────────────────────────────────────

class TestCountEditFailLoops(unittest.TestCase):
    def test_one_loop(self):
        events = [_edit(), _bash(fail=True), _edit()]
        self.assertEqual(_count_edit_fail_loops(events), 1)

    def test_three_loops(self):
        # Build: edit, bash-fail, edit, bash-fail, edit, bash-fail, edit
        events = [_edit(), _bash(fail=True), _edit(), _bash(fail=True),
                  _edit(), _bash(fail=True), _edit()]
        self.assertEqual(_count_edit_fail_loops(events), 3)

    def test_no_loop_when_bash_succeeds(self):
        events = [_edit(), _bash(fail=False), _edit()]
        self.assertEqual(_count_edit_fail_loops(events), 0)

    def test_no_loop_without_edit_before(self):
        events = [_bash(fail=True), _edit()]
        self.assertEqual(_count_edit_fail_loops(events), 0)

    def test_empty_events(self):
        self.assertEqual(_count_edit_fail_loops([]), 0)

    def test_write_counts_as_edit(self):
        events = [_write(), _bash(fail=True), _write()]
        self.assertEqual(_count_edit_fail_loops(events), 1)


# ── _infer_from_behavior: THRASH ──────────────────────────────────────────────

class TestInferThrash(unittest.TestCase):
    """Positive + negative fixtures for thrash detection."""

    def _make_thrash_events(self, loops=3, extra_reads=8):
        """Build a thrash sequence: edit→bash-fail loops + high reread rate."""
        events = []
        for _ in range(loops):
            events += [_edit(), _bash(fail=True)]
        events.append(_edit())
        # Add reads to push reread rate above threshold
        events += [_read() for _ in range(extra_reads)]
        return events

    def test_positive_thrash_three_loops_high_reread(self):
        """3 edit→bash-fail loops + high reread → thrashed."""
        run = _make_run(close_reason="idle_timeout", event_count=20)
        events = self._make_thrash_events(loops=3, extra_reads=8)
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertEqual(outcome, "thrashed")
        self.assertEqual(source, "behavior_inference")

    def test_positive_thrash_many_bash_fails(self):
        """>=10 bash calls with >30% failure rate + high reread → thrashed."""
        run = _make_run(close_reason="idle_timeout", event_count=30)
        events = (
            [_bash(fail=True) for _ in range(4)]
            + [_bash(fail=False) for _ in range(6)]
            + [_read() for _ in range(8)]  # reread rate high
        )
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertEqual(outcome, "thrashed")

    def test_negative_thrash_loops_but_low_reread(self):
        """3 edit→bash-fail loops but LOW reread rate → NOT thrashed.

        Rationale: loops alone could be a legitimate test-red-green cycle.
        The reread signal is required to confirm confusion/backtracking.
        """
        run = _make_run(close_reason="ended", event_count=15)
        events = []
        for _ in range(3):
            events += [_edit(), _bash(fail=True)]
        events.append(_edit())
        events += [_bash(fail=False) for _ in range(5)]  # mostly bash, low reread
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "thrashed")

    def test_negative_thrash_only_two_loops(self):
        """Only 2 edit→bash-fail loops (below threshold of 3) → NOT thrashed."""
        run = _make_run(close_reason="idle_timeout", event_count=15)
        events = []
        for _ in range(2):
            events += [_edit(), _bash(fail=True)]
        events.append(_edit())
        events += [_read() for _ in range(8)]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "thrashed")

    def test_negative_thrash_few_bash_calls(self):
        """<10 bash calls with high failure rate doesn't trigger bash_thrash path."""
        run = _make_run(close_reason="idle_timeout", event_count=15)
        events = (
            [_bash(fail=True) for _ in range(3)]
            + [_bash(fail=False) for _ in range(4)]
            + [_read() for _ in range(8)]
        )
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        # bash_thrash needs >=10 bash calls; with 7 it doesn't fire
        self.assertNotEqual(outcome, "thrashed")


# ── _infer_from_behavior: ABANDON ─────────────────────────────────────────────

class TestInferAbandon(unittest.TestCase):
    def test_positive_abandon_idle_timeout_no_resolving(self):
        """Idle timeout with no commit or tail edit → abandoned."""
        run = _make_run(close_reason="idle_timeout", event_count=10)
        # Only reads and bash calls, no edits in tail
        events = [_read() for _ in range(5)] + [_bash() for _ in range(5)]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertEqual(outcome, "abandoned")

    def test_recorder_shutdown_no_resolving_not_abandoned(self):
        """B1 fix: recorder_shutdown is operational, NOT semantic.
        Must never produce 'abandoned' — that's the B1 blocker fix."""
        run = _make_run(close_reason="recorder_shutdown", event_count=10)
        events = [_read() for _ in range(5)] + [_bash() for _ in range(5)]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "abandoned",
            "recorder_shutdown must NOT produce 'abandoned' (B1 fix)")

    def test_negative_abandon_too_few_events(self):
        """Runs with < ABANDON_MIN_EVENTS (5) events are not labeled abandoned."""
        run = _make_run(close_reason="idle_timeout", event_count=3)
        events = [_bash(), _read(), _bash()]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "abandoned")

    def test_negative_abandon_has_commit(self):
        """Idle timeout but run has a commit → not abandoned (likely clean)."""
        run = _make_run(close_reason="idle_timeout", event_count=10)
        events = [_read() for _ in range(4)] + [_bash()] + [_commit()]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "abandoned")

    def test_negative_abandon_has_tail_edit(self):
        """Idle timeout but last events include an Edit → not abandoned."""
        run = _make_run(close_reason="idle_timeout", event_count=10)
        events = [_read() for _ in range(5)] + [_bash() for _ in range(3)] + [_edit()]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "abandoned")

    def test_negative_abandon_clean_close_reason(self):
        """close_reason='ended' (not idle) → abandon check not triggered."""
        run = _make_run(close_reason="ended", event_count=10)
        events = [_bash() for _ in range(10)]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertNotEqual(outcome, "abandoned")


# ── _infer_from_behavior: CLEAN ───────────────────────────────────────────────

class TestInferClean(unittest.TestCase):
    def test_clean_with_commit(self):
        run = _make_run(close_reason="ended", event_count=8)
        events = [_read(), _edit(), _bash(), _commit()]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertEqual(outcome, "clean")

    def test_clean_with_tail_edit(self):
        run = _make_run(close_reason="ended", event_count=6)
        events = [_read(), _bash(), _read(), _bash(), _edit()]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertEqual(outcome, "clean")

    def test_small_run_no_signal_is_null(self):
        """B5 fix: small run (< 5 events) with no resolving action → null, not 'clean'.
        Requiring a POSITIVE resolving signal to assert 'clean' prevents poisoning
        the training set with opaque micro-runs."""
        run = _make_run(close_reason="idle_timeout", event_count=2)
        events = [_bash(), _read()]
        outcome, source = _infer_from_behavior(run, events, verbose=False)
        self.assertIsNone(outcome,
            "Micro/opaque runs with no signal must be null (B5 fix)")


# ── Explicit source precedence ────────────────────────────────────────────────

class TestExplicitSourcePrecedence(unittest.TestCase):
    """Explicit labels must win over behavior inference."""

    def _mock_bead_landed(self, bead_id):
        """Mock bead close → landed."""
        m = MagicMock(return_value=("landed", "bead_close"))
        return patch("label.label_from_bead", m)

    def _mock_pr_abandoned(self, branch):
        m = MagicMock(return_value=("abandoned", "pr_closed_unmerged"))
        return patch("label.label_from_pr", m)

    def test_bead_explicit_overrides_inferred_thrash(self):
        """Even a thrash-shaped run labels as 'landed' when bead is closed+merged."""
        run = _make_run(bead_id="nervous-bus-fhr1q", close_reason="idle_timeout")
        # Thrash-shaped events
        events = _wrap(
            [_edit(), _bash(fail=True)] * 4 + [_read()] * 8
        )
        with self._mock_bead_landed("nervous-bus-fhr1q"):
            with patch("label.label_from_pr", MagicMock(return_value=None)):
                result = compute_label(run, events, verbose=False)
        self.assertIsNotNone(result)
        outcome, source = result
        self.assertEqual(outcome, "landed")
        self.assertEqual(source, "bead_close")

    def test_pr_explicit_overrides_inferred_abandon(self):
        """An abandoned-shaped run labels as 'abandoned' via PR, not inferred."""
        run = _make_run(git_branch="feat/some-feature", close_reason="idle_timeout",
                        event_count=10)
        events = _wrap([_read() for _ in range(10)])
        with patch("label.label_from_bead", MagicMock(return_value=None)):
            with self._mock_pr_abandoned("feat/some-feature"):
                result = compute_label(run, events, verbose=False)
        self.assertIsNotNone(result)
        outcome, source = result
        self.assertEqual(outcome, "abandoned")
        self.assertEqual(source, "pr_closed_unmerged")

    def test_no_explicit_falls_back_to_inference(self):
        """When both bead and PR return None, behavior inference runs."""
        run = _make_run(close_reason="ended", event_count=5)
        events = _wrap([_read(), _edit(), _commit()])
        with patch("label.label_from_bead", MagicMock(return_value=None)):
            with patch("label.label_from_pr", MagicMock(return_value=None)):
                result = compute_label(run, events, verbose=False)
        self.assertIsNotNone(result)
        outcome, source = result
        self.assertEqual(source, "behavior_inference")

    def test_main_branch_skips_pr_check(self):
        """Runs on 'main' branch skip the PR lookup (structural branch)."""
        run = _make_run(git_branch="main", close_reason="ended", event_count=5)
        events = _wrap([_commit()])
        with patch("label.label_from_bead", MagicMock(return_value=None)):
            with patch("label.label_from_pr") as mock_pr:
                result = compute_label(run, events, verbose=False)
        # label_from_pr should NOT have been called for 'main'
        mock_pr.assert_not_called()


# ── label_history transitions ─────────────────────────────────────────────────

class TestLabelHistoryTransitions(unittest.TestCase):
    """apply_label appends to label_history on transitions, does nothing on no-change."""

    def _make_db(self, outcome=None, label_version=None):
        """Create an in-memory SQLite DB with a test run."""
        import sqlite3
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                outcome TEXT,
                labeled_at TEXT,
                label_version INTEGER,
                label_history TEXT NOT NULL DEFAULT '[]',
                features TEXT NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute(
            "INSERT INTO runs (run_id, outcome, label_version, label_history, features) "
            "VALUES (?, ?, ?, '[]', '{}')",
            ("run-001", outcome, label_version),
        )
        return conn

    def test_first_label_sets_version_1(self):
        conn = self._make_db(outcome=None, label_version=None)
        changed = apply_label(conn, "run-001", "clean", "behavior_inference")
        self.assertTrue(changed)
        row = conn.execute(
            "SELECT outcome, label_version, label_history FROM runs WHERE run_id='run-001'"
        ).fetchone()
        self.assertEqual(row[0], "clean")
        self.assertEqual(row[1], 1)
        history = json.loads(row[2])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["outcome"], "clean")
        self.assertEqual(history[0]["source"], "behavior_inference")
        self.assertEqual(history[0]["label_version"], 1)

    def test_same_label_does_not_append(self):
        conn = self._make_db(outcome="clean", label_version=1)
        # Pre-populate label_history
        conn.execute(
            "UPDATE runs SET label_history=? WHERE run_id='run-001'",
            (json.dumps([{"outcome": "clean", "labeled_at": "2026-01-01T00:00:00Z",
                          "label_version": 1, "source": "behavior_inference"}]),),
        )
        changed = apply_label(conn, "run-001", "clean", "behavior_inference")
        self.assertFalse(changed)
        row = conn.execute("SELECT label_version, label_history FROM runs").fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(len(json.loads(row[1])), 1)  # no new entry

    def test_transition_clean_to_landed(self):
        """PR merges hours later: clean → landed transition appends to history."""
        conn = self._make_db(outcome="clean", label_version=1)
        conn.execute(
            "UPDATE runs SET label_history=? WHERE run_id='run-001'",
            (json.dumps([{"outcome": "clean", "labeled_at": "2026-01-01T00:00:00Z",
                          "label_version": 1, "source": "behavior_inference"}]),),
        )
        changed = apply_label(conn, "run-001", "landed", "pr_merge")
        self.assertTrue(changed)
        row = conn.execute(
            "SELECT outcome, label_version, label_history FROM runs"
        ).fetchone()
        self.assertEqual(row[0], "landed")
        self.assertEqual(row[1], 2)
        history = json.loads(row[2])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["outcome"], "clean")
        self.assertEqual(history[1]["outcome"], "landed")
        self.assertEqual(history[1]["source"], "pr_merge")

    def test_triple_transition(self):
        """clean → thrashed → landed: three history entries, version bumps."""
        conn = self._make_db(outcome=None, label_version=None)
        apply_label(conn, "run-001", "clean", "behavior_inference")
        apply_label(conn, "run-001", "thrashed", "behavior_inference")
        apply_label(conn, "run-001", "landed", "pr_merge")
        row = conn.execute(
            "SELECT outcome, label_version, label_history FROM runs"
        ).fetchone()
        self.assertEqual(row[0], "landed")
        self.assertEqual(row[1], 3)
        history = json.loads(row[2])
        self.assertEqual(len(history), 3)
        self.assertEqual([h["outcome"] for h in history],
                         ["clean", "thrashed", "landed"])

    def test_dry_run_does_not_write(self):
        conn = self._make_db(outcome=None, label_version=None)
        apply_label(conn, "run-001", "clean", "behavior_inference", dry_run=True)
        row = conn.execute("SELECT outcome FROM runs").fetchone()
        self.assertIsNone(row[0])  # unchanged


# ── compute_features_signals ──────────────────────────────────────────────────

class TestComputeFeaturesSignals(unittest.TestCase):
    def test_signals_for_thrash(self):
        run = _make_run()
        # Build: edit, bash-fail, edit, bash-fail, edit, bash-fail, edit — 3 loops
        loop_events = []
        for _ in range(3):
            loop_events += [_edit(), _bash(fail=True)]
        loop_events.append(_edit())  # trailing edit to close the 3rd loop
        events = _wrap(loop_events + [_read() for _ in range(8)])
        signals = compute_features_signals(run, events)
        self.assertIn("thrash_edit_fail_loops", signals)
        self.assertGreaterEqual(signals["thrash_edit_fail_loops"], 3)
        self.assertIn("bash_fail_rate", signals)
        self.assertIn("reread_rate", signals)

    def test_signals_for_clean_run(self):
        run = _make_run()
        events = _wrap([_read(), _edit(), _commit()])
        signals = compute_features_signals(run, events)
        self.assertTrue(signals.get("has_resolving_commit", False))

    def test_empty_events_safe(self):
        run = _make_run()
        signals = compute_features_signals(run, [])
        self.assertEqual(signals, {})


# ── Integration: backfill over a real (temp) DB ───────────────────────────────

class TestBackfillIntegration(unittest.TestCase):
    """Smoke test: backfill over a minimal in-memory DB."""

    def _make_db_file(self) -> Path:
        """Create a minimal runs.db in a temp file and return its path."""
        import sqlite3
        tmp = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(tmp, isolation_level=None)
        conn.execute("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                run_key TEXT NOT NULL DEFAULT '',
                run_key_kind TEXT NOT NULL DEFAULT 'session',
                host_conversation_id TEXT,
                project TEXT NOT NULL DEFAULT 'test',
                agent_kind TEXT NOT NULL DEFAULT 'host_claude_code',
                session_id TEXT,
                agent_id TEXT,
                started TEXT NOT NULL DEFAULT '2026-06-13T00:00:00Z',
                ended TEXT NOT NULL DEFAULT '2026-06-13T01:00:00Z',
                close_reason TEXT DEFAULT 'ended',
                continues_run_id TEXT,
                event_count INTEGER NOT NULL DEFAULT 1,
                tool_histogram TEXT NOT NULL DEFAULT '{}',
                worktree TEXT,
                worktree_slug TEXT,
                git_branch TEXT,
                bead_id TEXT,
                outcome TEXT,
                labeled_at TEXT,
                label_version INTEGER,
                label_history TEXT NOT NULL DEFAULT '[]',
                features TEXT NOT NULL DEFAULT '{}',
                schema_version TEXT NOT NULL DEFAULT '1',
                recorded_at TEXT NOT NULL DEFAULT '2026-06-13T00:00:00Z'
            )
        """)
        conn.execute("""
            CREATE TABLE run_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_ts TEXT NOT NULL,
                event_type TEXT NOT NULL,
                raw_json TEXT NOT NULL
            )
        """)

        # Insert a run with commit (should be 'clean')
        conn.execute(
            "INSERT INTO runs (run_id, project, event_count) VALUES ('run-clean-01', 'testproject', 5)"
        )
        for i, ev in enumerate([_read(), _edit(), _commit()]):
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-clean-01", i + 1, "2026-06-13T00:00:00Z",
                 "bus.agent.activity.v1", json.dumps({"data": ev})),
            )

        # Insert an idle-timeout run with no resolving action (should be 'abandoned')
        conn.execute(
            "INSERT INTO runs (run_id, project, close_reason, event_count) "
            "VALUES ('run-abandon-01', 'testproject', 'idle_timeout', 10)"
        )
        for i, ev in enumerate([_read() for _ in range(5)] + [_bash() for _ in range(5)]):
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) "
                "VALUES (?, ?, ?, ?, ?)",
                ("run-abandon-01", i + 1, "2026-06-13T00:00:00Z",
                 "bus.agent.activity.v1", json.dumps({"data": ev})),
            )

        conn.close()
        return Path(tmp)

    def test_backfill_smoke(self):
        db_path = self._make_db_file()
        try:
            with patch("label.label_from_bead", MagicMock(return_value=None)):
                with patch("label.label_from_pr", MagicMock(return_value=None)):
                    results = backfill(db_path, dry_run=False, verbose=False)

            self.assertEqual(len(results), 2)
            by_id = {r["run_id"]: r for r in results}

            self.assertEqual(by_id["run-clean-01"]["outcome"], "clean")
            self.assertEqual(by_id["run-abandon-01"]["outcome"], "abandoned")
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
