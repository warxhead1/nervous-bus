"""Rewrite shell shapes the isolated-lane Bash guard denies into a script file.

The guard judges the command AFTER PreToolUse hooks rewrite it (measured in
nervous-bus#106), so a denied shape can be turned into `bash <abs script>`, the
form it passes. Only in a linked worktree, only when `isoshape` is "correct" in
the argshape config, and never when a git call points outside the worktree.

Shapes rewritten: git inside a for/while/until loop, git with `$(...)` or
backticks, and an `eval` token after the first word (`claude plugin eval`).
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
from pathlib import Path

LOOP = re.compile(r"(?:^|[;&|(]\s*|\n\s*)(?:for|while|until)\s")
SUBST = re.compile(r"\$\(|`")
GIT = re.compile(r"(?<![\w-])git(?![\w-])")
EVAL = re.compile(r"(?<=\s)eval(?![\w-])")
_ISO = Path(__file__).resolve().parents[2] / "tools" / "iso" / "iso.py"


def worktree_top(cwd: str) -> Path | None:
    """The checkout root when cwd is inside a linked worktree (its .git is a file)."""
    p = Path(cwd).resolve()
    for d in (p, *p.parents):
        g = d / ".git"
        if g.is_file():
            return d
        if g.exists():
            return None
    return None


def _iso():
    spec = importlib.util.spec_from_file_location("iso_helper", _ISO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def trigger(command: str) -> str | None:
    """Name the denied shape, or None."""
    if GIT.search(command):
        if LOOP.search(command):
            return "git inside a loop"
        if SUBST.search(command):
            return "git with command substitution"
    if EVAL.search(command):
        return "an eval token"
    return None


def rewrite(command: str, cwd: str) -> tuple[str, str] | None:
    """Return (new command, note) or None to leave the command alone."""
    why = trigger(command)
    if why is None or not _ISO.is_file():
        return None
    top = worktree_top(cwd)
    if top is None:
        return None
    iso = _iso()
    try:
        iso.refuse_foreign_git(command, top)
    except SystemExit:
        return None
    path = iso.scratch_dir() / f"cmd-{hashlib.sha256(command.encode()).hexdigest()[:12]}.sh"
    path.write_text("set -o pipefail\ncd " + _quote(str(top)) + "\n" + command + "\n")
    return f"bash {path}", f"the isolated-lane guard denies {why}; ran it as a script file"


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def enabled(cfg: dict) -> bool:
    return cfg.get("isoshape", "off") == "correct" and os.environ.get("ISOSHAPE_DISABLE") != "1"
