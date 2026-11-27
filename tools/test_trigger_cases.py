"""trigger-eval --cases: fixture shape, skill linking and the dry-run plan. No model calls."""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVAL = REPO / "steering" / "tools" / "trigger-eval"
CASES = REPO / "tools" / "trigger_cases"


def run(*args):
    return subprocess.run([sys.executable, str(EVAL), *args], capture_output=True, text=True, timeout=60)


def test_fixtures_have_both_groups_and_both_polarities():
    groups = {}
    for f in CASES.glob("*.json"):
        for case in json.loads(f.read_text()):
            assert case["should"] and case["should_not"], case["skill"]
            groups.setdefault(case["group"], []).append(case["skill"])
    assert set(groups) == {"top-used", "never-seen"}


def test_dry_run_links_only_the_skills_under_test(tmp_path):
    root = tmp_path / "skills"
    for name in ("alpha", "beta", "unused"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([
        {"skill": "alpha", "should": ["a1", "a2"], "should_not": ["n1"]},
        {"skill": "beta", "should": ["b1"], "should_not": []}]))
    p = run("--cases", str(cases), "--skill-root", str(root), "--runs", "3", "--dry-run")
    assert p.returncode == 0, p.stderr
    assert "alpha                    should=2 should_not=1" in p.stdout
    assert "requests: 12 (4 prompts x 3 runs" in p.stdout
    cwd = Path(p.stdout.rsplit("cwd ", 1)[1].splitlines()[0].strip())
    linked = sorted(x.name for x in (cwd / ".claude" / "skills").iterdir())
    assert linked == ["alpha", "beta"]
    assert (cwd / ".claude" / "skills" / "alpha").resolve() == (root / "alpha").resolve()


def test_missing_skill_dir_stops_before_any_call(tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"skill": "ghost", "should": ["x"], "should_not": []}))
    p = run("--cases", str(cases), "--skill-root", str(tmp_path), "--dry-run")
    assert p.returncode != 0
    assert "ghost" in p.stderr


def test_limit_trims_the_plan(tmp_path):
    p = run("--cases", str(CASES), "--limit", "2", "--dry-run")
    if p.returncode != 0:          # a host without these skills installed
        assert "skill dir not found" in p.stderr
        return
    assert "requests: 2 (2 prompts x 1 runs" in p.stdout
