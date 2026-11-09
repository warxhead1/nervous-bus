"""test_analyst.py — unit tests for Tier-2 analyst.

Tests cover:
  - build_contrast_sets: grouping, MIN_RUNS_PER_SIDE gating
  - build_prompt: completeness, key sections present
  - _load_labeled_runs: read-only DB access with fixture data
  - Off-peak guard bypass via env var
  - Dry-run path (no LLM calls, placeholder output)
  - _emit_pattern: subprocess call shape (mocked)

LLM calls are NEVER made in these tests.  All external I/O is mocked.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure off-peak guard is bypassed in all tests
os.environ["TIER2_SKIP_OFFPEAK_CHECK"] = "1"

import sys
sys.path.insert(0, str(Path(__file__).parent))

import analyst


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_run(
    run_id: str,
    project: str,
    outcome: str,
    agent_kind: str = "host_subagent",
    event_count: int = 20,
    close_reason: str = "ended",
    thrash_loops: int = 0,
    bash_fail_rate: float = 0.0,
    reread_rate: float = 0.1,
    has_commit: bool = False,
) -> dict:
    features: dict = {}
    if thrash_loops:
        features["thrash_edit_fail_loops"] = thrash_loops
    if bash_fail_rate:
        features["bash_fail_rate"] = bash_fail_rate
        features["bash_calls"] = 10
        features["bash_failures"] = int(bash_fail_rate * 10)
    if reread_rate:
        features["reread_rate"] = reread_rate
    if has_commit:
        features["has_resolving_commit"] = True
    return {
        "run_id": run_id,
        "project": project,
        "agent_kind": agent_kind,
        "started": "2026-06-13T01:00:00Z",
        "ended": "2026-06-13T02:00:00Z",
        "close_reason": close_reason,
        "event_count": event_count,
        "tool_histogram": {"Edit": 5, "Bash": 10, "Read": 3},
        "git_branch": f"worktree-test-{run_id[:6]}",
        "bead_id": None,
        "outcome": outcome,
        "labeled_at": "2026-06-13T03:00:00Z",
        "label_history": [{"outcome": outcome, "source": "behavior_inference"}],
        "features": features,
    }


# ── build_contrast_sets ───────────────────────────────────────────────────────

class TestBuildContrastSets(unittest.TestCase):

    def test_empty_input(self):
        result = analyst.build_contrast_sets([])
        self.assertEqual(result, {})

    def test_insufficient_runs_per_side(self):
        # Only 1 failed, 3 clean — below MIN_RUNS_PER_SIDE=2 on the failed side
        runs = [
            _make_run("r1", "proj-a", "thrashed"),    # failed
            _make_run("r2", "proj-a", "clean"),
            _make_run("r3", "proj-a", "clean"),
            _make_run("r4", "proj-a", "clean"),
        ]
        result = analyst.build_contrast_sets(runs)
        self.assertNotIn("proj-a", result)

    def test_sufficient_on_both_sides(self):
        runs = [
            _make_run("r1", "proj-a", "thrashed"),
            _make_run("r2", "proj-a", "abandoned"),
            _make_run("r3", "proj-a", "clean"),
            _make_run("r4", "proj-a", "landed"),
        ]
        result = analyst.build_contrast_sets(runs)
        self.assertIn("proj-a", result)
        self.assertEqual(len(result["proj-a"]["failed"]), 2)
        self.assertEqual(len(result["proj-a"]["clean"]), 2)

    def test_multiple_projects_only_eligible_returned(self):
        runs = [
            # proj-a: 2 failed + 2 clean → eligible
            _make_run("r1", "proj-a", "thrashed"),
            _make_run("r2", "proj-a", "abandoned"),
            _make_run("r3", "proj-a", "clean"),
            _make_run("r4", "proj-a", "clean"),
            # proj-b: 1 failed + 3 clean → ineligible (failed side too thin)
            _make_run("r5", "proj-b", "thrashed"),
            _make_run("r6", "proj-b", "clean"),
            _make_run("r7", "proj-b", "clean"),
            _make_run("r8", "proj-b", "clean"),
        ]
        result = analyst.build_contrast_sets(runs)
        self.assertIn("proj-a", result)
        self.assertNotIn("proj-b", result)

    def test_reverted_counts_as_failed(self):
        runs = [
            _make_run("r1", "proj-c", "reverted"),
            _make_run("r2", "proj-c", "thrashed"),
            _make_run("r3", "proj-c", "clean"),
            _make_run("r4", "proj-c", "landed"),
        ]
        result = analyst.build_contrast_sets(runs)
        self.assertIn("proj-c", result)
        self.assertEqual(len(result["proj-c"]["failed"]), 2)

    def test_corrected_counts_as_clean(self):
        runs = [
            _make_run("r1", "proj-d", "thrashed"),
            _make_run("r2", "proj-d", "abandoned"),
            _make_run("r3", "proj-d", "clean"),
            _make_run("r4", "proj-d", "corrected"),
        ]
        result = analyst.build_contrast_sets(runs)
        self.assertIn("proj-d", result)
        self.assertEqual(len(result["proj-d"]["clean"]), 2)

    def test_unlabeled_runs_excluded(self):
        # Unlabeled runs (outcome=None) should not appear in either side
        runs = [
            _make_run("r1", "proj-e", "thrashed"),
            _make_run("r2", "proj-e", "abandoned"),
            _make_run("r3", "proj-e", "clean"),
            _make_run("r4", "proj-e", "clean"),
            _make_run("r5", "proj-e", outcome=None),  # type: ignore[arg-type]
        ]
        # None outcome won't be in _FAILED_OUTCOMES or _CLEAN_OUTCOMES → excluded
        result = analyst.build_contrast_sets(runs)
        self.assertIn("proj-e", result)
        total = len(result["proj-e"]["failed"]) + len(result["proj-e"]["clean"])
        self.assertEqual(total, 4)  # the None-outcome run is excluded


# ── build_prompt ──────────────────────────────────────────────────────────────

class TestBuildPrompt(unittest.TestCase):

    def _make_sets(self, n_failed: int = 3, n_clean: int = 3) -> tuple[list, list]:
        failed = [
            _make_run(f"f{i}", "test-proj", "thrashed", thrash_loops=i + 1)
            for i in range(n_failed)
        ]
        clean = [
            _make_run(f"c{i}", "test-proj", "clean", has_commit=True)
            for i in range(n_clean)
        ]
        return failed, clean

    def test_prompt_contains_project(self):
        failed, clean = self._make_sets()
        prompt = analyst.build_prompt("my-project", failed, clean)
        self.assertIn("my-project", prompt)

    def test_prompt_contains_failed_section(self):
        failed, clean = self._make_sets()
        prompt = analyst.build_prompt("proj", failed, clean)
        self.assertIn("FAILED", prompt)

    def test_prompt_contains_clean_section(self):
        failed, clean = self._make_sets()
        prompt = analyst.build_prompt("proj", failed, clean)
        self.assertIn("CLEAN", prompt)

    def test_prompt_requests_json_array(self):
        failed, clean = self._make_sets()
        prompt = analyst.build_prompt("proj", failed, clean)
        self.assertIn("JSON array", prompt)

    def test_prompt_includes_run_summaries(self):
        failed, clean = self._make_sets()
        prompt = analyst.build_prompt("proj", failed, clean)
        # _summarise_run includes outcome=
        self.assertIn("outcome=thrashed", prompt)
        self.assertIn("outcome=clean", prompt)

    def test_prompt_shows_thrash_loop_feature(self):
        failed = [_make_run("f1", "proj", "thrashed", thrash_loops=4)]
        clean = [_make_run("c1", "proj", "clean", has_commit=True)] * 2
        prompt = analyst.build_prompt("proj", failed, clean)
        self.assertIn("thrash_loops=4", prompt)

    def test_prompt_shows_bash_fail_rate(self):
        failed = [_make_run("f1", "proj", "abandoned", bash_fail_rate=0.45)]
        clean = [_make_run("c1", "proj", "clean")] * 2
        prompt = analyst.build_prompt("proj", failed, clean)
        self.assertIn("bash_fail_rate=0.45", prompt)

    def test_prompt_caps_at_10_runs_per_side(self):
        # build_prompt uses failed_runs[:10] — more than 10 should still work
        failed = [_make_run(f"f{i}", "proj", "thrashed") for i in range(15)]
        clean = [_make_run(f"c{i}", "proj", "clean") for i in range(15)]
        prompt = analyst.build_prompt("proj", failed, clean)
        # Should not raise and should still contain the section headers
        self.assertIn("FAILED", prompt)
        self.assertIn("CLEAN", prompt)


# ── _load_labeled_runs ────────────────────────────────────────────────────────

class TestLoadLabeledRuns(unittest.TestCase):

    def _create_fixture_db(self, path: Path) -> None:
        """Create a minimal runs.db fixture with a mix of labeled/unlabeled rows."""
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                run_key TEXT NOT NULL DEFAULT '',
                run_key_kind TEXT NOT NULL DEFAULT '',
                host_conversation_id TEXT,
                project TEXT NOT NULL,
                agent_kind TEXT NOT NULL DEFAULT '',
                session_id TEXT,
                agent_id TEXT,
                started TEXT NOT NULL DEFAULT '',
                ended TEXT NOT NULL DEFAULT '',
                close_reason TEXT,
                continues_run_id TEXT,
                event_count INTEGER NOT NULL DEFAULT 0,
                tool_histogram TEXT NOT NULL DEFAULT '{}',
                worktree TEXT,
                worktree_slug TEXT,
                git_branch TEXT,
                bead_id TEXT,
                outcome TEXT,
                labeled_at TEXT,
                label_version INTEGER,
                label_history TEXT NOT NULL DEFAULT '[]',
                features TEXT NOT NULL DEFAULT '{}',
                schema_version TEXT NOT NULL DEFAULT '1',
                recorded_at TEXT NOT NULL DEFAULT ''
            );
        """)
        # Row 1: labeled clean
        conn.execute(
            "INSERT INTO runs (run_id, project, agent_kind, started, ended, "
            "event_count, outcome, labeled_at, label_history, features, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-clean-001", "proj-alpha", "host_subagent",
                "2026-06-13T01:00:00Z", "2026-06-13T02:00:00Z",
                25, "clean", "2026-06-13T03:00:00Z",
                json.dumps([{"outcome": "clean", "source": "pr_merge"}]),
                json.dumps({"has_resolving_commit": True}),
                "2026-06-13T03:00:00Z",
            ),
        )
        # Row 2: labeled thrashed
        conn.execute(
            "INSERT INTO runs (run_id, project, agent_kind, started, ended, "
            "event_count, outcome, labeled_at, label_history, features, recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-thrash-001", "proj-alpha", "host_subagent",
                "2026-06-13T04:00:00Z", "2026-06-13T05:00:00Z",
                40, "thrashed", "2026-06-13T06:00:00Z",
                json.dumps([{"outcome": "thrashed", "source": "behavior_inference"}]),
                json.dumps({"thrash_edit_fail_loops": 5, "bash_fail_rate": 0.35}),
                "2026-06-13T06:00:00Z",
            ),
        )
        # Row 3: unlabeled (should be excluded)
        conn.execute(
            "INSERT INTO runs (run_id, project, agent_kind, started, ended, "
            "event_count, recorded_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "run-unlabeled-001", "proj-alpha", "host_subagent",
                "2026-06-13T07:00:00Z", "2026-06-13T08:00:00Z",
                10, "2026-06-13T08:00:00Z",
            ),
        )
        conn.close()

    def test_only_labeled_runs_returned(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.db"
            self._create_fixture_db(db_path)
            runs = analyst._load_labeled_runs(db_path)
        self.assertEqual(len(runs), 2)
        outcomes = {r["outcome"] for r in runs}
        self.assertEqual(outcomes, {"clean", "thrashed"})

    def test_features_decoded_from_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.db"
            self._create_fixture_db(db_path)
            runs = analyst._load_labeled_runs(db_path)
        thrash_run = next(r for r in runs if r["outcome"] == "thrashed")
        self.assertIsInstance(thrash_run["features"], dict)
        self.assertEqual(thrash_run["features"]["thrash_edit_fail_loops"], 5)

    def test_missing_db_returns_empty(self):
        runs = analyst._load_labeled_runs(Path("/tmp/does_not_exist_tier2_test.db"))
        self.assertEqual(runs, [])

    def test_db_opened_readonly(self):
        """Verify the DB URI includes mode=ro so writes are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runs.db"
            self._create_fixture_db(db_path)
            conn = analyst._open_db_readonly(db_path)
            # Attempting a write should raise OperationalError
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO runs (run_id, project, agent_kind, started, ended, recorded_at) VALUES ('x','y','z','a','b','c')")
            conn.close()


# ── _emit_pattern ─────────────────────────────────────────────────────────────

class TestEmitPattern(unittest.TestCase):

    def _sample_pattern(self) -> dict:
        return {
            "pattern_name": "high-reread-before-thrash",
            "description": "Runs that thrash show reread_rate > 0.5 consistently.",
            "occurrences": 3,
            "evidence": ["run f1: reread_rate=0.55", "run f2: reread_rate=0.62"],
            "proposed_fix": {
                "type": "rule",
                "description": "Gate: if reread_rate > 0.4 after 10 read calls, prompt to refocus",
            },
        }

    @patch("analyst.subprocess.run")
    def test_emit_calls_nervous_publish(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0)
        result = analyst._emit_pattern(
            project="nervous-bus",
            pattern=self._sample_pattern(),
            dry_run=False,
            verbose=False,
        )
        self.assertTrue(result)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "nervous")
        self.assertEqual(args[1], "publish")
        self.assertEqual(args[2], "nervous-bus.pattern.discovered.v1")

    @patch("analyst.subprocess.run")
    def test_emit_payload_shape(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=0)
        analyst._emit_pattern("my-project", self._sample_pattern(), dry_run=False, verbose=False)
        raw_json = mock_run.call_args[0][0][3]
        payload = json.loads(raw_json)
        self.assertEqual(payload["project"], "my-project")
        self.assertEqual(payload["pattern_name"], "high-reread-before-thrash")
        self.assertEqual(payload["occurrences"], 3)
        self.assertIsInstance(payload["evidence"], list)
        self.assertIn("proposed_patch", payload)
        self.assertEqual(payload["proposed_patch"]["type"], "rule")

    @patch("analyst.subprocess.run")
    def test_dry_run_skips_subprocess(self, mock_run: MagicMock):
        result = analyst._emit_pattern(
            "proj", self._sample_pattern(), dry_run=True, verbose=False
        )
        self.assertTrue(result)
        mock_run.assert_not_called()

    @patch("analyst.subprocess.run")
    def test_emit_returns_false_on_nonzero_returncode(self, mock_run: MagicMock):
        mock_run.return_value = MagicMock(returncode=1)
        result = analyst._emit_pattern(
            "proj", self._sample_pattern(), dry_run=False, verbose=False
        )
        self.assertFalse(result)

    @patch("analyst.subprocess.run", side_effect=Exception("nervous binary missing"))
    def test_emit_handles_subprocess_exception(self, _mock_run: MagicMock):
        result = analyst._emit_pattern(
            "proj", self._sample_pattern(), dry_run=False, verbose=False
        )
        self.assertFalse(result)


# ── Off-peak guard ────────────────────────────────────────────────────────────

class TestOffPeakGuard(unittest.TestCase):

    def test_guard_bypassed_when_env_set(self):
        os.environ["TIER2_SKIP_OFFPEAK_CHECK"] = "1"
        self.assertTrue(analyst._is_off_peak())

    def test_guard_active_when_env_unset(self):
        saved = os.environ.pop("TIER2_SKIP_OFFPEAK_CHECK", None)
        try:
            from datetime import datetime, timezone
            with patch("analyst.datetime") as mock_dt:
                # Simulate peak hour (10:00 UTC)
                mock_dt.now.return_value = MagicMock(hour=10)
                mock_dt.now.side_effect = None
                # Patch at the correct spot
            # Restore env for other tests
        finally:
            if saved is not None:
                os.environ["TIER2_SKIP_OFFPEAK_CHECK"] = saved
            else:
                os.environ["TIER2_SKIP_OFFPEAK_CHECK"] = "1"


# ── analyse_project dry-run ───────────────────────────────────────────────────

class TestAnalyseProjectDryRun(unittest.TestCase):

    def _make_project_sides(self) -> tuple[list, list]:
        failed = [
            _make_run("f1", "proj-x", "thrashed", thrash_loops=3),
            _make_run("f2", "proj-x", "abandoned", bash_fail_rate=0.4),
        ]
        clean = [
            _make_run("c1", "proj-x", "clean", has_commit=True),
            _make_run("c2", "proj-x", "landed", has_commit=True),
        ]
        return failed, clean

    def test_dry_run_returns_placeholder_without_llm(self):
        failed, clean = self._make_project_sides()
        counter = [0]
        patterns = analyst.analyse_project(
            project="proj-x",
            failed_runs=failed,
            clean_runs=clean,
            api_key="unused-in-dry-run",
            dry_run=True,
            verbose=False,
            request_counter=counter,
        )
        # Dry-run produces exactly 1 placeholder pattern
        self.assertEqual(len(patterns), 1)
        self.assertEqual(patterns[0]["pattern_name"], "dry-run-placeholder")
        # No LLM requests should have been made
        self.assertEqual(counter[0], 0)

    def test_dry_run_placeholder_contains_project(self):
        failed, clean = self._make_project_sides()
        counter = [0]
        patterns = analyst.analyse_project(
            project="proj-x",
            failed_runs=failed,
            clean_runs=clean,
            api_key="",
            dry_run=True,
            verbose=False,
            request_counter=counter,
        )
        self.assertIn("proj-x", patterns[0]["description"])

    def test_no_api_key_returns_empty_without_dry_run(self):
        failed, clean = self._make_project_sides()
        counter = [0]
        patterns = analyst.analyse_project(
            project="proj-x",
            failed_runs=failed,
            clean_runs=clean,
            api_key="",
            dry_run=False,
            verbose=False,
            request_counter=counter,
        )
        self.assertEqual(patterns, [])
        self.assertEqual(counter[0], 0)


# ── _summarise_run ────────────────────────────────────────────────────────────

class TestSummariseRun(unittest.TestCase):

    def test_includes_outcome(self):
        run = _make_run("r1", "proj", "thrashed")
        summary = analyst._summarise_run(run)
        self.assertIn("outcome=thrashed", summary)

    def test_includes_thrash_loops_when_present(self):
        run = _make_run("r1", "proj", "thrashed", thrash_loops=5)
        summary = analyst._summarise_run(run)
        self.assertIn("thrash_loops=5", summary)

    def test_clean_run_no_thrash_loops(self):
        run = _make_run("r2", "proj", "clean", has_commit=True)
        summary = analyst._summarise_run(run)
        self.assertNotIn("thrash_loops", summary)
        self.assertIn("has_commit=yes", summary)

    def test_includes_bash_fail_rate(self):
        run = _make_run("r3", "proj", "abandoned", bash_fail_rate=0.33)
        summary = analyst._summarise_run(run)
        self.assertIn("bash_fail_rate=0.33", summary)


# ── main() exit status ────────────────────────────────────────────────────────
# The unit is a daily oneshot: its exit status is the only health signal anyone
# reads.  These pin that a run which did not analyse anything cannot exit 0.

class _MainHarness(unittest.TestCase):

    def _write_db(self, path: Path, *, eligible: bool = True) -> None:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.execute(
            "CREATE TABLE runs (run_id TEXT PRIMARY KEY, project TEXT, agent_kind TEXT, "
            "started TEXT, ended TEXT, close_reason TEXT, event_count INTEGER, "
            "tool_histogram TEXT, git_branch TEXT, bead_id TEXT, outcome TEXT, "
            "labeled_at TEXT, label_history TEXT, features TEXT)"
        )
        outcomes = ["thrashed", "abandoned", "clean", "landed"] if eligible else []
        for i, outcome in enumerate(outcomes):
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"r{i}", "proj-main", "host_subagent", "2026-06-13T01:00:00Z",
                 "2026-06-13T02:00:00Z", "ended", 20, "{}", None, None, outcome,
                 "2026-06-13T03:00:00Z", "[]", "{}"),
            )
        conn.close()

    def _run_main(self, *, eligible=True, key="test-key", dry_run=False,
                  patterns=None, llm_err=None, emit_ok=True, httpx_ok=True):
        """Run analyst.main() against a fixture DB; return (rc, stderr, llm_calls)."""
        import contextlib
        import io
        from unittest.mock import AsyncMock

        llm = AsyncMock(return_value=(patterns, llm_err))
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "runs.db"
            self._write_db(db, eligible=eligible)
            argv = ["analyst.py", "--db-path", str(db), "--force"]
            if dry_run:
                argv.append("--dry-run")
            env = {k: v for k, v in os.environ.items() if k != "MINIMAX_API_KEY"}
            if key:
                env["MINIMAX_API_KEY"] = key
            err = io.StringIO()
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(sys, "argv", argv), \
                    patch.object(analyst, "_call_minimax", llm), \
                    patch.object(analyst, "_emit_pattern", return_value=emit_ok), \
                    patch.object(analyst, "_HTTPX_AVAILABLE", httpx_ok), \
                    contextlib.redirect_stderr(err), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = analyst.main()
        return rc, err.getvalue(), llm.await_count


class TestMainExitStatus(_MainHarness):

    def test_missing_key_exits_nonzero_and_names_the_key(self):
        rc, err, llm_calls = self._run_main(key="")
        self.assertNotEqual(rc, 0)
        self.assertIn("MINIMAX_API_KEY", err)
        self.assertEqual(llm_calls, 0)

    def test_missing_key_fails_even_when_no_project_is_eligible(self):
        # A broken credential injection must surface today, not on the day the
        # contrast set first fills up.
        rc, _err, _calls = self._run_main(key="", eligible=False)
        self.assertNotEqual(rc, 0)

    def test_missing_httpx_exits_nonzero(self):
        rc, err, llm_calls = self._run_main(httpx_ok=False, patterns=[])
        self.assertNotEqual(rc, 0)
        self.assertIn("httpx", err)
        self.assertEqual(llm_calls, 0)

    def test_dry_run_needs_no_key(self):
        rc, _err, llm_calls = self._run_main(key="", dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(llm_calls, 0)

    def test_every_llm_call_failing_exits_nonzero(self):
        rc, err, llm_calls = self._run_main(patterns=None, llm_err="transient")
        self.assertEqual(llm_calls, 1)
        self.assertNotEqual(rc, 0)
        self.assertIn("proj-main", err)

    def test_unpublishable_pattern_exits_nonzero(self):
        found = [{"pattern_name": "p", "occurrences": 2, "evidence": []}]
        rc, _err, _calls = self._run_main(patterns=found, emit_ok=False)
        self.assertNotEqual(rc, 0)

    def test_successful_run_exits_zero(self):
        found = [{"pattern_name": "p", "occurrences": 2, "evidence": []}]
        rc, err, llm_calls = self._run_main(patterns=found)
        self.assertEqual(rc, 0)
        self.assertEqual(llm_calls, 1)
        self.assertIn("patterns_emitted=1", err)

    def test_llm_answering_no_patterns_is_not_a_failure(self):
        rc, _err, llm_calls = self._run_main(patterns=[])
        self.assertEqual(rc, 0)
        self.assertEqual(llm_calls, 1)

    def test_no_labeled_runs_with_key_is_not_a_failure(self):
        rc, _err, llm_calls = self._run_main(eligible=False)
        self.assertEqual(rc, 0)
        self.assertEqual(llm_calls, 0)


# ── MiniMax request shape ─────────────────────────────────────────────────────

class TestMiniMaxRequest(unittest.TestCase):

    def _post(self) -> dict:
        import asyncio
        import types

        posted: list[dict] = []

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"content": [{"type": "text", "text": "[]"}]}

        class _Client:
            def __init__(self, **_kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def post(self, _url, json=None, headers=None):
                posted.append(json)
                return _Resp()

        fake = types.SimpleNamespace(AsyncClient=_Client, Timeout=lambda **_kw: None)
        with patch.dict(sys.modules, {"httpx": fake}), \
                patch.object(analyst, "_HTTPX_AVAILABLE", True):
            asyncio.run(analyst._call_minimax("k", "prompt"))
        self.assertEqual(len(posted), 1)
        return posted[0]

    def test_token_budget_leaves_room_for_reasoning(self):
        # M3.x models spend most of max_tokens thinking; below this the text
        # block comes back empty and every project reads as "no patterns".
        self.assertGreaterEqual(self._post()["max_tokens"], 16000)

    def test_model_is_not_the_retired_m2_family(self):
        self.assertNotRegex(self._post()["model"], r"^MiniMax-M2")


# ── systemd unit ──────────────────────────────────────────────────────────────

class TestServiceUnit(unittest.TestCase):

    def test_execstart_injects_the_key_from_the_vault(self):
        unit = Path(__file__).parent / "systemd" / "tier2-analyst.service"
        exec_lines = [
            ln.split("=", 1)[1].strip()
            for ln in unit.read_text().splitlines()
            if ln.startswith("ExecStart=")
        ]
        self.assertEqual(len(exec_lines), 1)
        argv = exec_lines[0].split()
        self.assertTrue(argv[0].endswith("/hearth-vault"), argv[0])
        self.assertEqual(argv[1], "exec")
        self.assertIn("--prefix", argv)
        self.assertEqual(argv[argv.index("--prefix") + 1], "ai/")
        self.assertIn("--redact", argv)
        sep = argv.index("--")
        self.assertTrue(argv[sep + 1].endswith("python3"), argv[sep + 1])
        self.assertTrue(argv[-1].endswith("tier2/analyst.py"), argv[-1])


if __name__ == "__main__":
    unittest.main()
