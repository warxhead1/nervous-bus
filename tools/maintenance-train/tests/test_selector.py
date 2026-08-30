#!/usr/bin/env python3
"""Unit tests for selector.py — stubbed dolt/gh, no network. Run:
    python3 -m pytest tools/maintenance-train/tests/test_selector.py -v
or  python3 tools/maintenance-train/tests/test_selector.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import selector  # noqa: E402


REPOS_CFG = {
    "repo-a": {"path": "/tmp/repo-a", "gh_repo": "warxhead1/repo-a"},
    "repo-b": {"path": "/tmp/repo-b", "gh_repo": "warxhead1/repo-b"},
    "repo-c": {"path": "/tmp/repo-c", "gh_repo": "warxhead1/repo-c"},
}


def row(id_, project, title, priority=2, issue_type="bug", labels=None,
        estimated_minutes=None, blocked=False, created_at="2026-08-01T00:00:00"):
    return {
        "id": id_, "project": project, "title": title, "status": "open",
        "priority": priority, "issue_type": issue_type,
        "created_at": created_at, "notes": "",
        "labels": labels or [], "estimated_minutes": estimated_minutes,
        "blocked": blocked,
    }


class ClassifyTests(unittest.TestCase):
    def test_ci_red(self):
        r = row("x-1", "repo-a", "CI red: warxhead1/repo-a/Unit Tests")
        self.assertEqual(selector.classify(r), "ci-watch")

    def test_dependabot_title(self):
        r = row("x-2", "repo-a", "Bump dependabot foo from 1 to 2")
        self.assertEqual(selector.classify(r), "dependabot")

    def test_labelled_chore(self):
        r = row("x-3", "repo-a", "clean up thing", labels=["chore"])
        self.assertEqual(selector.classify(r), "labelled")

    def test_labelled_maintenance(self):
        r = row("x-4", "repo-a", "clean up thing", labels=["maintenance"])
        self.assertEqual(selector.classify(r), "labelled")

    def test_estimated_chore_within_cap(self):
        r = row("x-5", "repo-a", "small fix", issue_type="chore", estimated_minutes=45)
        self.assertEqual(selector.classify(r), "estimated-chore")

    def test_estimated_chore_over_cap_excluded(self):
        r = row("x-6", "repo-a", "big fix", issue_type="chore", estimated_minutes=90)
        self.assertIsNone(selector.classify(r))

    def test_unrelated_bead_excluded(self):
        r = row("x-7", "repo-a", "implement new feature")
        self.assertIsNone(selector.classify(r))


class ResolveRepoTests(unittest.TestCase):
    def test_ci_red_extracts_repo_from_title(self):
        self.assertEqual(
            selector.resolve_repo("nervous-bus", "CI red: warxhead1/tengine/cpu-extended"),
            "tengine",
        )

    def test_non_ci_red_falls_back_to_project(self):
        self.assertEqual(selector.resolve_repo("repo-b", "chore: tidy"), "repo-b")


class SelectTests(unittest.TestCase):
    def test_ranks_by_priority_then_age(self):
        rows = [
            row("p2-old", "repo-a", "CI red: warxhead1/repo-a/X", priority=2,
                created_at="2020-01-01T00:00:00"),
            row("p0-new", "repo-b", "CI red: warxhead1/repo-b/Y", priority=0,
                created_at="2026-08-29T00:00:00"),
        ]
        picked = selector.select(rows, REPOS_CFG, max_repos=3,
                                  skip_dirty_check=True, skip_pr_check=True)
        self.assertEqual([c.bead_id for c in picked], ["p0-new", "p2-old"])

    def test_one_bead_per_repo_per_run(self):
        rows = [
            row("a-1", "repo-a", "CI red: warxhead1/repo-a/X", priority=1),
            row("a-2", "repo-a", "CI red: warxhead1/repo-a/Y", priority=1),
        ]
        picked = selector.select(rows, REPOS_CFG, max_repos=3,
                                  skip_dirty_check=True, skip_pr_check=True)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].bead_id, "a-1")

    def test_cap_n_repos_per_night(self):
        rows = [
            row("a-1", "repo-a", "CI red: warxhead1/repo-a/X"),
            row("b-1", "repo-b", "CI red: warxhead1/repo-b/X"),
            row("c-1", "repo-c", "CI red: warxhead1/repo-c/X"),
        ]
        picked = selector.select(rows, REPOS_CFG, max_repos=2,
                                  skip_dirty_check=True, skip_pr_check=True)
        self.assertEqual(len(picked), 2)

    def test_blocked_bead_excluded(self):
        rows = [row("a-1", "repo-a", "CI red: warxhead1/repo-a/X", blocked=True)]
        picked = selector.select(rows, REPOS_CFG, max_repos=3,
                                  skip_dirty_check=True, skip_pr_check=True)
        self.assertEqual(picked, [])

    def test_repo_not_in_config_excluded(self):
        rows = [row("z-1", "repo-z", "CI red: warxhead1/repo-z/X")]
        picked = selector.select(rows, REPOS_CFG, max_repos=3,
                                  skip_dirty_check=True, skip_pr_check=True)
        self.assertEqual(picked, [])

    def test_dirty_repo_excluded(self):
        rows = [
            row("a-1", "repo-a", "CI red: warxhead1/repo-a/X"),
            row("b-1", "repo-b", "CI red: warxhead1/repo-b/X"),
        ]
        orig = selector.repo_is_dirty
        selector.repo_is_dirty = lambda path: path == REPOS_CFG["repo-a"]["path"]
        try:
            picked = selector.select(rows, REPOS_CFG, max_repos=3, skip_pr_check=True)
        finally:
            selector.repo_is_dirty = orig
        self.assertEqual([c.bead_id for c in picked], ["b-1"])

    def test_open_train_pr_excludes_repo(self):
        rows = [
            row("a-1", "repo-a", "CI red: warxhead1/repo-a/X"),
            row("b-1", "repo-b", "CI red: warxhead1/repo-b/X"),
        ]
        orig = selector.repo_has_open_train_pr
        selector.repo_has_open_train_pr = lambda gh_repo, gh_bin="gh": gh_repo == REPOS_CFG["repo-a"]["gh_repo"]
        try:
            picked = selector.select(rows, REPOS_CFG, max_repos=3, skip_dirty_check=True)
        finally:
            selector.repo_has_open_train_pr = orig
        self.assertEqual([c.bead_id for c in picked], ["b-1"])


class ManifestTests(unittest.TestCase):
    def test_write_manifest_roundtrip(self):
        import json
        import tempfile
        rows = [row("a-1", "repo-a", "CI red: warxhead1/repo-a/X")]
        picked = selector.select(rows, REPOS_CFG, max_repos=3,
                                  skip_dirty_check=True, skip_pr_check=True)
        with tempfile.TemporaryDirectory() as td:
            path = selector.write_manifest(picked, Path(td))
            data = json.loads(path.read_text())
            self.assertEqual(len(data["entries"]), 1)
            self.assertEqual(data["entries"][0]["bead_id"], "a-1")


if __name__ == "__main__":
    unittest.main()
