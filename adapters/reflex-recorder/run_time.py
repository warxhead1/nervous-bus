"""run_time.py — where a run's wall time went: model vs tool, by command class.

Answers "what is taking each agent the longest" from the transcript (the bus
has no per-call durations). Per run:

  wall   first to last transcript record inside the run window
  tool   UNION of tool-call intervals [tool_use, tool_result]; parallel calls in
         one assistant message overlap and are counted once
  model  wall - tool (thinking, generation, and API latency between calls)

Tool time is also split by class. Bash commands go through the project's
CommandTaxonomy (adapter_api.taxonomy_for), so a project overlay decides what
counts as `build` vs `run-verify` vs its own classes. Other tools map to fixed
classes (Read/Grep/Glob -> explore, Edit/Write -> edit, Agent/Task -> dispatch).
Per-class sums are NOT unioned (they can exceed `tool` when calls overlap).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from transcript_steps import RunRef, Step, StepCache, WINDOW_SLACK_S, run_refs

_TOOL_CLASS = {
    "Read": "explore", "Grep": "explore", "Glob": "explore", "LS": "explore",
    "WebFetch": "explore", "WebSearch": "explore", "ToolSearch": "explore",
    "Edit": "edit", "Write": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
    "Agent": "dispatch", "Task": "dispatch",
    "TaskOutput": "wait", "Monitor": "wait",
}


def classify_step(step: Step, taxonomy) -> str:
    if step.tool == "Bash":
        try:
            return taxonomy.classify(step.command)
        except Exception:
            return "act"
    return _TOOL_CLASS.get(step.tool, "other")


def _union(intervals: list[tuple[float, float]]) -> float:
    total, cur_lo, cur_hi = 0.0, None, None
    for lo, hi in sorted(intervals):
        if cur_hi is None or lo > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    if cur_hi is not None:
        total += cur_hi - cur_lo
    return total


def _short(cmd: str, n: int = 140) -> str:
    one = " ".join(cmd.split())
    return one if len(one) <= n else one[: n - 3] + "..."


def breakdown(steps: list[Step], taxonomy, top_k: int = 5,
              bounds: Optional[tuple[float, float]] = None) -> dict:
    """Time breakdown for one run's steps. `bounds` = (first, last) record ts."""
    if not steps:
        return {"wall_s": 0.0, "tool_s": 0.0, "model_s": 0.0, "calls": 0,
                "by_class": {}, "top": []}
    ivals = [(s.t0, s.t1 if s.t1 is not None else s.t0) for s in steps]
    lo = min(i[0] for i in ivals)
    hi = max(i[1] for i in ivals)
    if bounds:
        lo, hi = min(lo, bounds[0]), max(hi, bounds[1])
    wall = max(0.0, hi - lo)
    tool = min(wall, _union(ivals))
    by_class: dict[str, dict] = {}
    for s in steps:
        c = classify_step(s, taxonomy)
        e = by_class.setdefault(c, {"calls": 0, "seconds": 0.0})
        e["calls"] += 1
        e["seconds"] += s.duration
    top = sorted(steps, key=lambda s: s.duration, reverse=True)[:top_k]
    return {
        "wall_s": round(wall, 1),
        "tool_s": round(tool, 1),
        "model_s": round(wall - tool, 1),
        "calls": len(steps),
        "by_class": {k: {"calls": v["calls"], "seconds": round(v["seconds"], 1)}
                     for k, v in sorted(by_class.items(), key=lambda kv: -kv[1]["seconds"])},
        "top": [{"seconds": round(s.duration, 1), "class": classify_step(s, taxonomy),
                 "tool": s.tool,
                 "what": _short(s.command) if s.tool == "Bash" else (s.file_path or s.tool)}
                for s in top],
    }


def run_times(conn: sqlite3.Connection, project: Optional[str] = None,
              since_ts: Optional[str] = None, top_k: int = 5,
              min_events: int = 1, roots=None, adapters=None) -> list[dict]:
    """One breakdown per run that resolves to a transcript (others are listed
    with `transcript: null` so a capture gap is visible, never silently dropped)."""
    from adapter_api import load_adapters, taxonomy_for
    adapters = adapters if adapters is not None else load_adapters()
    cache = StepCache()
    out: list[dict] = []
    for ref in run_refs(conn, project=project, since_ts=since_ts,
                        min_events=min_events, roots=roots):
        row = {"run_id": ref.run_id, "project": ref.project, "agent": ref.agent_label,
               "transcript": str(ref.transcript) if ref.transcript else None}
        steps = cache.steps_for(ref)
        bounds = None
        if ref.started is not None and ref.ended is not None and steps:
            bounds = (min(ref.started, steps[0].t0), ref.ended)
        row.update(breakdown(steps, taxonomy_for(ref.project, adapters), top_k, bounds))
        out.append(row)
    return out


def transcript_time(path, taxonomy, top_k: int = 5) -> dict:
    """Breakdown for a whole transcript file (no run-store needed)."""
    from transcript_steps import parse_transcript
    return breakdown(parse_transcript(path), taxonomy, top_k)


__all__ = ["breakdown", "classify_step", "run_times", "transcript_time", "RunRef",
           "WINDOW_SLACK_S"]
