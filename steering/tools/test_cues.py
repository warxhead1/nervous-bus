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


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.name)
def test_cues_ignore_text_the_harness_wrote(plugin):
    """A cue answers the person; a notification that happens to contain the cue's words is not them."""
    cases = json.loads((plugin / "evals" / "trigger.json").read_text())
    q = cases["should"][0]
    assert say(plugin, q)
    assert not say(plugin, f"[SYSTEM NOTIFICATION - NOT USER INPUT]\n{q}")
    assert not say(plugin, f"<task-notification><summary>{q}</summary></task-notification>")
    assert not say(plugin, f"<system-reminder>\n{q}\n</system-reminder>")
    assert not say(plugin, f"This session is being continued from a previous conversation. {q}")
    assert say(plugin, f"<system-reminder>context</system-reminder>\n{q}")


def post(plugin, tool_name, tool_input=None, tool_response=None):
    ev = {"hook_event_name": "PostToolUse", "tool_name": tool_name,
          "tool_input": tool_input or {}, "tool_response": tool_response or ""}
    p = subprocess.run([sys.executable, str(plugin / "hooks" / "cue")], input=json.dumps(ev),
                       capture_output=True, text=True)
    assert p.returncode == 0
    return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""


def test_post_tool_use_cues_fire_on_traps_and_stay_quiet_on_clean_output():
    ebd = ROOT / "evidence-before-done"
    assert "[evidence-before-done]" in post(ebd, "Bash", {"command": "pytest"}, {"stdout": "FAILED tests/test_pricing.py::test_round_half_up\n1 failed, 2 passed"})
    assert not post(ebd, "Bash", {"command": "pytest"}, {"stdout": "3 passed in 0.12s"})

    rl = ROOT / "remediation-ladder"
    assert "[remediation-ladder]" in post(rl, "Bash", {"command": "./check.sh"}, {"stdout": "shop/shipping.py:2:5: C901 `shipping_cost` is too complex (14 > 12)"})
    assert "[remediation-ladder]" in post(rl, "Edit", {"file_path": "shop/shipping.py", "old_string": "def f():", "new_string": "def f():  # noqa: C901"})
    assert not post(rl, "Edit", {"file_path": "shop/shipping.py", "old_string": "def f():  # noqa: C901", "new_string": "def f():"})
    assert not post(rl, "Bash", {"command": "./check.sh"}, {"stdout": "All checks passed!"})

    km = ROOT / "keep-moving"
    assert "[keep-moving]" in post(km, "Read", {"file_path": "build.sh"}, {"content": "# Full release build. Slow: about 90 seconds.\nsleep 22\n"})
    assert "[keep-moving]" in post(km, "Bash", {"command": "./build.sh"}, {"stdout": "Command running in background"})
    assert not post(km, "Bash", {"command": "ls"}, {"stdout": "README.md\nshop/\n"})


def test_post_tool_use_cues_are_fast():
    import time
    ebd = ROOT / "evidence-before-done" / "hooks" / "cue"
    payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "pytest"}, "tool_response": {"stdout": "x" * 100000 + "\n1 failed"}})
    # Warm import cache, then measure median of 5 runs
    subprocess.run([sys.executable, str(ebd)], input=payload, capture_output=True, text=True)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, str(ebd)], input=payload, capture_output=True, text=True)
        times.append((time.perf_counter() - t0) * 1000)
        assert p.returncode == 0
    times.sort()
    assert times[2] < 50.0, f"median cue latency {times[2]:.1f}ms >= 50ms"
