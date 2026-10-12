"""tests/test_champion.py — champion.py (MASS Phase 0b/0c). Synthetic fixtures only."""
from __future__ import annotations

import json
import sqlite3
import threading
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import champion
from champion import (ENFORCED, PROSE, revise_cmd, build_bundle, build_pairs, extract, extract_features, lift,
                      pairs_cohort, pairs_recipe, pairs_retry, project_from_cwd)

FIX = Path(__file__).parent / "fixtures" / "champion"


# ── features ────────────────────────────────────────────────────────────────

def test_features_rich_brief():
    f = extract_features((FIX / "brief_rich.txt").read_text())
    for name in ("has_acceptance_cmd", "has_file_scope", "isolation_worktree", "model_explicit",
                 "has_relay_hop", "has_base_branch", "has_receipts_clause", "has_exhaustion_clause",
                 "names_report_path", "names_scratch_dir"):
        assert f[name] is True, name
    assert f["caps_count"] >= 3  # FILE, SCOPE, FIRST, DO, NOT
    assert f["length_chars"] == len((FIX / "brief_rich.txt").read_text())


def test_features_bare_brief_all_false():
    f = extract_features((FIX / "brief_bare.txt").read_text())
    for name in ENFORCED + PROSE:
        if name in ("caps_count", "length_chars"):
            continue
        assert f[name] is False, name
    assert f["caps_count"] == 0


def test_caps_ignores_acronym_stoplist():
    assert extract_features("use JSON over HTTP")["caps_count"] == 0
    assert extract_features("NEVER do this, ALWAYS that")["caps_count"] == 2


def test_acceptance_needs_runnable_command():
    assert not extract_features("Acceptance: it should work well")["has_acceptance_cmd"]
    assert extract_features("Verify with `cargo test -p foo` before done")["has_acceptance_cmd"]


def test_structured_meta_ors_with_prose_and_records_arm():
    f = extract_features("do the thing", {"gates": ["cargo check"], "scope": ["a.rs"], "model": "sonnet"},
                         cwd="/home/eric/data2/worktrees/p/agent-1")
    assert f["has_acceptance_cmd"] and f["has_file_scope"] and f["model_explicit"] and f["isolation_worktree"]
    assert f["_fired"]["has_file_scope"] == ["structured"]
    both = extract_features("FILE SCOPE: a.rs", {"scope": ["a.rs"]})
    assert both["_fired"]["has_file_scope"] == ["prose", "structured"]


def test_project_from_cwd():
    assert project_from_cwd("/home/eric/data2/worktrees/hearth/agent-1") == "hearth"
    assert project_from_cwd("/home/eric/projects/kb") == "kb"
    assert project_from_cwd("/home/eric/data2/orca/nervous-bus") == "nervous-bus"
    assert project_from_cwd("") == "unknown"


# ── synthetic stores ────────────────────────────────────────────────────────

def _make_runs_db(path: Path, runs: list[dict], hits: list[tuple] = ()):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE runs (run_id TEXT PRIMARY KEY, run_key TEXT, project TEXT, agent_kind TEXT,
        worktree TEXT, worktree_slug TEXT, git_branch TEXT, outcome TEXT, label_history TEXT, started TEXT);
    CREATE TABLE detector_hits (id INTEGER PRIMARY KEY, run_id TEXT, detector TEXT, signature TEXT,
        project TEXT, ts TEXT);
    CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER, event_ts TEXT,
        event_type TEXT, raw_json TEXT);
    """)
    for r in runs:
        hist = json.dumps([{"source": r["source"]}]) if r.get("source") else "[]"
        conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (r["run_id"], r["run_key"], r.get("project", "proj"), r.get("agent_kind", "host_subagent"),
                      r.get("worktree"), r.get("slug"), r.get("branch"), r.get("outcome"), hist,
                      r.get("started", "2026-10-01T00:00:00Z")))
    for run_id, det, sig in hits:
        conn.execute("INSERT INTO detector_hits (run_id,detector,signature,project,ts) VALUES (?,?,?,?,?)",
                     (run_id, det, sig, "proj", "2026-10-01"))
    conn.commit()
    conn.close()


def _make_orca_db(path: Path, tasks, dispatches, done):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE tasks (id TEXT PRIMARY KEY, task_title TEXT, spec TEXT, status TEXT, created_at TEXT);
    CREATE TABLE dispatch_contexts (id TEXT PRIMARY KEY, task_id TEXT, retry_of_dispatch_id TEXT,
        status TEXT, created_at TEXT);
    CREATE TABLE messages (id TEXT, type TEXT, payload TEXT);
    """)
    for t in tasks:
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?)", t)
    for d in dispatches:
        conn.execute("INSERT INTO dispatch_contexts VALUES (?,?,?,?,?)", d)
    for i, (disp, outcome) in enumerate(done):
        conn.execute("INSERT INTO messages VALUES (?,?,?)",
                     (f"m{i}", "worker_done", json.dumps({"dispatchId": disp, "outcome": outcome})))
    conn.commit()
    conn.close()


def _write_subagent(root: Path, proj: str, session: str, agent_id: str, text: str, ts: str,
                    cwd: str = "/home/eric/data2/worktrees/proj/agent-x", extra_lines=()):
    d = root / proj / session / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"agent-{agent_id}.jsonl"
    line0 = {"agentId": agent_id, "sessionId": session, "timestamp": ts, "cwd": cwd, "gitBranch": "b",
             "type": "user", "message": {"role": "user", "content": text}}
    p.write_text("\n".join([json.dumps(line0), *map(json.dumps, extra_lines)]) + "\n")
    return p


def _ns(tmp: Path, **kw) -> Namespace:
    base = dict(transcripts_root=tmp / "transcripts", projects_root=tmp / "projects",
                runs_db=tmp / "runs.db", orca_db=tmp / "orca.db", mmx_root=tmp / "mmx",
                since_days=None, project=None, out=tmp / "out" / "briefs.jsonl")
    base.update(kw)
    return Namespace(**base)


def _rows(path: Path):
    return [json.loads(l) for l in Path(path).read_text().splitlines()]


# ── extract + join ──────────────────────────────────────────────────────────

def test_extract_joins_label_tier_and_detectors(tmp_path):
    tr = tmp_path / "transcripts"
    _write_subagent(tr, "-p", "sess1", "aaa1", "Brief one. FILE SCOPE: a.py", "2026-10-01T10:00:00Z")
    _write_subagent(tr, "-p", "sess1", "bbb2", "Brief two", "2026-10-01T10:00:30Z")
    _write_subagent(tr, "-p", "sess1", "ccc3", "Brief three", "2026-10-01T10:01:00Z")
    _make_runs_db(tmp_path / "runs.db", [
        {"run_id": "R1", "run_key": "sess1#agent-aaa1", "outcome": "landed", "source": "git_merged_into_main"},
        {"run_id": "R2", "run_key": "sess1#agent-bbb2", "outcome": "clean", "source": "behavior_inference"},
        {"run_id": "R3", "run_key": "sess1#agent-ccc3", "outcome": None},
    ], hits=[("R1", "unverified_completion", "s"), ("R1", "failure_taxonomy", "planning_failure")])
    # meta fallback under projects_root (mirror has no meta)
    mp = tmp_path / "projects" / "-p" / "sess1" / "subagents"
    mp.mkdir(parents=True)
    (mp / "agent-aaa1.meta.json").write_text(json.dumps({"agentType": "sonnet-worker", "model": "sonnet"}))
    res = extract(_ns(tmp_path))
    assert res["briefs"] == 3
    rows = {r["source_id"]: r for r in _rows(tmp_path / "out" / "briefs.jsonl")}
    a, b, c = rows["aaa1"], rows["bbb2"], rows["ccc3"]
    assert (a["label_source"], a["label_tier"], a["explicit"], a["positive"]) == ("git_merged_into_main", 3, True, True)
    assert a["detectors"] == ["unverified_completion"] and a["failure_taxonomy"] == ["planning_failure"]
    assert a["agent_type"] == "sonnet-worker" and a["features"]["model_explicit"]
    assert (b["label_tier"], b["explicit"]) == (1, False)  # inferred never counts as explicit
    assert (c["label_tier"], c["explicit"], c["run_id"]) == (0, False, "R3")
    assert len(a["brief_sha256"]) == 64 and a["project"] == "proj"


def test_extract_unjoined_brief_has_no_label(tmp_path):
    _write_subagent(tmp_path / "transcripts", "-p", "s", "zzz", "x", "2026-10-01T00:00:00Z")
    _make_runs_db(tmp_path / "runs.db", [])
    extract(_ns(tmp_path))
    r = _rows(tmp_path / "out" / "briefs.jsonl")[0]
    assert r["run_id"] is None and r["explicit"] is False and r["label_tier"] == 0


def test_extract_since_days_and_project_filters(tmp_path):
    tr = tmp_path / "transcripts"
    _write_subagent(tr, "-p", "s", "old1", "x", "2020-01-01T00:00:00Z")
    _write_subagent(tr, "-p", "s", "new1", "y", "2999-01-01T00:00:00Z", cwd="/home/eric/data2/worktrees/keep/a")
    _make_runs_db(tmp_path / "runs.db", [])
    assert extract(_ns(tmp_path, since_days=30))["briefs"] == 1
    assert extract(_ns(tmp_path, project="nope"))["briefs"] == 0
    assert extract(_ns(tmp_path, project="keep"))["briefs"] == 1


def test_extract_orca_positive_only_semantics(tmp_path):
    _make_orca_db(tmp_path / "orca.db",
                  tasks=[("t1", "a", "spec ok", "completed", "2026-10-01 00:00:00"),
                         ("t2", "b", "spec failed", "failed", "2026-10-01 00:00:00")],
                  dispatches=[("d1", "t1", None, "completed", "x"), ("d2", "t2", None, "failed", "x")],
                  done=[("d1", "succeeded"), ("d2", "failed")])
    _make_runs_db(tmp_path / "runs.db", [])
    extract(_ns(tmp_path))
    rows = {r["source_id"]: r for r in _rows(tmp_path / "out" / "briefs.jsonl")}
    assert rows["t1"]["explicit"] and rows["t1"]["positive"] and rows["t1"]["label_source"] == "orca_worker_done"
    assert not rows["t2"]["explicit"] and rows["t2"]["positive"] is None  # failed -> abstain


def test_extract_mmx_recipe_joins_by_branch_and_uses_meta(tmp_path):
    d = tmp_path / "mmx" / "mmxd-1-x"
    d.mkdir(parents=True)
    (d / "brief.md").write_text("recipe brief")
    (d / "meta.json").write_text(json.dumps({"branch": "mmx/mmxd-1-x", "worktree": "/w/.mmx-worktrees/mmxd-1-x",
                                              "gates": ["cargo check"], "scope": ["a.rs"], "started_at": "2026-10-01T00:00:00",
                                              "title": "T"}))
    _make_runs_db(tmp_path / "runs.db", [{"run_id": "M1", "run_key": "k", "branch": "mmx/mmxd-1-x",
                                          "outcome": "landed", "source": "pr_merge"}])
    extract(_ns(tmp_path))
    r = _rows(tmp_path / "out" / "briefs.jsonl")[0]
    assert r["source"] == "mmx_recipe" and r["run_id"] == "M1" and r["explicit"]
    assert r["features"]["has_acceptance_cmd"] and r["features"]["has_file_scope"]


def test_missing_sources_do_not_crash(tmp_path):
    res = extract(_ns(tmp_path))
    assert res["briefs"] == 0


# ── pairs ───────────────────────────────────────────────────────────────────

def _row(bid, src="claude_subagent", outcome="landed", label_src="pr_merge", text="alpha beta gamma delta epsilon zeta eta theta iota kappa",
         session="S", ts="2026-10-01T10:00:00Z", project="p", kind="host_subagent", agent_type="w"):
    tier = champion._source_tier(label_src) if outcome else 0
    return {"brief_id": bid, "source": src, "source_id": bid, "brief_text": text,
            "brief_sha256": champion.sha256(text), "project": project, "agent_kind": kind,
            "agent_type": agent_type, "ts": ts, "session_id": session, "run_id": "run-" + bid,
            "outcome": outcome, "label_source": label_src, "label_tier": tier,
            "explicit": bool(outcome) and tier >= 2,
            "positive": (outcome in champion.POSITIVE) if outcome else None, "title": ""}


def test_cohort_requires_explicit_opposite_same_template_and_window():
    ok_a, ok_b = _row("a"), _row("b", outcome="abandoned", label_src="git_branch_gone", ts="2026-10-01T10:00:30Z")
    same_polarity = _row("c", ts="2026-10-01T10:00:40Z")
    inferred = _row("d", outcome="abandoned", label_src="behavior_inference", ts="2026-10-01T10:00:50Z")
    far = _row("e", outcome="abandoned", label_src="git_branch_gone", ts="2026-10-01T12:00:00Z")
    other_tmpl = _row("f", outcome="abandoned", label_src="git_branch_gone", text="totally different words here " * 5,
                      ts="2026-10-01T10:00:45Z")
    ps = pairs_cohort([ok_a, ok_b, same_polarity, inferred, far, other_tmpl])
    assert {frozenset((p["a"], p["b"])) for p in ps} == {frozenset(("a", "b")), frozenset(("b", "c"))}
    assert all(p["a_label_source"] != "behavior_inference" and p["b_label_source"] != "behavior_inference" for p in ps)


def test_recipe_pairs_group_by_family_and_need_opposite():
    a = _row("a", src="mmx_session", project="auto-x-run-19-20260912T1130")
    b = _row("b", src="mmx_session", outcome="abandoned", label_src="git_branch_gone", project="auto-x-run-20-20260913T1130")
    c = _row("c", src="mmx_session", project="other-thing")
    ps = pairs_recipe([a, b, c])
    assert [frozenset((p["a"], p["b"])) for p in ps] == [frozenset(("a", "b"))]


def test_similar_uses_threshold_and_excludes_covered_pairs():
    a = _row("a", session="S1")
    b = _row("b", outcome="abandoned", label_src="git_branch_gone", session="S2")
    c = _row("c", outcome="abandoned", label_src="git_branch_gone", session="S3",
             text="nothing in common whatsoever with the others here ok fine")
    res, _ = build_pairs([a, b, c], "similar")
    assert [frozenset((p["a"], p["b"])) for p in res["similar"]] == [frozenset(("a", "b"))]
    assert res["similar"][0]["similarity"] >= 0.8


def test_similar_excludes_orca_and_inferred():
    a = _row("a", src="orca_task", label_src="orca_worker_done", outcome="clean")
    a["orca"] = {"attempts": []}
    b = _row("b", outcome="abandoned", label_src="behavior_inference")
    res, _ = build_pairs([a, b], "similar")
    assert res["similar"] == []


def test_retry_pairs_follow_orca_semantics_and_report_raw_opposites():
    att = [{"dispatch_id": "d1", "retry_of": None, "raw_outcome": "failed", "status": "failed"},
           {"dispatch_id": "d2", "retry_of": "d1", "raw_outcome": "succeeded", "status": "completed"}]
    row = _row("t", src="orca_task", label_src="orca_worker_done", outcome="clean")
    row["orca"] = {"attempts": att}
    pairs, diag = pairs_retry([row])
    assert pairs == []  # failed abstains: no explicit opposite exists
    assert diag["retry_flagged_dispatches"] == 1 and diag["retry_raw_opposite"] == 1
    assert diag["retry_both_explicit"] == 0
    multi = _row("m", src="orca_task")
    multi["orca"] = {"attempts": [{"dispatch_id": "x", "retry_of": None, "raw_outcome": None},
                                  {"dispatch_id": "y", "retry_of": None, "raw_outcome": None}]}
    assert pairs_retry([multi])[1]["tasks_multi_dispatch_not_flagged"] == 1


def test_priority_dedup_across_populations():
    a = _row("a", src="mmx_session", project="auto-x-run-1")
    b = _row("b", src="mmx_session", outcome="abandoned", label_src="git_branch_gone", project="auto-x-run-2")
    res, _ = build_pairs([a, b], "all")
    assert len(res["recipe"]) == 1 and res["similar"] == []


# ── bundle ──────────────────────────────────────────────────────────────────

def test_bundle_caps_and_evidence(tmp_path):
    long_text = "z" * 400_000
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "u1", "name": "Bash",
                                                       "input": {"command": "python3 -m pytest -q"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "u1",
                                                  "content": "Exit code 2\nFAILED"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}},
    ]
    transcript.write_text("\n".join(map(json.dumps, lines)))
    row = _row("a")
    row.update(transcript=str(transcript), cwd=str(tmp_path / "nope"), git_branch="", brief_text="report /tmp/none-xyz.md")
    b = build_bundle(row)
    assert b["approx_tokens"] <= champion.MAX_TOTAL_TOKENS
    assert b["truncated"]["last_assistant_message"] is True
    assert b["gate_exit_codes"] == [{"cmd": "python3 -m pytest -q", "exit": 2}]
    assert b["diff_stat_recoverable"] is False and b["diff_stat"] is None


def test_bundle_diff_stat_capped_to_code_budget(monkeypatch):
    monkeypatch.setattr(champion, "diff_stat", lambda row, hint=None: "x" * 100_000)
    row = _row("a")
    row.update(transcript="", cwd="", git_branch="")
    b = build_bundle(row)
    assert len(b["diff_stat"]) == champion.MAX_CODE_TOKENS * champion.CHARS_PER_TOKEN
    assert b["truncated"]["diff_stat"] is True


def test_diff_stat_recovers_from_real_git_worktree(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "a.txt").write_text("1\n")
    run("add", ".")
    run("commit", "-m", "init")
    run("switch", "-c", "feat")
    (repo / "a.txt").write_text("1\n2\n")
    run("commit", "-am", "two")
    assert "a.txt" in champion.diff_stat({"cwd": str(repo), "git_branch": "feat", "project": "x"})
    run("switch", "main")
    assert "a.txt" in champion.diff_stat({"cwd": "", "git_branch": "feat", "project": "x"}, str(repo))


# ── lift ────────────────────────────────────────────────────────────────────

def _lrow(i, project, **flags):
    feats = {f: False for f in ENFORCED + PROSE if f not in champion.NUMERIC}
    feats.update(caps_count=0, length_chars=100, **{k: v for k, v in flags.items() if k not in ("positive",)})
    pos = flags["positive"]
    return {"brief_id": f"b{i}", "source": "claude_subagent", "project": project, "agent_kind": "host_subagent",
            "explicit": True, "label_source": "pr_merge", "positive": pos, "features": feats,
            "outcome": "landed" if pos else "abandoned"}


def _cells(feature, projects, w_pos, w_n, wo_pos, wo_n, start=0):
    rows, i = [], start
    for p in projects:
        for n, pos_n, flag in ((w_n, w_pos, True), (wo_n, wo_pos, False)):
            for k in range(n):
                rows.append(_lrow(i, p, **{feature: flag, "positive": k < pos_n}))
                i += 1
    return rows


def test_lift_load_bearing_when_two_projects_hold_ten_points():
    rows = _cells("has_acceptance_cmd", ["p1", "p2"], 20, 25, 10, 25)
    res = lift(rows)
    v = res["features"]["has_acceptance_cmd"]
    assert v["verdict"] == "load-bearing" and v["projects_holding_10pt"] == ["p1", "p2"]
    assert res["verdict"] == "load-bearing"
    assert res["prevalence"]["has_acceptance_cmd"]["lift_population"] == 0.5


def test_lift_single_project_is_untestable_not_no_lift():
    res = lift(_cells("has_file_scope", ["p1"], 20, 25, 5, 25))
    assert res["features"]["has_file_scope"]["verdict"] == "untestable"
    assert res["verdict"] == "corpus too homogeneous"


def test_lift_near_universal_feature_is_untestable():
    rows = _cells("isolation_worktree", ["p1", "p2"], 10, 50, 0, 3)  # without-arm n=3 < 20
    res = lift(rows)
    assert res["features"]["isolation_worktree"]["verdict"] == "untestable"


def test_lift_kill_needs_three_testable_enforced_none_clearing():
    rows, start = [], 0
    for f in ("has_acceptance_cmd", "has_file_scope", "has_relay_hop"):
        rows += _cells(f, ["p1", "p2"], 10, 20, 10, 20, start)  # zero lift
        start += 200
    res = lift(rows)
    assert len(res["enforced_testable"]) >= 3 and res["enforced_load_bearing"] == []
    assert res["verdict"] == "kill"


def test_lift_two_testable_enforced_is_homogeneous_not_kill():
    rows, start = [], 0
    for f in ("has_acceptance_cmd", "has_file_scope"):
        rows += _cells(f, ["p1", "p2"], 10, 20, 10, 20, start)
        start += 200
    assert lift(rows)["verdict"] == "corpus too homogeneous"


def test_lift_prose_clears_but_enforced_does_not_flags_contradiction():
    rows = _cells("has_receipts_clause", ["p1", "p2"], 20, 25, 5, 25)
    res = lift(rows)
    assert res["prose_load_bearing"] == ["has_receipts_clause"] and res["contradicts_spine"] is True
    assert res["verdict"] == "corpus too homogeneous"  # prose alone never rescues the enforced verdict


def test_lift_below_ten_points_is_no_lift():
    rows = _cells("has_acceptance_cmd", ["p1", "p2"], 13, 25, 12, 25)  # 52% vs 48%
    assert lift(rows)["features"]["has_acceptance_cmd"]["verdict"] == "no-lift"


def test_lift_excludes_positive_only_and_inferred():
    rows = _cells("has_acceptance_cmd", ["p1", "p2"], 10, 20, 10, 20)
    orca = [dict(_lrow(10_000 + i, "p1", has_acceptance_cmd=True, positive=True), label_source="orca_worker_done")
            for i in range(500)]
    inferred = [dict(_lrow(20_000, "p1", positive=False), explicit=False)]
    res = lift(rows + orca + inferred)
    assert res["population"]["lift_population"] == len(rows)
    assert res["population"]["excluded_positive_only"] == 500


def test_lift_cell_n_reported_and_numeric_features_binarized_at_median():
    rows = _cells("has_acceptance_cmd", ["p1"], 5, 21, 5, 21)
    for i, r in enumerate(rows):
        r["features"]["length_chars"] = i
    res = lift(rows)
    cell = [c for c in res["cells"]["has_acceptance_cmd"] if c["project"] == "p1"][0]
    assert (cell["n_with"], cell["n_without"], cell["valid"]) == (21, 21, True)
    assert res["prevalence"]["length_chars"]["median_cut"] == statistics_median(range(42))


def statistics_median(xs):
    import statistics
    return statistics.median(xs)


def test_cli_help_and_lift_roundtrip(tmp_path, capsys):
    briefs = tmp_path / "b.jsonl"
    briefs.write_text("\n".join(json.dumps(r) for r in _cells("has_acceptance_cmd", ["p1", "p2"], 20, 25, 10, 25)))
    md = tmp_path / "r.md"
    assert champion.main(["lift", "--briefs", str(briefs), "--md", str(md), "--compact", "--window", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "load-bearing" and "cells" not in out
    assert "VERDICT: load-bearing" in md.read_text()
    with pytest.raises(SystemExit):
        champion.main(["--help"])


# ── bus-dispatch source and window-mode lift ────────────────────────────────

def _dispatch_event(session, agent_id, prompt, model="sonnet", isolation="worktree"):
    summary = json.dumps({"description": "d", "isolation": isolation, "model": model, "name": "n",
                          "prompt": prompt, "subagent_type": "sonnet-worker"})
    resp = json.dumps({"agentId": agent_id, "description": "d"})
    return json.dumps({"time": "2026-08-01T00:00:00Z", "data": {
        "tool_name": "Agent", "session_id": session, "project": "projx", "ts": "2026-08-01T00:00:00Z",
        "tool_summary": summary, "tool_response_summary": resp}}, separators=(",", ":"))


def test_bus_dispatch_recovers_old_briefs_and_dedupes_against_transcripts(tmp_path):
    _write_subagent(tmp_path / "transcripts", "-p", "S", "full1", "FULL TEXT brief", "2026-10-01T00:00:00Z")
    _make_runs_db(tmp_path / "runs.db", [
        {"run_id": "R_old", "run_key": "S#agent-old1", "outcome": "abandoned", "source": "git_empty_branch"},
        {"run_id": "R_full", "run_key": "S#agent-full1", "outcome": "landed", "source": "pr_merge"}])
    conn = sqlite3.connect(tmp_path / "runs.db")
    for i, (aid, prompt) in enumerate([("old1", "OLD brief gate `pytest -q`"), ("full1", "dup bounded")]):
        conn.execute("INSERT INTO run_events (run_id,seq,event_ts,event_type,raw_json) VALUES (?,?,?,?,?)",
                     ("P", i, "t", "tool_call", _dispatch_event("S", aid, prompt)))
    conn.commit()
    conn.close()
    extract(_ns(tmp_path))
    rows = {r["source_id"]: r for r in _rows(tmp_path / "out" / "briefs.jsonl")}
    assert set(rows) == {"old1", "full1"}
    old = rows["old1"]
    assert old["source"] == "bus_dispatch" and old["text_source"] == "bounded"
    assert old["explicit"] and old["positive"] is False and old["label_source"] == "git_empty_branch"
    assert old["project"] == "projx" and old["agent_type"] == "sonnet-worker"
    assert old["features"]["model_explicit"] and old["features"]["isolation_worktree"]
    assert rows["full1"]["source"] == "claude_subagent" and rows["full1"]["text_source"] == "full"


def test_truncated_prompt_recovered_without_closing_quote():
    cut = '{"description":"d","model":"sonnet","name":"n","prompt":"Line one\\nSay \\"hi\\" then run `pytes'
    assert champion._truncated_prompt(cut) == 'Line one\nSay "hi" then run `pytes'
    assert champion._truncated_prompt('{"prompt":"ends on a backslash\\') == "ends on a backslash"
    assert champion._truncated_prompt('{"no":"prompt"}') == ""


def _wrow(i, project, text, pos):
    return {"brief_id": f"w{i}", "source": "claude_subagent", "project": project, "agent_kind": "host_subagent",
            "explicit": True, "label_source": "pr_merge", "positive": pos, "brief_text": text, "cwd": "",
            "meta": {}, "outcome": "landed" if pos else "abandoned", "features": {}}


def test_lift_window_measures_all_rows_on_same_prefix():
    late = "x" * 900 + " receipts go in the report"
    rows = []
    for p in ("p1", "p2"):
        for i in range(25):
            rows.append(_wrow(f"{p}a{i}", p, late, True))
            rows.append(_wrow(f"{p}b{i}", p, "plain", i % 2 == 0))
    res = lift(rows, window=800)
    assert "length_chars" not in res["features"]
    assert res["prevalence"]["has_receipts_clause"]["lift_population"] == 0.0  # clause is past the window
    assert lift(rows, window=2000)["prevalence"]["has_receipts_clause"]["lift_population"] == 0.5


def test_lift_all_positive_cell_is_not_valid_evidence():
    rows = _cells("has_acceptance_cmd", ["p1", "p2"], 25, 25, 25, 25)  # every outcome positive
    res = lift(rows)
    assert res["features"]["has_acceptance_cmd"]["verdict"] == "untestable"
    assert res["verdict"] == "corpus too homogeneous"


# ── Experiment C: revise / score-revisions (no model is ever called) ─────────

def _crow(i, dets, outcome="abandoned", text=None):
    r = _row(f"c{i}", outcome=outcome, label_src="git_branch_gone" if outcome == "abandoned" else "pr_merge",
             text=text or f"Synthetic task number {i}: update the widget module and report back. Keep going.")
    r.update(detectors=dets, transcript="", cwd="", git_branch="", run_worktree=None)
    return r


class _FakeRun:
    """Stands in for subprocess.run; records stdin so the blind guarantee can be asserted."""
    def __init__(self, reply):
        self.reply, self.inputs, self.lock = reply, [], threading.Lock()

    def __call__(self, cmd, input=None, **kw):
        with self.lock:
            self.inputs.append(input)
        body = self.reply(input) if callable(self.reply) else self.reply
        return type("R", (), {"stdout": json.dumps({"result": body, "is_error": False,
                                                    "total_cost_usd": 0.0, "modelUsage": {"m": {}}})})()


REV = {"role": {"agent_type": "w", "model": "sonnet", "isolation": "worktree"},
       "instruction": "Update the widget module and report back.",
       "contract": {"fileScope": ["widget/"], "acceptanceGates": ["pytest -q tests"], "receipts": [], "outOfScope": []},
       "hop": {"dependsOn": [], "relay_to": None, "merge_train": False, "scratch_dir": None}, "context": []}


def _cns(tmp_path, briefs, **kw):
    d = dict(briefs=str(briefs), out_dir=str(tmp_path / "rev"), brief=None, sample=100, seed=7, model="sonnet",
             parallel=2, cap=100, batch=0, bundle_only=False, config_dir=str(tmp_path / "cfg"))
    d.update(kw)
    return Namespace(**d)


def _write_briefs(tmp_path, rows):
    p = tmp_path / "briefs.jsonl"
    p.write_text("\n".join(map(json.dumps, rows)))
    return p


def test_blind_optimizer_input_never_contains_detector_terms(tmp_path):
    leaky = _crow(1, ["worktree_leak"], text="Fix the thing; a past run flagged worktree_leak and red baseline dispatch "
                  "and unverified-completion, plus rebuild cache miss noise. " + "x" * 50)
    briefs = _write_briefs(tmp_path, [leaky] + [_crow(i, ["reread_same_file"]) for i in range(2, 6)])
    fake = _FakeRun(json.dumps(REV))
    st = revise_cmd(_cns(tmp_path, briefs), run=fake)
    assert st["called"] == 5 and fake.inputs
    for prompt in fake.inputs:
        assert champion.blind_scan(prompt) == []
        for t in champion.BLIND_TERMS:
            assert t not in prompt and t.replace("_", " ") not in prompt.lower()
    assert any("[term]" in p for p in fake.inputs)  # the leaky brief was redacted, not dropped


def test_optimizer_prompt_ignores_row_detector_fields():
    row = _crow(9, ["unverified_completion"])
    row["failure_taxonomy"] = ["unverified_completion"]
    p = champion.optimizer_prompt(row, {"outcome": "abandoned", "label_source": "x"})
    assert "unverified" not in p and "abandoned" in p


def test_blind_violation_raised_if_not_redactable(monkeypatch):
    monkeypatch.setattr(champion, "redact_blind", lambda t: (t, 0))
    row = _crow(1, [], text="mentions kb_recall_gap explicitly in the brief " * 2)
    with pytest.raises(champion.BlindViolation):
        champion.optimizer_prompt(row, {})


def test_revise_cap_enforced_across_batches(tmp_path):
    briefs = _write_briefs(tmp_path, [_crow(i, ["reread_same_file"]) for i in range(6)])
    fake = _FakeRun(json.dumps(REV))
    a = revise_cmd(_cns(tmp_path, briefs, cap=4, batch=3), run=fake)
    b = revise_cmd(_cns(tmp_path, briefs, cap=4, batch=3), run=fake)
    assert a["called"] == 3 and b["called"] == 1 and b["cap_blocked"] == 2
    assert len(fake.inputs) == 4 and b["revise_calls_spent_total"] == 4
    c = revise_cmd(_cns(tmp_path, briefs, cap=4), run=fake)  # rerun: capped, nothing new
    assert c["called"] == 0 and len(fake.inputs) == 4


def test_revise_is_resumable_and_counts_parse_failures(tmp_path):
    briefs = _write_briefs(tmp_path, [_crow(i, ["reread_same_file"]) for i in range(2)])
    bad = _FakeRun("not json at all")
    st = revise_cmd(_cns(tmp_path, briefs), run=bad)
    assert st["parse_failures"] == 2 and st["revise_calls_spent_total"] == 2
    again = revise_cmd(_cns(tmp_path, briefs), run=bad)
    assert again["already_done"] == 2 and again["called"] == 0


def test_parse_and_normalize_fenced_json():
    v = champion.parse_json_obj("sure!\n```json\n" + json.dumps(REV) + "\n```\nthanks")
    assert champion.normalize_revision(v)["contract"]["acceptanceGates"] == ["pytest -q tests"]
    assert champion.normalize_revision({"no": "instruction"}) is None
    assert champion.parse_json_obj("{broken") is None


def _rev(**kw):
    r = json.loads(json.dumps(REV))
    r["contract"]["acceptanceGates"] = []
    r["contract"]["fileScope"] = []
    for k, v in kw.items():
        if k in r["contract"]:
            r["contract"][k] = v
        else:
            r[k] = v
    return r


ORIG = "Update the widget module and report back."


@pytest.mark.parametrize("det,hit,miss", [
    ("unverified_completion", _rev(acceptanceGates=["pytest -q tests/widget"]), _rev(instruction=ORIG + " Verify it carefully.")),
    ("red_baseline_dispatch", _rev(instruction=ORIG + " First run the suite and record the baseline failures before you edit anything."),
     _rev(instruction=ORIG + " Keep a good baseline of quality.")),
    ("inherited_rationalization", _rev(instruction=ORIG + " Do not assume the failure is pre-existing; reproduce it on a clean main first."),
     _rev(instruction=ORIG + " The failure is pre-existing.")),
    ("reread_same_file", _rev(context=["widget/core.py", "kb brief widget"]), _rev(instruction=ORIG + " Read carefully.")),
    ("rebuild_cache_miss", _rev(instruction=ORIG + " Set CARGO_TARGET_DIR to the shared build pool."), _rev(instruction=ORIG + " Build it.")),
    ("worktree_leak", _rev(instruction=ORIG + " When merged, run git worktree remove on your checkout."), _rev(instruction=ORIG + " Use a worktree.")),
    ("skill_opportunity", _rev(instruction=ORIG + " Load the beads skill before running bd."), _rev(instruction=ORIG + " Be skillful.")),
])
def test_scoring_rules_demand_mechanism_not_vocabulary(det, hit, miss):
    sh = champion.score_rule(det, hit, champion.revision_delta(ORIG, hit))
    sm = champion.score_rule(det, miss, champion.revision_delta(ORIG, miss))
    assert sh["strong"] is True
    assert sm["strong"] is False


def test_rules_only_see_new_content_not_the_original():
    orig = "Run pytest -q before reporting. " + ORIG
    rev = _rev(instruction=orig)  # unchanged restatement adds nothing
    d = champion.revision_delta(orig, rev)
    assert d["enforced"] == [] and d["prose"] == []
    assert champion.score_rule("unverified_completion", rev, d) == {"strong": False, "weak": False}


def test_enforced_share_counts_contract_hop_vs_prose():
    rev = _rev(acceptanceGates=["pytest -q"], fileScope=["a.py", "b.py"], instruction=ORIG + " A wholly new sentence appears here.")
    d = champion.revision_delta(ORIG, rev)
    assert len(d["enforced"]) == 3 and len(d["prose"]) == 1
    agg = champion.aggregate([{"detector": "x", "outcome": "abandoned", "targeted": True, "t_src": "regex", "t_strong": True,
                               "placebo_hit": False, "p_src": "regex", "p_strong": False,
                               "n_enforced": 3, "n_prose": 1}], {})
    assert agg["enforced_share_pooled"] == 0.75 and agg["verdict"] == "proceed"


def test_verdict_thresholds():
    def rows(k, n):
        return [{"detector": "x", "outcome": "abandoned", "targeted": i < k, "t_src": "regex", "t_strong": i < k,
                 "placebo_hit": False, "p_src": "regex", "p_strong": False, "n_enforced": 0, "n_prose": 1} for i in range(n)]
    assert champion.aggregate(rows(5, 10), {})["verdict"] == "proceed"
    assert champion.aggregate(rows(4, 10), {})["verdict"] == "inconclusive"
    assert champion.aggregate(rows(2, 10), {})["verdict"] == "kill"


def test_placebo_never_picks_own_detectors_and_is_deterministic():
    own = ["reread_same_file", "worktree_leak"]
    picks = {champion.placebo_detector(f"sha{i}", own, 1) for i in range(200)}
    assert picks and not picks & set(own) and picks <= set(champion.SCORABLE)
    assert champion.placebo_detector("s", own, 1) == champion.placebo_detector("s", own, 1)


def test_generic_boilerplate_scores_the_same_against_placebo(tmp_path):
    boiler = _rev(instruction=ORIG + " Run pytest -q, record the baseline failures first, load the beads skill, "
                  "do not assume anything is pre-existing, set CARGO_TARGET_DIR to the shared pool, "
                  "git worktree remove when done.")
    boiler["context"] = ["kb brief"]
    briefs = _write_briefs(tmp_path, [_crow(i, ["reread_same_file"]) for i in range(8)])
    ns = _cns(tmp_path, briefs)
    revise_cmd(ns, run=_FakeRun(json.dumps(boiler)))
    res = champion.score_revisions(ns, run=_FakeRun(json.dumps({"addresses": True, "why": "x"})))
    assert res["targeted_rate"] == 1.0 and res["placebo_rate"] == 1.0 and res["placebo_gap"] == 0.0


def test_score_judge_blind_cap_and_cache(tmp_path):
    vague = _rev(instruction=ORIG + " Please check the tests and the build more carefully.")
    briefs = _write_briefs(tmp_path, [_crow(i, ["unverified_completion"]) for i in range(6)])
    revise_cmd(_cns(tmp_path, briefs), run=_FakeRun(json.dumps(vague)))
    judge = _FakeRun(json.dumps({"addresses": False, "why": "vague"}))
    res = champion.score_revisions(_cns(tmp_path, briefs, cap=3), run=judge)
    assert res["score_calls_spent"] <= 3 and 0 < len(judge.inputs) <= 3
    for p in judge.inputs:
        assert champion.blind_scan(p) == []
        assert "without ever running any build" in p  # mechanism description, not the name
    n = len(judge.inputs)
    res2 = champion.score_revisions(_cns(tmp_path, briefs, cap=3), run=judge)
    assert res2["score_calls_spent"] <= 3 and len(judge.inputs) == n  # cache + on-disk ledger


def test_sample_is_seeded_stratified_abandoned_first():
    rows = [_crow(i, ["unverified_completion"], outcome="abandoned" if i % 3 else "landed") for i in range(60)]
    rows += [_crow(100 + i, ["worktree_leak"], outcome="landed") for i in range(30)]
    a, comp = champion.select_sample(rows, 100, 5)
    b, _ = champion.select_sample(rows, 100, 5)
    assert a == b and comp["seed"] == 5
    first = [m for m in a if m["detector"] == "unverified_completion"][:25]
    assert len(first) == 25 and all(m["outcome"] == "abandoned" for m in first)  # abandoned fill first
    assert comp["strata"]["worktree_leak"]["eligible"] == 30
    assert "user_correction" in comp["absent_strata"]
    assert len({m["brief_sha256"] for m in a}) == len(a)
