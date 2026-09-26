"""tests/test_label_orca_and_rollup.py

Orca `worker_done --outcome` as a tier-3 label source, wired into
compute_label and reverify_run via _prefer_higher_tier (never downgrades a
stronger git/pr result).

Session/run-key rollup: an 'abandoned' behavior_inference verdict is checked
against sibling runs sharing the SAME run_key (idle-timeout-split segments of
one agent/pane) before being made terminal; a landed/committed sibling turns
it into None instead. Deliberately NOT keyed on session_id/host_conversation_id:
those are shared by a parent host session and every subagent it dispatches, so
keying on either would treat unrelated subagents as siblings of each other.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from label import (
    _prefer_higher_tier,
    _session_has_landed_sibling,
    compute_label,
    reverify_run,
)


def _wrap(events):
    return [{"raw_json": json.dumps({"data": ev})} for ev in events]


def _make_run(**kwargs):
    run = {
        "run_id": "TESTRUN0001",
        "run_key": "key-1",
        "project": "test",
        "run_key_kind": "worktree",
        "close_reason": "idle_timeout",
        "git_branch": None,
        "bead_id": None,
        "worktree": None,
        "session_id": None,
        "host_conversation_id": None,
        "event_count": 10,
    }
    run.update(kwargs)
    return run


def _read():
    return {"event": "tool_call", "tool_name": "Read"}


def _bash():
    return {"event": "tool_call", "tool_name": "Bash",
            "tool_response_summary": json.dumps({"exitCode": 0})}


# ── _prefer_higher_tier ────────────────────────────────────────────────────────

class TestPreferHigherTier(unittest.TestCase):
    def test_none_a_returns_b(self):
        self.assertEqual(_prefer_higher_tier(None, ("clean", "orca_worker_done")),
                         ("clean", "orca_worker_done"))

    def test_none_b_returns_a(self):
        self.assertEqual(_prefer_higher_tier(("landed", "pr_merge"), None),
                         ("landed", "pr_merge"))

    def test_both_none_returns_none(self):
        self.assertIsNone(_prefer_higher_tier(None, None))

    def test_higher_tier_b_wins(self):
        # pr_closed_unmerged tier=2, orca_worker_done tier=3
        result = _prefer_higher_tier(("abandoned", "pr_closed_unmerged"),
                                      ("clean", "orca_worker_done"))
        self.assertEqual(result, ("clean", "orca_worker_done"))

    def test_tie_keeps_a(self):
        # git_merged_into_main tier=3, orca_worker_done tier=3 — keep git (a)
        result = _prefer_higher_tier(("landed", "git_merged_into_main"),
                                      ("clean", "orca_worker_done"))
        self.assertEqual(result, ("landed", "git_merged_into_main"))


# ── compute_label wiring (orca_worker_done) ─────────────────────────────────────

class TestComputeLabelOrcaWiring(unittest.TestCase):
    def test_orca_result_used_when_no_git_result(self):
        run = _make_run(session_id="01a00000-0000-7000-8000-000000000099")
        events = _wrap([_read() for _ in range(10)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done",
                   MagicMock(return_value=("clean", "orca_worker_done"))):
            result = compute_label(run, events, verbose=False)
        self.assertEqual(result, ("clean", "orca_worker_done"))

    def test_orca_result_upgrades_weaker_git_tier(self):
        """pr_closed_unmerged (tier 2) must NOT beat orca_worker_done (tier 3)."""
        run = _make_run(git_branch="warxhead1/some-lane",
                         session_id="01a00000-0000-7000-8000-000000000098")
        events = _wrap([_read() for _ in range(10)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr",
                   MagicMock(return_value=("abandoned", "pr_closed_unmerged"))), \
             patch("label.label_from_orca_worker_done",
                   MagicMock(return_value=("clean", "orca_worker_done"))):
            result = compute_label(run, events, verbose=False)
        self.assertEqual(result, ("clean", "orca_worker_done"))

    def test_orca_never_downgrades_stronger_git_landed(self):
        """A tier-3 'landed' from git/PR must survive even though
        orca_worker_done is also tier 3 (tie-break: keep git)."""
        run = _make_run(git_branch="warxhead1/some-lane",
                         session_id="01a00000-0000-7000-8000-000000000097")
        events = _wrap([_read() for _ in range(10)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr",
                   MagicMock(return_value=("landed", "pr_merge"))), \
             patch("label.label_from_orca_worker_done",
                   MagicMock(return_value=("clean", "orca_worker_done"))):
            result = compute_label(run, events, verbose=False)
        self.assertEqual(result, ("landed", "pr_merge"))

    def test_no_orca_match_falls_back_to_behavior_inference(self):
        run = _make_run(session_id=None, close_reason="ended", event_count=5)
        events = _wrap([_read(), _bash()])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done", MagicMock(return_value=None)):
            result = compute_label(run, events, verbose=False)
        # No positive resolving signal and no explicit tier -> None (B5), not
        # a regression from this fix.
        self.assertIsNone(result)


# ── reverify_run wiring (orca_worker_done) ──────────────────────────────────────

class TestReverifyRunOrcaWiring(unittest.TestCase):
    def test_reverify_uses_orca_when_no_git_signal(self):
        run = {"run_id": "r1", "git_branch": "codex/some-task",
               "worktree": "/nonexistent/path", "session_id": "01a0-x", "features": "{}"}
        with patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_git_merge", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done",
                   MagicMock(return_value=("abandoned", "orca_worker_done"))):
            result = reverify_run(run)
        self.assertEqual(result, ("abandoned", "orca_worker_done"))

    def test_reverify_prefers_git_over_orca_on_tie(self):
        run = {"run_id": "r2", "git_branch": "codex/some-task",
               "worktree": "/nonexistent/path", "session_id": "01a0-y", "features": "{}"}
        with patch("label.label_from_pr",
                   MagicMock(return_value=("landed", "pr_merge"))), \
             patch("label.label_from_orca_worker_done",
                   MagicMock(return_value=("clean", "orca_worker_done"))):
            result = reverify_run(run)
        self.assertEqual(result, ("landed", "pr_merge"))

    def test_reverify_with_no_branch_still_checks_orca(self):
        """A run with no git_branch at all (pure Orca worker, no dispatch
        branch recorded) must still be reverify-able via worker_done."""
        run = {"run_id": "r3", "git_branch": None, "session_id": "01a0-z", "features": "{}"}
        with patch("label.label_from_orca_worker_done",
                   MagicMock(return_value=("clean", "orca_worker_done"))):
            result = reverify_run(run)
        self.assertEqual(result, ("clean", "orca_worker_done"))

    def test_no_signal_anywhere_returns_none(self):
        run = {"run_id": "r4", "git_branch": None, "session_id": None, "features": "{}"}
        with patch("label.label_from_orca_worker_done", MagicMock(return_value=None)):
            result = reverify_run(run)
        self.assertIsNone(result)


# ── _session_has_landed_sibling (keyed on run_key) ──────────────────────────────

_DDL = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    run_key TEXT,
    session_id TEXT,
    host_conversation_id TEXT,
    outcome TEXT,
    label_history TEXT NOT NULL DEFAULT '[]',
    features TEXT NOT NULL DEFAULT '{}'
)
"""


def _make_conn(rows: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(_DDL)
    for r in rows:
        conn.execute(
            "INSERT INTO runs (run_id, run_key, session_id, host_conversation_id, "
            "outcome, label_history, features) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                r["run_id"], r.get("run_key"), r.get("session_id"),
                r.get("host_conversation_id"), r.get("outcome"),
                json.dumps(r.get("label_history", [])),
                json.dumps(r.get("features", {})),
            ),
        )
    return conn


class TestSessionHasLandedSibling(unittest.TestCase):
    def test_sibling_with_landed_outcome_any_source(self):
        conn = _make_conn([
            {"run_id": "self", "run_key": "key-1", "outcome": "abandoned"},
            {"run_id": "sib", "run_key": "key-1", "outcome": "landed",
             "label_history": [{"outcome": "landed", "source": "behavior_inference"}]},
        ])
        run = {"run_id": "self", "run_key": "key-1"}
        self.assertTrue(_session_has_landed_sibling(conn, run))

    def test_sibling_clean_via_explicit_source_counts(self):
        conn = _make_conn([
            {"run_id": "self", "run_key": "key-2", "outcome": "abandoned"},
            {"run_id": "sib", "run_key": "key-2", "outcome": "clean",
             "label_history": [{"outcome": "clean", "source": "pr_merge"}]},
        ])
        run = {"run_id": "self", "run_key": "key-2"}
        self.assertTrue(_session_has_landed_sibling(conn, run))

    def test_sibling_clean_via_behavior_inference_only_does_not_count(self):
        """A sibling 'clean' from pure behavior_inference is not stronger
        evidence than the run in question — do not roll up on it."""
        conn = _make_conn([
            {"run_id": "self", "run_key": "key-3", "outcome": "abandoned"},
            {"run_id": "sib", "run_key": "key-3", "outcome": "clean",
             "label_history": [{"outcome": "clean", "source": "behavior_inference"}]},
        ])
        run = {"run_id": "self", "run_key": "key-3"}
        self.assertFalse(_session_has_landed_sibling(conn, run))

    def test_sibling_has_resolving_commit_feature_counts(self):
        conn = _make_conn([
            {"run_id": "self", "run_key": "key-4", "outcome": "abandoned"},
            {"run_id": "sib", "run_key": "key-4", "outcome": None,
             "features": {"has_resolving_commit": True}},
        ])
        run = {"run_id": "self", "run_key": "key-4"}
        self.assertTrue(_session_has_landed_sibling(conn, run))

    def test_no_siblings_returns_false(self):
        conn = _make_conn([
            {"run_id": "self", "run_key": "key-5", "outcome": "abandoned"},
        ])
        run = {"run_id": "self", "run_key": "key-5"}
        self.assertFalse(_session_has_landed_sibling(conn, run))

    def test_no_run_key_returns_false(self):
        conn = _make_conn([{"run_id": "self", "outcome": "abandoned"}])
        run = {"run_id": "self", "run_key": None}
        self.assertFalse(_session_has_landed_sibling(conn, run))

    def test_shared_session_id_alone_is_not_a_sibling_match(self):
        """Regression: a parent host run that landed and a subagent (DIFFERENT
        run_key, same session_id/host_conversation_id) that idled must NOT
        roll the subagent up — session_id/host_conversation_id are shared by
        every subagent dispatched from one host session, not proof of the
        same logical run."""
        conn = _make_conn([
            {"run_id": "subagent", "run_key": "key-subagent",
             "session_id": "host-sess-1", "host_conversation_id": "host-conv-1",
             "outcome": "abandoned"},
            {"run_id": "parent", "run_key": "key-parent",
             "session_id": "host-sess-1", "host_conversation_id": "host-conv-1",
             "outcome": "landed"},
        ])
        run = {"run_id": "subagent", "run_key": "key-subagent",
               "session_id": "host-sess-1", "host_conversation_id": "host-conv-1"}
        self.assertFalse(
            _session_has_landed_sibling(conn, run),
            "a subagent must not inherit its parent host session's landed "
            "outcome merely by sharing session_id/host_conversation_id",
        )


# ── compute_label integration (run_key rollup) ──────────────────────────────────

class TestComputeLabelSessionRollup(unittest.TestCase):
    def test_abandoned_downgraded_to_none_when_sibling_landed(self):
        conn = _make_conn([
            {"run_id": "TESTRUN0001", "run_key": "key-x", "outcome": None},
            {"run_id": "other", "run_key": "key-x", "outcome": "landed"},
        ])
        run = _make_run(run_key="key-x", close_reason="idle_timeout", event_count=10)
        events = _wrap([_read() for _ in range(5)] + [_bash() for _ in range(5)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done", MagicMock(return_value=None)):
            result = compute_label(run, events, verbose=False, conn=conn)
        self.assertIsNone(result,
            "abandoned segment must roll up to None when a run_key sibling landed")

    def test_abandoned_stays_abandoned_without_conn(self):
        """Backward compatible: omitting conn (e.g. a caller with no DB
        handle) must not change existing abandoned behavior."""
        run = _make_run(run_key="key-y", close_reason="idle_timeout", event_count=10)
        events = _wrap([_read() for _ in range(5)] + [_bash() for _ in range(5)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done", MagicMock(return_value=None)):
            result = compute_label(run, events, verbose=False)
        self.assertEqual(result, ("abandoned", "behavior_inference"))

    def test_abandoned_stays_abandoned_when_no_sibling_landed(self):
        conn = _make_conn([
            {"run_id": "TESTRUN0001", "run_key": "key-z", "outcome": None},
        ])
        run = _make_run(run_key="key-z", close_reason="idle_timeout", event_count=10)
        events = _wrap([_read() for _ in range(5)] + [_bash() for _ in range(5)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done", MagicMock(return_value=None)):
            result = compute_label(run, events, verbose=False, conn=conn)
        self.assertEqual(result, ("abandoned", "behavior_inference"))

    def test_subagent_abandoned_stays_abandoned_when_only_parent_session_landed(self):
        """Integration-level regression for the same fix as
        TestSessionHasLandedSibling.test_shared_session_id_alone_is_not_a_sibling_match:
        a parent host run landed under a DIFFERENT run_key; a subagent run
        sharing session_id/host_conversation_id (but its own run_key) with no
        resolving action of its own must stay 'abandoned'."""
        conn = _make_conn([
            {"run_id": "TESTRUN0001", "run_key": "key-subagent",
             "session_id": "host-sess-2", "host_conversation_id": "host-conv-2",
             "outcome": None},
            {"run_id": "parent-run", "run_key": "key-parent",
             "session_id": "host-sess-2", "host_conversation_id": "host-conv-2",
             "outcome": "landed"},
        ])
        run = _make_run(run_key="key-subagent", session_id="host-sess-2",
                         host_conversation_id="host-conv-2",
                         close_reason="idle_timeout", event_count=10)
        events = _wrap([_read() for _ in range(5)] + [_bash() for _ in range(5)])
        with patch("label.label_from_bead", MagicMock(return_value=None)), \
             patch("label.label_from_pr", MagicMock(return_value=None)), \
             patch("label.label_from_orca_worker_done", MagicMock(return_value=None)):
            result = compute_label(run, events, verbose=False, conn=conn)
        self.assertEqual(
            result, ("abandoned", "behavior_inference"),
            "a subagent must not inherit its parent host session's landed "
            "outcome merely by sharing session_id/host_conversation_id",
        )


if __name__ == "__main__":
    unittest.main()
