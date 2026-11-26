import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
HOOK = PLUGIN / "hooks" / "prose-guard"
STEERING = PLUGIN.parent

# The before and after of the upstream README's own example.
BEFORE = """Great question! Kubernetes liveness probes are a powerful mechanism that enables the kubelet to determine whether a container is healthy — and it's not just a health check, it's your first line of defense. When a probe fails, the container will be automatically restarted by the kubelet, ensuring that your application remains available. However, it's important to note that misconfigured probes could potentially cause cascading restarts, so parameters like `initialDelaySeconds` and `failureThreshold` should be carefully tuned.

Get this right, and your cluster heals itself.
"""
AFTER = """A liveness probe tells the kubelet if a container is healthy. If the probe fails, the kubelet restarts the container. This keeps the application available.

A bad probe can also cause many restarts. For example, a timeout that is too short makes healthy containers fail the probe. To prevent this, set `initialDelaySeconds` and `failureThreshold` to values that fit your application.
"""
# No tell in it: only sentences that carry too much, joined with semicolons.
HEAVY = """The reader gets the whole plan in one breath; the writer saved a period and the reader pays for it; nobody asked for that trade.

The service reads the queue and then it checks the lease and after that it writes the row and then it tells the caller, which waits the whole time because nothing in the design lets the caller go and do other work while the write is still in flight.

The cache holds the last answer; the next call reads it; the one after that finds it stale.
"""


def run(tmp, path, tool="Write", tool_input=None, response=None, session="s1", cwd=None, **env):
    ev = {"session_id": session, "hook_event_name": "PostToolUse", "tool_name": tool,
          "tool_input": {"file_path": str(path), **(tool_input or {})}}
    if response is not None:
        ev["tool_response"] = response
    if cwd:
        ev["cwd"] = str(cwd)
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(ev), capture_output=True, encoding="utf-8",
                       env={**os.environ, "XDG_CACHE_HOME": str(tmp / "cache"), **env})
    return p.returncode, p.stderr


def write(tmp, name, text):
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_reports_the_ai_tells_in_a_new_document(tmp_path):
    rc, err = run(tmp_path, write(tmp_path, "probe.md", BEFORE))
    assert rc == 2
    assert "AI tell" in err and "not just X" in err and "ste80-humanizer" in err


def test_quiet_on_the_rewrite_of_the_same_text(tmp_path):
    assert run(tmp_path, write(tmp_path, "probe.md", AFTER)) == (0, "")


def test_reports_heavy_sentences_that_hold_no_tell(tmp_path):
    rc, err = run(tmp_path, write(tmp_path, "design.md", HEAVY))
    assert rc == 2
    assert "AI tell" not in err and "semicolon" in err and "over 30 words" in err


def test_one_long_sentence_in_a_long_plain_document_is_quiet(tmp_path):
    text = AFTER * 6 + HEAVY.split("\n\n")[1] + "\n"
    assert run(tmp_path, write(tmp_path, "guide.md", text))[0] == 0


@pytest.mark.parametrize("skill", sorted(STEERING.glob("*/skills/*/SKILL.md")), ids=lambda p: p.parent.name)
def test_quiet_on_the_skills_this_repo_ships(tmp_path, skill):
    assert run(tmp_path, write(tmp_path, "SKILL.md", skill.read_text(encoding="utf-8")))[0] == 0


def test_an_edit_is_blamed_only_for_the_lines_it_added(tmp_path):
    path = write(tmp_path, "notes.md", HEAVY + "\nThe retry limit is 5.\n")
    edit = {"old_string": "The retry limit is 3.", "new_string": "The retry limit is 5."}
    assert run(tmp_path, path, "Edit", edit)[0] == 0
    line = len(path.read_text(encoding="utf-8").splitlines())
    patch = [{"oldStart": line, "oldLines": 1, "newStart": line, "newLines": 1,
              "lines": ["-The retry limit is 3.", "+The retry limit is 5."]}]
    assert run(tmp_path, path, "Edit", edit, response={"structuredPatch": patch})[0] == 0


def test_an_edit_that_adds_the_heavy_text_is_reported_with_its_file_line(tmp_path):
    path = write(tmp_path, "notes.md", AFTER + "\n" + HEAVY)
    first = len(AFTER.splitlines()) + 2
    patch = [{"oldStart": first, "oldLines": 0, "newStart": first, "newLines": len(HEAVY.splitlines()),
              "lines": ["+" + l for l in HEAVY.splitlines()]}]
    for response in ({"structuredPatch": patch}, None):
        rc, err = run(tmp_path, path, "Edit", {"old_string": "", "new_string": HEAVY}, response=response,
                      session=f"s-{response is None}")
        assert rc == 2 and f"line {first} " in err


def test_a_phrase_under_discussion_and_code_are_not_tells(tmp_path):
    text = ('Cut the marketing voice: "not just X, but Y", "seamless", "let\'s dive in".\n\n'
            "```\nit's not just a check; it's a gate; let's dive in; utilize it\n```\n\n"
            "    it is not just indented code; it is a seamless tapestry\n\n"
            "> Great question! It's not just a faster build tool, it's a pivotal new way to think.\n")
    assert run(tmp_path, write(tmp_path, "style.md", text))[0] == 0


def test_list_items_that_end_in_a_semicolon_and_label_dashes_are_not_glue(tmp_path):
    text = "\n".join(f"- it trades only liquid names on day {n};" for n in range(8))
    text += "\n\n" + "\n".join(f"- `step{n}` — runs the build" for n in range(8)) + "\n"
    assert run(tmp_path, write(tmp_path, "rules.md", text))[0] == 0


def test_files_that_are_not_documents_or_not_ours_pass(tmp_path):
    for name in ("main.py", "LICENSE.md", "CHANGELOG.md", "node_modules/pkg/README.md", "scratch/notes.md"):
        assert run(tmp_path, write(tmp_path, name, BEFORE))[0] == 0
    assert run(tmp_path, tmp_path / "missing.md")[0] == 0


def test_a_file_is_reported_twice_a_session_at_most(tmp_path):
    path = write(tmp_path, "probe.md", BEFORE)
    assert [run(tmp_path, path)[0] for _ in range(4)] == [2, 2, 0, 0]
    assert run(tmp_path, path, session="s2")[0] == 2


def test_kill_switch_and_bad_input(tmp_path):
    assert run(tmp_path, write(tmp_path, "probe.md", BEFORE), PLAIN_PROSE_DISABLE="1")[0] == 0
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True)
    assert p.returncode == 0 and not p.stderr


# --- commit messages and pull request bodies (PreToolUse on Bash) ---

BODY = HEAVY.replace('"', "'")
PLAIN_BODY = "retry: raise the limit to 5\n\nThe queue drops a job after 3 tries. A slow disk needs 4.\nThe limit is now 5. The test covers a job that passes on try 5.\n"


def bash(tmp, command, event="PreToolUse", session="m1", cwd=None, **env):
    ev = {"session_id": session, "hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": command}}
    if cwd:
        ev["cwd"] = str(cwd)
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(ev), capture_output=True, encoding="utf-8",
                       env={**os.environ, "XDG_CACHE_HOME": str(tmp / "cache"), **env})
    return p.returncode, p.stderr


def heredoc(command, body):
    return f"{command} <<'EOF'\n{body}\nEOF"


def test_a_heavy_commit_message_is_sent_back_once_then_runs(tmp_path):
    cmd = heredoc("git add -A && git commit -q -F -", BODY)
    rc, err = bash(tmp_path, cmd)
    assert rc == 2 and "did not run" in err and "semicolon" in err
    shown = [l for l in err.splitlines() if l.startswith("  line ")]
    assert len(shown) == len(set(shown))                  # a line with two semicolons is shown once
    assert bash(tmp_path, cmd)[0] == 0                    # the same text again: the writer chose it
    assert bash(tmp_path, cmd)[0] == 2                    # a later heavy message is a new report


def test_a_rewrite_after_a_report_runs_even_if_still_heavy(tmp_path):
    assert bash(tmp_path, heredoc("git commit -F -", BODY))[0] == 2
    assert bash(tmp_path, heredoc("git commit -F -", BODY + "\nOne more line; and another; and a third."))[0] == 0


def test_a_plain_commit_message_runs(tmp_path):
    assert bash(tmp_path, heredoc("git commit -F -", PLAIN_BODY)) == (0, "")
    assert bash(tmp_path, 'git commit -m "retry: raise the limit to 5" -m "A slow disk needs 4 tries."')[0] == 0


@pytest.mark.parametrize("command", [
    'gh pr create --title "x" --body "$(cat <<\'EOF\'\n' + BODY + '\nEOF\n)"',
    heredoc("gh pr create --title x --body-file -", BODY),
    heredoc("gh issue comment 12 --body-file -", BODY),
    'git commit -m "' + " ".join(BODY.split()) + '"',
    "gh pr edit 7 --body '" + " ".join(BODY.split()) + "'",
], ids=["pr-body-cat", "pr-body-file", "issue-comment", "commit-m", "pr-edit"])
def test_every_way_of_passing_the_text_is_read(tmp_path, command):
    assert bash(tmp_path, command)[0] == 2


def test_a_message_file_is_read_from_the_working_directory(tmp_path):
    write(tmp_path, "repo/msg.txt", BODY)
    assert bash(tmp_path, "git commit -F msg.txt", cwd=tmp_path / "repo")[0] == 2
    assert bash(tmp_path, "git commit -F missing.txt", cwd=tmp_path / "repo", session="m2")[0] == 0


def test_text_that_is_not_the_message_is_not_scored(tmp_path):
    script = heredoc("python3 -", BODY) + '\ngit commit -q -m "retry: raise the limit to 5"'
    assert bash(tmp_path, script)[0] == 0
    for command in ("git commit --amend --no-edit", "git log --oneline | grep commit", "git status",
                    heredoc("cat > notes.txt", BODY), 'gh pr view 12 --json body'):
        assert bash(tmp_path, command)[0] == 0


def test_the_message_check_runs_before_the_command_only_and_has_a_kill_switch(tmp_path):
    cmd = heredoc("git commit -F -", BODY)
    assert bash(tmp_path, cmd, event="PostToolUse")[0] == 0
    assert bash(tmp_path, cmd, PLAIN_PROSE_DISABLE="1")[0] == 0
    assert bash(tmp_path, cmd)[0] == 2
