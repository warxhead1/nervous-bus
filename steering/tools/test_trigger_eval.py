"""trigger-eval keeps errors and timeouts out of the hit rate, caps spend, and states its resolution."""
import importlib.machinery
import importlib.util
import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

SRC = Path(__file__).with_name("trigger-eval")
loader = importlib.machinery.SourceFileLoader("trigger_eval", str(SRC))
te = importlib.util.module_from_spec(importlib.util.spec_from_loader("trigger_eval", loader))
loader.exec_module(te)


def stream(skill=None, cost=0.01, is_error=False, with_result=True):
    lines = []
    if skill:
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": skill}}]}}))
    if with_result:
        lines.append(json.dumps({"type": "result", "is_error": is_error, "total_cost_usd": cost}))
    return "\n".join(lines)


def proc(out, rc=0):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


def args(**kw):
    base = dict(claude="claude", model="m", timeout=5, config_dir=None, streams=None,
                max_cost_usd=0.0, max_runs=0, jobs=1)
    base.update(kw)
    return Namespace(**base)


def run(monkeypatch, outs, **kw):
    seq = iter(outs)

    def fake(*a, **k):
        o = next(seq)
        if isinstance(o, Exception):
            raise o
        return o
    monkeypatch.setattr(te.subprocess, "run", fake)
    jobs = [("s", True, f"p{i}", 0) for i in range(len(outs))]
    return te.collect(args(**kw), jobs, [], "/nonexistent")


def test_nonzero_exit_is_an_error_not_a_miss(monkeypatch):
    rows, _ = run(monkeypatch, [proc("", rc=1)])
    assert rows[0]["status"] == "error" and rows[0]["hit"] is None


def test_empty_stream_with_zero_exit_is_an_error(monkeypatch):
    rows, _ = run(monkeypatch, [proc("")])
    assert rows[0]["status"] == "error"


def test_result_is_error_is_an_error(monkeypatch):
    rows, _ = run(monkeypatch, [proc(stream(is_error=True))])
    assert rows[0]["status"] == "error"


def test_timeout_is_recorded(monkeypatch):
    rows, _ = run(monkeypatch, [subprocess.TimeoutExpired("c", 5)])
    assert rows[0]["status"] == "timeout" and rows[0]["hit"] is None


def test_clean_miss_and_pass(monkeypatch):
    rows, _ = run(monkeypatch, [proc(stream()), proc(stream("s"))])
    by = {r["prompt"]: r for r in rows}
    assert (by["p0"]["status"], by["p0"]["hit"]) == ("miss", False)
    assert (by["p1"]["status"], by["p1"]["hit"]) == ("pass", True)


def test_rates_exclude_error_and_timeout(monkeypatch):
    rows, _ = run(monkeypatch, [proc(stream("s")), proc("", rc=1), subprocess.TimeoutExpired("c", 5)])
    t = te.tally(rows)["s"]
    assert (t["pos_hit"], t["pos_n"], t["error"], t["timeout"]) == (1, 1, 1, 1)


def test_max_runs_stops_and_marks_partial(monkeypatch):
    rows, partial = run(monkeypatch, [proc(stream("s"))] * 5, max_runs=2)
    assert partial and len([r for r in rows if r["status"] != "skipped"]) == 2


def test_max_cost_stops_and_marks_partial(monkeypatch):
    rows, partial = run(monkeypatch, [proc(stream("s", cost=0.6))] * 5, max_cost_usd=1.0)
    assert partial and len([r for r in rows if r["status"] != "skipped"]) == 2


def test_uncapped_is_not_partial(monkeypatch):
    _, partial = run(monkeypatch, [proc(stream("s"))] * 3)
    assert not partial


def test_budget_flag_reaches_the_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(te.subprocess, "run", lambda cmd, **k: seen.append(cmd) or proc(stream("s")))
    te.run_one(args(max_cost_usd=2.5), "q", [], "/nonexistent")
    assert "--max-budget-usd" in seen[0] and "2.5" in seen[0]


def test_run_does_not_inherit_the_launching_session(monkeypatch):
    seen = []
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/some/session/plugin")
    monkeypatch.setattr(te.subprocess, "run", lambda cmd, **k: seen.append((cmd, k["env"])) or proc(stream("s")))
    te.run_one(args(), "q", [], "/nonexistent")
    cmd, env = seen[0]
    assert "CLAUDE_CODE_PLUGIN_DIRS" not in env and "--strict-mcp-config" in cmd
    assert env["AGENT_STEERING_HITS"] == "/nonexistent/hits.jsonl"


def test_clean_config_also_gets_a_clean_home(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(te.subprocess, "run", lambda cmd, **k: seen.append(k["env"]) or proc(stream("s")))
    te.run_one(args(config_dir=str(tmp_path / "cfg")), "q", [], str(tmp_path))
    assert seen[0]["HOME"] == str(tmp_path / "home") and (tmp_path / "home").is_dir()


@pytest.mark.parametrize("n,expect", [(3, None), (4, (4, 0)), (5, (4, 0)), (2, None)])
def test_min_detectable(n, expect):
    got = te.min_detectable(n)
    assert (got[:2] if got else None) == expect


def test_resolution_text_states_the_limit():
    assert "cannot reach" in te.resolution(3)
    assert "4/5 vs 0/5" in te.resolution(5)
