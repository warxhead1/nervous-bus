"""export builds a self-contained marketplace that holds only what was asked for."""
import json
import subprocess
import sys
from pathlib import Path

EXPORT = Path(__file__).resolve().parent / "export"
ROOT = Path(__file__).resolve().parents[2]


def run(*args):
    return subprocess.run([sys.executable, str(EXPORT), *args], capture_output=True, text=True)


def test_one_plugin_exports_alone(tmp_path):
    out = tmp_path / "out"
    p = run("plain-prose", str(out))
    assert p.returncode == 0, p.stderr
    m = json.loads((out / ".claude-plugin" / "marketplace.json").read_text())
    assert m["name"] == "plain-prose-standalone"
    assert [e["name"] for e in m["plugins"]] == ["plain-prose"]
    assert m["plugins"][0]["source"] == "./plain-prose"
    assert (out / "plain-prose" / ".claude-plugin" / "plugin.json").is_file()
    assert sorted(x.name for x in out.iterdir() if x.is_dir()) == [".claude-plugin", "plain-prose"]


def test_all_matches_the_repo_marketplace(tmp_path):
    out = tmp_path / "out"
    assert run("--all", str(out)).returncode == 0
    repo = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    got = json.loads((out / ".claude-plugin" / "marketplace.json").read_text())
    assert [e["name"] for e in got["plugins"]] == [e["name"] for e in repo["plugins"]]
    for e in got["plugins"]:
        assert (out / e["name"] / ".claude-plugin" / "plugin.json").is_file()


def test_every_exported_hook_command_stays_inside_its_plugin(tmp_path):
    out = tmp_path / "out"
    assert run("--all", str(out)).returncode == 0
    for hooks in out.glob("*/hooks/hooks.json"):
        for groups in json.loads(hooks.read_text())["hooks"].values():
            for group in groups:
                for h in group["hooks"]:
                    assert "${CLAUDE_PLUGIN_ROOT}" in h["command"], h["command"]
                    assert ".." not in h["command"], h["command"]


def test_unknown_plugin_and_non_empty_target_fail(tmp_path):
    assert run("nope", str(tmp_path / "a")).returncode != 0
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("x")
    assert run("plain-prose", str(tmp_path / "b")).returncode != 0


def test_exported_cue_hook_still_runs(tmp_path):
    out = tmp_path / "out"
    assert run("plain-prose", str(out)).returncode == 0
    p = subprocess.run([sys.executable, str(out / "plain-prose" / "hooks" / "cue")],
                       input=json.dumps({"prompt": "write the README for this"}), capture_output=True, text=True)
    assert p.returncode == 0
