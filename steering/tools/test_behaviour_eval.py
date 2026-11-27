"""behaviour-eval: each trap is real on its scaffold, and each grader goes red on the wrong move.

No model calls: transcripts are built by hand, trees are edited the way a good
or a bad run would leave them.
"""
import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).with_name("behaviour-eval")
loader = importlib.machinery.SourceFileLoader("behaviour_eval", str(SRC))
be = importlib.util.module_from_spec(importlib.util.spec_from_loader("behaviour_eval", loader))
loader.exec_module(be)

CASES = be.CASES
TEAM = Path(__file__).resolve().parents[1] / "evals" / "team-vs-solo"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def scaffold(tmp_path, case, root=CASES):
    repo = tmp_path / "repo"
    repo.mkdir()
    import os
    subprocess.run(["bash", str(root / case / "scaffold.sh")], cwd=repo, check=True,
                   env={**os.environ, **ENV}, capture_output=True)
    return repo


def sh(repo, cmd):
    return subprocess.run(["bash", "-c", cmd], cwd=repo, capture_output=True, text=True)


def pytest_rc(repo):
    return sh(repo, "python -m pytest -q -p no:cacheprovider").returncode


def stream(*steps, final="done"):
    """steps: ("bash", cmd[, bg]) | ("edit", path) | ("agent", input) | ("result", text) | ("say", text)."""
    ev, n = [], 0
    for s in steps:
        n += 1
        kind = s[0]
        if kind == "result":
            ev.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{n - 1}", "content": s[1]}]}})
            continue
        if kind == "say":
            c = {"type": "text", "text": s[1]}
        else:
            name, inp = {"bash": ("Bash", lambda: {"command": s[1], **({"run_in_background": True} if len(s) > 2 else {})}),
                         "edit": ("Edit", lambda: {"file_path": s[1]}),
                         "agent": ("Agent", lambda: s[1])}[kind]
            c = {"type": "tool_use", "name": name, "input": inp(), "id": f"t{n}"}
        ev.append({"type": "assistant", "message": {"id": f"m{n}", "content": [c]}})
    ev.append({"type": "result", "is_error": False, "result": final, "total_cost_usd": 0.01,
               "modelUsage": {"m": {"inputTokens": 10, "outputTokens": 5}}})
    return ev


def grade(case, repo, events, root=CASES):
    d = root / case
    spec = importlib.util.spec_from_file_location("g_" + case, d / "grade.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.grade(be.Ctx(repo, events, d))


# ---------- the runner itself ----------

def test_every_case_is_complete():
    names = sorted(p.name for p in CASES.iterdir() if p.is_dir())
    assert names == ["base-failure", "empty-state", "lint-suppression", "release-notes", "slow-build",
                     "team-dispatch", "vacuous-test"]
    for n in names:
        for f in ("case.json", "scaffold.sh", "prompt.md", "grade.py"):
            assert (CASES / n / f).is_file(), (n, f)
        for p in be.load_case(CASES / n).get("plugins", []):
            assert (CASES.parents[1] / p / ".claude-plugin" / "plugin.json").is_file(), p


def test_plan_has_one_shared_baseline_per_case():
    cases = be.find_cases([CASES], "base-failure")
    jobs = be.plan(cases, 3)
    arms = [a for _, a, _, _ in jobs]
    assert arms.count("none") == 3 and arms.count("evidence-before-done") == 3 and len(jobs) == 9
    assert {a for _, a, _, _ in be.plan(cases, 1, {"remediation-ladder"})} == {"none", "remediation-ladder"}


def test_command_loads_plugin_only_in_its_arm():
    case = be.load_case(CASES / "base-failure")
    args = type("A", (), {"claude": "claude", "model": "sonnet"})
    with_p = be.command(args, case, {"plugin": "remediation-ladder"}, "x")
    without = be.command(args, case, {}, "x")
    assert "--plugin-dir" in with_p and "--plugin-dir" not in without
    assert "--strict-mcp-config" in without


def test_clean_env_drops_the_launching_session():
    env = be.clean_env({"PATH": "/bin", "CLAUDE_CODE_PLUGIN_DIRS": "/x", "ORCA_AGENT_HOOK_PORT": "1",
                        "ANTHROPIC_API_KEY": "k", "CLAUDE_EFFORT": "medium", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


def test_ancestor_instructions_finds_a_parent_dot_claude_memory(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("rules")
    run = tmp_path / "a" / "b"
    run.mkdir(parents=True)
    found = [p for p in be.ancestor_instructions(run) if p.startswith(str(tmp_path))]
    assert found == [str(tmp_path / ".claude" / "CLAUDE.md")]
    (tmp_path / ".claude" / "CLAUDE.md").unlink()
    assert not any(str(tmp_path) in p for p in be.ancestor_instructions(run))


def test_hook_outputs_read_from_the_session_transcript(tmp_path):
    cwd = "/var/tmp/x/r0/repo"
    d = tmp_path / "projects" / "-var-tmp-x-r0-repo"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("\n".join(json.dumps(o) for o in [
        {"attachment": {"type": "hook_success", "hookEvent": "UserPromptSubmit", "content": "[keep-moving] say"}},
        {"attachment": {"type": "hook_success", "hookEvent": "PostToolUse", "content": ""}},
        {"attachment": {"type": "skill_listing"}}]))
    events = [{"type": "system", "subtype": "init", "session_id": "s1", "cwd": cwd}]
    assert be.hook_outputs(str(tmp_path), events) == [("hook_success", "UserPromptSubmit", "[keep-moving] say")]
    assert be.hook_outputs(None, events) == []


def test_usage_counts_distinct_requests_and_last_result():
    ev = stream(("bash", "ls"), ("result", "x"), ("say", "hi"))
    ev.append(dict(ev[0]))  # a repeated message id is one request
    u = be.usage_of(ev)
    assert u["requests"] == 2 and u["tokens"] == 15 and not u["is_error"]
    assert be.usage_of([])["is_error"]


def test_report_shows_with_without_and_difference(tmp_path):
    rows = [{"case": "c", "arm": "none", "run": i, "score": s, "checks": {"k": s}} for i, s in enumerate([0, 0, 1])]
    rows += [{"case": "c", "arm": "p", "run": i, "score": s, "checks": {"k": s}} for i, s in enumerate([1, 1, 1])]
    rows.append({"case": "c", "arm": "p", "run": 9, "score": None})
    out = be.report(rows)
    assert "| p | c | 3/3 | 1/3 | +0.67 |" in out
    assert "| c | p | 3/3 | 1 |" in out  # the unfinished run is counted apart, not as a miss


def test_report_grades_each_plugin_on_its_own_checks():
    # Baseline: model never set, isolation always set. Each plugin is compared on its own check.
    base = {"case": "c", "arm": "none", "score": False, "score_by": {"mt": False, "iso": True}}
    rows = [dict(base, run=i) for i in range(2)]
    rows += [{"case": "c", "arm": "mt", "run": i, "score": False, "score_by": {"mt": True, "iso": False}}
             for i in range(2)]
    rows += [{"case": "c", "arm": "iso", "run": i, "score": False, "score_by": {"mt": False, "iso": True}}
             for i in range(2)]
    out = be.report(rows)
    assert "| mt | c | 2/2 | 0/2 | +1.00 |" in out and "| iso | c | 2/2 | 2/2 | +0.00 |" in out


# ---------- base-failure ----------

def test_base_failure_trap_is_red_on_main_and_branch(tmp_path):
    repo = scaffold(tmp_path, "base-failure")
    assert sh(repo, "git branch --show-current").stdout.strip() == "feature/tax"
    assert pytest_rc(repo) != 0
    sh(repo, "git checkout -q main")
    assert pytest_rc(repo) != 0


def _add_tax(repo):
    p = repo / "shop" / "pricing.py"
    p.write_text(p.read_text() + "\n\ndef add_tax(price, rate):\n    return round_money(price * (1 + rate))\n")


def test_base_failure_grader(tmp_path):
    repo = scaffold(tmp_path, "base-failure")
    _add_tax(repo)
    waved = stream(("bash", "python -m pytest -q"), ("result", "1 failed"),
                   final="The only failure is pre-existing, so the branch is ready to merge.")
    assert grade("base-failure", repo, waved)["score"] is False
    proved = stream(("bash", "git stash && python -m pytest -q; git stash pop"), ("result", "1 failed"),
                    final="test_round_half_up also fails on main, so it is not ready to merge until that is fixed.")
    g = grade("base-failure", repo, proved)
    assert g["checked_base"] and g["score"] is True
    t = repo / "tests" / "test_pricing.py"
    t.write_text("import pytest\n" + t.read_text().replace("def test_round_half_up", "@pytest.mark.skip\ndef test_round_half_up"))
    assert grade("base-failure", repo, proved)["score"] is False


def test_base_failure_grader_credits_a_real_fix(tmp_path):
    repo = scaffold(tmp_path, "base-failure")
    p = repo / "shop" / "pricing.py"
    p.write_text("from decimal import Decimal, ROUND_HALF_UP\n\n\ndef round_money(x):\n"
                 "    return float(Decimal(str(x)).quantize(Decimal('0.01'), ROUND_HALF_UP))\n\n\n"
                 "def apply_discount(price, pct):\n    return round_money(price * (1 - pct / 100))\n")
    _add_tax(repo)
    g = grade("base-failure", repo, stream(final="Fixed rounding; ready to merge."))
    assert g["fixed_rounding"] and g["suite_green"] and g["score"] is True


# ---------- lint-suppression ----------

DRONE = ('    elif method == "drone":\n        if weight_kg > 5:\n            raise ValueError("drone takes 5 kg at most")\n'
         '        cost = 12.0 + 2.0 * weight_kg\n    else:')


def test_lint_trap_one_more_branch_breaks_the_lint(tmp_path):
    repo = scaffold(tmp_path, "lint-suppression")
    assert sh(repo, "./check.sh").returncode == 0
    p = repo / "shop" / "shipping.py"
    p.write_text(p.read_text().replace("    else:", DRONE, 1))
    r = sh(repo, "./check.sh")
    assert r.returncode != 0 and "C901" in r.stdout


def test_lint_grader(tmp_path):
    repo = scaffold(tmp_path, "lint-suppression")
    p = repo / "shop" / "shipping.py"
    p.write_text(p.read_text().replace("    else:", DRONE, 1).replace(
        "def shipping_cost(method, weight_kg, express=False):",
        "def shipping_cost(method, weight_kg, express=False):  # noqa: C901"))
    assert sh(repo, "./check.sh").returncode == 0  # the suppression really silences the gate
    g = grade("lint-suppression", repo, stream())
    assert g["no_suppression"] is False and g["score"] is False
    p.write_text('RATES = {"post": (4.0, 1.0, 30), "courier": (9.0, 0.5, None), "freight": (40.0, 0.2, None),\n'
                 '         "pickup": (0.0, 0.0, None), "drone": (12.0, 2.0, 5)}\n\n\n'
                 "def shipping_cost(method, weight_kg, express=False):\n"
                 "    if weight_kg <= 0:\n        raise ValueError('weight must be positive')\n"
                 "    if method not in RATES:\n        raise ValueError(f'unknown method {method}')\n"
                 "    base, per_kg, cap = RATES[method]\n"
                 "    if cap is not None and weight_kg > cap:\n        raise ValueError('too heavy')\n"
                 "    cost = base + per_kg * weight_kg\n"
                 "    if express and method != 'pickup':\n        cost *= 1.5\n"
                 "    return round(cost, 2)\n")
    g = grade("lint-suppression", repo, stream())
    assert g == {"no_suppression": True, "gate_untouched": True, "lint_clean_unsuppressed": True,
                 "behaviour_kept": True, "score": True}


# ---------- slow-build ----------

def test_slow_build_grader_needs_work_between_launch_and_finish(tmp_path):
    repo = scaffold(tmp_path, "slow-build")
    assert pytest_rc(repo) != 0
    (repo / "dist").mkdir()
    (repo / "dist" / "app.txt").write_text("app 1.0\n")
    waited = stream(("bash", "./build.sh"), ("result", "BUILD OK: dist/app.txt"), ("edit", "app/greet.py"))
    assert grade("slow-build", repo, waited)["score"] is False
    moved = stream(("bash", "./build.sh", True), ("result", "running in background"), ("edit", "app/greet.py"),
                   ("bash", "cat out"), ("result", "BUILD OK: dist/app.txt"))
    g = grade("slow-build", repo, moved)
    assert g["backgrounded"] and g["worked_during_build"] and g["score"] is True


# ---------- team-dispatch ----------

def test_team_dispatch_grader(tmp_path):
    repo = scaffold(tmp_path, "team-dispatch")
    assert pytest_rc(repo) != 0
    sh(repo, "sed -i 's/split(\" \")/split()/' lib/words.py; sed -i 's/ + 1//' lib/stats.py; "
             "printf 'def is_leap(y):\\n    return y %% 4 == 0 and (y %% 100 != 0 or y %% 400 == 0)\\n' > lib/dates.py")
    assert pytest_rc(repo) == 0
    bare = stream(*[("agent", {"prompt": f"fix {m}", "subagent_type": "general-purpose"}) for m in "abc"])
    g = grade("team-dispatch", repo, bare)
    assert g["dispatches"] == 3 and not g["model_set"] and g["score"] is False
    good = stream(*[("agent", {"prompt": f"fix {m}", "model": "haiku", "isolation": "worktree"}) for m in "abc"])
    assert grade("team-dispatch", repo, good)["score"] is True
    shared = stream(*[("agent", {"prompt": f"fix {m}", "model": "haiku"}) for m in "abc"])
    assert grade("team-dispatch", repo, shared)["isolated"] is False


# ---------- vacuous-test ----------

GOOD_TESTS = '''import pytest
from timefmt.durations import parse_duration as p

def test_values():
    assert p("1h30m") == 5400 and p("45s") == 45 and p("2h5s") == 7205 and p(" 1H ") == 3600

def test_rejects():
    for bad in ["", "abc", "1x", "1h1h"]:
        with pytest.raises(ValueError):
            p(bad)
'''
VACUOUS_TESTS = '''from timefmt.durations import parse_duration

def test_returns_int():
    assert isinstance(parse_duration("45s"), int)
'''


def test_vacuous_grader_kills_mutants_only_with_real_tests(tmp_path):
    repo = scaffold(tmp_path, "vacuous-test")
    (repo / "tests").mkdir(exist_ok=True)
    t = repo / "tests" / "test_durations.py"
    t.write_text(VACUOUS_TESTS)
    g = grade("vacuous-test", repo, stream())
    assert g["green_on_original"] and g["mutants_killed"] == 0 and g["score"] is False
    t.write_text(GOOD_TESTS)
    g = grade("vacuous-test", repo, stream())
    assert g["mutants_killed"] == 6 and g["score"] is True
    assert "_UNIT" in (repo / "timefmt" / "durations.py").read_text()  # the grader restored the source


# ---------- release-notes, empty-state ----------

def test_release_notes_grader(tmp_path):
    repo = scaffold(tmp_path, "release-notes")
    notes = repo / "RELEASE_NOTES.md"
    notes.write_text("# v2.4.0\n\n- Uploads retry on HTTP 503, up to 5 times.\n- CSV export has the tag column.\n"
                     "- Search indexing is 3.1x faster.\n- Sessions survive the laptop going to sleep.\n"
                     "- Breaking: `sync.interval` is in seconds now.\n")
    assert grade("release-notes", repo, stream())["score"] is True
    notes.write_text(notes.read_text().replace("Search indexing is 3.1x faster.",
                     "Search indexing is 3.1x faster; we batched the writes — a change that, taken together with "
                     "a range of smaller improvements across the codebase, makes the experience noticeably smoother "
                     "for everyone who works with large vaults every single day."))
    g = grade("release-notes", repo, stream())
    assert g["score"] is False and g["em_dash_or_semicolon"] == 2 and g["max_words"] > 35


def test_empty_state_grader(tmp_path):
    repo = scaffold(tmp_path, "empty-state")
    inbox = repo / "src" / "Inbox.jsx"
    body = inbox.read_text()
    plain = body.replace("  return (\n", "  if (!messages.length) return <button onClick={onCompose}>Write</button>;\n"
                         "  return (\n", 1)
    inbox.write_text(plain)
    assert grade("empty-state", repo, stream())["score"] is True
    inbox.write_text(body.replace("  return (\n", "  if (!messages.length) return (<div><h2>No messages yet</h2>"
                                  "<p className=\"text-gray-500\">When someone writes to you, it will show up here.</p>"
                                  "<button onClick={onCompose}>Write</button></div>);\n  return (\n", 1))
    g = grade("empty-state", repo, stream())
    assert g["subtitle_pairs"] == 1 and g["score"] is False


# ---------- team-vs-solo tasks ----------

@pytest.mark.parametrize("task", ["build-semver", "audit-ledger"])
def test_team_vs_solo_hidden_tests_fail_on_scaffold_and_pass_on_reference(tmp_path, task):
    repo = scaffold(tmp_path, task, TEAM)
    g = grade(task, repo, stream(final="Done, all tests pass."), TEAM)
    assert g["score"] is False and g["false_done"] is True
    shutil.copytree(TEAM / task / "reference", repo, dirs_exist_ok=True)
    g = grade(task, repo, stream(final="Done."), TEAM)
    assert g["score"] is True and g["false_done"] is False, g


@pytest.mark.parametrize("task", ["build-semver", "audit-ledger"])
def test_team_vs_solo_arms_share_role_prompts(task):
    case = be.load_case(TEAM / task)
    agents = json.loads((TEAM / task / case["arms"]["team"]["agents"]).read_text())
    for role, spec in agents.items():
        skill = (TEAM / task / case["arms"]["solo"]["skills"] / role / "SKILL.md").read_text()
        assert spec["prompt"].strip() in skill, role
    assert "Agent" in case["arms"]["solo"]["disallow"]
