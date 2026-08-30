"""repo_config — per-repo settings for the maintenance train.

Path is the SHARED checkout (used only for the dirty-check and to resolve the
default branch / gh repo slug). All agent edits happen in a data2 worktree cut
from this path, never here directly. Commands are read/paraphrased from each
repo's own CLAUDE.md / Makefile — see the comment above each entry for the
source line so a future edit can re-derive instead of guessing.

Add a repo here before selector.py or dispatch.sh will touch it; anything
absent is out of scope by construction (fail closed, not "assume defaults").
"""
from __future__ import annotations

from pathlib import Path

HOME = Path.home()

# gh_repo is "<owner>/<name>" as it appears on github.com — required for the
# `gh pr create --repo` / `gh pr list --repo` calls in finalize.sh/selector.py.
REPOS = {
    "nervous-bus": {
        "path": str(HOME / "projects" / "nervous-bus"),
        "gh_repo": "warxhead1/nervous-bus",
        # CLAUDE.md "Build / test": shell SDK has no build; schema validation
        # is the closest thing to a repo-wide gate.
        "build_cmd": "true",
        "test_cmd": "chmod +x sdk/shell/nervous && sdk/shell/nervous --help >/dev/null",
    },
    "hearth": {
        "path": str(HOME / "projects" / "hearth"),
        "gh_repo": "warxhead1/hearth",
        "build_cmd": "true",
        "test_cmd": "make test",
    },
    "hearth-loom": {
        "path": str(HOME / "projects" / "hearth-loom"),
        "gh_repo": "warxhead1/hearth-loom",
        "build_cmd": "true",
        "test_cmd": "make test",
    },
    "deer-flow": {
        "path": str(HOME / "projects" / "deer-flow"),
        "gh_repo": "warxhead1/deer-flow",
        "build_cmd": "true",
        "test_cmd": "make test",
    },
    "orca": {
        "path": str(HOME / "projects" / "orca"),
        "gh_repo": "warxhead1/orca",
        "build_cmd": "true",
        "test_cmd": "make test",
    },
    "claude-hook-fast": {
        "path": str(HOME / "projects" / "claude-hook-fast"),
        "gh_repo": "warxhead1/claude-hook-fast",
        # Measured 2026-08-30: `gofmt -l .` lists top-level drift (inbox.go,
        # main.go, main_test.go, event.go, ...). `go build ./...` / `go test
        # ./...` are the repo's real gates (Go module, no Makefile target
        # observed for this repo at audit time).
        "build_cmd": "go build ./...",
        "test_cmd": "gofmt -l . | grep -v '^\\.claude/worktrees/' | (! grep .) && go vet ./... && go test ./...",
    },
    "kb": {
        "path": str(HOME / "projects" / "kb"),
        "gh_repo": "warxhead1/kb",
        "build_cmd": "true",
        "test_cmd": "make test",
    },
    "tengine": {
        "path": str(HOME / "projects" / "tengine"),
        "gh_repo": "warxhead1/tengine",
        "build_cmd": "true",
        "test_cmd": "true",  # tengine's real gate is a multi-hour CI matrix; the
        # train never targets tengine chores until a fast subset gate exists
        # (see MAX_MINUTES gate in selector.py — tengine chores routinely
        # exceed the 60-minute cap and are excluded there, not here).
    },
}

WORKTREE_ROOT = HOME / "data2" / "worktrees"
CACHE_ROOT = HOME / ".cache" / "nervous-bus" / "maintenance-train"
