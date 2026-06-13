"""Tests for the channel taxonomy / cluster discovery (nervous-bus-z836).

Guards three things:
  * `nervous schemas --cluster autobench` returns ONLY autobench.* channels;
  * `nervous schemas --search prediction` surfaces the AHE prediction channels;
  * schemas/CHANNELS.md exists and is regenerated identically by the generator
    (so the committed doc never drifts from schemas/*.json);
  * naming-convention violations are detected.

Runs against the repo schemas; needs no live services. The `--cluster` /
`--search` CLI paths shell out to NERVOUS_PYTHON for classification, so we pin
it to the test interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NERVOUS = REPO / "sdk" / "shell" / "nervous"
GEN = REPO / "tools" / "gen_channels_md.py"
CHANNELS_MD = REPO / "schemas" / "CHANNELS.md"

sys.path.insert(0, str(REPO / "tools"))
from gen_channels_md import classify, NAME_RE, CLUSTERS  # noqa: E402


def _schemas(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(NERVOUS), "schemas", *args],
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            # Force NO user-home overlay so output is deterministic across hosts
            # (a real ~/.config/nervous-bus would add private channels).
            "NERVOUS_HOME": "/nonexistent-nervous-home-for-tests",
            "NERVOUS_PYTHON": sys.executable,
        },
    )


def test_cluster_autobench_returns_only_autobench(tmp_path: Path):
    proc = _schemas("--cluster", "autobench")
    assert proc.returncode == 0, proc.stderr
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, "expected at least one autobench channel"
    for ln in lines:
        ch = ln.split()[0]  # strip any trailing tags
        assert ch.startswith("autobench."), f"non-autobench channel leaked: {ln}"


def test_search_prediction_returns_ahe_channels():
    proc = _schemas("--search", "prediction")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "autobench.improver.prediction.v1" in out, out
    # All results must actually contain the keyword.
    for ln in [l.strip() for l in out.splitlines() if l.strip()]:
        assert "prediction" in ln.split()[0].lower(), ln


def test_clusters_listing_covers_all_five():
    proc = _schemas("--clusters")
    assert proc.returncode == 0, proc.stderr
    for name, _ in CLUSTERS:
        assert name in proc.stdout, f"cluster {name!r} missing from --clusters"


def test_unknown_cluster_errors():
    proc = _schemas("--cluster", "nonsense-xyz")
    assert proc.returncode != 0


def test_channels_md_exists():
    assert CHANNELS_MD.exists(), "schemas/CHANNELS.md must be committed"
    text = CHANNELS_MD.read_text()
    for name, _ in CLUSTERS:
        assert f"## {name}" in text, f"cluster section {name!r} missing from CHANNELS.md"


def test_channels_md_is_current(tmp_path: Path):
    """Regenerating must reproduce the committed CHANNELS.md byte-for-byte."""
    before = CHANNELS_MD.read_text()
    proc = subprocess.run([sys.executable, str(GEN)], text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    after = CHANNELS_MD.read_text()
    assert before == after, (
        "schemas/CHANNELS.md is stale — run `python3 tools/gen_channels_md.py` and commit"
    )


def test_every_channel_classifies_to_exactly_one_cluster():
    names = {n for n, _ in CLUSTERS}
    for p in (REPO / "schemas").glob("*.json"):
        ch = p.name[: -len(".json")]
        assert classify(ch) in names, f"{ch} classified to unknown cluster {classify(ch)!r}"


def test_known_violations_flagged():
    # These filenames are known to violate <project>.<subsystem>.<event>.v<n>.
    for ch in ["codeforces_problem.v1", "_per-project.skill.push.v1"]:
        assert not NAME_RE.match(ch), f"{ch} should be flagged as a naming violation"
    # A conforming name must pass.
    assert NAME_RE.match("autobench.improver.prediction.v1")
