"""tests/test_label_reverify.py — nervous-bus-33: re-verify inferred outcome
labels (abandoned/clean) against git ground truth.

Uses a REAL ephemeral git repo (same rationale as test_git_outcome.py: mocking
git's ancestry/patch-id machinery would test nothing real) plus a minimal
runs.db fixture wired so label.py's _project_repo_root() resolves the repo
from a run's `worktree` path (`<repo>/.claude/worktrees/<slug>` shape).

Covers:
- a branch labelled 'abandoned' by behavior_inference that was later
  squash-merged into main upgrades to 'landed' and label_history gets a NEW
  appended entry (never rewritten/removed).
- a branch that never merged stays 'abandoned' (git confirms it, but
  apply_label's own "no transition" rule means no new history entry — the
  label doesn't need to *change* to be confirmed).
- re-running reverify_labels is idempotent: no double writes, no duplicate
  history entries.
- dry_run=True (the default) writes nothing to the DB even when an explicit
  flip is available.
- false_abandon_rate() over a small seeded population is deterministic.
- select_reverify_candidates() excludes runs whose current label source is
  already explicit (e.g. pr_merge), and excludes trunk branches.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from label import (
    REVERIFY_OUTCOMES,
    _label_source,
    apply_label,
    false_abandon_rate,
    reverify_labels,
    reverify_run,
    select_reverify_candidates,
)


# ── git fixture helpers (mirrors tests/test_git_outcome.py) ────────────────────

def _run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "init", "-q", "-b", "main")
    _run(r, "config", "user.email", "t@t")
    _run(r, "config", "user.name", "t")
    (r / "base.txt").write_text("base\n")
    _run(r, "add", "-A")
    _run(r, "commit", "-qm", "base")
    return r


def _commit(repo: Path, fname: str, content: str, msg: str) -> None:
    (repo / fname).write_text(content)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", msg)


# ── runs.db fixture ─────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE runs (
    run_id          TEXT PRIMARY KEY,
    project         TEXT NOT NULL DEFAULT 'test',
    started         TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
    git_branch      TEXT,
    worktree        TEXT,
    bead_id         TEXT,
    outcome         TEXT,
    labeled_at      TEXT,
    label_version   INTEGER,
    label_history   TEXT NOT NULL DEFAULT '[]',
    features        TEXT NOT NULL DEFAULT '{}'
)
"""


def _make_db(rows: list[dict]) -> Path:
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp, isolation_level=None)
    conn.execute(_DDL)
    for r in rows:
        conn.execute(
            """
            INSERT INTO runs (run_id, project, started, git_branch, worktree,
                               outcome, labeled_at, label_version, label_history, features)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["run_id"], r.get("project", "test"),
                r.get("started", "2026-01-01T00:00:00Z"),
                r.get("git_branch"), r.get("worktree"),
                r.get("outcome"), r.get("labeled_at"), r.get("label_version"),
                json.dumps(r.get("label_history", [])),
                json.dumps(r.get("features", {})),
            ),
        )
    conn.close()
    return Path(tmp)


def _inferred_row(run_id: str, outcome: str, git_branch: str, worktree: str) -> dict:
    return {
        "run_id": run_id,
        "git_branch": git_branch,
        "worktree": worktree,
        "outcome": outcome,
        "labeled_at": "2026-01-01T00:00:00Z",
        "label_version": 1,
        "label_history": [{
            "outcome": outcome, "labeled_at": "2026-01-01T00:00:00Z",
            "label_version": 1, "source": "behavior_inference",
        }],
    }


def _read_run(db_path: Path, run_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    conn.close()
    return dict(row)


# ── select_reverify_candidates ──────────────────────────────────────────────────

class TestSelectReverifyCandidates(unittest.TestCase):
    def test_excludes_explicit_source(self):
        """A label whose CURRENT source is already explicit (e.g. pr_merge) is
        not a reverify candidate — it's not inferred, nothing to re-check."""
        rows = [
            _inferred_row("run-inferred", "abandoned", "worktree-agent-a", "/repo/.claude/worktrees/a"),
            {
                "run_id": "run-explicit", "git_branch": "worktree-agent-b",
                "worktree": "/repo/.claude/worktrees/b", "outcome": "landed",
                "labeled_at": "2026-01-01T00:00:00Z", "label_version": 1,
                "label_history": [{"outcome": "landed", "labeled_at": "2026-01-01T00:00:00Z",
                                    "label_version": 1, "source": "pr_merge"}],
            },
        ]
        db_path = _make_db(rows)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            candidates = select_reverify_candidates(conn, outcomes=REVERIFY_OUTCOMES + ("landed",))
            conn.close()
            ids = {c["run_id"] for c in candidates}
            self.assertIn("run-inferred", ids)
            self.assertNotIn("run-explicit", ids)
        finally:
            db_path.unlink(missing_ok=True)

    def test_excludes_trunk_branch(self):
        rows = [_inferred_row("run-main", "abandoned", "main", "/repo/.claude/worktrees/x")]
        db_path = _make_db(rows)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            candidates = select_reverify_candidates(conn)
            conn.close()
            self.assertEqual(candidates, [])
        finally:
            db_path.unlink(missing_ok=True)

    def test_missing_label_history_treated_as_inferred(self):
        """A pre-history-mechanism row (empty label_history) is conservatively
        eligible — its provenance can't be proven explicit, so it is not
        skipped (see label.py's _label_source docstring)."""
        row = {
            "run_id": "run-legacy", "git_branch": "worktree-agent-legacy",
            "worktree": "/repo/.claude/worktrees/legacy", "outcome": "abandoned",
            "labeled_at": "2026-01-01T00:00:00Z", "label_version": None,
            "label_history": [],
        }
        db_path = _make_db([row])
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            candidates = select_reverify_candidates(conn)
            conn.close()
            self.assertEqual(_label_source(row), "behavior_inference")
            self.assertEqual({c["run_id"] for c in candidates}, {"run-legacy"})
        finally:
            db_path.unlink(missing_ok=True)


# ── reverify_labels: real git repo ──────────────────────────────────────────────

class TestReverifyAgainstRealGit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.repo = _make_repo(self.tmp_path)
        self.worktree_path = str(self.repo) + "/.claude/worktrees/agent-x"

    def tearDown(self):
        self._tmp.cleanup()

    def _squash_land(self, branch: str) -> None:
        """Simulate a local squash-merge of `branch` into main (no PR, no
        merge commit) — the exact pattern nervous-bus-33 is about."""
        _run(self.repo, "checkout", "-q", "main")
        _run(self.repo, "merge", "-q", "--squash", branch)
        _run(self.repo, "commit", "-qm", f"squash {branch}")
        _run(self.repo, "checkout", "-q", "main")

    def test_merged_branch_upgrades_abandoned_to_landed(self):
        """AC: branch merged after labelling → abandoned upgrades to landed,
        history appended (never rewritten)."""
        _run(self.repo, "checkout", "-q", "-b", "worktree-agent-merged")
        _commit(self.repo, "feature.txt", "distinctive-feature-content-1234567890\n",
                "add feature")
        _run(self.repo, "checkout", "-q", "main")
        self._squash_land("worktree-agent-merged")

        row = _inferred_row("run-merged", "abandoned", "worktree-agent-merged", self.worktree_path)
        db_path = _make_db([row])
        try:
            results = reverify_labels(db_path, dry_run=False, verbose=False)
            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertEqual(r["old_outcome"], "abandoned")
            self.assertEqual(r["new_outcome"], "landed")
            self.assertTrue(r["would_flip"])
            self.assertTrue(r["written"])
            self.assertEqual(r["status"], "flip")

            after = _read_run(db_path, "run-merged")
            self.assertEqual(after["outcome"], "landed")
            history = json.loads(after["label_history"])
            self.assertEqual(len(history), 2, "must APPEND, not rewrite")
            self.assertEqual(history[0]["outcome"], "abandoned")
            self.assertEqual(history[0]["source"], "behavior_inference")
            self.assertEqual(history[1]["outcome"], "landed")
            self.assertNotEqual(history[1]["source"], "behavior_inference")
        finally:
            db_path.unlink(missing_ok=True)

    def test_unmerged_branch_stays_abandoned(self):
        """AC: unmerged branch stays — git confirms 'abandoned', no history
        entry is appended because the outcome did not change."""
        _run(self.repo, "checkout", "-q", "-b", "worktree-agent-neverlanded")
        _commit(self.repo, "throwaway.txt", "never-reaches-main-0987654321\n",
                "throwaway work")
        _run(self.repo, "checkout", "-q", "main")

        row = _inferred_row("run-unmerged", "abandoned", "worktree-agent-neverlanded", self.worktree_path)
        db_path = _make_db([row])
        try:
            results = reverify_labels(db_path, dry_run=False, verbose=False)
            self.assertEqual(len(results), 1)
            r = results[0]
            self.assertEqual(r["new_outcome"], "abandoned")
            self.assertFalse(r["would_flip"])
            self.assertEqual(r["status"], "confirmed")

            after = _read_run(db_path, "run-unmerged")
            self.assertEqual(after["outcome"], "abandoned")
            history = json.loads(after["label_history"])
            self.assertEqual(len(history), 1, "no transition -> no new entry")
        finally:
            db_path.unlink(missing_ok=True)

    def test_dry_run_writes_nothing(self):
        """AC: dry-run (the default) computes the flip but writes nothing."""
        _run(self.repo, "checkout", "-q", "-b", "worktree-agent-dryrun")
        _commit(self.repo, "dryrun.txt", "dry-run-distinctive-content-5566778899\n",
                "add dryrun feature")
        _run(self.repo, "checkout", "-q", "main")
        self._squash_land("worktree-agent-dryrun")

        row = _inferred_row("run-dryrun", "abandoned", "worktree-agent-dryrun", self.worktree_path)
        db_path = _make_db([row])
        try:
            results = reverify_labels(db_path, dry_run=True, verbose=False)
            r = results[0]
            self.assertTrue(r["would_flip"])
            self.assertFalse(r["written"], "dry-run must never write")

            after = _read_run(db_path, "run-dryrun")
            self.assertEqual(after["outcome"], "abandoned", "DB must be unchanged under dry-run")
            self.assertEqual(len(json.loads(after["label_history"])), 1)
        finally:
            db_path.unlink(missing_ok=True)

    def test_idempotent_rerun(self):
        """Running reverify_labels twice never double-writes or double-appends."""
        _run(self.repo, "checkout", "-q", "-b", "worktree-agent-idempotent")
        _commit(self.repo, "idem.txt", "idempotent-distinctive-content-1122334455\n",
                "add idempotent feature")
        _run(self.repo, "checkout", "-q", "main")
        self._squash_land("worktree-agent-idempotent")

        row = _inferred_row("run-idempotent", "abandoned", "worktree-agent-idempotent", self.worktree_path)
        db_path = _make_db([row])
        try:
            first = reverify_labels(db_path, dry_run=False, verbose=False)
            self.assertTrue(first[0]["written"])
            after_first = _read_run(db_path, "run-idempotent")
            history_after_first = json.loads(after_first["label_history"])
            self.assertEqual(len(history_after_first), 2)

            # Second pass: the run's current source is now explicit
            # (git_squash_equiv), so select_reverify_candidates excludes it —
            # nothing to recompute, nothing to write.
            second = reverify_labels(db_path, dry_run=False, verbose=False)
            self.assertEqual(second, [])

            after_second = _read_run(db_path, "run-idempotent")
            self.assertEqual(after_second["outcome"], "landed")
            self.assertEqual(len(json.loads(after_second["label_history"])), 2,
                             "idempotent: no duplicate history entries")
        finally:
            db_path.unlink(missing_ok=True)

    def test_no_explicit_signal_leaves_run_untouched(self):
        """A branch with a live/pending shape (still has unlanded commits,
        no PR, no merge record — git_outcome returns pending/None) is left
        exactly as-is: reverify never writes an absent verdict over a label."""
        _run(self.repo, "checkout", "-q", "-b", "worktree-agent-pending")
        _commit(self.repo, "pending.txt", "pending-work-still-in-flight-998877\n",
                "wip")
        _run(self.repo, "checkout", "-q", "main")
        # No merge, and simulate a LIVE worktree by adding a real git worktree
        # for this branch so git_outcome sees worktree_live=True → pending.
        live_wt_dir = self.tmp_path / "live-wt"
        _run(self.repo, "worktree", "add", "-q", str(live_wt_dir), "worktree-agent-pending")

        row = _inferred_row("run-pending", "abandoned", "worktree-agent-pending", self.worktree_path)
        db_path = _make_db([row])
        try:
            results = reverify_labels(db_path, dry_run=False, verbose=False)
            r = results[0]
            self.assertIsNone(r["new_outcome"])
            self.assertEqual(r["status"], "no_explicit_signal")
            self.assertFalse(r["written"])

            after = _read_run(db_path, "run-pending")
            self.assertEqual(after["outcome"], "abandoned")
            self.assertEqual(len(json.loads(after["label_history"])), 1)
        finally:
            _run(self.repo, "worktree", "remove", "--force", str(live_wt_dir))


# ── false_abandon_rate ──────────────────────────────────────────────────────────

class TestFalseAbandonRate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.repo = _make_repo(self.tmp_path)
        self.worktree_path = str(self.repo) + "/.claude/worktrees/agent-x"

    def tearDown(self):
        self._tmp.cleanup()

    def _squash_land(self, branch: str) -> None:
        _run(self.repo, "checkout", "-q", "main")
        _run(self.repo, "merge", "-q", "--squash", branch)
        _run(self.repo, "commit", "-qm", f"squash {branch}")
        _run(self.repo, "checkout", "-q", "main")

    def test_deterministic_and_counts_flips(self):
        rows = []
        # 2 falsely-abandoned (later merged) + 3 genuinely abandoned (never merged)
        for i in range(2):
            branch = f"worktree-agent-falseneg{i}"
            _run(self.repo, "checkout", "-q", "-b", branch)
            _commit(self.repo, f"f{i}.txt", f"distinctive-false-negative-content-{i}-0011223344\n",
                    f"work {i}")
            _run(self.repo, "checkout", "-q", "main")
            self._squash_land(branch)
            rows.append(_inferred_row(f"run-false-{i}", "abandoned", branch, self.worktree_path))

        for i in range(3):
            branch = f"worktree-agent-genuine{i}"
            _run(self.repo, "checkout", "-q", "-b", branch)
            _commit(self.repo, f"g{i}.txt", f"genuinely-discarded-content-{i}-5566778899\n",
                    f"discarded work {i}")
            _run(self.repo, "checkout", "-q", "main")
            rows.append(_inferred_row(f"run-genuine-{i}", "abandoned", branch, self.worktree_path))

        db_path = _make_db(rows)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            report1 = false_abandon_rate(conn, n=100, seed=7)
            report2 = false_abandon_rate(conn, n=100, seed=7)
            conn.close()

            self.assertEqual(report1, report2, "same (population, n, seed) must reproduce")
            self.assertEqual(report1["n_population"], 5)
            self.assertEqual(report1["n_sampled"], 5)
            self.assertEqual(report1["n_false"], 2)
            self.assertEqual(report1["n_confirmed"], 3)
            self.assertEqual(report1["rate"], 0.4)

            # Read-only connection: DB must be untouched by the measurement.
            after_conn = sqlite3.connect(str(db_path))
            outcomes = [row[0] for row in after_conn.execute("SELECT outcome FROM runs")]
            after_conn.close()
            self.assertTrue(all(o == "abandoned" for o in outcomes),
                            "false_abandon_rate must never write")
        finally:
            db_path.unlink(missing_ok=True)

    def test_sampling_respects_n(self):
        rows = [
            _inferred_row(f"run-{i}", "abandoned", "worktree-agent-nobranch", self.worktree_path)
            for i in range(10)
        ]
        db_path = _make_db(rows)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            report = false_abandon_rate(conn, n=4, seed=1)
            conn.close()
            self.assertEqual(report["n_population"], 10)
            self.assertEqual(report["n_sampled"], 4)
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
