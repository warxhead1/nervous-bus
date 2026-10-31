"""Gemini CLI: chat recordings under <gemini home>/tmp/<project>/chats/.

Two generations sit side by side:
- legacy: one JSON document per session (`session-*.json`) with `messages[]`;
- current: append-only `session-*.jsonl` -- a header line, one line per message,
  and bare `{"$set": {...}}` / `{lastUpdated}` metadata lines. A message id that
  repeats replaces the earlier line.
Each `gemini` message carries `toolCalls[]` {id, name, args, result, status}. The
result is `[{functionResponse: {response: {output|error}}}]`. Shell calls are
`run_shell_command`; the output text holds an `Exit Code: N` line when the
program exited nonzero or reported a code. Subagent sessions sit in a subfolder
of chats/ named for the parent session.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re

from model import SessionRef, Source, ToolCall, env_path, extra_roots, home, unique

SHELL_TOOLS = {"run_shell_command"}
_EXIT = re.compile(r"^Exit Code: (-?\d+)", re.M)
_HARNESS = re.compile(r"^(?:\[Operation Cancelled\]|Tool execution denied|Tool execution for|Command rejected)")


class Gemini(Source):
    name = "gemini"
    label = "Gemini CLI"
    live_hooks = "settings.json hooks: BeforeTool / AfterTool"

    def roots(self) -> list[str]:
        roots = [os.path.join(home(), ".gemini", "tmp")]
        if os.environ.get("GEMINI_CLI_HOME"):
            roots.insert(0, os.path.join(env_path("GEMINI_CLI_HOME", home()), ".gemini", "tmp"))
        return unique(roots + extra_roots(self.name))

    def sessions(self, since: float = 0.0):
        seen = set()
        for root in self.roots():
            cwds = _cwd_map(root)
            for ext in ("json", "jsonl"):
                pat = os.path.join(root, "*", "chats", "**", f"session-*.{ext}")
                for path in glob.glob(pat, recursive=True):
                    try:
                        mt = os.path.getmtime(path)
                    except OSError:
                        continue
                    if mt < since or path in seen:
                        continue
                    seen.add(path)
                    proj = os.path.relpath(path, root).split(os.sep)[0]
                    sid = os.path.splitext(os.path.basename(path))[0]
                    yield SessionRef(self.name, sid, path, mt, cwd=cwds.get(proj, ""), extra={"project": proj})

    def tool_calls(self, ref: SessionRef):
        for msg in _messages(ref.path):
            if msg.get("type") != "gemini":
                continue
            for t in msg.get("toolCalls") or []:
                if not isinstance(t, dict):
                    continue
                args = t.get("args") if isinstance(t.get("args"), dict) else {}
                name = t.get("name", "")
                shell = name in SHELL_TOOLS
                tc = ToolCall(self.name, ref.id, t.get("id", ""), name, args,
                              ts=t.get("timestamp") or msg.get("timestamp", ""),
                              cwd=args.get("dir_path") or ref.cwd if shell else ref.cwd,
                              shell=shell, command=args.get("command", "") if shell else "")
                out = _result_text(t.get("result"))
                status = t.get("status")
                if out is not None:
                    tc.output = out
                if status in ("error", "cancelled"):
                    tc.is_error = True
                    tc.harness = bool(out and _HARNESS.match(out)) or (status == "cancelled" and out is None)
                elif status == "success":
                    tc.is_error = False
                if shell and out is not None:
                    m = _EXIT.search(out)
                    if m:
                        tc.exit_code = int(m.group(1))
                        if tc.exit_code != 0:
                            tc.is_error = True
                yield tc


def _messages(path: str):
    """Message dicts in file order, a repeated id replaced in place by its later line."""
    try:
        f = open(path, errors="ignore")
    except OSError:
        return
    with f:
        if path.endswith(".jsonl"):
            order: list = []
            by_id: dict = {}
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or "type" not in e or "id" not in e:
                    continue          # header and $set / lastUpdated lines
                if e["id"] not in by_id:
                    order.append(e["id"])
                by_id[e["id"]] = e
            for i in order:
                yield by_id[i]
        else:
            try:
                doc = json.load(f)
            except ValueError:
                return
            for m in (doc.get("messages") if isinstance(doc, dict) else None) or []:
                if isinstance(m, dict):
                    yield m


def _result_text(result):
    if not isinstance(result, list):
        return None
    parts = []
    for r in result:
        resp = ((r or {}).get("functionResponse") or {}).get("response")
        if isinstance(resp, dict):
            v = resp.get("output", resp.get("error"))
            if v is not None:
                parts.append(v if isinstance(v, str) else json.dumps(v))
    return "\n".join(parts) if parts else None


def _cwd_map(root: str) -> dict:
    """tmp/<dir> -> project path, from projects.json (path -> short name) and the sha256 dirs."""
    out: dict = {}
    try:
        with open(os.path.join(os.path.dirname(root), "projects.json"), errors="ignore") as f:
            projects = (json.load(f) or {}).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return out
    for path, short in projects.items():
        if isinstance(short, str):
            out[short] = path
        out[hashlib.sha256(path.encode("utf-8", "ignore")).hexdigest()] = path
    return out
