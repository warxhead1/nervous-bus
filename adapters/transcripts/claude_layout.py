"""Where Claude Code (and the mmx sandbox, same layout) keeps session files.

One shared walk for every reader of a ``projects`` root: the transcript
reader, the token ledger and the durable mirror.

Layout under ``<root>/<mangled cwd>/``:

- ``<session>.jsonl``: a main session.
- ``<session>/subagents/agent-*.jsonl``: an Agent-tool child.
- ``<session>/subagents/workflows/<wf_id>/agent-*.jsonl``: a workflow lane.
  Any deeper level under ``subagents/`` is a subagent too.

Stdlib only, no imports from this package, so any adapter can load it.
"""
from __future__ import annotations

import os
from typing import Iterator, List, Tuple


def _subagent_files(sub: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(sub):
        dirnames.sort()
        out.extend(os.path.join(dirpath, n) for n in filenames if n.endswith(".jsonl"))
    return out


def project_dir_files(project_dir: str) -> List[str]:
    """Sorted session files in one ``<root>/<mangled cwd>`` dir, subagents at any depth."""
    try:
        names = os.listdir(project_dir)
    except OSError:
        return []
    paths = [os.path.join(project_dir, n) for n in names if n.endswith(".jsonl")]
    for n in names:
        sub = os.path.join(project_dir, n, "subagents")
        if os.path.isdir(sub):
            paths.extend(_subagent_files(sub))
    return sorted(paths)


def session_files(root: str) -> Iterator[Tuple[str, str]]:
    """``(relpath, path)`` for every session file under a ``projects`` root, sorted."""
    try:
        dirs = sorted(e.path for e in os.scandir(root) if e.is_dir())
    except OSError:
        return
    for d in dirs:
        for p in project_dir_files(d):
            yield os.path.relpath(p, root), p


def is_subagent(path: str) -> bool:
    return "/subagents/" in path.replace(os.sep, "/")
