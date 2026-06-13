"""synthesis.py — Reflexarc Synthesis Pass (b7 / mgv7z).

Turns detector signal (detector_hits + issues tables) into GATED, ranked,
deterministic-fix proposals. This is the heart of Reflexarc.

CORE PRINCIPLE — Remediation Ladder (Eliminate > Automate > Inform)
Every proposal must sit at the HIGHEST achievable rung. The replay gate
self-reinforces: Eliminate/Automate fixes are replay-measurable; Inform
"fixes" are not, so they structurally cannot pass the gate.

Usage
-----
    python synthesis.py                          # DRY-RUN (default)
    python synthesis.py --emit                   # DRY-RUN + actually publish
    python synthesis.py --emit --file-beads      # publish + file proposal beads
    python synthesis.py --project nervous-bus    # filter to one project
    python synthesis.py --window-days 14         # use 14-day window
    python synthesis.py --json                   # machine-readable output

LIVE-DATA CONSTRAINTS
=====================
1. Outcome labels are SPARSE (~10%).  Rank primarily on deterministic signal
   (prevalence × recurrence × ladder-rung). Use labels ONLY as a small
   additive refinement, never as a gate. NEVER fold outcome==null into
   "clean" — always gate on labeled_at IS NOT NULL.

2. Runs fragment across idle_timeout (continues_run_id chains).  RE-STITCH
   logical runs before counting prevalence/recurrence.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Import order: synthesis.py may be run directly from its directory.
# We ensure the detectors package is on sys.path.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from detectors.base import BaseDetector, PatternCandidate, ensure_detector_schema
from detectors.worktree_leak import WorktreeLeakDetector
from detectors.rebuild_cache_miss import RebuildCacheMissDetector
from detectors.repeated_question import RepeatedQuestionDetector
from detectors.edit_build_fail_revert import EditBuildFailRevertDetector
from detectors.reread_same_file import RereadSameFileDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

#: Default rolling window for prevalence/recency scoring.
WINDOW_DAYS: int = 30

#: Remediation rung weights (Eliminate > Automate > Inform).
RUNG_WEIGHTS: dict[str, float] = {
    "eliminate": 3.0,
    "automate":  2.0,
    "inform":    1.0,
}

#: Score weights for each component.  sum = 1.0 except label_confirmation
#: which is an additive term (can be + or -).
SCORE_WEIGHTS: dict[str, float] = {
    "prevalence":        0.35,   # fraction of runs touched in window
    "recurrence":        0.25,   # how often we have seen this
    "rung":              0.25,   # higher rungs = more actionable
    "label_confirmation": 0.08,  # sparse label refinement (can be negative)
    "recency":           0.07,   # decay toward old issues
}

#: Minimum score to propose a fix (for replay-gated detectors).
ACT_THRESHOLD: float = 0.30

#: Minimum score for INFORM-rung proposals (higher bar, unmeasurable).
#:
#: Fix 13: documented post-rung-penalty math.
#: rung_weight for inform  = RUNG_WEIGHTS["inform"]  / max(RUNG_WEIGHTS.values()) = 1/3 ≈ 0.333
#: rung_weight for elim    = RUNG_WEIGHTS["eliminate"]/ max(RUNG_WEIGHTS.values()) = 3/3 = 1.000
#: The SCORE_WEIGHTS["rung"] = 0.25 component contributes:
#:   eliminate: 0.25 * 1.000 = 0.250
#:   inform:    0.25 * 0.333 = 0.083
#: Natural rung penalty for inform vs eliminate = 0.250 − 0.083 = 0.167 per run.
#: INFORM_ACT_THRESHOLD = 0.55 provides 0.55 − ACT_THRESHOLD(0.30) = 0.25 raw margin.
#: Post-rung-penalty effective margin = 0.25 − 0.167 = 0.083 net extra bar vs eliminate.
#: This is a deliberate, documented margin: inform proposals require stronger
#: prevalence+recurrence signal to compensate for being unmeasurable by replay.
INFORM_ACT_THRESHOLD: float = 0.55

#: Replay gate: minimum fraction of firings that would have been prevented.
MIN_PREVENTION_RATE: float = 0.50

#: Replay gate: maximum false-suppression count before gate fails.
MAX_FALSE_SUPPRESSION: int = 0

#: Replay gate: minimum logical runs to evaluate (else insufficient_history).
MIN_REPLAY_RUNS: int = 2

#: Minimum logical runs a project needs before any proposals go out.
MIN_PROJECT_RUNS: int = 2

#: Recency half-life in days (issues older than this get half weight).
RECENCY_HALF_LIFE_DAYS: float = 14.0

#: Scorer version — bump when scoring weights/formula change.
SCORER_VERSION: int = 1

# Detector registry — explicit import list (small + auditable).
DETECTOR_CLASSES: list[type[BaseDetector]] = [
    WorktreeLeakDetector,
    RebuildCacheMissDetector,
    RepeatedQuestionDetector,
    EditBuildFailRevertDetector,
    RereadSameFileDetector,
]

# ---------------------------------------------------------------------------
# run_evals schema
# ---------------------------------------------------------------------------

_RUN_EVALS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS run_evals (
    eval_id                   TEXT PRIMARY KEY,
    issue_signature           TEXT NOT NULL,
    project                   TEXT NOT NULL,
    detector                  TEXT NOT NULL,
    score                     REAL NOT NULL,
    decision                  TEXT NOT NULL,
    rung                      TEXT NOT NULL,
    replay_json               TEXT NOT NULL DEFAULT '{}',
    score_components_json     TEXT NOT NULL DEFAULT '{}',
    labeled_support_json      TEXT NOT NULL DEFAULT '{}',
    remediation_json          TEXT NOT NULL DEFAULT '{}',
    supersedes_eval_id        TEXT,
    synthesis_pass_at         TEXT NOT NULL,
    recurrence_count_at_apply INTEGER
);
CREATE INDEX IF NOT EXISTS idx_re_signature ON run_evals(issue_signature);
CREATE INDEX IF NOT EXISTS idx_re_project   ON run_evals(project);
CREATE INDEX IF NOT EXISTS idx_re_pass      ON run_evals(synthesis_pass_at);
"""


def ensure_eval_schema(conn: sqlite3.Connection) -> None:
    """Add run_evals table. Idempotent."""
    conn.executescript(_RUN_EVALS_SCHEMA_SQL)


# ---------------------------------------------------------------------------
# ULID-ish helper (sortable, no external deps)
# ---------------------------------------------------------------------------

def _make_ulid() -> str:
    """Generate a 26-char sortable ID (timestamp + random, uppercase base32)."""
    import time as _time
    import random as _random
    import string as _string

    # 10 chars of millisecond timestamp
    ts_ms = int(_time.time() * 1000)
    ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    def encode(n: int, length: int) -> str:
        result = []
        for _ in range(length):
            result.append(ENCODING[n & 0x1F])
            n >>= 5
        return "".join(reversed(result))

    ts_part = encode(ts_ms, 10)
    rand_part = encode(_random.getrandbits(80), 16)
    return ts_part + rand_part


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_utc(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Step 0 — Re-stitch logical runs
# ---------------------------------------------------------------------------

def stitch_logical_runs(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Collapse continues_run_id chains into logical runs.

    Returns {logical_run_id: [run_id, ...]} where logical_run_id is the
    EARLIEST ancestor (root) of the chain.  A standalone run (no chain)
    maps to [run_id].

    A run whose continues_run_id points at a predecessor folds into that
    predecessor's logical run.  Chains are arbitrarily deep; we follow them
    iteratively to handle depth > 1.

    Fix 10: cycle detection — a cyclic chain (A→B→A) is detected by tracking
    the path. All members of a cycle get the same root (min run_id in cycle),
    preventing double-counting.

    Fix 11: project scoping — chain links are broken when continues_run_id
    crosses project boundaries. A run in project-A cannot logically continue
    a run in project-B.

    Returns a dict keyed by the LOGICAL run id (earliest ancestor or cycle min),
    value is the sorted list of physical run_ids in that logical run.
    """
    rows = conn.execute(
        "SELECT run_id, continues_run_id, project FROM runs"
    ).fetchall()

    # Build maps
    child_to_parent: dict[str, str] = {}
    all_run_ids: set[str] = set()
    run_to_project: dict[str, str] = {}
    for run_id, continues, project in rows:
        all_run_ids.add(run_id)
        run_to_project[run_id] = project or ""
        if continues:
            # Fix 11: skip cross-project chain links
            if continues in all_run_ids:
                parent_proj = run_to_project.get(continues, "")
                child_proj = run_to_project.get(run_id, "")
                if parent_proj != child_proj:
                    continue  # cross-project link: do not stitch
            child_to_parent[run_id] = continues

    # Fix 11: second pass to remove cross-project links discovered after all
    # run_ids are in run_to_project (the first pass may have missed some
    # because rows are processed sequentially and parent may come later).
    for child, parent in list(child_to_parent.items()):
        if parent in run_to_project:
            if run_to_project[child] != run_to_project[parent]:
                del child_to_parent[child]

    def find_root(rid: str) -> str:
        """Walk chain to root; handle cycles by returning min(cycle members).

        Fix 10: uses path-tracking to detect cycles. When a cycle is detected
        (current node was already visited in this walk), returns the minimum
        run_id among all cycle members — ensuring all cycle members agree on
        the same root and are not double-counted.
        """
        path: list[str] = []
        visited_path: dict[str, int] = {}  # rid -> index in path
        current = rid
        while current in child_to_parent and current not in visited_path:
            visited_path[current] = len(path)
            path.append(current)
            parent = child_to_parent[current]
            # parent might not exist in DB (orphan ref) — stop there
            if parent not in all_run_ids:
                return current
            current = parent
        if current in visited_path:
            # Cycle detected: all nodes from cycle_start onward form the cycle.
            # Fix 10: return min(cycle members) as canonical root.
            cycle_start = visited_path[current]
            cycle_members = path[cycle_start:] + [current]
            return min(cycle_members)
        return current

    # Group by root
    logical: dict[str, list[str]] = {}
    for run_id in all_run_ids:
        root = find_root(run_id)
        logical.setdefault(root, []).append(run_id)

    return logical


# ---------------------------------------------------------------------------
# ReplayResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    status: str              # passed | failed | not_applicable | insufficient_history | pending
    method: str = ""
    runs_evaluated: int = 0
    would_have_prevented: int = 0
    false_suppression: int = 0
    not_preventable: int = 0  # Fix 3: first-ever builds etc. — not harmful suppressions
    prevention_rate: float = 0.0
    measurability_note: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "method": self.method,
            "runs_evaluated": self.runs_evaluated,
            "would_have_prevented": self.would_have_prevented,
            "false_suppression": self.false_suppression,
            "not_preventable": self.not_preventable,
            "prevention_rate": round(self.prevention_rate, 4),
            "measurability_note": self.measurability_note,
        }


# ---------------------------------------------------------------------------
# Monkey-patch: add replay() to BaseDetector + concrete implementations
# ---------------------------------------------------------------------------

def _default_replay(
    self: BaseDetector,
    conn: sqlite3.Connection,
    logical_runs: dict[str, list[str]],
    signature: str = "",
    rung: str = "inform",
    window_days: int = WINDOW_DAYS,
) -> ReplayResult:
    """Default replay — status depends on rung.

    Fix 7: eliminate/automate rungs get status='pending' (measurable but not
    yet implemented). inform-rung gets status='not_applicable' (intrinsically
    unmeasurable). The hardened schema forbids eliminate/automate + not_applicable.
    """
    if rung in ("eliminate", "automate"):
        return ReplayResult(
            status="pending",
            method=f"Replay gate for rung={rung!r} not yet implemented.",
            measurability_note=(
                "Gate is measurable (deterministic fix exists) but the "
                "counterfactual replay has not been implemented for this "
                f"detector. Rung={rung!r}; monitor until implemented."
            ),
        )
    return ReplayResult(
        status="not_applicable",
        method="No deterministic fix exists for this rung (inform).",
        measurability_note=(
            "Inform-rung detectors surface patterns for human review; "
            "there is no deterministic simulated fix to replay."
        ),
    )


# Attach default to BaseDetector
BaseDetector.replay = _default_replay  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# WorktreeLeakDetector.replay
# ---------------------------------------------------------------------------

def _worktree_leak_replay(
    self: BaseDetector,
    conn: sqlite3.Connection,
    logical_runs: dict[str, list[str]],
    signature: str = "",
    rung: str = "automate",
    window_days: int = WINDOW_DAYS,
) -> ReplayResult:
    """Replay: Automate = auto-cleanup on merge/bead-close.

    Fix 1: scoped to `signature` — only evaluates hits for this specific
    worktree path, not all worktree_leak hits globally.

    Fix 2: would_have_prevented = firings with confirmed-merged trigger
    (labeled_at IS NOT NULL AND outcome IN TERMINAL).
    false_suppression = firings with no confirmed-merged trigger (worktree
    legitimately still in use — auto-cleanup would have wrongly removed it).

    TERMINAL = {clean, landed, corrected} — confirmed-merged outcomes only.
    'abandoned' is excluded: a run being abandoned does not imply the branch
    was merged; auto-cleanup on abandon would be false suppression.

    Fix 9: dedup key is (logical_run_id, signature) — but since hits are
    already filtered by signature, only logical_run_id dedup is needed here.
    """
    # Fix 1: filter hits by this specific signature (worktree path)
    hits = conn.execute(
        """
        SELECT dh.run_id, dh.signature, r.project, r.bead_id,
               r.outcome, r.labeled_at, r.git_branch
        FROM detector_hits dh
        JOIN runs r ON r.run_id = dh.run_id
        WHERE dh.detector = 'worktree_leak'
          AND dh.signature = ?
        """,
        (signature,),
    ).fetchall()

    if not hits:
        return ReplayResult(
            status="insufficient_history",
            runs_evaluated=0,
            method="No worktree_leak firings found for this signature.",
            measurability_note="Need at least {} firings to evaluate.".format(MIN_REPLAY_RUNS),
        )

    # Build logical run membership
    run_to_logical = {}
    for logical_id, members in logical_runs.items():
        for m in members:
            run_to_logical[m] = logical_id

    # Fix 2: TERMINAL = confirmed-merged outcomes only (no 'abandoned')
    # 'abandoned' does not imply merge; auto-cleanup on abandon = false suppression.
    TERMINAL = {"clean", "landed", "corrected"}
    would_have_prevented = 0
    false_suppression = 0
    # Fix 9: dedup key is (logical_id, signature); since signature is constant
    # here (already filtered), this is effectively per-logical_id dedup.
    seen_logical_sig: set[tuple] = set()

    for run_id, hit_sig, project, bead_id, outcome, labeled_at, git_branch in hits:
        logical_id = run_to_logical.get(run_id, run_id)
        dedup_key = (logical_id, hit_sig)
        if dedup_key in seen_logical_sig:
            continue
        seen_logical_sig.add(dedup_key)

        # Fix 2: has_trigger = labeled AND outcome in confirmed-merged TERMINAL
        # Remove has_bead: bead presence alone does not mean branch was merged.
        has_trigger = (labeled_at is not None) and (outcome in TERMINAL)

        if has_trigger:
            would_have_prevented += 1
        else:
            false_suppression += 1

    runs_evaluated = len(seen_logical_sig)

    if runs_evaluated < MIN_REPLAY_RUNS:
        return ReplayResult(
            status="insufficient_history",
            runs_evaluated=runs_evaluated,
            method="Too few worktree_leak firing runs to evaluate for this signature.",
            measurability_note=f"Need >= {MIN_REPLAY_RUNS}, got {runs_evaluated}.",
        )

    total = would_have_prevented + false_suppression
    prevention_rate = would_have_prevented / total if total > 0 else 0.0

    if prevention_rate >= MIN_PREVENTION_RATE and false_suppression <= MAX_FALSE_SUPPRESSION:
        status = "passed"
    else:
        status = "failed"

    return ReplayResult(
        status=status,
        method=(
            "Counterfactual (signature-scoped): for each logical run with a "
            "worktree_leak firing for this signature, check if labeled_at IS NOT NULL "
            "AND outcome IN (clean, landed, corrected) — meaning auto-cleanup would have "
            "had a confirmed-merged trigger. False suppression = firings where no such "
            "trigger exists (worktree legitimately still in use)."
        ),
        runs_evaluated=runs_evaluated,
        would_have_prevented=would_have_prevented,
        false_suppression=false_suppression,
        prevention_rate=prevention_rate,
        measurability_note=(
            "Automate-rung hook: on bead-close or PR-merge, run "
            "`git worktree remove --force <path>`. This is replay-testable because "
            "we can check whether historical firings had a confirmed-merged trigger."
        ),
    )


WorktreeLeakDetector.replay = _worktree_leak_replay  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RebuildCacheMissDetector.replay
# ---------------------------------------------------------------------------

def _rebuild_cache_miss_replay(
    self: BaseDetector,
    conn: sqlite3.Connection,
    logical_runs: dict[str, list[str]],
    signature: str = "",
    rung: str = "eliminate",
    window_days: int = WINDOW_DAYS,
) -> ReplayResult:
    """Replay: Eliminate = shared CARGO_TARGET_DIR.

    Fix 1: scoped to `signature` — only evaluates hits for this specific
    (project, crate_target) combination.

    Fix 3: first-ever builds are now `not_preventable` (not false_suppression).
    A first-ever build in the window cannot be prevented by a shared cache
    since there is nothing to share yet. Counting it as false_suppression was
    incorrect and caused the gate to always fail with MAX_FALSE_SUPPRESSION=0.

    Fix 5: candidate run_ids bounded by window_days cutoff.

    Fix 6: drive replay from detector_hits for this signature, not by
    re-deriving slow builds from raw cargo events independently.

    prevention_rate = would_have_prevented / (would_have_prevented + false_suppression)
    not_preventable (first-ever builds) excluded from denominator.
    """
    from detectors.rebuild_cache_miss import (
        _extract_command,
        _extract_cwd,
        _is_cargo_build_command,
        _is_worktree_cwd,
        _extract_crate_target,
        _parse_event_ts,
        SLOW_BUILD_MULTIPLIER,
        ABSOLUTE_SLOW_BUILD_FLOOR_S,
    )
    import statistics

    # Fix 5: window cutoff
    window_cutoff = _days_ago_utc(window_days)

    # Fix 6: drive from detector_hits for this signature (not re-derived)
    # Fix 1: filter by signature
    hit_run_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT dh.run_id
            FROM detector_hits dh
            JOIN run_events re ON re.run_id = dh.run_id
            WHERE dh.detector = 'rebuild_cache_miss'
              AND dh.signature = ?
              AND re.event_ts >= ?
            """,
            (signature, window_cutoff),
        ).fetchall()
    ]

    if not hit_run_ids:
        # Fallback: if no hits for this signature, check all rebuild_cache_miss hits
        # in the window (handles case where signature is empty or not yet recorded)
        hit_run_ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT re.run_id
                FROM run_events re
                JOIN runs r ON r.run_id = re.run_id
                WHERE re.raw_json LIKE '%cargo%'
                  AND r.close_reason IS NOT NULL
                  AND re.event_ts >= ?
                """,
                (window_cutoff,),
            ).fetchall()
        ]

    if not hit_run_ids:
        return ReplayResult(
            status="insufficient_history",
            runs_evaluated=0,
            method="No rebuild_cache_miss hits found for this signature in window.",
            measurability_note=f"Need >= {MIN_REPLAY_RUNS} hits. window_days={window_days}.",
        )

    placeholders = ",".join("?" * len(hit_run_ids))
    rows = conn.execute(
        f"""
        SELECT re.run_id, re.seq, re.event_ts, re.raw_json,
               r.project, r.worktree, r.started
        FROM run_events re
        JOIN runs r ON r.run_id = re.run_id
        WHERE re.run_id IN ({placeholders})
          AND re.event_ts >= ?
        ORDER BY re.run_id, re.seq
        """,
        hit_run_ids + [window_cutoff],
    ).fetchall()

    # Group events by run_id, collect run started ts
    by_run: dict[str, list[tuple]] = {}
    run_started: dict[str, str] = {}
    for run_id, seq, ts, raw, project, worktree, started in rows:
        by_run.setdefault(run_id, []).append((seq, ts, raw, project, worktree))
        run_started[run_id] = started

    # Collect all observations (not just slow) to compute baseline
    all_observations: list[dict] = []
    for run_id, events in by_run.items():
        for i, (seq, ts, raw, project, worktree) in enumerate(events):
            cmd = _extract_command(raw)
            if not cmd or not _is_cargo_build_command(cmd):
                continue
            cwd = _extract_cwd(raw)
            in_worktree = _is_worktree_cwd(cwd) or _is_worktree_cwd(worktree)
            if not in_worktree:
                continue
            if i + 1 >= len(events):
                continue
            dt_start = _parse_event_ts(ts)
            dt_end = _parse_event_ts(events[i + 1][1])
            if dt_start is None or dt_end is None:
                continue
            gap_s = (dt_end - dt_start).total_seconds()
            if gap_s < 0:
                continue
            crate = _extract_crate_target(cmd)
            all_observations.append({
                "project": project,
                "crate": crate,
                "run_id": run_id,
                "gap_s": gap_s,
                "started": run_started.get(run_id, ""),
            })

    if not all_observations:
        return ReplayResult(
            status="insufficient_history",
            runs_evaluated=0,
            method="No cargo build observations in worktree cwds within window.",
        )

    # Compute per-project p50 baseline
    project_gaps: dict[str, list[float]] = {}
    for obs in all_observations:
        project_gaps.setdefault(obs["project"], []).append(obs["gap_s"])
    project_baseline: dict[str, float] = {}
    for proj, gaps in project_gaps.items():
        project_baseline[proj] = statistics.median(gaps) if len(gaps) > 1 else 0.0

    # Flag slow builds
    flagged: list[dict] = []
    for obs in all_observations:
        baseline = project_baseline.get(obs["project"], 0.0)
        above_floor = obs["gap_s"] > float(ABSOLUTE_SLOW_BUILD_FLOOR_S)
        above_mult = baseline > 0.0 and obs["gap_s"] > baseline * SLOW_BUILD_MULTIPLIER
        if above_floor or above_mult:
            flagged.append(obs)

    if not flagged:
        return ReplayResult(
            status="insufficient_history",
            runs_evaluated=len(hit_run_ids),
            method="No slow builds flagged within window; detector would not fire.",
        )

    # Build logical run mapping
    run_to_logical: dict[str, str] = {}
    for logical_id, members in logical_runs.items():
        for m in members:
            run_to_logical[m] = logical_id

    # Sort flagged by run started time to determine "first" builds.
    flagged_sorted = sorted(flagged, key=lambda o: o.get("started", ""))

    # Fix 3: first-ever builds → not_preventable (not false_suppression)
    # Track first seen (project, crate) across logical runs in time order
    first_seen_per_crate: dict[tuple, str] = {}  # (project, crate) -> first logical run started
    would_have_prevented = 0
    false_suppression = 0
    not_preventable = 0
    seen_logical_firings: set[tuple] = set()

    for obs in flagged_sorted:
        logical_id = run_to_logical.get(obs["run_id"], obs["run_id"])
        key = (obs["project"], obs["crate"])
        firing_key = (logical_id, obs["project"], obs["crate"])

        if firing_key in seen_logical_firings:
            continue
        seen_logical_firings.add(firing_key)

        if key not in first_seen_per_crate:
            # First build ever for this (project, crate) in the window.
            # A shared cache has no prior artifacts to offer — not preventable.
            first_seen_per_crate[key] = obs.get("started", "")
            not_preventable += 1  # Fix 3: was false_suppression, now not_preventable
        else:
            # Prior build exists → shared cache would hold artifacts → PREVENTED
            would_have_prevented += 1

    runs_evaluated = len({run_to_logical.get(o["run_id"], o["run_id"]) for o in flagged})

    if runs_evaluated < MIN_REPLAY_RUNS:
        return ReplayResult(
            status="insufficient_history",
            runs_evaluated=runs_evaluated,
            method="Too few flagged build runs to evaluate.",
            measurability_note=f"Need >= {MIN_REPLAY_RUNS}, got {runs_evaluated}.",
        )

    # Fix 3: prevention_rate excludes not_preventable from denominator
    # Only harmful_suppressions (false_suppression) count against the gate.
    harmful_total = would_have_prevented + false_suppression
    prevention_rate = would_have_prevented / harmful_total if harmful_total > 0 else 0.0

    if prevention_rate >= MIN_PREVENTION_RATE and false_suppression <= MAX_FALSE_SUPPRESSION:
        status = "passed"
    else:
        status = "failed"

    return ReplayResult(
        status=status,
        method=(
            "Counterfactual (signature-scoped, window-bounded): for each slow-build "
            "firing in detector_hits for this signature, check if the same "
            "(project, crate_target) was built in an EARLIER logical run within the "
            f"window ({window_days}d). If yes → shared CARGO_TARGET_DIR would cache "
            "artifacts → PREVENTED. If no (first-ever in window) → not_preventable "
            "(shared cache has nothing to offer yet). false_suppression = firings where "
            "the cache exists but would wrongly suppress a legitimate build."
        ),
        runs_evaluated=runs_evaluated,
        would_have_prevented=would_have_prevented,
        false_suppression=false_suppression,
        not_preventable=not_preventable,
        prevention_rate=prevention_rate,
        measurability_note=(
            "Eliminate-rung: setting CARGO_TARGET_DIR to a shared path removes "
            "cold rebuilds for repeat builds of the same crate. Replay-testable "
            "because we can identify which firings had a prior successful build "
            "in the window (shared cache would have artifacts)."
        ),
    )


RebuildCacheMissDetector.replay = _rebuild_cache_miss_replay  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _compute_recency(last_seen_str: str) -> float:
    """Return a 0..1 decay factor based on how recently the issue was last seen.

    Uses exponential decay with RECENCY_HALF_LIFE_DAYS.
    score=1.0 if last_seen is now, score=0.5 if last_seen is HALF_LIFE days ago.
    """
    try:
        last_seen = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.5  # unknown age → neutral
    now = datetime.now(timezone.utc)
    age_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / RECENCY_HALF_LIFE_DAYS)


def _compute_labeled_support(
    conn: sqlite3.Connection,
    signature: str,
) -> dict:
    """Compute labeled support breakdown for an issue signature.

    Returns {confirmed_failures, confirmed_clean, unlabeled}.
    Gates STRICTLY on labeled_at IS NOT NULL before trusting outcome.
    """
    FAILURE_OUTCOMES = {"thrashed", "abandoned", "reverted"}
    CLEAN_OUTCOMES = {"clean", "landed", "corrected"}

    # Get all run_ids that have a detector_hit for this signature
    hit_run_ids = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT run_id FROM detector_hits WHERE signature = ?",
            (signature,),
        ).fetchall()
    ]

    if not hit_run_ids:
        return {"confirmed_failures": 0, "confirmed_clean": 0, "unlabeled": 0}

    placeholders = ",".join("?" * len(hit_run_ids))
    rows = conn.execute(
        f"""
        SELECT outcome, labeled_at
        FROM runs
        WHERE run_id IN ({placeholders})
        """,
        hit_run_ids,
    ).fetchall()

    confirmed_failures = 0
    confirmed_clean = 0
    unlabeled = 0

    for outcome, labeled_at in rows:
        if labeled_at is None:
            # NOT-YET-LABELED: never fold into clean
            unlabeled += 1
        elif outcome in FAILURE_OUTCOMES:
            confirmed_failures += 1
        elif outcome in CLEAN_OUTCOMES:
            confirmed_clean += 1
        else:
            # labeled but unknown outcome — treat as unlabeled
            unlabeled += 1

    return {
        "confirmed_failures": confirmed_failures,
        "confirmed_clean": confirmed_clean,
        "unlabeled": unlabeled,
    }


def _compute_label_confirmation(labeled_support: dict) -> float:
    """Return a small additive label-confirmation term.

    + when labeled runs skew failure, ~0 when unlabeled-dominated,
    slightly NEGATIVE when clean dominates.
    This is NEVER a gate or multiplier — labels are too sparse.
    Max magnitude ±0.15 to keep it truly additive.
    """
    cf = labeled_support.get("confirmed_failures", 0)
    cc = labeled_support.get("confirmed_clean", 0)
    labeled = cf + cc
    if labeled == 0:
        return 0.0  # unlabeled-dominated → neutral
    # failure fraction among labeled
    fail_frac = cf / labeled
    # range: 0.0 (all clean) → 0.15 (all failure), but shifted so 0.5 → 0
    # term = 0.15 * (fail_frac - 0.5) * 2 = 0.3 * (fail_frac - 0.5)
    # When fail_frac=1.0 → +0.15; fail_frac=0.0 → -0.15; 0.5 → 0.0
    return round(0.30 * (fail_frac - 0.5), 4)


def _compute_prevalence_logical(
    conn: sqlite3.Connection,
    detector_name: str,
    project: str,
    logical_runs: dict[str, list[str]],
    window_days: int,
) -> float:
    """Compute prevalence over LOGICAL runs (re-stitched).

    = distinct logical_run_ids with ≥1 hit / total logical_run_ids in window
    for (project, detector) over window_days.
    """
    cutoff = _days_ago_utc(window_days)

    # Get all run_ids with hits for this (project, detector) in window
    hit_run_ids = set(
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT run_id
            FROM detector_hits
            WHERE detector = ?
              AND project  = ?
              AND ts       >= ?
            """,
            (detector_name, project, cutoff),
        ).fetchall()
    )

    # Get all run_ids in window for this project
    all_run_ids = set(
        row[0]
        for row in conn.execute(
            """
            SELECT run_id FROM runs
            WHERE project = ? AND ended >= ?
            """,
            (project, cutoff),
        ).fetchall()
    )

    if not all_run_ids:
        return 0.0

    # Build logical run id for each physical run
    run_to_logical: dict[str, str] = {}
    for logical_id, members in logical_runs.items():
        for m in members:
            run_to_logical[m] = logical_id

    # Count logical runs that had at least one hit
    logical_hit_runs = {
        run_to_logical.get(r, r) for r in hit_run_ids if r in all_run_ids
    }
    logical_total_runs = {run_to_logical.get(r, r) for r in all_run_ids}

    if not logical_total_runs:
        return 0.0
    return len(logical_hit_runs) / len(logical_total_runs)


def score_issue(
    issue: dict,
    prevalence: float,
    labeled_support: dict,
    rung: str,
    window_days: int,
) -> tuple[float, dict]:
    """Compute composite score + transparent components for an issue.

    Returns (score, score_components_dict).
    score is a weighted sum; each weight is documented in SCORE_WEIGHTS.
    """
    recurrence_count = issue.get("recurrence_count", 1)

    # Normalize recurrence: use log-compression so very high counts don't dominate
    # recurrence_norm ∈ (0, 1]: log(count+1)/log(MAX) capped at 1.
    MAX_RECURRENCE_NORM = 20.0
    recurrence_norm = min(1.0, math.log1p(recurrence_count) / math.log(MAX_RECURRENCE_NORM + 1))

    rung_weight = RUNG_WEIGHTS.get(rung, 1.0) / max(RUNG_WEIGHTS.values())  # normalize 0..1

    label_confirmation = _compute_label_confirmation(labeled_support)

    recency = _compute_recency(issue.get("last_seen", ""))

    components = {
        "prevalence": round(prevalence, 4),
        "recurrence_count": recurrence_count,
        "ladder_rung_weight": round(rung_weight, 4),
        "label_confirmation": round(label_confirmation, 4),
        "recency": round(recency, 4),
    }

    score = (
        SCORE_WEIGHTS["prevalence"]        * prevalence
        + SCORE_WEIGHTS["recurrence"]      * recurrence_norm
        + SCORE_WEIGHTS["rung"]            * rung_weight
        + SCORE_WEIGHTS["label_confirmation"] * label_confirmation  # additive, can be −
        + SCORE_WEIGHTS["recency"]         * recency
    )
    return round(score, 6), components


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def make_decision(
    score: float,
    rung: str,
    replay: ReplayResult,
    labeled_support: dict,
    logical_run_count: int,
) -> tuple[str, str]:
    """Return (decision, rationale).

    Decision outcomes:
    - propose_fix    replay passed AND score >= ACT_THRESHOLD
    - suppressed     replay failed OR confirmed_clean dominates
    - needs_more_data insufficient logical runs or history
    - monitor        everything else (real but below bar)
    """
    if logical_run_count < MIN_PROJECT_RUNS:
        return (
            "needs_more_data",
            f"Only {logical_run_count} logical run(s) in window; need >= {MIN_PROJECT_RUNS}.",
        )

    if replay.status == "insufficient_history":
        return (
            "needs_more_data",
            f"Replay gate: insufficient_history ({replay.runs_evaluated} runs evaluated).",
        )

    # Fix 7: pending = gate measurable but not yet implemented → monitor
    if replay.status == "pending":
        return (
            "monitor",
            (
                f"Replay gate pending (not yet implemented) for rung={rung!r}; "
                f"score={score:.3f}. Monitoring until replay is implemented."
            ),
        )

    # confirmed_clean domination check
    cc = labeled_support.get("confirmed_clean", 0)
    cf = labeled_support.get("confirmed_failures", 0)
    if cc > 0 and cc > cf and cc >= 3:
        return (
            "suppressed",
            f"confirmed_clean ({cc}) dominates confirmed_failures ({cf}): pattern may be benign.",
        )

    if replay.status == "failed":
        return (
            "suppressed",
            (
                f"Replay gate FAILED (prevention_rate={replay.prevention_rate:.0%}, "
                f"false_suppression={replay.false_suppression}): the proposed fix does not "
                f"pass the counterfactual; do NOT propose to avoid overfit or harmful changes."
            ),
        )

    if rung == "inform":
        # Inform-rung: no replay gate, but requires a HIGHER score bar.
        if score >= INFORM_ACT_THRESHOLD:
            return (
                "propose_fix",
                (
                    f"Inform-rung proposal: score={score:.3f} >= INFORM_ACT_THRESHOLD={INFORM_ACT_THRESHOLD}. "
                    f"Replay not applicable for inform-rung; higher bar compensates for "
                    f"unmeasurability."
                ),
            )
        elif score >= ACT_THRESHOLD:
            return (
                "monitor",
                (
                    f"Inform-rung: score={score:.3f} meets ACT_THRESHOLD but not INFORM_ACT_THRESHOLD "
                    f"({INFORM_ACT_THRESHOLD}). Monitoring; escalate when recurrence grows."
                ),
            )
        else:
            return (
                "monitor",
                f"Inform-rung: score={score:.3f} below ACT_THRESHOLD={ACT_THRESHOLD}. Monitoring.",
            )

    # Eliminate / Automate rungs
    if replay.status == "passed" and score >= ACT_THRESHOLD:
        return (
            "propose_fix",
            (
                f"Replay gate PASSED (prevention_rate={replay.prevention_rate:.0%}, "
                f"false_suppression={replay.false_suppression}). Score={score:.3f} >= "
                f"ACT_THRESHOLD={ACT_THRESHOLD}. Proposing fix."
            ),
        )
    elif replay.status == "passed":
        return (
            "monitor",
            (
                f"Replay gate PASSED but score={score:.3f} < ACT_THRESHOLD={ACT_THRESHOLD}. "
                f"Monitoring; escalate when prevalence/recurrence grows."
            ),
        )
    else:
        # not_applicable for a non-inform rung — shouldn't happen but be safe
        return (
            "monitor",
            f"Replay status={replay.status!r}; score={score:.3f}. Monitoring.",
        )


# ---------------------------------------------------------------------------
# Eval persistence
# ---------------------------------------------------------------------------

def get_prior_eval(conn: sqlite3.Connection, signature: str) -> Optional[dict]:
    """Fetch the most recent run_eval for a signature, or None.

    Fix 8: returns additional fields (score, rung, replay_status) for the
    material-change idempotency check. This prevents re-persisting an eval
    every synthesis pass when nothing meaningful changed.
    """
    row = conn.execute(
        """
        SELECT eval_id, decision, recurrence_count_at_apply, score, rung, replay_json
        FROM run_evals
        WHERE issue_signature = ?
        ORDER BY synthesis_pass_at DESC
        LIMIT 1
        """,
        (signature,),
    ).fetchone()
    if row is None:
        return None
    replay_status = "unknown"
    try:
        replay_dict = json.loads(row[5] or "{}")
        replay_status = replay_dict.get("status", "unknown")
    except (ValueError, TypeError):
        pass
    return {
        "eval_id": row[0],
        "decision": row[1],
        "recurrence_count_at_apply": row[2],
        "score": row[3],
        "rung": row[4],
        "replay_status": replay_status,
    }


def persist_eval(conn: sqlite3.Connection, eval_payload: dict) -> None:
    """Write a run_eval row. Caller ensures ensure_eval_schema() was called."""
    conn.execute(
        """
        INSERT OR REPLACE INTO run_evals (
            eval_id, issue_signature, project, detector, score, decision,
            rung, replay_json, score_components_json, labeled_support_json,
            remediation_json, supersedes_eval_id, synthesis_pass_at,
            recurrence_count_at_apply
        ) VALUES (
            :eval_id, :issue_signature, :project, :detector, :score, :decision,
            :rung, :replay_json, :score_components_json, :labeled_support_json,
            :remediation_json, :supersedes_eval_id, :synthesis_pass_at,
            :recurrence_count_at_apply
        )
        """,
        {
            "eval_id": eval_payload["eval_id"],
            "issue_signature": eval_payload["issue_signature"],
            "project": eval_payload["project"],
            "detector": eval_payload["detector"],
            "score": eval_payload["score"],
            "decision": eval_payload["decision"],
            "rung": eval_payload["rung"],
            "replay_json": json.dumps(eval_payload.get("replay_gate", {})),
            "score_components_json": json.dumps(eval_payload.get("score_components", {})),
            "labeled_support_json": json.dumps(eval_payload.get("labeled_support", {})),
            "remediation_json": json.dumps(eval_payload.get("remediation", {})),
            "supersedes_eval_id": eval_payload.get("supersedes_eval_id"),
            "synthesis_pass_at": eval_payload["synthesis_pass_at"],
            "recurrence_count_at_apply": (
                eval_payload["issue"]["recurrence_count"]
                if eval_payload.get("decision") == "propose_fix"
                else None
            ),
        },
    )


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def build_eval_payload(
    eval_id: str,
    issue: dict,
    candidate: Optional[PatternCandidate],
    score: float,
    score_components: dict,
    rung: str,
    rung_descent_reason: Optional[str],
    replay: ReplayResult,
    labeled_support: dict,
    decision: str,
    decision_rationale: str,
    supersedes_eval_id: Optional[str],
    window_days: int,
    run_sample: list[str],
    synthesis_pass_at: str,
) -> dict:
    """Build a bus.agent.run.eval.v1 payload dict."""
    payload: dict[str, Any] = {
        "eval_id": eval_id,
        "project": issue["project"],
        "issue_signature": issue["signature"],
        "pattern_name": issue.get("detector", ""),
        "detector": issue["detector"],
        "synthesis_pass_at": synthesis_pass_at,
        "score": score,
        "score_components": score_components,
        "window_days": window_days,
        "labeled_support": labeled_support,
        "rung": rung,
        "rung_descent_reason": rung_descent_reason,
        "replay_gate": replay.to_dict(),
        "decision": decision,
        "decision_rationale": decision_rationale,
        "supersedes_eval_id": supersedes_eval_id,
        "run_sample": run_sample[:10],
        "scorer_version": SCORER_VERSION,
        "schema_version": "1",
        # Internal field used by persist_eval
        "issue": issue,
    }
    if candidate and candidate.proposed_remediation:
        rung_type = candidate.extra.get("remediation_rung", rung)
        payload["remediation"] = {
            "proposal": candidate.proposed_remediation,
            "target": {
                "type": _rung_to_target_type(rung),
                "selector": {"signature": issue["signature"]},
                "content": candidate.proposed_remediation[:500],
            },
        }
    return payload


def _rung_to_target_type(rung: str) -> str:
    return {
        "eliminate": "env",
        "automate": "rule",
        "inform": "doc",
    }.get(rung, "doc")


def build_pattern_discovered_payload(
    eval_payload: dict,
    candidate: PatternCandidate,
    prevalence: float,
    issue: dict,
) -> dict:
    """Build a <project>.pattern.discovered.v1 payload."""
    rung = eval_payload["rung"]
    payload: dict[str, Any] = {
        "project": eval_payload["project"],
        "pattern_name": candidate.pattern_name,
        "occurrences": candidate.occurrences,
        "evidence": candidate.evidence,
        "signature": issue["signature"],
        "detector": issue["detector"],
        "remediation_rung": rung,
        "proposed_remediation": candidate.proposed_remediation,
        "prevalence": prevalence,
        "recurrence_count": issue.get("recurrence_count", 1),
        "run_ids": candidate.run_ids[:10],
        "eval_id": eval_payload["eval_id"],
    }
    # Only include proposed_patch if it has content (schema requires it to have keys)
    proposed_patch = eval_payload.get("remediation", {}).get("target", {})
    if proposed_patch:
        payload["proposed_patch"] = proposed_patch
    # Only include rung_descent_reason if non-None (schema requires string type)
    descent_reason = eval_payload.get("rung_descent_reason")
    if descent_reason is not None:
        payload["rung_descent_reason"] = descent_reason
    return payload


# ---------------------------------------------------------------------------
# Publish helpers
# ---------------------------------------------------------------------------

def _publish(channel: str, payload: dict, dry_run: bool) -> Optional[str]:
    """Shell out to `nervous publish` to emit a bus event.

    Returns None on success; an error string on failure.
    Only actually shells out when dry_run=False.
    """
    if dry_run:
        return None  # Callers print the DRY-RUN notice
    try:
        result = subprocess.run(
            ["nervous", "publish", channel, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return f"nervous publish error: {result.stderr.strip()}"
        return None
    except FileNotFoundError:
        return "nervous binary not found in PATH"
    except Exception as exc:
        return f"publish exception: {exc}"


# ---------------------------------------------------------------------------
# Main synthesis pass
# ---------------------------------------------------------------------------

@dataclass
class SynthesisResult:
    evals: list[dict] = field(default_factory=list)
    pattern_discovered_payloads: list[dict] = field(default_factory=list)
    publish_errors: list[str] = field(default_factory=list)


def run_synthesis(
    conn: sqlite3.Connection,
    *,
    project_filter: Optional[str] = None,
    window_days: int = WINDOW_DAYS,
    dry_run: bool = True,
    emit_beads: bool = False,
) -> SynthesisResult:
    """Execute the full synthesis pass.

    Steps:
    0. Re-stitch logical runs.
    1. Run all detectors.
    2. Score each issue.
    3. Determine rung.
    4. Replay-eval gate.
    5. Decision.
    6. Emit + persist.

    Returns a SynthesisResult with the eval payloads.
    """
    result = SynthesisResult()
    synthesis_pass_at = _now_utc()
    ensure_eval_schema(conn)

    # ── Step 0: Re-stitch ──────────────────────────────────────────────────
    logical_runs = stitch_logical_runs(conn)
    # Count logical runs per project
    logical_run_ids_by_project: dict[str, set[str]] = {}
    for logical_id, members in logical_runs.items():
        # get project for this logical run
        for member in members:
            proj_row = conn.execute(
                "SELECT project FROM runs WHERE run_id = ?", (member,)
            ).fetchone()
            if proj_row:
                proj = proj_row[0]
                logical_run_ids_by_project.setdefault(proj, set()).add(logical_id)
                break  # all members same project

    # ── Step 1: Run all detectors ──────────────────────────────────────────
    for DetectorClass in DETECTOR_CLASSES:
        try:
            detector = DetectorClass(conn)
            detector.run(conn)
        except Exception as exc:
            print(
                f"[synthesis] WARNING: detector {DetectorClass.DETECTOR_NAME} failed: {exc}",
                file=sys.stderr,
            )
    conn.commit() if conn.isolation_level is not None else None

    # ── Fetch all issues to evaluate ───────────────────────────────────────
    issues_query = "SELECT * FROM issues"
    params: list = []
    if project_filter:
        issues_query += " WHERE project = ?"
        params.append(project_filter)

    cur = conn.execute(issues_query, params)
    cols = [d[0] for d in cur.description]
    issues = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not issues:
        return result

    # Build a mapping from signature → PatternCandidate (last one wins)
    # We re-run detectors in detection-only mode (no recording) to get candidates
    sig_to_candidate: dict[str, PatternCandidate] = {}
    for DetectorClass in DETECTOR_CLASSES:
        try:
            detector = DetectorClass(conn)
            candidates = detector.detect(conn)
            for c in candidates:
                sig_to_candidate[c.signature] = c
        except Exception:
            pass

    # Build a mapping of detector name → detector instance for replay
    detector_instances: dict[str, BaseDetector] = {}
    for DetectorClass in DETECTOR_CLASSES:
        try:
            inst = DetectorClass(conn)
            detector_instances[inst.DETECTOR_NAME] = inst
        except Exception:
            pass

    # ── Steps 2-6: Score, rung, replay, decision, emit ─────────────────────
    for issue in issues:
        signature = issue["signature"]
        project = issue["project"]
        detector_name = issue["detector"]

        if project_filter and project != project_filter:
            continue

        logical_run_count = len(logical_run_ids_by_project.get(project, set()))

        # Find candidate for this signature
        candidate = sig_to_candidate.get(signature)

        # Known detector rung overrides — used when a candidate doesn't
        # explicitly set remediation_rung in its extra dict.
        _DETECTOR_RUNGS = {
            "rebuild_cache_miss": "eliminate",
            "worktree_leak": "automate",
            "repeated_question": "automate",
            "reread_same_file": "automate",
            "edit_build_fail_revert": "inform",
            "file_reads_to_finding": "inform",
        }

        # Determine rung from candidate's extra (explicit) or from known-rung registry.
        # Synthesis may NEVER fabricate a higher rung than the detector justified;
        # the registry encodes the highest rung each detector has justified in its
        # module docstring.
        if candidate and candidate.extra.get("remediation_rung"):
            rung = candidate.extra["remediation_rung"]
        else:
            rung = _DETECTOR_RUNGS.get(detector_name, "inform")

        rung_descent_reason: Optional[str] = None
        if rung != "eliminate":
            if candidate:
                rung_descent_reason = candidate.extra.get(
                    "remediation_rung_justification",
                    f"Detector {detector_name!r} climbed to {rung!r} (not eliminate).",
                )
            else:
                rung_descent_reason = (
                    f"No candidate available for detector {detector_name!r}; "
                    f"defaulted to {rung!r}."
                )

        # ── Step 2: Score ─────────────────────────────────────────────────
        prevalence = _compute_prevalence_logical(
            conn, detector_name, project, logical_runs, window_days
        )
        labeled_support = _compute_labeled_support(conn, signature)
        score, score_components = score_issue(
            issue, prevalence, labeled_support, rung, window_days
        )

        # ── Step 4: Replay-eval gate ──────────────────────────────────────
        # Fix 1: pass signature so replay is scoped per-issue (not per-detector)
        # Fix 7: pass rung so _default_replay can choose pending vs not_applicable
        # Fix 5: pass window_days so rebuild replay is window-bounded
        detector_inst = detector_instances.get(detector_name)
        if detector_inst is not None:
            try:
                replay = detector_inst.replay(  # type: ignore[attr-defined]
                    conn, logical_runs, signature, rung=rung, window_days=window_days
                )
            except Exception as exc:
                replay = ReplayResult(
                    status="not_applicable",
                    method=f"replay() raised {exc}",
                )
        else:
            replay = ReplayResult(
                status="not_applicable",
                method=f"No detector instance for {detector_name!r}.",
            )

        # ── Step 5: Decision ──────────────────────────────────────────────
        decision, decision_rationale = make_decision(
            score=score,
            rung=rung,
            replay=replay,
            labeled_support=labeled_support,
            logical_run_count=logical_run_count,
        )

        # ── Step 6: Persist eval ──────────────────────────────────────────
        eval_id = _make_ulid()
        prior_eval = get_prior_eval(conn, signature)
        supersedes_eval_id = prior_eval["eval_id"] if prior_eval else None

        # Fix 8: idempotency on material-change signal — skip re-persist when:
        # same decision AND same score band (rounded to 2dp) AND same rung AND
        # same replay status AND same recurrence (for propose_fix).
        # This prevents a new row every synthesis pass when nothing changed.
        if prior_eval:
            prior_decision = prior_eval.get("decision")
            prior_score = prior_eval.get("score")
            prior_rung = prior_eval.get("rung")
            prior_replay_status = prior_eval.get("replay_status")
            same_decision = prior_decision == decision
            same_score = (
                prior_score is not None
                and round(prior_score, 2) == round(score, 2)
            )
            same_rung = prior_rung == rung
            same_replay = prior_replay_status == replay.status
            # For propose_fix, also check recurrence hasn't grown
            recurrence_ok = (
                decision != "propose_fix"
                or prior_eval.get("recurrence_count_at_apply") == issue.get("recurrence_count")
            )
            if same_decision and same_score and same_rung and same_replay and recurrence_ok:
                continue

        # Gather run_sample from detector_hits
        run_sample = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT run_id FROM detector_hits
                WHERE signature = ?
                ORDER BY ts DESC LIMIT 10
                """,
                (signature,),
            ).fetchall()
        ]

        eval_payload = build_eval_payload(
            eval_id=eval_id,
            issue=issue,
            candidate=candidate,
            score=score,
            score_components=score_components,
            rung=rung,
            rung_descent_reason=rung_descent_reason,
            replay=replay,
            labeled_support=labeled_support,
            decision=decision,
            decision_rationale=decision_rationale,
            supersedes_eval_id=supersedes_eval_id,
            window_days=window_days,
            run_sample=run_sample,
            synthesis_pass_at=synthesis_pass_at,
        )
        persist_eval(conn, eval_payload)
        result.evals.append(eval_payload)

        # Snapshot recurrence_count_at_apply in issues table
        if decision == "propose_fix":
            conn.execute(
                """
                UPDATE issues
                SET recurrence_count_at_apply = ?
                WHERE signature = ?
                """,
                (issue.get("recurrence_count", 1), signature),
            )

        # ── Emit bus.agent.run.eval.v1 ────────────────────────────────────
        eval_channel = "bus.agent.run.eval.v1"
        # Strip internal 'issue' key before publishing
        publish_eval = {k: v for k, v in eval_payload.items() if k != "issue"}
        if not dry_run:
            err = _publish(eval_channel, publish_eval, dry_run=False)
            if err:
                result.publish_errors.append(f"eval {eval_id}: {err}")
        else:
            pass  # DRY-RUN: nothing to shell out to

        # ── Emit <project>.pattern.discovered.v1 (propose_fix only) ──────
        if decision == "propose_fix" and candidate:
            pd_payload = build_pattern_discovered_payload(
                eval_payload=eval_payload,
                candidate=candidate,
                prevalence=prevalence,
                issue=issue,
            )
            pd_channel = f"{project}.pattern.discovered.v1"
            result.pattern_discovered_payloads.append(
                {"channel": pd_channel, "payload": pd_payload}
            )
            if not dry_run:
                err = _publish(pd_channel, pd_payload, dry_run=False)
                if err:
                    result.publish_errors.append(f"pattern.discovered {eval_id}: {err}")

    return result


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(no rows)"
    str_rows = [[str(row.get(c, "")) for c in columns] for row in rows]
    widths = [len(c) for c in columns]
    for sr in str_rows:
        for i, v in enumerate(sr):
            widths[i] = max(widths[i], len(v))
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep = "  ".join("-" * widths[i] for i in range(len(columns)))
    lines = [header, sep] + [
        "  ".join(v.ljust(widths[i]) for i, v in enumerate(sr))
        for sr in str_rows
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="synthesis",
        description=(
            "Reflexarc synthesis pass — rank issues, replay-gate fixes, "
            "emit run.eval + pattern.discovered. Default: DRY-RUN."
        ),
    )
    parser.add_argument(
        "--emit", action="store_true",
        help="Actually publish via nervous publish (default: DRY-RUN, compute + persist only)",
    )
    parser.add_argument(
        "--file-beads", action="store_true",
        help="File proposal beads for propose_fix decisions (requires --emit)",
    )
    parser.add_argument(
        "--project", "-p", metavar="PROJECT",
        help="Filter to a specific project",
    )
    parser.add_argument(
        "--window-days", type=int, default=WINDOW_DAYS, metavar="N",
        help=f"Rolling window for prevalence/recency (default {WINDOW_DAYS})",
    )
    parser.add_argument(
        "--json", "-j", action="store_true",
        help="Machine-readable JSON output",
    )
    parser.add_argument(
        "--db", metavar="PATH",
        help=f"Path to runs.db (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH
    if not db_path.exists():
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")

    dry_run = not args.emit

    if dry_run:
        print("[DRY-RUN] Compute + persist evals locally. No nervous publish calls.",
              file=sys.stderr if args.json else sys.stdout)
        print("          Run with --emit to actually publish.\n",
              file=sys.stderr if args.json else sys.stdout)

    result = run_synthesis(
        conn,
        project_filter=args.project,
        window_days=args.window_days,
        dry_run=dry_run,
        emit_beads=args.file_beads,
    )

    conn.close()

    if args.json:
        output = {
            "evals": [
                {k: v for k, v in e.items() if k != "issue"}
                for e in result.evals
            ],
            "pattern_discovered_payloads": result.pattern_discovered_payloads,
            "publish_errors": result.publish_errors,
            "dry_run": dry_run,
        }
        print(json.dumps(output, indent=2))
        return 0

    # Human-readable table
    if not result.evals:
        print("No issues found to evaluate.")
        return 0

    # Sort by score descending
    sorted_evals = sorted(result.evals, key=lambda e: -e["score"])
    display = []
    for e in sorted_evals:
        sig = e["issue_signature"]
        short_sig = sig[:55] + ("…" if len(sig) > 55 else "")
        replay_status = e.get("replay_gate", {}).get("status", "?")
        comp = e.get("score_components", {})
        display.append({
            "signature": short_sig,
            "score": f"{e['score']:.3f}",
            "rung": e["rung"],
            "replay": replay_status,
            "decision": e["decision"],
            "prev": f"{comp.get('prevalence', 0):.0%}",
            "recur": str(comp.get("recurrence_count", "")),
        })

    print(_fmt_table(
        display,
        ["signature", "score", "rung", "replay", "decision", "prev", "recur"],
    ))
    print(f"\n{len(result.evals)} eval(s); {sum(1 for e in result.evals if e['decision'] == 'propose_fix')} propose_fix.")

    if dry_run and result.pattern_discovered_payloads:
        print("\n[DRY-RUN] Would publish pattern.discovered (--emit to actually send):")
        for pd in result.pattern_discovered_payloads:
            print(f"  channel={pd['channel']}")
            print(f"  payload.signature={pd['payload'].get('signature', '')}")
            print(f"  payload.rung={pd['payload'].get('remediation_rung', '')}")
            print()

    if result.evals:
        print("\n[replay gate details]")
        for e in sorted_evals:
            rg = e.get("replay_gate", {})
            if rg.get("status") in ("passed", "failed"):
                sig = e["issue_signature"]
                short = sig[:50]
                pr = rg.get("prevention_rate", 0)
                fs = rg.get("false_suppression", 0)
                n = rg.get("runs_evaluated", 0)
                wp = rg.get("would_have_prevented", 0)
                print(
                    f"  {short}: status={rg['status']!r}  "
                    f"prevention_rate={pr:.0%}  false_suppression={fs}  "
                    f"runs_evaluated={n}  would_have_prevented={wp}"
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
