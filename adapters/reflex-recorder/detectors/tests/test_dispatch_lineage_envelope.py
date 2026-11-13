"""detectors/tests/test_dispatch_lineage_envelope.py -- dispatch lineage on
post-metadata-only capture (issue #119).

Current Agent events carry no `prompt` / `agentId` in their summaries
(`{"description":..,"model":..,"prompt_bytes":N}` / `{"bytes":N,"is_error":false}`).
The child id comes from child envelopes (`parent_tool_use_id` -> `subagent_id`)
and, failing that, from the session transcript; the prompt only from the
transcript. All fixtures are synthetic.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.dispatch_lineage import (
    derive_subagent_outcomes,
    group_cohorts,
    join_cohort_to_children,
    load_run_events,
    load_session_events,
    parse_dispatches,
)
from detectors.transcript_dispatch import parse_transcript_dispatches

TUID = "toolu_synthetic_0001"
CHILD = "a0123456789abcdef"
PROMPT = "Fix the flaky widget test in module X. Run the suite before reporting."


def _agent_event(seq=1, tuid=TUID, **extra):
    ev = {
        "seq": seq, "ts": "2026-11-01T10:00:00Z", "tool_name": "Agent",
        "tool_summary": json.dumps({"description": "d", "model": "sonnet", "prompt_bytes": 70},
                                   sort_keys=True, separators=(",", ":")),
        "tool_response_summary": json.dumps({"bytes": 100, "is_error": False},
                                            separators=(",", ":")),
        "tool_use_id": tuid,
    }
    ev.update(extra)
    return ev


def _child_event(seq=2, parent=TUID, sub=CHILD):
    return {"seq": seq, "ts": "2026-11-01T10:00:05Z", "tool_name": "Bash",
            "cwd": f"/work/data2/worktrees/proj/agent-{sub}", "tool_summary": "{}",
            "tool_response_summary": "{}", "parent_tool_use_id": parent, "subagent_id": sub}


def _transcript(path: Path, tuid=TUID, agent_id=CHILD, prompt=PROMPT) -> Path:
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tuid, "name": "Agent",
             "input": {"prompt": prompt, "isolation": "worktree", "model": "sonnet"}}]}},
        # streamed duplicate whose result is not a dict must not erase anything
        {"type": "user", "toolUseResult": None, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tuid, "content": "launched"}]}},
        {"type": "user", "toolUseResult": {"status": "async_launched", "agentId": agent_id},
         "message": {"content": [{"type": "tool_result", "tool_use_id": tuid, "content": "ok"}]}},
        {"type": "api-request-blob", "message": "noise"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    return path


def test_child_id_from_child_envelopes():
    d = parse_dispatches([_agent_event(), _child_event()], use_transcripts=False)[0]
    assert d.child_agent_id == CHILD
    assert d.child_source == "envelope"
    assert d.prompt == ""  # not recoverable from envelopes


def test_envelope_join_matches_worktree_outcome():
    events = [_agent_event(), _child_event()]
    cohort = group_cohorts(parse_dispatches(events, use_transcripts=False))[0]
    joined = join_cohort_to_children(cohort, derive_subagent_outcomes(events))
    assert joined[0].matched


def test_legacy_summary_agent_id_wins():
    ev = _agent_event(tool_response_summary=json.dumps({"agentId": "legacy1", "bytes": 5}))
    d = parse_dispatches([ev, _child_event()], use_transcripts=False)[0]
    assert (d.child_agent_id, d.child_source) == ("legacy1", "summary")


def test_unrelated_child_does_not_join():
    d = parse_dispatches([_agent_event(), _child_event(parent="toolu_other")],
                         use_transcripts=False)[0]
    assert d.child_agent_id == ""


def test_prompt_and_child_from_transcript(tmp_path):
    t = _transcript(tmp_path / "sess.jsonl")
    ev = _agent_event(transcript_path=str(t), session_id="sess")
    d = parse_dispatches([ev])[0]
    assert d.prompt == PROMPT and d.prompt_source == "transcript"
    assert d.child_agent_id == CHILD and d.child_source == "transcript"
    assert d.isolation == "worktree"


def test_transcript_missing_or_other_id_is_empty(tmp_path):
    ev = _agent_event(transcript_path=str(tmp_path / "gone.jsonl"), session_id="s")
    d = parse_dispatches([ev])[0]
    assert d.prompt == "" and d.child_agent_id == ""
    t = _transcript(tmp_path / "s2.jsonl", tuid="toolu_elsewhere")
    d = parse_dispatches([_agent_event(transcript_path=str(t), session_id="s2")])[0]
    assert d.prompt == ""


def test_transcript_parser_merges_duplicates():
    rows = [json.dumps({"type": "user", "toolUseResult": {"agentId": "x1", "prompt": "p"},
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "t"}]}})] * 2
    assert parse_transcript_dispatches(rows) == {"t": {"agentId": "x1", "prompt": "p"}}


def _store(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE run_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, "
                 "seq INTEGER, event_ts TEXT, event_type TEXT, raw_json TEXT)")
    for run_id, seq, data in rows:
        conn.execute("INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) "
                     "VALUES (?,?,?,?,?)",
                     (run_id, seq, data.get("ts", "2026-11-01T10:00:00Z"), "activity",
                      json.dumps({"data": data})))
    return conn


def test_loaders_pass_lineage_fields_and_join_across_runs(tmp_path):
    t = _transcript(tmp_path / "sess.jsonl")
    agent = {k: v for k, v in _agent_event().items() if k not in ("seq",)}
    agent.update(transcript_path=str(t), session_id="sess")
    child = {k: v for k, v in _child_event().items() if k not in ("seq",)}
    conn = _store([("run-a", 1, agent), ("run-b", 1, child)])
    assert load_run_events(conn, "run-a")[0]["tool_use_id"] == TUID
    assert load_run_events(conn, "run-b")[0]["parent_tool_use_id"] == TUID
    pooled = load_session_events(conn, ["run-a", "run-b"])
    d = parse_dispatches(pooled, use_transcripts=False)[0]
    assert d.child_agent_id == CHILD and d.child_source == "envelope"
    # per-run list: no child envelopes, transcript still recovers both
    d = parse_dispatches(load_run_events(conn, "run-a"))[0]
    assert (d.child_agent_id, d.prompt) == (CHILD, PROMPT)
