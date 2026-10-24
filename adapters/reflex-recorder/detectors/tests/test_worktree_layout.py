"""Every worktree-aware detector recognises the ~/data2/worktrees/<project>/<slug>
layout, not only the retired <repo>/.claude/worktrees/<slug> one.

Agent worktrees moved on 2026-08-24; detectors that matched only the old paths
saw no delegated-agent segments and went silent while their inputs kept flowing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from detectors.dispatch_lineage import segment_by_worktree, worktree_slug_of
from detectors.edit_build_fail_revert import _normalize_area
from detectors.rebuild_cache_miss import _is_worktree_cwd
from detectors.reread_same_file import _normalize_path
from detectors.user_correction import _resolve_run

NEW = "/home/eric/data2/worktrees/hearth/agent-a1b2c3/crates/core"
OLD = "/home/eric/projects/hearth/.claude/worktrees/agent-a1b2c3/crates/core"


def test_slug_from_both_layouts():
    assert worktree_slug_of(NEW) == "agent-a1b2c3"
    assert worktree_slug_of(OLD) == "agent-a1b2c3"
    assert worktree_slug_of("/home/eric/projects/hearth/crates/core") is None
    assert worktree_slug_of("/home/eric/data2/worktrees") is None


def test_segment_from_new_layout():
    segs = segment_by_worktree([
        {"cwd": NEW, "tool_name": "Edit", "data": {"file_path": f"{NEW}/src/lib.rs"}},
        {"cwd": "/home/eric/projects/hearth", "tool_name": "Bash"},
    ])
    assert list(segs) == ["agent-a1b2c3"]
    assert segs["agent-a1b2c3"].child_agent_id == "a1b2c3"


def test_rebuild_cache_miss_worktree_cwd():
    assert _is_worktree_cwd(NEW) and _is_worktree_cwd(OLD)
    assert not _is_worktree_cwd("/home/eric/projects/hearth")


def test_paths_anchor_the_same_across_layouts():
    assert _normalize_path(f"{NEW}/src/lib.rs") == _normalize_path(f"{OLD}/src/lib.rs") == "crates/core/src/lib.rs"
    assert _normalize_area(f"{NEW}/src/lib.rs", "", "hearth") == _normalize_area(f"{OLD}/src/lib.rs", "", "hearth")


def test_opening_prompt_resolves_to_first_run():
    runs = [("r2", "p", 200.0, 300.0), ("r1", "p", 100.0, 150.0)]
    assert _resolve_run(runs, 99.0) == ("r1", "p")
    assert _resolve_run(runs, 160.0) == ("r1", "p")
    assert _resolve_run([], 99.0) == (None, None)
