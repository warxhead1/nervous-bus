"""detectors/dispatch_lineage.py — shared substrate for the orchestration-quality
detector family (A/B/C/D/F in the reflexarc orchestration-detector spec).

This is NOT a detector — it has no Detector class and is never auto-registered.
It is the join + baseline-snapshot machinery that the dispatch-quality detectors
(red_baseline_dispatch, unverified_completion, and the cohort/F-family to come)
build on. Three primitives:

1. parse_dispatches(events)
   Extract the unified dispatch record from a parent run's activity stream. A
   "dispatch" is an Agent/Task tool_call: its tool_summary carries the bounded
   prompt + model + name + isolation; its tool_response_summary carries the
   CHILD agent id (`agentId`). That child id is the lineage key — it matches the
   `worktree-agent-<hex>` branch and the child run's own agent_id, so a later
   cohort-join can correlate parent dispatch → child outcome.

2. group_cohorts(dispatches, window_s)
   A "fan-out cohort" is a burst of dispatches launched close together in time
   (the orchestrator spreading work across N agents at once). Detectors that
   reason about a fan-out (over-provisioning, inherited rationalization, shared
   baseline) operate per-cohort, not per-child — one flag per fan-out.

3. last_test_signal_before(events, seq)
   The A-family precondition: reconstruct the parent's test/build state at the
   moment of fan-out by scanning backward from the dispatch for the most recent
   build/test Bash invocation and classifying its captured output as
   passed / failed / unknown. This is the "baseline snapshot" the spec calls for,
   derived from the activity stream rather than a separate probe.

Build/test classification (BUILD_KEYWORDS / FAIL_PATTERNS) is imported from
edit_build_fail_revert to stay DRY — the two detectors must agree on what
"a test ran and failed" means.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from detectors.edit_build_fail_revert import (
    BUILD_KEYWORDS,
    FAIL_PATTERNS,
    _parse_summary,
)

# Tool names that spawn a child agent (the dispatch boundary). Workflow-tool
# agents are reconstructed separately by workflow_dispatch.py and fold into the
# same record shape downstream; here we key on the in-stream Agent/Task calls.
DISPATCH_TOOLS = ("Agent", "Task")

# Default temporal window for grouping dispatches into one fan-out cohort.
# Empirically, orchestrator fan-outs land within a few seconds of each other
# (the model emits the parallel tool_calls in one turn); 90s is generous slack
# for serialized emission + hook latency without merging distinct waves.
DEFAULT_COHORT_WINDOW_S = 90.0


# ── Unified dispatch record ─────────────────────────────────────────────────────

@dataclass
class Dispatch:
    """One Agent/Task dispatch extracted from a parent run's activity stream.

    child_agent_id is the lineage key (tool_response_summary.agentId) linking
    this dispatch to the child run/worktree it spawned. It may be empty if the
    response summary was truncated before the agentId field.
    """
    seq: int
    ts: str
    tool_name: str
    child_agent_id: str
    prompt: str
    model: str
    name: str
    isolation: str
    description: str
    cohort_id: int = -1


def _epoch(ts: str) -> Optional[float]:
    """Parse an RFC3339/ISO timestamp to epoch seconds, or None.

    Python 3.11's fromisoformat handles the trailing 'Z' and 9-digit fractional
    seconds our producers emit. A naive timestamp (no offset) is assumed UTC.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_dispatches(events: list[dict]) -> list[Dispatch]:
    """Extract all Agent/Task dispatches from a run's event dicts.

    `events` is the per-run list produced by load_run_events() (below) or any
    equivalent list of dicts with keys: seq, tool_name, tool_summary,
    tool_response_summary, ts.
    """
    out: list[Dispatch] = []
    for ev in events:
        if ev.get("tool_name") not in DISPATCH_TOOLS:
            continue
        ts = _parse_summary(ev.get("tool_summary", ""))
        rs = _parse_summary(ev.get("tool_response_summary", ""))
        out.append(
            Dispatch(
                seq=ev.get("seq", 0),
                ts=ev.get("ts", "") or "",
                tool_name=ev.get("tool_name", ""),
                child_agent_id=str(rs.get("agentId", "") or ""),
                prompt=str(ts.get("prompt", "") or ""),
                model=str(ts.get("model", "") or ""),
                name=str(ts.get("name", "") or ""),
                isolation=str(ts.get("isolation", "") or ""),
                description=str(ts.get("description", "") or rs.get("description", "") or ""),
            )
        )
    return out


def group_cohorts(
    dispatches: list[Dispatch],
    window_s: float = DEFAULT_COHORT_WINDOW_S,
) -> list[list[Dispatch]]:
    """Cluster dispatches into fan-out cohorts by temporal proximity.

    Dispatches are bucketed greedily: a new cohort starts whenever the gap from
    the previous dispatch exceeds window_s (or timestamps are unavailable, in
    which case we fall back to seq adjacency). Mutates each Dispatch.cohort_id.
    Returns the list of cohorts (each a list of Dispatch), in dispatch order.
    """
    if not dispatches:
        return []
    ordered = sorted(dispatches, key=lambda d: d.seq)
    cohorts: list[list[Dispatch]] = []
    current: list[Dispatch] = []
    prev_epoch: Optional[float] = None
    prev_seq: Optional[int] = None

    for d in ordered:
        e = _epoch(d.ts)
        start_new = False
        if not current:
            start_new = False
        elif e is not None and prev_epoch is not None:
            start_new = (e - prev_epoch) > window_s
        elif prev_seq is not None:
            # No usable timestamps — treat a seq jump of >8 tool calls as a
            # separate wave (a fan-out emits its dispatches back-to-back).
            start_new = (d.seq - prev_seq) > 8
        if start_new and current:
            cohorts.append(current)
            current = []
        current.append(d)
        prev_epoch = e if e is not None else prev_epoch
        prev_seq = d.seq

    if current:
        cohorts.append(current)

    for cid, cohort in enumerate(cohorts):
        for d in cohort:
            d.cohort_id = cid
    return cohorts


# ── Baseline-snapshot derivation ────────────────────────────────────────────────

@dataclass
class BaselineSignal:
    """The parent's test/build state immediately before a dispatch.

    status:
      "failed"  — a build/test ran and its captured output matched a fail pattern
      "passed"  — a build/test ran and showed no fail pattern in captured output
      "unknown" — a build/test ran but output was truncated/empty (can't classify)
      "absent"  — no build/test invocation found before the dispatch
    """
    status: str
    command: str = ""
    seq: int = -1
    events_back: int = 0


def _has_fail_output(stdout: str, stderr: str) -> bool:
    combined = (stdout + "\n" + stderr).lower()
    return any(p.lower() in combined for p in FAIL_PATTERNS)


def _is_build_command(cmd: str) -> bool:
    lower = cmd.lower()
    return any(kw in lower for kw in BUILD_KEYWORDS)


def last_test_signal_before(
    events: list[dict],
    seq: int,
    max_lookback: int = 200,
) -> BaselineSignal:
    """Scan backward from `seq` for the most recent build/test invocation.

    Returns a BaselineSignal classifying that invocation's outcome. Only events
    strictly before `seq` are considered (the baseline as of the dispatch). We
    look back at most `max_lookback` tool calls — far enough to cross a normal
    edit/verify stretch, bounded so an enormous run doesn't get O(n) re-scanned
    per dispatch.
    """
    # events are assumed ordered by seq ascending.
    before = [e for e in events if e.get("seq", 0) < seq]
    scanned = 0
    for ev in reversed(before):
        if scanned >= max_lookback:
            break
        scanned += 1
        if ev.get("tool_name") != "Bash":
            continue
        ts = _parse_summary(ev.get("tool_summary", ""))
        cmd = ts.get("command", "") or ""
        if not _is_build_command(cmd):
            continue
        rs = _parse_summary(ev.get("tool_response_summary", ""))
        stdout = rs.get("stdout", "") or ""
        stderr = rs.get("stderr", "") or ""
        if _has_fail_output(stdout, stderr):
            status = "failed"
        elif stdout or stderr:
            status = "passed"
        else:
            status = "unknown"
        return BaselineSignal(
            status=status,
            command=cmd[:200],
            seq=ev.get("seq", -1),
            events_back=scanned,
        )
    return BaselineSignal(status="absent")


# ── run_events loader (shared by detectors) ─────────────────────────────────────

def load_run_events(conn, run_id: str) -> list[dict]:
    """Load one run's activity events as lightweight dicts, ordered by seq.

    Centralizes the raw_json → flat-dict shaping so each detector doesn't
    re-implement it. Malformed rows are skipped.
    """
    cur = conn.execute(
        "SELECT seq, raw_json FROM run_events WHERE run_id = ? ORDER BY seq",
        (run_id,),
    )
    out: list[dict] = []
    for seq, raw_json in cur.fetchall():
        try:
            raw = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError):
            continue
        data = raw.get("data", {})
        out.append({
            "seq": seq,
            "tool_name": data.get("tool_name", ""),
            "cwd": data.get("cwd", ""),
            "tool_summary": data.get("tool_summary", ""),
            "tool_response_summary": data.get("tool_response_summary", ""),
            "project": data.get("project", ""),
            "ts": data.get("ts", "") or raw.get("time", ""),
        })
    return out


def has_code_edit(events: list[dict]) -> bool:
    """True if the run made any code modification (Edit/Write tool call)."""
    return any(e.get("tool_name") in ("Edit", "Write") for e in events)


def ran_build_or_test(events: list[dict]) -> bool:
    """True if the run invoked any build/test command at least once."""
    for e in events:
        if e.get("tool_name") != "Bash":
            continue
        ts = _parse_summary(e.get("tool_summary", ""))
        if _is_build_command(ts.get("command", "") or ""):
            return True
    return False


# ── Worktree-segment lineage ────────────────────────────────────────────────────
#
# Subagent work is NOT recorded as a separate run — the recorder attributes every
# delegated agent's tool calls to the PARENT host session, discriminated only by
# the worktree cwd (`.../.claude/worktrees/<slug>` or `.../.worktrees/<slug>`).
# The slug is the child agent id (`agent-<hex>` matches a dispatch's
# child_agent_id; `wf_<...>` is a workflow agent). So a delegated agent's
# trajectory is the run's events filtered to its worktree cwd — the right unit for
# "did this delegated agent verify its own work?" questions.

_WORKTREE_RE = re.compile(r"(?:\.claude/worktrees|\.worktrees)/([^/]+)")


def worktree_slug_of(cwd: str) -> Optional[str]:
    """Return the worktree slug for a cwd, or None if cwd is not in a worktree."""
    if not cwd:
        return None
    m = _WORKTREE_RE.search(cwd)
    return m.group(1) if m else None


@dataclass
class WorktreeSegment:
    """One delegated agent's slice of a parent run, keyed by worktree slug."""
    slug: str
    events: list[dict] = field(default_factory=list)
    edit_count: int = 0
    build_test_count: int = 0

    @property
    def child_agent_id(self) -> str:
        # `agent-<hex>` -> `<hex>` (matches Dispatch.child_agent_id); other slugs
        # (e.g. `wf_<...>`) are returned verbatim.
        return self.slug[len("agent-"):] if self.slug.startswith("agent-") else self.slug


def segment_by_worktree(events: list[dict]) -> dict[str, WorktreeSegment]:
    """Group a run's events into per-worktree-slug delegated-agent segments.

    Events not in any worktree (the orchestrator's own main-tree work) are
    omitted — this function returns only the delegated-agent segments.
    """
    segments: dict[str, WorktreeSegment] = {}
    for e in events:
        slug = worktree_slug_of(e.get("cwd", ""))
        if not slug:
            continue
        seg = segments.get(slug)
        if seg is None:
            seg = WorktreeSegment(slug=slug)
            segments[slug] = seg
        seg.events.append(e)
        tn = e.get("tool_name")
        if tn in ("Edit", "Write"):
            seg.edit_count += 1
        elif tn == "Bash":
            ts = _parse_summary(e.get("tool_summary", ""))
            if _is_build_command(ts.get("command", "") or ""):
                seg.build_test_count += 1
    return segments
