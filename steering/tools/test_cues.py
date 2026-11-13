"""Every plugin's cues fire on its own should-trigger prompts and stay quiet on the rest."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = sorted(p.parents[1] for p in ROOT.glob("*/hooks/cues.json"))


def say(plugin, prompt):
    p = subprocess.run([sys.executable, str(plugin / "hooks" / "cue")], input=json.dumps({"prompt": prompt}),
                       capture_output=True, text=True)
    assert p.returncode == 0
    return p.stdout.strip()


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.name)
def test_cues_cover_the_eval_prompts(plugin):
    cases = json.loads((plugin / "evals" / "trigger.json").read_text())
    for q in cases["should"]:
        assert say(plugin, q), f"no cue for: {q}"
    for q in cases["should_not"]:
        assert not say(plugin, q), f"cue fired on: {q}"


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.name)
def test_cues_stay_quiet_on_other_plugins_negatives_and_bad_input(plugin):
    for other in ROOT.glob("*/evals/trigger.json"):
        for q in json.loads(other.read_text())["should_not"]:
            assert not say(plugin, q), f"{plugin.name} fired on: {q}"
    p = subprocess.run([sys.executable, str(plugin / "hooks" / "cue")], input="nope", capture_output=True, text=True)
    assert p.returncode == 0 and not p.stdout
