"""detectors/reread_same_file.py — Tier-1 reread-same-file detector.

Detects context churn: within a single run, the same file path is read more
than a threshold number of times.  A read is a Read tool call, a Bash
`sed -n 'A,Bp' FILE` slice, or a plain Bash `cat FILE`.  Chronic re-reading of a file signals that
the agent lacks a persistent memory/summary of that file's key facts, so it
reconstructs context by re-reading on every turn.

Algorithm
=========
1. For every run_id (this detector never reads `outcome`, so the null-vs-clean
   rule does not apply; it fires on behaviour shape over all stored — i.e.
   closed — runs, labeled or not):

   a. Query run_events for tool_name = 'Read' and tool_name = 'Bash' events.
   b. For a Read, extract the file_path from data.tool_summary
      (JSON {"file_path": "..."}) or, when tool_summary is truncated/absent,
      from a regex scan of the raw_json payload.
   c. For a Bash call, read data.tool_summary.command.  Split it into
      statements at `;`, `&&`, `||`, `&` and newlines.  A statement counts
      only when it is `sed -n 'A,Bp' FILE...` or `cat FILE...` with no pipe,
      redirect or heredoc.  `sed -i` and sed without a file never count.
      The tool_summary command is cut at a fixed length.  When it is cut, the
      last statement is dropped because its file name may be cut too.
   d. A relative Bash path is joined to data.cwd when there is one.
      Several slices of one file in one Bash call count as one read.
   e. Count occurrences of each unique (run_id, normalized_path).
   f. If count > REREAD_THRESHOLD: emit a PatternCandidate.

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
import shlex
import sqlite3
from collections import defaultdict
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate
from detectors._truncated_json import string_field


# ── Constants ─────────────────────────────────────────────────────────────────

#: A file must be Read more than this many times within a single run to fire.
#: Threshold is exclusive: count > REREAD_THRESHOLD → fire.
REREAD_THRESHOLD: int = 3

#: Tool names whose tool_summary contains a "file_path" key and represents a
#: file-read operation.  Read is the primary target; the set is kept narrow to
#: avoid false-positives from Edit/Write (which also carry file_path but are
#: write operations, not context-reconstruction reads).
_READ_TOOL_NAMES: frozenset[str] = frozenset({"Read"})

#: Bash events are scanned too: `sed -n 'A,Bp' FILE` and `cat FILE` read files.
_BASH_TOOL_NAME = "Bash"
_SCANNED_TOOL_NAMES: frozenset[str] = _READ_TOOL_NAMES | {_BASH_TOOL_NAME}

# A sed script that prints one line range: 5p, 5,20p, 5,$p, $p.
_SED_RANGE_RE = re.compile(r"^(\d+|\$)(,(\d+|\$))?p$")

# Statement separators.  A pipe is deliberately not one: a piped statement is
# ordinary shell use and is dropped whole.
_STATEMENT_SEPARATORS = frozenset({";", ";;", "&&", "||", "&", "\n"})
_DROP_STATEMENT_TOKENS = frozenset({"|", "|&", ">", ">>", ">|", "&>", "&>>", "<", "<<", "<<-", "<<<"})

# Regex to extract file_path from a possibly-truncated JSON string such as
#   {"file_path": "/absolute/path/to/file.py", "offset": 0, ...}
# The value is captured up to the first closing quote (handles truncation).
_FILE_PATH_RE = re.compile(r'"file_path"\s*:\s*"([^"]+)"')

# Automate-rung remediation template
_REMEDIATION_TEMPLATE = (
    "Automate-rung: file '{path}' was read {count} times in run '{run_id}' "
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


def _statements(command: str) -> list[list[str]]:
    """Split a Bash command into statements of shell tokens.

    Unquoted newlines become statement breaks.  Everything after the line that
    opens a heredoc is dropped so the heredoc body is never read as commands.
    Returns [] when the command cannot be tokenised.
    """
    lines, quote, esc, buf, i = [], "", False, [], 0
    while i < len(command):
        ch = command[i]
        if esc:
            esc = False
        elif ch == "\\" and quote != "'":
            esc = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            lines.append("".join(buf))
            buf = []
            i += 1
            continue
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            rest = command[i:].split("\n", 1)[0]
            buf.append(rest)
            break
        buf.append(ch)
        i += 1
    lines.append("".join(buf))

    out: list[list[str]] = []
    for line in lines:
        lex = shlex.shlex(line, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            tokens = list(lex)
        except ValueError:
            continue
        cur: list[str] = []
        for tok in tokens:
            if tok in _STATEMENT_SEPARATORS:
                out.append(cur)
                cur = []
            else:
                cur.append(tok)
        out.append(cur)
    return [st for st in out if st]


def _is_file_arg(tok: str) -> bool:
    return bool(tok) and not tok.startswith("-") and not any(c in tok for c in "*?[]$`~{}")


def _statement_files(tokens: list[str]) -> list[str]:
    """Files read by one statement shaped `sed -n 'A,Bp' FILE...` or `cat FILE...`."""
    if not tokens or any(t in _DROP_STATEMENT_TOKENS or t.startswith((">", "<")) for t in tokens):
        return []
    if tokens[0] == "cat":
        args = tokens[1:]
        files = [a for a in args if not a.startswith("-")]
        return files if files and all(_is_file_arg(f) for f in files) else []
    if tokens[0] == "sed" and len(tokens) >= 4 and tokens[1] == "-n" and _SED_RANGE_RE.match(tokens[2]):
        files = tokens[3:]
        return files if all(_is_file_arg(f) for f in files) else []
    return []


def _bash_read_paths(command: str, truncated: bool = False, cwd: Optional[str] = None) -> list[str]:
    """Distinct files one Bash command reads through sed slices or cat.

    truncated: the command text was cut, so its last statement is dropped.
    Relative paths are joined to cwd when given, else used as written.
    """
    stmts = _statements(command)
    if truncated and stmts:
        stmts = stmts[:-1]
    seen: dict[str, None] = {}
    for st in stmts:
        for f in _statement_files(st):
            if cwd and not os.path.isabs(f):
                f = os.path.join(cwd, f)
            seen[f] = None
    return list(seen)


def _extract_bash_command(tool_summary: Optional[str], raw_json: Optional[str]) -> tuple[str, bool]:
    """Return (command, truncated) for a Bash event; ("", False) when absent."""
    if tool_summary:
        try:
            ts = json.loads(tool_summary)
            if isinstance(ts, dict):
                return str(ts.get("command") or ""), False
        except (json.JSONDecodeError, ValueError):
            pass
        cmd = string_field(tool_summary, "command", partial=True)
        if cmd:
            return cmd, True
    if raw_json:
        cmd = string_field(raw_json, "command", partial=True)
        if cmd:
            return cmd, True
    return "", False


def _normalize_path(file_path: str) -> str:
    """Normalize a file path to a stable anchor.

    Reduces BOTH worktree and direct-repo paths to the same repo-relative
    anchor so a file accessed via a worktree in one run and directly in another
    shares ONE signature (otherwise cross-run dedup splits):
      /.../projects/<proj>/.claude/worktrees/<slug>/rest/of/path -> rest/of/path
      /.../projects/<proj>/rest/of/path                          -> rest/of/path

    Falls back to os.path.normpath if neither pattern matches.
    """
    norm = os.path.normpath(file_path)
    # 1. Worktree paths: strip .../.claude/worktrees/<slug>/ (or .worktrees/<slug>/).
    m = re.search(r"(?:\.claude/worktrees|\.worktrees|/data2/worktrees/[^/]+)/[^/]+/(.+)$", norm)
    if m:
        return m.group(1)
    # 2. Direct repo paths: strip the .../projects/<proj>/ prefix so a directly
    #    accessed file collapses to the SAME repo-relative anchor as its
    #    worktree-accessed twin. Without this, mixed access yields two anchors.
    m = re.search(r"/projects/[^/]+/(.+)$", norm)
    if m:
        return m.group(1)
    return norm


# ── Detector ──────────────────────────────────────────────────────────────────

class RereadSameFileDetector(BaseDetector):
    """Detect context churn caused by re-reading the same file many times per run.

    See module docstring for the full algorithm.
    """

    DETECTOR_NAME = "reread_same_file"

    def detect(
        self, conn: sqlite3.Connection, since_ts: Optional[str] = None
    ) -> list[PatternCandidate]:
        """Scan run_events for runs where the same file is read > REREAD_THRESHOLD times.

        Parameters
        ----------
        conn : sqlite3.Connection
            Live connection to runs.db (same connection passed to __init__).
            Do NOT close it.
        since_ts : Optional[str]
            RFC3339 UTC cutoff (issue #32); bounds the scan to runs started
            at/after this time. None (default) is unbounded.

        Returns
        -------
        list[PatternCandidate]
            One candidate per (project, normalized_file_path) that fired.
        """
        # Pull all Read-tool events, ordered for per-run analysis.
        #
        # Two-step when since_ts is given, not a single JOIN filtered on
        # r.started (issue #32 perf fix): the tool_name filter is a json_extract() over
        # re.raw_json with no index, so a single-query JOIN drives from
        # run_events regardless of the runs.started filter (verified via
        # EXPLAIN QUERY PLAN on the live DB: `SCAN re USING INDEX
        # idx_run_events_seq`, i.e. still a full run_events scan). Selecting
        # windowed run_ids from `runs` first (idx_runs_started) and then
        # filtering run_events by `run_id IN (...)` bounds the scan.
        read_tool_placeholders = ",".join("?" * len(_SCANNED_TOOL_NAMES))
        if since_ts:
            run_rows = conn.execute(
                "SELECT run_id, project FROM runs WHERE started >= ?", (since_ts,)
            ).fetchall()
            if not run_rows:
                return []
            project_by_run = {r[0]: r[1] for r in run_rows}
            run_ids = list(project_by_run.keys())
            run_id_placeholders = ",".join("?" * len(run_ids))
            cur = conn.execute(
                f"""
                SELECT run_id,
                       json_extract(raw_json, '$.data.tool_name') AS tool_name,
                       json_extract(raw_json, '$.data.tool_summary') AS tool_summary,
                       json_extract(raw_json, '$.data.cwd') AS cwd,
                       raw_json
                FROM run_events
                WHERE json_extract(raw_json, '$.data.tool_name') IN ({read_tool_placeholders})
                  AND run_id IN ({run_id_placeholders})
                ORDER BY run_id, seq
                """,
                list(_SCANNED_TOOL_NAMES) + run_ids,
            )
            rows = [
                (run_id, project_by_run.get(run_id), tool_name, tool_summary, cwd, raw_json)
                for run_id, tool_name, tool_summary, cwd, raw_json in cur.fetchall()
            ]
        else:
            cur = conn.execute(
                """
                SELECT re.run_id,
                       r.project,
                       json_extract(re.raw_json, '$.data.tool_name') AS tool_name,
                       json_extract(re.raw_json, '$.data.tool_summary') AS tool_summary,
                       json_extract(re.raw_json, '$.data.cwd') AS cwd,
                       re.raw_json
                FROM run_events AS re
                JOIN runs AS r USING (run_id)
                WHERE json_extract(re.raw_json, '$.data.tool_name') IN ({placeholders})
                ORDER BY re.run_id, re.seq
                """.format(placeholders=read_tool_placeholders),
                tuple(_SCANNED_TOOL_NAMES),
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

        for run_id, project, tool_name, tool_summary, cwd, raw_json in rows:
            if tool_name == _BASH_TOOL_NAME:
                command, truncated = _extract_bash_command(tool_summary, raw_json)
                raw_paths = _bash_read_paths(command, truncated, cwd) if command else []
            else:
                raw_path = _extract_file_path(tool_summary, raw_json)
                raw_paths = [raw_path] if raw_path else []
            # One event reads a normalized file once, however many slices it holds.
            by_norm: dict[str, str] = {}
            for raw_path in raw_paths:
                by_norm.setdefault(_normalize_path(raw_path), raw_path)
            for norm, raw_path in by_norm.items():
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
