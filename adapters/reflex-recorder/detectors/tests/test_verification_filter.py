"""tests/test_verification_filter.py — project-aware verification + non-code filter.

The orchestration detectors must NOT flag an agent as "unverified" when it ran a
project's bespoke verification harness (tengine: silo_tester / shadergen /
gpu_verify_lock / tsdl_validate), nor when it edited only non-code artifacts
(docs / .planning). Covers:
  - is_code_path classification (code vs non-code vs unknown-conservative)
  - segment_by_worktree code_edit_count (separate from edit_count)
  - derive_subagent_outcomes 'docs' class for non-code-only edits
  - injected is_verify reclassifies an otherwise-unverified agent as verified
  - build_verifier: generic floor + a project adapter's taxonomy verbs
  - unverified_completion exempts doc-only agents and harness-verified agents
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
    is_code_path,
    segment_by_worktree,
    derive_subagent_outcomes,
    load_run_events,
    default_verify,
)
from detectors.verification import build_verifier
from detectors.unverified_completion import UnverifiedCompletionDetector, EDIT_THRESHOLD
from adapter_api import ProjectAdapter, CommandTaxonomy


_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, project TEXT NOT NULL, session_id TEXT,
    started TEXT NOT NULL, ended TEXT NOT NULL, outcome TEXT, close_reason TEXT
);
"""
_EV_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    event_ts TEXT NOT NULL, event_type TEXT NOT NULL, raw_json TEXT NOT NULL
);
"""


def _db():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.executescript(_RUNS_SCHEMA + _EV_SCHEMA)
    ensure_detector_schema(c)
    return c


def _run(c, rid, project="proj"):
    n = _now_utc()
    c.execute("INSERT OR REPLACE INTO runs (run_id,project,session_id,started,ended,close_reason)"
              " VALUES (?,?,?,?,?,?)", (rid, project, "s", n, n, "idle_timeout"))


def _ev(c, rid, seq, tool, summary="", resp="", cwd="/home/eric/projects/proj", project="proj"):
    s = json.dumps(summary) if not isinstance(summary, str) else summary
    r = json.dumps(resp) if not isinstance(resp, str) else resp
    data = {"cwd": cwd, "event": "tool_call", "project": project, "tool_name": tool,
            "tool_summary": s, "tool_response_summary": r, "ts": "2026-06-14T00:00:00Z"}
    raw = json.dumps({"type": "bus.agent.activity.v1", "time": "2026-06-14T00:00:00Z", "data": data})
    c.execute("INSERT INTO run_events (run_id,seq,event_ts,event_type,raw_json) VALUES (?,?,?,?,?)",
              (rid, seq, "2026-06-14T00:00:00Z", "bus.agent.activity.v1", raw))


def _wt(slug, proj="proj"):
    return f"/home/eric/projects/{proj}/.claude/worktrees/{slug}"


# ── is_code_path ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("/p/x/foo.rs", True), ("/p/x/shader.slang", True), ("/p/x/grammar.tsdl", True),
    ("/p/x/tool.py", True), ("/p/x/mod.c", True),
    ("/p/x/.planning/notes.md", False), ("/p/x/docs/guide.md", False),
    ("/p/x/findings.txt", False), ("/p/x/.beads/issues.jsonl", False),
    ("", True),                       # unknown (truncated) -> conservative code
    ("/p/x/weirdfile", True),         # no extension -> conservative code
])
def test_is_code_path(path, expected):
    assert is_code_path(path) is expected


# ── code_edit_count ─────────────────────────────────────────────────────────────

def test_segment_tracks_code_vs_all_edits():
    c = _db(); _run(c, "r1")
    wt = _wt("agent-a")
    _ev(c, "r1", 1, "Edit", summary={"file_path": "/p/x/a.rs"}, cwd=wt)
    _ev(c, "r1", 2, "Edit", summary={"file_path": "/p/x/.planning/plan.md"}, cwd=wt)
    _ev(c, "r1", 3, "Write", summary={"file_path": "/p/x/notes.txt"}, cwd=wt)
    seg = segment_by_worktree(load_run_events(c, "r1"))["agent-a"]
    assert seg.edit_count == 3 and seg.code_edit_count == 1


# ── docs outcome class ──────────────────────────────────────────────────────────

def test_docs_only_agent_is_not_unverified():
    c = _db(); _run(c, "r1")
    wt = _wt("agent-docs")
    for i in range(4):
        _ev(c, "r1", i, "Write", summary={"file_path": f"/p/x/.planning/finding{i}.md"}, cwd=wt)
    outs = derive_subagent_outcomes(load_run_events(c, "r1"), "proj")
    assert outs["agent-docs"].outcome_class == "docs"


# ── injected verifier reclassifies ──────────────────────────────────────────────

def test_injected_verifier_marks_harness_run_as_verified():
    c = _db(); _run(c, "r1")
    wt = _wt("agent-h")
    _ev(c, "r1", 1, "Edit", summary={"file_path": "/p/x/shader.slang"}, cwd=wt)
    _ev(c, "r1", 2, "Bash", summary={"command": "silo_tester mysilo --frames=200"},
        resp={"stdout": "frames ok"}, cwd=wt)
    # default verifier does NOT know silo_tester -> unverified.
    d = derive_subagent_outcomes(load_run_events(c, "r1"), "proj")
    assert d["agent-h"].outcome_class == "unverified"
    # a verifier that recognizes silo_tester -> verified.
    isv = lambda cmd, project="": "silo_tester" in cmd or default_verify(cmd, project)
    d2 = derive_subagent_outcomes(load_run_events(c, "r1"), "proj", is_verify=isv)
    assert d2["agent-h"].outcome_class == "verified"


# ── build_verifier: generic floor + adapter taxonomy ────────────────────────────

class _FakeTaxonomy(CommandTaxonomy):
    def classify(self, cmd):
        if "myverify" in (cmd or ""):
            return "run-verify"
        return super().classify(cmd)


class _FakeAdapter(ProjectAdapter):
    name = "faketengine"
    def matches(self, project):
        return project == "faketengine"
    def taxonomy(self):
        return _FakeTaxonomy()


def test_build_verifier_generic_floor():
    isv = build_verifier(adapters=[])
    assert isv("cargo test -p x", "anyproj") is True
    assert isv("pytest -q", "anyproj") is True
    assert isv("grep foo bar", "anyproj") is False
    assert isv("", "anyproj") is False


def test_build_verifier_uses_adapter_taxonomy():
    isv = build_verifier(adapters=[_FakeAdapter()])
    # a verb only the fake adapter knows, scoped to its project
    assert isv("myverify run", "faketengine") is True
    # same verb for a different project falls back to generic floor (not verify)
    assert isv("myverify run", "otherproj") is False
    # generic floor still works regardless of project
    assert isv("cargo build -p y", "faketengine") is True


# ── unverified_completion end-to-end exemptions ─────────────────────────────────

def test_unverified_completion_exempts_docs_only_agent():
    c = _db(); _run(c, "r1")
    wt = _wt("agent-docs")
    for i in range(EDIT_THRESHOLD + 2):
        _ev(c, "r1", i, "Write", summary={"file_path": f"/p/x/.planning/d{i}.md"}, cwd=wt)
    assert UnverifiedCompletionDetector(c).detect(c) == []


def test_unverified_completion_still_flags_real_code_no_test():
    c = _db(); _run(c, "r1")
    wt = _wt("agent-code")
    for i in range(EDIT_THRESHOLD):
        _ev(c, "r1", i, "Edit", summary={"file_path": f"/p/x/m{i}.rs"}, cwd=wt)
    cands = UnverifiedCompletionDetector(c).detect(c)
    assert len(cands) == 1 and cands[0].extra["unverified_agents"] == 1
