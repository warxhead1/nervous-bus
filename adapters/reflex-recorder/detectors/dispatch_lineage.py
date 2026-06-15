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

# ── Truncation-tolerant field extraction ────────────────────────────────────────
#
# tool_summary / tool_response_summary are BOUNDED (Bash 200c, dispatch tools
# 1000c). When the underlying JSON is longer than the bound it is cut mid-string,
# so json.loads (what _parse_summary does) FAILS and returns {} — silently losing
# fields that are textually present. The fields we join on (agentId, model, name)
# appear BEFORE the long prompt, so they survive truncation as raw text. These
# helpers recover them: try the parsed dict first, then regex the raw string.

def _summary_field(raw: str, key: str) -> str:
    """Extract a string field from a possibly-truncated summary JSON string."""
    if not raw:
        return ""
    parsed = _parse_summary(raw)
    val = parsed.get(key)
    if isinstance(val, str) and val:
        return val
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    return m.group(1) if m else ""


def _summary_contains(raw: str, *needles: str) -> bool:
    """True if any needle appears in the raw summary string (truncation-proof)."""
    if not raw:
        return False
    low = raw.lower()
    return any(n.lower() in low for n in needles)


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
        raw_ts = ev.get("tool_summary", "") or ""
        raw_rs = ev.get("tool_response_summary", "") or ""
        # Truncation-tolerant: these fields precede the long prompt, so regex
        # recovers them even when the bounded JSON is cut and json.loads fails.
        out.append(
            Dispatch(
                seq=ev.get("seq", 0),
                ts=ev.get("ts", "") or "",
                tool_name=ev.get("tool_name", ""),
                child_agent_id=_summary_field(raw_rs, "agentId"),
                prompt=_summary_field(raw_ts, "prompt"),
                model=_summary_field(raw_ts, "model"),
                name=_summary_field(raw_ts, "name"),
                isolation=_summary_field(raw_ts, "isolation"),
                description=(_summary_field(raw_ts, "description")
                            or _summary_field(raw_rs, "description")),
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


# ── Verification predicate (project-extensible) ──────────────────────────────────
#
# "Did the agent run a verification?" is project-specific: tengine verifies via
# silo_tester / shadergen check-shader / gpu_verify_lock.sh / tsdl_validate — none
# of which the generic BUILD_KEYWORDS recognize. Detectors inject a project-aware
# predicate (built from the adapter taxonomy, see detectors/verification.py) so the
# substrate stays pure and adapter-free. The DEFAULT is the generic build/test
# floor — passing nothing reproduces the old behavior, keeping existing tests valid.
#
# Signature: is_verify(cmd: str, project: str) -> bool.

VerifyPredicate = "callable[[str, str], bool]"


def default_verify(cmd: str, project: str = "") -> bool:
    """Generic build/test floor — project is ignored."""
    return _is_build_command(cmd)


# Code file extensions: editing one of these is what creates a verification
# obligation. Edits confined to non-code artifacts (docs, planning, beads) do not.
_CODE_EXTS = {
    ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".cu", ".cuh",
    ".slang", ".wgsl", ".glsl", ".comp", ".frag", ".vert", ".hlsl",
    ".go", ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rb",
    ".tsdl", ".toml", ".json", ".yaml", ".yml", ".sh", ".build", "build.rs",
}
# Clearly-non-code path markers (docs / planning / task-tracking / logs).
_NONCODE_DIR_MARKERS = ("/.planning/", "/.beads/", "/knowledge/", "/docs/")
_NONCODE_EXTS = {".md", ".txt", ".rst", ".log", ".csv", ".lock"}


def is_code_path(path: str) -> bool:
    """Whether editing this path creates a verification obligation.

    Conservative: an unrecoverable/empty path is treated as code (we'd rather ask
    'did you verify?' than silently exempt). Only clearly-non-code paths (docs,
    .planning, .beads, .md/.txt/...) return False.
    """
    p = (path or "").lower()
    if not p:
        return True  # unknown (e.g. truncated) — assume code, stay conservative
    if any(m in p for m in _NONCODE_DIR_MARKERS):
        return False
    base = p.rsplit("/", 1)[-1]
    ext = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    if ext in _NONCODE_EXTS:
        return False
    if ext in _CODE_EXTS:
        return True
    return True  # default conservative


def last_test_signal_before(
    events: list[dict],
    seq: int,
    max_lookback: int = 200,
    is_verify=default_verify,
) -> BaselineSignal:
    """Scan backward from `seq` for the most recent build/test invocation.

    Returns a BaselineSignal classifying that invocation's outcome. Only events
    strictly before `seq` are considered (the baseline as of the dispatch). We
    look back at most `max_lookback` tool calls — far enough to cross a normal
    edit/verify stretch, bounded so an enormous run doesn't get O(n) re-scanned
    per dispatch. `is_verify(cmd, project)` decides what counts as a build/test
    (project-aware; default = generic floor).
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
        cmd = _summary_field(ev.get("tool_summary", ""), "command")
        if not is_verify(cmd, ev.get("project", "")):
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


def session_run_ids(conn) -> dict[str, list[str]]:
    """Map session_id -> [run_id, ...] from the runs table.

    The segmenter splits one host session into several idle-bounded runs, so a
    fan-out dispatch and the child worktree activity it spawned usually land in
    DIFFERENT runs of the same session. Lineage joins (cohort -> child outcome)
    must therefore pool a session's runs, not look within one run. Runs with a
    NULL session_id are skipped (nothing to pool them by).
    """
    out: dict[str, list[str]] = {}
    for session_id, run_id in conn.execute(
        "SELECT session_id, run_id FROM runs WHERE session_id IS NOT NULL ORDER BY started"
    ).fetchall():
        out.setdefault(session_id, []).append(run_id)
    return out


def load_session_events(conn, run_ids: list[str]) -> list[dict]:
    """Pool every run's events for one session, ordered globally by time.

    Per-run `seq` resets to 1 in each idle-split run, so it cannot order a pooled
    session. We order by event_ts (the recorder's wall-clock) and reassign a
    monotonic synthetic `seq` so downstream cohort grouping / backscans see one
    coherent timeline.
    """
    if not run_ids:
        return []
    placeholders = ",".join("?" * len(run_ids))
    cur = conn.execute(
        f"SELECT event_ts, raw_json FROM run_events "
        f"WHERE run_id IN ({placeholders}) ORDER BY event_ts, seq",
        run_ids,
    )
    out: list[dict] = []
    for i, (event_ts, raw_json) in enumerate(cur.fetchall()):
        try:
            raw = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError):
            continue
        data = raw.get("data", {})
        out.append({
            "seq": i,
            "tool_name": data.get("tool_name", ""),
            "cwd": data.get("cwd", ""),
            "tool_summary": data.get("tool_summary", ""),
            "tool_response_summary": data.get("tool_response_summary", ""),
            "project": data.get("project", ""),
            "ts": data.get("ts", "") or raw.get("time", "") or event_ts,
        })
    return out


def has_code_edit(events: list[dict]) -> bool:
    """True if the run made any code modification (Edit/Write tool call)."""
    return any(e.get("tool_name") in ("Edit", "Write") for e in events)


def ran_build_or_test(events: list[dict], is_verify=default_verify) -> bool:
    """True if the run invoked any build/test command at least once."""
    for e in events:
        if e.get("tool_name") != "Bash":
            continue
        cmd = _summary_field(e.get("tool_summary", ""), "command")
        if is_verify(cmd, e.get("project", "")):
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
    """One delegated agent's slice of a parent run, keyed by worktree slug.

    edit_count counts ALL Edit/Write calls; code_edit_count counts only edits to
    code files (is_code_path) — the ones that create a verification obligation.
    An agent that edited only docs/.planning has code_edit_count == 0.
    """
    slug: str
    events: list[dict] = field(default_factory=list)
    edit_count: int = 0
    code_edit_count: int = 0
    build_test_count: int = 0

    @property
    def child_agent_id(self) -> str:
        # `agent-<hex>` -> `<hex>` (matches Dispatch.child_agent_id); other slugs
        # (e.g. `wf_<...>`) are returned verbatim.
        return self.slug[len("agent-"):] if self.slug.startswith("agent-") else self.slug


def segment_by_worktree(events: list[dict], is_verify=default_verify) -> dict[str, WorktreeSegment]:
    """Group a run's events into per-worktree-slug delegated-agent segments.

    Events not in any worktree (the orchestrator's own main-tree work) are
    omitted — this function returns only the delegated-agent segments.
    `is_verify(cmd, project)` decides what counts as a build/test (project-aware).
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
            if is_code_path(_summary_field(e.get("tool_summary", ""), "file_path")):
                seg.code_edit_count += 1
        elif tn == "Bash":
            cmd = _summary_field(e.get("tool_summary", ""), "command")
            if is_verify(cmd, e.get("project", "")):
                seg.build_test_count += 1
    return segments


# ── Per-subagent outcome derivation ─────────────────────────────────────────────
#
# The recorder emits no per-subagent close or outcome (delegated agents are folded
# into the parent host run; only host sessions close, always on idle_timeout). The
# proper fix is a SubagentStop emission (bead nervous-bus-jvzkn.8). Until then we
# DERIVE a per-subagent outcome from the segment's own trajectory — grounded,
# signal-based, and honest: we report what the agent DID (verified / left red /
# unverified / committed), not a success/fail verdict (which needs the parent's
# acceptance check we don't have).

# Bash command fragments indicating the agent committed its work.
_COMMIT_KEYWORDS = ("git commit", "git push")


@dataclass
class SubagentOutcome:
    """Derived outcome signals for one delegated agent (worktree segment)."""
    slug: str
    child_agent_id: str
    project: str
    edit_count: int
    build_test_count: int
    last_test_status: str        # "passed" | "failed" | "none"
    committed: bool
    outcome_class: str           # see _classify below
    event_count: int
    code_edit_count: int = 0

    @property
    def is_clean_finish(self) -> bool:
        """A finish we'd trust: edited, last test passed, work committed."""
        return self.outcome_class == "verified" and self.committed


def _last_test_status_in_segment(events: list[dict], is_verify=default_verify) -> str:
    """Status of the LAST build/test in a segment: passed/failed/none."""
    for e in reversed(events):
        if e.get("tool_name") != "Bash":
            continue
        cmd = _summary_field(e.get("tool_summary", ""), "command")
        if not is_verify(cmd, e.get("project", "")):
            continue
        rs = _parse_summary(e.get("tool_response_summary", ""))
        if _has_fail_output(rs.get("stdout", "") or "", rs.get("stderr", "") or ""):
            return "failed"
        return "passed"
    return "none"


def _segment_committed(events: list[dict]) -> bool:
    for e in events:
        if e.get("tool_name") != "Bash":
            continue
        # Truncation-tolerant: a long `git commit -m "..."` truncates the bounded
        # tool_summary JSON, so match the substring on the raw string.
        if _summary_contains(e.get("tool_summary", ""), *_COMMIT_KEYWORDS):
            return True
    return False


def _classify(code_edit_count: int, edit_count: int, last_test_status: str) -> str:
    """Map signals to a derived outcome class.

    readonly   — made no edits at all (investigation / read-only agent)
    docs       — edited only non-code artifacts (docs/.planning) — no test owed
    left_red   — edited code and its LAST build/test was failing (shipped red)
    unverified — edited code and ran NO build/test at all (the F1 case)
    verified   — edited code and its last build/test passed
    """
    if edit_count == 0:
        return "readonly"
    if code_edit_count == 0:
        return "docs"
    if last_test_status == "failed":
        return "left_red"
    if last_test_status == "none":
        return "unverified"
    return "verified"


def derive_subagent_outcomes(
    events: list[dict],
    project: str = "",
    is_verify=default_verify,
) -> dict[str, SubagentOutcome]:
    """Derive a per-delegated-agent outcome record for every worktree segment.

    Built on segment_by_worktree; adds last-test status, commit signal, and a
    derived outcome class. `project` falls back to each event's own project tag.
    `is_verify(cmd, project)` is the project-aware build/test predicate — pass the
    adapter-built verifier so a project's bespoke harness (tengine: silo_tester,
    shadergen, tsdl_validate, ...) counts as verification.
    """
    out: dict[str, SubagentOutcome] = {}
    for slug, seg in segment_by_worktree(events, is_verify=is_verify).items():
        proj = project or (seg.events[0].get("project", "") if seg.events else "")
        last_status = _last_test_status_in_segment(seg.events, is_verify=is_verify)
        committed = _segment_committed(seg.events)
        out[slug] = SubagentOutcome(
            slug=slug,
            child_agent_id=seg.child_agent_id,
            project=proj,
            edit_count=seg.edit_count,
            code_edit_count=seg.code_edit_count,
            build_test_count=seg.build_test_count,
            last_test_status=last_status,
            committed=committed,
            outcome_class=_classify(seg.code_edit_count, seg.edit_count, last_status),
            event_count=len(seg.events),
        )
    return out


# ── Cohort → child join ─────────────────────────────────────────────────────────

@dataclass
class CohortChild:
    """One dispatch joined to the delegated agent it spawned (if recoverable)."""
    dispatch: Dispatch
    outcome: Optional[SubagentOutcome]   # None if no worktree segment matched

    @property
    def matched(self) -> bool:
        return self.outcome is not None


def join_cohort_to_children(
    cohort: list[Dispatch],
    outcomes: dict[str, SubagentOutcome],
) -> list[CohortChild]:
    """Join a fan-out cohort to its children via child_agent_id == segment slug.

    `outcomes` is the dict from derive_subagent_outcomes (keyed by slug). The
    match is dispatch.child_agent_id == outcome.child_agent_id (the slug stripped
    of its `agent-` prefix). A dispatch whose child ran in the main tree (no
    worktree) or whose response summary was truncated before agentId yields a
    CohortChild with outcome=None — surfaced, not silently dropped.
    """
    by_child = {o.child_agent_id: o for o in outcomes.values()}
    joined: list[CohortChild] = []
    for d in cohort:
        joined.append(CohortChild(dispatch=d, outcome=by_child.get(d.child_agent_id)))
    return joined
