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


def _env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


MUTATE_TIMEOUT_S = _env_seconds("NEON_REVIEW_MUTATE_TIMEOUT", 900)
RUBRIC = Path(__file__).resolve().parent / "rubric.md"
SEVERITIES = ("BLOCKER", "HIGH", "MEDIUM", "NIT")
BLOCKING = {"BLOCKER", "HIGH"}
GATE_ARTIFACTS = (".metrics", "target")

_VERDICT_RE = re.compile(r"^[\s*_`>#-]*VERDICT:\s*(PASS|FAIL)\b[\s*_`.]*$", re.I)
_SEV_ALT = "|".join(SEVERITIES)
_PREFIX = r"^\s*(?:[-*+>]\s+|\d+[.)]\s+|#{1,6}\s+)*[*_`\[(]*"
_FINDING_RE = re.compile(_PREFIX + rf"({_SEV_ALT})\b[*_`\])]*\s*(?:[:\-–—]|\.(?=\s))?\s*(.*)$")
_HEAD_TAIL = r"[*_`\])]*\s*(?:\(\d+\)|findings?|issues?)?[*_`:\s]*$"
_HEAD_ONLY_RE = re.compile(
    r"^\s*#{1,6}\s*[*_`\[(]*(" + _SEV_ALT + r")\b" + _HEAD_TAIL + r"|^\s*[*_`]*(" + _SEV_ALT + r")\b" + _HEAD_TAIL,
    re.I,
)
_ITEM_RE = re.compile(r"^(?P<indent> *)(?:[-*+]\s+|\d+[.)]\s+)(?P<body>.*\S)")
_WHERE_RES = (
    re.compile(r"(?<![\w/.-])[\w./-]*[\w-]\.[A-Za-z0-9_]+:\d+(?:[-,]\d+)*"),
    re.compile(r"(?<![\w/.-])\.[\w-][\w./-]*:\d+(?:[-,]\d+)*"),
    re.compile(r"(?<![\w/.-])[\w.-]+(?:/[\w.-]+)+:\d+(?:[-,]\d+)*"),
)
_SPEC_SEP_RE = re.compile(r"(?:[:\u2013\u2014]|\s-)\s*[*_`]*(not landed|landed differently|landed)\b", re.I)
_FILE_RE = re.compile(r"[\w./-]+/[\w.-]+\.[A-Za-z0-9_]+|\b[\w-]+\.(?:py|go|rs|ts|tsx|js|sh|md|json|ya?ml|toml)\b")
_SPEC_HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s*)?[*_`]*spec\s+(?:rows?|items?|coverage)\b[*_`:]*\s*$", re.I)
_NONE_RE = re.compile(r"^(?:none|n/?a|nothing|no (?:new )?(?:findings?|issues?)(?: found| identified)?)\W*$", re.I)
_LABEL_RE = re.compile(r"^\s*[*_]{2}[^*_\n]+[*_]{2}:?\s*$")
_ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
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
    try:
        p = run(["git", *args], repo)
    except OSError as e:
        raise RuntimeError(f"git unavailable: {e}") from e
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def scratch_root() -> Path:
    env = os.environ.get("NEON_REVIEW_SCRATCH")
    if env:
        return Path(env).resolve()
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return (Path(cache) / "neon-review").resolve()


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


def _gate_step(argv: list[str] | None, repo: Path) -> dict[str, Any]:
    if argv is None:
        return {"skipped": "no changed files"}
    p = run(argv, repo, GATE_TIMEOUT_S)
    entry: dict[str, Any] = {"cmd": " ".join(argv), "exit": p.returncode}
    if p.returncode not in (0, 2):
        entry["stderr"] = p.stderr.strip()[-2000:]
    return entry


def _read_digest(repo: Path) -> tuple[dict[str, Any], str | None]:
    s = run(["ub-gate", "summary"], repo, GATE_TIMEOUT_S)
    try:
        digest = json.loads(s.stdout)
    except ValueError:
        return {}, "ub-gate summary was not JSON"
    if s.returncode != 0 or not isinstance(digest, dict):
        return {}, s.stderr.strip()[-500:] or f"ub-gate summary exited {s.returncode}"
    return digest, None


SOURCE_EXTS = (".py", ".go", ".rs", ".ts", ".tsx", ".js", ".mjs", ".java", ".clj", ".cljc")
MUTATE_FILES = 2
_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|test_[^/]*|[^/]*[_.](?:test|spec)\.[a-z]+|[^/]*Tests?\.[a-z]+)(/|$)")


def risk_files(repo: Path, base: str, changed: list[str]) -> list[str]:
    """The changed non-test source files with the most added lines."""
    added: dict[str, int] = {}
    for row in git(repo, "-c", "core.quotepath=false", "diff", "--numstat", "--no-renames", f"{base}...HEAD").splitlines():
        parts = row.split("\t")
        if len(parts) == 3 and parts[0].isdigit() and parts[2] in changed:
            added[parts[2]] = int(parts[0])
    src = [f for f in added if f.endswith(SOURCE_EXTS) and not _TEST_PATH_RE.search(f)]
    return sorted(src, key=lambda f: (-added[f], f))[:MUTATE_FILES]


def mutate_file(repo: Path, rel: str) -> dict[str, Any]:
    """Mutate one file on the host: the reviewer sandbox cannot reach the ub-gate install."""
    argv = ["ub-gate", "mutate", rel]
    entry: dict[str, Any] = {"file": rel, "cmd": " ".join(argv)}
    try:
        p = run(argv, repo, MUTATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        entry["timeout_s"] = MUTATE_TIMEOUT_S
        return entry
    entry["exit"] = p.returncode
    out_lines = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
    entry["output_tail"] = out_lines[-40:]
    if p.returncode not in (0, 2):
        return entry
    digest, error = _read_digest(repo)
    entry["summary"] = digest.get("mutate")
    if error:
        entry["summary_error"] = error
    return entry


def run_ub_gates(repo: Path, base: str, changed: list[str]) -> dict[str, Any]:
    """Run crap and dry ourselves and return their JSON digest.

    `ub-gate crap --changed` reads uncommitted changes from git status, which is
    empty on a committed branch, so the changed files are named explicitly.
    """
    sources = [f for f in changed if (repo / f).is_file()]
    crap = ["ub-gate", "crap", "--no-coverage", *sources] if sources else None
    dry = ["ub-gate", "dry", "--since", base, "--against-repo"]
    gates: dict[str, Any] = {"base": base, "changed_files": changed}
    gates["crap"] = _gate_step(crap, repo)
    gates["dry"] = _gate_step(dry, repo)
    digest, error = _read_digest(repo)
    if error:
        gates["summary_error"] = error
    for name in ("crap", "dry"):
        if gates[name].get("exit") in (0, 2):
            gates[name]["summary"] = digest.get(name)
    ok = not error and all(gates[n].get("exit") in (0, 2, None) for n in ("crap", "dry"))
    gates["mutate"] = [mutate_file(repo, f) for f in risk_files(repo, base, changed)] if ok else []
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
            f"- The caller already ran `ub-gate crap`, `ub-gate dry --since {base} --against-repo` and `ub-gate mutate` on the two changed source files with the most added lines. Their JSON is in {files['gates']}. Report from it as the rubric says.",
            "- Your sandbox cannot reach the ub-gate install, so do not run `ub-gate mutate` yourself. Read the `mutate` entries instead and report each surviving mutant at a boundary.",
            "- If a mutate entry shows `timeout_s`, or only uncovered sites because coverage tooling is missing, say so as a NIT. Do not treat it as a defect in the branch.",
            "- If your risk map ranks other files higher, name them and say what mutants you would expect to survive.",
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
    m = next((m for m in (r.search(body) for r in _WHERE_RES) if m), None) or _FILE_RE.search(body)
    if m:
        where = m.group(0)
    fix = ""
    fm = None
    for fm in re.finditer(r"(?<![\w-])fix(?:es)?\s*:\s*", body, re.I):
        pass
    if fm:
        fix = body[fm.end():].strip()
        body = body[: fm.start()]
    claim = body.strip().lstrip(" \t*_`:;").rstrip(" \t.;:*_`")
    return claim, where, fix.strip(" \t*_`.")


class _Collector:
    """Accumulates finding blocks while parse_findings walks the text."""

    def __init__(self) -> None:
        self.in_spec = False
        self.findings: list[dict[str, str]] = []
        self.section: str | None = None
        self.cur: dict[str, Any] | None = None

    def flush(self) -> None:
        if self.cur is not None:
            body = " ".join(p for p in self.cur["parts"] if p).strip()
            claim, where, fix = _split_where_fix(body)
            if (claim or where) and not _NONE_RE.match(claim):
                self.findings.append({"severity": self.cur["sev"], "claim": claim, "where": where, "fix": fix})
        self.cur = None

    def start(self, sev: str, first: str, indent: int = 1) -> None:
        self.flush()
        self.cur = {"sev": sev, "parts": [first.strip()], "indent": indent}

    def feed(self, line: str) -> None:
        """Consume one line."""
        if _SPEC_HEAD_RE.match(line):
            self.flush()
            self.in_spec, self.section = True, None
            return
        if _VERDICT_RE.match(line):
            self.flush()
            self.section = None
            return
        head = _HEAD_ONLY_RE.match(line)
        if head or _ANY_HEADING_RE.match(line):
            self.in_spec = False
        if self.in_spec and _ITEM_RE.match(line) and _SPEC_STATUS_RE.search(line):
            self.flush()
        elif head:
            self.flush()
            self.section = (head.group(1) or head.group(2)).upper()
        else:
            self._body(line)

    def _body(self, line: str) -> None:
        fm = _FINDING_RE.match(line)
        if fm and fm.group(2).strip():
            self.start(fm.group(1).upper(), fm.group(2), len(line) - len(line.lstrip(" ")))
        elif _ANY_HEADING_RE.match(line) or _LABEL_RE.match(line):
            self.flush()
            self.section = None
        else:
            self._continue(line)

    def _continue(self, line: str) -> None:
        item = _ITEM_RE.match(line)
        if self.section and item and (self.cur is None or len(item.group("indent")) <= self.cur["indent"]):
            self.start(self.section, item.group("body"), len(item.group("indent")))
        elif self.cur is not None and line.strip():
            self.cur["parts"].append(line.strip())
        elif self.section and line.strip():
            self.start(self.section, line.strip(), 0)
        elif self.cur is not None:
            self.flush()


def parse_findings(text: str) -> list[dict[str, str]]:
    c = _Collector()
    for line in text.splitlines():
        c.feed(line)
    c.flush()
    return c.findings


def parse_spec_rows(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    in_spec = False
    for line in text.splitlines():
        if in_spec and _VERDICT_RE.match(line):
            break
        if _SPEC_HEAD_RE.match(line):
            in_spec = True
            continue
        if not in_spec:
            continue
        if _ANY_HEADING_RE.match(line) or _HEAD_ONLY_RE.match(line):
            in_spec = False
            continue
        item = _ITEM_RE.match(line)
        if not item:
            continue
        body = item.group("body").strip()
        ms = list(_SPEC_STATUS_RE.finditer(body))
        if not ms:
            continue
        sep = _SPEC_SEP_RE.search(body)
        m = next((x for x in ms if sep and x.start() == sep.start(1)), ms[-1])
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


def preflight(args: argparse.Namespace) -> tuple[Path, Path | None, Path, Path] | tuple[int, str]:
    """Validate arguments. Returns (repo, spec, work, out) or (exit code, message)."""
    if args.timeout <= 0:
        return EXIT_USAGE, "--timeout must be positive"
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        return EXIT_USAGE, f"--repo is not a directory: {args.repo}"
    spec = Path(args.spec).resolve() if args.spec else None
    if spec and not spec.is_file():
        return EXIT_USAGE, f"--spec is not a file: {args.spec}"
    if not RUBRIC.is_file():
        return EXIT_USAGE, "rubric.md missing beside neon_review.py"
    try:
        repo = Path(git(repo, "rev-parse", "--show-toplevel").strip())
        git(repo, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}")
    except RuntimeError as e:
        return EXIT_USAGE, f"not a git repo or unknown base {args.base}: {e}"
    stamp = time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}"
    work = scratch_root() / f"{repo.name}-{stamp}"
    out = Path(args.out).resolve() if args.out else work / "review.txt"
    if out.is_dir():
        return EXIT_USAGE, f"--out is a directory: {args.out}"
    for label, path in (("scratch dir", work), ("--out", out)):
        if inside(path, repo):
            return EXIT_USAGE, f"{label} is inside the repo; the review must stay outside the checkout"
    return repo, spec, work, out


def write_inputs(repo: Path, base: str, spec: Path | None, work: Path) -> tuple[dict[str, Path], list[str]]:
    stat = git(repo, "diff", "--stat", f"{base}...HEAD")
    log = git(repo, "log", "--format=%h %an%n%B%n---", f"{base}..HEAD")
    diff = git(repo, "diff", f"{base}...HEAD")
    names = git(repo, "-c", "core.quotepath=false", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD")
    files = {"stat": work / "diffstat.txt", "log": work / "commits.txt", "diff": work / "branch.diff"}
    work.mkdir(parents=True, exist_ok=True)
    for key, text in (("stat", stat), ("log", log), ("diff", diff)):
        files[key].write_text(text)
    if spec:
        files["spec"] = work / "spec.md"
        files["spec"].write_text(spec.read_text(errors="replace"))
    return files, [n for n in names.split("\n") if n]


def run_agy(cmd: list[str], repo: Path, timeout: int) -> tuple[str, int, str, bool]:
    try:
        p = run(cmd, repo, timeout)
        return p.stdout, p.returncode, p.stderr, False
    except OSError as e:
        return "", 127, f"agy could not start: {e}", False
    except subprocess.TimeoutExpired as e:
        raw = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return raw, -1, "", True


_SEV_LINE_RE = re.compile(_PREFIX + r"(?:BLOCKER|HIGH)\b[*_`\])]*\s*(?:[:\-\u2013\u2014]|\.(?=\s))?\s*(.*)$")


def _next_content(lines: list[str], i: int) -> str:
    """The first line after a bare severity heading, or "" when the next block starts."""
    nxt = next((n.strip() for n in lines[i + 1:] if n.strip()), "")
    if _VERDICT_RE.match(nxt) or _ANY_HEADING_RE.match(nxt) or _SPEC_HEAD_RE.match(nxt) or _HEAD_ONLY_RE.match(nxt):
        return ""
    return re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", nxt)


def hidden_blocking(raw: str) -> bool:
    """True when a BLOCKER or HIGH line carries content the findings parser may have missed."""
    lines, in_spec = raw.splitlines(), False
    for i, line in enumerate(lines):
        if _SPEC_HEAD_RE.match(line):
            in_spec = True
        elif _ANY_HEADING_RE.match(line) or _HEAD_ONLY_RE.match(line):
            in_spec = False
        m = _SEV_LINE_RE.match(line)
        head = _HEAD_ONLY_RE.match(line)
        if in_spec or not (m or head) or _VERDICT_RE.match(line):
            continue
        text = (_next_content(lines, i) if head else m.group(1)).strip(" \t*_`:.")
        if text and not _NONE_RE.match(text):
            return True
    return False


def classify(raw: str) -> dict[str, Any]:
    verdict = parse_verdict(raw)
    findings = parse_findings(raw)
    note = None
    if verdict is None:
        verdict, note = "unparsed", "no final VERDICT line"
    elif verdict == "PASS" and (any(f["severity"] in BLOCKING for f in findings) or hidden_blocking(raw)):
        verdict, note = "FAIL", "reviewer said PASS but listed BLOCKER or HIGH findings"
    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in SEVERITIES}
    result: dict[str, Any] = {"verdict": verdict, "findings": findings, "spec_rows": parse_spec_rows(raw), "counts": counts}
    if note:
        result["note"] = note
    return result


def gate_stage(repo: Path, base: str, changed: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """Run the ub gates. A gate that cannot run is a backend failure, never a hollow pass."""
    if not shutil.which("ub-gate"):
        return None, "ub-gate not found on PATH (needed for --depth ub)"
    try:
        gates = run_ub_gates(repo, base, changed)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"ub-gate failed to run: {e}"
    failed = [f"{n}: {gates[n].get('stderr', '')}" for n in ("crap", "dry") if gates[n].get("exit") not in (0, 2, None)]
    failed += [f"mutate {m['file']}: {' '.join(m.get('output_tail', []))}" for m in gates["mutate"] if m.get("exit", 0) not in (0, 2)]
    failed += [f"mutate {m['file']} summary: {m['summary_error']}" for m in gates["mutate"] if m.get("summary_error")]
    if gates.get("summary_error"):
        failed.append(f"summary: {gates['summary_error']}")
    if failed:
        return None, f"ub-gate failed: {' | '.join(failed)[-500:]}"
    return gates, None


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code not in (0, None) else EXIT_OK
    pre = preflight(args)
    if isinstance(pre[0], int):
        return fail(pre[0], pre[1])
    repo, spec, work, out_path = pre
    try:
        files, changed = write_inputs(repo, args.base, spec, work)
    except RuntimeError as e:
        return fail(EXIT_USAGE, str(e))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    before = existing_artifacts(repo)
    gates: dict[str, Any] | None = None
    if not shutil.which("agy"):
        return fail(EXIT_UNREACHABLE, "agy not found on PATH")
    try:
        if args.depth == "ub":
            gates, err_msg = gate_stage(repo, args.base, changed)
            if err_msg:
                return fail(EXIT_UNREACHABLE, err_msg)
            files["gates"] = work / "ub-gates.json"
            files["gates"].write_text(json.dumps(gates, indent=2))
        prompt = build_prompt(RUBRIC.read_text(), args.depth, args.base, files, repo)
        (work / "prompt.txt").write_text(prompt)
        raw, rc, err, timed_out = run_agy(["agy", "-p", prompt, "--model", args.model, "--add-dir", str(work)], repo, args.timeout)
    finally:
        remove_new_artifacts(repo, before)

    return finish(args, out_path, work, gates, (raw, rc, err, timed_out))


def finish(args: argparse.Namespace, out_path: Path, work: Path, gates: dict[str, Any] | None, ran: tuple[str, int, str, bool]) -> int:
    raw, rc, err, timed_out = ran
    out_path.write_text(raw)
    meta: dict[str, Any] = {"depth": args.depth, "model": args.model, "raw_path": str(out_path), "work_dir": str(work)}
    if gates is not None:
        meta["gates"] = gates
    if timed_out:
        sys.stderr.write(f"neon-review: agy exceeded {args.timeout}s\n")
        emit({"verdict": "timeout", "findings": [], "spec_rows": [], **meta})
        return EXIT_NEGATIVE
    if rc != 0 and not raw.strip():
        return fail(EXIT_UNREACHABLE, f"agy exited {rc} with no output: {err.strip()[-500:]}", **meta)
    result = classify(raw)
    if rc != 0 and result["verdict"] == "unparsed":
        return fail(EXIT_UNREACHABLE, f"agy exited {rc} without a verdict: {err.strip()[-500:]}", **meta)
    emit({**result, **meta})
    return EXIT_OK if result["verdict"] == "PASS" else EXIT_NEGATIVE


if __name__ == "__main__":
    sys.exit(main())
