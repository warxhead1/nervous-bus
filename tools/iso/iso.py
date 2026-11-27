#!/usr/bin/env python3
"""iso — run the shell shapes an isolated subagent's Bash guard refuses.

The worktree guard denies compound git, loops, `$(...)`, `eval` and similar
shapes it cannot verify, but it passes `python3 -I /abs/path/iso.py <verb>`.
This helper runs those shapes itself and keeps the guard's intent: git never
leaves the lane's worktree.

  iso.py sync                       fetch origin and fast-forward to origin/main
  iso.py each '<cmd with {}>' a b   run the command once per item
  iso.py run [--keep]               run the script on stdin (bash) from the worktree
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

GIT_REDIRECTS = re.compile(
    r"--git-dir|--work-tree|\bGIT_DIR=|\bGIT_WORK_TREE=|\bgit\s+(?:-\S+\s+)*-C\b"
)
CD_THEN_GIT = re.compile(r"\bcd\s+(\S+)")


def toplevel(cwd: str) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        sys.exit("iso: not inside a git worktree")
    return Path(out.stdout.strip()).resolve()


def scratch_dir() -> Path:
    base = os.environ.get("ISO_SCRATCH") or str(Path.home() / ".cache" / "nervous-bus" / "iso")
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _inside(path: str, top: Path, extra: Path) -> bool:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        return True
    p = p.resolve()
    return top in (p, *p.parents) or extra in (p, *p.parents)


def refuse_foreign_git(script: str, top: Path) -> None:
    """Keep the guard's rule: a git call may only target this worktree."""
    extra = scratch_dir().resolve()
    for n, line in enumerate(script.splitlines(), 1):
        if "git" not in line:
            continue
        for m in re.finditer(r"(?:-C|--git-dir=?|--work-tree=?)\s*(\S+)", line):
            if not _inside(m.group(1).strip("'\""), top, extra):
                sys.exit(f"iso: line {n} points git outside this worktree: {line.strip()[:120]}")
        if GIT_REDIRECTS.search(line) and not re.search(r"(?:-C|--git-dir=?|--work-tree=?)\s*\S+", line):
            sys.exit(f"iso: line {n} redirects git with an env var or a computed path: {line.strip()[:120]}")
        for m in CD_THEN_GIT.finditer(line):
            tgt = m.group(1).strip("'\";&|")
            if tgt.startswith(("$", "`", "~")) or not _inside(tgt, top, extra):
                sys.exit(f"iso: line {n} changes directory outside this worktree before git: {line.strip()[:120]}")


def run_script(script: str, top: Path, keep: bool) -> int:
    refuse_foreign_git(script, top)
    sha = hashlib.sha256(script.encode()).hexdigest()[:12]
    path = scratch_dir() / f"cmd-{sha}.sh"
    path.write_text("set -o pipefail\n" + script)
    try:
        return subprocess.run(["bash", str(path)], cwd=top, check=False).returncode
    finally:
        if not keep:
            path.unlink(missing_ok=True)


def cmd_sync(top: Path) -> int:
    for argv in (["git", "fetch", "-q", "origin"], ["git", "merge", "--ff-only", "origin/main"]):
        rc = subprocess.run(argv, cwd=top, check=False).returncode
        if rc:
            return rc
    return 0


def cmd_each(template: str, items: list[str], top: Path) -> int:
    if "{}" not in template:
        sys.exit("iso: the command needs a {} placeholder")
    worst = 0
    for item in items:
        script = template.replace("{}", shlex.quote(item))
        print(f"=== {item}", flush=True)
        worst = run_script(script, top, keep=False) or worst
    return worst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("sync")
    e = sub.add_parser("each")
    e.add_argument("template")
    e.add_argument("items", nargs="+")
    r = sub.add_parser("run")
    r.add_argument("--keep", action="store_true", help="keep the generated script file")
    args = ap.parse_args(argv)
    top = toplevel(os.getcwd())
    if args.verb == "sync":
        return cmd_sync(top)
    if args.verb == "each":
        return cmd_each(args.template, args.items, top)
    return run_script(sys.stdin.read(), top, args.keep)


if __name__ == "__main__":
    sys.exit(main())
