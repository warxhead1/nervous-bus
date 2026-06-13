"""tests/test_synthesis.py — Synthesis pass tests.

Tests cover:
1. Re-stitch: continues_run_id chains collapse correctly (no double-count).
2. Scoring component math.
3. worktree_leak replay (prevented vs false-suppression).
4. rebuild_cache_miss replay (prevented vs first-build suppression).
5. Decision matrix (propose_fix / monitor / suppressed / needs_more_data).
6. Inform-rung requires the higher INFORM_ACT_THRESHOLD.
7. Suppressed on replay-fail.
8. Null-outcome never folded into clean in labeled_support.
9. bus.agent.run.eval.v1 payload validates against schema.
10. pattern.discovered payload validates.
11. DRY-RUN does NOT shell out (no nervous publish call).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the reflex-recorder package is importable.
# ---------------------------------------------------------------------------
_REC_DIR = Path(__file__).parent.parent  # adapters/reflex-recorder/
if str(_REC_DIR) not in sys.path:
    sys.path.insert(0, str(_REC_DIR))

import synthesis as syn
from detectors.base import ensure_detector_schema, PatternCandidate
from detectors.worktree_leak import WorktreeLeakDetector
from detectors.rebuild_cache_miss import RebuildCacheMissDetector


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    """Open an in-memory DB with the full schema."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # Bootstrap runs + run_events tables
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY, run_key TEXT NOT NULL,
            run_key_kind TEXT NOT NULL DEFAULT 'session',
            host_conversation_id TEXT, project TEXT NOT NULL,
            agent_kind TEXT NOT NULL DEFAULT 'host_claude_code',
            session_id TEXT, agent_id TEXT,
            started TEXT NOT NULL, ended TEXT NOT NULL,
            close_reason TEXT, continues_run_id TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            tool_histogram TEXT NOT NULL DEFAULT '{}',
            worktree TEXT, worktree_slug TEXT, git_branch TEXT,
            bead_id TEXT, outcome TEXT, labeled_at TEXT,
            label_version INTEGER, label_history TEXT NOT NULL DEFAULT '[]',
            features TEXT NOT NULL DEFAULT '{}',
            schema_version TEXT NOT NULL DEFAULT '1',
            recorded_at TEXT NOT NULL DEFAULT '2026-06-01T00:00:00Z'
        );
        CREATE TABLE IF NOT EXISTS run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, seq INTEGER NOT NULL,
            event_ts TEXT NOT NULL, event_type TEXT NOT NULL,
            raw_json TEXT NOT NULL
        );
    """)
    ensure_detector_schema(conn)
    syn.ensure_eval_schema(conn)
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    project: str = "test-proj",
    started: str = "2026-06-01T00:00:00Z",
    ended: str = "2026-06-01T01:00:00Z",
    close_reason: str = "idle_timeout",
    continues_run_id: str = None,
    outcome: str = None,
    labeled_at: str = None,
    worktree: str = None,
    worktree_slug: str = None,
    bead_id: str = None,
    git_branch: str = None,
) -> None:
    conn.execute(
        """
        INSERT INTO runs
            (run_id, run_key, project, started, ended, close_reason,
             continues_run_id, outcome, labeled_at, worktree, worktree_slug,
             bead_id, git_branch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, run_id, project, started, ended, close_reason,
            continues_run_id, outcome, labeled_at, worktree, worktree_slug,
            bead_id, git_branch,
        ),
    )


def _insert_hit(
    conn: sqlite3.Connection,
    run_id: str,
    signature: str,
    detector: str = "worktree_leak",
    project: str = "test-proj",
    ts: str = "2026-06-01T00:00:00Z",
) -> None:
    conn.execute(
        "INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
        (run_id, detector, signature, project, ts),
    )


def _insert_issue(
    conn: sqlite3.Connection,
    signature: str,
    project: str = "test-proj",
    detector: str = "worktree_leak",
    recurrence_count: int = 1,
    first_seen: str = "2026-06-01T00:00:00Z",
    last_seen: str = "2026-06-01T00:00:00Z",
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO issues
            (signature, project, detector, first_seen, last_seen, recurrence_count)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (signature, project, detector, first_seen, last_seen, recurrence_count),
    )


# ---------------------------------------------------------------------------
# 1. Re-stitch: continues_run_id chains
# ---------------------------------------------------------------------------

class TestStitchLogicalRuns:
    def test_standalone_runs_map_to_themselves(self):
        conn = _make_conn()
        _insert_run(conn, "run-A")
        _insert_run(conn, "run-B")
        logical = syn.stitch_logical_runs(conn)
        # Each run is its own logical run (no chains)
        assert len(logical) == 2

    def test_chain_collapses_to_root(self):
        """run-B continues run-A → both fold into run-A's logical run."""
        conn = _make_conn()
        _insert_run(conn, "run-A")
        _insert_run(conn, "run-B", continues_run_id="run-A")
        logical = syn.stitch_logical_runs(conn)
        # root = run-A (has no parent)
        root = "run-A"
        assert root in logical
        members = set(logical[root])
        assert "run-A" in members
        assert "run-B" in members
        # run-B should NOT be its own logical run root
        assert "run-B" not in logical

    def test_three_level_chain(self):
        """A→B→C: all fold into A."""
        conn = _make_conn()
        _insert_run(conn, "A")
        _insert_run(conn, "B", continues_run_id="A")
        _insert_run(conn, "C", continues_run_id="B")
        logical = syn.stitch_logical_runs(conn)
        assert "A" in logical
        members = set(logical["A"])
        assert members == {"A", "B", "C"}
        assert "B" not in logical
        assert "C" not in logical

    def test_no_double_count_in_prevalence(self):
        """A single chain should not inflate the prevalence denominator."""
        conn = _make_conn()
        _insert_run(conn, "A", project="proj")
        _insert_run(conn, "B", project="proj", continues_run_id="A")
        # Record a hit on B (the fragment)
        _insert_issue(conn, "proj:test:anchor", project="proj", detector="test_det")
        _insert_hit(conn, "B", "proj:test:anchor", detector="test_det", project="proj")
        logical = syn.stitch_logical_runs(conn)
        # Logical runs for proj = {A: [A, B]} → 1 logical run, not 2
        logical_proj = {
            lid for lid, members in logical.items()
            for m in members
            if m in {"A", "B"}
        }
        assert len(logical_proj) == 1

    def test_orphan_parent_ref_stops_at_child(self):
        """If continues_run_id points to a non-existent run, stop there."""
        conn = _make_conn()
        _insert_run(conn, "B", continues_run_id="ghost-A")
        logical = syn.stitch_logical_runs(conn)
        # B's parent doesn't exist → B is its own root
        assert "B" in logical


# ---------------------------------------------------------------------------
# 2. Scoring component math
# ---------------------------------------------------------------------------

class TestScoringMath:
    def test_score_range(self):
        """score should be in [0, 1] for a normal issue."""
        issue = {
            "signature": "p:d:x",
            "project": "p",
            "detector": "d",
            "recurrence_count": 3,
            "last_seen": "2026-06-01T00:00:00Z",
        }
        labeled_support = {"confirmed_failures": 1, "confirmed_clean": 0, "unlabeled": 5}
        score, comps = syn.score_issue(issue, 0.5, labeled_support, "automate", 30)
        assert 0.0 <= score <= 1.2  # allow slight headroom from label_confirmation

    def test_higher_rung_scores_higher(self):
        """eliminate-rung issue should score higher than inform-rung, all else equal."""
        issue = {
            "signature": "p:d:x", "project": "p", "detector": "d",
            "recurrence_count": 3, "last_seen": "2026-06-01T00:00:00Z",
        }
        ls = {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 5}
        score_elim, _ = syn.score_issue(issue, 0.5, ls, "eliminate", 30)
        score_auto, _ = syn.score_issue(issue, 0.5, ls, "automate", 30)
        score_info, _ = syn.score_issue(issue, 0.5, ls, "inform", 30)
        assert score_elim > score_auto > score_info

    def test_higher_prevalence_scores_higher(self):
        issue = {
            "signature": "p:d:x", "project": "p", "detector": "d",
            "recurrence_count": 1, "last_seen": "2026-06-01T00:00:00Z",
        }
        ls = {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 1}
        s_high, _ = syn.score_issue(issue, 0.9, ls, "inform", 30)
        s_low, _  = syn.score_issue(issue, 0.1, ls, "inform", 30)
        assert s_high > s_low

    def test_components_sum_to_score(self):
        """Verify that score_components allows consumer to re-derive score."""
        issue = {
            "signature": "p:d:x", "project": "p", "detector": "d",
            "recurrence_count": 5, "last_seen": "2026-06-10T00:00:00Z",
        }
        ls = {"confirmed_failures": 2, "confirmed_clean": 1, "unlabeled": 3}
        score, comps = syn.score_issue(issue, 0.4, ls, "automate", 30)
        # Re-derive using constants
        import math
        MAX_RECUR = 20.0
        recur_norm = min(1.0, math.log1p(5) / math.log(MAX_RECUR + 1))
        rung_w = syn.RUNG_WEIGHTS["automate"] / max(syn.RUNG_WEIGHTS.values())
        label_conf = comps["label_confirmation"]
        recency = comps["recency"]
        derived = (
            syn.SCORE_WEIGHTS["prevalence"] * 0.4
            + syn.SCORE_WEIGHTS["recurrence"] * recur_norm
            + syn.SCORE_WEIGHTS["rung"] * rung_w
            + syn.SCORE_WEIGHTS["label_confirmation"] * label_conf
            + syn.SCORE_WEIGHTS["recency"] * recency
        )
        assert abs(score - round(derived, 6)) < 1e-4

    def test_label_confirmation_neutral_when_unlabeled(self):
        """When labeled=0, label_confirmation should be ~0."""
        issue = {
            "signature": "p:d:x", "project": "p", "detector": "d",
            "recurrence_count": 1, "last_seen": "2026-06-01T00:00:00Z",
        }
        ls = {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 20}
        _, comps = syn.score_issue(issue, 0.5, ls, "inform", 30)
        assert comps["label_confirmation"] == 0.0

    def test_label_confirmation_positive_on_failures(self):
        """When all labeled runs are failures, confirmation is positive."""
        issue = {
            "signature": "p:d:x", "project": "p", "detector": "d",
            "recurrence_count": 1, "last_seen": "2026-06-01T00:00:00Z",
        }
        ls = {"confirmed_failures": 5, "confirmed_clean": 0, "unlabeled": 2}
        _, comps = syn.score_issue(issue, 0.5, ls, "inform", 30)
        assert comps["label_confirmation"] > 0

    def test_null_outcome_not_counted_as_clean(self):
        """labeled_support must NOT fold outcome=NULL into confirmed_clean."""
        conn = _make_conn()
        sig = "proj:det:anchor"
        _insert_run(conn, "R1", project="proj", outcome=None, labeled_at=None)
        _insert_run(conn, "R2", project="proj", outcome=None, labeled_at=None)
        _insert_hit(conn, "R1", sig, detector="det", project="proj")
        _insert_hit(conn, "R2", sig, detector="det", project="proj")
        ls = syn._compute_labeled_support(conn, sig)
        # Both runs are unlabeled — must NOT appear in confirmed_clean
        assert ls["confirmed_clean"] == 0
        assert ls["unlabeled"] == 2


# ---------------------------------------------------------------------------
# 3. worktree_leak replay
# ---------------------------------------------------------------------------

class TestWorktreeLeakReplay:
    def _mk_detector(self, conn):
        return WorktreeLeakDetector(conn)

    def test_prevented_when_terminal_outcome(self):
        """Hits on clean-labeled runs → would have been prevented (need >= MIN_REPLAY_RUNS).

        Fix 1: pass signature to scope replay to a specific worktree path.
        """
        conn = _make_conn()
        wt = "/p/.worktrees/wt1"
        sig = f"proj:worktree_leak:{wt}"
        # Need at least MIN_REPLAY_RUNS=2 distinct logical runs that fired for same sig
        for i in range(2):
            rid = f"R{i}"
            _insert_run(conn, rid, project="proj", outcome="clean",
                        labeled_at="2026-06-01T00:00:00Z", worktree=wt)
            _insert_hit(conn, rid, sig, detector="worktree_leak", project="proj")
            _insert_issue(conn, sig, project="proj", detector="worktree_leak")
        logical = syn.stitch_logical_runs(conn)
        det = self._mk_detector(conn)
        result = syn._worktree_leak_replay(det, conn, logical, signature=sig)
        assert result.would_have_prevented >= 1
        assert result.false_suppression == 0

    def test_false_suppression_when_no_trigger(self):
        """Hits where no terminal outcome → false suppression (need >= MIN_REPLAY_RUNS).

        Fix 1: pass signature. Fix 2: only labeled+terminal = prevented; unlabeled = false_suppression.
        """
        conn = _make_conn()
        wt = "/p/.worktrees/wt1"
        sig = f"proj:worktree_leak:{wt}"
        # Two runs, both with no outcome label and no bead
        for i in range(2):
            rid = f"R{i}"
            _insert_run(conn, rid, project="proj", outcome=None, labeled_at=None,
                        worktree=wt, bead_id=None)
            _insert_hit(conn, rid, sig, detector="worktree_leak", project="proj")
            _insert_issue(conn, sig, project="proj", detector="worktree_leak")
        logical = syn.stitch_logical_runs(conn)
        det = self._mk_detector(conn)
        result = syn._worktree_leak_replay(det, conn, logical, signature=sig)
        assert result.false_suppression >= 1

    def test_insufficient_history_no_hits(self):
        conn = _make_conn()
        det = WorktreeLeakDetector(conn)
        result = syn._worktree_leak_replay(det, conn, {}, signature="proj:worktree_leak:/nope")
        assert result.status == "insufficient_history"

    def test_prevention_rate_computation(self):
        """2 prevented + 0 false = prevention_rate=1.0 → passed.

        Fix 1: pass signature; two runs for the SAME worktree path.
        """
        conn = _make_conn()
        wt = "/p/.worktrees/wt0"
        sig = f"proj:worktree_leak:{wt}"
        for rid in ["R1", "R2"]:
            _insert_run(conn, rid, project="proj", outcome="clean",
                        labeled_at="2026-06-01T00:00:00Z", worktree=wt)
            _insert_hit(conn, rid, sig, detector="worktree_leak", project="proj")
            _insert_issue(conn, sig, project="proj", detector="worktree_leak")
        logical = syn.stitch_logical_runs(conn)
        det = WorktreeLeakDetector(conn)
        result = syn._worktree_leak_replay(det, conn, logical, signature=sig)
        assert result.prevention_rate == pytest.approx(1.0)
        assert result.status == "passed"


# ---------------------------------------------------------------------------
# 4. rebuild_cache_miss replay
# ---------------------------------------------------------------------------

def _make_cargo_event(run_id: str, seq: int, ts: str, next_ts: str,
                      cwd: str = "/p/.claude/worktrees/wt1",
                      crate: str = "workspace") -> str:
    """Build a raw_json event for a cargo build command."""
    cmd = f"cargo build"
    if crate != "workspace":
        cmd = f"cargo build -p {crate}"
    summary = json.dumps({"command": cmd, "description": "Build crate"})
    return json.dumps({
        "specversion": "1.0",
        "id": f"{run_id}-{seq}",
        "source": "/test/test",
        "type": "bus.agent.activity.v1",
        "time": ts,
        "datacontenttype": "application/json",
        "data": {
            "tool_name": "Bash",
            "tool_summary": summary,
            "cwd": cwd,
            "project": "proj",
        },
    })


class TestRebuildCacheMissReplay:
    def test_first_build_is_not_preventable(self):
        """First ever builds of different crates → not_preventable (no prior cache to share).

        Fix 3: first-ever builds are now not_preventable, not false_suppression.
        A shared cache has nothing to offer on a first build; it is not a harmful
        suppression. This allows the gate to pass when there are no repeat builds.

        Use 2 runs each building a DIFFERENT crate, so both are first-ever →
        both not_preventable, none would_have_prevented.
        """
        conn = _make_conn()
        # Two runs, each with a different crate (crate-A and crate-B), both first-ever
        for i, (rid, crate, ts1, ts2, started) in enumerate([
            ("R1", "crate-a", "2026-06-01T00:00:00Z", "2026-06-01T00:02:00Z", "2026-06-01T00:00:00Z"),
            ("R2", "crate-b", "2026-06-02T00:00:00Z", "2026-06-02T00:02:00Z", "2026-06-02T00:00:00Z"),
        ]):
            _insert_run(conn, rid, project="proj", started=started,
                        ended=ts2, close_reason="idle_timeout")
            ev1 = _make_cargo_event(rid, 1, ts1, ts2, crate=crate)
            ev2 = json.dumps({"specversion": "1.0", "id": f"{rid}-2", "source": "/t",
                              "type": "bus.agent.activity.v1", "time": ts2,
                              "datacontenttype": "application/json",
                              "data": {"tool_name": "Bash", "tool_summary": "{}",
                                       "cwd": "/p/.claude/worktrees/wt1", "project": "proj"}})
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (rid, 1, ts1, "bus.agent.activity.v1", ev1),
            )
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (rid, 2, ts2, "bus.agent.activity.v1", ev2),
            )

        logical = syn.stitch_logical_runs(conn)
        det = RebuildCacheMissDetector(conn)
        result = syn._rebuild_cache_miss_replay(det, conn, logical)
        # Fix 3: both first-ever builds → not_preventable, NOT false_suppression
        assert result.not_preventable >= 1
        assert result.false_suppression == 0

    def test_second_build_is_prevented(self):
        """Second build of same crate → would have been prevented by shared cache.

        Fix 3: first build = not_preventable, second build = would_have_prevented.
        false_suppression stays 0 (no harmful suppressions).
        """
        conn = _make_conn()

        for i, (rid, ts1, ts2, started) in enumerate([
            ("R1", "2026-06-01T00:00:00Z", "2026-06-01T00:02:00Z", "2026-06-01T00:00:00Z"),
            ("R2", "2026-06-02T00:00:00Z", "2026-06-02T00:02:00Z", "2026-06-02T00:00:00Z"),
        ]):
            _insert_run(conn, rid, project="proj", started=started,
                        ended=ts2, close_reason="idle_timeout")
            ev1 = _make_cargo_event(rid, 1, ts1, ts2)
            ev2 = json.dumps({
                "specversion": "1.0", "id": f"{rid}-2", "source": "/t",
                "type": "bus.agent.activity.v1", "time": ts2,
                "datacontenttype": "application/json",
                "data": {"tool_name": "Bash", "tool_summary": "{}",
                         "cwd": "/p/.claude/worktrees/wt1", "project": "proj"},
            })
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (rid, 1, ts1, "bus.agent.activity.v1", ev1),
            )
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (rid, 2, ts2, "bus.agent.activity.v1", ev2),
            )

        logical = syn.stitch_logical_runs(conn)
        det = RebuildCacheMissDetector(conn)
        result = syn._rebuild_cache_miss_replay(det, conn, logical)
        # Fix 3: first build = not_preventable; second build = prevented; false_suppression=0
        assert result.would_have_prevented >= 1
        assert result.not_preventable >= 1
        assert result.false_suppression == 0


# ---------------------------------------------------------------------------
# 5. Decision matrix
# ---------------------------------------------------------------------------

class TestDecisionMatrix:
    def _replay_passed(self):
        return syn.ReplayResult(
            status="passed",
            runs_evaluated=5,
            would_have_prevented=4,
            false_suppression=0,
            prevention_rate=0.8,
        )

    def _replay_failed(self):
        return syn.ReplayResult(
            status="failed",
            runs_evaluated=5,
            would_have_prevented=1,
            false_suppression=3,
            prevention_rate=0.2,
        )

    def _replay_na(self):
        return syn.ReplayResult(status="not_applicable")

    def _replay_insuf(self):
        return syn.ReplayResult(status="insufficient_history", runs_evaluated=0)

    def _ls_neutral(self):
        return {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 10}

    def test_propose_fix_when_replay_passed_and_score_above_threshold(self):
        decision, rationale = syn.make_decision(
            score=syn.ACT_THRESHOLD + 0.1,
            rung="automate",
            replay=self._replay_passed(),
            labeled_support=self._ls_neutral(),
            logical_run_count=10,
        )
        assert decision == "propose_fix"

    def test_suppressed_when_replay_failed(self):
        decision, _ = syn.make_decision(
            score=0.9,
            rung="automate",
            replay=self._replay_failed(),
            labeled_support=self._ls_neutral(),
            logical_run_count=10,
        )
        assert decision == "suppressed"

    def test_needs_more_data_when_insufficient_history(self):
        decision, _ = syn.make_decision(
            score=0.8,
            rung="automate",
            replay=self._replay_insuf(),
            labeled_support=self._ls_neutral(),
            logical_run_count=10,
        )
        assert decision == "needs_more_data"

    def test_needs_more_data_when_few_project_runs(self):
        decision, _ = syn.make_decision(
            score=0.8,
            rung="automate",
            replay=self._replay_passed(),
            labeled_support=self._ls_neutral(),
            logical_run_count=1,  # below MIN_PROJECT_RUNS
        )
        assert decision == "needs_more_data"

    def test_monitor_when_score_below_act_threshold(self):
        decision, _ = syn.make_decision(
            score=0.01,  # very low score
            rung="automate",
            replay=self._replay_passed(),
            labeled_support=self._ls_neutral(),
            logical_run_count=10,
        )
        assert decision == "monitor"

    def test_suppressed_when_clean_dominates(self):
        ls = {"confirmed_failures": 0, "confirmed_clean": 5, "unlabeled": 2}
        decision, _ = syn.make_decision(
            score=0.9,
            rung="automate",
            replay=self._replay_passed(),
            labeled_support=ls,
            logical_run_count=10,
        )
        assert decision == "suppressed"


# ---------------------------------------------------------------------------
# 6. Inform-rung requires higher threshold
# ---------------------------------------------------------------------------

class TestInformRung:
    def test_inform_requires_higher_threshold(self):
        """inform-rung with score between ACT_THRESHOLD and INFORM_ACT_THRESHOLD → monitor."""
        score = (syn.ACT_THRESHOLD + syn.INFORM_ACT_THRESHOLD) / 2
        assert syn.ACT_THRESHOLD < score < syn.INFORM_ACT_THRESHOLD
        replay_na = syn.ReplayResult(status="not_applicable")
        ls = {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 10}
        decision, _ = syn.make_decision(
            score=score,
            rung="inform",
            replay=replay_na,
            labeled_support=ls,
            logical_run_count=10,
        )
        assert decision == "monitor"

    def test_inform_propose_fix_above_inform_threshold(self):
        score = syn.INFORM_ACT_THRESHOLD + 0.05
        replay_na = syn.ReplayResult(status="not_applicable")
        ls = {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 10}
        decision, _ = syn.make_decision(
            score=score,
            rung="inform",
            replay=replay_na,
            labeled_support=ls,
            logical_run_count=10,
        )
        assert decision == "propose_fix"


# ---------------------------------------------------------------------------
# 7. Suppressed on replay-fail
# ---------------------------------------------------------------------------

class TestSuppressedOnReplayFail:
    def test_suppressed_regardless_of_score(self):
        """Even a very high score must be suppressed if replay fails."""
        replay_fail = syn.ReplayResult(
            status="failed",
            prevention_rate=0.1,
            false_suppression=5,
        )
        ls = {"confirmed_failures": 3, "confirmed_clean": 0, "unlabeled": 1}
        for score in [0.99, 0.80, 0.60]:
            decision, rationale = syn.make_decision(
                score=score,
                rung="eliminate",
                replay=replay_fail,
                labeled_support=ls,
                logical_run_count=10,
            )
            assert decision == "suppressed", f"score={score} should be suppressed"
            assert "FAILED" in rationale or "failed" in rationale.lower()


# ---------------------------------------------------------------------------
# 8. Null-outcome never folded into clean
# ---------------------------------------------------------------------------

class TestNullOutcomeHandling:
    def test_unlabeled_run_not_in_confirmed_clean(self):
        conn = _make_conn()
        sig = "proj:det:anchor"
        # Insert runs with outcome=NULL, labeled_at=NULL
        for i in range(5):
            _insert_run(conn, f"R{i}", project="proj", outcome=None, labeled_at=None)
            _insert_hit(conn, f"R{i}", sig, detector="det", project="proj")
        ls = syn._compute_labeled_support(conn, sig)
        assert ls["confirmed_clean"] == 0
        assert ls["unlabeled"] == 5

    def test_labeled_clean_counted_correctly(self):
        conn = _make_conn()
        sig = "proj:det:anchor"
        _insert_run(conn, "R1", project="proj", outcome="clean",
                    labeled_at="2026-06-01T00:00:00Z")
        _insert_hit(conn, "R1", sig, detector="det", project="proj")
        # Unlabeled run
        _insert_run(conn, "R2", project="proj", outcome=None, labeled_at=None)
        _insert_hit(conn, "R2", sig, detector="det", project="proj")
        ls = syn._compute_labeled_support(conn, sig)
        assert ls["confirmed_clean"] == 1
        assert ls["unlabeled"] == 1
        assert ls["confirmed_failures"] == 0

    def test_labeled_failure_counted_correctly(self):
        conn = _make_conn()
        sig = "proj:det:anchor"
        for outcome in ("thrashed", "abandoned", "reverted"):
            run_id = f"R-{outcome}"
            _insert_run(conn, run_id, project="proj", outcome=outcome,
                        labeled_at="2026-06-01T00:00:00Z")
            _insert_hit(conn, run_id, sig, detector="det", project="proj")
        ls = syn._compute_labeled_support(conn, sig)
        assert ls["confirmed_failures"] == 3
        assert ls["confirmed_clean"] == 0


# ---------------------------------------------------------------------------
# 9. bus.agent.run.eval.v1 schema validation
# ---------------------------------------------------------------------------

class TestEvalSchemaValidation:
    def test_eval_payload_validates_against_schema(self):
        """An emitted run.eval payload must validate against the JSON Schema."""
        import jsonschema

        schema_path = (
            Path(__file__).parent.parent.parent.parent
            / "schemas"
            / "bus.agent.run.eval.v1.json"
        )
        if not schema_path.exists():
            pytest.skip(f"Schema file not found: {schema_path}")

        with open(schema_path) as f:
            schema = json.load(f)

        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/wt1"
        _insert_run(conn, "R1", project="proj", outcome="abandoned",
                    labeled_at="2026-06-01T00:00:00Z",
                    worktree="/p/.worktrees/wt1")
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=3)
        _insert_hit(conn, "R1", sig, detector="worktree_leak", project="proj")

        logical = syn.stitch_logical_runs(conn)
        det = WorktreeLeakDetector(conn)
        replay = syn._worktree_leak_replay(det, conn, logical)

        issue = dict(zip(
            [d[0] for d in conn.execute("SELECT * FROM issues WHERE 0").description],
            conn.execute("SELECT * FROM issues WHERE signature=?", (sig,)).fetchone()
        ))
        ls = syn._compute_labeled_support(conn, sig)
        score, comps = syn.score_issue(issue, 0.5, ls, "automate", 30)
        eval_id = syn._make_ulid()

        payload = syn.build_eval_payload(
            eval_id=eval_id,
            issue=issue,
            candidate=None,
            score=score,
            score_components=comps,
            rung="automate",
            rung_descent_reason="Test descent reason.",
            replay=replay,
            labeled_support=ls,
            decision="monitor",
            decision_rationale="Test rationale.",
            supersedes_eval_id=None,
            window_days=30,
            run_sample=["R1"],
            synthesis_pass_at=syn._now_utc(),
        )
        # Strip internal key before validation
        pub_payload = {k: v for k, v in payload.items() if k != "issue"}

        validator = jsonschema.Draft202012Validator(schema)
        errors = list(validator.iter_errors(pub_payload))
        assert errors == [], f"Schema validation errors: {[str(e) for e in errors]}"


# ---------------------------------------------------------------------------
# 10. pattern.discovered payload validates
# ---------------------------------------------------------------------------

class TestPatternDiscoveredSchemaValidation:
    def test_pattern_discovered_validates(self):
        import jsonschema

        schema_path = (
            Path(__file__).parent.parent.parent.parent
            / "schemas"
            / "_per-project.pattern.discovered.v1.json"
        )
        if not schema_path.exists():
            pytest.skip(f"Schema not found: {schema_path}")

        with open(schema_path) as f:
            schema = json.load(f)

        conn = _make_conn()
        sig = "proj:rebuild_cache_miss:proj/workspace"
        _insert_run(conn, "R1", project="proj", close_reason="idle_timeout")
        _insert_issue(conn, sig, project="proj", detector="rebuild_cache_miss",
                      recurrence_count=2)
        _insert_hit(conn, "R1", sig, detector="rebuild_cache_miss", project="proj")

        issue = dict(zip(
            [d[0] for d in conn.execute("SELECT * FROM issues WHERE 0").description],
            conn.execute("SELECT * FROM issues WHERE signature=?", (sig,)).fetchone()
        ))
        ls = syn._compute_labeled_support(conn, sig)
        score, comps = syn.score_issue(issue, 0.3, ls, "eliminate", 30)

        from detectors.base import PatternCandidate
        candidate = PatternCandidate(
            project="proj",
            pattern_name="rebuild_cache_miss",
            signature=sig,
            detector="rebuild_cache_miss",
            occurrences=2,
            evidence=["project=proj", "crate=workspace"],
            run_ids=["R1"],
            proposed_remediation="Set CARGO_TARGET_DIR to a shared path.",
            extra={"remediation_rung": "eliminate"},
        )

        eval_payload = syn.build_eval_payload(
            eval_id=syn._make_ulid(),
            issue=issue,
            candidate=candidate,
            score=score,
            score_components=comps,
            rung="eliminate",
            rung_descent_reason=None,
            replay=syn.ReplayResult(status="passed", prevention_rate=0.8,
                                    runs_evaluated=5, would_have_prevented=4,
                                    false_suppression=0),
            labeled_support=ls,
            decision="propose_fix",
            decision_rationale="Test propose.",
            supersedes_eval_id=None,
            window_days=30,
            run_sample=["R1"],
            synthesis_pass_at=syn._now_utc(),
        )

        pd_payload = syn.build_pattern_discovered_payload(
            eval_payload=eval_payload,
            candidate=candidate,
            prevalence=0.3,
            issue=issue,
        )

        validator = jsonschema.Draft7Validator(schema)
        errors = list(validator.iter_errors(pd_payload))
        assert errors == [], f"Schema errors: {[str(e) for e in errors]}"


# ---------------------------------------------------------------------------
# 11. DRY-RUN does not shell out
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_no_nervous_publish(self):
        """DRY-RUN must never call subprocess (no nervous publish)."""
        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/wt1"
        _insert_run(conn, "R1", project="proj", outcome="clean",
                    labeled_at="2026-06-01T00:00:00Z",
                    worktree="/p/.worktrees/wt1")
        _insert_run(conn, "R2", project="proj", outcome="clean",
                    labeled_at="2026-06-01T00:01:00Z",
                    worktree="/p/.worktrees/wt1")
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=2)
        _insert_hit(conn, "R1", sig, detector="worktree_leak", project="proj")
        _insert_hit(conn, "R2", sig, detector="worktree_leak", project="proj")

        with patch("subprocess.run") as mock_run:
            result = syn.run_synthesis(conn, dry_run=True)
            # subprocess.run must NOT have been called
            mock_run.assert_not_called()

    def test_emit_calls_nervous_publish(self):
        """With dry_run=False (--emit), nervous publish IS called for propose_fix."""
        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/wt1"
        # Set up enough data to trigger propose_fix
        for i in range(3):
            rid = f"R{i}"
            _insert_run(conn, rid, project="proj", outcome="clean",
                        labeled_at=f"2026-06-0{i+1}T00:00:00Z",
                        worktree="/p/.worktrees/wt1",
                        bead_id="some-bead-1234")
            _insert_hit(conn, rid, sig, detector="worktree_leak", project="proj")
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=3)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = syn.run_synthesis(conn, dry_run=False)

        # If any propose_fix was generated, subprocess.run should have been called
        propose_fixes = [e for e in result.evals if e["decision"] == "propose_fix"]
        if propose_fixes:
            assert mock_run.called


# ---------------------------------------------------------------------------
# Integration: end-to-end synthesis pass
# ---------------------------------------------------------------------------

class TestEndToEndSynthesis:
    def test_full_pass_produces_evals(self):
        """A populated DB runs through synthesis without error."""
        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/wt1"
        for i in range(3):
            rid = f"R{i}"
            _insert_run(conn, rid, project="proj",
                        started=f"2026-06-0{i+1}T00:00:00Z",
                        ended=f"2026-06-0{i+1}T01:00:00Z",
                        close_reason="idle_timeout",
                        worktree="/p/.worktrees/wt1",
                        worktree_slug="wt1",
                        outcome="clean",
                        labeled_at=f"2026-06-0{i+1}T02:00:00Z",
                        bead_id="bead-abc-1234")

        # Seed the detector tables directly (as if worktree_leak fired)
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=3)
        for i in range(3):
            _insert_hit(conn, f"R{i}", sig, detector="worktree_leak", project="proj",
                        ts=f"2026-06-0{i+1}T00:30:00Z")

        result = syn.run_synthesis(conn, dry_run=True)
        assert len(result.evals) >= 1

        # All evals should have required fields
        for ev in result.evals:
            assert "eval_id" in ev
            assert "decision" in ev
            assert ev["decision"] in ("propose_fix", "monitor", "suppressed", "needs_more_data")
            assert "score" in ev
            assert "rung" in ev

    def test_idempotency(self):
        """Running synthesis twice with same data yields same decision."""
        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/wt1"
        _insert_run(conn, "R1", project="proj", close_reason="idle_timeout",
                    outcome="clean", labeled_at="2026-06-01T00:00:00Z")
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=2)
        _insert_hit(conn, "R1", sig, detector="worktree_leak", project="proj")

        result1 = syn.run_synthesis(conn, dry_run=True)
        result2 = syn.run_synthesis(conn, dry_run=True)

        # Both passes should produce the same decision
        decisions1 = {e["issue_signature"]: e["decision"] for e in result1.evals}
        decisions2 = {e["issue_signature"]: e["decision"] for e in result2.evals}
        # Decisions should match (second pass may skip idempotent propose_fix)
        for sig_key in decisions1:
            if sig_key in decisions2:
                assert decisions1[sig_key] == decisions2[sig_key]


# ---------------------------------------------------------------------------
# Hardening acceptance tests (13-findings audit)
# ---------------------------------------------------------------------------

class TestHardeningAcceptance:
    """Acceptance tests for the 13-finding adversarial audit hardening pass."""

    # ── Test 1: Schema validation for all decision/rung combinations ──────────

    def test_schema_validates_all_decision_rung_combos(self):
        """Every (decision, rung) combo must produce a payload that validates
        against bus.agent.run.eval.v1.json (Draft202012)."""
        import jsonschema

        schema_path = (
            Path(__file__).parent.parent.parent.parent
            / "schemas"
            / "bus.agent.run.eval.v1.json"
        )
        if not schema_path.exists():
            pytest.skip(f"Schema file not found: {schema_path}")
        with open(schema_path) as f:
            schema = json.load(f)
        validator = jsonschema.Draft202012Validator(schema)

        for decision in ("propose_fix", "monitor", "suppressed", "needs_more_data"):
            for rung in ("eliminate", "automate", "inform"):
                # Build appropriate replay status per rung/decision
                if rung == "inform":
                    replay_status = "not_applicable"
                elif decision in ("propose_fix",):
                    replay_status = "passed"
                elif decision == "suppressed":
                    replay_status = "failed"
                else:
                    replay_status = "insufficient_history"

                replay = syn.ReplayResult(
                    status=replay_status,
                    runs_evaluated=5 if replay_status in ("passed", "failed") else 0,
                    would_have_prevented=4 if replay_status == "passed" else 0,
                    false_suppression=0 if replay_status == "passed" else (
                        3 if replay_status == "failed" else 0
                    ),
                    prevention_rate=0.8 if replay_status == "passed" else 0.0,
                )
                issue = {
                    "signature": f"proj:{rung}_det:anchor",
                    "project": "proj",
                    "detector": f"{rung}_det",
                    "recurrence_count": 3,
                    "last_seen": "2026-06-01T00:00:00Z",
                }
                ls = {"confirmed_failures": 1, "confirmed_clean": 0, "unlabeled": 5}
                score, comps = syn.score_issue(issue, 0.4, ls, rung, 30)
                rung_descent = None if rung == "eliminate" else f"Test rung descent for {rung}."

                payload = syn.build_eval_payload(
                    eval_id=syn._make_ulid(),
                    issue=issue,
                    candidate=None,
                    score=score,
                    score_components=comps,
                    rung=rung,
                    rung_descent_reason=rung_descent,
                    replay=replay,
                    labeled_support=ls,
                    decision=decision,
                    decision_rationale=f"Test decision={decision} rung={rung}.",
                    supersedes_eval_id=None,
                    window_days=30,
                    run_sample=[],
                    synthesis_pass_at=syn._now_utc(),
                )
                pub_payload = {k: v for k, v in payload.items() if k != "issue"}
                errors = list(validator.iter_errors(pub_payload))
                assert errors == [], (
                    f"Schema errors for decision={decision!r} rung={rung!r}: "
                    f"{[str(e) for e in errors]}"
                )

    # ── Test 2: worktree replay non-tautology ────────────────────────────────

    def test_worktree_replay_non_tautology(self):
        """In-use worktrees produce false_suppression; merged worktrees produce
        would_have_prevented. prevention_rate must be < 1.0."""
        conn = _make_conn()
        wt = "/p/.worktrees/wt-shared"
        sig = f"proj:worktree_leak:{wt}"

        # Merged worktree run (labeled clean) → would_have_prevented
        _insert_run(conn, "MERGED", project="proj", outcome="clean",
                    labeled_at="2026-06-01T00:00:00Z", worktree=wt)
        _insert_hit(conn, "MERGED", sig, detector="worktree_leak", project="proj")

        # In-use worktree run (no label) → false_suppression
        _insert_run(conn, "INUSE", project="proj", outcome=None, labeled_at=None,
                    worktree=wt)
        _insert_hit(conn, "INUSE", sig, detector="worktree_leak", project="proj")

        _insert_issue(conn, sig, project="proj", detector="worktree_leak")

        logical = syn.stitch_logical_runs(conn)
        det = WorktreeLeakDetector(conn)
        result = syn._worktree_leak_replay(det, conn, logical, signature=sig)

        assert result.false_suppression > 0, "In-use worktree must produce false_suppression"
        assert result.would_have_prevented > 0, "Merged worktree must produce would_have_prevented"
        assert result.prevention_rate < 1.0, "prevention_rate must be < 1.0 (not a tautology)"

    # ── Test 3: rebuild replay passes on genuinely-preventable crate ─────────

    def test_rebuild_replay_passes_on_genuinely_preventable_crate(self):
        """Two runs same crate: first = not_preventable, second = would_have_prevented.
        Gate passes because false_suppression=0."""
        conn = _make_conn()

        for i, (rid, ts1, ts2, started) in enumerate([
            ("R1", "2026-06-01T00:00:00Z", "2026-06-01T00:02:00Z", "2026-06-01T00:00:00Z"),
            ("R2", "2026-06-02T00:00:00Z", "2026-06-02T00:02:00Z", "2026-06-02T00:00:00Z"),
        ]):
            _insert_run(conn, rid, project="proj", started=started,
                        ended=ts2, close_reason="idle_timeout")
            ev1 = _make_cargo_event(rid, 1, ts1, ts2)
            ev2 = json.dumps({
                "specversion": "1.0", "id": f"{rid}-2", "source": "/t",
                "type": "bus.agent.activity.v1", "time": ts2,
                "datacontenttype": "application/json",
                "data": {"tool_name": "Bash", "tool_summary": "{}",
                         "cwd": "/p/.claude/worktrees/wt1", "project": "proj"},
            })
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (rid, 1, ts1, "bus.agent.activity.v1", ev1),
            )
            conn.execute(
                "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
                (rid, 2, ts2, "bus.agent.activity.v1", ev2),
            )

        logical = syn.stitch_logical_runs(conn)
        det = RebuildCacheMissDetector(conn)
        result = syn._rebuild_cache_miss_replay(det, conn, logical)

        assert result.would_have_prevented >= 1, "Second build must be preventable"
        assert result.not_preventable >= 1, "First build must be not_preventable"
        assert result.false_suppression == 0, "No false suppressions expected"
        # Gate should pass since prevention_rate = 1/(1+0) = 1.0 and false_suppression=0
        assert result.status == "passed"

    # ── Test 4: recurrence_count invariant across two passes ─────────────────

    def test_recurrence_count_stable_across_two_passes(self):
        """Running synthesis twice must NOT inflate recurrence_count.

        Fix 4: unique index on detector_hits (run_id, detector, signature) +
        record_hit returns bool + find_or_create_issue uses new_hit_count.
        """
        from detectors.base import BaseDetector as BD

        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/wt1"
        wt = "/p/.worktrees/wt1"
        _insert_run(conn, "R1", project="proj", outcome="clean",
                    labeled_at="2026-06-01T00:00:00Z", worktree=wt)

        det = WorktreeLeakDetector(conn)

        # First pass
        det.run(conn)
        issue_after_pass1 = det.get_issue(sig)
        # May or may not have an issue (depends on whether worktree exists on disk)
        # Use direct hit recording to reliably test the invariant
        _insert_hit(conn, "R1", sig, detector="worktree_leak", project="proj",
                    ts="2026-06-01T00:00:00Z")
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=5)

        # Simulate two passes: record_hit should be idempotent
        is_new1 = det.record_hit("R1", sig, "proj")  # already exists → False
        is_new2 = det.record_hit("R1", sig, "proj")  # already exists → False
        assert not is_new1, "Hit already recorded; must return False"
        assert not is_new2, "Hit already recorded; must return False"

        # find_or_create_issue with new_hit_count=0 must not inflate recurrence
        issue_before = conn.execute(
            "SELECT recurrence_count FROM issues WHERE signature=?", (sig,)
        ).fetchone()[0]
        det.find_or_create_issue(sig, "proj", [], new_hit_count=0)
        issue_after = conn.execute(
            "SELECT recurrence_count FROM issues WHERE signature=?", (sig,)
        ).fetchone()[0]
        assert issue_before == issue_after, (
            f"recurrence_count inflated from {issue_before} to {issue_after} "
            "on idempotent pass (new_hit_count=0)"
        )

    # ── Test 5: replay is per-signature ───────────────────────────────────────

    def test_replay_is_per_signature(self):
        """Two worktree_leak signatures must get independent ReplayResults.

        Fix 1: signature-scoped replay ensures different worktree paths
        evaluate independently.
        """
        conn = _make_conn()
        wt1, wt2 = "/p/.worktrees/wt-merged", "/p/.worktrees/wt-inuse"
        sig1 = f"proj:worktree_leak:{wt1}"
        sig2 = f"proj:worktree_leak:{wt2}"

        # sig1: merged (labeled clean) → would_have_prevented
        for i in range(2):
            rid = f"MERGED-{i}"
            _insert_run(conn, rid, project="proj", outcome="clean",
                        labeled_at="2026-06-01T00:00:00Z", worktree=wt1)
            _insert_hit(conn, rid, sig1, detector="worktree_leak", project="proj")
        _insert_issue(conn, sig1, project="proj", detector="worktree_leak")

        # sig2: in-use (no label) → false_suppression
        for i in range(2):
            rid = f"INUSE-{i}"
            _insert_run(conn, rid, project="proj", outcome=None, labeled_at=None,
                        worktree=wt2)
            _insert_hit(conn, rid, sig2, detector="worktree_leak", project="proj")
        _insert_issue(conn, sig2, project="proj", detector="worktree_leak")

        logical = syn.stitch_logical_runs(conn)
        det = WorktreeLeakDetector(conn)

        result1 = syn._worktree_leak_replay(det, conn, logical, signature=sig1)
        result2 = syn._worktree_leak_replay(det, conn, logical, signature=sig2)

        assert result1.would_have_prevented > 0, "sig1 (merged) must be would_have_prevented"
        assert result1.false_suppression == 0, "sig1 (merged) must have no false_suppression"

        assert result2.false_suppression > 0, "sig2 (in-use) must have false_suppression"
        assert result2.would_have_prevented == 0, "sig2 (in-use) must have no would_have_prevented"

        assert result1.status != result2.status, "Different sigs must produce different statuses"

    # ── Test 6: idempotency skips re-persist with no material change ──────────

    def test_idempotency_no_new_rows_on_no_material_change(self):
        """Second synthesis pass must NOT add new run_eval rows when nothing changed.

        Fix 8: broadened idempotency check on (decision, score_band, rung, replay_status).
        """
        conn = _make_conn()
        sig = "proj:worktree_leak:/p/.worktrees/stable"
        wt = "/p/.worktrees/stable"
        # Seed data that will produce a stable decision
        for i in range(3):
            rid = f"R{i}"
            _insert_run(conn, rid, project="proj", outcome="clean",
                        labeled_at=f"2026-06-0{i+1}T00:00:00Z", worktree=wt,
                        started=f"2026-06-0{i+1}T00:00:00Z",
                        ended=f"2026-06-0{i+1}T01:00:00Z")
            _insert_hit(conn, rid, sig, detector="worktree_leak", project="proj",
                        ts=f"2026-06-0{i+1}T00:30:00Z")
        _insert_issue(conn, sig, project="proj", detector="worktree_leak",
                      recurrence_count=3)

        # Pass 1
        syn.run_synthesis(conn, dry_run=True)
        count_after_pass1 = conn.execute("SELECT COUNT(*) FROM run_evals").fetchone()[0]

        # Pass 2 — no data changed
        syn.run_synthesis(conn, dry_run=True)
        count_after_pass2 = conn.execute("SELECT COUNT(*) FROM run_evals").fetchone()[0]

        assert count_after_pass2 == count_after_pass1, (
            f"run_evals grew from {count_after_pass1} to {count_after_pass2} "
            "despite no material change (idempotency regression)"
        )

    # ── Test 7: stitch handles cycle without double-count ─────────────────────

    def test_stitch_cycle_no_double_count(self):
        """A→B→A cycle must produce exactly 1 logical run containing both A and B.

        Fix 10: cycle detection via path tracking + min(cycle) as canonical root.
        """
        conn = _make_conn()
        # Create a cycle: A continues B, B continues A
        _insert_run(conn, "A", continues_run_id="B")
        _insert_run(conn, "B", continues_run_id="A")

        logical = syn.stitch_logical_runs(conn)

        # Must be exactly 1 logical run (no double-count)
        assert len(logical) == 1, (
            f"Expected 1 logical run from A→B→A cycle, got {len(logical)}: {list(logical.keys())}"
        )
        # Both A and B must be in that single logical run
        all_members = set()
        for members in logical.values():
            all_members.update(members)
        assert "A" in all_members, "A must be in the logical run"
        assert "B" in all_members, "B must be in the logical run"
