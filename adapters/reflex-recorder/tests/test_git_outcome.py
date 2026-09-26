"""Tests for git_outcome.py — git-grounded outcome attribution.

Each test builds a real ephemeral git repo in tmp_path and exercises one
classification path.  Real git is used (not mocks) because the whole point of
the module is correct interrogation of git's ancestry/patch-id machinery —
mocking it would test nothing real.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import git_outcome as go  # noqa: E402


# ── fixture helpers ─────────────────────────────────────────────────────────────

def _run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
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


def _new_branch(repo: Path, name: str, from_ref: str = "main") -> None:
    _run(repo, "branch", name, from_ref)


# ── empty branch ────────────────────────────────────────────────────────────────

def test_empty_branch_abstains(repo: Path):
    """ahead==0 never asserts 'abandoned': read-only runs never commit and
    ff-merged work also shows ahead==0."""
    _new_branch(repo, "worktree-agent-empty")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-empty", worktree_live=False)
    assert bo.outcome is None
    assert bo.source == "git_empty_branch_unconfirmed"
    assert bo.ahead == 0


def test_empty_but_live_worktree_still_abstains(repo: Path):
    """Empty branches abstain regardless of worktree liveness (zero work)."""
    _new_branch(repo, "worktree-agent-emptylive")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-emptylive", worktree_live=True)
    assert bo.outcome is None
    assert bo.source == "git_empty_branch_unconfirmed"


# ── squash / cherry landed ──────────────────────────────────────────────────────

def test_squash_equiv_landed(repo: Path):
    """Commit whose patch is cherry-picked onto main → landed/git_squash_equiv."""
    _new_branch(repo, "worktree-agent-squash")
    _run(repo, "switch", "-q", "worktree-agent-squash")
    _commit(repo, "feat.txt", "feature work\n", "feat: add feature")
    sha = _run(repo, "rev-parse", "HEAD")
    _run(repo, "switch", "-q", "main")
    # advance main so the cherry-pick lands on a DIVERGED parent → different SHA
    # but identical patch-id (the real squash-merge shape; without this the
    # cherry-picked commit would be byte-identical and collide with the branch tip)
    _commit(repo, "other.txt", "unrelated main work\n", "chore: main advances")
    _run(repo, "cherry-pick", sha)  # same patch-id, new SHA → lands on main
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-squash", worktree_live=False)
    assert bo.outcome == "landed"
    assert bo.source == "git_squash_equiv"
    assert bo.landed_commits == 1 and bo.unlanded_commits == 0


# ── merged via merge commit (the empty-vs-merged disambiguation) ─────────────────

def test_merged_into_main_not_called_empty(repo: Path):
    """Branch merged into main via a merge commit reports ahead==0 but must be
    classified landed (git_merged_into_main), NOT empty."""
    _new_branch(repo, "worktree-agent-merged")
    _run(repo, "switch", "-q", "worktree-agent-merged")
    _commit(repo, "m.txt", "merged work\n", "feat: merged work")
    _run(repo, "switch", "-q", "main")
    _run(repo, "merge", "--no-ff", "-q", "worktree-agent-merged",
         "-m", "Merge branch 'worktree-agent-merged'")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-merged", worktree_live=False)
    assert bo.outcome == "landed"
    assert bo.source == "git_merged_into_main"


# ── pending vs discarded ─────────────────────────────────────────────────────────

def test_unlanded_with_live_worktree_is_pending(repo: Path):
    """Unlanded commits + live worktree → pending (no terminal label)."""
    _new_branch(repo, "worktree-agent-pending")
    _run(repo, "switch", "-q", "worktree-agent-pending")
    _commit(repo, "wip.txt", "wip\n", "wip: in progress")
    _run(repo, "switch", "-q", "main")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-pending", worktree_live=True)
    assert bo.outcome is None
    assert bo.source == "git_pending"
    assert bo.as_label() is None


def test_unlanded_no_worktree_is_discarded_medium(repo: Path):
    """Unlanded commits + no worktree + no merge → discarded at MEDIUM confidence."""
    _new_branch(repo, "worktree-agent-discard")
    _run(repo, "switch", "-q", "worktree-agent-discard")
    _commit(repo, "lost.txt", "lost work xyzzy_unique_token_42\n", "feat: lost work")
    _run(repo, "switch", "-q", "main")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-discard", worktree_live=False)
    assert bo.outcome == "abandoned"
    assert bo.source == "git_discarded_unmerged"
    assert bo.confidence == "medium"   # never high on patch-id alone


# ── reverted ────────────────────────────────────────────────────────────────────

def test_landed_then_reverted(repo: Path):
    """Work that landed then was reverted on main → reverted."""
    _new_branch(repo, "worktree-agent-rev")
    _run(repo, "switch", "-q", "worktree-agent-rev")
    _commit(repo, "r.txt", "revert me\n", "feat: revert me")
    sha = _run(repo, "rev-parse", "HEAD")
    _run(repo, "switch", "-q", "main")
    _commit(repo, "other.txt", "unrelated main work\n", "chore: main advances")
    _run(repo, "cherry-pick", sha)
    main_sha = _run(repo, "rev-parse", "HEAD")
    _run(repo, "revert", "--no-edit", main_sha)  # creates  Revert "feat: revert me"
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-rev", worktree_live=False)
    assert bo.outcome == "reverted"
    assert bo.source == "git_revert_on_main"


# ── pickaxe deep verifier ────────────────────────────────────────────────────────

def test_verify_discarded_landed_true_when_code_reworded(repo: Path):
    """A branch whose code landed under a REWORDED commit (patch-id drift) is
    refuted as discarded by the pickaxe verifier (follows the code)."""
    uniq = "const distinctive_marker_xyz = compute_the_special_value(42);"
    _new_branch(repo, "worktree-agent-drift")
    _run(repo, "switch", "-q", "worktree-agent-drift")
    _commit(repo, "drift.txt", uniq + "\n", "feat: original message")
    _run(repo, "switch", "-q", "main")
    # land the SAME code but via a different file + reworded msg + extra context
    # → different patch-id, but pickaxe -S finds the line.
    (repo / "landed.txt").write_text("// preceding context\n" + uniq + "\n// trailing\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-qm", "feat: completely different wording")
    assert go.verify_discarded_landed(str(repo), "worktree-agent-drift") is True


def test_verify_discarded_landed_false_when_truly_gone(repo: Path):
    """Code that never reached main → pickaxe returns False (genuinely discarded)."""
    _new_branch(repo, "worktree-agent-gone")
    _run(repo, "switch", "-q", "worktree-agent-gone")
    _commit(repo, "gone.txt",
            "const truly_unique_never_landed_token = orphaned_dispatch_work();\n",
            "feat: orphaned")
    _run(repo, "switch", "-q", "main")
    assert go.verify_discarded_landed(str(repo), "worktree-agent-gone") is False


# ── live worktree enumeration ────────────────────────────────────────────────────

def test_live_worktree_branches(repo: Path, tmp_path: Path):
    """live_worktree_branches reflects attached worktrees."""
    _run(repo, "branch", "worktree-agent-wt")
    wt = tmp_path / "wt"
    _run(repo, "worktree", "add", "-q", str(wt), "worktree-agent-wt")
    live = go.live_worktree_branches(str(repo))
    assert "worktree-agent-wt" in live


# ── missing branch ───────────────────────────────────────────────────────────────

def test_nonexistent_branch(repo: Path):
    """A gone branch with no merge trace is unverifiable: abstain."""
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-nope", worktree_live=False)
    assert bo.outcome is None
    assert bo.source == "git_branch_gone"


# ── git_branch_gone must be verified, not assumed ──────────────────────────────

def test_branch_gone_but_merge_commit_recorded_is_landed(repo: Path):
    """A branch merged via a local merge commit, then DELETED, must recover as
    'landed' — not 'git_branch_gone' abandoned. This is the tachyonac/
    delete-branch-after-merge shape (GitHub 'delete branch' button)."""
    _new_branch(repo, "worktree-agent-mergedgone")
    _run(repo, "switch", "-q", "worktree-agent-mergedgone")
    _commit(repo, "m.txt", "merged then deleted\n", "feat: merged then deleted")
    _run(repo, "switch", "-q", "main")
    _run(repo, "merge", "--no-ff", "-q", "worktree-agent-mergedgone",
         "-m", "Merge branch 'worktree-agent-mergedgone'")
    _run(repo, "branch", "-D", "worktree-agent-mergedgone")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-mergedgone", worktree_live=False)
    assert bo.outcome == "landed"
    assert bo.source == "git_merged_into_main"


def test_branch_gone_but_github_pr_merge_commit_recorded_is_landed(repo: Path):
    """GitHub's 'Merge pull request #N from <owner>/<branch>' commit shape,
    branch since deleted (hearth warxhead1/bizworthy-mailbox-corrections /
    PR #158 is the measured real-world case)."""
    _new_branch(repo, "warxhead1/bizworthy-mailbox-corrections")
    _run(repo, "switch", "-q", "warxhead1/bizworthy-mailbox-corrections")
    _commit(repo, "pr.txt", "pr work\n", "fix: mailbox corrections")
    _run(repo, "switch", "-q", "main")
    _run(repo, "merge", "--no-ff", "-q", "warxhead1/bizworthy-mailbox-corrections",
         "-m", "Merge pull request #158 from warxhead1/warxhead1/bizworthy-mailbox-corrections")
    _run(repo, "branch", "-D", "warxhead1/bizworthy-mailbox-corrections")
    bo = go.classify_branch_outcome(
        str(repo), "warxhead1/bizworthy-mailbox-corrections", worktree_live=False)
    assert bo.outcome == "landed"
    assert bo.source == "git_merged_into_main"


def test_branch_gone_with_no_merge_trace_is_unverified_none(repo: Path):
    """Genuinely discarded-then-deleted work (no merge trace, no ref to diff
    or pickaxe) must abstain, NOT assert 'abandoned' — this fix must not turn
    EVERY gone branch landed either; it must turn unverified ones into a
    non-write (None), same for both a truly-discarded branch and one that was
    never dispatched at all."""
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-neverexisted", worktree_live=False)
    assert bo.outcome is None
    assert bo.source == "git_branch_gone"
    assert bo.confidence == "low"


def test_git_branch_gone_in_source_tier():
    """fix 11: git_branch_gone has an explicit tier entry (>1) for
    documentation/defense-in-depth, even though its outcome is now always
    None (as_label() never writes it) after the follow-up fix."""
    import label
    assert label._SOURCE_TIER["git_branch_gone"] > label._SOURCE_TIER["behavior_inference"]


# ── fast-forward-merge disambiguation ──────────────────────────────────────────
# A 30-day dry-run of --reverify-days measured 178 git_empty_branch flips and
# 146 git_branch_gone flips over-asserting 'abandoned' on runs that actually
# committed (features.has_resolving_commit=true), or where no ref survived to
# verify against. run_has_commit threads the run's own transcript evidence
# through to settle the ahead==0 ambiguity `git cherry` can't resolve alone.

def test_ff_merged_branch_with_run_commit_signal_is_landed(repo: Path):
    """A branch fast-forward merged into main (no merge commit — main's ref
    just advances) shows ahead==0 by patch-id, same as a truly empty branch.
    When the run's own transcript recorded a resolving commit, and the branch
    IS an ancestor of main, this must resolve to 'landed', not 'abandoned'."""
    _new_branch(repo, "worktree-agent-ffmerged")
    _run(repo, "switch", "-q", "worktree-agent-ffmerged")
    _commit(repo, "ff.txt", "ff work\n", "feat: ff work")
    _run(repo, "switch", "-q", "main")
    _run(repo, "merge", "--ff-only", "-q", "worktree-agent-ffmerged")
    bo = go.classify_branch_outcome(
        str(repo), "worktree-agent-ffmerged", worktree_live=False, run_has_commit=True)
    assert bo.outcome == "landed"
    assert bo.source == "git_ff_landed"


def test_empty_branch_without_context_abstains(repo: Path):
    """run_has_commit=None (no run-level context, e.g. the standalone
    classify_project CLI scan) must also abstain."""
    _new_branch(repo, "worktree-agent-emptyctx")
    bo = go.classify_branch_outcome(str(repo), "worktree-agent-emptyctx", worktree_live=False)
    assert bo.outcome is None
    assert bo.source == "git_empty_branch_unconfirmed"


def test_readonly_audit_run_empty_branch_abstains(repo: Path):
    """(b): ahead==0 AND the run's own transcript recorded NO resolving
    commit either — consistent with a by-design read-only/audit run,
    not necessarily abandonment. Must abstain (None), not assert abandoned."""
    _new_branch(repo, "worktree-agent-auditonly")
    bo = go.classify_branch_outcome(
        str(repo), "worktree-agent-auditonly", worktree_live=False, run_has_commit=False)
    assert bo.outcome is None
    assert bo.source == "git_empty_branch_unconfirmed"


def test_is_ancestor_helper(repo: Path):
    """Direct unit test of the _is_ancestor primitive the ff-landed path
    relies on: a branch that never diverged from main IS an ancestor; a
    branch with unlanded, unmerged commits is NOT."""
    _new_branch(repo, "worktree-agent-anc-yes")
    assert go._is_ancestor(str(repo), "worktree-agent-anc-yes", "main") is True

    _new_branch(repo, "worktree-agent-anc-no")
    _run(repo, "switch", "-q", "worktree-agent-anc-no")
    _commit(repo, "notmerged.txt", "not merged into main\n", "feat: not merged")
    _run(repo, "switch", "-q", "main")
    assert go._is_ancestor(str(repo), "worktree-agent-anc-no", "main") is False


# ── label.py integration ─────────────────────────────────────────────────────────

def test_label_from_git_merge_landed(repo: Path):
    """compute_label's git-ancestry tier labels a squash-landed dispatch branch."""
    import label
    # make a dispatch branch whose work is merged into main
    _new_branch(repo, "worktree-agent-li")
    _run(repo, "switch", "-q", "worktree-agent-li")
    _commit(repo, "li.txt", "landed integration work\n", "feat: li work")
    _run(repo, "switch", "-q", "main")
    _run(repo, "merge", "--no-ff", "-q", "worktree-agent-li",
         "-m", "Merge branch 'worktree-agent-li'")
    # worktree path that strips to <repo> so _project_repo_root finds main
    run = {"project": "x", "git_branch": "worktree-agent-li",
           "worktree": f"{repo}/.claude/worktrees/agent-li"}
    res = label.label_from_git_merge("worktree-agent-li", run)
    assert res is not None and res[0] == "landed"


def test_label_from_git_merge_ignores_non_dispatch(repo: Path):
    """Non-dispatch branch names are not git-ancestry labeled."""
    import label
    run = {"project": "x", "git_branch": "feat/normal-feature", "worktree": None}
    assert label.label_from_git_merge("feat/normal-feature", run) is None
