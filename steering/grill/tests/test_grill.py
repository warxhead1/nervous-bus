import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
HOOKS = PLUGIN / "hooks"
SHIM = PLUGIN.parent / "adapters" / "shim"
GUARD = str(HOOKS / "ledgered-ask-guard")
QID = "q-01JD0000000000000000000002"

BARE = 'hearth-ask post --project game --title "Name the crew" --option a:Scrap --option b:Salvage'
LEDGERED = f'hearth-ask post --project game --title "Name the crew" --option a:Scrap --id {QID}'


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "game" / "design" / "ledger").mkdir(parents=True)
    (tmp_path / "game" / "crates").mkdir()
    (tmp_path / "plain").mkdir()
    return tmp_path


def env(**extra):
    e = {k: v for k, v in os.environ.items() if k != "CRUCIBLE_DESIGN_ROOT"}
    e.update(extra)
    return e


def guard(command, cwd, tool="Bash", **extra):
    ev = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    p = subprocess.run([sys.executable, GUARD], input=json.dumps(ev), capture_output=True, text=True, env=env(**extra))
    return p.returncode, p.stderr


def test_blocks_an_unledgered_post_and_names_the_path(repo):
    for cwd in (repo / "game", repo / "game" / "crates"):
        rc, err = guard(BARE, cwd)
        assert rc == 2
        assert "crucible grill ask" in err


def test_allows_the_ledgered_post(repo):
    for cmd in (LEDGERED, LEDGERED.replace(f"--id {QID}", f"--id={QID}"), LEDGERED.replace(QID, QID + ".2"),
                LEDGERED.replace(f"--id {QID}", f'--id "{QID}"'), BARE + f" --id {QID}", BARE + ' --id "$QID"'):
        assert guard(cmd, repo / "game")[0] == 0, cmd


def test_allows_a_post_outside_a_crucible_repo(repo):
    assert guard(BARE, repo / "plain")[0] == 0


def test_a_malformed_id_does_not_count_as_ledgered(repo):
    for cmd in (BARE + " --id codename-1114", BARE + " --id q-123", BARE + " --id q-01JD000000000000000000000I"):
        assert guard(cmd, repo / "game")[0] == 2, cmd


def test_only_posting_is_guarded(repo):
    g = repo / "game"
    for cmd in ("hearth-ask list --status pending", "hearth-ask get q-1", "hearth-ask wait q-1", "hearth-ask withdraw q-1",
                'echo "hearth-ask post --project x"', "git commit -m 'never hearth-ask post raw'",
                "cat > note.md <<'EOF'\nhearth-ask post --project x\nEOF\nls"):
        assert guard(cmd, g)[0] == 0, cmd


def test_finds_the_post_inside_a_pipeline_a_path_or_a_chain(repo):
    g = repo / "game"
    for cmd in (f"cd {g} && {BARE}", f"echo hi; {BARE}", f"/usr/local/bin/{BARE}", f"python3 {BARE}"):
        assert guard(cmd, g)[0] == 2, cmd
    assert guard(f"{LEDGERED} && {BARE}", g)[0] == 2


def test_a_cd_moves_the_check_to_the_directory_it_names(repo):
    assert guard(f"cd {repo / 'game'} && {BARE}", repo / "plain")[0] == 2
    assert guard(f"cd {repo / 'plain'} && {BARE}", repo / "game")[0] == 0
    assert guard(f"cd ../game && {BARE}", repo / "plain")[0] == 2


def test_design_root_variable_marks_a_repo(repo):
    assert guard(BARE, repo / "plain", CRUCIBLE_DESIGN_ROOT=str(repo / "game"))[0] == 2


def test_kill_switch_other_tools_and_bad_input(repo):
    g = repo / "game"
    assert guard(BARE, g, LEDGERED_ASK_GUARD_DISABLE="1")[0] == 0
    assert guard(BARE, g, tool="Edit")[0] == 0
    p = subprocess.run([sys.executable, GUARD], input="nope", capture_output=True, text=True)
    assert p.returncode == 0
    p = subprocess.run([sys.executable, GUARD], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}}),
                       capture_output=True, text=True)
    assert p.returncode == 0


def shim(harness, tool, command, cwd, event):
    ev = {"hook_event_name": event, "tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    p = subprocess.run([sys.executable, str(SHIM), "--harness", harness, "--event", event, GUARD],
                       input=json.dumps(ev), capture_output=True, text=True, env=env())
    return p.returncode, p.stderr


@pytest.mark.parametrize("harness,tool,event", [("claude-code", "Bash", "PreToolUse"), ("codex", "Bash", "PreToolUse"),
                                                ("gemini", "run_shell_command", "BeforeTool")])
def test_each_harness_gets_a_decision_through_the_shim(repo, harness, tool, event):
    rc, err = shim(harness, tool, BARE, repo / "game", event)
    assert rc == 2 and "crucible grill ask" in err
    assert shim(harness, tool, LEDGERED, repo / "game", event)[0] == 0
    assert shim(harness, tool, BARE, repo / "plain", event)[0] == 0


def test_hooks_json_runs_the_guard_on_bash_and_the_cue_on_prompts():
    hooks = json.loads((HOOKS / "hooks.json").read_text())["hooks"]
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "Bash" and pre["hooks"][0]["command"].endswith("/hooks/ledgered-ask-guard")
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("/hooks/cue")
    assert (HOOKS / "ledgered-ask-guard").stat().st_mode & 0o111


def cue(prompt):
    p = subprocess.run([sys.executable, str(HOOKS / "cue")], input=json.dumps({"prompt": prompt}), capture_output=True, text=True)
    return p.stdout


def test_cue_loads_the_skill_for_grill_me_and_design_questions_only():
    assert "crucible grill work" in cue("Grill me on the economy")
    assert "crucible grill ask" in cue("Open design question: should the crew keep half its scrap?")
    assert "Load the grill skill" in cue("grill us on the combat pillars")
    assert not cue("How long should I grill salmon?")
    assert not cue("Fix the failing test in the pagination helper.")


def test_trigger_eval_records_a_load_rate_that_matches_its_runs():
    doc = json.loads((PLUGIN / "evals" / "trigger.json").read_text())
    m = doc["measured"]
    assert m["should"]["n"] == len(doc["should"]) * m["runs_per_prompt"]
    assert m["load_rate"] == round(m["should"]["loaded"] / m["should"]["n"], 4)
    assert 0 <= m["should"]["loaded"] <= m["should"]["n"]
    assert m["should_not"]["n"] == len(doc["should_not"]) * m["runs_per_prompt"]
    assert m["model"] and m["date"]
