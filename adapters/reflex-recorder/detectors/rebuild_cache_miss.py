"""detectors/rebuild_cache_miss.py — Tier-1 rebuild-cache-miss detector.

Detects full (cold) cargo rebuilds that happen inside agent worktrees because
they do not share a Cargo target directory or sccache with the main project
tree.  Each worktree rebuilds every crate from scratch, wasting minutes of
wall time per agent run.

Algorithm
=========
1. For every run_event whose command contains a cargo build/test/run/check
   invocation AND whose cwd indicates a worktree (path contains
   "/.claude/worktrees/" or "/.worktrees/"), compute the APPARENT build
   duration as the gap (seconds) from that event's event_ts to the
   event_ts of the NEXT event in the same run (by seq).  This gap is a
   reliable lower bound — the agent was blocked waiting for the build.

2. Compute a per-project baseline using the p50 (median) of all observed
   build gaps for that project.  For the first observation there is no
   historical baseline, so we also apply an absolute floor:
   ABSOLUTE_SLOW_BUILD_FLOOR_S.

3. Flag any build whose gap exceeds max(baseline * SLOW_BUILD_MULTIPLIER,
   ABSOLUTE_SLOW_BUILD_FLOOR_S).

4. Group flagged builds by (project, crate_target) where crate_target is
   extracted from the cargo command (-p <crate> or the workspace root).
   The stable_anchor is "<project>/<crate_target>".

5. Emit a PatternCandidate per (project, crate_target) with:
   - evidence listing run IDs, build durations, cwd
   - proposed_remediation: Eliminate-rung fix (shared CARGO_TARGET_DIR or
     sccache)
   - remediation_rung = "eliminate" in extra

Thresholds (named constants, adjust as calibration improves)
=============================================================
  SLOW_BUILD_MULTIPLIER     = 3    # flag if > 3× project p50
  ABSOLUTE_SLOW_BUILD_FLOOR_S = 60  # flag if > 60s regardless of baseline

These are conservative — they catch only clear cold-rebuild anomalies
without firing on normal incremental builds.

Remediation ladder — ELIMINATE rung
=====================================
The root cause is that each worktree has its own empty `target/` directory.
The fix is structural: share the compilation cache across worktrees.

Options (all Eliminate-rung — they remove the cost, not just warn about it):
  A. Set CARGO_TARGET_DIR=~/.cache/cargo-target/<project>/<crate> in the
     worktree launch environment (shared incremental cache).
  B. Deploy sccache / cargo-cache and set RUSTC_WRAPPER=sccache.
  C. Use cargo's `[build] target-dir = "/shared/path"` in a workspace-level
     .cargo/config.toml that is also present in the worktree.

proposed_remediation proposes option A as the simplest zero-dependency fix.
The remediation_rung is "eliminate" because the fix removes the repeated
compilation entirely, not just alerts operators to it.

Signature
=========
  f"{project}:rebuild_cache_miss:{project}/{crate_target}"
  — stable across runs; does NOT include run_id or timestamp.

Usage
=====
    import sqlite3
    from detectors.rebuild_cache_miss import RebuildCacheMissDetector

    conn = sqlite3.connect("~/.cache/nervous-bus/reflex/runs.db")
    detector = RebuildCacheMissDetector(conn)
    candidates = detector.run()
    for c in candidates:
        payload = detector.emit_candidate(c)
        print(payload)
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate


# ── Thresholds ────────────────────────────────────────────────────────────────

#: Multiplier applied to project p50 build time; builds exceeding this are slow.
SLOW_BUILD_MULTIPLIER: int = 3

#: Absolute minimum gap (seconds) to flag, independent of baseline.
ABSOLUTE_SLOW_BUILD_FLOOR_S: int = 60

#: Cargo subcommands that trigger a build (and may produce a slow full rebuild).
CARGO_BUILD_COMMANDS: tuple[str, ...] = (
    "cargo build",
    "cargo test",
    "cargo run",
    "cargo check",
    "cargo bench",
)

#: Path fragment that identifies a worktree cwd.
WORKTREE_CWD_MARKERS: tuple[str, ...] = (
    "/.claude/worktrees/",
    "/.worktrees/",
)


# ── Remediation template ──────────────────────────────────────────────────────

_REMEDIATION_TEMPLATE = (
    "Eliminate-rung: share the Cargo compilation cache across all worktrees "
    "for project '{project}' crate '{crate}'. "
    "Root cause: each worktree has its own empty target/ dir so every agent "
    "run triggers a full cold rebuild (observed {occurrences} slow build(s), "
    "max gap {max_gap_s:.0f}s, baseline p50 {baseline_s:.0f}s). "
    "Fix: set CARGO_TARGET_DIR={shared_target_dir} in the worktree launch "
    "environment (e.g. nervous-bus agent launch hook or ~/.cargo/config.toml "
    "[build] target-dir). "
    "Alternative: RUSTC_WRAPPER=sccache for cross-machine sharing. "
    "This removes the repeated compilation entirely — no cache warming needed."
)

_SHARED_TARGET_DIR_TEMPLATE = "~/.cache/cargo-target/{project}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_command(raw_json: str) -> Optional[str]:
    """Extract the bash command string from a raw_json event.

    tool_summary may be a JSON object with a "command" key, or a plain string.
    Returns None if no command is found.
    """
    try:
        d = json.loads(raw_json)
        data = d.get("data", d)
        ts_sum = data.get("tool_summary") or ""
        if not ts_sum:
            return None
        if isinstance(ts_sum, str) and ts_sum.startswith("{"):
            try:
                obj = json.loads(ts_sum)
                return obj.get("command") if isinstance(obj, dict) else ts_sum
            except json.JSONDecodeError:
                return ts_sum
        return str(ts_sum)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _extract_cwd(raw_json: str) -> Optional[str]:
    """Extract the cwd field from the event data."""
    try:
        d = json.loads(raw_json)
        data = d.get("data", d)
        return data.get("cwd") or None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _is_cargo_build_command(cmd: str) -> bool:
    """Return True if *cmd* contains a cargo subcommand that triggers a build."""
    return any(token in cmd for token in CARGO_BUILD_COMMANDS)


def _is_worktree_cwd(cwd: Optional[str]) -> bool:
    """Return True if *cwd* looks like a git worktree path."""
    if not cwd:
        return False
    return any(marker in cwd for marker in WORKTREE_CWD_MARKERS)


def _extract_crate_target(cmd: str) -> str:
    """Extract the crate name from a cargo command.

    Looks for -p <crate> or --package <crate>.  Falls back to "workspace".
    """
    m = re.search(r"(?:-p|--package)\s+(\S+)", cmd)
    if m:
        return m.group(1)
    return "workspace"


def _parse_event_ts(ts_str: str) -> Optional[datetime]:
    """Parse a nanosecond-precision RFC3339 timestamp into a datetime."""
    try:
        # Truncate sub-second part to microsecond precision for fromisoformat
        ts_norm = re.sub(r"(\.\d{6})\d+(Z|[+-])", r"\1\2", ts_str)
        ts_norm = ts_norm.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_norm)
    except (ValueError, TypeError):
        return None


# ── Detector ──────────────────────────────────────────────────────────────────


class RebuildCacheMissDetector(BaseDetector):
    """Detect cold/full cargo rebuilds in agent worktrees.

    See module docstring for the full algorithm and remediation rationale.
    """

    DETECTOR_NAME = "rebuild_cache_miss"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Scan run_events for slow cargo builds executed inside worktree cwds.

        Strategy:
          1. Load all Bash events whose raw_json mentions "cargo" and whose cwd
             is inside a worktree path.  We need consecutive pairs to compute
             gap duration, so we pull them as (run_id, seq, event_ts, raw_json)
             and post-process in Python to find inter-event gaps.
          2. For each cargo command that is followed by another event in the
             same run, record (project, crate_target, run_id, gap_s, cwd).
          3. Compute per-project p50 baseline from all observed gap_s values.
          4. Flag gaps that exceed the threshold.
          5. Group flagged observations by (project, crate_target) and emit one
             PatternCandidate per group.

        COMPLETENESS GATE: only CLOSED runs (close_reason IS NOT NULL) feed the
        baseline and flagging.  The run-store only ever holds closed runs — the
        recorder writes the run row + its full event stream atomically at close,
        so an in-flight run has no row and is already excluded by the INNER JOIN.
        We still gate on close_reason IS NOT NULL explicitly so the invariant is
        enforced at the query layer and survives any future incremental-write
        change.  We deliberately do NOT gate on labeled_at: outcome labeling is a
        separate backfill pass, most complete runs are not-yet-labeled, and the
        baseline needs event-stream completeness (close_reason), not a label.
        This detector never reads `outcome`, so the null-vs-clean rule does not
        apply here.
        """

        # ── Step 1: fetch all run_events rows that contain "cargo" ────────────
        # We join against runs to get project + worktree context.  We need the
        # full event sequence per run so we can compute inter-event gaps, so we
        # pull ALL events for runs that have at least one cargo event.
        candidate_run_ids: list[str] = [
            row[0]
            for row in conn.execute(
                """
                SELECT DISTINCT re.run_id
                FROM run_events re
                JOIN runs r ON r.run_id = re.run_id
                WHERE re.raw_json LIKE '%cargo%'
                  AND r.close_reason IS NOT NULL
                """
            ).fetchall()
        ]

        if not candidate_run_ids:
            return []

        placeholders = ",".join("?" * len(candidate_run_ids))
        rows = conn.execute(
            f"""
            SELECT re.run_id, re.seq, re.event_ts, re.raw_json,
                   r.project, r.worktree
            FROM run_events re
            JOIN runs r ON r.run_id = re.run_id
            WHERE re.run_id IN ({placeholders})
            ORDER BY re.run_id, re.seq
            """,
            candidate_run_ids,
        ).fetchall()

        # ── Step 2: group by run_id and compute inter-event gaps ──────────────
        # Structure: {run_id: [(seq, ts, raw_json, project, worktree), ...]}
        by_run: dict[str, list[tuple]] = {}
        for run_id, seq, ts, raw, project, worktree in rows:
            by_run.setdefault(run_id, []).append((seq, ts, raw, project, worktree))

        # Observations: list of dicts with keys:
        #   project, crate, run_id, gap_s, cwd, is_worktree
        observations: list[dict] = []

        for run_id, events in by_run.items():
            for i, (seq, ts, raw, project, worktree) in enumerate(events):
                cmd = _extract_command(raw)
                if not cmd:
                    continue
                if not _is_cargo_build_command(cmd):
                    continue

                cwd = _extract_cwd(raw)

                # Only flag events whose cwd is a worktree path.
                # We use both the event-level cwd and the run-level worktree
                # column as signals; prefer the event-level cwd (more precise).
                in_worktree = _is_worktree_cwd(cwd) or _is_worktree_cwd(worktree)
                if not in_worktree:
                    continue

                # Compute gap to the next event in this run.
                if i + 1 >= len(events):
                    # Last event in run — cannot measure gap.
                    continue
                next_ts = events[i + 1][1]
                dt_start = _parse_event_ts(ts)
                dt_end = _parse_event_ts(next_ts)
                if dt_start is None or dt_end is None:
                    continue
                gap_s = (dt_end - dt_start).total_seconds()
                if gap_s < 0:
                    continue

                crate = _extract_crate_target(cmd)
                observations.append(
                    {
                        "project": project,
                        "crate": crate,
                        "run_id": run_id,
                        "gap_s": gap_s,
                        "cwd": cwd or worktree or "",
                        "cmd": cmd[:120],
                    }
                )

        if not observations:
            return []

        # ── Step 3: compute per-project p50 baseline ──────────────────────────
        # All observations (slow + fast) contribute to the baseline.
        # We compute the median gap per project across all worktree cargo calls.
        project_gaps: dict[str, list[float]] = {}
        for obs in observations:
            project_gaps.setdefault(obs["project"], []).append(obs["gap_s"])

        project_baseline: dict[str, float] = {}
        for project, gaps in project_gaps.items():
            if len(gaps) == 1:
                # Single observation — use absolute floor only; no meaningful p50.
                project_baseline[project] = 0.0
            else:
                project_baseline[project] = statistics.median(gaps)

        # ── Step 4: flag slow builds ──────────────────────────────────────────
        # Two independent conditions — a build fires if EITHER is true:
        #   A. gap > ABSOLUTE_SLOW_BUILD_FLOOR_S  (hard minimum; always active)
        #   B. gap > baseline * SLOW_BUILD_MULTIPLIER  (regression vs project p50)
        # Condition A catches cold rebuilds when all observations are slow
        # (baseline would be high and A-only would miss them).
        # Condition B catches projects that are normally fast but suddenly slow.
        flagged: list[dict] = []
        for obs in observations:
            baseline = project_baseline.get(obs["project"], 0.0)
            above_floor = obs["gap_s"] > float(ABSOLUTE_SLOW_BUILD_FLOOR_S)
            above_multiplier = baseline > 0.0 and obs["gap_s"] > baseline * SLOW_BUILD_MULTIPLIER
            if above_floor or above_multiplier:
                obs["threshold_s"] = float(ABSOLUTE_SLOW_BUILD_FLOOR_S)
                obs["baseline_s"] = baseline
                flagged.append(obs)

        if not flagged:
            return []

        # ── Step 5: group by (project, crate) and build PatternCandidates ─────
        groups: dict[tuple[str, str], list[dict]] = {}
        for obs in flagged:
            key = (obs["project"], obs["crate"])
            groups.setdefault(key, []).append(obs)

        candidates: list[PatternCandidate] = []
        for (project, crate), group_obs in groups.items():
            run_ids = list({o["run_id"] for o in group_obs})
            max_gap = max(o["gap_s"] for o in group_obs)
            baseline = group_obs[0]["baseline_s"]
            shared_target = _SHARED_TARGET_DIR_TEMPLATE.format(project=project)

            evidence: list[str] = [
                f"project={project}",
                f"crate={crate}",
                f"slow_builds={len(group_obs)}",
                f"max_gap_s={max_gap:.0f}",
                f"baseline_p50_s={baseline:.0f}",
                f"threshold_s={group_obs[0]['threshold_s']:.0f}",
            ]
            for obs in group_obs[:5]:  # cap evidence list at 5 examples
                evidence.append(
                    f"run={obs['run_id'][:20]} gap={obs['gap_s']:.0f}s cwd=...{obs['cwd'][-40:]}"
                )

            remediation = _REMEDIATION_TEMPLATE.format(
                project=project,
                crate=crate,
                occurrences=len(group_obs),
                max_gap_s=max_gap,
                baseline_s=baseline,
                shared_target_dir=shared_target,
            )

            # Signature: stable cross-run; no run_id, no timestamp.
            anchor = f"{project}/{crate}"
            signature = f"{project}:{self.DETECTOR_NAME}:{anchor}"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="rebuild_cache_miss",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(group_obs),
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=remediation,
                    extra={
                        "crate_target": crate,
                        "max_gap_s": max_gap,
                        "baseline_p50_s": baseline,
                        "slow_build_count": len(group_obs),
                        "slow_build_multiplier": SLOW_BUILD_MULTIPLIER,
                        "absolute_slow_build_floor_s": ABSOLUTE_SLOW_BUILD_FLOOR_S,
                        "shared_target_dir_proposal": shared_target,
                        "remediation_rung": "eliminate",
                        "remediation_rung_justification": (
                            "Setting CARGO_TARGET_DIR to a shared path eliminates "
                            "cold rebuilds entirely — no repeated compilation, no "
                            "warming step required. This is higher than 'automate' "
                            "(pre-warm) or 'inform' (warn the user) because it "
                            "removes the root cause structurally."
                        ),
                    },
                )
            )

        return candidates
