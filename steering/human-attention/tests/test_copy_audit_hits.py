import json
import os
import subprocess
import sys

from test_copy_audit import GAME, HOOK, QUIET_GAME, hook, write_event


def hits(path):
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def hook_env(tmp_path, event, **env):
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True,
                          env={**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache"), **env})


def test_a_report_is_logged_with_counts_and_no_copy(tmp_path, hit_log):
    f = tmp_path / "game.html"
    f.write_text(GAME)
    ev = {**write_event(f), "cwd": str(tmp_path)}
    assert hook(tmp_path, ev).returncode == 2
    (rec,) = hits(hit_log)
    assert set(rec) == {"v", "ts", "plugin", "check", "surface", "session_id", "cwd", "path", "action", "findings", "rules", "words"}
    assert (rec["v"], rec["plugin"], rec["check"], rec["surface"], rec["action"]) == (1, "human-attention", "copy-audit", "ui-file", "report")
    assert rec["path"] == "game.html" and rec["session_id"] == "s" and rec["ts"].endswith("Z")
    assert rec["findings"] == len(rec["rules"]) >= 1 and rec["words"] > 0
    raw = hit_log.read_text()
    assert "blue tile" not in raw and "Fever" not in raw


def test_a_quiet_file_logs_nothing(tmp_path, hit_log):
    f = tmp_path / "game.html"
    f.write_text(QUIET_GAME)
    hook(tmp_path, write_event(f))
    assert hits(hit_log) == []


def test_logging_off_and_a_dead_log_change_nothing(tmp_path, hit_log):
    f = tmp_path / "game.html"
    f.write_text(GAME)
    base = hook_env(tmp_path, write_event(f, "a"))
    off = hook_env(tmp_path, write_event(f, "b"), AGENT_STEERING_LOG="0")
    dead = hook_env(tmp_path, write_event(f, "c"), AGENT_STEERING_HITS=str(tmp_path))
    assert (base.returncode, base.stderr) == (off.returncode, off.stderr) == (dead.returncode, dead.stderr)
    assert len(hits(hit_log)) == 1


def test_the_log_follows_xdg_state_home_and_rotates_past_20_mb(tmp_path):
    f = tmp_path / "game.html"
    f.write_text(GAME)
    log = tmp_path / "xdg" / "agent-steering" / "hits.jsonl"
    env = {"AGENT_STEERING_HITS": "", "XDG_STATE_HOME": str(tmp_path / "xdg")}
    hook_env(tmp_path, write_event(f, "a"), **env)
    assert len(hits(log)) == 1
    with open(log, "ab") as fh:
        fh.truncate(20 * 1024 * 1024 + 1)
    hook_env(tmp_path, write_event(f, "b"), **env)
    assert log.with_name("hits.jsonl.1").stat().st_size > 20 * 1024 * 1024
    assert len(hits(log)) == 1
