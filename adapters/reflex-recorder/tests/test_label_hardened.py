"""tests/test_label_hardened.py — Tests for B1–B5, M1–M3, and precedence guard.

Covers every fix from the adversarial audit:

B1 — recorder_shutdown NOT abandoned; continues_run_id survives restart
B2 — failure detection: tool_is_error primary, tool_response_summary fallback
B3 — resolving commit: structured gitOperation field + git -C pattern
B4 — gh -C invocation (mocked); degrades to None on failure
B5 — null-not-clean: opaque/micro/no-signal runs get outcome=None
M1 — bus.bead.closed triggers bd lookup, never assigns outcome directly
M2 — bd structured resolution field, not free-text substring
M3 — abandoned over-fire: require failure signal or substantial run
Precedence — explicit label beats inferred; no wrong history entry
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from label import (
    _bash_is_failure,
    _count_bash_failures,
    _count_edit_fail_loops,
    _has_resolving_commit,
    _has_resolving_edit,
    _infer_from_behavior,
    _redis_bead_closed_outcome,
    apply_label,
    backfill,
    compute_label,
    label_from_bead,
    label_from_pr,
)


# ── Shared fixture helpers ────────────────────────────────────────────────────

def _ev(tool_name="Bash", event="tool_call", tool_is_error=False,
        tool_summary="", tool_response_summary=""):
    return {
        "event": event,
        "tool_name": tool_name,
        "tool_is_error": tool_is_error,
        "tool_summary": tool_summary,
        "tool_response_summary": tool_response_summary,
    }


def _bash(fail=False, cmd="ls", resp_override=None):
    if fail:
        resp = resp_override or '{"exitCode":1,"stderr":"error: build failed","interrupted":false}'
    else:
        resp = resp_override or '{"exitCode":0,"stdout":"ok"}'
    return _ev(
        tool_name="Bash",
        tool_is_error=fail,
        tool_summary=json.dumps({"command": cmd}),
        tool_response_summary=resp,
    )


def _bash_no_is_error(fail_resp=False, cmd="ls"):
    """Bash event WITHOUT tool_is_error (pre-upgrade accrued event shape).
    Failure is only in tool_response_summary.
    """
    if fail_resp:
        resp = '{"exitCode":1,"stderr":"error: build failed","interrupted":false}'
    else:
        resp = '{"exitCode":0,"stdout":"ok"}'
    return _ev(
        tool_name="Bash",
        tool_is_error=False,  # explicitly False, not present
        tool_summary=json.dumps({"command": cmd}),
        tool_response_summary=resp,
    )


def _git_op_commit():
    """Bash event with structured gitOperation.commit in tool_response_summary."""
    resp = json.dumps({
        "gitOperation": {"commit": {"kind": "committed", "sha": "abc123f"}},
        "interrupted": False,
        "stderr": "",
        "stdout": "[reflexarc abc123f] feat: something\n 3 files changed",
    })
    return _ev(
        tool_name="Bash",
        tool_summary=json.dumps({"command": "git -C /home/eric/projects/nervous-bus commit -m 'feat: something'"}),
        tool_response_summary=resp,
    )


def _git_commit_simple():
    """Bash event with plain 'git commit' (old-style, no -C)."""
    return _ev(
        tool_name="Bash",
        tool_summary=json.dumps({"command": "git commit -m 'fix: something'"}),
        tool_response_summary='{"stdout":"[main abc123] fix: something","stderr":""}',
    )


def _edit():
    return _ev(tool_name="Edit")


def _read():
    return _ev(tool_name="Read")


def _make_run(close_reason="ended", git_branch=None, bead_id=None, event_count=10,
              worktree=None):
    return {
        "run_id": "TESTRUNID0001",
        "project": "test",
        "run_key_kind": "worktree",
        "close_reason": close_reason,
        "git_branch": git_branch,
        "bead_id": bead_id,
        "worktree": worktree,
        "event_count": event_count,
    }


def _wrap(events):
    """Wrap activity dicts as run_events rows (with raw_json encoding)."""
    return [{"raw_json": json.dumps({"data": ev})} for ev in events]


def _make_db(runs: list[dict]) -> Path:
    """Create a minimal runs.db temp file with the given run rows."""
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
    for run in runs:
        conn.execute(
            "INSERT INTO runs (run_id, project, close_reason, event_count, git_branch, bead_id, worktree) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run["run_id"], run.get("project", "test"),
                run.get("close_reason", "ended"),
                run.get("event_count", 1),
                run.get("git_branch"),
                run.get("bead_id"),
                run.get("worktree"),
            ),
        )
        for i, ev in enumerate(run.get("events", [])):
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (run["run_id"], i + 1, "2026-06-13T00:00:00Z",
                 "bus.agent.activity.v1", json.dumps({"data": ev})),
            )
    conn.close()
    return Path(tmp)


# ── B1: recorder_shutdown NOT abandoned ───────────────────────────────────────

class TestB1RecorderShutdownNotAbandoned(unittest.TestCase):
    """recorder_shutdown MUST NOT contribute to 'abandoned' outcome."""

    def test_recorder_shutdown_not_abandoned_with_no_signals(self):
        """A run closed by recorder_shutdown with 10 events and no resolving
        action must NOT be labeled 'abandoned' — it's an operational close."""
        run = _make_run(close_reason="recorder_shutdown", event_count=10)
        events = [_read() for _ in range(5)] + [_bash() for _ in range(5)]
        outcome, source = _infer_from_behavior(run, events)
        self.assertNotEqual(outcome, "abandoned",
            "recorder_shutdown must never produce 'abandoned'")

    def test_recorder_shutdown_with_commit_is_clean(self):
        """recorder_shutdown with a resolving commit → clean."""
        run = _make_run(close_reason="recorder_shutdown", event_count=10)
        events = [_read() for _ in range(5)] + [_git_op_commit()]
        outcome, source = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "clean")

    def test_recorder_shutdown_without_commit_is_null(self):
        """recorder_shutdown with no positive resolving signal → null (not clean, not abandoned)."""
        run = _make_run(close_reason="recorder_shutdown", event_count=10)
        events = [_read() for _ in range(5)] + [_bash() for _ in range(5)]
        outcome, source = _infer_from_behavior(run, events)
        # Must not be abandoned; with no commit it should be null (B5)
        self.assertIsNone(outcome)

    def test_idle_timeout_with_no_resolving_still_abandoned(self):
        """idle_timeout with 10+ events and no resolving action → abandoned (positive case)."""
        run = _make_run(close_reason="idle_timeout", event_count=10)
        # Need substantial events (M3: >= 2 * ABANDON_MIN_EVENTS = 10)
        events = [_read() for _ in range(5)] + [_bash() for _ in range(5)]
        outcome, source = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "abandoned")

    def test_backfill_recorder_shutdown_run_gets_null_not_clean_not_abandoned(self):
        """Integration: backfill over a recorder_shutdown run with no commit → null."""
        db_path = _make_db([{
            "run_id": "run-shutdown-01",
            "close_reason": "recorder_shutdown",
            "event_count": 10,
            "events": [_read() for _ in range(5)] + [_bash() for _ in range(5)],
        }])
        try:
            with patch("label.label_from_bead", MagicMock(return_value=None)):
                with patch("label.label_from_pr", MagicMock(return_value=None)):
                    results = backfill(db_path, dry_run=True, verbose=False)
            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertIsNone(r["outcome"], f"Expected null, got {r['outcome']!r}")
        finally:
            db_path.unlink(missing_ok=True)


# ── B1: continues_run_id survives restart ─────────────────────────────────────

class TestB1ContinuesRunIdRestart(unittest.TestCase):
    """latest_run_id_per_key in store.py and _rebuild_last_closed_id in recorder.py."""

    def test_store_latest_run_id_per_key_basic(self):
        """Store.latest_run_id_per_key returns the most recently closed run
        per run_key, excluding recorder_shutdown runs."""
        from store import SQLiteStore
        import tempfile

        tmp = tempfile.mktemp(suffix=".db")
        store = SQLiteStore(Path(tmp))
        try:
            # Save two runs for the same run_key
            store.save_run({
                "run_id": "run-a", "run_key": "key-1", "run_key_kind": "session",
                "host_conversation_id": None, "project": "p", "agent_kind": "host_claude_code",
                "session_id": None, "agent_id": None,
                "started": "2026-06-13T05:00:00Z", "ended": "2026-06-13T05:10:00Z",
                "close_reason": "idle_timeout", "continues_run_id": None,
                "event_count": 5, "tool_histogram": {}, "worktree": None,
                "worktree_slug": None, "git_branch": None, "bead_id": None,
                "outcome": None, "labeled_at": None, "label_version": None,
                "label_history": [], "features": {}, "schema_version": "1",
            })
            store.save_run({
                "run_id": "run-b", "run_key": "key-1", "run_key_kind": "session",
                "host_conversation_id": None, "project": "p", "agent_kind": "host_claude_code",
                "session_id": None, "agent_id": None,
                "started": "2026-06-13T05:20:00Z", "ended": "2026-06-13T05:30:00Z",
                "close_reason": "ended", "continues_run_id": "run-a",
                "event_count": 3, "tool_histogram": {}, "worktree": None,
                "worktree_slug": None, "git_branch": None, "bead_id": None,
                "outcome": None, "labeled_at": None, "label_version": None,
                "label_history": [], "features": {}, "schema_version": "1",
            })
            # Save a recorder_shutdown run for key-2 (should be excluded)
            store.save_run({
                "run_id": "run-c", "run_key": "key-2", "run_key_kind": "session",
                "host_conversation_id": None, "project": "p", "agent_kind": "host_claude_code",
                "session_id": None, "agent_id": None,
                "started": "2026-06-13T05:00:00Z", "ended": "2026-06-13T05:05:00Z",
                "close_reason": "recorder_shutdown", "continues_run_id": None,
                "event_count": 2, "tool_histogram": {}, "worktree": None,
                "worktree_slug": None, "git_branch": None, "bead_id": None,
                "outcome": None, "labeled_at": None, "label_version": None,
                "label_history": [], "features": {}, "schema_version": "1",
            })

            result = store.latest_run_id_per_key()
            # key-1 should return the most recent (run-b by ended timestamp)
            self.assertIn("key-1", result)
            self.assertEqual(result["key-1"], "run-b")
            # key-2 should NOT be in result (recorder_shutdown excluded)
            self.assertNotIn("key-2", result)
        finally:
            store.close()
            Path(tmp).unlink(missing_ok=True)

    def test_recorder_rebuilds_last_closed_id(self):
        """Recorder.__init__ calls _rebuild_last_closed_id to restore continuity."""
        from recorder import Recorder

        cfg = {
            "idle_timeout_s": 900.0,
            "metrics_interval_s": 60.0,
            "tick_interval_s": 30.0,
            "db_path": None,
        }

        # Patch SQLiteStore and latest_run_id_per_key
        mock_store = MagicMock()
        mock_store.db_path = "/tmp/fake.db"
        mock_store.latest_run_id_per_key.return_value = {"key-abc": "run-xyz"}

        with patch("recorder.SQLiteStore", return_value=mock_store):
            rec = Recorder(cfg)

        # After init, _last_closed_id should have been populated
        self.assertEqual(rec.segmenter._last_closed_id.get("key-abc"), "run-xyz")


# ── B2: Failure detection with real event shapes ──────────────────────────────

class TestB2FailureDetection(unittest.TestCase):
    """tool_is_error primary; tool_response_summary structured fallback."""

    def test_tool_is_error_true_detects_failure(self):
        """tool_is_error=True → failure (new hook format)."""
        ev = _bash(fail=True)
        ev["tool_is_error"] = True
        self.assertTrue(_bash_is_failure(ev))

    def test_tool_is_error_false_structured_exit_1(self):
        """tool_is_error absent/false + exitCode=1 in structured resp → failure."""
        ev = _bash_no_is_error(fail_resp=True)
        self.assertTrue(_bash_is_failure(ev))

    def test_tool_is_error_false_no_exit_code_no_failure(self):
        """tool_is_error=False, exitCode=0 in resp → NOT a failure."""
        ev = _bash_no_is_error(fail_resp=False)
        self.assertFalse(_bash_is_failure(ev))

    def test_informational_stderr_not_a_failure(self):
        """Cargo warning in stderr with exit 0 is not a failure."""
        resp = '{"exitCode":0,"stderr":"warning: unused variable x","interrupted":false}'
        ev = _ev(
            tool_name="Bash", tool_is_error=False,
            tool_summary='{"command":"cargo build"}',
            tool_response_summary=resp,
        )
        self.assertFalse(_bash_is_failure(ev))

    def test_empty_response_summary_not_failure(self):
        ev = _ev(tool_name="Bash", tool_is_error=False,
                 tool_summary='{"command":"ls"}', tool_response_summary="")
        self.assertFalse(_bash_is_failure(ev))

    def test_bash_fail_rate_non_zero_on_real_data(self):
        """With real accrued event shapes, bash_fail_rate can be non-zero."""
        events = [
            _bash_no_is_error(fail_resp=True),   # failure via structured exitCode
            _bash_no_is_error(fail_resp=False),
            _bash_no_is_error(fail_resp=False),
            _bash_no_is_error(fail_resp=False),
        ]
        total, fails = _count_bash_failures(events)
        self.assertEqual(total, 4)
        self.assertEqual(fails, 1)
        rate = fails / total
        self.assertGreater(rate, 0.0, "bash_fail_rate must be non-zero when failures exist")

    def test_thrash_detect_with_no_tool_is_error_field(self):
        """Thrash detection works even when tool_is_error is absent (accrued events)."""
        run = _make_run(close_reason="idle_timeout", event_count=20)
        # Build edit→bash-fail loops using structured response (no tool_is_error)
        events = []
        for _ in range(3):
            events.append(_edit())
            events.append(_bash_no_is_error(fail_resp=True))
        events.append(_edit())
        # Add reads to push reread rate above threshold
        events += [_read() for _ in range(8)]
        outcome, _ = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "thrashed")


# ── B3: Resolving commit detection ───────────────────────────────────────────

class TestB3ResolvingCommitDetection(unittest.TestCase):
    """gitOperation.commit field and git -C <path> commit pattern."""

    def test_git_op_commit_detected(self):
        """Structured gitOperation.commit in tool_response_summary → resolving."""
        events = [_git_op_commit()]
        self.assertTrue(_has_resolving_commit(events))

    def test_git_op_push_detected(self):
        """Structured gitOperation.push → resolving."""
        resp = json.dumps({
            "gitOperation": {"push": {"remote": "origin", "branch": "main"}},
            "interrupted": False, "stderr": "", "stdout": "",
        })
        ev = _ev(
            tool_name="Bash",
            tool_summary='{"command":"git push origin main"}',
            tool_response_summary=resp,
        )
        self.assertTrue(_has_resolving_commit([ev]))

    def test_git_dash_c_commit_detected(self):
        """git -C <path> commit (worktree/loom pattern) → resolving."""
        ev = _ev(
            tool_name="Bash",
            tool_summary='{"command":"git -C /home/eric/projects/nervous-bus commit -m \'fix: thing\'"}',
            tool_response_summary='{"stdout":"[reflexarc abc123] fix: thing","stderr":"","interrupted":false}',
        )
        self.assertTrue(_has_resolving_commit([ev]))

    def test_plain_git_commit_still_detected(self):
        """Plain 'git commit' still works as tertiary match."""
        ev = _git_commit_simple()
        self.assertTrue(_has_resolving_commit([ev]))

    def test_no_commit_returns_false(self):
        """Pure read/bash run with no commit → False."""
        events = [_read(), _bash(), _bash(), _read()]
        self.assertFalse(_has_resolving_commit(events))

    def test_bash_git_log_not_a_commit(self):
        """git log command is not a resolving commit."""
        ev = _ev(
            tool_name="Bash",
            tool_summary='{"command":"git log --oneline -5"}',
            tool_response_summary='{"stdout":"abc1234 fix: something","stderr":""}',
        )
        self.assertFalse(_has_resolving_commit([ev]))

    def test_infer_clean_from_git_op_commit(self):
        """A run with gitOperation.commit is labeled 'clean' by behavior inference."""
        run = _make_run(close_reason="ended", event_count=5)
        events = [_read(), _edit(), _git_op_commit()]
        outcome, source = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "clean")

    def test_infer_clean_from_git_dash_c_commit(self):
        """A run with 'git -C <path> commit' is labeled 'clean'."""
        run = _make_run(close_reason="ended", event_count=5)
        ev = _ev(
            tool_name="Bash",
            tool_summary='{"command":"git -C /home/eric/projects/foo commit -m \'feat: foo\'"}',
            tool_response_summary='{"stdout":"[branch abc123] feat: foo","stderr":""}',
        )
        events = [_read(), _edit(), ev]
        outcome, source = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "clean")


# ── B4: gh -C invocation ─────────────────────────────────────────────────────

class TestB4GhCInvocation(unittest.TestCase):
    """gh pr view uses -C <dir>, not --repo ."""

    def test_gh_called_with_dash_c(self):
        """_gh_pr_state must call gh with -C <dir>, not --repo ."""
        from label import _gh_pr_state

        captured_cmds = []

        def mock_run_cmd(cmd, **kwargs):
            captured_cmds.append(cmd)
            return json.dumps({"state": "MERGED", "mergedAt": "2026-06-13T12:00:00Z"})

        with patch("label._run_cmd", side_effect=mock_run_cmd):
            result = _gh_pr_state("my-branch", "/home/eric/projects/foo")

        self.assertIsNotNone(result)
        self.assertEqual(len(captured_cmds), 1)
        cmd = captured_cmds[0]
        # Must use -C, not --repo
        self.assertIn("-C", cmd)
        self.assertNotIn("--repo", cmd)
        # Must use the worktree path
        self.assertIn("/home/eric/projects/foo", cmd)
        # Must include branch name
        self.assertIn("my-branch", cmd)

    def test_gh_degrades_to_none_on_failure(self):
        """If gh fails (no remote, network error), _gh_pr_state returns None — no mislabel."""
        from label import _gh_pr_state

        with patch("label._run_cmd", return_value=None):
            result = _gh_pr_state("my-branch", "/nonexistent/path")
        self.assertIsNone(result)

    def test_label_from_pr_degrades_safely(self):
        """label_from_pr returns None (not an exception) when gh fails."""
        result = label_from_pr("my-branch", "/nonexistent/path")
        # No exception; should be None or git_revert result
        # Since git also won't work with /nonexistent/path, should be None
        # (git_has_revert will also fail gracefully)
        # We just confirm no crash:
        self.assertIsNone(result)  # or (_, "git_revert") if git happens to run


# ── B5: null-not-clean ────────────────────────────────────────────────────────

class TestB5NullNotClean(unittest.TestCase):
    """Opaque/micro/no-signal runs must get outcome=None, not 'clean'."""

    def test_one_event_idle_timeout_is_null(self):
        """1-event idle_timeout run → null (not clean, not abandoned)."""
        run = _make_run(close_reason="idle_timeout", event_count=1)
        events = [_bash()]
        outcome, _ = _infer_from_behavior(run, events)
        self.assertIsNone(outcome)

    def test_micro_run_recorder_shutdown_is_null(self):
        """Micro run (3 events) with recorder_shutdown → null."""
        run = _make_run(close_reason="recorder_shutdown", event_count=3)
        events = [_bash(), _read(), _bash()]
        outcome, _ = _infer_from_behavior(run, events)
        self.assertIsNone(outcome)

    def test_empty_events_is_null(self):
        """Run with no events → null."""
        run = _make_run(close_reason="ended", event_count=0)
        outcome, _ = _infer_from_behavior(run, [])
        self.assertIsNone(outcome)

    def test_pure_read_run_is_null(self):
        """Run with only Read calls and no commit → null."""
        run = _make_run(close_reason="idle_timeout", event_count=4)
        events = [_read() for _ in range(4)]
        outcome, _ = _infer_from_behavior(run, events)
        # Fewer than ABANDON_MIN_EVENTS=5, so not abandoned; no commit → null
        self.assertIsNone(outcome)

    def test_ended_run_with_edit_tail_is_clean(self):
        """Positive control: run ending with Edit + close_reason=ended → clean."""
        run = _make_run(close_reason="ended", event_count=6)
        events = [_read(), _bash(), _read(), _bash(), _edit()]
        outcome, _ = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "clean")

    def test_backfill_null_outcome_not_written_to_db(self):
        """Integration: backfill leaves outcome=None in DB for a null-signal run."""
        db_path = _make_db([{
            "run_id": "run-null-01",
            "close_reason": "idle_timeout",
            "event_count": 1,
            "events": [_bash()],
        }])
        try:
            with patch("label.label_from_bead", MagicMock(return_value=None)):
                with patch("label.label_from_pr", MagicMock(return_value=None)):
                    results = backfill(db_path, dry_run=False, verbose=False)

            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertIsNone(r["outcome"])

            # Verify DB row still has outcome=NULL
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT outcome FROM runs WHERE run_id='run-null-01'"
            ).fetchone()
            conn.close()
            self.assertIsNone(row[0])
        finally:
            db_path.unlink(missing_ok=True)


# ── M1: bus.bead.closed triggers bd lookup ───────────────────────────────────

class TestM1BusBeadClosedTriggersBdLookup(unittest.TestCase):
    """bus.bead.closed must TRIGGER a bd show lookup, not assign outcome directly."""

    def test_bus_event_triggers_bd_lookup(self):
        """When a bus.bead.closed event matches, we call _bd_bead_state, not read bus fields.

        M1 fix: bus.bead.closed has no disposition field; the bus event only
        triggers a bd show lookup to get the authoritative state.
        """
        mock_redis = MagicMock()
        mock_redis.xrevrange.return_value = [
            ("stream-id-1", {
                "_raw": json.dumps({
                    "type": "bus.bead.closed",
                    "data": {
                        "bead_id": "nervous-bus-fhr1q",
                        # Note: NO disposition or outcome field here (per M1 fix)
                    }
                })
            })
        ]

        # redis is imported inside _redis_bead_closed_outcome; patch via sys.modules
        import sys
        mock_redis_module = MagicMock()
        mock_redis_module.Redis.return_value = mock_redis

        with patch.dict(sys.modules, {"redis": mock_redis_module}):
            with patch("label._bd_bead_state", return_value="CLOSED") as mock_state:
                with patch("label._bd_structured_resolution", return_value="done"):
                    result = _redis_bead_closed_outcome("nervous-bus-fhr1q")

        # bd_bead_state MUST have been called
        mock_state.assert_called_once_with("nervous-bus-fhr1q")
        # Result should be 'landed' (bd says CLOSED with resolution=done)
        self.assertEqual(result, "landed")

    def test_bus_event_wontfix_calls_bd(self):
        """bus.bead.closed for a wontfix bead still calls bd for authoritative state."""
        mock_redis = MagicMock()
        mock_redis.xrevrange.return_value = [
            ("stream-id-1", {
                "_raw": json.dumps({
                    "data": {"bead_id": "loom-abcde"}
                })
            })
        ]
        import sys
        mock_redis_module = MagicMock()
        mock_redis_module.Redis.return_value = mock_redis

        with patch.dict(sys.modules, {"redis": mock_redis_module}):
            with patch("label._bd_bead_state", return_value="CLOSED"):
                with patch("label._bd_structured_resolution", return_value="wontfix"):
                    result = _redis_bead_closed_outcome("loom-abcde")

        self.assertEqual(result, "abandoned")


# ── M2: structured bd close-reason ───────────────────────────────────────────

class TestM2StructuredBdCloseReason(unittest.TestCase):
    """bd close-reason must use structured resolution field, not free-text."""

    def _mock_bd_show(self, output: str):
        """Patch _bd_bead_state and _bd_structured_resolution with raw bd output."""
        return output

    def test_reverted_bad_approach_not_reverted(self):
        """Free-text 'reverted the bad approach, shipped the fix' should NOT map to 'reverted'."""
        from label import _bd_structured_resolution
        bd_output = (
            "[● P1 · CLOSED]\n"
            "Close reason: reverted the bad approach, shipped the fix\n"
            "Resolution: done\n"
        )
        with patch("label._run_cmd", return_value=bd_output):
            result = _bd_structured_resolution("nervous-bus-fhr1q")
        # Structured resolution='done' wins over close_reason free-text
        self.assertEqual(result, "done")

    def test_wontfix_in_resolution_field(self):
        """Structured Resolution: wontfix → 'wontfix'."""
        from label import _bd_structured_resolution
        bd_output = "[● P1 · CLOSED]\nResolution: wontfix\n"
        with patch("label._run_cmd", return_value=bd_output):
            result = _bd_structured_resolution("bead-xyz")
        self.assertEqual(result, "wontfix")

    def test_label_from_bead_reverted_uses_structured_field(self):
        """label_from_bead maps structured resolution='reverted' → 'reverted'."""
        with patch("label._redis_bead_closed_outcome", return_value=None):
            with patch("label._bd_bead_state", return_value="CLOSED"):
                with patch("label._bd_structured_resolution", return_value="reverted"):
                    result = label_from_bead("loom-abcde", None)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "reverted")
        self.assertEqual(result[1], "bead_close")

    def test_label_from_bead_done_is_landed(self):
        """Structured resolution='done' → 'landed'."""
        with patch("label._redis_bead_closed_outcome", return_value=None):
            with patch("label._bd_bead_state", return_value="CLOSED"):
                with patch("label._bd_structured_resolution", return_value="done"):
                    result = label_from_bead("loom-abcde", None)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "landed")


# ── M3: abandoned over-fire prevention ───────────────────────────────────────

class TestM3AbandonedOverFire(unittest.TestCase):
    """Abandoned requires stronger signal: failure/error or substantial run size."""

    def test_small_idle_run_without_failure_not_abandoned(self):
        """A 5-event idle_timeout run with no failures and no resolving action
        (no failure signal, not substantial) → NOT abandoned (should be null)."""
        run = _make_run(close_reason="idle_timeout", event_count=5)
        events = [_bash(fail=False) for _ in range(5)]  # all succeed, no failures
        outcome, _ = _infer_from_behavior(run, events)
        # Without bash failures or substantial size, M3 prevents abandon
        # Actually 5 == ABANDON_MIN_EVENTS*1, not ABANDON_MIN_EVENTS*2=10
        # So this is borderline — let's assert it's NOT abandoned for the small case
        self.assertNotEqual(outcome, "abandoned")

    def test_substantial_run_without_failure_is_abandoned(self):
        """A 15-event idle_timeout run (>= 2*ABANDON_MIN_EVENTS) with all-bash
        (no edit tail, no commit, no failures) → abandoned (M3 substantial path)."""
        run = _make_run(close_reason="idle_timeout", event_count=15)
        events = [_read() for _ in range(7)] + [_bash(fail=False) for _ in range(8)]
        outcome, _ = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "abandoned")

    def test_failed_bash_triggers_abandon_even_at_min_size(self):
        """A 5-event idle_timeout run WITH bash failures → abandoned (failure signal path)."""
        run = _make_run(close_reason="idle_timeout", event_count=5)
        events = [_bash(fail=True)] + [_bash(fail=False) for _ in range(4)]
        outcome, _ = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "abandoned")

    def test_tengine_cargo_test_run_is_abandoned(self):
        """The real tengine run: idle_timeout, 15 events, 9 bash failures visible
        in the test result output → should be abandoned."""
        run = _make_run(close_reason="idle_timeout", event_count=15)
        # 15 bash events, 1 has 'FAILED' in stdout (test failure)
        fail_resp = json.dumps({
            "exitCode": 1,
            "stderr": "error: test failed, to rerun pass ...",
            "interrupted": False,
        })
        events = (
            [_bash(fail=False) for _ in range(8)]
            + [_ev(tool_name="Bash", tool_is_error=False,
                   tool_summary='{"command":"cargo test"}',
                   tool_response_summary=fail_resp)]
            + [_bash(fail=False) for _ in range(6)]
        )
        outcome, _ = _infer_from_behavior(run, events)
        self.assertEqual(outcome, "abandoned")


# ── Precedence guard ──────────────────────────────────────────────────────────

class TestPrecedenceGuard(unittest.TestCase):
    """Explicit label must beat inferred; no wrong history entry appended."""

    def _make_mem_db(self, outcome=None, label_version=None, source="behavior_inference"):
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
        history = []
        if outcome is not None:
            history = [{"outcome": outcome, "labeled_at": "2026-06-13T00:00:00Z",
                        "label_version": label_version or 1, "source": source}]
        conn.execute(
            "INSERT INTO runs (run_id, outcome, label_version, label_history, features) "
            "VALUES (?, ?, ?, ?, '{}')",
            ("run-001", outcome, label_version, json.dumps(history)),
        )
        return conn

    def test_inferred_does_not_overwrite_explicit(self):
        """An inferred 'clean' must NOT overwrite an explicit 'landed'."""
        conn = self._make_mem_db(outcome="landed", label_version=1, source="pr_merge")
        changed = apply_label(conn, "run-001", "clean", "behavior_inference")
        self.assertFalse(changed, "Inferred label overwrote explicit — FAIL")

        row = conn.execute("SELECT outcome, label_version FROM runs").fetchone()
        self.assertEqual(row[0], "landed", "outcome must not have changed")
        self.assertEqual(row[1], 1, "version must not have been bumped")

    def test_inferred_does_not_overwrite_bead_close(self):
        """Inferred 'abandoned' must NOT overwrite explicit 'landed' from bead_close."""
        conn = self._make_mem_db(outcome="landed", label_version=1, source="bead_close")
        changed = apply_label(conn, "run-001", "abandoned", "behavior_inference")
        self.assertFalse(changed)
        row = conn.execute("SELECT outcome FROM runs").fetchone()
        self.assertEqual(row[0], "landed")

    def test_explicit_can_overwrite_inferred(self):
        """An explicit 'landed' (pr_merge) CAN overwrite an inferred 'clean'."""
        conn = self._make_mem_db(outcome="clean", label_version=1, source="behavior_inference")
        changed = apply_label(conn, "run-001", "landed", "pr_merge")
        self.assertTrue(changed)
        row = conn.execute("SELECT outcome, label_version FROM runs").fetchone()
        self.assertEqual(row[0], "landed")
        self.assertEqual(row[1], 2)

    def test_explicit_can_overwrite_lower_explicit(self):
        """A higher-tier explicit (bead_close tier=3) overwrites lower explicit (pr_closed_unmerged tier=2)."""
        conn = self._make_mem_db(outcome="abandoned", label_version=1, source="pr_closed_unmerged")
        changed = apply_label(conn, "run-001", "landed", "bead_close")
        self.assertTrue(changed)
        row = conn.execute("SELECT outcome FROM runs").fetchone()
        self.assertEqual(row[0], "landed")

    def test_no_wrong_history_entry_on_blocked_overwrite(self):
        """When an overwrite is blocked by precedence, no history entry is appended."""
        conn = self._make_mem_db(outcome="landed", label_version=1, source="pr_merge")
        apply_label(conn, "run-001", "clean", "behavior_inference")

        row = conn.execute("SELECT label_history FROM runs").fetchone()
        history = json.loads(row[0])
        # Only the original 'landed' entry — no spurious 'clean' entry
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["outcome"], "landed")
        self.assertEqual(history[0]["source"], "pr_merge")

    def test_infer_corrected_returns_none(self):
        """_infer_corrected always returns None (stub, not wired as outcome source)."""
        from label import _infer_corrected
        result = _infer_corrected([_edit(), _bash(), _read()])
        self.assertIsNone(result)


# ── Integration: full backfill over multi-run DB ──────────────────────────────

class TestBackfillHardened(unittest.TestCase):
    """End-to-end backfill with all fixes applied."""

    def test_backfill_smoke_hardened(self):
        """Backfill over a DB with:
        - recorder_shutdown run with commit → clean
        - recorder_shutdown run without commit → null (B1+B5)
        - idle_timeout substantial run without resolving action → abandoned (M3)
        - idle_timeout small run with git -C commit → clean (B3)
        """
        db_path = _make_db([
            {
                "run_id": "run-rs-commit",
                "close_reason": "recorder_shutdown",
                "event_count": 5,
                "events": [_read(), _edit(), _git_op_commit()],
            },
            {
                "run_id": "run-rs-no-commit",
                "close_reason": "recorder_shutdown",
                "event_count": 10,
                "events": [_read() for _ in range(5)] + [_bash() for _ in range(5)],
            },
            {
                "run_id": "run-idle-substantial",
                "close_reason": "idle_timeout",
                "event_count": 15,
                "events": [_read() for _ in range(7)] + [_bash() for _ in range(8)],
            },
            {
                "run_id": "run-idle-commit",
                "close_reason": "idle_timeout",
                "event_count": 5,
                "events": [_read(), _edit(), _bash(
                    cmd="git -C /home/eric/projects/foo commit -m 'x'",
                    resp_override=json.dumps({
                        "gitOperation": {"commit": {"kind": "committed", "sha": "abc"}},
                        "interrupted": False, "stderr": "", "stdout": "[branch abc] x",
                    }),
                )],
            },
        ])
        try:
            with patch("label.label_from_bead", MagicMock(return_value=None)):
                with patch("label.label_from_pr", MagicMock(return_value=None)):
                    results = backfill(db_path, dry_run=False, verbose=False)

            by_id = {r["run_id"]: r for r in results}

            # recorder_shutdown with commit → clean (B1: no abandon, B3: git_op_commit)
            self.assertEqual(by_id["run-rs-commit"]["outcome"], "clean")

            # recorder_shutdown without commit → null (B1: no abandon, B5: no clean)
            self.assertIsNone(by_id["run-rs-no-commit"]["outcome"])

            # idle_timeout substantial run → abandoned (M3: substantial path)
            self.assertEqual(by_id["run-idle-substantial"]["outcome"], "abandoned")

            # idle_timeout with git -C commit → clean (B3)
            self.assertEqual(by_id["run-idle-commit"]["outcome"], "clean")
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
