import json
import os
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "hooks"


def run(hook, tool, tool_input, **env):
    p = subprocess.run([sys.executable, str(HOOKS / hook)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True, env={**os.environ, **env})
    return p.returncode


def dispatch(**ti):
    return run("dispatch-isolation-guard", "Agent", ti)


def stash(command, **env):
    return run("stash-guard", "Bash", {"command": command}, **env)


def test_blocks_an_editing_dispatch_without_isolation():
    assert dispatch(prompt="Fix the failing parser test and commit it.") == 2
    assert dispatch(prompt="Implement the retry handler in client.py", subagent_type="general-purpose") == 2


def test_allows_isolated_readonly_and_reader_types():
    assert dispatch(prompt="Fix the failing parser test.", isolation="worktree") == 0
    assert dispatch(prompt="READ-ONLY. Find why the parser test fails; do not fix it.") == 0
    assert dispatch(prompt="List every caller of parse() and report file and line.") == 0
    assert dispatch(prompt="Fix it", subagent_type="Explore") == 0
    assert dispatch(prompt="Fix it", subagent_type="builder") == 2
    assert run("dispatch-isolation-guard", "Agent", {"prompt": "Fix it", "subagent_type": "builder"},
               ISOLATED_DISPATCH_READONLY_TYPES="builder") == 0


def test_dispatch_guard_ignores_other_tools_bad_input_and_kill_switch():
    assert run("dispatch-isolation-guard", "Bash", {"command": "fix"}) == 0
    assert run("dispatch-isolation-guard", "Agent", {"prompt": "Fix it"}, ISOLATED_DISPATCH_DISABLE="1") == 0
    p = subprocess.run([sys.executable, str(HOOKS / "dispatch-isolation-guard")], input="nope", capture_output=True, text=True)
    assert p.returncode == 0


def test_blocks_bare_stash_and_pop():
    for cmd in ("git stash", "git stash -u", "git stash && git checkout main", "cd repo && git stash push",
                "git stash pop", "git -C /repo stash drop", "git stash clear", "git stash save"):
        assert stash(cmd) == 2, cmd


def test_allows_tagged_stash_and_reads():
    for cmd in ("git stash push -u -m wip-parser -- src/parser.py", "git stash list", "git stash show -p stash@{1}",
                "git stash apply 3f2a91c", 'git commit -m "explain why we never git stash pop"', "git status",
                "echo 'git stash'", "git stash list --format='%H %gs'"):
        assert stash(cmd) == 0, cmd


def test_stash_guard_kill_switch_and_other_tools():
    assert stash("git stash", STASH_GUARD_DISABLE="1") == 0
    assert run("stash-guard", "Edit", {"file_path": "x", "new_string": "git stash"}) == 0


def test_stash_text_inside_a_heredoc_is_not_a_command_and_named_entries_pass():
    assert stash("cat > brief.md <<'EOF'\nNever run git stash or git stash pop.\nEOF\ngit status") == 0
    assert stash("git stash drop stash@{2}") == 0
    assert stash("git stash drop $n") == 0
