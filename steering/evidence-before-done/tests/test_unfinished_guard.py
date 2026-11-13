import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "unfinished-guard"


def run(tool, tool_input, **env):
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True, env={**os.environ, **env})
    return p.returncode, p.stderr


def test_edit_adding_a_stub_is_reported():
    rc, err = run("Edit", {"file_path": "/x/a.rs", "old_string": "fn f() {}", "new_string": "fn f() { todo!() }"})
    assert rc == 2 and "1 stub" in err


def test_edit_adding_a_suppression_is_reported():
    rc, err = run("Edit", {"file_path": "/x/a.py", "old_string": "x = f()", "new_string": "x = f()  # type: ignore"})
    assert rc == 2 and "1 suppression" in err


def test_existing_marker_is_not_blamed_on_the_edit():
    rc, _ = run("Edit", {"file_path": "/x/a.py", "old_string": "# TODO tidy\nx = 1", "new_string": "# TODO tidy\nx = 2"})
    assert rc == 0


def test_removing_a_marker_passes():
    rc, _ = run("Edit", {"file_path": "/x/a.js", "old_string": "it.skip('later', f)", "new_string": "it('now', f)"})
    assert rc == 0


def test_new_file_write_counts_everything(tmp_path):
    f = tmp_path / "new.ts"
    rc, err = run("Write", {"file_path": str(f), "content": "// @ts-ignore\nexport const a = 1\n// TODO wire the real client\n"})
    assert rc == 2 and "1 stub" in err and "1 suppression" in err


def test_clean_code_docs_and_kill_switch_pass():
    assert run("Write", {"file_path": "/x/clean.py", "content": "def f():\n    return 1\n"})[0] == 0
    assert run("Write", {"file_path": "/x/notes.md", "content": "TODO write this"})[0] == 0
    assert run("Edit", {"file_path": "/x/a.rs", "old_string": "", "new_string": "todo!()"}, UNFINISHED_GUARD_DISABLE="1")[0] == 0


def test_multiedit_and_bad_input():
    rc, _ = run("MultiEdit", {"file_path": "/x/a.js", "edits": [{"old_string": "a", "new_string": "it.skip('x', f)"}]})
    assert rc == 2
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True)
    assert p.returncode == 0


def test_ordinary_words_and_idioms_do_not_fire():
    for new in ('<input placeholder="Paste here" />', "if not todo and phase == 'all':", "rm -f x || true",
                "// the placeholder branch still wins", 't.Skip("DATABASE_URL not set")', "stub = make_stub()"):
        assert run("Edit", {"file_path": "/x/a.tsx", "old_string": "", "new_string": new})[0] == 0, new
