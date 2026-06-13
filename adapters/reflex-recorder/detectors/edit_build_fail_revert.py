"""detectors/edit_build_fail_revert.py — Tier-1 edit-build-fail-revert thrash detector.

Detects the n-gram: Edit/Write -> Build(Bash) -> Failure(error output or re-edit
without success) -> Revert(git checkout/restore/reset OR re-Edit of the same path).

Algorithm
=========
For each run (ordered by seq):
1. Slide through events looking for "thrash cycles":
   a. EDIT event (tool_name in Edit, Write) — record file_path if extractable, else cwd.
   b. BUILD event (Bash command matching BUILD_KEYWORDS) — record command.
   c. FAIL signal — stdout/stderr matching FAIL_PATTERNS, OR the build was backgrounded
      (assistantAutoBackgrounded) and immediately followed by another edit.
   d. REVERT event — git checkout/restore/reset of a path (Bash matching REVERT_KEYWORDS)
      OR another Edit/Write of the *same* file_path that was edited in step (a).

A "cycle" is a complete a→b→c→d sequence within a single run (no cross-run
matching; each cycle must complete within MAX_CYCLE_SPAN events of the edit).
A run with >=1 complete cycle fires; runs with >=2 cycles are flagged as "strong thrash".

Stable anchor
=============
The stable_anchor encodes (project, area) where area = the normalized file path
(if recoverable) or the cwd of the editing events. This lets the dedup layer
aggregate recurring thrash on the same code area across runs, rather than treating
each run independently.

signature = f"{project}:edit_build_fail_revert:{area}"

REMEDIATION LADDER
==================
The appropriate rung depends on the pattern of failing edits:
- If the same build error class recurs (e.g., "command not found: cargo" → missing
  PATH, or a known API mismatch), an AUTOMATE fix is possible (pre-edit lint / PATH
  check).
- If the failing edit class is not deterministically classifiable from available
  data, we fall to INFORM (raise a pattern.discovered event and annotate the run).

Because the current run_events data does NOT reliably capture full build error text
(stdout is often truncated or empty due to backgrounding), we cannot always classify
the error class. This detector therefore defaults to INFORM, but upgrades to AUTOMATE
when it detects the specific "command not found" pattern (a trivially fixable PATH
issue) or repeated identical build commands (suggesting a known-broken API usage).

Usage
=====
    import sqlite3
    from detectors.edit_build_fail_revert import EditBuildFailRevertDetector

    conn = sqlite3.connect("~/.cache/nervous-bus/reflex/runs.db")
    detector = EditBuildFailRevertDetector(conn)
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
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate


# ── Keyword constants ──────────────────────────────────────────────────────────

# Bash command content that indicates a build/test invocation.
BUILD_KEYWORDS = (
    "cargo build",
    "cargo test",
    "cargo check",
    "cargo run",
    "go build",
    "go test",
    "go run",
    "pytest",
    "python -m pytest",
    "npm run build",
    "npm run test",
    "npm test",
    "make test",
    "make build",
    "mvn test",
    "mvn build",
    "gradle test",
    "gradle build",
    "tsc ",            # TypeScript compiler
    "python setup.py",
)

# Patterns in stdout/stderr that definitively indicate a build/test failure.
# These are checked case-insensitively against the combined stdout+stderr text.
FAIL_PATTERNS = (
    "error[e",          # Rust error[E...] diagnostics
    "error[",           # Generic error[ pattern (Go, Rust)
    " failed",          # "test failed", "build failed", etc.
    "fail\n",           # standalone FAIL line (Go test)
    "fail\t",           # FAIL<TAB>... prefix in Go test output (e.g. FAIL\tgithub.com/...)
    "fail ",            # FAIL prefix in Go test output
    "^fail$",           # Exact FAIL (multiline)
    "panic:",           # Go/Rust panics
    "cannot ",          # Go "cannot use X as Y"
    "undefined: ",      # Go "undefined: SomeName"
    "exit status 1",    # Shell exit status
    "command not found",  # Missing binary (PATH issue)
    "no such file",     # Missing file
    "syntax error",     # Syntax problems
    "compilation failed",
    "does not compile",
    "build constraints exclude",
    " error:",          # Generic "error:" on a line (gcc, clang, etc.)
    "error: aborting",  # Rust aborting due to errors
    "test result: fail", # Rust test output
    "tests failed",
)

# Bash command content indicating a git revert operation.
REVERT_KEYWORDS = (
    "git checkout ",
    "git restore ",
    "git reset ",
    "git revert ",
    "git stash",
)

# Maximum number of events between edit and revert to count as the same cycle.
MAX_CYCLE_SPAN = 20

# Minimum number of events between an edit and its build command to avoid false
# positives from normal "edit then build" flows (we want edit -> build -> fail -> edit).
# Actually we want ANY edit followed by a build; the FAIL determines the cycle.
# This constant limits lookahead for the build after an edit.
MAX_EDIT_TO_BUILD_SPAN = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_summary(s: str) -> dict:
    """Parse a tool_summary or tool_response_summary string into a dict.

    Returns {} on failure. The field is always a JSON string (possibly truncated).
    If it's valid JSON, return the parsed dict; otherwise return {}.
    """
    if not s:
        return {}
    try:
        result = json.loads(s)
        if isinstance(result, dict):
            return result
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_file_path(ts: dict, rs: dict) -> str:
    """Extract the edited file path from tool_summary and tool_response_summary.

    Returns empty string if not determinable.
    """
    return (
        ts.get("file_path", "")
        or rs.get("filePath", "")
        or ts.get("file", "")
        or ""
    )


def _normalize_area(file_path: str, cwd: str, project: str) -> str:
    """Normalise the 'area' identifier for stable signature anchoring.

    Prefers file_path (most specific). Falls back to cwd. Strips worktree-
    specific path prefixes so the same code area across worktrees maps to the
    same anchor.

    e.g.  /home/eric/projects/tachyonac-engine/.claude/worktrees/wf_abc/internal/sim/foo.go
          → tachyonac-engine/internal/sim/foo.go
    """
    path = file_path or cwd or ""
    if not path:
        return project

    # Strip worktree prefixes: .../projects/<project>/.claude/worktrees/<slug>/... -> rest
    worktree_re = re.compile(
        r".*/projects/[^/]+/(?:\.claude/worktrees|\.worktrees)/[^/]+/(.*)"
    )
    m = worktree_re.match(path)
    if m:
        return m.group(1) or project

    # Strip main project root: .../projects/<project>/... -> rest
    project_re = re.compile(r".*/projects/[^/]+/(.*)")
    m = project_re.match(path)
    if m:
        return m.group(1) or project

    # Fallback: last 2 components of the path
    parts = path.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else path


def _is_build_command(cmd: str) -> bool:
    lower = cmd.lower()
    return any(kw in lower for kw in BUILD_KEYWORDS)


def _is_revert_command(cmd: str) -> bool:
    lower = cmd.lower()
    return any(kw in lower for kw in REVERT_KEYWORDS)


def _has_fail_output(stdout: str, stderr: str) -> bool:
    """Return True if stdout+stderr contains a recognised failure pattern."""
    combined = (stdout + "\n" + stderr).lower()
    return any(p.lower() in combined for p in FAIL_PATTERNS)


def _is_command_not_found(stdout: str, stderr: str) -> bool:
    combined = (stdout + "\n" + stderr).lower()
    return "command not found" in combined or "no such file or directory" in combined


# ── Detector ──────────────────────────────────────────────────────────────────

class EditBuildFailRevertDetector(BaseDetector):
    """Detect edit->build->fail->revert thrash cycles within a run.

    See module docstring for the full algorithm and remediation ladder
    justification.
    """

    DETECTOR_NAME = "edit_build_fail_revert"

    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Scan all runs for edit-build-fail-revert thrash cycles.

        Only considers labeled runs (labeled_at IS NOT NULL) for the prevalence
        denominator, but fires on ALL runs (including unlabeled) so newly
        recorded sessions are captured immediately.
        """
        # Fetch all run_ids with their projects in chronological order.
        runs_cur = conn.execute(
            """
            SELECT run_id, project
            FROM runs
            ORDER BY started
            """
        )
        runs = [(row[0], row[1]) for row in runs_cur.fetchall()]

        if not runs:
            return []

        # Fetch all run_events in one query, grouped by run_id.
        events_cur = conn.execute(
            """
            SELECT run_id, seq, raw_json
            FROM run_events
            ORDER BY run_id, seq
            """
        )
        # Group events by run_id
        from collections import defaultdict
        run_events_map: dict[str, list[dict]] = defaultdict(list)
        for run_id, seq, raw_json in events_cur.fetchall():
            try:
                raw = json.loads(raw_json)
            except (json.JSONDecodeError, ValueError):
                continue
            data = raw.get("data", {})
            run_events_map[run_id].append({
                "seq": seq,
                "tool_name": data.get("tool_name", ""),
                "cwd": data.get("cwd", ""),
                "tool_summary": data.get("tool_summary", ""),
                "tool_response_summary": data.get("tool_response_summary", ""),
                "project": data.get("project", ""),
            })

        # Map run_id -> project for lookup
        run_project_map = {rid: proj for rid, proj in runs}

        candidates: list[PatternCandidate] = []

        for run_id, project in runs:
            events = run_events_map.get(run_id, [])
            if not events:
                continue

            cycles = _find_thrash_cycles(events)
            if not cycles:
                continue

            # Determine the dominant "area" for the signature: most common area
            # across all cycles.
            area_counts: dict[str, int] = {}
            for cycle in cycles:
                area = cycle["area"]
                area_counts[area] = area_counts.get(area, 0) + 1
            dominant_area = max(area_counts, key=lambda a: area_counts[a])

            # Determine remediation rung
            has_automate_evidence = any(c.get("command_not_found") for c in cycles)
            repeated_same_build = _has_repeated_identical_build(cycles)

            if has_automate_evidence:
                remediation_rung = "automate"
                remediation_note = (
                    "AUTOMATE: 'command not found' detected — the build tool is not "
                    "on PATH. A pre-edit hook can check/fix PATH before the agent "
                    "attempts a build (e.g., ensure ~/.cargo/bin is in PATH before "
                    "any cargo invocation)."
                )
            elif repeated_same_build:
                remediation_rung = "automate"
                remediation_note = (
                    "AUTOMATE: identical build command repeated across multiple "
                    "edit-fail-revert cycles. This suggests a known-broken API usage "
                    "or missing dependency. A pre-commit lint/check skill can catch "
                    "this before the thrash loop begins."
                )
            else:
                remediation_rung = "inform"
                remediation_note = (
                    "INFORM: edit-build-fail-revert thrash detected but the specific "
                    "error class cannot be determined from available telemetry "
                    "(build output is often truncated or backgrounded). "
                    "Surface this pattern as a pattern.discovered event so the "
                    "developer can investigate the recurring failure. "
                    "To upgrade to AUTOMATE: capture full build stdout/stderr in "
                    "run_events (increase tool_response_summary length limit)."
                )

            signature = f"{project}:{self.DETECTOR_NAME}:{dominant_area}"

            # Build evidence list
            evidence: list[str] = [
                f"project={project}",
                f"run_id={run_id}",
                f"thrash_cycles={len(cycles)}",
                f"area={dominant_area}",
            ]
            for i, cycle in enumerate(cycles[:3]):  # Cap evidence at 3 cycles
                evidence.append(
                    f"cycle_{i+1}: edit@seq{cycle['edit_seq']}"
                    f" -> build@seq{cycle['build_seq']}"
                    f" -> fail@seq{cycle['fail_seq']}"
                    f" -> revert@seq{cycle['revert_seq']}"
                )
            if len(cycles) > 3:
                evidence.append(f"... and {len(cycles) - 3} more cycles")

            candidates.append(
                PatternCandidate(
                    project=project,
                    pattern_name="edit_build_fail_revert",
                    signature=signature,
                    detector=self.DETECTOR_NAME,
                    occurrences=len(cycles),
                    evidence=evidence,
                    run_ids=[run_id],
                    proposed_remediation=remediation_note,
                    extra={
                        "thrash_cycles": len(cycles),
                        "strong_thrash": len(cycles) >= 2,
                        "dominant_area": dominant_area,
                        "remediation_rung": remediation_rung,
                        "command_not_found": has_automate_evidence,
                        "repeated_same_build": repeated_same_build,
                        "cycles": [
                            {
                                "area": c["area"],
                                "edit_seq": c["edit_seq"],
                                "build_seq": c["build_seq"],
                                "fail_seq": c["fail_seq"],
                                "revert_seq": c["revert_seq"],
                                "build_cmd": c.get("build_cmd", ""),
                            }
                            for c in cycles
                        ],
                    },
                )
            )

        return candidates


# ── Cycle detection helpers ───────────────────────────────────────────────────

def _find_thrash_cycles(events: list[dict]) -> list[dict]:
    """Find all complete edit->build->fail->revert cycles in an event list.

    Returns a list of cycle dicts with keys:
      area, edit_seq, build_seq, fail_seq, revert_seq,
      build_cmd, command_not_found
    """
    cycles = []
    n = len(events)
    i = 0

    while i < n:
        ev = events[i]
        tool = ev["tool_name"]

        # ── Step 1: look for an Edit/Write event ──────────────────────────────
        if tool not in ("Edit", "Write"):
            i += 1
            continue

        ts = _parse_summary(ev["tool_summary"])
        rs = _parse_summary(ev["tool_response_summary"])
        file_path = _extract_file_path(ts, rs)
        cwd = ev.get("cwd", "")
        project = ev.get("project", "")
        area = _normalize_area(file_path, cwd, project)
        edit_seq = ev["seq"]

        # ── Step 2: look ahead for a build command ────────────────────────────
        build_idx = None
        build_cmd = ""
        for j in range(i + 1, min(i + 1 + MAX_EDIT_TO_BUILD_SPAN, n)):
            jev = events[j]
            if jev["tool_name"] != "Bash":
                continue
            jts = _parse_summary(jev["tool_summary"])
            cmd = jts.get("command", "") or ""
            if _is_build_command(cmd):
                build_idx = j
                build_cmd = cmd
                break

        if build_idx is None:
            i += 1
            continue

        # ── Step 3: look for failure signal after the build ───────────────────
        fail_idx = None
        cmd_not_found = False

        # Check the build event's own response for failure output
        build_ev = events[build_idx]
        brs = _parse_summary(build_ev["tool_response_summary"])
        stdout = brs.get("stdout", "") or ""
        stderr = brs.get("stderr", "") or ""
        if _has_fail_output(stdout, stderr):
            fail_idx = build_idx
            cmd_not_found = _is_command_not_found(stdout, stderr)

        # If no explicit fail output, look ahead for another Bash event with
        # failure output, or for a revert signal (implicit failure).
        if fail_idx is None:
            for j in range(build_idx + 1, min(build_idx + 1 + MAX_CYCLE_SPAN, n)):
                jev = events[j]
                if jev["tool_name"] == "Bash":
                    jrs = _parse_summary(jev["tool_response_summary"])
                    jts = _parse_summary(jev["tool_summary"])
                    jcmd = jts.get("command", "") or ""
                    jstdout = jrs.get("stdout", "") or ""
                    jstderr = jrs.get("stderr", "") or ""
                    if _has_fail_output(jstdout, jstderr):
                        fail_idx = j
                        cmd_not_found = _is_command_not_found(jstdout, jstderr)
                        break
                    # Implicit failure: another build command immediately after
                    # the first (agent retrying without editing = not thrash, skip).
                    # But if there's an edit in between, that's the revert phase.
                elif jev["tool_name"] in ("Edit", "Write"):
                    # An edit right after a build (without success output) is an
                    # implicit failure signal: the agent had to revise.
                    jts = _parse_summary(jev["tool_summary"])
                    jrs = _parse_summary(jev["tool_response_summary"])
                    jfp = _extract_file_path(jts, jrs)
                    if jfp == file_path and file_path:
                        # Re-edit of the SAME file = implicit fail + revert
                        fail_idx = j  # The re-edit IS both the fail evidence and revert
                        break

        if fail_idx is None:
            i += 1
            continue

        # ── Step 4: look for a revert after the fail ──────────────────────────
        revert_idx = None
        revert_start = fail_idx + 1 if fail_idx != build_idx else build_idx + 1

        # If fail_idx points to an Edit event (implicit fail+revert), that edit
        # IS the revert; record it directly.
        if fail_idx < n and events[fail_idx]["tool_name"] in ("Edit", "Write"):
            revert_idx = fail_idx

        if revert_idx is None:
            for j in range(revert_start, min(revert_start + MAX_CYCLE_SPAN, n)):
                jev = events[j]
                if jev["tool_name"] in ("Edit", "Write"):
                    jts = _parse_summary(jev["tool_summary"])
                    jrs = _parse_summary(jev["tool_response_summary"])
                    jfp = _extract_file_path(jts, jrs)
                    # Re-edit of same file path = revert (agent undoing work)
                    if jfp == file_path and file_path:
                        revert_idx = j
                        break
                elif jev["tool_name"] == "Bash":
                    jts = _parse_summary(jev["tool_summary"])
                    jcmd = jts.get("command", "") or ""
                    if _is_revert_command(jcmd):
                        revert_idx = j
                        break

        if revert_idx is None:
            i += 1
            continue

        # ── Complete cycle found ───────────────────────────────────────────────
        cycles.append({
            "area": area,
            "edit_seq": edit_seq,
            "build_seq": events[build_idx]["seq"],
            "fail_seq": events[fail_idx]["seq"],
            "revert_seq": events[revert_idx]["seq"],
            "build_cmd": build_cmd[:200],
            "command_not_found": cmd_not_found,
        })

        # Advance past the revert event to avoid double-counting
        i = revert_idx + 1

    return cycles


def _has_repeated_identical_build(cycles: list[dict]) -> bool:
    """Return True if the same build command appears in multiple cycles."""
    if len(cycles) < 2:
        return False
    cmds = [c.get("build_cmd", "").strip() for c in cycles]
    # Normalise whitespace
    cmds = [" ".join(c.split()) for c in cmds if c]
    return len(cmds) >= 2 and len(set(cmds)) < len(cmds)
