#!/usr/bin/env python3
"""Tests for ci-watch. All network I/O (gh run list, gh repo view, gh run view
--log-failed, `nervous publish`, `bd create`/`bd show`) is stubbed via fixture
data / monkeypatched functions -- these tests never touch the network or a
real bd database, per house convention (adapters/staleness/test_monitor.py
uses a dedicated Redis namespace instead since its substrate IS the network;
ci-watch's substrate is entirely external APIs, so full stubbing is the
correct analogue here).

Run: python3 test_watch.py -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import watch


FIXTURE_ROSTER = {
    "default_ignore_workflows": ["readme downloads badge", "dependency graph"],
    "repos": [
        {"repo": "acme/widgets"},
        {"repo": "acme/gizmos", "ignore_workflows": ["nightly build"]},
    ],
}


def make_run(workflow, conclusion, branch="main", sha="abc123", updated_at="2026-08-30T10:00:00Z", url="https://x/1", run_id=1):
    return {
        "workflowName": workflow, "conclusion": conclusion, "headBranch": branch,
        "headSha": sha, "updatedAt": updated_at, "url": url, "databaseId": run_id,
    }


class TestClassifyRun(unittest.TestCase):
    def test_success_is_green(self):
        self.assertEqual(watch.classify_run("success"), watch.GREEN)

    def test_empty_is_pending(self):
        self.assertEqual(watch.classify_run(""), watch.PENDING)
        self.assertEqual(watch.classify_run(None), watch.PENDING)

    def test_skipped(self):
        self.assertEqual(watch.classify_run("skipped"), watch.SKIPPED)

    def test_failure_variants_are_red(self):
        for c in ("failure", "cancelled", "timed_out", "action_required", "neutral", "stale"):
            self.assertEqual(watch.classify_run(c), watch.RED, c)


class TestIgnore(unittest.TestCase):
    def test_global_ignore_case_insensitive(self):
        self.assertTrue(watch.is_ignored("README Downloads Badge", ["readme downloads badge"], []))

    def test_repo_ignore(self):
        self.assertTrue(watch.is_ignored("Nightly Build", [], ["nightly build"]))

    def test_not_ignored(self):
        self.assertFalse(watch.is_ignored("Test battery", ["readme downloads badge"], []))


class TestGroupAndComputeState(unittest.TestCase):
    def test_group_filters_by_branch(self):
        runs = [make_run("CI", "success", branch="main"), make_run("CI", "success", branch="feature")]
        groups = watch.group_runs(runs, ["main"])
        self.assertEqual(len(groups), 1)
        self.assertIn(("CI", "main"), groups)

    def test_consecutive_failures_counts_leading_red_only(self):
        # newest-first after sort: red, red, green, red -- streak stops at green
        runs = [
            make_run("CI", "failure", updated_at="2026-08-30T12:00:00Z"),
            make_run("CI", "failure", updated_at="2026-08-30T11:00:00Z"),
            make_run("CI", "success", updated_at="2026-08-30T10:00:00Z"),
            make_run("CI", "failure", updated_at="2026-08-30T09:00:00Z"),
        ]
        groups = watch.group_runs(runs, ["main"])
        computed = watch.compute_workflow_state(groups[("CI", "main")], {})
        self.assertEqual(computed["state"], watch.RED)
        self.assertEqual(computed["consecutive_failures"], 2)
        self.assertEqual(computed["red_since"], "2026-08-30T11:00:00Z")

    def test_red_since_preserved_when_window_fully_red(self):
        runs = [make_run("CI", "failure", updated_at=f"2026-08-30T{h:02d}:00:00Z") for h in range(10, 15)]
        groups = watch.group_runs(runs, ["main"])
        prior = {"red_since": "2026-08-29T00:00:00Z"}
        computed = watch.compute_workflow_state(groups[("CI", "main")], prior)
        # entire sampled window is red -> preserve the earlier persisted red_since
        self.assertEqual(computed["red_since"], "2026-08-29T00:00:00Z")

    def test_no_runs(self):
        computed = watch.compute_workflow_state([], {})
        self.assertEqual(computed["state"], watch.NO_RUNS)


class TestPipelinePass(unittest.TestCase):
    def setUp(self):
        self.published = []
        self.beads_created = []
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_pass(self, runs_by_repo, state, dry_run=False):
        def fake_fetch(repo, limit):
            return runs_by_repo.get(repo, [])

        def fake_default_branch(repo):
            return "main"

        def fake_log_fetch(repo, run_id):
            return "line1\nAuthorization: Bearer xyz\nline3"

        orig_publish = watch.publish_event
        orig_bead = watch.file_triage_bead
        watch.publish_event = lambda payload, dry_run=False: self.published.append(payload) or True
        watch.file_triage_bead = lambda repo, wf, url, sha, log, dry_run=False: self.beads_created.append((repo, wf)) or "bd-999"
        try:
            return watch.run_pipeline_pass(
                FIXTURE_ROSTER, state, default_branch_fn=fake_default_branch,
                run_fetch_fn=fake_fetch, log_fetch_fn=fake_log_fetch, dry_run=dry_run,
            )
        finally:
            watch.publish_event = orig_publish
            watch.file_triage_bead = orig_bead

    def test_first_seen_red_emits_and_files_bead(self):
        runs = {"acme/widgets": [
            make_run("CI", "failure", updated_at="2026-08-30T12:00:00Z"),
            make_run("CI", "failure", updated_at="2026-08-30T11:00:00Z"),
        ]}
        state = {}
        results = self._run_pass(runs, state)
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["state"], watch.RED)
        self.assertEqual(self.published[0]["prev_state"], "unknown")
        self.assertEqual(self.published[0]["reason"], "transition")
        self.assertEqual(self.beads_created, [("acme/widgets", "CI")])

    def test_green_to_green_no_event(self):
        runs = {"acme/widgets": [make_run("CI", "success")]}
        state = {watch.state_key("acme/widgets", "CI", "main"): {"state": watch.GREEN}}
        self._run_pass(runs, state)
        self.assertEqual(self.published, [])
        self.assertEqual(self.beads_created, [])

    def test_red_to_green_emits_transition_and_clears_bead(self):
        runs = {"acme/widgets": [make_run("CI", "success")]}
        state = {watch.state_key("acme/widgets", "CI", "main"): {"state": watch.RED, "bead_id": "bd-1"}}

        import watch as w
        orig_is_open = w.bead_is_open
        w.bead_is_open = lambda bid: True
        try:
            self._run_pass(runs, state)
        finally:
            w.bead_is_open = orig_is_open

        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["state"], watch.GREEN)
        self.assertEqual(self.published[0]["prev_state"], watch.RED)
        self.assertIsNone(self.published[0]["bead_id"])

    def test_persistent_red_no_reminder_before_cooldown(self):
        runs = {"acme/widgets": [make_run("CI", "failure")]}
        state = {watch.state_key("acme/widgets", "CI", "main"): {
            "state": watch.RED, "last_notified_at": watch.iso(time.time() - 60), "bead_id": "bd-1",
        }}
        import watch as w
        w.bead_is_open = lambda bid: True
        self._run_pass(runs, state)
        self.assertEqual(self.published, [])

    def test_persistent_red_reminder_after_cooldown(self):
        runs = {"acme/widgets": [make_run("CI", "failure")]}
        state = {watch.state_key("acme/widgets", "CI", "main"): {
            "state": watch.RED, "last_notified_at": watch.iso(time.time() - 25 * 3600), "bead_id": "bd-1",
        }}
        import watch as w
        w.bead_is_open = lambda bid: True
        self._run_pass(runs, state)
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["reason"], "persistent-red-reminder")

    def test_pending_never_emits_or_overwrites_state(self):
        runs = {"acme/widgets": [make_run("CI", "")]}
        state = {watch.state_key("acme/widgets", "CI", "main"): {"state": watch.GREEN}}
        results = self._run_pass(runs, state)
        self.assertEqual(self.published, [])
        self.assertEqual(state[watch.state_key("acme/widgets", "CI", "main")]["state"], watch.GREEN)

    def test_ignored_workflow_never_appears(self):
        runs = {"acme/gizmos": [make_run("Nightly Build", "failure")]}
        state = {}
        results = self._run_pass(runs, state)
        self.assertEqual(results, [])
        self.assertEqual(self.published, [])

    def test_bead_dedup_no_refile_while_open(self):
        runs = {"acme/widgets": [
            make_run("CI", "failure", updated_at="2026-08-30T12:00:00Z"),
            make_run("CI", "failure", updated_at="2026-08-30T11:00:00Z"),
        ]}
        state = {watch.state_key("acme/widgets", "CI", "main"): {
            "state": watch.RED, "bead_id": "bd-1", "consecutive_failures": 1,
            "last_notified_at": watch.iso(time.time()),
        }}
        import watch as w
        w.bead_is_open = lambda bid: True
        self._run_pass(runs, state)
        self.assertEqual(self.beads_created, [])  # already open, no refile

    def test_secret_redaction_in_log_excerpt(self):
        redacted = watch.redact("normal line\nAuthorization: Bearer abc\npassword=hunter2\nfine")
        self.assertNotIn("Bearer", redacted)
        self.assertNotIn("hunter2", redacted)
        self.assertIn("normal line", redacted)
        self.assertIn("fine", redacted)


class TestReport(unittest.TestCase):
    def test_render_report_has_both_tables(self):
        pipeline = [{"repo": "acme/widgets", "workflow": "CI", "branch": "main", "state": "red",
                     "consecutive_failures": 3, "red_since": "2026-08-30T00:00:00Z", "run_url": "https://x", "bead_id": "bd-1"}]
        lint = [{"repo": "acme/widgets", "has_workflows_dir": True, "tests_run_on_main": True,
                 "has_claude_or_agents_md": True, "contract_files": ["CLAUDE.md"], "tests_run_notes": "ok"}]
        report = watch.render_report(pipeline, lint)
        self.assertIn("## Pipeline status", report)
        self.assertIn("## Contract lint scorecard", report)
        self.assertIn("acme/widgets", report)
        self.assertIn("bd-1", report)


class TestLint(unittest.TestCase):
    def test_lint_missing_local_checkout(self):
        result = watch.lint_repo("acme/does-not-exist", projects_root=Path("/nonexistent-root-xyz"))
        self.assertFalse(result["has_workflows_dir"])
        self.assertIn("not found", result["tests_run_notes"])

    def test_resolve_local_checkout_worktree_container(self):
        # Simulates hearth-loom's layout: /projects/<name>/ is a container of
        # sibling worktrees (main/, worktrees/, wt/), not the checkout itself.
        root = Path(tempfile.mkdtemp())
        try:
            container = root / "hearth-loom"
            (container / "main" / ".github" / "workflows").mkdir(parents=True)
            (container / "worktrees").mkdir()
            resolved = watch.resolve_local_checkout(container)
            self.assertEqual(resolved, container / "main")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_resolve_local_checkout_direct(self):
        root = Path(tempfile.mkdtemp())
        try:
            direct = root / "nervous-bus"
            (direct / ".github" / "workflows").mkdir(parents=True)
            resolved = watch.resolve_local_checkout(direct)
            self.assertEqual(resolved, direct)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_lint_real_hearth_disabled_job(self):
        # Exercises the real local checkout read-only (per FILE SCOPE: read
        # anywhere, write only inside this adapter/cache/beads). Skips
        # gracefully if the checkout isn't present in this environment.
        root = Path.home() / "projects"
        if not (root / "hearth" / ".github" / "workflows" / "ci.yml").is_file():
            self.skipTest("hearth checkout not present in this environment")
        result = watch.lint_repo("warxhead1/hearth", projects_root=root)
        self.assertTrue(result["has_workflows_dir"])
        self.assertIn("if: false", result["tests_run_notes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
