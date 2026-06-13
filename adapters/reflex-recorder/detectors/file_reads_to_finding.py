"""detectors/file_reads_to_finding.py — Tier-1 file-reads-to-finding detector.

Measures the navigation cost to the first mutation per run: how many
Read/Bash/Grep/Glob events fire BEFORE the first Edit/Write ("the finding")?
A high count indicates the agent is over-searching instead of navigating
deterministically to the right file.

Algorithm
=========
1. For each labeled run (labeled_at IS NOT NULL), walk run_events ordered by seq.
   Count navigation events (Read, Bash, Grep, Glob) before the first mutation
   event (Edit, Write).  Runs with zero mutations are skipped (read-only tasks
   are not a navigation problem).

2. Per project, collect the per-run counts and compute p50, p90, max.
   Flag runs that exceed READ_THRESHOLD.

3. Emit a PatternCandidate per project ONLY when ≥ MIN_FLAGGED_RUNS runs in
   that project exceeded the threshold (recurring pattern, not a one-off).

4. stable_anchor = project  (the recurring problem is "agents over-search in
   THIS project", not a single run).  Signature = "<project>:file_reads_to_finding:<project>".

Remediation ladder
==================
High reads-to-finding is an AUTOMATE/INFORM problem, NOT eliminatable by code
change (the agent behavior is emergent).  The highest achievable rung is
AUTOMATE: generate or maintain a project index file (key file → purpose map)
that the harness injects into the system prompt, so agents navigate
deterministically to the right file rather than ls/find/grep thrashing.
If no automated index mechanism is available yet, fall back to INFORM: add
explicit navigation hints to CLAUDE.md.

Both rungs are encoded in proposed_remediation and extra["remediation_rung"].

Constants
=========
READ_THRESHOLD       — minimum pre-mutation navigation events to flag a run (20)
MIN_FLAGGED_RUNS     — minimum flagged runs per project to emit a candidate (2)

Usage
=====
    import sqlite3
    from detectors.file_reads_to_finding import FileReadsToFindingDetector

    conn = sqlite3.connect("~/.cache/nervous-bus/reflex/runs.db")
    detector = FileReadsToFindingDetector(conn)
    candidates = detector.run()
    for c in candidates:
        payload = detector.emit_candidate(c)
        print(payload)
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate


# ── Tuning constants ──────────────────────────────────────────────────────────

# Number of navigation events before first mutation that qualifies a run as
# "over-searching".  Rationale: a well-indexed project needs ≤ ~10 lookups
# to reach the target file; 20 is conservative (2× upper-normal).
READ_THRESHOLD: int = 20

# A project is only flagged when this many runs exceeded READ_THRESHOLD.
# Prevents single-run noise from becoming a false alarm.
MIN_FLAGGED_RUNS: int = 2

# Tool names we classify as "navigation" (read-only exploration).
_NAV_TOOLS: frozenset[str] = frozenset({"Read", "Bash", "Grep", "Glob"})

# Tool names we classify as "mutation" (first resolving edit = the "finding").
_MUTATION_TOOLS: frozenset[str] = frozenset({"Edit", "Write"})

# Automate-rung remediation template
_REMEDIATION_TEMPLATE = (
    "AUTOMATE-rung: for project '{project}', generate and maintain a "
    "key-files index (e.g. CLAUDE.md § 'Key files' or a dedicated "
    "project-map skill) that enumerates <file> → <purpose> so the harness "
    "can inject it at session start.  This collapses the navigation phase "
    "from O(search) to O(lookup).  "
    "p50={p50}, p90={p90}, max={max} nav-events-before-first-mutation "
    "across {flagged_runs} flagged runs (threshold={threshold}).  "
    "If automation is not yet possible, add explicit navigation hints to "
    "CLAUDE.md as an INFORM-rung interim measure."
)


class FileReadsToFindingDetector(BaseDetector):
    """Detect projects where agents over-search before their first mutation.

    See module docstring for the full algorithm and remediation ladder.
    """

    DETECTOR_NAME = "file_reads_to_finding"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Compute per-run navigation costs and flag over-searching projects.

        Only considers labeled runs (labeled_at IS NOT NULL) so we gate on
        outcome quality — unlabeled runs may not yet represent real signal.
        """
        # 1. Fetch labeled runs that have at least one event.
        labeled_runs = conn.execute(
            """
            SELECT r.run_id, r.project
            FROM runs r
            WHERE r.labeled_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM run_events e WHERE e.run_id = r.run_id
              )
            ORDER BY r.run_id
            """
        ).fetchall()

        if not labeled_runs:
            return []

        # 2. For each run, compute nav_count_before_first_mutation.
        #    Map: project → list of (run_id, nav_count) for runs that HAD a mutation.
        from collections import defaultdict
        project_runs: dict[str, list[tuple[str, int]]] = defaultdict(list)

        for run_id, project in labeled_runs:
            nav_count, had_mutation = _nav_events_before_first_mutation(conn, run_id)
            if not had_mutation:
                # Read-only tasks (no Edit/Write) are not a navigation failure;
                # skip them to avoid polluting the distribution.
                continue
            project_runs[project].append((run_id, nav_count))

        if not project_runs:
            return []

        # 3. Per project: compute distribution, find flagged runs.
        candidates: list[PatternCandidate] = []

        for project, run_counts in project_runs.items():
            counts = [c for _, c in run_counts]
            flagged = [(rid, c) for rid, c in run_counts if c >= READ_THRESHOLD]

            if len(flagged) < MIN_FLAGGED_RUNS:
                # Not a recurring pattern — skip.
                continue

            # 4. Compute distribution stats.
            p50 = int(statistics.median(counts))
            p90 = int(_percentile(counts, 90))
            max_val = max(counts)

            flagged_run_ids = [rid for rid, _ in flagged]
            flagged_counts = [c for _, c in flagged]

            # 5. Build evidence list.
            evidence: list[str] = [
                f"project={project}",
                f"flagged_runs={len(flagged)} (threshold={READ_THRESHOLD})",
                f"distribution: p50={p50}, p90={p90}, max={max_val}",
                f"total_labeled_runs_with_mutations={len(run_counts)}",
            ]
            # Add sample flagged run IDs (up to 5).
            for rid, cnt in flagged[:5]:
                evidence.append(f"  run={rid} nav_before_mutation={cnt}")

            # 6. Proposed remediation.
            remediation = _REMEDIATION_TEMPLATE.format(
                project=project,
                p50=p50,
                p90=p90,
                max=max_val,
                flagged_runs=len(flagged),
                threshold=READ_THRESHOLD,
            )

            # 7. Signature — stable anchor is the project (NOT a run_id).
            signature = f"{project}:{self.DETECTOR_NAME}:{project}"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="file_reads_to_finding",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(flagged),
                    evidence=evidence,
                    run_ids=flagged_run_ids,
                    proposed_remediation=remediation,
                    extra={
                        "remediation_rung": "automate",
                        "remediation_rung_justification": (
                            "Root cause is agent navigation behavior (emergent, not "
                            "deterministic), so we cannot Eliminate it via code change. "
                            "AUTOMATE is achievable: inject a key-file index at session "
                            "start so navigation becomes O(lookup) not O(search). "
                            "INFORM (CLAUDE.md hints) is available as an interim measure "
                            "if automation is not yet wired up."
                        ),
                        "p50": p50,
                        "p90": p90,
                        "max": max_val,
                        "flagged_run_ids": flagged_run_ids,
                        "flagged_counts": flagged_counts,
                        "all_run_counts": {rid: c for rid, c in run_counts},
                        "read_threshold": READ_THRESHOLD,
                        "min_flagged_runs": MIN_FLAGGED_RUNS,
                    },
                )
            )

        return candidates


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nav_events_before_first_mutation(
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[int, bool]:
    """Count navigation events before the first mutation for a run.

    Returns (nav_count, had_mutation):
      nav_count   — number of nav tool events before first Edit/Write
      had_mutation — True if the run had at least one Edit/Write event

    Stops scanning the moment it finds the first mutation (Edit/Write).
    """
    events = conn.execute(
        """
        SELECT seq, raw_json
        FROM run_events
        WHERE run_id = ?
        ORDER BY seq
        """,
        (run_id,),
    ).fetchall()

    nav_count = 0
    for _seq, raw_json in events:
        try:
            data = json.loads(raw_json).get("data", {})
        except (json.JSONDecodeError, AttributeError):
            continue
        tool = data.get("tool_name", "")
        if tool in _MUTATION_TOOLS:
            return nav_count, True
        if tool in _NAV_TOOLS:
            nav_count += 1

    # No mutation found — run had no Edit/Write.
    return nav_count, False


def _percentile(values: list[int], p: int) -> float:
    """Compute the p-th percentile of a list of integers (0–100).

    Uses nearest-rank method.  Returns 0.0 for empty lists.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    # nearest rank
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return float(sorted_vals[idx])
