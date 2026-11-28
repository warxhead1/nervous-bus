"""Command classification for tool spans. Stdlib only.

classify(tool, command) -> (class, wait_target). The class names what kind of work a
call was. A call is a wait when it polls, sleeps or blocks; its wait target says what
it waits on (a lock, a build, an e2e run, a background task), taken from the whole
command text. The word "wait" alone decides nothing.
"""
from __future__ import annotations

import re
from typing import Optional

_QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\s*\1\b", re.S)

# What makes a shell command a wait (checked on the command with quoted text removed).
_WAIT = re.compile(
    r"\bsleep\s+[\d$]"
    r"|\buntil\b"
    r"|\bwhile\b[^\n]*\bsleep\b"
    r"|\bflock\b"
    r"|(?:^|[;&|(]\s*)wait(?:\s+-n)?(?:\s+[\d$%]\S*)*\s*(?:$|[;&|)])"
    r"|(?<![/\w-])(?:neon-run|orca)\s[^\n]*\bwait\b"
    r"|\bgh\s+run\s+watch\b"
    r"|--watch\b"
    r"|\btail\s+-[a-zA-Z]*f\b"
    r"|\binotifywait\b"
    r"|\bkill\s+-0\b",
    re.M,
)

# Wait targets, first match wins, searched in the full command text.
_TARGETS = [
    ("lock", re.compile(r"flock|\.lock\b|lockfile|slots?\b|mkdir\s+\S*lock|\block\b", re.I)),
    ("e2e", re.compile(r"e2e|journey|playwright|cypress|selenium", re.I)),
    ("gate", re.compile(r"\b(?:gate|preflight|verify|check)(?:\.sh)?\b", re.I)),
    ("test", re.compile(r"cargo\s+(?:nextest|test)|pytest|go\s+test|npm\s+(?:run\s+)?test|jest|vitest|\btests?\b", re.I)),
    ("build", re.compile(r"cargo\s+(?:build|check|clippy|run)|\bmake\b|cmake|go\s+build|\bbuild\b|compil|webpack|vite|gradle|docker\s+build", re.I)),
    ("ci", re.compile(r"gh\s+(?:run|pr\s+checks|workflow)|actions|\bci\b|pipeline", re.I)),
    ("background_task", re.compile(r"neon-run|(?<![/\w-])orca\s|\.output\b|task|\bpid\b|pgrep|ps\s+-p|/proc/|worker|agent|kill\s+-0|\bjobs?\b|\$!", re.I)),
]
_BARE_SLEEP = re.compile(r"^\s*sleep\s+\d+(?:\.\d+)?[smh]?\s*$")

# Non-wait classes for shell commands, first match wins, on the command with quotes removed.
_SHELL = [
    ("gate", re.compile(r"\b(?:gate|check|ci|verify|preflight)\.sh\b|\bmake\s+(?:gate|check|ci|verify)\b")),
    ("e2e", re.compile(r"e2e|journey|playwright|cypress")),
    ("test", re.compile(r"cargo\s+(?:nextest|test)|\bpytest\b|go\s+test|npm\s+(?:run\s+)?test|\bjest\b|\bvitest\b|\bctest\b")),
    ("build", re.compile(r"cargo\s+(?:build|check|clippy|run)|\bmake\b|\bcmake\b|go\s+build|npm\s+run\s+build|\bgradle|docker\s+build|\bwasm-pack\b")),
    ("vcs", re.compile(r"(?:^|[;&|(]\s*)(?:cd\s+[^;&|]*(?:&&|;)\s*)?git\s", re.M)),
    ("github", re.compile(r"(?:^|[;&|(]\s*)gh\s", re.M)),
    ("orchestration", re.compile(r"(?:^|[;&|(]\s*)(?:\S*/)?(?:neon-run|orca|bd|nervous)\s", re.M)),
]

_TOOL_CLASS = {
    "Read": "read", "NotebookRead": "read",
    "Grep": "search", "Glob": "search", "LS": "search", "ToolSearch": "search",
    "Edit": "edit", "Write": "edit", "NotebookEdit": "edit", "MultiEdit": "edit",
    "Agent": "subagent", "Task": "subagent", "SendMessage": "subagent",
    "WebFetch": "web", "WebSearch": "web",
}


def strip_text(command: str) -> str:
    """Drop heredoc bodies and quoted strings so prose never decides a class."""
    return _QUOTED.sub('""', _HEREDOC.sub("<<EOF", command))


def wait_target(text: str) -> str:
    """What a wait waits on. `text` is the whole command (or a monitor's command)."""
    if _BARE_SLEEP.match(text):
        return "time"
    for name, rx in _TARGETS:
        if rx.search(text):
            return name
    return "unknown"


def tool_only_class(tool: str, command: Optional[str]) -> Optional[tuple[str, Optional[str]]]:
    """Class for tools whose name decides it (waiting tools, non-shell tools); None for a shell command."""
    if tool in ("TaskOutput", "BashOutput"):
        return "wait", "background_task"
    if tool == "Monitor":
        return "wait", (wait_target(command) if command else "background_task")
    if tool != "Bash" and not command:
        return _TOOL_CLASS.get(tool, "other"), None
    return None


def classify(tool: str, command: Optional[str] = None) -> tuple[str, Optional[str]]:
    decided = tool_only_class(tool, command)
    if decided:
        return decided
    command = command or ""
    stripped = strip_text(command)
    if _WAIT.search(stripped):
        return "wait", wait_target(command)
    return next(((name, None) for name, rx in _SHELL if rx.search(stripped)), ("shell-other", None))
