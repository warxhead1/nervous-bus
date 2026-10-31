"""Cursor CLI: ~/.cursor/projects/<cwd slug>/agent-transcripts/<id>/<id>.jsonl.

Claude-shaped lines: `{role, message: {content: [{type: text|tool_use, ...}]}}`.
The shell tool is `Shell` with input {command, description}. The transcript
records no tool results, timestamps or exit codes, so output / is_error /
exit_code stay None. A line `{type: turn_ended, status: error}` is a turn
marker, not a call. store.db is not read.
"""
from __future__ import annotations

import glob
import json
import os

from model import SessionRef, Source, ToolCall, env_path, extra_roots, home, unique

SHELL_TOOLS = {"Shell", "Bash", "run_terminal_cmd"}


class Cursor(Source):
    name = "cursor"
    label = "Cursor CLI"
    live_hooks = "~/.cursor/hooks.json: preToolUse / postToolUse / afterShellExecution"

    def roots(self) -> list[str]:
        base = env_path("CURSOR_CONFIG_DIR", home(), ".cursor")
        return unique([os.path.join(base, "projects")] + extra_roots(self.name))

    def sessions(self, since: float = 0.0):
        for root in self.roots():
            for path in glob.glob(os.path.join(root, "*", "agent-transcripts", "*", "*.jsonl")):
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if mt >= since:
                    slug = os.path.relpath(path, root).split(os.sep)[0]
                    yield SessionRef(self.name, os.path.splitext(os.path.basename(path))[0], path, mt,
                                     extra={"project": slug})

    def tool_calls(self, ref: SessionRef):
        n = 0
        with open(ref.path, errors="ignore") as f:
            for line in f:
                if '"tool_use"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                content = (e.get("message") or {}).get("content") if isinstance(e, dict) else None
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    n += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    shell = b.get("name") in SHELL_TOOLS
                    yield ToolCall(self.name, ref.id, b.get("id") or f"{ref.id}:{n}", b.get("name", ""), inp,
                                   ts=e.get("timestamp", ""), cwd=inp.get("working_directory", "") if shell else "",
                                   shell=shell, command=inp.get("command", "") if shell else "")
