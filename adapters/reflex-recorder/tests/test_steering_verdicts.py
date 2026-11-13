"""tests/test_steering_verdicts.py — per-rule verdicts, budget, judge gate, atomic writes."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import query
import steering_audit
import steering_verdicts as sv
from store import SQLiteStore

NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


def _git(tree, *args, date=None):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True, env=env)


RULES = """
[rule.works]
files = ["works.md"]
rung = "automate"
detectors = ["det_works"]

[rule.noeffect]
files = ["noeffect.md"]
rung = "automate"
themes = ["t_noeffect"]

[rule.zero]
files = ["zero.md"]
rung = "automate"
detectors = ["det_zero"]

[rule.recurs]
files = ["recurs.md"]
rung = "inform"
themes = ["t_recurs"]

[rule.untargeted]
files = ["untargeted.md"]
rung = "inform"

[rule.silent]
files = ["silent.md"]
rung = "automate"
detectors = ["det_never_fires"]

[rule.open]
files = ["open.md"]
rung = "automate"
detectors = ["det_open"]
"""


@pytest.fixture()
def env(tmp_path):
    tree = tmp_path / "rules"
    tree.mkdir()
    _git(tree, "init", "-q", "-b", "main")
    (tree / "rules.toml").write_text(RULES)
    names = ["works", "noeffect", "zero", "recurs", "untargeted", "silent", "open"]
    for n in names:
        (tree / f"{n}.md").write_text("v1")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base", date="2026-07-01T00:00:00Z")
    for n in names:
        (tree / f"{n}.md").write_text("v2")
        _git(tree, "commit", "-qam", f"edit {n}",
             date="2099-01-01T00:00:00Z" if n == "open" else "2026-08-15T00:00:00Z")

    db = tmp_path / "runs.db"
    SQLiteStore(db)._conn.close()
    c = sqlite3.connect(db)
    n = 0

    def run(day):
        nonlocal n
        n += 1
        ts = f"{day}T12:00:00Z"
        c.execute("INSERT INTO runs (run_id, run_key, run_key_kind, project, agent_kind, started, ended,"
                  " recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                  (f"r{n}", f"r{n}", "session", "p", "host_claude_code", ts, ts, ts))
        return f"r{n}", ts

    def hit(day, det, sig="x"):
        rid, ts = run(day)
        c.execute("INSERT INTO detector_hits (run_id, detector, signature, project, ts) VALUES (?,?,?,?,?)",
                  (rid, det, sig, "p", ts))

    for d in range(2, 14):                      # before the 08-15 change
        run(f"2026-08-{d:02d}")
    for d in range(16, 28):                     # after it
        run(f"2026-08-{d:02d}")
    for d in range(8, 20):                      # the recent window before NOW
        run(f"2026-09-{d:02d}")
    for _ in range(4):
        hit("2026-08-05", "det_works")          # 4 before, 1 after: improved
    hit("2026-08-20", "det_works")
    for _ in range(2):
        hit("2026-08-06", "user_correction", "t_noeffect")
    for _ in range(3):
        hit("2026-08-21", "user_correction", "t_noeffect")   # did not fall
    hit("2026-07-10", "det_zero")               # fired once, long before any window
    hit("2026-07-10", "det_open")
    hit("2026-09-12", "user_correction", "t_recurs")          # still recurring
    c.commit()
    c.close()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield tree, conn
    conn.close()


def _build(env, tmp_path, **kw):
    tree, conn = env
    home = tmp_path / "claude"
    home.mkdir(exist_ok=True)
    return sv.build(conn, tree, tmp_path / "digest", home, now=NOW, **kw)


def test_every_verdict_branch(env, tmp_path):
    rep = _build(env, tmp_path)
    got = {r: (v["verdict"], v["reason"]) for r, v in rep["verdicts"].items()}
    assert got == {
        "works": ("keep", "effect_measured"),
        "noeffect": ("cut_candidate", "no_effect"),
        "zero": ("cut_candidate", "zero_before_after"),
        "recurs": ("promote_candidate", "target_recurs"),
        "untargeted": ("unmeasurable", "no_target"),
        "silent": ("unmeasurable", "no_hits_ever"),
        "open": ("keep", "window_open"),
    }
    assert rep["counts"] == {"keep": 2, "cut_candidate": 2, "promote_candidate": 1, "unmeasurable": 2}


def test_inform_rule_that_is_not_recurring_is_not_promoted(env, tmp_path):
    rep = _build(env, tmp_path, recent_days=3)      # the hit was 8 days before NOW
    assert rep["verdicts"]["recurs"]["verdict"] != "promote_candidate"


def test_automate_rule_with_recent_hits_is_not_promoted():
    rule = {"rung": "automate", "themes": ["t_recurs"]}
    v = sv.rule_verdict(rule, [], {"theme:t_recurs": 1}, {"theme:t_recurs": 5.0})
    assert v["verdict"] == "keep"


def test_budget_over_and_within(tmp_path):
    home = tmp_path / "claude"
    home.mkdir()
    (home / "CLAUDE.md").write_text("@a.md\n" + "x" * 400 + "\n")
    (home / "a.md").write_text("y" * 400 + "\n")
    est = steering_audit.import_chain(home / "CLAUDE.md", None)["est_tokens"]
    b = tmp_path / "budget.toml"
    b.write_text(f"max_est_tokens = {est + 10}\nraised_because = \"r\"\n")
    ok = sv.budget_finding(home, b)
    assert (ok["status"], ok["headroom"], ok["files"]) == ("within", 10, 2)
    b.write_text(f"max_est_tokens = {est - 1}\n")
    assert sv.budget_finding(home, b)["status"] == "over"
    assert sv.budget_finding(home, tmp_path / "none.toml")["reason"] == "no_budget_toml"


def _digest(d: Path, day: str, tokens: int):
    (d / f"{day}.json").write_text(json.dumps({"global_chain": {"est_tokens": tokens}}))


def test_judge_due_on_age_growth_and_neither(tmp_path):
    d = tmp_path
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert sv.judge_due(d, 1000, now)["reasons"] == ["never_audited"]

    (d / "last_audit").write_text("2026-10-30T10:00:00Z\n")
    _digest(d, "2026-10-30", 1000)
    fresh = sv.judge_due(d, 1040, now)
    assert fresh["due"] is False and fresh["chain_growth"] == 0.04
    assert sv.judge_due(d, 1050, now)["reasons"] == ["chain_growth"]

    (d / "last_audit").write_text("2026-10-20T10:00:00Z\n")
    _digest(d, "2026-10-20", 1000)
    assert sv.judge_due(d, 1000, now)["reasons"] == ["steering_audit_stale"]


def test_digest_health_flags_empty_and_missing_days(tmp_path):
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    for day in ("2026-11-04", "2026-11-02"):
        (tmp_path / f"{day}.json").write_text("{}")
    (tmp_path / "2026-11-03.json").write_text("")
    h = sv.digest_health(tmp_path, now, days=3)
    assert h["zero_byte"] == ["2026-11-03.json"]
    assert h["missing_recent_days"] == []
    assert sv.digest_health(tmp_path, now, days=4)["missing_recent_days"] == ["2026-11-01"]


def test_atomic_write_keeps_old_file_when_replace_fails(tmp_path, monkeypatch):
    p = tmp_path / "out.json"
    p.write_text("old")

    def boom(*a):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        sv.atomic_write(p, "new")
    assert p.read_text() == "old"
    assert [x.name for x in tmp_path.iterdir()] == ["out.json"]


def test_atomic_write_refuses_empty(tmp_path):
    with pytest.raises(ValueError):
        sv.atomic_write(tmp_path / "x.json", "")
    assert list(tmp_path.iterdir()) == []


def test_write_produces_parseable_json_and_table(env, tmp_path):
    rep = _build(env, tmp_path)
    paths = sv.write(rep, tmp_path / "digest")
    assert {p.name for p in paths} == {"verdicts-latest.json", "verdicts-latest.md",
                                       "verdicts-2026-09-20.json", "verdicts-2026-09-20.md"}
    assert json.loads((tmp_path / "digest" / "verdicts-latest.json").read_text())["verdicts"]["zero"]
    assert "| zero | automate | cut_candidate |" in (tmp_path / "digest" / "verdicts-latest.md").read_text()


def test_cli_subcommand_prints_written_file_and_errors_when_absent(env, tmp_path, monkeypatch, capsys):
    digest = tmp_path / "digest"
    monkeypatch.setattr(steering_audit, "STEERING_DIGEST_DIR", digest)
    db = tmp_path / "runs.db"
    assert query.main(["--db", str(db), "steering-verdicts"]) == 0
    assert "no readable verdicts" in capsys.readouterr().err
    sv.write(_build(env, tmp_path), digest)
    assert query.main(["--db", str(db), "steering-verdicts", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdicts"]["recurs"]["verdict"] == "promote_candidate"
