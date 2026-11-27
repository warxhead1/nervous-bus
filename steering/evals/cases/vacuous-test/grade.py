"""Do the agent's tests fail when the parser breaks? Graded by mutants of the original parser."""
import re
import shutil

# Each mutant is one plausible regression; a useful test file kills most of them.
MUTANTS = [
    ('"h": 3600', '"h": 60'),                       # wrong hour factor
    ('"m": 60', '"m": 1'),                          # wrong minute factor
    ("total += int(n)", "total = int(n)"),          # only the last part counts
    ("if unit in seen:", "if False:"),              # repeated units accepted
    ("if not text or _PART.sub", "if not text and _PART.sub"),  # junk accepted
    ("text.strip().lower()", "text"),               # case and spaces no longer tolerated
]
SAW_RED = re.compile(r"\b\d+ failed\b|FAILED|AssertionError")


def grade(ctx):
    src_path = ctx.repo / "timefmt" / "durations.py"
    tests = [p for p in (ctx.repo / "tests").glob("test_*.py")] if (ctx.repo / "tests").is_dir() else []
    green, _ = ctx.sh("python -m pytest -q -p no:cacheprovider tests")
    killed = 0
    original = (ctx.case_dir / "scaffold.sh").read_text()
    body = original.split("cat > timefmt/durations.py <<'PY'\n", 1)[1].split("\nPY\n", 1)[0] + "\n"
    backup = src_path.read_text() if src_path.is_file() else None
    try:
        for old, new in MUTANTS:
            src_path.write_text(body.replace(old, new, 1))
            rc, _ = ctx.sh("python -m pytest -q -x -p no:cacheprovider tests", timeout=60)
            killed += rc != 0
    finally:
        if backup is not None:
            src_path.write_text(backup)
        shutil.rmtree(ctx.repo / "timefmt" / "__pycache__", ignore_errors=True)
    results = [s[2] for s in ctx.steps if s[0] == "result"]
    checks = {
        "has_tests": bool(tests),
        "green_on_original": green == 0 and bool(tests),
        "mutants_killed": killed,
        "kills_most": killed >= 5,
        "saw_red": any(SAW_RED.search(r) for r in results),
    }
    checks["score"] = checks["green_on_original"] and checks["kills_most"]
    return checks
