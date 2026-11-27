#!/usr/bin/env python3
"""Tests for codegraph-sync. The debounce and pass logic run against injected
fingerprint/sync/publish functions; one test builds a real git repo to prove
the fingerprint moves on a second edit to an already-dirty file.

Run: python3 -m pytest -q test_sync.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import sync

FP_A = {"head": "a" * 40, "tree": "1"}
FP_B = {"head": "a" * 40, "tree": "2"}


class TestDecide(unittest.TestCase):
    def test_first_sight_waits_one_tick_then_syncs(self):
        st = {}
        self.assertIsNone(sync.decide(st, FP_A, 0))
        self.assertEqual(sync.decide(st, FP_A, 300), "quiescent")

    def test_synced_fingerprint_is_a_no_op(self):
        st = {"synced": FP_A}
        self.assertIsNone(sync.decide(st, FP_A, 0))
        self.assertNotIn("pending", st)

    def test_moving_tree_defers_until_max_pending(self):
        st = {"synced": FP_A}
        self.assertIsNone(sync.decide(st, FP_B, 0))
        for i, t in enumerate(range(300, sync.MAX_PENDING_S, 300)):
            self.assertIsNone(sync.decide(st, {"head": "a" * 40, "tree": f"x{i}"}, t))
        self.assertEqual(sync.decide(st, {"head": "b" * 40, "tree": "y"}, sync.MAX_PENDING_S), "max_pending")


class TestRunPass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.indexed = root / "indexed"
        (self.indexed / ".codegraph").mkdir(parents=True)
        (self.indexed / ".codegraph" / "codegraph.db").write_bytes(b"")
        self.bare = root / "bare"
        self.bare.mkdir()
        self.repos = [{"name": "indexed", "path": str(self.indexed)},
                      {"name": "bare", "path": str(self.bare)}]
        sync.CACHE_DIR = root / "cache"
        sync.STATE_FILE = sync.CACHE_DIR / "state.json"
        self.synced, self.published = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def _pass(self, now, fp=FP_A, ok=True, state=None, dry_run=False):
        def fake_sync(repo):
            self.synced.append(repo)
            return ok, 42
        return sync.run_pass(dry_run=dry_run, now=now, repos=self.repos,
                             fingerprint_fn=lambda r: fp, sync_fn=fake_sync,
                             publish_fn=self.published.append, state=state)

    def test_only_indexed_repo_is_synced_after_debounce(self):
        state, _ = self._pass(0)
        self.assertEqual(self.synced, [])
        state, events = self._pass(300, state=state)
        self.assertEqual(self.synced, [self.indexed])
        self.assertEqual(events[0]["repo"], "indexed")
        self.assertEqual(events[0]["trigger"], "quiescent")
        self.assertEqual(self.published, events)
        self._pass(600, state=state)
        self.assertEqual(len(self.synced), 1, "unchanged tree must not resync")

    def test_failed_sync_retries_next_tick(self):
        state, _ = self._pass(0)
        state, events = self._pass(300, ok=False, state=state)
        self.assertFalse(events[0]["ok"])
        self._pass(600, state=state)
        self.assertEqual(len(self.synced), 2)

    def test_dry_run_never_syncs_or_publishes(self):
        state, _ = self._pass(0, dry_run=True)
        self._pass(300, state=state, dry_run=True)
        self.assertEqual((self.synced, self.published), ([], []))

    def test_state_persists_between_runs(self):
        self._pass(0)
        self._pass(300)  # state=None: reloads from STATE_FILE
        self.assertEqual(len(self.synced), 1)


class TestFingerprint(unittest.TestCase):
    def test_second_edit_to_dirty_file_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
            run = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, env=env)
            run("init", "-q", "-b", "main")
            (repo / "a.py").write_text("x = 1\n")
            run("add", "a.py")
            run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
            clean = sync.fingerprint(repo)
            (repo / "a.py").write_text("x = 2\n")
            dirty1 = sync.fingerprint(repo)
            time.sleep(0.01)
            (repo / "a.py").write_text("x = 3\n")
            os.utime(repo / "a.py", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
            dirty2 = sync.fingerprint(repo)
            self.assertNotEqual(clean, dirty1)
            self.assertNotEqual(dirty1, dirty2)
            self.assertEqual(clean["head"], dirty2["head"])

    def test_churn_inside_untracked_dir_does_not_change_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"}
            run = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, env=env)
            run("init", "-q", "-b", "main")
            (repo / "a.py").write_text("x = 1\n")
            run("add", "a.py")
            run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
            (repo / ".beads").mkdir()
            (repo / ".beads" / "db").write_text("1")
            before = sync.fingerprint(repo)
            (repo / ".beads" / "db.tmp").write_text("2")
            os.replace(repo / ".beads" / "db.tmp", repo / ".beads" / "db")
            os.utime(repo / ".beads", ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
            self.assertEqual(before, sync.fingerprint(repo))

    def test_not_a_repo_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sync.fingerprint(Path(d)))


class TestLoadRepos(unittest.TestCase):
    def test_reads_codegraph_block_and_ignores_rest(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.yaml"
            cfg.write_text("vault: {path: /v, projects: []}\ncodegraph:\n  repos:\n"
                           "    - name: kb\n      path: /p/kb\n    - name: broken\n")
            self.assertEqual(sync.load_repos(cfg), [{"name": "kb", "path": "/p/kb"}])

    def test_missing_config_is_empty(self):
        self.assertEqual(sync.load_repos(Path("/nonexistent/config.yaml")), [])


class UnitIndependence(unittest.TestCase):
    """The timer must not run code from the shared checkout."""

    def test_service_runs_from_its_own_clone(self):
        unit = (Path(__file__).parent / "systemd" / "codegraph-sync.service").read_text()
        execs = [ln for ln in unit.splitlines() if ln.startswith("ExecStart=")]
        self.assertEqual(len(execs), 1)
        self.assertNotIn("projects/nervous-bus", unit)
        self.assertIn("${SRC}/adapters/codegraph-sync/sync.py", execs[0])


if __name__ == "__main__":
    unittest.main()
