"""test_trajectory_profile.py — unit tests for the inductive trajectory profiler.

Builds an in-memory runs.db with a hand-authored run_events stream that plants
each heuristic, then asserts the profiler flags it. No LLM, no live DB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Pin the overlay to an empty location so taxonomy resolution is hermetic
# (generic default) and never couples to whatever private adapters are installed.
os.environ["NERVOUS_HOME"] = "/tmp/reflex-test-empty-home-doesnotexist"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trajectory_profile as tp  # noqa: E402


# ── fixture: in-memory store with a synthetic run ────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, run_key_kind TEXT,
            worktree_slug TEXT, git_branch TEXT, outcome TEXT, event_count INTEGER,
            started TEXT, ended TEXT);
        CREATE TABLE run_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            seq INTEGER, event_ts TEXT, event_type TEXT, raw_json TEXT);
        """
    )
    return c


def _ev(tool: str, summary: dict, ts: str, stderr: str = "") -> str:
    data = {
        "tool_name": tool, "event": "tool_call", "ts": ts,
        "tool_summary": json.dumps(summary),
        "tool_response_summary": json.dumps({"stderr": stderr}),
    }
    return json.dumps({"data": data})


def _seed(c: sqlite3.Connection, run_id: str, events: list[tuple[str, dict, str]]) -> None:
    c.execute(
        "INSERT INTO runs (run_id, project, run_key_kind, worktree_slug, event_count) "
        "VALUES (?,?,?,?,?)",
        (run_id, "tengine", "worktree", "agent-test", len(events)),
    )
    for i, (tool, summ, ts) in enumerate(events):
        c.execute(
            "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) "
            "VALUES (?,?,?,?,?)",
            (run_id, i, ts, "bus.agent.activity.v1", _ev(tool, summ, ts)),
        )
    c.commit()


def _t(sec: int) -> str:
    """RFC3339 ts at base + sec seconds."""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"2026-06-13T{10 + h:02d}:{m:02d}:{s:02d}Z"


# ── normalisation ─────────────────────────────────────────────────────────────

def test_normalise_collapses_literals():
    a = tp.normalise_cmd('TENGINE_ROOT=/home/eric/x silo_tester svdag_racing 350')
    b = tp.normalise_cmd('TENGINE_ROOT=/home/eric/y silo_tester svdag_racing 80')
    assert a == b, f"{a!r} != {b!r}"


def test_normalise_distinguishes_different_verbs():
    assert tp.normalise_cmd("cargo build") != tp.normalise_cmd("cargo test")


# ── missing-tooling smell ─────────────────────────────────────────────────────

def test_missing_tooling_smell_flagged():
    c = _conn()
    evs = []
    for i in range(6):
        evs.append(("Bash",
                    {"command": f'export TENGINE_ROOT="$PWD"; silo_tester svdag_racing {i*10}'},
                    _t(i * 30)))
    _seed(c, "r1", evs)
    p = tp.profile_run(c, "r1")
    clusters = [r for r in p.repeated_cmd_clusters if r["count"] >= 5]
    assert clusters, "should cluster the 6 structurally-identical invocations"
    assert any("missing_tooling_smell" in f for f in p.flags)


# ── redo reads ────────────────────────────────────────────────────────────────

def test_redo_reads_flagged():
    c = _conn()
    f = "/home/eric/projects/tengine/crates/x/svdag_raymarch.slang"
    evs = [("Read", {"file_path": f}, _t(i * 10)) for i in range(4)]
    _seed(c, "r2", evs)
    p = tp.profile_run(c, "r2")
    assert p.redo_reads and p.redo_reads[0]["count"] == 4
    assert any("redo_reads" in f for f in p.flags)


# ── busy-wait polling ─────────────────────────────────────────────────────────

def test_busy_wait_polling_flagged():
    c = _conn()
    evs = [
        ("Bash", {"command": "sleep 15 && cat /tmp/silo.log"}, _t(0)),
        ("Bash", {"command": "sleep 10 && tail -20 /tmp/silo.log"}, _t(30)),
    ]
    _seed(c, "r3", evs)
    p = tp.profile_run(c, "r3")
    assert len(p.poll_loops) == 2
    assert any("busy_wait_polling" in f for f in p.flags)


# ── stalls / wait-dominated ───────────────────────────────────────────────────

def test_stall_and_wait_dominated():
    c = _conn()
    # two quick calls then a 600s GPU step gap, total span dominated by the wait
    evs = [
        ("Bash", {"command": "shadergen start svdag_racing"}, _t(0)),
        ("Bash", {"command": "shadergen step 350"}, _t(10)),       # gap_after=600
        ("Bash", {"command": "shadergen status"}, _t(610)),
    ]
    _seed(c, "r4", evs)
    p = tp.profile_run(c, "r4")
    assert any(s["gap_s"] >= 500 for s in p.stalls)
    assert "wait_dominated" in p.flags


# ── no-edit-verify (pure exploration) ─────────────────────────────────────────

def test_no_edit_verify_cycle_on_pure_exploration():
    c = _conn()
    evs = [("Grep" if i % 2 else "Read",
            {"pattern": f"sym_{i}"} if i % 2 else {"file_path": f"/x/f{i}.rs"},
            _t(i * 5)) for i in range(32)]
    _seed(c, "r5", evs)
    p = tp.profile_run(c, "r5")
    assert p.edit_verify_cycles == 0
    assert "no_edit_verify_cycle" in p.flags


def test_edit_verify_cycle_detected():
    c = _conn()
    evs = [
        ("Read", {"file_path": "/x/a.rs"}, _t(0)),
        ("Edit", {"file_path": "/x/a.rs"}, _t(10)),
        ("Bash", {"command": "cargo build -p tengine-dgc-hal"}, _t(20)),
    ]
    _seed(c, "r6", evs)
    p = tp.profile_run(c, "r6")
    assert p.edit_verify_cycles >= 1
    assert "no_edit_verify_cycle" not in p.flags


# ── subagent enumeration ──────────────────────────────────────────────────────

def test_subagent_run_ids_filters_by_events():
    c = _conn()
    _seed(c, "big", [("Read", {"file_path": "/x"}, _t(i)) for i in range(12)])
    _seed(c, "small", [("Read", {"file_path": "/x"}, _t(i)) for i in range(3)])
    ids = tp.subagent_run_ids(c, project="tengine", min_events=10)
    assert "big" in ids and "small" not in ids
