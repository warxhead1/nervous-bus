"""Claude Code: one JSONL per session under <config>/projects/<mangled cwd>/.

Sub-agent transcripts live in <session>/subagents/agent-*.jsonl next to the
parent file; they are sessions of their own (most Bash calls happen there).
"""
from __future__ import annotations

import glob
import json
import os
import re

from model import SessionRef, Source, ToolCall, env_path, extra_roots, home, unique

# Text that means the program never ran or its output is gone (hook/permission
# denial, block, interrupt). Mirrors argshape's harness_failure().
_HARNESS = re.compile(r"^(?:PreToolUse:\w+ hook error|The user doesn't want to proceed|"
                      r"Permission to use \w+ with|Permission for this command|<tool_use_error>|"
                      r"\[Tool call interrupted|Hook PreToolUse)")
_LOST = re.compile(r"Exit code \d+\n(?:Command output was lost|\[Tool call interrupted)")
_EXIT = re.compile(r"Exit code (\d+)")


class Claude(Source):
    name = "claude"
    label = "Claude Code"
    live_hooks = "settings.json hooks: PreToolUse / PostToolUse / PostToolUseFailure"

    def roots(self) -> list[str]:
        return unique([os.path.join(env_path("CLAUDE_CONFIG_DIR", home(), ".claude"), "projects")] + extra_roots(self.name))

    def sessions(self, since: float = 0.0):
        for root in self.roots():
            for pat in ("*/*.jsonl", "*/*/subagents/*.jsonl"):
                for path in glob.glob(os.path.join(root, pat)):
                    try:
                        mt = os.path.getmtime(path)
                    except OSError:
                        continue
                    if mt >= since:
                        sid = os.path.splitext(os.path.basename(path))[0]
                        yield SessionRef(self.name, sid, path, mt,
                                         extra={"subagent": "/subagents/" in path.replace(os.sep, "/")})

    def tool_calls(self, ref: SessionRef):
        pend: dict[str, ToolCall] = {}
        for line in open(ref.path, errors="ignore"):
            if '"tool_' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    shell = b.get("name") == "Bash"
                    pend[b.get("id", "")] = ToolCall(
                        self.name, e.get("sessionId") or ref.id, b.get("id", ""), b.get("name", ""), inp,
                        ts=e.get("timestamp", ""), cwd=e.get("cwd", ""), shell=shell,
                        command=inp.get("command", "") if shell else "")
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pend:
                    tc = pend.pop(b["tool_use_id"])
                    txt = b.get("content")
                    if isinstance(txt, list):
                        txt = "\n".join(x.get("text", "") for x in txt if isinstance(x, dict))
                    tc.output = txt if isinstance(txt, str) else json.dumps(txt)
                    tc.is_error = bool(b.get("is_error"))
                    if tc.is_error:
                        m = _EXIT.match(tc.output)
                        tc.exit_code = int(m.group(1)) if m else None
                        tc.harness = bool(_HARNESS.match(tc.output) or _LOST.match(tc.output) or
                                          (tc.shell and not m))
                    elif tc.shell:
                        tc.exit_code = 0
                    yield tc
        yield from pend.values()          # calls whose result never landed (session cut off)
