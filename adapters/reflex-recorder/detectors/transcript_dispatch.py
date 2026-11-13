"""detectors/transcript_dispatch.py -- recover Agent/Task dispatch fields from a
session transcript, keyed by tool_use_id.

Since the live capture stopped carrying the dispatch prompt and the child
`agentId` (claude-hook-fast activity events became metadata-only), the session
transcript is the remaining source for both. The parent transcript holds, per
dispatch:

  * an assistant `tool_use` block (name Agent/Task) whose `input` has the full
    `prompt`, `isolation`, `model`, `name`, `description`, and
  * a user `tool_result` block with the same `tool_use_id`, whose line carries a
    dict `toolUseResult` containing `agentId` (and, for async launches, `prompt`).

The same tool_use_id can appear on several lines (streamed copies, request
blobs); entries are merged, preferring the dict-valued result.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

_DISPATCH_NAMES = ("Agent", "Task")
_KEEP_INPUT = ("prompt", "isolation", "model", "name", "description")


def _merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if v and not dst.get(k):
            dst[k] = v


def parse_transcript_dispatches(lines) -> dict[str, dict]:
    """tool_use_id -> {prompt, isolation, model, name, description, agentId}."""
    out: dict[str, dict] = {}
    for line in lines:
        # Cheap prefilter before json.loads: these files are megabytes.
        if '"tool_use"' not in line and '"agentId"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") not in ("assistant", "user"):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        result = obj.get("toolUseResult")
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use" and block.get("name") in _DISPATCH_NAMES:
                tid = block.get("id")
                inp = block.get("input")
                if isinstance(tid, str) and isinstance(inp, dict):
                    _merge(out.setdefault(tid, {}),
                           {k: inp[k] for k in _KEEP_INPUT if isinstance(inp.get(k), str)})
            elif kind == "tool_result" and isinstance(result, dict):
                tid = block.get("tool_use_id")
                aid = result.get("agentId")
                if isinstance(tid, str) and isinstance(aid, str) and aid:
                    rec = {"agentId": aid}
                    if isinstance(result.get("prompt"), str):
                        rec["prompt"] = result["prompt"]
                    _merge(out.setdefault(tid, {}), rec)
    return out


@lru_cache(maxsize=32)
def _index(path: str, mtime_ns: int, size: int) -> dict[str, dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_transcript_dispatches(fh)


def transcript_dispatches(path: Optional[Path]) -> dict[str, dict]:
    """Cached per-file index; {} when the file is missing or unreadable."""
    if path is None:
        return {}
    try:
        st = Path(path).stat()
        return _index(str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}


def lookup(transcript_path: str, session_id: str, tool_use_id: str,
           roots: Optional[list[Path]] = None) -> dict:
    """Recovered fields for one dispatch, or {} (no transcript / unknown id)."""
    if not (transcript_path and tool_use_id):
        return {}
    try:
        from transcript_steps import resolve_transcript  # adapter-root module
    except ImportError:
        return {}
    found = resolve_transcript(transcript_path, session_id=session_id, roots=roots)
    return transcript_dispatches(found).get(tool_use_id, {})
