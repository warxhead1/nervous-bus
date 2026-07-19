"""detectors/harness_change_watch.py — A1: the harness-change watch (sensor half).

Harness Engineering Adoption Map, Part 2 Tier 1, A1. This is purely OBSERVATIONAL:
it tags runs and surfaces a digest line so a human (or a later convention-doc /
soak-gate half, tracked separately) can see harness churn happen. It does NOT
block, gate, or enforce anything.

WHY THIS MATTERS
================
"Harness artifacts" are the files that shape how EVERY future agent in a project
behaves: CLAUDE.md / AGENTS.md (standing instructions), .claude/hooks/** and
.claude/skills/** (automated behavior + packaged workflows), settings.json
(permissions/env), and ~/.config/hermes/routing.toml (cross-project routing) when
visible in the run's diff surface. A change to any of these has a much larger
blast radius than an ordinary code edit — every subsequent session inherits it
silently. Before any soak-gate discipline can be built around that fact, there
has to be a SENSOR that notices the change happened at all. That is this detector.

SIGNAL (already captured — no new capture invented)
====================================================
Two sources, both already flowing through the existing activity stream:

1. Edit/Write tool calls whose file_path (tool_summary/tool_response_summary,
   same extraction convention as edit_build_fail_revert._extract_file_path)
   matches a harness-artifact pattern (see HARNESS_PATTERNS below).
2. Bash tool calls that look like a git operation (the command text contains
   "git") whose tool_summary command text OR tool_response_summary (stdout,
   e.g. `git status`/`git diff --stat`/`git show` output) mentions a harness
   artifact basename or path fragment — this is the "diff surface" half: we
   are not diffing anything ourselves, we are reading text the agent already
   produced and the recorder already stored.

Both sources are attributed to the run they occurred in; each run also carries
project + session_id (from the runs table) so the digest line can name the
run/session that made the change.

Grouping / signature
=====================
Candidates are grouped by (project, harness_label) — NOT by run_id — so the
Kyoko recurrence/prevalence layer (base.find_or_create_issue) tracks "how often
does THIS project's CLAUDE.md/hooks/skills/settings churn", exactly like
reread_same_file groups by (project, norm_path). run_ids carries every run that
touched this harness artifact so per-run tagging + the digest's "recent harness
changes" line can both be reconstructed from detector_hits (run_id, ts) joined
back to runs (project, session_id) — no extra state needed beyond what
BaseDetector.record_hit already persists.

Remediation ladder
==================
INFORM only. There is no deterministic fix to propose here — the point of this
detector is to make harness churn VISIBLE, not to prevent it. (The soak-gate
convention half, which WOULD prescribe a review/wait step before a harness
change takes effect, is a separate piece of work; this detector is explicitly
scoped to sensing, not enforcing.)
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate
from detectors.dispatch_lineage import load_run_events

# ── Harness artifact patterns ────────────────────────────────────────────────
#
# Matched against a file_path (Edit/Write) OR arbitrary text (Bash command /
# stdout) via `.search()` — deliberately substring-permissive since the Bash
# path is reading free-form command/diff text, not a structured path field.
HARNESS_PATTERNS: dict[str, re.Pattern] = {
    "CLAUDE.md": re.compile(r"(?:^|[\s/])CLAUDE\.md\b"),
    "AGENTS.md": re.compile(r"(?:^|[\s/])AGENTS\.md\b"),
    ".claude/hooks": re.compile(r"\.claude/hooks/"),
    ".claude/skills": re.compile(r"\.claude/skills/"),
    "settings.json": re.compile(r"\.claude/settings(?:\.local)?\.json\b"),
    "hermes/routing.toml": re.compile(r"\.config/hermes/routing\.toml\b"),
}

# Order matters only for evidence readability; a path can only match one label
# in practice since the patterns are mutually exclusive by construction.
_LABELS_IN_ORDER = list(HARNESS_PATTERNS.keys())


def classify_harness_text(text: str) -> Optional[str]:
    """Return the harness label a path/text fragment matches, or None.

    Substring search (not full-path anchoring) — callers pass either a
    structured file_path (Edit/Write) or free-form command/stdout text
    (Bash git operations), and both need the same permissive match.
    """
    if not text:
        return None
    for label in _LABELS_IN_ORDER:
        if HARNESS_PATTERNS[label].search(text):
            return label
    return None


# ── tool_summary / tool_response_summary parsing (shared convention) ────────

def _parse_summary(s: str) -> dict:
    """Parse a tool_summary/tool_response_summary JSON string. {} on failure.

    Mirrors detectors/edit_build_fail_revert.py's _parse_summary — duplicated
    rather than imported to keep this detector's dependency surface to the
    generic dispatch_lineage substrate only.
    """
    if not s:
        return {}
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_edit_file_path(ts: dict, rs: dict) -> str:
    """Extract the edited file path from tool_summary/tool_response_summary."""
    return (
        ts.get("file_path", "")
        or rs.get("filePath", "")
        or ts.get("file", "")
        or ""
    )


def _extract_bash_command(ts: dict, raw_ts: str) -> str:
    """Extract the bash command string, or the raw summary if not JSON."""
    cmd = ts.get("command", "")
    if cmd:
        return cmd
    return raw_ts or ""


# ── Detector ──────────────────────────────────────────────────────────────────

class HarnessChangeWatchDetector(BaseDetector):
    """Tag runs that touched a harness artifact; purely observational (no gate)."""

    DETECTOR_NAME = "harness_change_watch"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        runs_cur = conn.execute(
            """
            SELECT run_id, project, session_id
            FROM runs
            WHERE close_reason IS NOT NULL
            ORDER BY started
            """
        )
        runs = [(r[0], r[1], r[2]) for r in runs_cur.fetchall()]

        # {(project, harness_label): [{"run_id":..., "session_id":..., "via":..., "detail":...}]}
        cross_run: dict[tuple[str, str], list[dict]] = defaultdict(list)

        for run_id, project, session_id in runs:
            events = load_run_events(conn, run_id)
            if not events:
                continue

            seen_labels_this_run: set[str] = set()

            for ev in events:
                tool_name = ev.get("tool_name", "")
                raw_ts = ev.get("tool_summary", "") or ""
                raw_rs = ev.get("tool_response_summary", "") or ""

                if tool_name in ("Edit", "Write"):
                    ts = _parse_summary(raw_ts)
                    rs = _parse_summary(raw_rs)
                    file_path = _extract_edit_file_path(ts, rs)
                    label = classify_harness_text(file_path)
                    if label and label not in seen_labels_this_run:
                        seen_labels_this_run.add(label)
                        cross_run[(project or "", label)].append({
                            "run_id": run_id,
                            "session_id": session_id or "",
                            "via": "edit",
                            "detail": file_path,
                        })
                    continue

                if tool_name == "Bash":
                    ts = _parse_summary(raw_ts)
                    cmd = _extract_bash_command(ts, raw_ts)
                    if not cmd or "git" not in cmd.lower():
                        continue
                    # Diff-surface signal: scan the command text itself AND
                    # whatever stdout/output the recorder already captured
                    # (tool_response_summary) for a harness artifact mention —
                    # e.g. `git status`/`git diff --stat` naming the changed file.
                    combined = f"{cmd}\n{raw_rs}"
                    label = classify_harness_text(combined)
                    if label and label not in seen_labels_this_run:
                        seen_labels_this_run.add(label)
                        cross_run[(project or "", label)].append({
                            "run_id": run_id,
                            "session_id": session_id or "",
                            "via": "git_diff_surface",
                            "detail": cmd[:200],
                        })

        candidates: list[PatternCandidate] = []
        for (project, label), hits in cross_run.items():
            run_ids = [h["run_id"] for h in hits]

            evidence = [
                f"harness_artifact={label}",
                f"project={project}",
                f"runs_touched={len(hits)}",
            ]
            for h in hits[:8]:
                evidence.append(
                    f"run={h['run_id']} session={h['session_id'] or '?'} "
                    f"via={h['via']} detail={h['detail'][:80]}"
                )

            signature = f"{project}:{self.DETECTOR_NAME}:{label}"

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="harness_change_watch",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(hits),
                    evidence=evidence,
                    run_ids=run_ids,
                    proposed_remediation=None,
                    extra={
                        "harness_artifact": label,
                        "runs_touched": len(hits),
                        "remediation_rung": "inform",
                        "remediation_rung_justification": (
                            "Purely observational — no deterministic fix exists for "
                            "'a harness file changed'. This detector's job is visibility "
                            "(the sensor half of the soak-gate discipline); the "
                            "convention/enforcement half is separate work and is "
                            "explicitly NOT implemented here."
                        ),
                    },
                )
            )

        return candidates
