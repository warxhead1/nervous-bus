"""tests/test_nightly_analysis.py — issue #32 (fail-loud) + issue #34
(tool_call coverage gauge) for nightly_analysis.py.
"""
from __future__ import annotations

import json
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

        def fake_run_step(name, cmd, timeout, **_):
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
            lambda name, cmd, timeout, **_: self._make_ok_step(name),
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


class TestCorrectionAndBypassSections:
    def test_digest_reports_user_correction_and_skill_opportunity(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)

        def fake_run_json(name, cmd, timeout):
            if "user_correction/skill_opportunity" in name:
                return [
                    {"detector": "user_correction", "signature": "proj:user_correction:sudo", "n": 2},
                    {"detector": "skill_opportunity", "signature": "beads", "n": 5},
                ]
            return []

        monkeypatch.setattr(na, "_run_json", fake_run_json)
        ok = na.StepResult("x", True, "ok", 1.0, "", "")
        text = na.build_digest(db_path, ok, ok, window_days=7,
                               struggle_window_days=14, step_timeout=300)
        assert "## User corrections by theme (7d)" in text
        assert "- sudo: 2 run(s)" in text
        assert "## Skill bypasses (CLI used, skill not loaded) (7d)" in text
        assert "- beads: 5 run(s)" in text

    def test_digest_sections_present_when_empty(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])
        ok = na.StepResult("x", True, "ok", 1.0, "", "")
        text = na.build_digest(db_path, ok, ok, window_days=7,
                               struggle_window_days=14, step_timeout=300)
        assert "- no user corrections in window" in text
        assert "- no skill bypasses in window" in text


# ---------------------------------------------------------------------------
# Classifier / suspect-calibration handling in the digest
# ---------------------------------------------------------------------------

def _ok_step():
    return na.StepResult("x", True, "ok", 1.0, "", "")


def _digest(monkeypatch, tmp_path, fake_run_json):
    db_path = tmp_path / "runs.db"
    _make_empty_db(db_path)
    monkeypatch.setattr(na, "_run_json", fake_run_json)
    return na.build_digest(db_path, _ok_step(), _ok_step(), window_days=7,
                           struggle_window_days=14, step_timeout=300)


def _section(text, heading):
    """Body of the '## heading' section (up to the next '## ' heading)."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


class TestPrevalenceSection:
    def test_suspect_rows_are_listed_apart_and_never_ranked(self, monkeypatch, tmp_path):
        rows = [
            {"project": "calm", "detector": "worktree_leak", "hit_runs": 3, "total_runs": 40,
             "rate": 0.075, "suspect_calibration": False},
            {"project": "hot", "detector": "noisy_detector", "hit_runs": 90, "total_runs": 100,
             "rate": 0.9, "suspect_calibration": True},
        ]

        def fake(name, cmd, timeout):
            return rows if "query.py prevalence" in name else []

        text = _digest(monkeypatch, tmp_path, fake)
        section = _section(text, "## Top detector prevalence (7d)")
        ranked, _, suspect = section.partition("### Suspect calibration (not ranked)")
        assert "| calm | worktree_leak |" in ranked
        assert "noisy_detector" not in ranked
        assert "| hot | noisy_detector |" in suspect

    def test_no_suspect_heading_when_nothing_is_suspect(self, monkeypatch, tmp_path):
        rows = [{"project": "calm", "detector": "worktree_leak", "hit_runs": 3, "total_runs": 40,
                 "rate": 0.075, "suspect_calibration": False}]
        text = _digest(monkeypatch, tmp_path,
                       lambda name, cmd, timeout: rows if "query.py prevalence" in name else [])
        assert "Suspect calibration" not in text


class TestTaxonomySection:
    def _classifications(self):
        return [
            {"project": "p1", "detector": "failure_taxonomy", "label": "planning_failure",
             "classified": True, "runs": 4, "total_runs": 30},
            {"project": "p2", "detector": "failure_taxonomy", "label": "context_failure",
             "classified": True, "runs": 2, "total_runs": 9},
            {"project": "p1", "detector": "failure_taxonomy", "label": "unclassified",
             "classified": False, "runs": 25, "total_runs": 30},
            {"project": "p1", "detector": "failure_taxonomy",
             "label": "planning_failure_unconfirmed_cadence",
             "classified": False, "runs": 3, "total_runs": 30},
        ]

    def test_unclassified_is_not_summed_into_the_failure_buckets(self, monkeypatch, tmp_path):
        rows = self._classifications()
        text = _digest(monkeypatch, tmp_path,
                       lambda name, cmd, timeout: rows if "query.py classifications" in name else [])
        section = _section(text, "## Failure taxonomy (7d)")
        totals, _, others = section.partition("### Not classified as failures")
        assert "- planning_failure: 4 run(s)" in totals
        assert "- context_failure: 2 run(s)" in totals
        assert "unclassified" not in totals
        assert "- unclassified: 25 run(s)" in others
        assert "- planning_failure_unconfirmed_cadence: 3 run(s)" in others
        by_project = others.partition("### By project")[2]
        assert "| p1 | planning_failure | 4 |" in by_project
        assert "unclassified" not in by_project

    def test_empty_classifications_render_the_empty_line(self, monkeypatch, tmp_path):
        text = _digest(monkeypatch, tmp_path, lambda name, cmd, timeout: [])
        assert "- no failure-taxonomy hits in window (or query unavailable)" in text


class TestDigestWindowFollowsRunEnd:
    """The digest's detector_hits queries window on the run's end, not on the
    hit's recording time (a late synthesis pass records old runs' hits 'now')."""

    def _db(self, tmp_path):
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from detectors.base import ensure_detector_schema

        fmt = "%Y-%m-%dT%H:%M:%SZ"
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=40)).strftime(fmt)
        recent = (now - timedelta(hours=2)).strftime(fmt)
        db_path = tmp_path / "runs.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, run_key TEXT, project TEXT, agent_kind TEXT,
                session_id TEXT, started TEXT, ended TEXT, close_reason TEXT,
                tool_histogram TEXT DEFAULT '{}', outcome TEXT, labeled_at TEXT,
                event_count INTEGER DEFAULT 0, label_history TEXT DEFAULT '[]'
            );
            """
        )
        ensure_detector_schema(conn)
        for rid, ended in (("old-run", old), ("new-run", recent)):
            conn.execute(
                "INSERT INTO runs (run_id, run_key, project, agent_kind, session_id, started, ended, "
                "close_reason) VALUES (?,?,?,?,?,?,?,'idle_timeout')",
                (rid, rid, "proj", "host_claude_code", "s", ended, ended),
            )
            # Both hits were RECORDED just now.
            conn.execute(
                "INSERT INTO detector_hits (run_id, detector, signature, project, ts) "
                "VALUES (?, 'user_correction', 'proj:user_correction:sudo', 'proj', ?)",
                (rid, recent),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_late_recorded_hit_for_an_old_run_is_not_counted(self, monkeypatch, tmp_path):
        db_path = self._db(tmp_path)
        real = na._run_json

        def only_query_py(name, cmd, timeout):
            return real(name, cmd, timeout) if "query.py" in name else []

        monkeypatch.setattr(na, "_run_json", only_query_py)
        text = na.build_digest(db_path, _ok_step(), _ok_step(), window_days=7,
                               struggle_window_days=14, step_timeout=120)
        assert "- sudo: 1 run(s)" in text  # new-run only; old-run ended 40d ago


# ---------------------------------------------------------------------------
# Issue #133 — label.py runs under a budget, reports progress, and a streak of
# failed label nights fails the unit
# ---------------------------------------------------------------------------

class TestLabelStep:
    def _argv(self, db_path, digest_path):
        return ["nightly_analysis.py", "--db", str(db_path), "--digest-path", str(digest_path)]

    def test_label_argv_ends_before_the_step_kill(self, tmp_path):
        argv = na.label_argv(tmp_path / "runs.db", 30, 300)
        i = argv.index("--budget-seconds")
        assert float(argv[i + 1]) == 300 - na.LABEL_BUDGET_MARGIN_S
        assert "--unlabeled-only" in argv and argv[argv.index("--since-days") + 1] == "30"
        assert float(na.label_argv(tmp_path / "runs.db", 30, 10)[i + 1]) >= 1

    def test_main_passes_the_budget_and_echoes_label_progress(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        seen = {}

        def fake_run_step(name, cmd, timeout, echo_stdout_lines=0):
            seen[name] = (cmd, echo_stdout_lines)
            return na.StepResult(name, True, "ok", 1.0, "", "")

        monkeypatch.setattr(na, "run_step", fake_run_step)
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])
        monkeypatch.setattr(sys, "argv", self._argv(db_path, tmp_path / "d.md"))
        assert na.main() == 0
        cmd, echo = seen["label.py"]
        assert "--budget-seconds" in cmd
        assert echo == na.LABEL_ECHO_LINES

    def test_echo_tail_copies_the_last_lines_to_stderr(self, capsys):
        na._echo_tail("label.py", "a\nb\n[label] 40/90 runs\n[label] done\n", 2)
        err = capsys.readouterr().err
        assert "[label] 40/90 runs" in err and "[label] done" in err and "| a" not in err
        na._echo_tail("label.py", b"[label] bytes\n", 5)
        assert "[label] bytes" in capsys.readouterr().err

    def test_two_failed_label_nights_fail_the_unit_and_a_clean_night_resets(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        label_ok = {"v": False}

        def fake_run_step(name, cmd, timeout, echo_stdout_lines=0):
            if name == "label.py" and not label_ok["v"]:
                return na.StepResult(name, False, "timeout after 300s", 300.0, "", "")
            return na.StepResult(name, True, "ok", 1.0, "", "")

        monkeypatch.setattr(na, "run_step", fake_run_step)
        monkeypatch.setattr(na, "_run_json", lambda *a, **k: [])
        monkeypatch.setattr(sys, "argv", self._argv(db_path, tmp_path / "d.md"))
        assert na.main() == 0, "one failed label night stays non-fatal"
        assert na.main() == 1, "the second failed night in a row fails the unit"
        label_ok["v"] = True
        assert na.main() == 0
        state = json.loads((tmp_path / na.STATE_FILE).read_text())
        assert state["label_failures"] == 0 and state["label_last"]["ok"] is True

    def test_unreadable_state_file_never_fails_the_batch(self, tmp_path):
        (tmp_path / na.STATE_FILE).write_text("{not json")
        res = na.StepResult("label.py", False, "exit 1", 1.0, "", "")
        assert na.record_label_result(tmp_path / "runs.db", res) == 1


class TestPendingVsParked:
    def _db(self, tmp_path, with_state=True):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cols = ("run_id TEXT, project TEXT, started TEXT, outcome TEXT, labeled_at TEXT, event_count INT, "
                "tool_histogram TEXT DEFAULT '{}', label_history TEXT")
        if with_state:
            cols += ", label_state TEXT"
        conn.execute(f"CREATE TABLE runs ({cols})")
        rows = [("a", "p", "2026-11-01T00:00:00Z", "landed", "2026-11-02T00:00:00Z", 5, None),
                ("b", "p", "2026-11-01T00:00:00Z", None, None, 5, None),
                ("c", "p", "2026-11-01T00:00:00Z", None, None, 5, "unattributable"),
                ("d", "p", "2026-11-01T00:00:00Z", None, None, 5, "rolled_up")]
        for r in rows:
            names = "run_id, project, started, outcome, labeled_at, event_count" + (", label_state" if with_state else "")
            conn.execute(f"INSERT INTO runs ({names}) VALUES ({','.join('?' * (7 if with_state else 6))})",
                         r if with_state else r[:6])
        return conn

    def test_parked_runs_are_split_out_of_unlabeled(self, tmp_path):
        import query
        row = query.query_stats(self._db(tmp_path))[0]
        assert (row["labeled_runs"], row["unlabeled_runs"]) == (1, 3)
        assert (row["pending_runs"], row["parked_runs"]) == (1, 2)

    def test_store_without_label_state_reports_all_unlabeled_as_pending(self, tmp_path):
        import query
        row = query.query_stats(self._db(tmp_path, with_state=False))[0]
        assert (row["pending_runs"], row["parked_runs"]) == (3, 0)

    def test_digest_shows_pending_and_parked(self, monkeypatch, tmp_path):
        db_path = tmp_path / "runs.db"
        _make_empty_db(db_path)
        stats = [{"project": "p", "total_runs": 4, "labeled_runs": 1, "unlabeled_runs": 3,
                  "pending_runs": 1, "parked_runs": 2, "outcome_breakdown": {}}]
        monkeypatch.setattr(na, "_run_json", lambda name, *a, **k: stats if "stats" in name else [])
        ok = na.StepResult("x", True, "ok", 1.0, "", "")
        text = na.build_digest(db_path, ok, ok, window_days=7, struggle_window_days=14,
                               step_timeout=300)
        assert "| project | runs | labeled | pending | parked |" in text
        assert "| p | 4 | 1 | 1 | 2 |" in text
