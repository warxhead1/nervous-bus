#!/usr/bin/env python3
"""neon-review: adversarial branch review through `agy -p`, as JSON.

JSON on stdout, errors on stderr.
Exit codes: 0 PASS, 1 negative result (FAIL, unparsed, timeout), 2 usage,
3 backend unreachable (agy or, at ub depth, ub-gate missing or failing to start).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3

DEFAULT_MODEL = "gemini-4-neon-medium"
DEFAULT_TIMEOUT_S = 1800
GATE_TIMEOUT_S = 600
RUBRIC = Path(__file__).resolve().parent / "rubric.md"
SEVERITIES = ("BLOCKER", "HIGH", "MEDIUM", "NIT")
BLOCKING = {"BLOCKER", "HIGH"}
GATE_ARTIFACTS = (".metrics", "target")

_VERDICT_RE = re.compile(r"^[\s*_`>#-]*VERDICT:\s*(PASS|FAIL)\b[\s*_`.]*$", re.I)
_SEV_ALT = "|".join(SEVERITIES)
_PREFIX = r"^\s*(?:[-*+>]\s+|\d+[.)]\s+|#{1,6}\s+)*[*_`\[(]*"
_FINDING_RE = re.compile(_PREFIX + rf"({_SEV_ALT})\b[*_`\])]*\s*[:\-.–—]?\s*(.*)$")
_HEAD_ONLY_RE = re.compile(
    r"^\s*#{1,6}\s*[*_`\[(]*(" + _SEV_ALT + r")\b[^\n]*$|^\s*[*_`]+(" + _SEV_ALT + r")\b[*_`:]*\s*$"
)
_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(.*\S)")
_WHERE_RE = re.compile(r"[\w./-]*[\w-]\.[A-Za-z0-9_]+:\d+(?:[-,]\d+)*")
_FILE_RE = re.compile(r"[\w./-]+/[\w.-]+\.[A-Za-z0-9_]+|\b[\w-]+\.(?:py|go|rs|ts|tsx|js|sh|md|json|ya?ml|toml)\b")
_SPEC_HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*)?[*_`]*spec(?:\s+rows?|\s+items?|\s+coverage)?\b", re.I)
_SPEC_STATUS_RE = re.compile(r"\b(not landed|landed differently|landed)\b", re.I)


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=False) + "\n")


def fail(code: int, msg: str, **extra: Any) -> int:
    sys.stderr.write(f"neon-review: {msg}\n")
    emit({"verdict": "error", "error": msg, "findings": [], "spec_rows": [], **extra})
    return code


def run(argv: list[str], cwd: Path, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, errors="replace")


def git(repo: Path, *args: str) -> str:
    p = run(["git", *args], repo)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def scratch_root() -> Path:
    env = os.environ.get("NEON_REVIEW_SCRATCH")
    if env:
        return Path(env)
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "neon-review"


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- gates


def existing_artifacts(repo: Path) -> set[str]:
    return {a for a in GATE_ARTIFACTS if (repo / a).exists()}


def remove_new_artifacts(repo: Path, before: set[str]) -> None:
    """Remove gate output this run created, so the checkout is left as found."""
    for name in GATE_ARTIFACTS:
        path = repo / name
        if name not in before and path.exists():
            shutil.rmtree(path, ignore_errors=True)


def run_ub_gates(repo: Path, base: str, changed: list[str]) -> dict[str, Any]:
    """Run crap and dry ourselves and return their JSON digest.

    `ub-gate crap --changed` reads uncommitted changes from git status, which is
    empty on a committed branch, so the changed files are named explicitly.
    """
    gates: dict[str, Any] = {"base": base, "changed_files": changed}
    sources = [f for f in changed if (repo / f).is_file()]
    crap = ["ub-gate", "crap", "--no-coverage", *sources] if sources else None
    dry = ["ub-gate", "dry", "--since", base, "--against-repo"]
    steps = {"crap": crap, "dry": dry}
    for name, argv in steps.items():
        if argv is None:
            gates[name] = {"skipped": "no changed files"}
            continue
        p = run(argv, repo, GATE_TIMEOUT_S)
        entry: dict[str, Any] = {"cmd": " ".join(argv), "exit": p.returncode}
        if p.returncode not in (0, 2):
            entry["stderr"] = p.stderr.strip()[-2000:]
        gates[name] = entry
    s = run(["ub-gate", "summary"], repo, GATE_TIMEOUT_S)
    try:
        digest = json.loads(s.stdout)
    except ValueError:
        digest = {"error": "ub-gate summary was not JSON", "stdout": s.stdout[-1000:]}
    for name in ("crap", "dry"):
        if isinstance(gates.get(name), dict):
            gates[name]["summary"] = digest.get(name) if isinstance(digest, dict) else None
    return gates


# ---------------------------------------------------------------- prompt


def build_prompt(rubric: str, depth: str, base: str, files: dict[str, Path], repo: Path) -> str:
    lines = [
        rubric.strip(),
        "",
        "---",
        "",
        f"Review the branch at the current directory ({repo.name}) against base `{base}`.",
        "The inputs are in files. Read each one before you form findings:",
        f"- diff stat: {files['stat']}",
        f"- commit log: {files['log']}",
        f"- full diff: {files['diff']}",
    ]
    if "spec" in files:
        lines.append(f"- spec, issue or brief (list its items as spec rows): {files['spec']}")
    else:
        lines.append("- no spec was given, so report no spec rows.")
    lines.append("")
    if depth == "ub":
        lines += [
            "This is a `ub` depth review.",
            f"- The caller already ran `ub-gate crap` and `ub-gate dry --since {base} --against-repo`. Their JSON is in {files['gates']}. Report from it as the rubric says.",
            "- Run `ub-gate mutate <file>` yourself on the two highest-risk changed files from your risk map. Run it one file at a time and report surviving mutants.",
            "",
        ]
    lines += [
        "Output format: one finding per line, `SEVERITY file:line claim. Fix: fix and test.`,",
        "then a section headed `Spec rows` with one `- item: landed|not landed|landed differently (where)` per row,",
        "then the single last line `VERDICT: PASS` or `VERDICT: FAIL`.",
        "Do not edit, commit or reset anything on the branch. Remove build or gate output you create.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- parse


def parse_verdict(text: str) -> str | None:
    """The verdict is the last non-empty line, and it must match exactly."""
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        m = _VERDICT_RE.match(line)
        return m.group(1).upper() if m else None
    return None


def _split_where_fix(body: str) -> tuple[str, str, str]:
    where = ""
    m = _WHERE_RE.search(body) or _FILE_RE.search(body)
    if m:
        where = m.group(0)
    fix = ""
    fm = re.search(r"\bfix(?:es)?\s*[:\-]\s*", body, re.I)
    if fm:
        fix = body[fm.end():].strip()
        body = body[: fm.start()]
    claim = body.strip(" \t.;:*_`")
    return claim, where, fix.strip(" \t*_`.")


def parse_findings(text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    section_sev: str | None = None
    cur: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            body = " ".join(p for p in cur["parts"] if p).strip()
            claim, where, fix = _split_where_fix(body)
            if claim or where:
                findings.append({"severity": cur["sev"], "claim": claim, "where": where, "fix": fix})
        cur = None

    for line in text.splitlines():
        if _VERDICT_RE.match(line) or _SPEC_HEAD_RE.match(line):
            flush()
            section_sev = None
            if _SPEC_HEAD_RE.match(line):
                break
            continue
        head = _HEAD_ONLY_RE.match(line)
        if head:
            flush()
            section_sev = (head.group(1) or head.group(2)).upper()
            continue
        fm = _FINDING_RE.match(line)
        if fm and fm.group(2).strip():
            flush()
            cur = {"sev": fm.group(1).upper(), "parts": [fm.group(2).strip()]}
            continue
        if re.match(r"^\s*#{1,6}\s", line):
            flush()
            section_sev = None
            continue
        item = _ITEM_RE.match(line)
        if section_sev and item:
            flush()
            cur = {"sev": section_sev, "parts": [item.group(1).strip()]}
            continue
        if cur is not None:
            if not line.strip():
                flush()
            else:
                cur["parts"].append(line.strip())
    flush()
    return findings


def parse_spec_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    in_spec = False
    for line in text.splitlines():
        if _VERDICT_RE.match(line):
            break
        if _SPEC_HEAD_RE.match(line):
            in_spec = True
            continue
        if not in_spec:
            continue
        item = _ITEM_RE.match(line)
        if not item:
            continue
        body = item.group(1).strip()
        m = _SPEC_STATUS_RE.search(body)
        if not m:
            continue
        status = m.group(1).lower()
        row = body[: m.start()].strip(" \t:-*_`–—()")
        detail = body[m.end():].strip(" \t:-*_`–—().")
        rows.append({"row": row, "status": status, "detail": detail})
    return rows


# ---------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="neon-review", description="Adversarial review of a branch via agy.")
    p.add_argument("--repo", required=True, help="repository directory to review")
    p.add_argument("--base", required=True, help="base ref the branch is reviewed against")
    p.add_argument("--spec", help="spec, issue or brief file whose items become spec rows")
    p.add_argument("--depth", choices=("medium", "ub"), default="medium")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="seconds for the agy run")
    p.add_argument("--out", help="file for the raw review text")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code not in (0, None) else EXIT_OK

    if args.timeout <= 0:
        return fail(EXIT_USAGE, "--timeout must be positive")
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        return fail(EXIT_USAGE, f"--repo is not a directory: {args.repo}")
    spec_path = Path(args.spec).resolve() if args.spec else None
    if spec_path and not spec_path.is_file():
        return fail(EXIT_USAGE, f"--spec is not a file: {args.spec}")
    if not RUBRIC.is_file():
        return fail(EXIT_USAGE, "rubric.md missing beside neon_review.py")
    try:
        top = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        git(repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}")
    except RuntimeError as e:
        return fail(EXIT_USAGE, f"not a git repo or unknown base {args.base}: {e}")
    repo = top

    stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    work = scratch_root() / f"{repo.name}-{stamp}"
    if inside(work, repo):
        return fail(EXIT_USAGE, "scratch dir is inside the repo; set NEON_REVIEW_SCRATCH outside it")
    work.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out).resolve() if args.out else work / "review.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        stat = git(repo, "diff", "--stat", f"{args.base}...HEAD")
        log = git(repo, "log", "--format=%h %an%n%B%n---", f"{args.base}..HEAD")
        diff = git(repo, "diff", f"{args.base}...HEAD")
        changed = git(repo, "diff", "--name-only", "--diff-filter=AM", f"{args.base}...HEAD").split("\n")
    except RuntimeError as e:
        return fail(EXIT_USAGE, str(e))
    changed = [c for c in changed if c]

    files: dict[str, Path] = {"stat": work / "diffstat.txt", "log": work / "commits.txt", "diff": work / "branch.diff"}
    files["stat"].write_text(stat)
    files["log"].write_text(log)
    files["diff"].write_text(diff)
    if spec_path:
        files["spec"] = work / "spec.md"
        files["spec"].write_text(spec_path.read_text(errors="replace"))

    before = existing_artifacts(repo)
    gates: dict[str, Any] | None = None
    result: dict[str, Any]
    try:
        if args.depth == "ub":
            if not shutil.which("ub-gate"):
                return fail(EXIT_UNREACHABLE, "ub-gate not found on PATH (needed for --depth ub)")
            try:
                gates = run_ub_gates(repo, args.base, changed)
            except (OSError, subprocess.TimeoutExpired) as e:
                return fail(EXIT_UNREACHABLE, f"ub-gate failed to run: {e}")
            files["gates"] = work / "ub-gates.json"
            files["gates"].write_text(json.dumps(gates, indent=2))

        prompt = build_prompt(RUBRIC.read_text(), args.depth, args.base, files, repo)
        (work / "prompt.txt").write_text(prompt)
        cmd = ["agy", "-p", prompt, "--model", args.model, "--add-dir", str(work)]
        timed_out = False
        try:
            p = run(cmd, repo, args.timeout)
            raw, rc, err = p.stdout, p.returncode, p.stderr
        except FileNotFoundError:
            return fail(EXIT_UNREACHABLE, "agy not found on PATH")
        except subprocess.TimeoutExpired as e:
            timed_out = True
            raw = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            rc, err = -1, ""
    finally:
        remove_new_artifacts(repo, before)

    out_path.write_text(raw)
    base_out: dict[str, Any] = {
        "depth": args.depth,
        "model": args.model,
        "raw_path": str(out_path),
        "work_dir": str(work),
    }
    if gates is not None:
        base_out["gates"] = gates

    if timed_out:
        result = {"verdict": "timeout", "findings": [], "spec_rows": [], **base_out}
        sys.stderr.write(f"neon-review: agy exceeded {args.timeout}s\n")
        emit(result)
        return EXIT_NEGATIVE
    if rc != 0 and not raw.strip():
        return fail(EXIT_UNREACHABLE, f"agy exited {rc} with no output: {err.strip()[-500:]}", **base_out)

    verdict = parse_verdict(raw)
    findings = parse_findings(raw)
    spec_rows = parse_spec_rows(raw)
    note = None
    if verdict is None:
        verdict, note = "unparsed", "no final VERDICT line"
    elif verdict == "PASS" and any(f["severity"] in BLOCKING for f in findings):
        verdict, note = "FAIL", "reviewer said PASS but listed BLOCKER or HIGH findings"
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    result = {"verdict": verdict, "findings": findings, "spec_rows": spec_rows, "counts": counts, **base_out}
    if note:
        result["note"] = note
    emit(result)
    return EXIT_OK if verdict == "PASS" else EXIT_NEGATIVE


if __name__ == "__main__":
    sys.exit(main())
