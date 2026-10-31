"""Codex CLI: rollout JSONL per session under $CODEX_HOME/sessions/YYYY/MM/DD/.

Shell calls appear in two generations:
- newer: event_msg/item_completed with item.type == CommandExecution, which
  carries command, cwd, exit_code and output already normalized;
- older: response_item function_call (shell, shell_command, exec_command,
  local_shell) paired with function_call_output by call_id, exit code in the
  output text ("Exit code: N") or its JSON metadata.
A file that has CommandExecution items is read from those alone, so a call is
never counted twice.
"""
from __future__ import annotations

import glob
import json
import os
import re

from model import SessionRef, Source, ToolCall, env_path, extra_roots, home, shell_text, unique

SHELL_TOOLS = {"shell", "shell_command", "exec_command", "local_shell", "container.exec"}
_EXIT = re.compile(r"(?:Exit code|Process exited with code):? (\d+)")


class Codex(Source):
    name = "codex"
    label = "Codex CLI"
    live_hooks = "hooks.json (PreToolUse / PostToolUse)"

    def roots(self) -> list[str]:
        base = env_path("CODEX_HOME", home(), ".codex")
        roots = [os.path.join(base, "sessions"), os.path.join(base, "archived_sessions")]
        # Orca keeps one CODEX_HOME per account.
        roots += sorted(glob.glob(os.path.join(home(), ".config", "orca", "codex-accounts", "*", "home", "sessions")))
        return unique(roots + extra_roots(self.name))

    def sessions(self, since: float = 0.0):
        seen = set()
        for root in self.roots():
            for path in glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True):
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if mt < since:
                    continue
                sid, cwd = _meta(path)
                if sid in seen:
                    continue
                seen.add(sid)
                yield SessionRef(self.name, sid, path, mt, cwd=cwd)

    def tool_calls(self, ref: SessionRef):
        with open(ref.path, errors="ignore") as f:
            lines = f.readlines()
        modern = any("CommandExecution" in ln for ln in lines)
        pend: dict[str, ToolCall] = {}
        cwd = ref.cwd
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get("payload") or {}
            t = p.get("type")
            ts = e.get("timestamp", "")
            if e.get("type") == "turn_context":
                cwd = p.get("cwd") or cwd
            elif modern and t == "item_completed" and (p.get("item") or {}).get("type") == "CommandExecution":
                it = p["item"]
                rc = it.get("exit_code")
                out = it.get("aggregated_output")
                if out is None:
                    out = (it.get("stdout") or "") + (it.get("stderr") or "")
                yield ToolCall(self.name, ref.id, it.get("id", ""), "exec_command", {"command": it.get("command")},
                               ts=ts, cwd=it.get("cwd") or cwd, shell=True, command=shell_text(it.get("command")),
                               output=out, exit_code=rc, is_error=None if rc is None else rc != 0,
                               harness=it.get("status") in ("declined", "denied"))
            elif not modern and t == "function_call":
                try:
                    args = json.loads(p.get("arguments") or "{}")
                except ValueError:
                    args = {}
                name = p.get("name", "")
                shell = name in SHELL_TOOLS
                cmd = shell_text(args.get("command", args.get("cmd", ""))) if shell else ""
                pend[p.get("call_id", "")] = ToolCall(self.name, ref.id, p.get("call_id", ""), name, args, ts=ts,
                                                      cwd=args.get("workdir") or cwd, shell=shell, command=cmd)
            elif not modern and t == "function_call_output" and p.get("call_id") in pend:
                tc = pend.pop(p["call_id"])
                out = p.get("output")
                rc = None
                if isinstance(out, str) and out.startswith("{"):
                    try:
                        o = json.loads(out)
                        rc = (o.get("metadata") or {}).get("exit_code")
                        out = o.get("output", out)
                    except ValueError:
                        pass
                tc.output = out if isinstance(out, str) else json.dumps(out)
                if rc is None:
                    m = _EXIT.search(tc.output[:200])
                    rc = int(m.group(1)) if m else None
                tc.exit_code = rc
                tc.is_error = None if rc is None else rc != 0
                yield tc
        yield from pend.values()


def _meta(path: str) -> tuple[str, str]:
    try:
        with open(path, errors="ignore") as f:
            e = json.loads(f.readline())
        p = e.get("payload") or {}
        if e.get("type") == "session_meta":
            return p.get("id") or p.get("session_id") or path, p.get("cwd", "")
    except (OSError, ValueError):
        pass
    return os.path.splitext(os.path.basename(path))[0], ""
