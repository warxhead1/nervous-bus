"""tests/test_enrich.py — Unit tests for enrich.py (PART A + PART C).

Covers:
- git_branch derivation from worktree path / cwd fallback / worktree-gone
- bead_id derivation from branch naming conventions
- Negative cases (structural branches that should return None)
- Token feature folding (PART C) including finalize aggregates
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from enrich import (
    _looks_like_bead_id,
    derive_bead_id,
    derive_git_branch,
    finalize_token_features,
    fold_token_features,
)


# ── git_branch derivation ─────────────────────────────────────────────────────

class TestDeriveGitBranch(unittest.TestCase):
    """Tests for derive_git_branch() — mocked git subprocess."""

    def _mock_git(self, branch: str):
        """Return a context manager that makes _git_branch_at return branch."""
        mock_run = MagicMock()
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = branch + "\n"
        return patch("enrich.subprocess.run", mock_run)

    def _mock_git_fail(self):
        """git fails (worktree gone, not a git repo, etc.)."""
        mock_run = MagicMock()
        mock_run.return_value.returncode = 128
        mock_run.return_value.stdout = ""
        return patch("enrich.subprocess.run", mock_run)

    def test_branch_from_worktree_path(self):
        with self._mock_git("feat/jit-parallel-spirv-prefetch"):
            branch = derive_git_branch(
                worktree_path="/home/eric/projects/tengine/.claude/worktrees/wf_abc",
                cwd=None,
            )
        self.assertEqual(branch, "feat/jit-parallel-spirv-prefetch")

    def test_branch_from_cwd_when_no_worktree(self):
        """Session runs (no worktree) derive branch from cwd."""
        with self._mock_git("reflexarc"):
            branch = derive_git_branch(
                worktree_path=None,
                cwd="/home/eric/projects/nervous-bus",
            )
        self.assertEqual(branch, "reflexarc")

    def test_worktree_path_tried_first(self):
        """Worktree path is tried before cwd."""
        call_args = []

        def fake_run(cmd, **kwargs):
            call_args.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "worktree-branch\n"
            return m

        with patch("enrich.subprocess.run", fake_run):
            branch = derive_git_branch(
                worktree_path="/wt/path",
                cwd="/cwd/path",
            )

        self.assertEqual(branch, "worktree-branch")
        # First call should be for the worktree path
        self.assertIn("/wt/path", call_args[0])

    def test_worktree_gone_falls_back_to_cwd(self):
        """When worktree path gives rc=128, fall back to cwd."""
        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            m = MagicMock()
            if call_count[0] == 1:
                # First call (worktree path) fails
                m.returncode = 128
                m.stdout = ""
            else:
                # Second call (cwd) succeeds
                m.returncode = 0
                m.stdout = "main\n"
            return m

        with patch("enrich.subprocess.run", fake_run):
            branch = derive_git_branch(
                worktree_path="/gone/worktree",
                cwd="/home/eric/projects/nervous-bus",
            )
        # Returns "main" from cwd, but "main" branch is returned as-is by
        # _git_branch_at (we don't filter main here — that's bead_id's job)
        self.assertEqual(branch, "main")

    def test_both_paths_fail_returns_none(self):
        with self._mock_git_fail():
            branch = derive_git_branch(
                worktree_path="/gone",
                cwd="/also/gone",
            )
        self.assertIsNone(branch)

    def test_no_paths_returns_none(self):
        branch = derive_git_branch(worktree_path=None, cwd=None)
        self.assertIsNone(branch)

    def test_head_detached_returns_none(self):
        """git returns 'HEAD' for detached — we return None."""
        with self._mock_git("HEAD"):
            branch = derive_git_branch(worktree_path="/wt", cwd=None)
        self.assertIsNone(branch)

    def test_git_timeout_returns_none(self):
        """Subprocess timeout is handled gracefully."""
        import subprocess as sp

        def timeout_run(*args, **kwargs):
            raise sp.TimeoutExpired(["git"], 3)

        with patch("enrich.subprocess.run", timeout_run):
            branch = derive_git_branch(worktree_path="/wt", cwd="/cwd")
        self.assertIsNone(branch)


# ── bead_id derivation ────────────────────────────────────────────────────────

class TestDeriveBreadId(unittest.TestCase):

    def test_loom_prefix_extracts_bead(self):
        self.assertEqual(derive_bead_id("loom/nervous-bus-22fc"), "nervous-bus-22fc")

    def test_loom_prefix_loom_yluar(self):
        self.assertEqual(derive_bead_id("loom/loom-yluar"), "loom-yluar")

    def test_deer_flow_prefix(self):
        self.assertEqual(derive_bead_id("deer-flow/deer-flow-0y8"), "deer-flow-0y8")

    def test_direct_bead_id_on_branch(self):
        self.assertEqual(derive_bead_id("nervous-bus-fhr1q"), "nervous-bus-fhr1q")
        self.assertEqual(derive_bead_id("nervous-bus-oukii"), "nervous-bus-oukii")

    def test_bead_with_subbead_suffix(self):
        self.assertEqual(derive_bead_id("nervous-bus-fhr1q.1"), "nervous-bus-fhr1q.1")

    def test_main_returns_none(self):
        self.assertIsNone(derive_bead_id("main"))
        self.assertIsNone(derive_bead_id("master"))

    def test_feat_slash_returns_none(self):
        self.assertIsNone(derive_bead_id("feat/jit-parallel-spirv-prefetch"))
        self.assertIsNone(derive_bead_id("fix/poses-camera-latch-trap"))

    def test_worktree_internal_branch_returns_none(self):
        """worktree-agent-... branches are internal, not beads."""
        self.assertIsNone(derive_bead_id("worktree-agent-a3f67d389e54ce3f8"))

    def test_head_returns_none(self):
        self.assertIsNone(derive_bead_id("HEAD"))

    def test_none_input_returns_none(self):
        self.assertIsNone(derive_bead_id(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(derive_bead_id(""))

    def test_task_slash_branch_returns_none(self):
        self.assertIsNone(derive_bead_id("task/assess-the-current-beads"))

    def test_chore_branch_returns_none(self):
        self.assertIsNone(derive_bead_id("chore-ruff-format-baseline"))

    def test_reflexarc_branch_returns_none(self):
        """'reflexarc' looks like a project name but not a bead_id (no hyphens with id)."""
        self.assertIsNone(derive_bead_id("reflexarc"))

    def test_ci_prefix_returns_none(self):
        self.assertIsNone(derive_bead_id("ci-clean-false"))


class TestLooksLikeBeadId(unittest.TestCase):
    def test_typical_bead_ids(self):
        self.assertTrue(_looks_like_bead_id("nervous-bus-fhr1q"))
        self.assertTrue(_looks_like_bead_id("nervous-bus-oukii"))
        self.assertTrue(_looks_like_bead_id("loom-yluar"))
        self.assertTrue(_looks_like_bead_id("deer-flow-0y8"))
        self.assertTrue(_looks_like_bead_id("nervous-bus-fhr1q.1"))

    def test_structural_branches_not_beads(self):
        self.assertFalse(_looks_like_bead_id("main"))
        self.assertFalse(_looks_like_bead_id("reflexarc"))
        self.assertFalse(_looks_like_bead_id("jit-parallel-spirv-prefetch"))

    def test_slash_in_string_is_not_bead(self):
        self.assertFalse(_looks_like_bead_id("feat/foo-bar-baz12"))

    def test_last_segment_too_short(self):
        # 2-char last segment is too short
        self.assertFalse(_looks_like_bead_id("nervous-bus-ab"))

    def test_last_segment_too_long(self):
        # 8-char last segment is too long
        self.assertFalse(_looks_like_bead_id("nervous-bus-toolongid"))

    def test_uppercase_in_last_segment_not_bead(self):
        self.assertFalse(_looks_like_bead_id("nervous-bus-FHR1Q"))


# ── Token feature folding (PART C) ───────────────────────────────────────────

class TestFoldTokenFeatures(unittest.TestCase):

    def _activity(self, **kwargs):
        base = {"event": "tool_call", "tool_name": "Bash"}
        base.update(kwargs)
        return base

    def test_basic_token_accumulation(self):
        features = {}
        fold_token_features(features, self._activity(input_tokens=100, output_tokens=50))
        fold_token_features(features, self._activity(input_tokens=200, output_tokens=30))
        self.assertEqual(features["tokens_input_total"], 300)
        self.assertEqual(features["tokens_output_total"], 80)

    def test_cache_tokens_accumulated(self):
        features = {}
        fold_token_features(features, self._activity(
            cache_read_tokens=400, cache_write_tokens=1000
        ))
        fold_token_features(features, self._activity(
            cache_read_tokens=600
        ))
        self.assertEqual(features["tokens_cache_read"], 1000)
        self.assertEqual(features["tokens_cache_write"], 1000)

    def test_model_tracking(self):
        features = {}
        fold_token_features(features, self._activity(model="claude-sonnet-4-6"))
        fold_token_features(features, self._activity(model="claude-sonnet-4-6"))
        fold_token_features(features, self._activity(model="claude-opus-4-8"))
        self.assertEqual(features["models_seen"]["claude-sonnet-4-6"], 2)
        self.assertEqual(features["models_seen"]["claude-opus-4-8"], 1)

    def test_tool_error_counting(self):
        features = {}
        fold_token_features(features, self._activity(tool_is_error=True, tool_error_type="exit_code"))
        fold_token_features(features, self._activity(tool_is_error=True, tool_error_type="timeout"))
        fold_token_features(features, self._activity())  # no error
        self.assertEqual(features["tool_errors"], 2)
        self.assertEqual(features["tool_error_exit_code"], 1)
        self.assertEqual(features["tool_error_timeout"], 1)

    def test_tool_call_counter(self):
        features = {}
        fold_token_features(features, self._activity(event="tool_call"))
        fold_token_features(features, self._activity(event="tool_call"))
        fold_token_features(features, {"event": "ended"})  # not a tool_call
        self.assertEqual(features.get("tool_calls", 0), 2)

    def test_missing_optional_fields_safe(self):
        """Events without token fields should not crash or add spurious keys."""
        features = {}
        fold_token_features(features, {"event": "tool_call", "tool_name": "Read"})
        self.assertNotIn("tokens_input_total", features)
        self.assertNotIn("tokens_output_total", features)

    def test_tool_error_default_type(self):
        """tool_is_error=True without tool_error_type uses 'error' fallback."""
        features = {}
        fold_token_features(features, self._activity(tool_is_error=True))
        self.assertEqual(features.get("tool_error_error"), 1)


class TestFinalizeTokenFeatures(unittest.TestCase):

    def test_cache_hit_rate(self):
        features = {"tokens_cache_read": 800, "tokens_cache_write": 1000}
        finalize_token_features(features)
        self.assertAlmostEqual(features["cache_hit_rate"], 0.8)

    def test_cache_hit_rate_not_set_when_no_writes(self):
        features = {"tokens_cache_read": 100, "tokens_cache_write": 0}
        finalize_token_features(features)
        self.assertNotIn("cache_hit_rate", features)

    def test_primary_model_max_count(self):
        features = {
            "models_seen": {"claude-sonnet-4-6": 5, "claude-opus-4-8": 2}
        }
        finalize_token_features(features)
        self.assertEqual(features["primary_model"], "claude-sonnet-4-6")

    def test_primary_model_not_set_when_no_models(self):
        features = {}
        finalize_token_features(features)
        self.assertNotIn("primary_model", features)

    def test_tool_error_rate(self):
        features = {"tool_calls": 10, "tool_errors": 3}
        finalize_token_features(features)
        self.assertAlmostEqual(features["tool_error_rate"], 0.3)

    def test_tool_error_rate_zero_when_no_calls(self):
        features = {"tool_calls": 0, "tool_errors": 0}
        finalize_token_features(features)
        self.assertNotIn("tool_error_rate", features)

    def test_full_pipeline_integration(self):
        """Fold 3 events then finalize; verify aggregate correctness."""
        features = {}
        events = [
            {"event": "tool_call", "tool_name": "Bash",
             "input_tokens": 1000, "output_tokens": 200,
             "cache_read_tokens": 500, "cache_write_tokens": 800,
             "model": "claude-sonnet-4-6"},
            {"event": "tool_call", "tool_name": "Read",
             "input_tokens": 800, "output_tokens": 50,
             "cache_read_tokens": 500,
             "model": "claude-sonnet-4-6",
             "tool_is_error": False},
            {"event": "tool_call", "tool_name": "Bash",
             "input_tokens": 900, "output_tokens": 150,
             "model": "claude-sonnet-4-6",
             "tool_is_error": True, "tool_error_type": "exit_code"},
        ]
        for ev in events:
            fold_token_features(features, ev)
        finalize_token_features(features)

        self.assertEqual(features["tokens_input_total"], 2700)
        self.assertEqual(features["tokens_output_total"], 400)
        self.assertEqual(features["tokens_cache_read"], 1000)
        self.assertEqual(features["tokens_cache_write"], 800)
        self.assertAlmostEqual(features["cache_hit_rate"], 1.25)
        self.assertEqual(features["primary_model"], "claude-sonnet-4-6")
        self.assertEqual(features["tool_errors"], 1)
        self.assertAlmostEqual(features["tool_error_rate"], 1 / 3, places=3)


if __name__ == "__main__":
    unittest.main()
