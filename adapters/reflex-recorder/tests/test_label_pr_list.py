"""tests/test_label_pr_list.py — PR lookups inside a labeling pass (#134).

A pass asks gh for each repo's PR list once and answers every branch from it.
Outside a pass, a lookup is a single `gh pr view`. A run with no worktree asks
its own repo, never label.py's cwd.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import label


class FakeGh:
    """Stands in for _run_cmd_cwd: answers `gh pr list` and `gh pr view`."""

    def __init__(self, prs: list[dict], view: dict | None = None):
        self.prs = prs
        self.view = view
        self.calls: list[tuple[str, str]] = []

    def __call__(self, cmd, cwd=None, **kw):
        self.calls.append((cmd[2], cwd))
        if cmd[2] == "list":
            return json.dumps(self.prs)
        if cmd[2] == "view":
            return json.dumps(self.view) if self.view else None
        raise AssertionError(cmd)

    def count(self, verb: str) -> int:
        return sum(1 for v, _ in self.calls if v == verb)


def in_pass(fn):
    return label._with_pass_memo(fn)()


class PrListTests(unittest.TestCase):
    def setUp(self):
        self.repo = str(Path(__file__).resolve().parent)  # any existing dir

    def test_one_list_answers_every_branch_of_a_repo(self):
        gh = FakeGh([
            {"headRefName": "a", "state": "MERGED", "mergedAt": "2026-11-01T00:00:00Z"},
            {"headRefName": "b", "state": "CLOSED", "closedAt": "2026-11-02T00:00:00Z"},
        ])
        with patch.object(label, "_run_cmd_cwd", side_effect=gh), \
                patch.object(label, "_git_has_revert", return_value=False):
            got = in_pass(lambda: [
                label.label_from_pr("a", None, self.repo),
                label.label_from_pr("b", None, self.repo),
                label.label_from_pr("c", None, self.repo),
            ])
        self.assertEqual(got, [("landed", "pr_merge"),
                               ("abandoned", "pr_closed_unmerged"),
                               None])
        self.assertEqual(gh.count("list"), 1)
        self.assertEqual(gh.count("view"), 0, "a short list proves the branch has no PR")

    def test_a_full_list_falls_back_to_view_for_a_miss(self):
        gh = FakeGh([{"headRefName": "x", "state": "OPEN"}],
                    view={"state": "MERGED", "mergedAt": "2026-11-01T00:00:00Z"})
        with patch.object(label, "_run_cmd_cwd", side_effect=gh), \
                patch.object(label, "_git_has_revert", return_value=False), \
                patch.object(label, "_PR_LIST_LIMIT", 1):
            got = in_pass(lambda: label.label_from_pr("old-branch", None, self.repo))
        self.assertEqual(got, ("landed", "pr_merge"))
        self.assertEqual(gh.count("view"), 1)

    def test_picks_the_pr_gh_pr_view_would(self):
        """gh pr view <branch> returns an open PR, else the newest one. The
        list path must agree, or one branch gets two verdicts."""
        newest_closed = [
            {"headRefName": "a", "state": "MERGED", "createdAt": "2026-11-01T00:00:00Z",
             "mergedAt": "2026-11-01T01:00:00Z"},
            {"headRefName": "a", "state": "CLOSED", "createdAt": "2026-11-05T00:00:00Z"},
        ]
        open_and_merged = [
            {"headRefName": "b", "state": "MERGED", "createdAt": "2026-11-01T00:00:00Z",
             "mergedAt": "2026-11-01T01:00:00Z"},
            {"headRefName": "b", "state": "OPEN", "createdAt": "2026-10-01T00:00:00Z"},
        ]
        gh = FakeGh(newest_closed + open_and_merged)
        with patch.object(label, "_run_cmd_cwd", side_effect=gh), \
                patch.object(label, "_git_has_revert", return_value=False):
            got = in_pass(lambda: [label.label_from_pr("a", None, self.repo),
                                   label.label_from_pr("b", None, self.repo)])
        self.assertEqual(got, [("abandoned", "pr_closed_unmerged"), None],
                         "an open PR keeps the branch in flight")

    def test_reaped_worktree_asks_the_run_repo(self):
        gh = FakeGh([], view=None)
        with patch.object(label, "_run_cmd_cwd", side_effect=gh), \
                patch.object(label, "_git_has_revert", return_value=False):
            label.label_from_pr("a", "/gone/worktree/path", self.repo)
        self.assertEqual(gh.calls, [("view", self.repo)])

    def test_no_worktree_and_no_repo_makes_no_lookup(self):
        gh = FakeGh([], view={"state": "MERGED"})
        with patch.object(label, "_run_cmd_cwd", side_effect=gh):
            self.assertIsNone(label.label_from_pr("a", None, None))
        self.assertEqual(gh.calls, [], "label.py's own cwd is another project's repo")


if __name__ == "__main__":
    unittest.main()
