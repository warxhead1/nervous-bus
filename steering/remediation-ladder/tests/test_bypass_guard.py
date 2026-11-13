import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "bypass-guard"


def run(command, tool="Bash", **env):
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}),
                       capture_output=True, text=True, env={**os.environ, **env})
    return p.returncode


def test_blocks_skipped_checks():
    for cmd in ("git commit --no-verify -m 'wip'", "git push --no-verify origin main", "git commit -n -m x",
                "git commit -anm x", "HUSKY=0 git commit -m x", "SKIP=ruff,mypy git commit -m x",
                "RERUN_GUARD_DISABLE=1 pytest -q", "cd app && LINT_HOOK_OFF=1 make check", "mvn package -DskipTests",
                "SKIP_TESTS=1 ./release.sh", "git -C repo commit -m x --no-verify"):
        assert run(cmd) == 2, cmd


def test_allows_ordinary_commands():
    for cmd in ("git commit -m 'fix parser'", "git commit -am 'x'", "git commit --amend --no-edit",
                'git commit -m "never use --no-verify"', "git push -u origin feature", "git log -n 5",
                "git rebase -i HEAD~3", "NEXT_TELEMETRY_DISABLED=1 npm run build", "CGO_ENABLED=0 go build ./...",
                "PIP_DISABLE_PIP_VERSION_CHECK=1 pip install x", "pytest -q --no-header", "grep -n SKIP= .pre-commit-config.yaml",
                "echo 'HUSKY=0'"):
        assert run(cmd) == 0, cmd


def test_kill_switch_other_tools_and_bad_input():
    assert run("git commit --no-verify", BYPASS_GUARD_DISABLE="1") == 0
    assert run("git commit --no-verify", tool="Edit") == 0
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True)
    assert p.returncode == 0


def test_shell_variables_and_heredoc_text_are_not_bypasses():
    assert run("SKIP=$(cat list.txt | wc -l); echo $SKIP") == 0
    assert run("cat > notes.md <<'EOF'\ngit commit --no-verify is never the answer\nEOF") == 0
