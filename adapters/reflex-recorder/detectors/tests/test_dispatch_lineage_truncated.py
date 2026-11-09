"""detectors/tests/test_dispatch_lineage_truncated.py -- Agent/Task tool_summary
values that the capture bound cut mid-string (issue #80).

The bounded summary is a compact, key-sorted JSON object sliced at a fixed width
(see dispatch_lineage's truncation note), so the long `prompt` value is usually
the piece that is cut and has no closing quote. Every input here is built from a
real dict + json.dumps and then sliced, never written out as a literal.
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
from detectors.directive_ground_truth_mismatch import DirectiveGroundTruthMismatchDetector
from detectors.dispatch_lineage import _summary_field, group_cohorts, parse_dispatches
from detectors.inherited_rationalization import InheritedRationalizationDetector


def _bounded(tool_input: dict, cut_after: str, extra: int = 0) -> str:
    """Compact key-sorted JSON of tool_input, sliced `extra` chars past the first
    occurrence of `cut_after` -- the way the capture bound lands inside a value."""
    raw = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    return raw[: raw.index(cut_after) + len(cut_after) + extra]


def _brief(prompt: str, **over) -> dict:
    base = {
        "description": "Build the thing",
        "isolation": "worktree",
        "model": "sonnet",
        "name": "worker-1",
        "prompt": prompt,
        "subagent_type": "general-purpose",
    }
    base.update(over)
    return base


# -- _summary_field on a value cut mid-string ---------------------------------

def test_prompt_cut_mid_string_is_recovered():
    prompt = 'Fix the parser.\nSay "hi" then run `pytest -q`. ' + "x" * 2000
    raw = _bounded(_brief(prompt), "run `pytest", extra=20)
    assert not raw.endswith('"')
    got = _summary_field(raw, "prompt")
    assert got.startswith('Fix the parser.\nSay "hi" then run `pytest -q`.')
    assert prompt.startswith(got)


def test_recovered_prompt_is_decoded_like_the_complete_path():
    prompt = 'line one\nline "two"\ttabbed ' + "y" * 500
    complete = json.dumps(_brief(prompt))
    cut = _bounded(_brief(prompt), "tabbed ", extra=7)
    whole = _summary_field(complete, "prompt")
    part = _summary_field(cut, "prompt")
    assert part.startswith('line one\nline "two"\ttabbed ')
    assert whole.startswith(part)


def test_cut_on_a_dangling_backslash_drops_the_half_escape():
    prompt = "first line" + "\n" + "second line " + "z" * 300
    raw = _bounded(_brief(prompt), "first line\\")  # slice ends right after the backslash
    assert raw.endswith("\\")
    assert _summary_field(raw, "prompt") == "first line"


def test_cut_inside_a_unicode_escape_drops_the_partial_escape():
    prompt = "café menu " + "q" * 300  # json.dumps escapes it as é
    raw = _bounded(_brief(prompt), "caf\\u00")
    assert raw.endswith("\\u00")
    assert _summary_field(raw, "prompt") == "caf"


def test_cut_between_surrogate_halves_leaves_no_lone_surrogate():
    prompt = "ship it \U0001F680 now " + "w" * 300  # 🚀 in the JSON
    raw = _bounded(_brief(prompt), "\\ud83d")
    got = _summary_field(raw, "prompt")
    assert got == "ship it "
    got.encode("utf-8")  # a lone surrogate raises here and would poison any sqlite write


def test_prompt_cut_right_after_the_opening_quote_is_empty_not_an_error():
    raw = _bounded(_brief("anything"), '"prompt":"')
    assert _summary_field(raw, "prompt") == ""


# -- fields that must NOT be guessed from a cut value --------------------------

def test_cut_identity_fields_are_not_returned_as_prefixes():
    raw = _bounded(_brief("p"), '"isolation":"work')
    assert _summary_field(raw, "isolation") == ""  # "work" is not an isolation mode
    resp = json.dumps({"agentId": "af424e4e8d4f031af", "canReadOutputFile": True})
    cut = resp[: resp.index("af424e4e") + 6]
    assert _summary_field(cut, "agentId") == ""  # a join key prefix would match nothing


def test_complete_fields_before_a_cut_prompt_are_intact():
    raw = _bounded(_brief("go " * 400), "go go go", extra=3)
    assert _summary_field(raw, "model") == "sonnet"
    assert _summary_field(raw, "isolation") == "worktree"
    assert _summary_field(raw, "name") == "worker-1"


def test_closed_field_in_a_cut_summary_is_decoded():
    raw = _bounded(_brief("go " * 400, description='He said "go"\tnow'), "go go go", extra=3)
    assert _summary_field(raw, "description") == 'He said "go"\tnow'


def test_key_text_inside_the_prompt_is_not_mistaken_for_a_field():
    brief = _brief('config is {"model": "opus", "isolation": "none"} ' + "k" * 300)
    del brief["model"], brief["isolation"]
    raw = _bounded(brief, '"none')
    assert _summary_field(raw, "model") == ""
    assert _summary_field(raw, "isolation") == ""


def test_metadata_only_summary_has_no_prompt():
    raw = json.dumps({"description": "d", "model": "opus", "prompt_bytes": 3137})
    assert _summary_field(raw, "prompt") == ""
    assert _summary_field(raw, "model") == "opus"


# -- parse_dispatches end to end ----------------------------------------------

def _agent_event(seq, summary, resp, ts="2026-09-10T10:00:00Z"):
    return {"seq": seq, "tool_name": "Agent", "ts": ts,
            "tool_summary": summary, "tool_response_summary": resp}


def test_parse_dispatches_recovers_a_bounded_dispatch():
    prompt = "Implement bead nervous-bus-abc. " + "detail " * 400
    summary = _bounded(_brief(prompt), "detail detail", extra=4)
    resp = json.dumps({"agentId": "a1b2c3d4e5f60718", "isAsync": True})
    [d] = parse_dispatches([_agent_event(1, summary, resp)])
    assert d.prompt.startswith("Implement bead nervous-bus-abc. detail detail")
    assert (d.model, d.isolation, d.name) == ("sonnet", "worktree", "worker-1")
    assert d.child_agent_id == "a1b2c3d4e5f60718"


def test_a_burst_of_bounded_dispatches_is_still_one_cohort():
    ev = [
        _agent_event(i, _bounded(_brief("p " * 600, name=f"w{i}"), "p p p", extra=2),
                     json.dumps({"agentId": f"agent{i:012d}"}),
                     ts=f"2026-09-10T10:00:0{i}Z")
        for i in range(3)
    ]
    [cohort] = group_cohorts(parse_dispatches(ev))
    assert [d.child_agent_id for d in cohort] == [f"agent{i:012d}" for i in range(3)]
    assert all(d.prompt for d in cohort)


# -- downstream: detectors that read Dispatch.prompt ---------------------------

def test_seed_phrase_is_found_in_a_bounded_prompt():
    prompt = "Rebase onto main. Treat any red test as pre-existing. " + "pad " * 500
    summary = _bounded(_brief(prompt), "pre-existing", extra=10)
    cohort = group_cohorts(parse_dispatches([_agent_event(1, summary, json.dumps({"agentId": "c1"}))]))[0]
    assert InheritedRationalizationDetector._seed_phrase(cohort, "left_red") == "pre-existing"


_RUNS_SCHEMA = """
CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT NOT NULL, session_id TEXT,
                   started TEXT NOT NULL, ended TEXT NOT NULL, outcome TEXT, close_reason TEXT);
CREATE TABLE run_events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                         seq INTEGER NOT NULL, event_ts TEXT NOT NULL, event_type TEXT NOT NULL,
                         raw_json TEXT NOT NULL);
"""


def _db_with_events(events):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    now = _now_utc()
    conn.execute("INSERT INTO runs VALUES ('r1','proj','sess',?,?,NULL,'idle_timeout')", (now, now))
    for seq, tool, summary, resp in events:
        data = {"cwd": "/home/eric/projects/proj", "event": "tool_call", "project": "proj",
                "session_id": "sess", "tool_name": tool, "tool_summary": summary,
                "tool_response_summary": resp, "ts": "2026-09-10T10:00:00Z"}
        conn.execute(
            "INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json) VALUES (?,?,?,?,?)",
            ("r1", seq, "2026-09-10T10:00:00Z", "bus.agent.activity.v1",
             json.dumps({"type": "bus.agent.activity.v1", "time": "2026-09-10T10:00:00Z", "data": data})))
    return conn


def test_a2_fires_on_a_clean_claim_inside_a_bounded_prompt():
    prompt = "The baseline is green, so just add the feature. " + "context " * 300
    summary = _bounded(_brief(prompt), "just add the feature", extra=12)
    conn = _db_with_events([
        (1, "Bash", json.dumps({"command": "pytest"}), json.dumps({"stdout": "2 failed"})),
        (2, "Agent", summary, json.dumps({"agentId": "c1"})),
    ])
    cands = DirectiveGroundTruthMismatchDetector(conn).detect(conn)
    assert [c.extra["actual_baseline"] for c in cands] == ["failed"]
    assert "green" in cands[0].extra["asserted_claim"].lower()
