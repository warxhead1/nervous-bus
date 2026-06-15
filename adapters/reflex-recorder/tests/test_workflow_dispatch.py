"""test_workflow_dispatch.py — workflow-agent dispatch reconstruction.

Hermetic: builds a synthetic ~/.claude/projects/**/subagents/workflows/wf_*/ tree
mirroring the real on-disk shape (journal.jsonl 'started' entries + agent-<hex>.jsonl
line-0 prompt + agent-<hex>.meta.json) and asserts the ingester reconstructs one
dispatch record per started agent with the full prompt bounded correctly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REC))

import workflow_dispatch as wd  # noqa: E402


def _mk_run(root: Path, proj: str, session: str, wf: str, agents: list[dict]) -> Path:
    wf_dir = root / proj / session / "subagents" / "workflows" / wf
    wf_dir.mkdir(parents=True, exist_ok=True)
    journal = wf_dir / "journal.jsonl"
    lines = []
    for a in agents:
        if a.get("started", True):
            lines.append(json.dumps({"type": "started", "agentId": a["id"]}))
        # encoded dir name like '-home-eric-projects-tengine' -> real cwd '/home/eric/projects/tengine'
        default_cwd = "/home/eric/projects/" + proj.split("-projects-")[-1]
        line0 = {
            "agentId": a["id"], "type": "user", "timestamp": a.get("ts", "2026-06-14T16:48:00Z"),
            "sessionId": session, "cwd": a.get("cwd", default_cwd),
            "gitBranch": a.get("branch", "main"), "slug": a.get("slug", "happy-slug"),
            "message": {"role": "user", "content": a["prompt"]},
        }
        (wf_dir / f"agent-{a['id']}.jsonl").write_text(json.dumps(line0) + "\n")
        (wf_dir / f"agent-{a['id']}.meta.json").write_text(
            json.dumps({"agentType": a.get("agent_type", "workflow-subagent")}))
    journal.write_text("\n".join(lines) + ("\n" if lines else ""))
    return wf_dir


def test_reconstructs_one_record_per_started_agent(tmp_path):
    _mk_run(tmp_path, "-home-eric-projects-tengine", "sess-1", "wf_abc-1", [
        {"id": "a1", "prompt": "Do the camera fix.", "slug": "calm-otter"},
        {"id": "a2", "prompt": "Do the entity fix.", "slug": "calm-otter"},
    ])
    recs = wd.scan(tmp_path)
    assert len(recs) == 2
    ids = {r.agent_id for r in recs}
    assert ids == {"a1", "a2"}
    r = next(r for r in recs if r.agent_id == "a1")
    assert r.wf_run_id == "wf_abc-1"
    assert r.prompt == "Do the camera fix."
    assert r.prompt_chars == len("Do the camera fix.")
    assert r.slug == "calm-otter"           # per-run slug, shared
    assert r.agent_type == "workflow-subagent"
    assert r.project == "tengine"            # derived from cwd
    assert r.parent_session_id == "sess-1"


def test_prompt_is_bounded_but_full_length_recorded(tmp_path):
    big = "X" * 5000
    _mk_run(tmp_path, "-home-eric-projects-kb", "s", "wf_big-1",
            [{"id": "b1", "prompt": big}])
    recs = wd.scan(tmp_path, bound=1000)
    assert len(recs) == 1
    assert len(recs[0].prompt) == 1000          # bounded for transport
    assert recs[0].prompt_chars == 5000          # full length preserved
    # the join helper returns the LOSSLESS prompt from disk
    assert len(wd.join_full_prompt(recs[0].transcript_path)) == 5000


def test_agent_not_in_journal_is_skipped(tmp_path):
    """A stray agent transcript with no 'started' journal entry is not a dispatch."""
    _mk_run(tmp_path, "-home-eric-projects-x", "s", "wf_x-1", [
        {"id": "real", "prompt": "real work", "started": True},
        {"id": "stray", "prompt": "stray", "started": False},
    ])
    recs = wd.scan(tmp_path)
    assert {r.agent_id for r in recs} == {"real"}


def test_run_filter(tmp_path):
    _mk_run(tmp_path, "-home-eric-projects-x", "s", "wf_keep-1", [{"id": "k", "prompt": "k"}])
    _mk_run(tmp_path, "-home-eric-projects-x", "s", "wf_drop-1", [{"id": "d", "prompt": "d"}])
    recs = wd.scan(tmp_path, run_filter="keep")
    assert {r.wf_run_id for r in recs} == {"wf_keep-1"}


def test_event_payload_matches_schema_required(tmp_path):
    _mk_run(tmp_path, "-home-eric-projects-x", "s", "wf_e-1", [{"id": "e1", "prompt": "hi"}])
    ev = wd.scan(tmp_path)[0].to_event()
    for req in ("ts", "wf_run_id", "agent_id", "prompt"):
        assert req in ev and ev[req] != ""
    assert ev["event"] == "workflow_agent_dispatch"
