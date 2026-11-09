"""token_report in the unit the plan bills: requests (shape, unbatched reads, denied calls)."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import token_ledger as tl  # noqa: E402
import token_report as tr  # noqa: E402

T0 = datetime.now(timezone.utc) - timedelta(hours=2)
SINCE = (T0 - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S")
GUARD = "This agent is isolated in the worktree /SEKRIT-PATH/wt. Run git from there."
SLEEP = "<tool_use_error>Blocked: sleep 60 followed by a poll. Use Monitor.</tool_use_error>"


def _ts(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _asst(mid, sec, blocks):
    return json.dumps({"type": "assistant", "timestamp": _ts(sec), "sessionId": "s1",
                       "agentId": "aLANE1", "cwd": "/home/u/data2/worktrees/proj/wt",
                       "message": {"id": mid, "model": "m1", "content": blocks,
                                   "usage": {"input_tokens": 1, "cache_read_input_tokens": 100_000,
                                             "cache_creation_input_tokens": 0, "output_tokens": 10}}})


def _use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _res(tid, sec, text="ok", error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": text}
    if error:
        block["is_error"] = True
    return json.dumps({"type": "user", "timestamp": _ts(sec), "message": {"content": [block]}})


def _text(mid, sec):
    return _asst(mid, sec, [{"type": "text", "text": "done"}])


def _turn(n, sec, *uses, results=None):
    """One assistant request carrying `uses` and the tool results that answer it."""
    lines = [_asst(f"m{n}", sec, list(uses))]
    for i, u in enumerate(uses):
        text, err = (results or {}).get(u["id"], ("ok", False))
        lines.append(_res(u["id"], sec + 1 + i, text, err))
    return lines


def _lane():
    """13 requests: a 3-long read chain, an edit, a read followed by a parallel
    pair of reads, an all-denied request, a half-denied one, text, a 2-long chain."""
    L = []
    L += _turn(1, 0, _use("r1", "Read", file_path="/a.rs"))
    L += _turn(2, 10, _use("r2", "Grep", pattern="x"))
    L += _turn(3, 20, _use("r3", "Bash", command="cat b.rs"))
    L += _turn(4, 30, _use("e1", "Edit", file_path="/a.rs", old_string="1", new_string="2"))
    L += _turn(5, 40, _use("r5", "Read", file_path="/c.rs"))
    L += _turn(6, 50, _use("r6a", "Read", file_path="/d.rs"), _use("r6b", "Read", file_path="/e.rs"))
    L += _turn(7, 60, _use("d7", "Bash", command="git push origin x"),
               results={"d7": (GUARD, True)})
    L += _turn(8, 70, _use("o8", "Bash", command="ls /tmp"), _use("d8", "Bash", command="sleep 60; ls"),
               results={"d8": (SLEEP, True)})
    L.append(_text("m9", 80))
    L += _turn(10, 90, _use("r10", "Read", file_path="/f.rs"))
    L += _turn(11, 100, _use("r11", "Read", file_path="/g.rs"))
    L.append(_text("m12", 110))
    L += _turn(13, 120, _use("c13", "Bash", command="git commit -m x"))
    return L


def _ledger(tmp_path, lines=None):
    root = tmp_path / "root" / "-proj" / "s1" / "subagents"
    root.mkdir(parents=True)
    (root / "agent-aLANE1.jsonl").write_text("\n".join(lines or _lane()) + "\n")
    (root / "agent-aLANE1.meta.json").write_text(json.dumps(
        {"agentType": "worker", "model": "sonnet", "description": "SECRET lane description"}))
    db = tmp_path / "tokens.db"
    tl.run(db, roots=[("claude", tmp_path / "root")])
    return sqlite3.connect(db), [("claude", tmp_path / "root")]


def test_request_shape_counts_requests_by_tool_calls(tmp_path):
    conn, roots = _ledger(tmp_path)
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    [src] = view["by_source"]
    assert (src["requests"], src["no_tool"], src["one_tool"], src["multi_tool"]) == (13, 2, 9, 2)
    assert view["totals"]["single_call_share"] == round(9 / 13, 3)


def test_unbatched_reads_count_requests_not_calls_and_say_upper_bound(tmp_path):
    conn, roots = _ledger(tmp_path)
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    unb = view["unbatched_reads"]
    # r2, r3 follow a read request; r6 (a pair) follows r5; r11 follows r10
    assert unb["requests"] == 4
    # per call, the pair's second read also "follows a read" though it is already parallel
    assert unb["read_calls_after_read"] == 5 and unb["of_which_in_another_request"] == 4
    assert unb["read_calls"] == 9
    chains = {c["requests_in_run"]: (c["runs"], c["absorbable_requests"]) for c in unb["chain_lengths"]}
    assert chains["2"] == (2, 2) and chains["3"] == (1, 2) and chains["4-5"] == (0, 0)
    w = {x["id"]: x for x in view["waste"]}
    assert w["unbatched_reads"]["requests"] == 4 and w["unbatched_reads"]["bound"].startswith("upper")
    assert w["unbatched_reads"]["turn_iet"] > 0


def test_a_text_turn_or_edit_breaks_a_read_chain(tmp_path):
    L = []
    L += _turn(1, 0, _use("r1", "Read", file_path="/a.rs"))
    L.append(_text("m2", 10))
    L += _turn(3, 20, _use("r3", "Read", file_path="/b.rs"))
    L += _turn(4, 30, _use("e4", "Edit", file_path="/a.rs", old_string="1", new_string="2"))
    L += _turn(5, 40, _use("r5", "Read", file_path="/c.rs"))
    conn, roots = _ledger(tmp_path, L)
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    assert view["unbatched_reads"]["requests"] == 0


def test_denied_calls_are_costed_in_requests_by_family(tmp_path):
    conn, roots = _ledger(tmp_path)
    view, by_session = tr.request_view(conn, SINCE, roots=roots)
    den = view["denied_calls"]
    assert den["calls"] == 2 and den["calls_matched_to_requests"] == 2
    # d7 is a request that did nothing; o8 succeeded beside the blocked sleep
    assert (den["requests_all_denied"], den["requests_some_denied"]) == (1, 2)
    by_class = {c["class"]: (c["calls"], c["requests_all_denied"]) for c in den["by_class"]}
    assert by_class == {"worktree_guard": (1, 1), "blocked_sleep": (1, 0)}
    assert den["scan"]["scanned"] == 1 and den["scan"]["missing"] == 0
    assert {w["id"]: w["requests"] for w in view["waste"]}["denied_calls"] == 1
    assert sorted(v["denied_calls"] for v in by_session.values()) == [2]


def test_a_transcript_that_is_gone_is_reported_missing_not_skipped(tmp_path):
    conn, _ = _ledger(tmp_path)
    view, _ = tr.request_view(conn, SINCE, roots=[("claude", tmp_path / "nowhere")])
    den = view["denied_calls"]
    assert den["scan"]["sessions"] == 1 and den["scan"]["scanned"] == 0 and den["scan"]["missing"] == 1
    assert den["calls"] == 0 and den["available"] is True


def test_rerun_requests_come_from_the_rerun_ids_handed_in(tmp_path):
    conn, roots = _ledger(tmp_path)
    view, _ = tr.request_view(conn, SINCE, roots=roots, rerun_ids={"r3", "r10"})
    assert {w["id"]: w["requests"] for w in view["waste"]}["redundant_reruns"] == 2


def test_requests_per_agent_tier_project_and_commit(tmp_path):
    conn, roots = _ledger(tmp_path)
    view, _ = tr.request_view(conn, SINCE, roots=roots)
    [agent] = view["by_agent_tier"]
    assert (agent["agent"], agent["requested_tier"]) == ("worker", "sonnet")
    assert (agent["requests"], agent["commits"], agent["requests_per_commit"]) == (13, 1, 13.0)
    assert (agent["unbatched_read_requests"], agent["denied_requests"]) == (4, 1)
    [proj] = view["by_project"]
    assert proj["project"] == "proj" and proj["denied_calls"] == 2
    assert view["totals"]["requests_per_commit"] == 13.0


def test_lanes_show_the_requested_tier_and_request_columns(tmp_path):
    conn, roots = _ledger(tmp_path)
    _, by_session = tr.request_view(conn, SINCE, roots=roots)
    [lane] = tr.lanes(conn, SINCE, per_session=by_session)
    assert lane["agent_type"] == "worker" and lane["agent_model"] == "sonnet"
    assert (lane["turns"], lane["single_call_requests"], lane["commits"]) == (13, 9, 1)
    assert (lane["unbatched_read_requests"], lane["denied_calls"]) == (4, 2)
    table = "\n".join(tr.lanes_table([lane]))
    assert "| tier |" in table and "| sonnet |" in table


def test_digest_ranks_requests_and_stays_sanitized(tmp_path):
    conn, roots = _ledger(tmp_path)
    rep = tr.build(conn, days=1, roots=roots)
    cands = {c["id"]: c for c in rep["candidates"]}
    assert cands["unbatched_reads"]["requests"] == 4 and cands["denied_calls"]["requests"] == 1
    assert cands["unbatched_reads"]["iet"] > 0 and cands["denied_calls"]["iet"] > 0
    counts = [w["requests"] for w in rep["requests"]["waste"]]
    assert counts == sorted(counts, reverse=True)
    assert rep["requests"]["top_lanes"][0]["turns"] == 13
    md = tr.to_markdown(rep)
    assert "## Requests (the plan bills per request)" in md and "### Request waste (ranked by requests)" in md
    blob = md + json.dumps(rep)
    for leak in ("SEKRIT", "Run git from there", "followed by a poll", "cat b.rs", "SECRET"):
        assert leak not in blob


def test_a_window_without_recorded_tool_calls_has_no_request_section(tmp_path):
    conn, roots = _ledger(tmp_path)
    conn.execute("DELETE FROM tool_calls")
    rep = tr.build(conn, days=1, roots=roots)
    assert "requests" not in rep
    assert "## Requests" not in tr.to_markdown(rep)
    assert {"unbatched_reads", "denied_calls"} <= {c["id"] for c in rep["candidates"]}


def test_build_finds_reruns_in_requests(tmp_path):
    L = []
    L += _turn(1, 0, _use("t1", "Bash", command="cargo test -p x"))
    L += _turn(2, 10, _use("t2", "Bash", command="cargo test -p x"))
    L += _turn(3, 20, _use("e3", "Edit", file_path="/a.rs", old_string="1", new_string="2"))
    L += _turn(4, 30, _use("t4", "Bash", command="cargo test -p x"))
    conn, roots = _ledger(tmp_path, L)
    rep = tr.build(conn, days=1, roots=roots)
    rr = {c["id"]: c for c in rep["candidates"]}["redundant_reruns"]
    assert rr["requests"] == 1
    assert {w["id"]: w["requests"] for w in rep["requests"]["waste"]}["redundant_reruns"] == 1


def test_denial_families():
    cls = tr.classify_denial
    assert cls(GUARD) == "worktree_guard"
    assert cls(SLEEP) == "blocked_sleep"
    assert cls("PreToolUse:Bash hook error: [guard.py]: nope") == "hook_blocked"
    assert cls("Permission for this action was denied by the user") == "permission_denied"
    assert cls("<tool_use_error>Error: No such tool available: bash</tool_use_error>") == "tool_unavailable"
    assert cls("<tool_use_error>File has not been read yet</tool_use_error>") == "tool_use_error"
    assert cls("something else entirely") == "other" and cls(None) == "other"
