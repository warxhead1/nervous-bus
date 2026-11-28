"""Tests for adapters/neon-review against a fake `agy` and a fake `ub-gate` on PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER_DIR = Path(__file__).resolve().parent
CLI = ADAPTER_DIR / "neon-review"

FAKE_AGY = r"""#!/usr/bin/env python3
import json, os, sys, time
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "agy", "argv": argv, "cwd": os.getcwd()}) + "\n")
mode = os.environ.get("FAKE_AGY_MODE", "pass")
if mode == "timeout":
    sys.stdout.write("partial review\n"); sys.stdout.flush()
    time.sleep(30)
if mode == "dead":
    sys.stderr.write("backend down\n"); sys.exit(7)
prompt = argv[argv.index("-p") + 1]
# the diff must be reachable through a file the prompt names
import re
diff_path = re.search(r"full diff: (\S+)", prompt).group(1)
size = os.path.getsize(diff_path)
body = {
 "pass": "Reviewed %d bytes of diff.\n## Spec rows\n- row one: landed (a.py)\nVERDICT: PASS\n",
 "fail": ("Reviewed %d bytes of diff.\n"
          "BLOCKER spans.py:248 drops the done set at offset zero. Fix: use `same`; test offset zero.\n"
          "- HIGH `lib/x.go:10-12` leaks a path\n  into a label. Fix: slug it.\n"
          "MEDIUM none.py:3 bad exit code.\n"
          "## Spec rows\n- row one: not landed\n- row two: landed differently (b.py)\nVERDICT: FAIL\n"),
 "unparsed": "Reviewed %d bytes of diff.\nLooks fine to me.\n",
 "passwithhigh": "Reviewed %d bytes.\nHIGH a.py:1 real bug. Fix: do it.\nVERDICT: PASS\n",
 "headings": "Reviewed %d bytes.\n### BLOCKER\n- a.py:5 first. Fix: one.\n- b.py:6 second. Fix: two.\n### NIT\n- c.py:7 style.\nVERDICT: FAIL\n",
}[mode]
sys.stdout.write(body % size if "%d" in body else body)
"""

FAKE_UB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps({"tool": "ub-gate", "argv": argv, "cwd": os.getcwd()}) + "\n")
if argv[0] == "crap":
    Path(".metrics").mkdir(exist_ok=True)
    print("CRAP Report")
elif argv[0] == "dry":
    Path(".metrics").mkdir(exist_ok=True)
    print("No duplicate candidates found.")
elif argv[0] == "summary":
    print(json.dumps({"crap": {"functions": 3, "over_10": 1}, "dry": {"pairs": 2}}))
"""


def sh(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def only_git(tmp: Path, with_agy: bool = False) -> str:
    d = tmp / "only_git"
    d.mkdir(exist_ok=True)
    link = d / "git"
    if not link.exists():
        link.symlink_to(shutil.which("git"))
    agy = d / "agy"
    if with_agy and not agy.exists():
        agy.symlink_to(tmp / "bin" / "agy")
    return str(d)


@pytest.fixture()
def env(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("agy", FAKE_AGY), ("ub-gate", FAKE_UB)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "base")
    sh(repo, "git", "checkout", "-qb", "feat")
    (repo / "a.py").write_text("x = 2\n")
    (repo / "b.py").write_text("def f():\n    return 1\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "change a and add b\n\nbody claim")
    log = tmp_path / "log.jsonl"
    log.write_text("")
    e = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log),
        "NEON_REVIEW_SCRATCH": str(tmp_path / "scratch"),
    }
    return {"repo": repo, "env": e, "log": log, "tmp": tmp_path}


def review(env, *extra: str, mode: str = "pass", base: str = "main"):
    e = {**env["env"], "FAKE_AGY_MODE": mode}
    p = subprocess.run(
        [str(CLI), "--repo", str(env["repo"]), "--base", base, *extra],
        capture_output=True, text=True, env=e, timeout=60,
    )
    out = json.loads(p.stdout) if p.stdout.strip() else None
    return p.returncode, out, p


def calls(env, tool: str) -> list[dict]:
    rows = [json.loads(line) for line in env["log"].read_text().splitlines() if line]
    return [r for r in rows if r["tool"] == tool]


def test_pass(env):
    rc, out, _ = review(env)
    assert rc == 0
    assert out["verdict"] == "PASS"
    assert out["findings"] == []
    assert out["spec_rows"] == [{"row": "row one", "status": "landed", "detail": "a.py"}]
    raw = Path(out["raw_path"]).read_text()
    assert raw.rstrip().endswith("VERDICT: PASS")
    assert not Path(out["raw_path"]).resolve().is_relative_to(env["repo"])
    (call,) = calls(env, "agy")
    assert call["argv"][call["argv"].index("--model") + 1] == "gemini-4-neon-medium"
    assert Path(call["cwd"]).resolve() == env["repo"].resolve()
    assert calls(env, "ub-gate") == []


def test_fail_parses_findings_and_spec_rows(env):
    rc, out, _ = review(env, mode="fail")
    assert rc == 1
    assert out["verdict"] == "FAIL"
    sev = [f["severity"] for f in out["findings"]]
    assert sev == ["BLOCKER", "HIGH", "MEDIUM"]
    first = out["findings"][0]
    assert first["where"] == "spans.py:248"
    assert first["claim"].startswith("spans.py:248 drops the done set")
    assert first["fix"].startswith("use `same`")
    assert out["findings"][1]["where"] == "lib/x.go:10-12"
    assert out["findings"][1]["fix"] == "slug it"
    assert out["counts"] == {"BLOCKER": 1, "HIGH": 1, "MEDIUM": 1, "NIT": 0}
    assert [r["status"] for r in out["spec_rows"]] == ["not landed", "landed differently"]


def test_heading_sections_make_one_finding_per_item(env):
    rc, out, _ = review(env, mode="headings")
    assert rc == 1
    assert [(f["severity"], f["where"]) for f in out["findings"]] == [
        ("BLOCKER", "a.py:5"), ("BLOCKER", "b.py:6"), ("NIT", "c.py:7"),
    ]


def test_unparsed_is_never_a_pass(env):
    rc, out, _ = review(env, mode="unparsed")
    assert rc == 1
    assert out["verdict"] == "unparsed"
    assert Path(out["raw_path"]).read_text().startswith("Reviewed")


def test_pass_with_high_finding_is_overridden_to_fail(env):
    rc, out, _ = review(env, mode="passwithhigh")
    assert rc == 1
    assert out["verdict"] == "FAIL"
    assert "BLOCKER or HIGH" in out["note"]


def test_timeout_keeps_partial_output_and_exits_1(env):
    rc, out, _ = review(env, "--timeout", "1", mode="timeout")
    assert rc == 1
    assert out["verdict"] == "timeout"
    assert "partial review" in Path(out["raw_path"]).read_text()


def test_agy_dead_without_output_is_exit_3(env):
    rc, out, p = review(env, mode="dead")
    assert rc == 3
    assert "backend down" in out["error"]


def test_agy_missing_is_exit_3(env):
    e = {**env["env"], "PATH": only_git(env["tmp"])}
    p = subprocess.run([sys.executable, "-I", str(ADAPTER_DIR / "neon_review.py"), "--repo", str(env["repo"]), "--base", "main"],
                       capture_output=True, text=True, env=e)
    assert p.returncode == 3


def test_ub_depth_runs_gates_and_hands_json_over(env):
    rc, out, _ = review(env, "--depth", "ub")
    assert rc == 0
    argvs = [c["argv"] for c in calls(env, "ub-gate")]
    assert ["crap", "--no-coverage", "a.py", "b.py"] in argvs
    assert ["dry", "--since", "main", "--against-repo"] in argvs
    assert out["gates"]["crap"]["summary"] == {"functions": 3, "over_10": 1}
    assert out["gates"]["dry"]["summary"] == {"pairs": 2}
    (call,) = calls(env, "agy")
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert "yourself on the two highest-risk" in prompt and "mutate <file>" in prompt
    gates_file = Path(out["work_dir"]) / "ub-gates.json"
    assert json.loads(gates_file.read_text())["dry"]["summary"] == {"pairs": 2}
    assert str(gates_file) in prompt
    assert not (env["repo"] / ".metrics").exists()
    assert sh(env["repo"], "git", "status", "--porcelain") == ""


def test_medium_depth_skips_gates_and_mutate_instruction(env):
    review(env)
    (call,) = calls(env, "agy")
    prompt = call["argv"][call["argv"].index("-p") + 1]
    assert "Their JSON is in" not in prompt
    assert "yourself on the two highest-risk" not in prompt and "ub-gates.json" not in prompt
    assert "gates" not in review(env)[1]


def test_ub_depth_without_ub_gate_is_exit_3(env):
    e = {**env["env"], "PATH": only_git(env["tmp"], with_agy=True)}
    p = subprocess.run([sys.executable, "-I", str(ADAPTER_DIR / "neon_review.py"), "--repo", str(env["repo"]), "--base", "main", "--depth", "ub"],
                       capture_output=True, text=True, env=e)
    assert p.returncode == 3


def test_large_diff_goes_through_a_file_not_argv(env):
    repo = env["repo"]
    (repo / "big.txt").write_text("".join(f"line {i} " + "x" * 80 + "\n" for i in range(20000)))
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "big")
    rc, out, _ = review(env)
    assert rc == 0
    (call,) = calls(env, "agy")
    argv_bytes = sum(len(a) for a in call["argv"])
    assert argv_bytes < 30_000
    diff = Path(out["work_dir"]) / "branch.diff"
    assert diff.stat().st_size > 1_500_000
    assert "line 19999 " in diff.read_text()
    assert str(diff) in call["argv"][call["argv"].index("-p") + 1]
    assert f"Reviewed {diff.stat().st_size} bytes" in Path(out["raw_path"]).read_text()


def test_spec_and_log_reach_agy_by_file_and_out_is_honoured(env, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("- item alpha\n- item beta\n")
    out_file = tmp_path / "o" / "raw.txt"
    rc, out, _ = review(env, "--spec", str(spec), "--out", str(out_file))
    assert rc == 0
    assert out["raw_path"] == str(out_file)
    work = Path(out["work_dir"])
    assert "item beta" in (work / "spec.md").read_text()
    assert "body claim" in (work / "commits.txt").read_text()
    assert "b.py" in (work / "diffstat.txt").read_text()
    prompt = (work / "prompt.txt").read_text()
    assert str(work / "spec.md") in prompt and "item beta" not in prompt
    assert "Adversarial review rubric" in prompt


@pytest.mark.parametrize("args", [["--base", "nope"], ["--base", "main", "--spec", "/no/such"], ["--base", "main", "--timeout", "0"]])
def test_usage_errors_exit_2(env, args):
    p = subprocess.run([str(CLI), "--repo", str(env["repo"]), *args], capture_output=True, text=True, env=env["env"])
    assert p.returncode == 2


def test_missing_required_flag_exits_2(env):
    p = subprocess.run([str(CLI), "--repo", str(env["repo"])], capture_output=True, text=True, env=env["env"])
    assert p.returncode == 2


def test_scratch_inside_repo_is_refused(env):
    e = {**env["env"], "NEON_REVIEW_SCRATCH": str(env["repo"] / "scratch")}
    p = subprocess.run([str(CLI), "--repo", str(env["repo"]), "--base", "main"], capture_output=True, text=True, env=e)
    assert p.returncode == 2


def test_out_inside_repo_is_refused_and_leaves_no_file(env):
    leak = env["repo"] / "leaked.txt"
    rc, out, _ = review(env, "--out", str(leak))
    assert rc == 2
    assert not leak.exists()
    assert sh(env["repo"], "git", "status", "--porcelain") == ""


def test_bad_ub_summary_is_recorded_not_silent(env):
    bad = env["tmp"] / "bin" / "ub-gate"
    bad.write_text(bad.read_text().replace('print(json.dumps({"crap"', 'print("not json"); sys.exit(1); print(json.dumps({"crap"'))
    rc, out, _ = review(env, "--depth", "ub")
    assert out["gates"]["summary_error"]
    assert out["gates"]["crap"].get("summary") is None


def test_renamed_and_edited_file_is_a_changed_source(env):
    repo = env["repo"]
    sh(repo, "git", "mv", "b.py", "c.py")
    (repo / "c.py").write_text("def f():\n    return 2\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "rename and edit")
    review(env, "--depth", "ub")
    crap = [c["argv"] for c in calls(env, "ub-gate") if c["argv"][0] == "crap"]
    assert crap == [["crap", "--no-coverage", "a.py", "c.py"]]


def test_prose_starting_with_spec_does_not_end_findings():
    sys.path.insert(0, str(ADAPTER_DIR))
    import neon_review

    text = "Spec was not provided.\nBLOCKER foo.py:10 sql injection. Fix: parameterize.\nVERDICT: PASS\n"
    got = neon_review.parse_findings(text)
    assert [(f["severity"], f["where"], f["fix"]) for f in got] == [("BLOCKER", "foo.py:10", "parameterize")]


def test_heading_with_inline_finding_is_kept_and_later_headings_close_spec():
    sys.path.insert(0, str(ADAPTER_DIR))
    import neon_review

    text = "### BLOCKER foo.py:10 drops cursor. Fix: keep it.\n## Spec rows\n- a: landed (x.py)\n## Notes\n- not landed in prose\nVERDICT: FAIL\n"
    got = neon_review.parse_findings(text)
    assert [(f["severity"], f["where"]) for f in got] == [("BLOCKER", "foo.py:10")]
    assert [r["row"] for r in neon_review.parse_spec_rows(text)] == ["a"]
