"""detectors/worktree_leak.py — Tier-1 worktree-leak detector.

Detects a worktree that is integrated (branch merged OR bead closed / outcome
is a terminal-success state) but whose directory still exists on disk.

Algorithm
=========
1. Query runs with outcome IN ('clean', 'abandoned') AND worktree IS NOT NULL.
   Those outcomes mean the work is done; the worktree *should* have been cleaned.

2. For each unique (project, worktree) from those runs:
   a. Check if the directory still exists on disk (os.path.isdir).
   b. Cross-reference against `git worktree list --porcelain` in the *repo root*
      inferred from the worktree path.  Join on ABSOLUTE path — never the slug.
      A slug-vs-absolute join silently returns ZERO matches (audit finding b1a).

3. If the directory exists AND git still knows about it → LEAKED.
   (If git no longer knows about it but the dir exists, that is an orphan —
    a separate future detector.  This detector focuses on the git-tracked case.)

4. Emit a PatternCandidate per (project, worktree_path) with:
   - evidence listing the run IDs, git branch, bead_id, outcome
   - proposed_remediation: an Automate-rung hook spec that runs
     `git worktree remove --force <path>` on bead-close / PR-merge

Automate-rung remediation
==========================
The proposed fix is expressed as a hook spec string that describes the
autonomous action.  The remediation ladder (b7+) will pick this up and
promote it to an actual hook when confidence is high enough.

Signature = f"{project}:worktree_leak:{worktree_path}"
   — stable across runs; does NOT include run_id.

Usage
=====
    import sqlite3
    from detectors.worktree_leak import WorktreeLeakDetector

    conn = sqlite3.connect("~/.cache/nervous-bus/reflex/runs.db")
    detector = WorktreeLeakDetector(conn)
    candidates = detector.run()
    for c in candidates:
        payload = detector.emit_candidate(c)
        print(payload)
"""
from __future__ import annotations

import os
import subprocess
import sqlite3
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate


# Outcomes that indicate the work is done and the worktree should be gone.
_TERMINAL_OUTCOMES = ("clean", "abandoned")

# Automate-rung remediation template
_REMEDIATION_TEMPLATE = (
    "Automate-rung: on bead-close or PR-merge for project '{project}', "
    "run `git worktree remove --force {worktree_path}` to prune stale "
    "worktree '{slug}'.  Hook trigger: bus.agent.run.closed.v1 with "
    "outcome IN {outcomes} AND worktree_slug='{slug}'."
)


def _git_worktree_paths(repo_root: str) -> set[str]:
    """Return the set of absolute worktree paths registered with git.

    Runs `git worktree list --porcelain` in *repo_root* and parses the
    'worktree <path>' lines.  Returns an empty set on any failure.

    CRITICAL: we return ABSOLUTE paths exactly as git reports them.
    Never compare against slugs — that is the b1a audit failure mode.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=10,
        )
        if result.returncode != 0:
            return set()
        paths: set[str] = set()
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
                if path:
                    paths.add(path)
        return paths
    except Exception:
        return set()


def _infer_repo_root(worktree_path: str) -> Optional[str]:
    """Infer the main git repo root from a worktree absolute path.

    Worktree paths follow the convention:
      /home/eric/projects/<project>/.claude/worktrees/<slug>
      /home/eric/projects/<project>/.worktrees/<slug>
      /home/eric/projects/<project>-<suffix>   (top-level worktrees/ style)

    We walk up the path until we find a parent that is a git main worktree
    (i.e., has a .git *directory*, not a .git *file* which is a worktree).

    Fallback: try running `git -C <path> rev-parse --git-common-dir` to get
    the common git dir, then resolve from there.
    """
    # Strategy 1: git rev-parse --git-common-dir
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=worktree_path,
            timeout=5,
        )
        if result.returncode == 0:
            common_dir = result.stdout.strip()
            # common_dir is e.g. /home/eric/projects/foo/.git
            # repo root is its parent
            import os.path
            if common_dir.endswith("/.git") or common_dir == ".git":
                candidate = os.path.dirname(os.path.abspath(common_dir))
                if os.path.isdir(candidate):
                    return candidate
            # could also be an absolute path directly
            candidate = os.path.dirname(common_dir)
            if os.path.isdir(candidate):
                return candidate
    except Exception:
        pass

    # Strategy 2: walk upward looking for .git directory (not file)
    current = worktree_path
    for _ in range(10):
        parent = os.path.dirname(current)
        if parent == current:
            break
        git_path = os.path.join(parent, ".git")
        if os.path.isdir(git_path):
            return parent
        current = parent

    return None


class WorktreeLeakDetector(BaseDetector):
    """Detect worktrees that are done (terminal outcome) but still on disk.

    See module docstring for the full algorithm.
    """

    DETECTOR_NAME = "worktree_leak"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Scan runs for terminal-outcome worktrees that still exist on disk.

        Join on reconstructed ABSOLUTE worktree path (runs.worktree) against
        `git worktree list` output — never against the slug alone.
        """
        # Pull all distinct (project, worktree, worktree_slug) for terminal runs.
        # Include git_branch + bead_id for evidence richness.
        cur = conn.execute(
            """
            SELECT project, worktree, worktree_slug, git_branch, bead_id,
                   GROUP_CONCAT(run_id, '|') AS run_ids,
                   GROUP_CONCAT(outcome, '|') AS outcomes
            FROM runs
            WHERE outcome IN ({placeholders})
              AND worktree IS NOT NULL
              AND worktree != ''
            GROUP BY project, worktree
            """.format(
                placeholders=",".join("?" * len(_TERMINAL_OUTCOMES))
            ),
            _TERMINAL_OUTCOMES,
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        run_rows = [dict(zip(cols, row)) for row in rows]

        candidates: list[PatternCandidate] = []

        # Cache git worktree sets per repo root to avoid redundant subprocess calls.
        _git_cache: dict[str, set[str]] = {}

        for row in run_rows:
            worktree_path: str = row["worktree"]
            project: str = row["project"]
            slug: str = row.get("worktree_slug") or os.path.basename(worktree_path)

            # 1. Does the directory still exist on disk?
            if not os.path.isdir(worktree_path):
                continue

            # 2. Is it still registered with git?  Use absolute path join.
            repo_root = _infer_repo_root(worktree_path)
            if repo_root is None:
                # Can't determine repo; skip rather than false-positive.
                continue

            if repo_root not in _git_cache:
                _git_cache[repo_root] = _git_worktree_paths(repo_root)
            git_paths = _git_cache[repo_root]

            # CRITICAL: compare absolute paths, NOT slugs.
            if worktree_path not in git_paths:
                # Dir exists but git already pruned it — orphan, not a leak.
                continue

            # 3. Build evidence
            run_ids_raw: str = row.get("run_ids") or ""
            run_ids: list[str] = [r for r in run_ids_raw.split("|") if r]
            evidence: list[str] = [
                f"worktree_path={worktree_path}",
                f"project={project}",
                f"slug={slug}",
            ]
            if row.get("git_branch"):
                evidence.append(f"git_branch={row['git_branch']}")
            if row.get("bead_id"):
                evidence.append(f"bead_id={row['bead_id']}")
            if run_ids:
                evidence.append(f"run_ids={','.join(run_ids[:5])}")
            outcomes_str: str = row.get("outcomes") or ""
            if outcomes_str:
                evidence.append(f"outcomes={outcomes_str}")

            # 4. Proposed remediation (Automate-rung)
            remediation = _REMEDIATION_TEMPLATE.format(
                project=project,
                worktree_path=worktree_path,
                slug=slug,
                outcomes=str(_TERMINAL_OUTCOMES),
            )

            signature = f"{project}:worktree_leak:{worktree_path}"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="worktree_leak",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(run_ids),
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=remediation,
                    extra={
                        "worktree_path": worktree_path,
                        "worktree_slug": slug,
                        "git_branch": row.get("git_branch"),
                        "bead_id": row.get("bead_id"),
                        "repo_root": repo_root,
                    },
                )
            )

        return candidates
