"""The catalog lists real pieces, and every listed check runs alone from a temp dir with no repo around it."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ST = Path(__file__).resolve().parents[1]
ROOT = ST.parent
EXPORT = ST / "tools" / "export"
CATALOG = json.loads((ST / "catalog.json").read_text())
PIECES = CATALOG["pieces"]
CHECKS = [p for p in PIECES if p["kind"] == "check"]
RAW = CATALOG["raw_base"]


def test_generated_files_are_current():
    p = subprocess.run([sys.executable, str(ST / "tools" / "catalog"), "--check"], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


def test_every_piece_path_exists_and_url_matches():
    ids = [p["id"] for p in PIECES]
    assert len(ids) == len(set(ids))
    for p in PIECES:
        assert (ROOT / p["path"]).exists(), p["id"]
        assert p["url"] == RAW + p["path"], p["id"]
        for f in p.get("files", []) + p["requires"]:
            assert (ROOT / f).is_file(), (p["id"], f)
        assert p["purpose"].strip(), p["id"]


def test_each_plugin_lists_a_ruling_a_skill_a_check_and_an_adapter_for_every_harness():
    plugins = {p["plugin"] for p in PIECES if p["plugin"]}
    assert len(plugins) == 10
    hooked = {name for name in plugins if (ST / name / "hooks").is_dir()}
    assert len(hooked) >= 9
    for name in plugins:
        kinds = {p["kind"] for p in PIECES if p["plugin"] == name}
        assert {"ruling", "skill"} <= kinds, name
    for name in hooked:
        kinds = {p["kind"] for p in PIECES if p["plugin"] == name}
        assert {"check", "adapter"} <= kinds, name
        for h in ("claude-code", "codex", "gemini", "kiro"):
            assert any(p["id"].startswith(f"adapter/{h}/{name}") for p in PIECES), (h, name)


def test_llms_txt_lists_every_piece_url():
    text = (ST / "llms.txt").read_text()
    for p in PIECES:
        assert (p.get("entry") and RAW + p["entry"] or p["url"]) in text, p["id"]


def test_adapter_configs_point_at_real_checks():
    for p in PIECES:
        if p["kind"] != "adapter" or not p["plugin"]:
            continue
        text = (ROOT / p["path"]).read_text()
        for line in text.splitlines():
            if "$STEERING_HOME/" + p["plugin"] + "/" in line:
                rel = line.split("$STEERING_HOME/")[-1].split('"')[0].split("\\")[0]
                assert (ST / rel).is_file(), (p["id"], rel)


def run_steps(case, root, tmp):
    for name, body in case.get("files", {}).items():
        (tmp / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp / name).write_text(body)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CLAUDE_PLUGIN_ROOT")}
    env.update({k: v.replace("{TMP}", str(tmp)) for k, v in case.get("env", {}).items()})
    for step in case["steps"]:
        args = [a.replace("{TMP}", str(tmp)) for a in step.get("args", [])]
        stdin = json.dumps(step["stdin"]).replace("{TMP}", json.dumps(str(tmp))[1:-1]) if "stdin" in step else None
        yield step, subprocess.run([sys.executable, str(root), *args], input=stdin, capture_output=True, text=True,
                                   cwd=tmp, env=env)


@pytest.mark.parametrize("piece", [c for c in CHECKS if c["smoke"]] + [p for p in PIECES if p["id"] == "adapter/shim"],
                         ids=lambda p: p["id"])
def test_check_runs_alone_with_no_repo_around_it(piece, tmp_path):
    out, work = tmp_path / "piece", tmp_path / "work"
    work.mkdir()
    p = subprocess.run([sys.executable, str(EXPORT), "--piece", piece["id"], str(out)], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    script = out / Path(piece["path"]).relative_to("steering")
    assert not (tmp_path / ".git").exists() and ROOT not in script.parents
    for step, r in run_steps(piece["smoke"], script, work):
        if step.get("exit") is not None:
            assert r.returncode == step["exit"], (step, r.stdout, r.stderr)
        if "stdout_contains" in step:
            assert step["stdout_contains"] in r.stdout, (step, r.stdout, r.stderr)


def test_every_check_has_a_smoke_case():
    assert all(c["smoke"] for c in CHECKS)


def test_export_one_ruling_and_one_skill(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert subprocess.run([sys.executable, str(EXPORT), "--piece", "keep-moving/ruling", str(a)]).returncode == 0
    assert [f.name for f in a.rglob("*") if f.is_file()] == ["keep-moving.md"]
    assert subprocess.run([sys.executable, str(EXPORT), "--piece", "plain-prose/skill", str(b)]).returncode == 0
    assert (b / "plain-prose" / "skills" / "ste80-humanizer" / "SKILL.md").is_file()


def test_shim_maps_gemini_and_codex_events(tmp_path):
    shim = ST / "adapters" / "shim"
    guard = ST / "remediation-ladder" / "hooks" / "bypass-guard"
    ev = {"hook_event_name": "BeforeTool", "tool_name": "run_shell_command", "tool_input": {"command": "git commit --no-verify -m x"}}
    r = subprocess.run([sys.executable, str(shim), "--harness", "gemini", str(guard)], input=json.dumps(ev), capture_output=True, text=True)
    assert r.returncode == 2
    ev = {"hook_event_name": "PreToolUse", "tool_name": "shell", "tool_input": {"cmd": "git commit -m ok"}}
    r = subprocess.run([sys.executable, str(shim), "--harness", "kiro", str(guard)], input=json.dumps(ev), capture_output=True, text=True)
    assert r.returncode == 0
    ev = {"toolName": "bash", "input": {"command": "HUSKY=0 git commit -m x"}}
    r = subprocess.run([sys.executable, str(shim), "--harness", "omp", "--event", "PreToolUse", str(guard)], input=json.dumps(ev), capture_output=True, text=True)
    assert r.returncode == 2


def test_shim_wraps_cue_text_as_json_for_gemini(tmp_path):
    shim = ST / "adapters" / "shim"
    cue = ST / "keep-moving" / "hooks" / "cue"
    prompt = json.loads((ST / "keep-moving" / "evals" / "trigger.json").read_text())["should"][0]
    r = subprocess.run([sys.executable, str(shim), "--harness", "gemini", "--event", "BeforeAgent", str(cue)],
                       input=json.dumps({"prompt": prompt}), capture_output=True, text=True)
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["additionalContext"].startswith("[keep-moving]")
