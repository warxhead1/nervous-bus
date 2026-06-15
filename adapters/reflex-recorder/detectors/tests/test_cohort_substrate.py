"""tests/test_cohort_substrate.py — session-scoped lineage substrate + C1/A2.

Covers the second substrate tranche built on dispatch_lineage:
  - _summary_field / _summary_contains  (truncation-tolerant extraction)
  - derive_subagent_outcomes            (per-delegated-agent outcome class)
  - session_run_ids / load_session_events (pool idle-split runs of a session)
  - join_cohort_to_children             (dispatch -> child outcome, by agent id)
and the two detectors built on them:
  - directive_ground_truth_mismatch (A2)
  - inherited_rationalization       (C1, session-scoped)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.dispatch_lineage import (
    _summary_field,
    _summary_contains,
    derive_subagent_outcomes,
    session_run_ids,
    load_session_events,
    join_cohort_to_children,
    parse_dispatches,
    group_cohorts,
    load_run_events,
)
from detectors.directive_ground_truth_mismatch import (
    DirectiveGroundTruthMismatchDetector,
    find_clean_claim,
)
from detectors.inherited_rationalization import InheritedRationalizationDetector


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
    session_id   TEXT,
    started      TEXT NOT NULL,
    ended        TEXT NOT NULL,
    outcome      TEXT,
    close_reason TEXT
);
"""
_RUN_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    event_ts   TEXT NOT NULL,
    event_type TEXT NOT NULL,
    raw_json   TEXT NOT NULL
);
"""


def _make_db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA + _RUN_EVENTS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _run(conn, run_id, project="proj", session_id="sess", close_reason="idle_timeout"):
    now = _now_utc()
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, project, session_id, started, ended, close_reason)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, project, session_id, now, now, close_reason),
    )


def _ev(conn, run_id, seq, tool_name, summary="", resp="", cwd="/home/eric/projects/proj",
        project="proj", ts="2026-06-14T00:00:00Z"):
    s = json.dumps(summary) if not isinstance(summary, str) else summary
    r = json.dumps(resp) if not isinstance(resp, str) else resp
    data = {"cwd": cwd, "event": "tool_call", "project": project, "session_id": "sess",
            "tool_name": tool_name, "tool_summary": s, "tool_response_summary": r, "ts": ts}
    raw = json.dumps({"type": "bus.agent.activity.v1", "time": ts, "data": data})
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, ts, "bus.agent.activity.v1", raw),
    )


def _wt(slug, proj="proj"):
    return f"/home/eric/projects/{proj}/.claude/worktrees/{slug}"


# ── Truncation-tolerant extraction ──────────────────────────────────────────────

def test_summary_field_parses_valid_json():
    raw = json.dumps({"agentId": "abc", "prompt": "x"})
    assert _summary_field(raw, "agentId") == "abc"


def test_summary_field_recovers_from_truncated_json():
    # A real bounded summary cut mid-prompt — json.loads fails, regex recovers.
    truncated = '{"agentId":"af424e4e8d4f031af","canReadOutputFile":true,"prompt":"Implement bead nervous'
    assert _summary_field(truncated, "agentId") == "af424e4e8d4f031af"


def test_summary_field_missing_key():
    assert _summary_field('{"agentId":"a"}', "model") == ""
    assert _summary_field("", "agentId") == ""


def test_summary_contains_on_truncated_command():
    truncated = '{"command":"git commit -m \\"a very long message that gets cut off mid'
    assert _summary_contains(truncated, "git commit")
    assert not _summary_contains(truncated, "git push")


# ── derive_subagent_outcomes ────────────────────────────────────────────────────

def test_outcome_classes():
    conn = _make_db()
    _run(conn, "r1")
    # verified: edits + passing test
    _ev(conn, "r1", 1, "Edit", summary={"file_path": "a"}, cwd=_wt("agent-v"))
    _ev(conn, "r1", 2, "Bash", summary={"command": "cargo test"},
        resp={"stdout": "test result: ok. 5 passed"}, cwd=_wt("agent-v"))
    # unverified: edits, no test
    for i in range(3):
        _ev(conn, "r1", 10 + i, "Edit", summary={"file_path": f"f{i}"}, cwd=_wt("agent-u"))
    # left_red: edits + failing test last
    _ev(conn, "r1", 20, "Edit", summary={"file_path": "b"}, cwd=_wt("agent-r"))
    _ev(conn, "r1", 21, "Bash", summary={"command": "pytest"},
        resp={"stdout": "1 failed"}, cwd=_wt("agent-r"))
    # readonly: no edits
    _ev(conn, "r1", 30, "Read", summary={"file_path": "z"}, cwd=_wt("agent-ro"))

    outs = derive_subagent_outcomes(load_run_events(conn, "r1"), "proj")
    assert outs["agent-v"].outcome_class == "verified"
    assert outs["agent-u"].outcome_class == "unverified"
    assert outs["agent-r"].outcome_class == "left_red"
    assert outs["agent-ro"].outcome_class == "readonly"


def test_outcome_commit_signal_truncation_tolerant():
    conn = _make_db()
    _run(conn, "r1")
    _ev(conn, "r1", 1, "Edit", summary={"file_path": "a"}, cwd=_wt("agent-c"))
    _ev(conn, "r1", 2, "Bash",
        summary='{"command":"git commit -m \\"long msg cut off here so json is invalid',
        cwd=_wt("agent-c"))
    outs = derive_subagent_outcomes(load_run_events(conn, "r1"), "proj")
    assert outs["agent-c"].committed is True


# ── session pooling + cohort join ───────────────────────────────────────────────

def test_session_run_ids_groups_runs():
    conn = _make_db()
    _run(conn, "r1", session_id="sA")
    _run(conn, "r2", session_id="sA")
    _run(conn, "r3", session_id="sB")
    m = session_run_ids(conn)
    assert set(m["sA"]) == {"r1", "r2"} and m["sB"] == ["r3"]


def test_join_cohort_to_children_across_runs():
    # dispatch lands in run r1; child worktree activity lands in r2 of same session.
    conn = _make_db()
    _run(conn, "r1", session_id="sA")
    _run(conn, "r2", session_id="sA")
    _ev(conn, "r1", 1, "Agent",
        summary={"prompt": "do", "model": "sonnet", "name": "w1"},
        resp={"agentId": "deadbeef"}, ts="2026-06-14T00:00:00Z")
    # child runs in agent-deadbeef worktree, in the LATER run r2.
    for i in range(3):
        _ev(conn, "r2", i, "Edit", summary={"file_path": f"f{i}"},
            cwd=_wt("agent-deadbeef"), ts=f"2026-06-14T00:05:0{i}Z")

    run_ids = session_run_ids(conn)["sA"]
    events = load_session_events(conn, run_ids)
    outs = derive_subagent_outcomes(events)
    cohorts = group_cohorts(parse_dispatches(events))
    joined = join_cohort_to_children(cohorts[0], outs)
    assert len(joined) == 1
    assert joined[0].matched
    assert joined[0].outcome.child_agent_id == "deadbeef"
    assert joined[0].outcome.outcome_class == "unverified"


def test_join_unmatched_when_no_worktree():
    conn = _make_db()
    _run(conn, "r1", session_id="sA")
    _ev(conn, "r1", 1, "Agent", summary={"prompt": "x", "name": "w"},
        resp={"agentId": "nomatch"})
    events = load_session_events(conn, ["r1"])
    outs = derive_subagent_outcomes(events)
    joined = join_cohort_to_children(group_cohorts(parse_dispatches(events))[0], outs)
    assert joined[0].matched is False and joined[0].outcome is None


# ── directive_ground_truth_mismatch (A2) ────────────────────────────────────────

def test_find_clean_claim():
    assert find_clean_claim("the tests are green, proceed")
    assert find_clean_claim("treat the synthesis failure as pre-existing")
    assert find_clean_claim("just implement the feature") == ""


def test_a2_fires_on_false_clean_claim():
    conn = _make_db()
    _run(conn, "r1")
    _ev(conn, "r1", 1, "Bash", summary={"command": "pytest"},
        resp={"stdout": "1 failed"})  # baseline RED
    _ev(conn, "r1", 2, "Agent",
        summary={"prompt": "tests are green; add the feature", "name": "w"},
        resp={"agentId": "c1"})
    cands = DirectiveGroundTruthMismatchDetector(conn).detect(conn)
    assert len(cands) == 1
    assert cands[0].extra["actual_baseline"] == "failed"
    assert "green" in cands[0].extra["asserted_claim"].lower()


def test_a2_no_fire_when_baseline_actually_green():
    conn = _make_db()
    _run(conn, "r1")
    _ev(conn, "r1", 1, "Bash", summary={"command": "pytest"},
        resp={"stdout": "5 passed"})  # baseline GREEN — claim is true
    _ev(conn, "r1", 2, "Agent",
        summary={"prompt": "tests are green; add the feature", "name": "w"},
        resp={"agentId": "c1"})
    assert DirectiveGroundTruthMismatchDetector(conn).detect(conn) == []


def test_a2_no_fire_without_claim():
    conn = _make_db()
    _run(conn, "r1")
    _ev(conn, "r1", 1, "Bash", summary={"command": "pytest"}, resp={"stdout": "1 failed"})
    _ev(conn, "r1", 2, "Agent", summary={"prompt": "implement X", "name": "w"},
        resp={"agentId": "c1"})
    assert DirectiveGroundTruthMismatchDetector(conn).detect(conn) == []


# ── inherited_rationalization (C1) ──────────────────────────────────────────────

def _spawn_child(conn, run_id, slug, edits, test_status, base_seq):
    """Create a worktree segment with `edits` edits and a final test of given status."""
    for i in range(edits):
        _ev(conn, run_id, base_seq + i, "Edit", summary={"file_path": f"{slug}-f{i}"},
            cwd=_wt(slug), ts=f"2026-06-14T01:00:{base_seq + i:02d}Z")
    if test_status in ("passed", "failed"):
        out = "5 passed" if test_status == "passed" else "1 failed"
        _ev(conn, run_id, base_seq + edits, "Bash", summary={"command": "pytest"},
            resp={"stdout": out}, cwd=_wt(slug), ts=f"2026-06-14T01:00:{base_seq + edits:02d}Z")


def test_c1_fires_on_converged_unverified_cohort():
    conn = _make_db()
    _run(conn, "r1", session_id="sA")
    _run(conn, "r2", session_id="sA")
    # one fan-out of 3 dispatches
    for i, cid in enumerate(("ca", "cb", "cc")):
        _ev(conn, "r1", i, "Agent", summary={"prompt": "do work", "name": f"w{i}"},
            resp={"agentId": cid}, ts=f"2026-06-14T00:00:0{i}Z")
    # all three children ship UNVERIFIED (edits, no test) in the later run
    _spawn_child(conn, "r2", "agent-ca", 3, "none", 0)
    _spawn_child(conn, "r2", "agent-cb", 3, "none", 10)
    _spawn_child(conn, "r2", "agent-cc", 3, "none", 20)
    cands = InheritedRationalizationDetector(conn).detect(conn)
    assert len(cands) == 1
    assert cands[0].extra["shared_class"] == "unverified"
    assert cands[0].extra["matched_children"] == 3


def test_c1_no_fire_on_diverse_cohort():
    conn = _make_db()
    _run(conn, "r1", session_id="sB")
    _run(conn, "r2", session_id="sB")
    for i, cid in enumerate(("da", "db", "dc")):
        _ev(conn, "r1", i, "Agent", summary={"prompt": "do", "name": f"w{i}"},
            resp={"agentId": cid}, ts=f"2026-06-14T00:00:0{i}Z")
    # diverse outcomes: verified / unverified / readonly -> no majority degenerate class
    _spawn_child(conn, "r2", "agent-da", 3, "passed", 0)
    _spawn_child(conn, "r2", "agent-db", 3, "none", 10)
    _ev(conn, "r2", 30, "Read", summary={"file_path": "z"}, cwd=_wt("agent-dc"))
    assert InheritedRationalizationDetector(conn).detect(conn) == []


def test_c1_benign_verified_convergence_does_not_fire():
    conn = _make_db()
    _run(conn, "r1", session_id="sC")
    _run(conn, "r2", session_id="sC")
    for i, cid in enumerate(("ea", "eb")):
        _ev(conn, "r1", i, "Agent", summary={"prompt": "do", "name": f"w{i}"},
            resp={"agentId": cid}, ts=f"2026-06-14T00:00:0{i}Z")
    # both verified — a GOOD shared outcome must not fire.
    _spawn_child(conn, "r2", "agent-ea", 3, "passed", 0)
    _spawn_child(conn, "r2", "agent-eb", 3, "passed", 10)
    assert InheritedRationalizationDetector(conn).detect(conn) == []
