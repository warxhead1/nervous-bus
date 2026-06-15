"""tests/test_orchestration_detectors.py — the dispatch-quality detector family.

Covers the shared substrate (detectors/dispatch_lineage.py) plus the two
detectors built on it:
  - red_baseline_dispatch   (A1: fan-out on a red / unestablished baseline)
  - unverified_completion   (F1 grounded: delegated agent ships edits, no test)

Substrate:
  - parse_dispatches extracts prompt/model/name/child_agent_id from Agent/Task
  - group_cohorts buckets dispatches by temporal proximity
  - last_test_signal_before classifies failed/passed/unknown/absent
  - segment_by_worktree slices a run into per-delegated-agent segments

Detectors:
  - red: fires when last build/test before fan-out failed
  - no_baseline: fires only when width >= MIN_NO_BASELINE_WIDTH and no test ran
  - passed baseline / narrow no-baseline do NOT fire
  - unverified fires for a worktree segment with >=EDIT_THRESHOLD edits, 0 tests
  - a segment that DID run a test does NOT fire
  - signatures contain no run_id/timestamp (cross-run dedup stable)
  - recurrence_count increments across scans
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import pytest

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.dispatch_lineage import (
    parse_dispatches,
    group_cohorts,
    last_test_signal_before,
    segment_by_worktree,
    worktree_slug_of,
    load_run_events,
    _epoch,
)
from detectors.red_baseline_dispatch import (
    RedBaselineDispatchDetector,
    MIN_NO_BASELINE_WIDTH,
)
from detectors.unverified_completion import (
    UnverifiedCompletionDetector,
    EDIT_THRESHOLD,
)


# ── In-memory DB scaffolding ────────────────────────────────────────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    project      TEXT NOT NULL,
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


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA + _RUN_EVENTS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(conn, run_id, project="testproj", close_reason="idle_timeout", outcome=None):
    now = _now_utc()
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, project, started, ended, outcome, close_reason)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, project, now, now, outcome, close_reason),
    )


def _event_json(tool_name, tool_summary, tool_response_summary="", project="testproj",
                cwd="/home/eric/projects/testproj", ts="2026-06-14T00:00:00Z"):
    ts_str = json.dumps(tool_summary) if not isinstance(tool_summary, str) else tool_summary
    rs_str = (json.dumps(tool_response_summary)
              if not isinstance(tool_response_summary, str) else tool_response_summary)
    data = {
        "agent_kind": "host_claude_code", "cwd": cwd, "event": "tool_call",
        "project": project, "session_id": "s", "tool_name": tool_name,
        "tool_summary": ts_str, "tool_response_summary": rs_str, "ts": ts,
    }
    return json.dumps({"type": "bus.agent.activity.v1", "time": ts, "data": data})


def _insert_event(conn, run_id, seq, tool_name, tool_summary="", tool_response_summary="",
                  project="testproj", cwd="/home/eric/projects/testproj", ts="2026-06-14T00:00:00Z"):
    conn.execute(
        "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
        (run_id, seq, ts, "bus.agent.activity.v1",
         _event_json(tool_name, tool_summary, tool_response_summary, project, cwd, ts)),
    )


def _dispatch_event(conn, run_id, seq, child_id, prompt="do work", name="w", model="sonnet",
                    ts="2026-06-14T00:00:00Z", project="testproj"):
    _insert_event(
        conn, run_id, seq, "Agent",
        tool_summary={"description": "d", "isolation": "worktree", "model": model,
                      "name": name, "prompt": prompt},
        tool_response_summary={"agentId": child_id, "description": "d"},
        project=project, ts=ts,
    )


# ── Substrate: parse_dispatches ─────────────────────────────────────────────────

def test_parse_dispatches_extracts_fields():
    conn = _make_db()
    _insert_run(conn, "r1")
    _dispatch_event(conn, "r1", 1, "abc123", prompt="implement X", name="impl-x", model="opus")
    events = load_run_events(conn, "r1")
    ds = parse_dispatches(events)
    assert len(ds) == 1
    assert ds[0].child_agent_id == "abc123"
    assert ds[0].prompt == "implement X"
    assert ds[0].name == "impl-x"
    assert ds[0].model == "opus"


def test_parse_dispatches_ignores_non_dispatch_tools():
    conn = _make_db()
    _insert_run(conn, "r1")
    _insert_event(conn, "r1", 1, "Bash", tool_summary={"command": "ls"})
    _insert_event(conn, "r1", 2, "Read", tool_summary={"file_path": "/x"})
    assert parse_dispatches(load_run_events(conn, "r1")) == []


# ── Substrate: group_cohorts ────────────────────────────────────────────────────

def test_group_cohorts_splits_on_time_gap():
    conn = _make_db()
    _insert_run(conn, "r1")
    # 3 dispatches close together, then a 4th far later -> 2 cohorts.
    _dispatch_event(conn, "r1", 1, "a", ts="2026-06-14T00:00:00Z")
    _dispatch_event(conn, "r1", 2, "b", ts="2026-06-14T00:00:05Z")
    _dispatch_event(conn, "r1", 3, "c", ts="2026-06-14T00:00:10Z")
    _dispatch_event(conn, "r1", 4, "d", ts="2026-06-14T01:00:00Z")
    cohorts = group_cohorts(parse_dispatches(load_run_events(conn, "r1")))
    assert [len(c) for c in cohorts] == [3, 1]
    assert cohorts[0][0].cohort_id == 0 and cohorts[1][0].cohort_id == 1


def test_group_cohorts_single_burst():
    conn = _make_db()
    _insert_run(conn, "r1")
    for i in range(5):
        _dispatch_event(conn, "r1", i, f"c{i}", ts=f"2026-06-14T00:00:0{i}Z")
    cohorts = group_cohorts(parse_dispatches(load_run_events(conn, "r1")))
    assert len(cohorts) == 1 and len(cohorts[0]) == 5


def test_epoch_handles_nanosecond_and_z():
    assert _epoch("2026-06-13T06:56:48.040997826Z") is not None
    assert _epoch("2026-06-13T06:56:48Z") is not None
    assert _epoch("") is None
    assert _epoch("not-a-time") is None


# ── Substrate: last_test_signal_before ──────────────────────────────────────────

def test_baseline_failed():
    conn = _make_db()
    _insert_run(conn, "r1")
    _insert_event(conn, "r1", 1, "Bash",
                  tool_summary={"command": "cargo test"},
                  tool_response_summary={"stdout": "test result: FAILED", "stderr": ""})
    _dispatch_event(conn, "r1", 2, "child")
    ev = load_run_events(conn, "r1")
    sig = last_test_signal_before(ev, seq=2)
    assert sig.status == "failed"


def test_baseline_passed():
    conn = _make_db()
    _insert_run(conn, "r1")
    _insert_event(conn, "r1", 1, "Bash",
                  tool_summary={"command": "pytest"},
                  tool_response_summary={"stdout": "5 passed in 1.2s", "stderr": ""})
    _dispatch_event(conn, "r1", 2, "child")
    sig = last_test_signal_before(load_run_events(conn, "r1"), seq=2)
    assert sig.status == "passed"


def test_baseline_absent_when_no_build_command():
    conn = _make_db()
    _insert_run(conn, "r1")
    _insert_event(conn, "r1", 1, "Bash", tool_summary={"command": "git status"})
    _dispatch_event(conn, "r1", 2, "child")
    sig = last_test_signal_before(load_run_events(conn, "r1"), seq=2)
    assert sig.status == "absent"


def test_baseline_only_considers_events_before_seq():
    conn = _make_db()
    _insert_run(conn, "r1")
    _dispatch_event(conn, "r1", 1, "child")
    # failing test AFTER the dispatch must not count as the baseline.
    _insert_event(conn, "r1", 2, "Bash",
                  tool_summary={"command": "cargo test"},
                  tool_response_summary={"stdout": "error[E0001]"})
    sig = last_test_signal_before(load_run_events(conn, "r1"), seq=1)
    assert sig.status == "absent"


# ── Substrate: segment_by_worktree ──────────────────────────────────────────────

def test_worktree_slug_of():
    assert worktree_slug_of("/p/x/.claude/worktrees/agent-abc/sub") == "agent-abc"
    assert worktree_slug_of("/p/x/.worktrees/wf_123-1/sub") == "wf_123-1"
    assert worktree_slug_of("/home/eric/projects/x") is None


def test_segment_by_worktree_groups_and_counts():
    conn = _make_db()
    _insert_run(conn, "r1")
    wt = "/home/eric/projects/x/.claude/worktrees/agent-deadbeef"
    _insert_event(conn, "r1", 1, "Edit", tool_summary={"file_path": "a.py"}, cwd=wt)
    _insert_event(conn, "r1", 2, "Write", tool_summary={"file_path": "b.py"}, cwd=wt)
    _insert_event(conn, "r1", 3, "Bash", tool_summary={"command": "cargo test"}, cwd=wt)
    # an event in the main tree must be excluded from segments.
    _insert_event(conn, "r1", 4, "Edit", tool_summary={"file_path": "c.py"},
                  cwd="/home/eric/projects/x")
    segs = segment_by_worktree(load_run_events(conn, "r1"))
    assert set(segs) == {"agent-deadbeef"}
    seg = segs["agent-deadbeef"]
    assert seg.edit_count == 2 and seg.build_test_count == 1
    assert seg.child_agent_id == "deadbeef"


# ── red_baseline_dispatch ───────────────────────────────────────────────────────

def test_red_baseline_fires_on_failed_test():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    _insert_event(conn, "r1", 1, "Bash",
                  tool_summary={"command": "cargo test"},
                  tool_response_summary={"stdout": "test result: FAILED"}, project="proj")
    _dispatch_event(conn, "r1", 2, "child", project="proj")
    cands = RedBaselineDispatchDetector(conn).detect(conn)
    assert len(cands) == 1
    c = cands[0]
    assert c.extra["kind"] == "red"
    assert c.extra["remediation_rung"] == "eliminate"
    assert c.signature == "proj:red_baseline_dispatch:red"


def test_no_baseline_fires_only_at_width_threshold():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    # exactly MIN_NO_BASELINE_WIDTH dispatches, no prior test -> fires no_baseline.
    for i in range(MIN_NO_BASELINE_WIDTH):
        _dispatch_event(conn, "r1", i, f"c{i}", ts=f"2026-06-14T00:00:0{i}Z", project="proj")
    cands = RedBaselineDispatchDetector(conn).detect(conn)
    assert len(cands) == 1 and cands[0].extra["kind"] == "no_baseline"


def test_narrow_no_baseline_does_not_fire():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    # width below threshold, no prior test -> must NOT fire.
    for i in range(MIN_NO_BASELINE_WIDTH - 1):
        _dispatch_event(conn, "r1", i, f"c{i}", ts=f"2026-06-14T00:00:0{i}Z", project="proj")
    assert RedBaselineDispatchDetector(conn).detect(conn) == []


def test_green_baseline_does_not_fire():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    _insert_event(conn, "r1", 1, "Bash",
                  tool_summary={"command": "pytest"},
                  tool_response_summary={"stdout": "10 passed"}, project="proj")
    for i in range(2, 2 + MIN_NO_BASELINE_WIDTH):
        _dispatch_event(conn, "r1", i, f"c{i}", ts=f"2026-06-14T00:00:0{i}Z", project="proj")
    assert RedBaselineDispatchDetector(conn).detect(conn) == []


def test_red_baseline_signature_has_no_run_id():
    conn = _make_db()
    _insert_run(conn, "rUNIQUE123", project="proj")
    _insert_event(conn, "rUNIQUE123", 1, "Bash",
                  tool_summary={"command": "cargo test"},
                  tool_response_summary={"stdout": "test result: FAILED"}, project="proj")
    _dispatch_event(conn, "rUNIQUE123", 2, "child", project="proj")
    c = RedBaselineDispatchDetector(conn).detect(conn)[0]
    assert "rUNIQUE123" not in c.signature


# ── unverified_completion ───────────────────────────────────────────────────────

def _wt(slug, proj="proj"):
    return f"/home/eric/projects/{proj}/.claude/worktrees/{slug}"


def test_unverified_fires_for_edits_without_test():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    wt = _wt("agent-aaa")
    for i in range(EDIT_THRESHOLD):
        _insert_event(conn, "r1", i, "Edit", tool_summary={"file_path": f"f{i}.py"},
                      cwd=wt, project="proj")
    cands = UnverifiedCompletionDetector(conn).detect(conn)
    assert len(cands) == 1
    assert cands[0].extra["unverified_agents"] == 1
    assert cands[0].signature == "proj:unverified_completion"


def test_unverified_does_not_fire_when_test_ran():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    wt = _wt("agent-aaa")
    for i in range(EDIT_THRESHOLD):
        _insert_event(conn, "r1", i, "Edit", tool_summary={"file_path": f"f{i}.py"},
                      cwd=wt, project="proj")
    _insert_event(conn, "r1", 99, "Bash", tool_summary={"command": "pytest -q"},
                  cwd=wt, project="proj")
    assert UnverifiedCompletionDetector(conn).detect(conn) == []


def test_unverified_below_edit_threshold_does_not_fire():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    wt = _wt("agent-aaa")
    for i in range(EDIT_THRESHOLD - 1):
        _insert_event(conn, "r1", i, "Edit", tool_summary={"file_path": f"f{i}.py"},
                      cwd=wt, project="proj")
    assert UnverifiedCompletionDetector(conn).detect(conn) == []


def test_unverified_ignores_main_tree_edits():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    # edits in the orchestrator's own main tree are not a delegated segment.
    for i in range(EDIT_THRESHOLD + 2):
        _insert_event(conn, "r1", i, "Edit", tool_summary={"file_path": f"f{i}.py"},
                      cwd="/home/eric/projects/proj", project="proj")
    assert UnverifiedCompletionDetector(conn).detect(conn) == []


def test_unverified_multiple_agents_one_candidate():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    for slug in ("agent-aaa", "agent-bbb"):
        for i in range(EDIT_THRESHOLD):
            _insert_event(conn, "r1", hash((slug, i)) % 100000, "Edit",
                          tool_summary={"file_path": f"f{i}.py"}, cwd=_wt(slug), project="proj")
    cands = UnverifiedCompletionDetector(conn).detect(conn)
    assert len(cands) == 1
    assert cands[0].extra["unverified_agents"] == 2
    assert cands[0].occurrences == 2


# ── Kyoko recurrence across both detectors ──────────────────────────────────────

def test_recurrence_increments_across_scans():
    conn = _make_db()
    _insert_run(conn, "r1", project="proj")
    _insert_event(conn, "r1", 1, "Bash",
                  tool_summary={"command": "cargo test"},
                  tool_response_summary={"stdout": "test result: FAILED"}, project="proj")
    _dispatch_event(conn, "r1", 2, "child", project="proj")
    det = RedBaselineDispatchDetector(conn)
    det.run(conn)
    sig = "proj:red_baseline_dispatch:red"
    first = det.get_issue(sig)["recurrence_count"]
    # a second run with the same signature increments recurrence.
    _insert_run(conn, "r2", project="proj")
    _insert_event(conn, "r2", 1, "Bash",
                  tool_summary={"command": "cargo test"},
                  tool_response_summary={"stdout": "test result: FAILED"}, project="proj")
    _dispatch_event(conn, "r2", 2, "child2", project="proj")
    det.run(conn)
    assert det.get_issue(sig)["recurrence_count"] == first + 1
    # re-scanning the SAME data does not inflate recurrence (idempotent).
    det.run(conn)
    assert det.get_issue(sig)["recurrence_count"] == first + 1
