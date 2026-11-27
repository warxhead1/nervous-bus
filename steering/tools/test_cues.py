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
    import importlib.machinery
    import importlib.util
    import io
    import time
    from contextlib import redirect_stdout
    from unittest import mock

    ebd = ROOT / "evidence-before-done" / "hooks" / "cue"
    payload = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                          "tool_input": {"command": "pytest"}, "tool_response": {"stdout": "x" * 100000 + "\n1 failed"}})
    # 1. In-process budget: actual parsing + regex matching over 100KB must stay under 15ms.
    loader = importlib.machinery.SourceFileLoader("cue_perf", str(ebd))
    spec = importlib.util.spec_from_loader("cue_perf", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    in_proc = []
    for _ in range(5):
        t0 = time.perf_counter()
        with mock.patch.object(sys, "stdin", io.StringIO(payload)), redirect_stdout(io.StringIO()):
            assert mod.main() == 0
        in_proc.append((time.perf_counter() - t0) * 1000)
    in_proc.sort()
    assert in_proc[2] < 15.0, f"median in-process cue latency {in_proc[2]:.1f}ms >= 15ms"

    # 2. Subprocess budget: net overhead over bare Python startup stays under 50ms
    # (avoids flaking when shared CI runners have >30ms interpreter startup).
    subprocess.run([sys.executable, "-c", "pass"], capture_output=True, text=True)
    subprocess.run([sys.executable, str(ebd)], input=payload, capture_output=True, text=True)
    base_times, cue_times = [], []
    for _ in range(5):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True, text=True)
        base_times.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        p = subprocess.run([sys.executable, str(ebd)], input=payload, capture_output=True, text=True)
        cue_times.append((time.perf_counter() - t0) * 1000)
        assert p.returncode == 0
    base_times.sort()
    cue_times.sort()
    net_ms = cue_times[2] - base_times[2]
    assert net_ms < 50.0, f"median net cue latency {net_ms:.1f}ms >= 50ms (cue={cue_times[2]:.1f}ms, py={base_times[2]:.1f}ms)"
