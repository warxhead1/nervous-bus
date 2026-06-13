"""detectors/reread_same_file.py — Tier-1 reread-same-file detector.

Detects context churn: within a single run, the same file path is Read more
than a threshold number of times.  Chronic re-reading of a file signals that
the agent lacks a persistent memory/summary of that file's key facts, so it
reconstructs context by re-reading on every turn.

Algorithm
=========
1. For every run_id (gate on labeled_at IS NOT NULL when checking outcome,
   but detect over ALL runs — unlabeled runs still exhibit the pattern):

   a. Query run_events for tool_name = 'Read' events.
   b. Extract the file_path from data.tool_summary (JSON {"file_path": "..."})
      or, when tool_summary is truncated/absent, from a regex scan of the
      raw_json payload.
   c. Count occurrences of each unique (run_id, normalized_path).
   d. If count > REREAD_THRESHOLD: emit a PatternCandidate.

2. Aggregate across runs: if the same file is re-read excessively in N≥1 runs,
   the cross-run recurrence dedup (base.find_or_create_issue) tracks the durable
   pattern.

Signature
=========
    f"{project}:reread_same_file:{norm_path}"

    norm_path = os.path.normpath(file_path).  This collapses worktree-local
    paths that differ only in worktree slug prefix to the same anchor — e.g.
    both .claude/worktrees/wf_abc/adapters/foo.py and
         .claude/worktrees/wf_def/adapters/foo.py
    resolve to the same suffix if the file *content* is what the agent needs.

    CRITICAL: no run_id in signature (dedup across runs is the point).

Remediation ladder
==================
AUTOMATE-rung: chronic re-reads of the same file mean the agent lacks a
persistent memory/summary.  The fix is to pin the file's key facts into
CLAUDE.md, a memory entry, or a project skill so the agent's context already
has the facts it needs.  This can be done autonomously (emit a CLAUDE.md
append proposal) when recurrence_count reaches a confidence threshold.

Inform-rung remediation (just warning the user) would be INSUFFICIENT here
because the root cause — no persistent fact cache — is mechanically fixable.
We record remediation_rung="automate" and explain why eliminate is not
applicable (we cannot remove the file or the need to read it, but we CAN
pre-populate context so re-reads stop happening).

Usage
=====
    import sqlite3
    from detectors.reread_same_file import RereadSameFileDetector

    conn = sqlite3.connect("~/.cache/nervous-bus/reflex/runs.db")
    detector = RereadSameFileDetector(conn)
    candidates = detector.run()
    for c in candidates:
        payload = detector.emit_candidate(c)
        print(payload)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate


# ── Constants ─────────────────────────────────────────────────────────────────

#: A file must be Read more than this many times within a single run to fire.
#: Threshold is exclusive: count > REREAD_THRESHOLD → fire.
REREAD_THRESHOLD: int = 3

#: Tool names whose tool_summary contains a "file_path" key and represents a
#: file-read operation.  Read is the primary target; the set is kept narrow to
#: avoid false-positives from Edit/Write (which also carry file_path but are
#: write operations, not context-reconstruction reads).
_READ_TOOL_NAMES: frozenset[str] = frozenset({"Read"})

# Regex to extract file_path from a possibly-truncated JSON string such as
#   {"file_path": "/absolute/path/to/file.py", "offset": 0, ...}
# The value is captured up to the first closing quote (handles truncation).
_FILE_PATH_RE = re.compile(r'"file_path"\s*:\s*"([^"]+)"')

# Automate-rung remediation template
_REMEDIATION_TEMPLATE = (
    "Automate-rung: file '{path}' was Read {count} times in run '{run_id}' "
    "(project '{project}').  Root cause: agent re-reads the file to reconstruct "
    "context that is not cached elsewhere.  Fix: append a summary of '{path}' "
    "key facts to CLAUDE.md or create a memory/skill entry so the agent's initial "
    "context already contains the needed information.  This is mechanically "
    "automatable — a CLAUDE.md append hook can be triggered when "
    "reread_count(path) > {threshold} for the {n_runs} run(s) that showed the "
    "pattern.  Eliminate-rung is not applicable: we cannot remove the file or "
    "the need to read it; we CAN remove the need to re-read it by pre-caching."
)


# ── Helper ────────────────────────────────────────────────────────────────────

def _extract_file_path(tool_summary: Optional[str], raw_json: Optional[str]) -> Optional[str]:
    """Extract the file_path from a Read event's tool_summary or raw_json.

    Tries JSON parse first, then falls back to regex on the raw string.
    Returns None if no path can be extracted.
    """
    # Strategy 1: tool_summary is a well-formed JSON dict
    if tool_summary:
        try:
            ts = json.loads(tool_summary)
            if isinstance(ts, dict) and "file_path" in ts:
                return str(ts["file_path"])
        except (json.JSONDecodeError, ValueError):
            pass
        # Strategy 2: regex on possibly-truncated tool_summary
        m = _FILE_PATH_RE.search(tool_summary)
        if m:
            return m.group(1)

    # Strategy 3: scan the whole raw_json envelope (slower, last resort)
    if raw_json:
        try:
            envelope = json.loads(raw_json)
            # data.tool_summary
            data = envelope.get("data", {})
            ts_in_data = data.get("tool_summary", "")
            if ts_in_data:
                try:
                    ts_dict = json.loads(ts_in_data)
                    if isinstance(ts_dict, dict) and "file_path" in ts_dict:
                        return str(ts_dict["file_path"])
                except (json.JSONDecodeError, ValueError):
                    pass
                m2 = _FILE_PATH_RE.search(ts_in_data)
                if m2:
                    return m2.group(1)
        except (json.JSONDecodeError, ValueError):
            pass
        # Final fallback: regex on raw envelope string
        m3 = _FILE_PATH_RE.search(raw_json)
        if m3:
            return m3.group(1)

    return None


def _normalize_path(file_path: str) -> str:
    """Normalize a file path to a stable anchor.

    Strips worktree slug prefixes of the form
      /.../.claude/worktrees/<slug>/rest/of/path
    down to /rest/of/path so the same logical file across different worktrees
    shares one signature anchor.

    Falls back to os.path.normpath if no worktree pattern matches.
    """
    norm = os.path.normpath(file_path)
    # Match .claude/worktrees/<slug>/ or .worktrees/<slug>/
    m = re.search(r"(?:\.claude/worktrees|\.worktrees)/[^/]+/(.+)$", norm)
    if m:
        return m.group(1)
    return norm


# ── Detector ──────────────────────────────────────────────────────────────────

class RereadSameFileDetector(BaseDetector):
    """Detect context churn caused by re-reading the same file many times per run.

    See module docstring for the full algorithm.
    """

    DETECTOR_NAME = "reread_same_file"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Scan run_events for runs where the same file is Read > REREAD_THRESHOLD times.

        Parameters
        ----------
        conn : sqlite3.Connection
            Live connection to runs.db (same connection passed to __init__).
            Do NOT close it.

        Returns
        -------
        list[PatternCandidate]
            One candidate per (project, normalized_file_path) that fired.
        """
        # Pull all Read-tool events, ordered for per-run analysis.
        cur = conn.execute(
            """
            SELECT re.run_id,
                   r.project,
                   json_extract(re.raw_json, '$.data.tool_summary') AS tool_summary,
                   re.raw_json
            FROM run_events AS re
            JOIN runs AS r USING (run_id)
            WHERE json_extract(re.raw_json, '$.data.tool_name') IN ({placeholders})
            ORDER BY re.run_id, re.seq
            """.format(
                placeholders=",".join("?" * len(_READ_TOOL_NAMES))
            ),
            tuple(_READ_TOOL_NAMES),
        )
        rows = cur.fetchall()

        # Accumulate per-(run_id, norm_path) counts.
        # Structure: {(project, norm_path): {run_id: [(raw_path, count)]}}
        # Simplified: track (run_id, norm_path) → list of raw_paths + count.
        #
        # We want: for each run, which norm_paths exceeded the threshold?
        # Then aggregate across runs for cross-run evidence.

        # {run_id: {norm_path: {"project": str, "raw_paths": [str], "count": int}}}
        per_run: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
            lambda: {"project": "", "raw_paths": [], "count": 0}
        ))

        for run_id, project, tool_summary, raw_json in rows:
            raw_path = _extract_file_path(tool_summary, raw_json)
            if not raw_path:
                continue
            norm = _normalize_path(raw_path)
            entry = per_run[run_id][norm]
            entry["project"] = project or ""
            entry["raw_paths"].append(raw_path)
            entry["count"] += 1

        # Now find (norm_path, project) combinations that fired in one or more runs.
        # Group by (project, norm_path) to enable cross-run dedup via signature.
        #
        # {(project, norm_path): [{"run_id": ..., "count": ..., "raw_paths": [...]}]}
        cross_run: dict[tuple[str, str], list[dict]] = defaultdict(list)

        for run_id, paths in per_run.items():
            for norm_path, entry in paths.items():
                if entry["count"] > REREAD_THRESHOLD:
                    cross_run[(entry["project"], norm_path)].append(
                        {
                            "run_id": run_id,
                            "count": entry["count"],
                            "raw_paths": entry["raw_paths"],
                        }
                    )

        candidates: list[PatternCandidate] = []

        for (project, norm_path), run_hits in cross_run.items():
            # Sort by count descending so evidence leads with the worst run.
            run_hits_sorted = sorted(run_hits, key=lambda x: -x["count"])
            worst = run_hits_sorted[0]
            total_occurrences = sum(h["count"] for h in run_hits_sorted)
            run_ids = [h["run_id"] for h in run_hits_sorted]
            max_count = worst["count"]

            evidence = [
                f"file={norm_path}",
                f"project={project}",
                f"max_reread_count={max_count} (threshold={REREAD_THRESHOLD})",
                f"runs_fired={len(run_hits_sorted)}",
            ]
            for h in run_hits_sorted[:5]:
                evidence.append(f"run={h['run_id']} reread_count={h['count']}")

            # Signature: stable across runs, no run_id.
            signature = f"{project}:{self.DETECTOR_NAME}:{norm_path}"

            remediation = _REMEDIATION_TEMPLATE.format(
                path=norm_path,
                count=max_count,
                run_id=worst["run_id"],
                project=project,
                threshold=REREAD_THRESHOLD,
                n_runs=len(run_hits_sorted),
            )

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="reread_same_file",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=total_occurrences,
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=remediation,
                    extra={
                        "norm_path": norm_path,
                        "max_reread_count": max_count,
                        "threshold": REREAD_THRESHOLD,
                        "runs_fired": len(run_hits_sorted),
                        "remediation_rung": "automate",
                        "remediation_rung_justification": (
                            "Eliminate is not applicable — we cannot remove the file or "
                            "the need to read it.  Automate is achievable: append the "
                            "file's key facts to CLAUDE.md or a memory/skill entry so "
                            "re-reads stop happening.  Inform-only would be a band-aid."
                        ),
                    },
                )
            )

        return candidates
