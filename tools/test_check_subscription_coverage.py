"""Tests for check_subscription_coverage.py.

Run with:
    python -m pytest tools/test_check_subscription_coverage.py -xvs
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).parent / "check_subscription_coverage.py"


def run_checker(
    subs: list[dict],
    schemas: Path,
    emitted: list[dict],
    tmp_path: Path,
    allowlist: Path | None = None,
    baseline: Path | None = None,
    report_only: bool = False,
    strict: bool = False,
) -> subprocess.CompletedProcess:
    subs_f = tmp_path / "subs.json"
    subs_f.write_text(json.dumps(subs))
    emits_f = tmp_path / "emits.json"
    emits_f.write_text(json.dumps(emitted))
    if allowlist is None:
        allowlist = tmp_path / "allow.txt"
        allowlist.write_text("")
    if baseline is None:
        baseline = tmp_path / "base.txt"
        baseline.write_text("")
    cmd = [
        sys.executable,
        str(CHECKER),
        "--input", str(subs_f),
        "--emitted", str(emits_f),
        "--schemas", str(schemas),
        "--allowlist", str(allowlist),
        "--baseline", str(baseline),
    ]
    if report_only:
        cmd.append("--report-only")
    if strict:
        cmd.append("--strict")
    return subprocess.run(cmd, capture_output=True, text=True)


def make_schemas(tmp_path: Path, names: list[str]) -> Path:
    schemas = tmp_path / "schemas"
    schemas.mkdir(exist_ok=True)
    for n in names:
        (schemas / f"{n}.json").write_text("{}")
    return schemas


def sub(channel: str, match_type: str = "exact") -> dict:
    return {
        "consumer": "h",
        "file": "x.rs",
        "line": 1,
        "channel": channel,
        "match_type": match_type,
    }


def emit(channel: str) -> dict:
    return {"producer": "p", "file": "y.py", "line": 1, "channel": channel}


# ─── Class 1: subscribed-but-no-schema (BLOCK) ────────────────────────────────


def test_no_schema_blocks(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.foo.v1"])
    res = run_checker(
        subs=[sub("deer-flow.run", "prefix")],
        schemas=schemas,
        emitted=[],
        tmp_path=tmp_path,
    )
    assert res.returncode == 1, res.stdout
    assert "BLOCK" in res.stdout
    assert "deer-flow.run" in res.stdout


def test_no_schema_baseline_downgrades(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.foo.v1"])
    baseline = tmp_path / "base.txt"
    baseline.write_text("deer-flow.run\n")
    res = run_checker(
        subs=[sub("deer-flow.run", "prefix")],
        schemas=schemas,
        emitted=[],
        tmp_path=tmp_path,
        baseline=baseline,
    )
    assert res.returncode == 0, res.stdout
    assert "baselined" in res.stdout.lower()
    assert "deer-flow.run" in res.stdout


# ─── Class 2: subscribed-but-never-emitted (dead handler, WARN) ───────────────


def test_dead_handler_warns_not_block(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.cycle.snapshot.v1"])
    res = run_checker(
        subs=[sub("deer-flow.cycle.snapshot", "prefix")],
        schemas=schemas,
        emitted=[emit("autobench.case.v1")],  # schema'd but unrelated emit
        tmp_path=tmp_path,
    )
    # WARN only → exit 0 by default.
    assert res.returncode == 0, res.stdout
    assert "dead handler" in res.stdout.lower()
    assert "deer-flow.cycle.snapshot" in res.stdout


def test_dead_handler_strict_escalates(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.cycle.snapshot.v1"])
    res = run_checker(
        subs=[sub("deer-flow.cycle.snapshot", "prefix")],
        schemas=schemas,
        emitted=[],
        tmp_path=tmp_path,
        strict=True,
    )
    assert res.returncode == 1, res.stdout
    assert "dead handler" in res.stdout.lower()


# ─── Class 3: schema-exists-but-never-used (orphan, WARN) ─────────────────────


def test_orphan_schema_warns(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.orphan.v1", "deer-flow.live.v1"])
    res = run_checker(
        subs=[sub("deer-flow.live", "prefix")],
        schemas=schemas,
        emitted=[emit("deer-flow.live.v1")],
        tmp_path=tmp_path,
    )
    # orphan is WARN only → exit 0.
    assert res.returncode == 0, res.stdout
    assert "orphan" in res.stdout.lower()
    assert "deer-flow.orphan" in res.stdout
    # the live one (subscribed + emitted) must NOT be an orphan.
    orphan_section = res.stdout.split("orphan")[-1]
    assert "deer-flow.live" not in orphan_section


# ─── Healthy paths ────────────────────────────────────────────────────────────


def test_healthy_broad_glob_not_flagged(tmp_path: Path) -> None:
    """A broad prefix matching many emitted+schema'd channels is healthy."""
    schemas = make_schemas(
        tmp_path,
        ["deer-flow.forge.session.created.v1", "deer-flow.forge.seal.stamped.v1"],
    )
    res = run_checker(
        subs=[sub("deer-flow.forge", "prefix")],
        schemas=schemas,
        emitted=[emit("deer-flow.forge.session.created.v1")],
        tmp_path=tmp_path,
    )
    assert res.returncode == 0, res.stdout
    # Not in any drift section: no BLOCK section header, no dead-handler section.
    assert "--- BLOCK" not in res.stdout
    assert "--- WARN" not in res.stdout


def test_exact_base_matches_versioned_schema(tmp_path: Path) -> None:
    """exact `foo.bar.v1` matches schema base `foo.bar` and vice versa."""
    schemas = make_schemas(tmp_path, ["deer-flow.research.cycle.completed.v1"])
    res = run_checker(
        subs=[sub("deer-flow.research.cycle.completed.v1", "exact")],
        schemas=schemas,
        emitted=[emit("deer-flow.research.cycle.completed.v1")],
        tmp_path=tmp_path,
    )
    assert res.returncode == 0, res.stdout
    assert "--- BLOCK" not in res.stdout


def test_exact_subscribe_to_base_matches_versioned(tmp_path: Path) -> None:
    """Subscribing to base `foo.bar` matches a `foo.bar.v1` schema."""
    schemas = make_schemas(tmp_path, ["agent.session.v1"])
    res = run_checker(
        subs=[sub("agent.session", "exact")],
        schemas=schemas,
        emitted=[emit("agent.session.v1")],
        tmp_path=tmp_path,
    )
    assert res.returncode == 0, res.stdout
    assert "--- BLOCK" not in res.stdout


# ─── Allowlist ─────────────────────────────────────────────────────────────────


def test_allowlist_exempts_all_classes(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.foo.v1"])
    allow = tmp_path / "allow.txt"
    allow.write_text("# comment\ntachyonos.command.halt.v1\n")
    res = run_checker(
        subs=[sub("tachyonos.command.halt.v1", "exact")],  # no schema → would BLOCK
        schemas=schemas,
        emitted=[],
        tmp_path=tmp_path,
        allowlist=allow,
    )
    assert res.returncode == 0, res.stdout
    assert "allowlisted:                   1" in res.stdout
    # Must not appear in BLOCK list.
    if "BLOCK" in res.stdout:
        block_section = res.stdout.split("BLOCK")[1]
        assert "tachyonos.command.halt.v1" not in block_section


# ─── report-only ───────────────────────────────────────────────────────────────


def test_report_only_returns_zero(tmp_path: Path) -> None:
    schemas = make_schemas(tmp_path, ["deer-flow.foo.v1"])
    res = run_checker(
        subs=[sub("no.schema.here", "exact")],
        schemas=schemas,
        emitted=[],
        tmp_path=tmp_path,
        report_only=True,
    )
    assert res.returncode == 0
    assert "no.schema.here" in res.stdout
    assert "(--report-only)" in res.stdout


# ─── prefix vs exact matching ──────────────────────────────────────────────────


def test_prefix_matches_only_dotted_descendants(tmp_path: Path) -> None:
    """prefix `bus.bead` matches bus.bead.* but NOT bus.beadfoo."""
    schemas = make_schemas(tmp_path, ["bus.bead.lifecycle.v1", "bus.beadfoo.v1"])
    res = run_checker(
        subs=[sub("bus.bead", "prefix")],
        schemas=schemas,
        emitted=[emit("bus.bead.lifecycle.v1")],
        tmp_path=tmp_path,
    )
    assert res.returncode == 0, res.stdout
    # bus.beadfoo.v1 is unmatched by the prefix and unemitted → orphan.
    assert "bus.beadfoo" in res.stdout
    assert "orphan" in res.stdout.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs"]))
