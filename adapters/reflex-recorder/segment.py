"""segment.py — Run segmentation logic for the reflex-recorder.

Implements the HARDENED segmentation model from the 2026-06-13 opus audit:

  run_key_kind=session  → conversation_id (no worktree)
  run_key_kind=worktree → conversation_id + '#' + worktree_slug

One conversation_id was observed spanning 26 parallel worktrees (all shards
inherit the parent host session ULID into agent_id/session_id/conversation_id).
Therefore conversation_id alone is NOT a valid run key.

Worktree absolute path reconstruction:
  activity `cwd` is absolute and points into .claude/worktrees/<slug>/...
  We locate the worktrees/ sentinel and truncate at <worktrees_root>/<slug>.
  The raw `worktree` field in activity events is only the bare slug.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple


# ── ULID (same approach as pattern-bundler, no external dep) ─────────────────

def _ulid() -> str:
    ts = int(time.time() * 1000)
    rnd = os.urandom(10).hex().upper()[:16]
    return f"{ts:013d}{rnd}"


# ── Worktree path reconstruction ─────────────────────────────────────────────

_WORKTREES_SENTINEL = ".claude/worktrees/"


def extract_worktree_slug(activity: dict) -> Optional[str]:
    """Extract the worktree slug from an activity event.

    The activity `worktree` field is the raw slug (e.g. 'agent-a273f362...').
    The activity `cwd` is an absolute path like
      /home/eric/projects/foo/.claude/worktrees/<slug>/path/inside
    We prefer the worktree field when present; fall back to parsing cwd.
    Returns None if the event has no worktree context.
    """
    # Primary: explicit worktree slug field
    slug = activity.get("worktree")
    if slug and isinstance(slug, str) and slug.strip():
        return slug.strip()

    # Fallback: parse from cwd
    cwd = activity.get("cwd", "")
    if not cwd:
        return None
    idx = cwd.find(_WORKTREES_SENTINEL)
    if idx == -1:
        return None
    after = cwd[idx + len(_WORKTREES_SENTINEL):]
    if not after:
        return None
    # slug is everything up to the next /
    slug = after.split("/")[0]
    return slug if slug else None


def reconstruct_worktree_path(activity: dict, slug: str) -> Optional[str]:
    """Derive the ABSOLUTE worktree root path from cwd + slug.

    cwd example: /home/eric/projects/foo/.claude/worktrees/agent-abc/subdir
    result:      /home/eric/projects/foo/.claude/worktrees/agent-abc

    Returns None if we can't locate the sentinel in cwd.
    """
    cwd = activity.get("cwd", "")
    if not cwd:
        return None
    idx = cwd.find(_WORKTREES_SENTINEL)
    if idx == -1:
        return None
    worktree_root = cwd[: idx + len(_WORKTREES_SENTINEL)] + slug
    return worktree_root


# ── Run key computation ───────────────────────────────────────────────────────

def compute_run_key(activity: dict) -> Tuple[str, str, Optional[str]]:
    """Return (run_key, run_key_kind, worktree_slug).

    run_key_kind is 'session' or 'worktree'.
    worktree_slug is the raw slug string (or None for session runs).
    """
    slug = extract_worktree_slug(activity)
    conv_id = activity.get("conversation_id") or activity.get("session_id", "")

    if slug:
        run_key = f"{conv_id}#{slug}"
        return run_key, "worktree", slug
    else:
        return conv_id, "session", None


# ── In-memory run state ───────────────────────────────────────────────────────

@dataclass
class OpenRun:
    """Mutable accumulator for a run that is still open."""
    run_id: str
    run_key: str
    run_key_kind: str
    project: str
    agent_kind: str
    host_conversation_id: str
    session_id: str
    agent_id: str
    started: str           # RFC3339 UTC of first event
    last_event_ts: float   # Unix timestamp of last folded event (for idle timeout)
    event_count: int = 0
    tool_histogram: Dict[str, int] = field(default_factory=dict)
    worktree_slug: Optional[str] = None
    worktree: Optional[str] = None       # reconstructed absolute path
    git_branch: Optional[str] = None
    continues_run_id: Optional[str] = None  # set if re-opened after idle_timeout

    def fold(self, activity: dict, now: float) -> None:
        """Incorporate one activity event into this run."""
        self.event_count += 1
        self.last_event_ts = now

        # Update git_branch if available (take the first non-null)
        if not self.git_branch and activity.get("git_branch"):
            self.git_branch = activity["git_branch"]

        # tool_histogram: only on tool_call events with a tool_name
        if activity.get("event") == "tool_call" and activity.get("tool_name"):
            tool = activity["tool_name"]
            self.tool_histogram[tool] = self.tool_histogram.get(tool, 0) + 1

    def to_closed_payload(self, ended: str, close_reason: str) -> dict:
        """Build the bus.agent.run.closed.v1 data payload."""
        return {
            "run_id": self.run_id,
            "run_key": self.run_key,
            "run_key_kind": self.run_key_kind,
            "host_conversation_id": self.host_conversation_id,
            "project": self.project,
            "agent_kind": self.agent_kind,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "started": self.started,
            "ended": ended,
            "close_reason": close_reason,
            "continues_run_id": self.continues_run_id,
            "event_count": self.event_count,
            "tool_histogram": dict(self.tool_histogram),
            "worktree": self.worktree,
            "worktree_slug": self.worktree_slug,
            "git_branch": self.git_branch,
            "bead_id": None,
            "outcome": None,
            "labeled_at": None,
            "label_version": None,
            "label_history": [],
            "features": {},
            "schema_version": "1",
        }


# ── Segmenter ─────────────────────────────────────────────────────────────────

class Segmenter:
    """In-memory run segmenter.

    Maintains open runs keyed by run_key.  On each activity event:
    - Open a new run if none exists for this run_key.
    - Fold the event into the existing open run.
    - If the event is `ended`, close the run.
    - On tick(), close any runs that have been idle longer than idle_timeout_s.
    - On shutdown(), close all open runs with close_reason=recorder_shutdown.

    The caller receives closed run payloads via a callback.
    """

    def __init__(
        self,
        idle_timeout_s: float = 900.0,
        on_run_closed=None,
    ):
        self.idle_timeout_s = idle_timeout_s
        self.on_run_closed = on_run_closed  # callable(payload: dict)
        self._open: Dict[str, OpenRun] = {}
        # Map run_key → most-recently-closed run_id (for continues_run_id)
        self._last_closed_id: Dict[str, str] = {}

    def ingest(self, activity: dict, now: Optional[float] = None) -> None:
        """Ingest one bus.agent.activity.v1 data payload."""
        if now is None:
            now = time.time()

        run_key, run_key_kind, slug = compute_run_key(activity)
        if not run_key:
            return  # can't segment without a run key

        event_ts = _ts_to_str(activity.get("ts") or activity.get("time") or "")

        if run_key not in self._open:
            # Open a new run, possibly continuing a prior idle-closed one
            continues = self._last_closed_id.get(run_key)
            wt_path = reconstruct_worktree_path(activity, slug) if slug else None
            run = OpenRun(
                run_id=_ulid(),
                run_key=run_key,
                run_key_kind=run_key_kind,
                project=activity.get("project", "unknown"),
                agent_kind=activity.get("agent_kind", "host_claude_code"),
                host_conversation_id=activity.get("conversation_id", ""),
                session_id=activity.get("session_id", ""),
                agent_id=activity.get("agent_id", ""),
                started=event_ts or _now_utc(now),
                last_event_ts=now,
                worktree_slug=slug,
                worktree=wt_path,
                continues_run_id=continues,
            )
            self._open[run_key] = run

        run = self._open[run_key]
        run.fold(activity, now)

        if activity.get("event") == "ended":
            self._close_run(run_key, ended=event_ts or _now_utc(now), reason="ended")

    def tick(self, now: Optional[float] = None) -> None:
        """Check for idle runs and close them. Call periodically."""
        if now is None:
            now = time.time()
        deadline = now - self.idle_timeout_s
        to_close = [
            rk for rk, run in self._open.items()
            if run.last_event_ts < deadline
        ]
        for rk in to_close:
            run = self._open[rk]
            self._close_run(rk, ended=_now_utc(run.last_event_ts), reason="idle_timeout")

    def shutdown(self) -> None:
        """Close all open runs with recorder_shutdown reason."""
        for rk in list(self._open.keys()):
            run = self._open[rk]
            self._close_run(rk, ended=_now_utc(run.last_event_ts), reason="recorder_shutdown")

    def _close_run(self, run_key: str, ended: str, reason: str) -> None:
        run = self._open.pop(run_key, None)
        if run is None:
            return
        payload = run.to_closed_payload(ended=ended, close_reason=reason)
        self._last_closed_id[run_key] = run.run_id
        if self.on_run_closed:
            self.on_run_closed(payload)

    @property
    def open_run_count(self) -> int:
        return len(self._open)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc(ts: Optional[float] = None) -> str:
    """Return current time (or ts) as RFC3339 UTC string."""
    import datetime
    t = datetime.datetime.utcfromtimestamp(ts) if ts is not None else datetime.datetime.utcnow()
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_to_str(ts_val: str) -> str:
    """Normalise an activity ts field to an RFC3339 UTC string (strip sub-seconds)."""
    if not ts_val:
        return ""
    # Truncate at 'Z' suffix — strip trailing fractional seconds if present
    # e.g. "2026-06-13T05:09:49.544739415Z" → "2026-06-13T05:09:49Z"
    if "." in ts_val and ts_val.endswith("Z"):
        base = ts_val.split(".")[0]
        return base + "Z"
    return ts_val
