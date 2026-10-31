"""Antigravity CLI: brain/<id>/.system_generated/logs/transcript[_full].jsonl.

One line per step: {step_index, source, type, status, created_at, content}.
A PLANNER_RESPONSE step carries `tool_calls[{name, args}]`; the steps that
follow it, up to the next PLANNER_RESPONSE, are those calls' results in the
same order (the result step is typed RUN_COMMAND for most shell calls and
GENERIC for the rest). The pairing is only trusted when the counts match.
Shell is `run_command` with args.CommandLine / args.Cwd. Result content reads
"The command completed successfully." or "The command exited with code N";
status RUNNING means the command went to a background task with no result yet.
transcript_full.jsonl is untruncated and preferred over transcript.jsonl.
"""
from __future__ import annotations

import glob
import json
import os
import re

from model import SessionRef, Source, ToolCall, env_path, extra_roots, home, unique

SHELL_TOOLS = {"run_command"}
_EXIT = re.compile(r"The command (?:exited with code|failed with exit code:?) (-?\d+)")
_OK = re.compile(r"The command completed successfully")


class Agy(Source):
    name = "agy"
    label = "Antigravity CLI"
    live_hooks = ""

    def roots(self) -> list[str]:
        base = env_path("ANTIGRAVITY_CLI_HOME", home(), ".gemini", "antigravity-cli")
        return unique([os.path.join(base, "brain")] + extra_roots(self.name))

    def sessions(self, since: float = 0.0):
        for root in self.roots():
            for d in glob.glob(os.path.join(root, "*", ".system_generated", "logs")):
                for fname in ("transcript_full.jsonl", "transcript.jsonl"):
                    path = os.path.join(d, fname)
                    if os.path.isfile(path):
                        break
                else:
                    continue
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if mt >= since:
                    sid = os.path.basename(os.path.dirname(os.path.dirname(d)))
                    yield SessionRef(self.name, sid, path, mt, extra={"full": fname == "transcript_full.jsonl"})

    def tool_calls(self, ref: SessionRef):
        rows = []
        with open(ref.path, errors="ignore") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    rows.append(e)
        i = 0
        while i < len(rows):
            r = rows[i]
            if r.get("type") != "PLANNER_RESPONSE":
                i += 1
                continue
            j = i + 1
            while j < len(rows) and rows[j].get("type") != "PLANNER_RESPONSE" and rows[j].get("source") == "MODEL":
                j += 1
            calls = [t for t in r.get("tool_calls") or [] if isinstance(t, dict)]
            results = rows[i + 1:j]
            paired = len(results) == len(calls)
            for k, t in enumerate(calls):
                args = t.get("args") if isinstance(t.get("args"), dict) else {}
                shell = t.get("name") in SHELL_TOOLS
                tc = ToolCall(self.name, ref.id, f"{r.get('step_index', i)}:{k}", t.get("name", ""), args,
                              ts=r.get("created_at", ""), cwd=args.get("Cwd", "") if shell else "",
                              shell=shell, command=args.get("CommandLine", "") if shell else "")
                if shell and paired:
                    _apply_result(tc, results[k])
                yield tc
            i = j


def _apply_result(tc: ToolCall, step: dict) -> None:
    content = step.get("content")
    if isinstance(content, str):
        tc.output = content
    status = step.get("status")
    text = tc.output or ""
    m = _EXIT.search(text)
    if m:
        tc.exit_code = int(m.group(1))
        tc.is_error = tc.exit_code != 0
    elif _OK.search(text):
        tc.exit_code, tc.is_error = 0, False
    elif status in ("ERROR", "CANCELED"):
        tc.is_error = True
        tc.harness = True
    elif status == "RUNNING":
        tc.output = None           # background task: no result yet
