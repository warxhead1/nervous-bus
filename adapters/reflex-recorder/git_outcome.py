"""git_outcome.py — git-grounded outcome attribution for worktree-dispatched runs.

WHY THIS EXISTS
===============
label.py derives a run's outcome from, in precedence order, (1) bead state,
(2) a GitHub PR (`gh pr view`), (3) a behavioural shape inference.  That chain
has a blind spot for the dominant dispatch pattern in projects like tengine:

    an agent is spun up in a local git worktree on branch `worktree-agent-XXXX`
    (or `worktree-wf_XXXX`), does its work, and the work is **squash-merged into
    main locally with NO GitHub PR**.

For such runs there is no bead (bead_id is null on 100% of tengine runs) and no
PR, so both explicit paths return None and the run is left `outcome=NULL` —
unanalysable.  Yet the ground truth is fully recoverable from git itself:

    * ahead==0                      → the branch produced ZERO commits  → abandoned (empty)
    * all unique commits in main    → squash/cherry landed              → landed
    * commits NOT in main, worktree → still in flight                  → pending (no terminal label)
    * commits NOT in main, no wt    → work done then thrown away        → abandoned (discarded)
    * landed, then Revert on main   → landed-then-reverted              → reverted

This module is PROJECT-AGNOSTIC: any repo that dispatches agents into local
worktrees and merges without PRs benefits.  It reads git only — never the
conversation transcript (same privacy posture as label.py).

`git cherry main <branch>` is the key primitive.  It compares by patch-id, so a
commit whose patch was squash-merged into main shows as `-` (already present)
even though the merge commit's SHA differs.  This is what lets us detect
squash landings that `gh pr view` (no PR) and naive ancestry checks both miss.

PUBLIC API
==========
    classify_branch_outcome(repo, branch, *, worktree_live, main_ref="main")
        -> BranchOutcome(outcome, source, confidence, detail)

    live_worktree_branches(repo) -> set[str]      # branches with a live worktree
    classify_project(project, repo=None, main_ref="main") -> list[dict]  # CLI backing

CLI
===
    python3 git_outcome.py --project tengine
    python3 git_outcome.py --project tengine --repo ~/projects/tengine --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# ── Outcome vocabulary (subset of label.py's enum, plus None for pending) ───────
#   landed     — work reached main (ff/merge/squash/cherry-equivalent)
#   reverted   — landed, then a Revert of it appears on main
#   abandoned  — produced nothing (empty branch) OR work done but discarded
#   None       — pending / in-flight; caller should NOT write a terminal label

#: How far back to scan main for Revert commits matching a landed subject.
_REVERT_SCAN_DEPTH = 400

#: Subprocess timeout (seconds). git ancestry queries are local + fast.
_GIT_TIMEOUT = 8


@dataclass(frozen=True)
class BranchOutcome:
    """Result of classifying one worktree branch against main."""
    outcome: Optional[str]          # landed | reverted | abandoned | None(=pending)
    source: str                     # machine-readable provenance tag
    confidence: str                 # high | medium | low
    detail: str                     # human-readable one-liner
    ahead: int = 0                  # commits unique to the branch
    landed_commits: int = 0         # unique commits whose patch is in main
    unlanded_commits: int = 0       # unique commits whose patch is NOT in main

    def as_label(self) -> Optional[tuple[str, str]]:
        """Adapt to label.py's (outcome, source) contract. None ⇒ no terminal label."""
        if self.outcome is None:
            return None
        return self.outcome, self.source


# ── git plumbing ────────────────────────────────────────────────────────────────

def _git(repo: str, *args: str, timeout: int = _GIT_TIMEOUT) -> Optional[str]:
    """Run `git -C <repo> <args>`; return stdout stripped, or None on failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _branch_exists(repo: str, branch: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}") is not None


def live_worktree_branches(repo: str) -> set[str]:
    """Return the set of branch names that currently have a live (checked-out) worktree.

    `git worktree list --porcelain` emits `branch refs/heads/<name>` lines for
    each attached worktree.  Detached worktrees have no branch line and are
    skipped.  Used to distinguish *pending* (work not in main, worktree still
    live) from *discarded* (work not in main, worktree gone).
    """
    out = _git(repo, "worktree", "list", "--porcelain")
    branches: set[str] = set()
    if not out:
        return branches
    for line in out.splitlines():
        if line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branches.add(ref.removeprefix("refs/heads/"))
    return branches


def _subject(repo: str, ref: str) -> Optional[str]:
    return _git(repo, "log", "-1", "--format=%s", ref)


def _was_merged_into_main(repo: str, main_ref: str, branch: str) -> bool:
    """Did main record a merge commit for this branch (`Merge branch '<branch>'`)?

    This is the signal that disambiguates ahead==0: a branch whose work was
    pulled into main via a real merge commit becomes an ancestor of main, so it
    reports ahead==0 — identical, by ancestry alone, to a branch that never
    committed.  A recorded merge of the branch name proves the former.

    It also rescues the patch-id-drift case: a branch whose commits were merged
    (not squashed) but whose tip was later reset/reused will fail `git cherry`
    yet still have a merge recorded here.
    """
    out = _git(repo, "log", main_ref, f"--max-count={_REVERT_SCAN_DEPTH * 3}",
               "--merges", "--format=%s")
    if not out:
        return False
    needle = f"merge branch '{branch.lower()}'"
    return any(needle in line.lower() for line in out.splitlines())


def _has_revert_on_main(repo: str, main_ref: str, subject: str) -> bool:
    """Has main reverted a commit whose subject was `subject`?

    git's auto-generated revert subject is `Revert "<original subject>"`.  We
    scan a bounded window of main for that exact form.  Conservative: only an
    explicit auto-revert subject counts (no fuzzy substring matching that would
    false-positive on prose like 'revert the bad approach').
    """
    if not subject:
        return False
    out = _git(repo, "log", f"--max-count={_REVERT_SCAN_DEPTH}",
               "--format=%s", main_ref)
    if not out:
        return False
    target = f'revert "{subject.lower()}"'
    return any(line.strip().lower().startswith(target) for line in out.splitlines())


# ── core classifier ─────────────────────────────────────────────────────────────

def classify_branch_outcome(
    repo: str,
    branch: str,
    *,
    worktree_live: bool,
    main_ref: str = "main",
) -> BranchOutcome:
    """Classify one worktree branch's outcome relative to `main_ref`.

    Parameters
    ----------
    repo : str
        Path to the git repository (the main checkout, not the worktree).
    branch : str
        Branch name to classify (e.g. 'worktree-agent-a6a1c1efa9c3ad6fb').
    worktree_live : bool
        Whether this branch still has a live worktree (see live_worktree_branches).
        Disambiguates pending (live) from discarded (gone) for unlanded work.
    main_ref : str
        The trunk to measure against.  Default 'main'.

    Returns
    -------
    BranchOutcome
        outcome is None when the run is pending / in-flight (no terminal label).
    """
    if not _branch_exists(repo, branch):
        return BranchOutcome(
            outcome="abandoned", source="git_branch_gone", confidence="medium",
            detail=f"branch {branch} no longer exists (deleted without trace in main)",
        )

    mb = _git(repo, "merge-base", main_ref, branch)
    if mb is None:
        return BranchOutcome(
            outcome=None, source="git_no_merge_base", confidence="low",
            detail=f"no merge-base between {main_ref} and {branch}; cannot classify",
        )

    # Count the branch's OWN, NON-MERGE commits via `git cherry`, which compares
    # by patch-id and skips merge commits.  Using cherry (not `rev-list --count`)
    # means a branch that only merged main into itself — adding no original
    # work — is correctly seen as having zero own commits, and merge commits
    # never inflate the unlanded tally.
    #   '-' = patch present in main (landed)   '+' = patch absent (unlanded)
    cherry = _git(repo, "cherry", main_ref, branch)
    landed = unlanded = 0
    if cherry:
        for line in cherry.splitlines():
            if line.startswith("-"):
                landed += 1
            elif line.startswith("+"):
                unlanded += 1
    ahead = landed + unlanded

    # (1) No own non-merge commits. Either the branch never produced work, OR
    #     its work was merged into main via a merge commit (which makes it an
    #     ancestor → ahead==0) and the branch was later reset/reused.  A recorded
    #     `Merge branch '<branch>'` on main proves the latter — don't call it empty.
    if ahead == 0:
        if _was_merged_into_main(repo, main_ref, branch):
            return BranchOutcome(
                outcome="landed", source="git_merged_into_main", confidence="high",
                detail=f"{branch}'s work was merged into {main_ref} (merge commit recorded); branch since reset",
            )
        return BranchOutcome(
            outcome="abandoned", source="git_empty_branch", confidence="high",
            detail=f"{branch} never advanced past merge-base and was never merged — zero commits produced",
            ahead=0,
        )

    # (3) Everything landed → landed (unless reverted on main).
    if unlanded == 0 and landed > 0:
        subj = _subject(repo, branch) or ""
        if _has_revert_on_main(repo, main_ref, subj):
            return BranchOutcome(
                outcome="reverted", source="git_revert_on_main", confidence="high",
                detail=f"{branch}'s work landed then was reverted on {main_ref}",
                ahead=ahead, landed_commits=landed, unlanded_commits=0,
            )
        return BranchOutcome(
            outcome="landed", source="git_squash_equiv", confidence="high",
            detail=f"all {landed} commit(s) of {branch} are patch-present in {main_ref} (squash/cherry landed)",
            ahead=ahead, landed_commits=landed, unlanded_commits=0,
        )

    # (4) Partially landed — most work merged, a tail left behind. Treat as landed
    #     (the productive intent reached main) but flag the residue.
    if landed > 0 and unlanded > 0:
        return BranchOutcome(
            outcome="landed", source="git_partial_squash", confidence="medium",
            detail=f"{landed}/{landed + unlanded} commit(s) of {branch} landed in {main_ref}; {unlanded} not",
            ahead=ahead, landed_commits=landed, unlanded_commits=unlanded,
        )

    # (5) Patch-id says nothing landed — but a recorded merge of the branch into
    #     main overrides that (merge-then-reword/rebase drifts the patch-id).
    if _was_merged_into_main(repo, main_ref, branch):
        return BranchOutcome(
            outcome="landed", source="git_merged_into_main", confidence="high",
            detail=f"{branch} merged into {main_ref} via merge commit (patch-id drifted, merge recorded)",
            ahead=ahead, landed_commits=landed, unlanded_commits=unlanded,
        )

    # (6) Live worktree ⇒ still in flight (no terminal label).
    if worktree_live:
        return BranchOutcome(
            outcome=None, source="git_pending", confidence="high",
            detail=f"{branch} has {unlanded} unlanded commit(s) and a live worktree — in flight",
            ahead=ahead, landed_commits=0, unlanded_commits=unlanded,
        )

    # (7) No live worktree, patch-id absent, no recorded merge → likely discarded.
    #     MEDIUM confidence only: `git cherry` patch-id can drift under
    #     rebase/squash-with-edits, so the work *could* have landed under a
    #     reworded commit.  verify_discarded_landed() (pickaxe over the actual
    #     added code) upgrades or refutes this before any terminal "abandoned"
    #     label is written.  We never poison the run-store on patch-id alone.
    return BranchOutcome(
        outcome="abandoned", source="git_discarded_unmerged", confidence="medium",
        detail=f"{branch} has {unlanded} commit(s) none in {main_ref}, no merge, no live worktree — "
               f"likely discarded (verify with pickaxe before labeling)",
        ahead=ahead, landed_commits=0, unlanded_commits=unlanded,
    )


# ── deep verification: follow the code, not the commit identity ─────────────────

#: An added line must be at least this long (after strip) to be a usable pickaxe
#: probe — short lines (`}`, `return;`) match everywhere and prove nothing.
_PROBE_MIN_LEN = 24
_PICKAXE_DEPTH = 2500


def _distinctive_added_lines(repo: str, mb: str, branch: str, limit: int = 6) -> list[str]:
    """Sample the most distinctive ADDED source lines from branch's diff vs mb."""
    diff = _git(repo, "diff", mb, branch, timeout=20) or ""
    cands: list[str] = []
    for ln in diff.splitlines():
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        s = ln[1:].strip()
        if len(s) < _PROBE_MIN_LEN or re.match(r"^(//|\*|#|/\*)", s) or not re.search(r"[A-Za-z0-9_]{6}", s):
            continue
        cands.append(s)
    seen: set[str] = set()
    out: list[str] = []
    for s in sorted(cands, key=len, reverse=True):
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def verify_discarded_landed(repo: str, branch: str, main_ref: str = "main") -> Optional[bool]:
    """Pickaxe check: did the branch's added code actually reach `main_ref`?

    Returns True if ALL sampled distinctive lines are found in main's history
    (`git log -S`) — the work landed under a different/reworded commit and the
    'discarded' verdict is a patch-id-drift false positive.  Returns False if
    NONE are found (genuinely discarded).  Returns None if no usable probe lines
    could be sampled (cannot decide — leave the medium-confidence verdict).

    This follows the CODE, so it survives rebase, squash, and reword that defeat
    commit-identity checks.  Slower than the ancestry classifier (history-wide
    pickaxe), so it is opt-in: call it only to confirm a medium-confidence
    'abandoned' before writing a terminal label.
    """
    mb = _git(repo, "merge-base", main_ref, branch)
    if not mb:
        return None
    probes = _distinctive_added_lines(repo, mb, branch)
    if not probes:
        return None
    found = 0
    for line in probes:
        if _git(repo, "log", main_ref, f"--max-count={_PICKAXE_DEPTH}", "-S", line, "--oneline", timeout=40):
            found += 1
    if found == len(probes):
        return True       # all the code is in main → it landed
    if found == 0:
        return False      # none of the code is in main → genuinely discarded
    return None           # partial → ambiguous, don't override


# ── project-level convenience (CLI + analysis) ──────────────────────────────────

def _default_repo(project: str) -> str:
    return os.path.expanduser(f"~/projects/{project}")


def _worktree_dispatch_branches(repo: str) -> list[str]:
    """All local branches that look like agent/workflow worktree dispatches."""
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    if not out:
        return []
    pat = re.compile(r"^(worktree-agent-|worktree-wf[_-]|agent-|wf[_-])")
    return [b for b in out.splitlines() if pat.match(b)]


def classify_project(
    project: str,
    repo: Optional[str] = None,
    main_ref: str = "main",
    branches: Optional[list[str]] = None,
    verify: bool = False,
) -> list[dict]:
    """Classify every worktree-dispatch branch in a project's repo.

    ⚠️  SURVIVORSHIP BIAS — read before trusting the aggregate.  This scans
    branches that STILL EXIST.  Projects that delete merged worktree branches
    (e.g. tachyonac-engine, deer-flow) leave only the empty/abandoned failures
    behind, so a branch-population scan over-reports 'abandoned_empty' (observed:
    tachyonac 100%, deer-flow 68% empty — an artifact, not a true waste rate).
    Projects that keep landed branches (tengine) give a representative scan.
    For an UNBIASED dispatch-outcome rate, measure over the RUN-STORE instead —
    every run is recorded at dispatch time, immune to later branch deletion —
    via label.py's label_from_git_merge (run-keyed).  Use this scan for triage
    and per-branch drill-down, not for cross-project rate comparison.

    Returns a list of dicts (BranchOutcome + branch name), one per branch.
    When verify=True, every medium-confidence 'git_discarded_unmerged' verdict
    is pickaxe-checked (verify_discarded_landed): confirmed-landed flips to
    landed/git_pickaxe_landed (high), confirmed-gone is upgraded to high.
    """
    repo = repo or _default_repo(project)
    if not Path(repo, ".git").exists() and not Path(repo).joinpath(".git").is_file():
        # tolerate worktree-style .git file; only bail if path is clearly not a repo
        if _git(repo, "rev-parse", "--git-dir") is None:
            raise SystemExit(f"not a git repo: {repo}")

    live = live_worktree_branches(repo)
    branch_list = branches if branches is not None else _worktree_dispatch_branches(repo)

    results: list[dict] = []
    for br in branch_list:
        bo = classify_branch_outcome(
            repo, br, worktree_live=(br in live), main_ref=main_ref
        )
        if verify and bo.source == "git_discarded_unmerged":
            landed = verify_discarded_landed(repo, br, main_ref)
            if landed is True:
                bo = BranchOutcome(
                    outcome="landed", source="git_pickaxe_landed", confidence="high",
                    detail=f"{br}: all sampled added code present in {main_ref} history (landed under reworded commit)",
                    ahead=bo.ahead, landed_commits=bo.landed_commits, unlanded_commits=bo.unlanded_commits,
                )
            elif landed is False:
                bo = BranchOutcome(
                    outcome="abandoned", source="git_discarded_verified", confidence="high",
                    detail=f"{br}: none of the sampled added code reached {main_ref} (pickaxe-confirmed discard)",
                    ahead=bo.ahead, landed_commits=bo.landed_commits, unlanded_commits=bo.unlanded_commits,
                )
        row = {"branch": br, "worktree_live": br in live, **asdict(bo)}
        results.append(row)
    return results


def _summarize(rows: list[dict]) -> dict:
    """Roll up a per-branch classification into a project-level verdict."""
    buckets: dict[str, int] = {}
    for r in rows:
        key = r["outcome"] if r["outcome"] is not None else "pending"
        # refine abandoned into empty vs discarded for the headline
        if r["source"] == "git_empty_branch":
            key = "abandoned_empty"
        elif r["source"] in ("git_discarded_unmerged", "git_discarded_verified"):
            key = "abandoned_discarded"
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda kv: -kv[1]))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="git-grounded outcome attribution for worktree-dispatched agent runs")
    ap.add_argument("--project", required=True, help="project name (e.g. tengine)")
    ap.add_argument("--repo", default=None, help="repo path (default ~/projects/<project>)")
    ap.add_argument("--main", default="main", dest="main_ref", help="trunk ref (default main)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--verify", action="store_true",
                    help="pickaxe-verify medium-confidence discards (slower, refutes patch-id-drift false positives)")
    ap.add_argument("--branch", action="append", dest="branches",
                    help="classify a specific branch (repeatable); default = all dispatch branches")
    args = ap.parse_args(argv)

    rows = classify_project(args.project, args.repo, args.main_ref, args.branches, verify=args.verify)
    summary = _summarize(rows)

    if args.json:
        print(json.dumps({"project": args.project, "summary": summary, "branches": rows}, indent=2))
        return 0

    print(f"\n=== {args.project}: {len(rows)} dispatch branch(es) vs {args.main_ref} ===")
    for r in sorted(rows, key=lambda x: (x["outcome"] or "zzz", x["source"])):
        oc = r["outcome"] or "pending"
        print(f"  {oc:10s} [{r['confidence']:6s}] {r['source']:26s} {r['branch']:40s}  "
              f"(ahead={r['ahead']} landed={r['landed_commits']} unlanded={r['unlanded_commits']})")
    print(f"\n  --- summary ---")
    total = sum(summary.values()) or 1
    for k, v in summary.items():
        print(f"    {k:22s} {v:4d}  ({100*v//total}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
