import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "visual-proof-guard"


def use(name, **tool_input):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]}}


def stop(tmp_path, events, active=False, session="s1", **env):
    t = tmp_path / f"{session}.jsonl"
    t.write_text("".join(json.dumps(e) + "\n" for e in events))
    p = subprocess.run([sys.executable, str(HOOK)],
                       input=json.dumps({"session_id": session, "transcript_path": str(t), "stop_hook_active": active}),
                       capture_output=True, text=True, env={**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache"), **env})
    return p.returncode, p.stderr


EDIT = use("Edit", file_path="/app/src/Card.tsx", old_string="a", new_string="b")


def test_sends_back_when_a_ui_edit_was_never_looked_at(tmp_path):
    code, err = stop(tmp_path, [use("Bash", command="npm test"), EDIT])
    assert code == 2 and "Card.tsx" in err


def test_fires_once_per_edit_then_again_for_a_newer_edit(tmp_path):
    assert stop(tmp_path, [EDIT])[0] == 2
    assert stop(tmp_path, [EDIT])[0] == 0
    assert stop(tmp_path, [EDIT, use("Bash", command="ls"), use("Write", file_path="/app/site.css", content="x")])[0] == 2


def test_quiet_after_any_look_that_follows_the_edit(tmp_path):
    looks = [use("Read", file_path="/shots/card.png"), use("Bash", command="npx playwright test --headed"),
             use("mcp__browser__take_screenshot")]
    for n, look in enumerate(looks):
        assert stop(tmp_path, [EDIT, look], session=f"look{n}")[0] == 0
    assert stop(tmp_path, [use("Read", file_path="/shots/card.png"), EDIT], session="stale")[0] == 2


def test_quiet_on_non_ui_work_tests_and_continuations(tmp_path):
    assert stop(tmp_path, [use("Edit", file_path="/app/server.py", old_string="a", new_string="b")], session="a")[0] == 0
    assert stop(tmp_path, [use("Edit", file_path="/app/src/Card.test.tsx", old_string="a", new_string="b")], session="b")[0] == 0
    assert stop(tmp_path, [EDIT], active=True, session="c")[0] == 0
    assert stop(tmp_path, [EDIT], session="d", VISUAL_PROOF_DISABLE="1")[0] == 0


def test_bad_input_never_blocks(tmp_path):
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True)
    assert p.returncode == 0
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"transcript_path": str(tmp_path / "missing")}),
                       capture_output=True, text=True)
    assert p.returncode == 0
