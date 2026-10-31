"""Normalized records shared by every agent source.

A source maps its own transcript format onto these; consumers (argshape
backtest, reflex-recorder ledgers) never see a client-specific shape.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class SessionRef:
    agent: str
    id: str
    path: str                    # file (or db) the session lives in
    mtime: float
    cwd: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    agent: str
    session: str
    call_id: str
    tool: str                    # the agent's own tool name (Bash, exec_command, shell, ...)
    input: dict
    ts: str = ""                 # RFC3339 as the agent wrote it, "" when absent
    cwd: str = ""
    shell: bool = False          # True when this call ran a shell command
    command: str = ""            # the shell command text when shell
    output: Optional[str] = None
    is_error: Optional[bool] = None
    exit_code: Optional[int] = None
    harness: bool = False        # failed before/around the program: denied, blocked, lost output


class Source:
    """One coding agent. Subclasses set `name` and implement roots/sessions/tool_calls."""
    name = ""
    label = ""
    live_hooks = ""              # how a live tool (argshape) attaches, "" when none

    def roots(self) -> list[str]:
        raise NotImplementedError

    def sessions(self, since: float = 0.0) -> Iterator[SessionRef]:
        raise NotImplementedError

    def tool_calls(self, ref: SessionRef) -> Iterator[ToolCall]:
        raise NotImplementedError


# ---------------------------------------------------------------- paths

def home() -> str:
    return os.path.expanduser("~")


def env_path(var: str, *default: str) -> str:
    v = os.environ.get(var)
    return os.path.expanduser(v) if v else os.path.join(*default)


def xdg_data() -> str:
    if sys.platform == "win32":
        return os.environ.get("LOCALAPPDATA") or os.path.join(home(), "AppData", "Local")
    if sys.platform == "darwin" and not os.environ.get("XDG_DATA_HOME"):
        return os.path.join(home(), "Library", "Application Support")
    return env_path("XDG_DATA_HOME", home(), ".local", "share")


def unique(paths: list[str]) -> list[str]:
    """Drop repeats (by real path) keeping order: one dir reached two ways is read once."""
    seen, out = set(), []
    for p in paths:
        k = os.path.realpath(p)
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def extra_roots(agent: str) -> list[str]:
    """Operator-added roots: NBUS_TRANSCRIPTS_<AGENT>=dir1<pathsep>dir2 (globs allowed)."""
    import glob
    v = os.environ.get(f"NBUS_TRANSCRIPTS_{agent.upper().replace('-', '_')}", "")
    out: list[str] = []
    for p in filter(None, v.split(os.pathsep)):
        out += sorted(glob.glob(os.path.expanduser(p)))
    return out


# ---------------------------------------------------------------- shell text

_SHELL_WRAP = re.compile(r"^(?:/\S*/)?(?:ba|z|da)?sh$")


def shell_text(command) -> str:
    """`["bash", "-lc", "x"]` -> "x"; a string passes through."""
    if isinstance(command, str):
        return command
    if isinstance(command, list) and command:
        parts = [str(c) for c in command]
        if len(parts) >= 3 and _SHELL_WRAP.match(parts[0]) and parts[1] in ("-c", "-lc", "-ic"):
            return parts[2]
        return " ".join(parts)
    return ""
