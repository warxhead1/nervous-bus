"""tests/test_worktree_leak.py — Unit tests for WorktreeLeakDetector.

Covers:
  - Positive: a terminal-outcome run whose worktree path exists on disk AND
    is registered with git → fires.
  - Negative: worktree dir missing from disk → no fire.
  - Negative: worktree exists but NOT in git worktree list → no fire (orphan).
  - CRITICAL: a bare slug must NOT match git worktree list output
    (audit finding b1a — slug-vs-absolute join silently returns ZERO).
  - Signature is stable (does not contain run_id).
  - Proposed remediation is present and references the worktree path.
  - Prevalence and recurrence integrate correctly with a leaked worktree.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_ADAPTER_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ADAPTER_ROOT))

from detectors.base import ensure_detector_schema, _now_utc
from detectors.worktree_leak import WorktreeLeakDetector, _git_worktree_paths, _infer_repo_root


# ── Minimal schema ────────────────────────────────────────────────────────────

_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    worktree      TEXT,
    worktree_slug TEXT,
    git_branch    TEXT,
    bead_id       TEXT,
    outcome       TEXT,
    labeled_at    TEXT,
    ended         TEXT NOT NULL,
    close_reason  TEXT
);
"""


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_RUNS_SCHEMA)
    ensure_detector_schema(conn)
    return conn


def _insert_run(
    conn, run_id, project, worktree=None, outcome="clean",
    worktree_slug=None, git_branch=None, bead_id=None,
    labeled_at="AUTO",
):
    """Insert a run into the test DB.

    labeled_at="AUTO" (default) → set to now (so outcome is trusted).
    labeled_at=None             → NULL in DB (outcome NOT trusted by detector).
    labeled_at=<str>            → use that literal value.

    The WorktreeLeakDetector now requires labeled_at IS NOT NULL to trust
    any outcome (null-vs-clean discipline, 2026-06 contract).
    """
    la = _now_utc() if labeled_at == "AUTO" else labeled_at
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (run_id, project, worktree, worktree_slug, git_branch, bead_id, outcome, labeled_at, ended)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (run_id, project, worktree, worktree_slug, git_branch, bead_id, outcome, la, _now_utc()),
    )


# ── Helper: fake git worktree list output ─────────────────────────────────────

def _make_git_output(*paths: str) -> str:
    """Build --porcelain git worktree list output for the given absolute paths."""
    lines = []
    for p in paths:
        lines.append(f"worktree {p}")
        lines.append("HEAD abc123def456")
        lines.append("branch refs/heads/main")
        lines.append("")
    return "\n".join(lines)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGitWorktreePaths(unittest.TestCase):
    def test_parses_absolute_paths(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=_make_git_output(
                    "/home/eric/projects/foo",
                    "/home/eric/projects/foo/.claude/worktrees/agent-abc123",
                ),
            )
            paths = _git_worktree_paths("/home/eric/projects/foo")
        self.assertIn("/home/eric/projects/foo", paths)
        self.assertIn("/home/eric/projects/foo/.claude/worktrees/agent-abc123", paths)

    def test_returns_empty_on_git_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            paths = _git_worktree_paths("/no/such/repo")
        self.assertEqual(paths, set())

    def test_returns_empty_on_exception(self):
        with patch("subprocess.run", side_effect=OSError("no git")):
            paths = _git_worktree_paths("/fake/path")
        self.assertEqual(paths, set())


class TestWorktreeLeakDetectorPositive(unittest.TestCase):
    """A terminal-outcome run whose dir exists and git knows about → fires."""

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        # Simulate a worktree directory: create it on disk
        self.wt_path = os.path.join(self.tmpdir, ".claude", "worktrees", "agent-deadbeef")
        os.makedirs(self.wt_path)
        _insert_run(
            self.conn, "run-001", "myproject",
            worktree=self.wt_path,
            worktree_slug="agent-deadbeef",
            git_branch="feat/some-work",
            bead_id="myproject-abc",
            outcome="clean",
        )

    def _make_detector_with_git(self, git_paths):
        """Return a WorktreeLeakDetector whose _git_worktree_paths is mocked."""
        det = WorktreeLeakDetector(self.conn)
        return det, git_paths

    def test_fires_when_dir_exists_and_git_knows(self):
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()

        self.assertEqual(len(candidates), 1, "Expected exactly one leaked worktree")
        c = candidates[0]
        self.assertEqual(c.project, "myproject")
        self.assertEqual(c.pattern_name, "worktree_leak")
        self.assertIn(self.wt_path, c.evidence[0])

    def test_remediation_references_path(self):
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()

        self.assertIsNotNone(candidates[0].proposed_remediation)
        self.assertIn(self.wt_path, candidates[0].proposed_remediation)
        self.assertIn("git worktree remove", candidates[0].proposed_remediation)

    def test_signature_does_not_contain_run_id(self):
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()

        sig = candidates[0].signature
        self.assertNotIn("run-001", sig)
        self.assertIn("myproject", sig)
        self.assertIn("worktree_leak", sig)
        self.assertIn(self.wt_path, sig)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestWorktreeLeakDetectorNegativeMissingDir(unittest.TestCase):
    """Dir does not exist on disk → no fire."""

    def test_no_fire_when_dir_missing(self):
        conn = _make_db()
        _insert_run(
            conn, "run-002", "proj",
            worktree="/nonexistent/path/agent-abc",
            outcome="clean",
        )
        det = WorktreeLeakDetector(conn)
        # _infer_repo_root / _git_worktree_paths should NOT be called because
        # os.path.isdir fails first.
        with patch("detectors.worktree_leak._infer_repo_root") as mock_infer:
            candidates = det.run()
        mock_infer.assert_not_called()
        self.assertEqual(candidates, [])


class TestWorktreeLeakDetectorNegativeNotInGit(unittest.TestCase):
    """Dir exists but NOT in git worktree list → orphan, not a leak, no fire."""

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        self.wt_path = os.path.join(self.tmpdir, ".claude", "worktrees", "agent-orphan")
        os.makedirs(self.wt_path)
        _insert_run(
            self.conn, "run-003", "proj",
            worktree=self.wt_path,
            outcome="clean",
        )

    def test_no_fire_when_not_in_git(self):
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.tmpdir},  # only repo root, NOT the worktree path
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()
        self.assertEqual(candidates, [])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestWorktreeLeakDetectorNegativeNoRepoRoot(unittest.TestCase):
    """Cannot infer repo root → skip (no false positive)."""

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        self.wt_path = os.path.join(self.tmpdir, "worktree")
        os.makedirs(self.wt_path)
        _insert_run(self.conn, "run-004", "proj", worktree=self.wt_path, outcome="clean")

    def test_no_fire_when_repo_root_not_found(self):
        det = WorktreeLeakDetector(self.conn)
        with patch("detectors.worktree_leak._infer_repo_root", return_value=None):
            candidates = det.run()
        self.assertEqual(candidates, [])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestSlugVsAbsoluteJoin(unittest.TestCase):
    """CRITICAL: a bare slug must NOT match git worktree list output (b1a audit).

    If we compare slug "agent-abc123" against the set of absolute paths from
    `git worktree list`, it will never match because the set contains strings
    like "/home/eric/projects/foo/.claude/worktrees/agent-abc123".
    This test explicitly verifies that the slug alone does not produce a match.
    """

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        self.slug = "agent-abc123"
        # The ABSOLUTE path (what git emits and what runs.worktree stores)
        self.abs_path = os.path.join(self.tmpdir, ".claude", "worktrees", self.slug)
        os.makedirs(self.abs_path)
        _insert_run(
            self.conn, "run-005", "proj",
            worktree=self.abs_path,
            worktree_slug=self.slug,
            outcome="clean",
        )

    def test_slug_alone_does_not_match(self):
        """Demonstrate that comparing slug against absolute-path set returns no match."""
        abs_paths = {self.abs_path, self.tmpdir}
        # The slug is NOT in the set of absolute paths — this is the b1a bug.
        self.assertNotIn(self.slug, abs_paths)
        # But the absolute path IS in the set — this is correct.
        self.assertIn(self.abs_path, abs_paths)

    def test_detector_uses_absolute_path(self):
        """The detector must use runs.worktree (absolute) not worktree_slug."""
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.abs_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()
        # Should fire — the absolute path matched
        self.assertEqual(len(candidates), 1)

    def test_detector_with_slug_only_in_git_output_does_not_fire(self):
        """If somehow git output only contained the slug (impossible in practice),
        the detector would correctly NOT fire — slug != absolute path."""
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            # Intentionally put only the slug (bad data) in the git paths set.
            return_value={self.slug, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()
        # The absolute path is NOT in {slug, tmpdir} → not a leak in this case
        # (this is the orphan path — dir exists, not in git) → no fire.
        self.assertEqual(candidates, [])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestPrevalenceAndRecurrenceIntegration(unittest.TestCase):
    """Prevalence + recurrence work end-to-end with WorktreeLeakDetector."""

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        self.wt_path = os.path.join(self.tmpdir, ".claude", "worktrees", "agent-prv")
        os.makedirs(self.wt_path)

    def _run_detector(self):
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            return det.run(), det

    def test_recurrence_stable_across_repeated_scans(self):
        """CONTRACT (2026-06): recurrence_count is INVARIANT across repeated passes
        over the SAME data.  It only grows when a genuinely NEW run_id fires.

        Two passes over one run → recurrence_count stays at 1.
        Adding a second distinct run and re-scanning → recurrence_count becomes 2.
        """
        _insert_run(self.conn, "run-p1", "proj", worktree=self.wt_path, outcome="clean")

        # Pass 1
        _, det1 = self._run_detector()
        issue1 = det1.get_issue(f"proj:worktree_leak:{self.wt_path}")
        self.assertEqual(issue1["recurrence_count"], 1, "first scan must create issue with count 1")

        # Pass 2 — same data, no new run_id → recurrence_count must NOT change
        _, det2 = self._run_detector()
        issue2 = det2.get_issue(f"proj:worktree_leak:{self.wt_path}")
        self.assertEqual(
            issue2["recurrence_count"], 1,
            "second pass over identical data must NOT inflate recurrence_count",
        )

        # Now add a genuinely new run and re-scan → count must grow
        _insert_run(self.conn, "run-p2", "proj", worktree=self.wt_path, outcome="clean")
        _, det3 = self._run_detector()
        issue3 = det3.get_issue(f"proj:worktree_leak:{self.wt_path}")
        self.assertEqual(
            issue3["recurrence_count"], 2,
            "a new distinct run_id firing the same signature must increment recurrence_count",
        )

    def test_prevalence_rate(self):
        # 4 runs for this project; 2 of them triggered the worktree_leak detector
        for i in range(4):
            _insert_run(self.conn, f"run-prv{i}", "proj2")
        det = WorktreeLeakDetector(self.conn)
        det.record_hit("run-prv0", f"proj2:worktree_leak:some-path", "proj2")
        det.record_hit("run-prv1", f"proj2:worktree_leak:some-path", "proj2")
        rate = det.prevalence("proj2", window_days=7)
        self.assertAlmostEqual(rate, 0.5)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestAbandonedOutcomeAlsoDetected(unittest.TestCase):
    """'abandoned' is NOT a terminal-merge outcome; it must NOT fire.

    CONTRACT CHANGE (2026-06): the detector fires only on
    outcome IN ('clean','landed','corrected') AND labeled_at IS NOT NULL.
    'abandoned' means the work was dropped without merging, not that the
    worktree was cleaned up by a successful landing — so it is no longer
    treated as a leak indicator.

    'landed' and 'corrected' ARE terminal-merge outcomes and MUST fire.
    """

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        self.wt_path = os.path.join(self.tmpdir, ".claude", "worktrees", "agent-aband")
        os.makedirs(self.wt_path)

    def test_abandoned_does_NOT_fire(self):
        """'abandoned' outcome must not trigger the worktree-leak detector."""
        _insert_run(
            self.conn, "run-ab1", "proj",
            worktree=self.wt_path,
            outcome="abandoned",
        )
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()
        self.assertEqual(
            candidates, [],
            "'abandoned' must NOT fire — it is not a confirmed-merge outcome",
        )

    def test_landed_fires(self):
        """'landed' (confirmed merged) must fire when labeled_at is set."""
        _insert_run(
            self.conn, "run-land1", "proj",
            worktree=self.wt_path,
            outcome="landed",
        )
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()
        self.assertEqual(len(candidates), 1, "'landed' must fire")

    def test_corrected_fires(self):
        """'corrected' (confirmed merged after correction) must fire when labeled_at is set."""
        _insert_run(
            self.conn, "run-corr1", "proj",
            worktree=self.wt_path,
            outcome="corrected",
        )
        det = WorktreeLeakDetector(self.conn)
        with patch(
            "detectors.worktree_leak._git_worktree_paths",
            return_value={self.wt_path, self.tmpdir},
        ), patch(
            "detectors.worktree_leak._infer_repo_root",
            return_value=self.tmpdir,
        ):
            candidates = det.run()
        self.assertEqual(len(candidates), 1, "'corrected' must fire")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestNonTerminalOutcomeNotDetected(unittest.TestCase):
    """Runs with in-progress, NULL outcome, or NULL labeled_at must not fire.

    CONTRACT (2026-06): fires only on outcome IN ('clean','landed','corrected')
    AND labeled_at IS NOT NULL.  Any run that fails either condition is excluded.
    """

    def setUp(self):
        self.conn = _make_db()
        self.tmpdir = tempfile.mkdtemp()
        self.wt_path = os.path.join(self.tmpdir, ".claude", "worktrees", "agent-ip")
        os.makedirs(self.wt_path)

    def test_no_fire_on_null_outcome(self):
        """outcome=NULL (even with labeled_at set) must not fire."""
        _insert_run(
            self.conn, "run-ip1", "proj",
            worktree=self.wt_path, outcome=None,
            labeled_at=_now_utc(),
        )
        det = WorktreeLeakDetector(self.conn)
        with patch("detectors.worktree_leak._git_worktree_paths", return_value={self.wt_path}), \
             patch("detectors.worktree_leak._infer_repo_root", return_value=self.tmpdir):
            candidates = det.run()
        self.assertEqual(candidates, [])

    def test_no_fire_on_in_progress_outcome(self):
        """outcome='in_progress' (not in terminal set) must not fire."""
        _insert_run(
            self.conn, "run-ip2", "proj",
            worktree=self.wt_path, outcome="in_progress",
            labeled_at=_now_utc(),
        )
        det = WorktreeLeakDetector(self.conn)
        with patch("detectors.worktree_leak._git_worktree_paths", return_value={self.wt_path}), \
             patch("detectors.worktree_leak._infer_repo_root", return_value=self.tmpdir):
            candidates = det.run()
        self.assertEqual(candidates, [])

    def test_no_fire_on_null_labeled_at(self):
        """outcome='clean' but labeled_at IS NULL must not fire (null-vs-clean discipline)."""
        _insert_run(
            self.conn, "run-ip3", "proj",
            worktree=self.wt_path, outcome="clean",
            labeled_at=None,
        )
        det = WorktreeLeakDetector(self.conn)
        with patch("detectors.worktree_leak._git_worktree_paths", return_value={self.wt_path}), \
             patch("detectors.worktree_leak._infer_repo_root", return_value=self.tmpdir):
            candidates = det.run()
        self.assertEqual(candidates, [], "labeled_at=NULL must exclude the run even if outcome='clean'")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
