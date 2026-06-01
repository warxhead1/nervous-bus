"""Tests for check_schema_coverage.py.

Run with:
    python -m pytest tools/test_check_schema_coverage.py -xvs
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).parent / "check_schema_coverage.py"


def run_checker(
    records: list[dict],
    schemas: Path,
    allowlist: Path,
    deprecated: Path | None = None,
    report_only: bool = False,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(CHECKER),
        "--schemas",
        str(schemas),
        "--allowlist",
        str(allowlist),
    ]
    if deprecated is not None:
        cmd.extend(["--deprecated", str(deprecated)])
    if report_only:
        cmd.append("--report-only")
    return subprocess.run(
        cmd,
        input=json.dumps(records),
        capture_output=True,
        text=True,
    )


def make_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    allow = tmp_path / "allow.txt"
    allow.write_text("")
    dep = tmp_path / "dep.txt"
    dep.write_text("")
    return schemas, allow, dep


def test_all_covered_exits_zero(tmp_path: Path) -> None:
    schemas, allow, dep = make_env(tmp_path)
    (schemas / "bus.bead.lifecycle.v1.json").write_text("{}")
    (schemas / "loom.coord.v1.json").write_text("{}")

    records = [
        {"producer": "h", "file": "x.go", "line": 1, "channel": "bus.bead.lifecycle.v1"},
        {"producer": "h", "file": "x.go", "line": 2, "channel": "loom.coord"},
    ]
    res = run_checker(records, schemas, allow, dep)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "MISSING:" in res.stdout
    assert "MISSING:                       0" in res.stdout


def test_missing_channel_exits_one(tmp_path: Path) -> None:
    schemas, allow, dep = make_env(tmp_path)
    (schemas / "covered.v1.json").write_text("{}")
    records = [
        {"producer": "h", "file": "x.go", "line": 1, "channel": "covered.v1"},
        {"producer": "h", "file": "x.go", "line": 2, "channel": "uncovered.ch"},
    ]
    res = run_checker(records, schemas, allow, dep)
    assert res.returncode == 1, res.stdout
    assert "uncovered.ch" in res.stdout
    assert "covered.v1" not in res.stdout.split("Missing schemas")[1] if "Missing schemas" in res.stdout else True


def test_allowlist_exempts_channel(tmp_path: Path) -> None:
    schemas, allow, dep = make_env(tmp_path)
    allow.write_text("# comment\ninternal.debug.ch\n")
    records = [
        {"producer": "h", "file": "x.py", "line": 1, "channel": "internal.debug.ch"},
    ]
    res = run_checker(records, schemas, allow, dep)
    assert res.returncode == 0, res.stdout
    assert "allowlisted:                   1" in res.stdout


def test_deprecated_channel_treated_as_missing(tmp_path: Path) -> None:
    schemas, allow, dep = make_env(tmp_path)
    (schemas / "old.foo.v1.json").write_text("{}")
    dep.write_text("old.foo\n")
    records = [
        {"producer": "h", "file": "x.py", "line": 1, "channel": "old.foo"},
    ]
    res = run_checker(records, schemas, allow, dep)
    assert res.returncode == 1
    assert "old.foo" in res.stdout


def test_versioned_and_unversioned_lookup(tmp_path: Path) -> None:
    schemas, allow, dep = make_env(tmp_path)
    (schemas / "foo.bar.v1.json").write_text("{}")
    # emit "foo.bar" → matches v1 base
    records = [
        {"producer": "p", "file": "f", "line": 1, "channel": "foo.bar"},
        {"producer": "p", "file": "f", "line": 2, "channel": "foo.bar.v1"},
    ]
    res = run_checker(records, schemas, allow, dep)
    assert res.returncode == 0, res.stdout


def test_report_only_returns_zero_on_missing(tmp_path: Path) -> None:
    schemas, allow, dep = make_env(tmp_path)
    records = [
        {"producer": "h", "file": "x.py", "line": 1, "channel": "not.in.schemas"},
    ]
    res = run_checker(records, schemas, allow, dep, report_only=True)
    assert res.returncode == 0
    assert "not.in.schemas" in res.stdout
    assert "(--report-only)" in res.stdout


def test_underscore_prefixed_schemas_ignored(tmp_path: Path) -> None:
    """Schemas starting with `_` are README/template files, not real schemas."""
    schemas, allow, dep = make_env(tmp_path)
    (schemas / "_per-project.foo.v1.json").write_text("{}")
    records = [
        {"producer": "p", "file": "f", "line": 1, "channel": "_per-project.foo"},
    ]
    res = run_checker(records, schemas, allow, dep)
    # `_per-project.foo` shouldn't be considered covered by the underscore file.
    assert res.returncode == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs"]))
