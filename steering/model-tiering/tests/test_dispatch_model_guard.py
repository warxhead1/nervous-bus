import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "dispatch-model-guard"


def run(tool_input, tool="Agent", **env):
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True, env={**os.environ, **env})
    return p.returncode


def test_blocks_dispatch_without_model():
    assert run({"prompt": "x", "description": "y"}) == 2


def test_allows_dispatch_with_model():
    assert run({"prompt": "x", "model": "haiku"}) == 0


def test_allows_fork_and_listed_types():
    assert run({"prompt": "x", "subagent_type": "fork"}) == 0
    assert run({"prompt": "x", "subagent_type": "pinned"}, MODEL_TIERING_ALLOW_TYPES="pinned, other") == 0
    assert run({"prompt": "x", "subagent_type": "unlisted"}, MODEL_TIERING_ALLOW_TYPES="pinned") == 2


def test_ignores_other_tools_and_kill_switch():
    assert run({"command": "ls"}, tool="Bash") == 0
    assert run({"prompt": "x"}, MODEL_TIERING_DISABLE="1") == 0


def test_bad_input_never_blocks():
    p = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
    assert p.returncode == 0
