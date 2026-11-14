import importlib.util
import json
import os
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "steering_judge_launch", Path(__file__).resolve().parents[1] / "automation" / "steering_judge_launch.py")
sjl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sjl)

NOW = 1_800_000_000.0
DUE = {"due": True, "reasons": ["steering_audit_stale"]}
DAY = 86400


def test_launches_when_due_and_quiet_and_never_launched():
    assert sjl.decide(DUE, {}, None, NOW) == (True, "due: steering_audit_stale")
    assert sjl.decide(DUE, {}, 3 * 3600, NOW)[0] is True


def test_skips_when_the_gate_is_not_due_or_missing():
    assert sjl.decide({"due": False}, {}, None, NOW)[0] is False
    assert sjl.decide(None, {}, None, NOW)[0] is False


def test_one_attempt_a_week_even_if_the_gate_stays_due():
    assert sjl.decide(DUE, {"last_launch": NOW - 6 * DAY}, None, NOW)[0] is False
    assert sjl.decide(DUE, {"last_launch": NOW - 8 * DAY}, None, NOW)[0] is True


def test_skips_while_a_session_is_active_and_when_disabled():
    assert sjl.decide(DUE, {}, 10 * 60, NOW)[0] is False
    assert sjl.decide(DUE, {}, None, NOW, disabled=True)[0] is False


def run_main(tmp_path, monkeypatch, gate, age_s=None, argv=()):
    digest = tmp_path / "digest"
    digest.mkdir(exist_ok=True)
    (digest / "verdicts-latest.json").write_text(json.dumps({"judge": gate}))
    tr = tmp_path / "projects" / "p"
    tr.mkdir(parents=True, exist_ok=True)
    if age_s is not None:
        f = tr / "s.jsonl"
        f.write_text("{}")
        os.utime(f, (time.time() - age_s, time.time() - age_s))
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(sjl.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())
    monkeypatch.delenv("STEERING_JUDGE_DISABLE", raising=False)
    rc = sjl.main(["--digest-dir", str(digest), "--transcripts", str(tmp_path / "projects"),
                   "--cwd", str(tmp_path), *argv])
    return rc, calls, digest


def test_a_launch_records_the_attempt_so_the_next_night_skips(tmp_path, monkeypatch):
    rc, calls, digest = run_main(tmp_path, monkeypatch, DUE, age_s=4 * 3600)
    assert rc == 0 and len(calls) == 1 and calls[0][0] == "claude"
    assert json.loads((digest / "judge-launch.json").read_text())["last_launch"] > 0
    rc, calls, _ = run_main(tmp_path, monkeypatch, DUE, age_s=4 * 3600)
    assert rc == 0 and calls == []


def test_dry_run_and_an_active_session_start_nothing(tmp_path, monkeypatch):
    rc, calls, digest = run_main(tmp_path, monkeypatch, DUE, argv=("--dry-run",))
    assert calls == [] and not (digest / "judge-launch.json").exists()
    rc, calls, digest = run_main(tmp_path, monkeypatch, DUE, age_s=60)
    assert calls == [] and not (digest / "judge-launch.json").exists()
