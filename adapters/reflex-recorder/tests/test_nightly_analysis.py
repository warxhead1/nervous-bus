"""tests/test_nightly_analysis.py — issue #32 (fail-loud) + issue #34
(tool_call coverage gauge) for nightly_analysis.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REC_DIR = Path(__file__).parent.parent
if str(_REC_DIR) not in sys.path:
    sys.path.insert(0, str(_REC_DIR))

import nightly_analysis as na


@pytest.fixture(autouse=True)
def _forbid_default_db_path(monkeypatch):
    """Every test here passes an explicit --db tmp_path (see sys.argv patches
    below and _make_empty_db()) — never nightly_analysis.py's DEFAULT_DB_PATH
    (the live ~/.cache/nervous-bus/reflex/runs.db). Guard structurally: point
    it somewhere that fails loudly if ever touched by accident, same pattern
    as tests/test_synthesis.py's _forbid_default_db_path."""
    monkeypatch.setattr(
        na, "DEFAULT_DB_PATH",
        Path("/nonexistent-test-guard/must-not-use-default-db/runs.db"),
    )


# ---------------------------------------------------------------------------
# Issue #34 — compute_coverage()
# ---------------------------------------------------------------------------

class TestComputeCoverage:
    def test_heartbeat_only_kind_is_zero_percent_and_flagged(self):
        """antigravity-cli shape: runs exist, but NONE ever had a tool_call
        event (heartbeat/started only) — 0% coverage, flagged."""
        rows = [{"agent_kind": "antigravity-cli", "total_runs": 731, "covered_runs": 0}]
        out = na.compute_coverage(rows)
        assert out[0]["rate"] == 0.0
        assert out[0]["flagged"] is True

    def test_fully_covered_kind_is_not_flagged(self):
        rows = [{"agent_kind": "host_claude_code", "total_runs": 100, "covered_runs": 100}]
        out = na.compute_coverage(rows)
        assert out[0]["rate"] == 1.0
        assert out[0]["flagged"] is False

    def test_threshold_boundary_exactly_at_threshold_is_not_flagged(self):
        """rate == threshold must NOT be flagged (strict less-than)."""
        rows = [{"agent_kind": "k", "total_runs": 100, "covered_runs": 80}]
        out = na.compute_coverage(rows, threshold=0.80)
        assert out[0]["rate"] == pytest.approx(0.80)
        assert out[0]["flagged"] is False

    def test_just_below_threshold_is_flagged(self):
        rows = [{"agent_kind": "k", "total_runs": 100, "covered_runs": 79}]
        out = na.compute_coverage(rows, threshold=0.80)
        assert out[0]["flagged"] is True

    def test_zero_total_runs_does_not_divide_by_zero(self):
        """A kind with literally zero closed runs in window (distinct from
        the heartbeat-only case, which HAS runs but zero coverage): must not
        raise ZeroDivisionError, and is reported at rate 0.0."""
        rows = [{"agent_kind": "ghost-kind", "total_runs": 0, "covered_runs": 0}]
        out = na.compute_coverage(rows)
        assert out[0]["rate"] == 0.0

    def test_matches_issue_34_cited_figures(self):
        """Regression pin against the exact figures issue #34 cites, measured
        against the live DB 2026-09-25."""
        rows = [
            {"agent_kind": "codex-cli", "total_runs": 2631, "covered_runs": 1456},
            {"agent_kind": "antigravity-cli", "total_runs": 731, "covered_runs": 0},
            {"agent_kind": "claude-code", "total_runs": 257, "covered_runs": 11},
        ]
        out = {r["agent_kind"]: r for r in na.compute_coverage(rows)}
        # codex-cli: 44.7% lack tool events -> ~55.3% have them
        assert out["codex-cli"]["rate"] == pytest.approx(1456 / 2631, abs=1e-4)
        assert out["codex-cli"]["flagged"] is True  # 55.3% < 80%
        assert out["antigravity-cli"]["rate"] == 0.0
        assert out["antigravity-cli"]["flagged"] is True
        # claude-code: 95.7% lack tool events -> ~4.3% have them
        assert out["claude-code"]["rate"] == pytest.approx(11 / 257, abs=1e-4)
        assert out["claude-code"]["flagged"] is True

    def test_sorted_worst_first(self):
        rows = [
            {"agent_kind": "good", "total_runs": 10, "covered_runs": 10},
            {"agent_kind": "bad", "total_runs": 10, "covered_runs": 0},
            {"agent_kind": "mid", "total_runs": 10, "covered_runs": 5},
        ]
        out = na.compute_coverage(rows)
        assert [r["agent_kind"] for r in out] == ["bad", "mid", "good"]


# ---------------------------------------------------------------------------
# Issue #32 — fail-loud: SYNTHESIS FAILED first line + non-zero exit
# ---------------------------------------------------------------------------

class TestFailLoud:
    def _make_ok_step(self, name):
        return na.StepResult(name, True, "ok", 1.0, "", "")

    def _make_timeout_step(self, name, timeout=300):
        return na.StepResult(name, False, f"timeout after {timeout}s", float(timeout), "", "")

    def test_digest_first_content_line_is_synthesis_failed_on_timeout(self, monkeypatch, tmp_path):
        """build_digest's first CONTENT line (after the YAML frontmatter,
        which is metadata) must be the SYNTHESIS FAILED marker when the
        detectors step timed out."""
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)

        # Stub out every subprocess-shelling helper build_digest calls so this
        # test is hermetic (no live query.py/struggle_ledger.py subprocess).
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])

        label_result = self._make_ok_step("label.py")
        detector_result = self._make_timeout_step("synthesis.py")

        text = na.build_digest(
            db_path, label_result, detector_result,
            window_days=7, struggle_window_days=14, step_timeout=300,
        )

        # Split off the YAML frontmatter (--- ... ---) and inspect the first
        # non-blank line of the BODY.
        assert text.startswith("---\n")
        _, _, body = text.partition("\n---\n")
        body_lines = [l for l in body.splitlines() if l.strip()]
        assert body_lines[0].startswith("**SYNTHESIS FAILED:"), (
            f"expected the digest body's first content line to be the "
            f"SYNTHESIS FAILED marker, got: {body_lines[0]!r}"
        )
        assert "timeout after 300s" in body_lines[0]

    def test_digest_has_no_failure_marker_when_synthesis_ok(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])

        text = na.build_digest(
            db_path, self._make_ok_step("label.py"), self._make_ok_step("synthesis.py"),
            window_days=7, struggle_window_days=14, step_timeout=300,
        )
        assert "SYNTHESIS FAILED" not in text

    def test_main_exits_nonzero_when_synthesis_step_fails(self, monkeypatch, tmp_path):
        """End-to-end main(): a synthesis.py step that times out must make
        the whole batch exit non-zero (so systemd shows the unit failed),
        never a silently-swallowed success."""
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        digest_path = tmp_path / "digest.md"

        def fake_run_step(name, cmd, timeout):
            if name == "synthesis.py":
                return self._make_timeout_step(name, timeout)
            return self._make_ok_step(name)

        monkeypatch.setattr(na, "run_step", fake_run_step)
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])
        monkeypatch.setattr(
            sys, "argv",
            [
                "nightly_analysis.py",
                "--db", str(db_path),
                "--digest-path", str(digest_path),
            ],
        )

        rc = na.main()
        assert rc != 0, "main() must exit non-zero when synthesis.py fails/times out"
        assert digest_path.exists()
        assert "SYNTHESIS FAILED" in digest_path.read_text()

    def test_main_exits_zero_when_all_steps_ok(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        digest_path = tmp_path / "digest.md"

        monkeypatch.setattr(
            na, "run_step",
            lambda name, cmd, timeout: self._make_ok_step(name),
        )
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])
        monkeypatch.setattr(
            sys, "argv",
            [
                "nightly_analysis.py",
                "--db", str(db_path),
                "--digest-path", str(digest_path),
            ],
        )

        rc = na.main()
        assert rc == 0
        assert "SYNTHESIS FAILED" not in digest_path.read_text()


def _make_empty_db(db_path: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, run_key TEXT, project TEXT,
            agent_kind TEXT DEFAULT 'host_claude_code',
            started TEXT, ended TEXT, close_reason TEXT,
            tool_histogram TEXT DEFAULT '{}',
            outcome TEXT, labeled_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
