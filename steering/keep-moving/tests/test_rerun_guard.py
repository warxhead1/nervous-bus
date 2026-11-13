import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "rerun-guard"


def run(tmp, tool, tool_input, event="PreToolUse", **env):
    ev = {"session_id": "s1", "hook_event_name": event, "tool_name": tool, "tool_input": tool_input}
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(ev), capture_output=True, text=True,
                       env={**os.environ, "XDG_CACHE_HOME": str(tmp), **env})
    return p.returncode


def test_third_identical_test_run_is_blocked_then_passes(tmp_path):
    cmd = {"command": "cargo test -p core"}
    assert [run(tmp_path, "Bash", cmd) for _ in range(4)] == [0, 0, 2, 0]


def test_an_edit_resets_the_count(tmp_path):
    cmd = {"command": "pytest -q tests"}
    assert run(tmp_path, "Bash", cmd) == 0 and run(tmp_path, "Bash", cmd) == 0
    assert run(tmp_path, "Edit", {"file_path": "/x/a.py"}, event="PostToolUse") == 0
    assert [run(tmp_path, "Bash", cmd) for _ in range(3)] == [0, 0, 2]


def test_different_commands_and_non_runners_pass(tmp_path):
    for c in ("go test ./a", "go test ./b", "go test ./c"):
        assert run(tmp_path, "Bash", {"command": c}) == 0
    for _ in range(5):
        assert run(tmp_path, "Bash", {"command": "git status --short"}) == 0
        assert run(tmp_path, "Bash", {"command": "gh pr checks 12"}) == 0


def test_whitespace_does_not_hide_a_repeat(tmp_path):
    assert run(tmp_path, "Bash", {"command": "npm  test"}) == 0
    assert run(tmp_path, "Bash", {"command": "npm test"}) == 0
    assert run(tmp_path, "Bash", {"command": " npm test "}) == 2


def test_kill_switch_and_bad_input(tmp_path):
    for _ in range(4):
        assert run(tmp_path, "Bash", {"command": "make test"}, RERUN_GUARD_DISABLE="1") == 0
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True)
    assert p.returncode == 0


def test_a_tree_change_from_the_shell_resets_and_detached_runs_pass(tmp_path):
    cmd = {"command": "go build ./..."}
    assert run(tmp_path, "Bash", cmd) == 0 and run(tmp_path, "Bash", cmd) == 0
    assert run(tmp_path, "Bash", {"command": "git pull --ff-only"}) == 0
    assert [run(tmp_path, "Bash", cmd) for _ in range(3)] == [0, 0, 2]
    for _ in range(4):
        assert run(tmp_path, "Bash", {"command": "setsid nohup make gate > g.log 2>&1 < /dev/null & disown"}) == 0


def test_output_filters_do_not_hide_a_repeat(tmp_path):
    assert run(tmp_path, "Bash", {"command": "cd /w && cargo test 2>&1 | tail -5"}) == 0
    assert run(tmp_path, "Bash", {"command": "timeout 600 cargo test | grep FAIL"}) == 0
    assert run(tmp_path, "Bash", {"command": "cargo test"}) == 2
