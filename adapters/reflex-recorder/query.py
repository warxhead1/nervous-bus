"""query.py — CLI query layer over the Reflexarc SQLite run-store.

Usage
-----
    python3 query.py runs [--project P] [--outcome O] [--since TS] [--days N] [--json]
    python3 query.py thrash [--project P] [--since TS] [--days N] [--json]
    python3 query.py prevalence [--project P] [--days N] [--json]
    python3 query.py issues [--project P] [--since TS] [--days N] [--json]
    python3 query.py stats [--project P] [--since TS] [--days N] [--json]
    python3 query.py sql "<SELECT ...>" [--json]
    python3 query.py schema [--json]

Also importable as a library — all public functions accept a sqlite3.Connection.

Null-vs-clean invariant
-----------------------
``outcome IS NULL`` means NOT-YET-LABELED, never "clean".
Every aggregation that touches ``outcome`` gates on ``labeled_at IS NOT NULL``
before trusting the value.  Unlabeled runs are always reported as a distinct
"unlabeled" bucket — never folded into clean.

DB is always opened READ-ONLY for live queries (uri mode, mode=ro) + PRAGMA
query_only=ON.  The ``sql`` subcommand additionally rejects any statement that
is not a single bare SELECT (no semicolons, no PRAGMA/ATTACH/INSERT/etc.).

Agent usage
-----------
``reflex`` is the generic read-only behavioral-memory handle for autonomous
agents and tools.  Run ``reflex schema`` to self-describe the tables before
composing a ``reflex sql`` query.  All subcommands support ``--json`` for
machine consumption.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

# Tool names that constitute "read" activity (before a resolving Edit)
_READ_TOOL_NAMES = {"Read", "Grep", "Glob", "LS"}
# Tool names that constitute a "finding" (code change or output write)
_WRITE_TOOL_NAMES = {"Edit", "Write", "NotebookEdit"}

# Outcomes that indicate thrashed / abandoned / reverted work
THRASH_OUTCOMES = {"thrashed", "abandoned", "reverted"}


# ── DB connection helpers ─────────────────────────────────────────────────────

def open_db_ro(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open the run-store read-only.  Returns a sqlite3.Connection.

    Uses SQLite URI mode (mode=ro) so the query never acquires a write lock
    and cannot corrupt the recorder's WAL.

    Raises FileNotFoundError if the DB does not exist.
    """
    path = db_path or DEFAULT_DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"Run store not found: {path}")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cutoff(since: Optional[str], days: Optional[int]) -> Optional[str]:
    """Compute a RFC3339 cutoff timestamp from --since or --days flags.

    --since wins over --days when both are supplied.
    Returns None if neither is given (no time restriction).
    """
    if since:
        return since
    if days is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
        return cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


# ── runs ──────────────────────────────────────────────────────────────────────

def query_runs(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    outcome: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
    limit: int = 50,
) -> list[dict]:
    """List runs, optionally filtered by project / outcome / time window.

    Outcome handling:
    - ``outcome=None`` (default) → return ALL runs (labeled and unlabeled).
    - ``outcome='unlabeled'``    → runs where labeled_at IS NULL.
    - Any other value            → runs where labeled_at IS NOT NULL AND outcome=<value>.

    Returns list of dicts with keys:
        run_id, project, outcome, labeled, close_reason, started, ended,
        event_count, worktree_slug, git_branch, bead_id
    """
    cutoff = _cutoff(since, days)

    clauses: list[str] = []
    params: list = []

    if project:
        clauses.append("project = ?")
        params.append(project)

    if outcome == "unlabeled":
        clauses.append("labeled_at IS NULL")
    elif outcome is not None:
        # Gate on labeled_at to prevent null-vs-clean confusion
        clauses.append("labeled_at IS NOT NULL")
        clauses.append("outcome = ?")
        params.append(outcome)

    if cutoff:
        clauses.append("started >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    cur = conn.execute(
        f"""
        SELECT
            run_id,
            project,
            CASE
                WHEN labeled_at IS NULL THEN NULL
                ELSE outcome
            END AS outcome,
            labeled_at IS NOT NULL AS labeled,
            close_reason,
            started,
            ended,
            event_count,
            worktree_slug,
            git_branch,
            bead_id
        FROM runs
        {where}
        ORDER BY started DESC
        LIMIT ?
        """,
        params + [limit],
    )
    return [dict(row) for row in cur.fetchall()]


# ── thrash ────────────────────────────────────────────────────────────────────

def query_thrash(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Return runs with thrash outcomes (thrashed / abandoned / reverted).

    Gates on labeled_at IS NOT NULL before trusting outcome.
    Unlabeled runs are NOT included here — they are not confirmed thrash.
    """
    cutoff = _cutoff(since, days)

    placeholders = ",".join("?" * len(THRASH_OUTCOMES))
    params: list = list(THRASH_OUTCOMES)

    clauses: list[str] = [
        "labeled_at IS NOT NULL",
        f"outcome IN ({placeholders})",
    ]

    if project:
        clauses.append("project = ?")
        params.append(project)

    if cutoff:
        clauses.append("started >= ?")
        params.append(cutoff)

    where = "WHERE " + " AND ".join(clauses)

    cur = conn.execute(
        f"""
        SELECT
            run_id,
            project,
            outcome,
            close_reason,
            started,
            ended,
            event_count,
            worktree_slug,
            git_branch,
            bead_id,
            features
        FROM runs
        {where}
        ORDER BY started DESC
        """,
        params,
    )
    rows = []
    for row in cur.fetchall():
        d = dict(row)
        # Parse features JSON for richer output
        try:
            d["features"] = json.loads(d["features"] or "{}")
        except (json.JSONDecodeError, TypeError):
            d["features"] = {}
        rows.append(d)
    return rows


# ── prevalence ────────────────────────────────────────────────────────────────

def query_prevalence(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    days: int = 7,
) -> list[dict]:
    """Return detector hit prevalence per (project, detector).

    Reuses the same math as BaseDetector.prevalence():
        rate = distinct run_ids with a hit / total runs in window

    ``project=None`` returns all projects.
    Always uses a rolling window (``days``).

    Returns list of dicts:
        project, detector, hit_runs, total_runs, rate, window_days
    """
    cutoff = _cutoff(None, days)

    # Get distinct (project, detector) pairs that have hits in the window
    clauses = ["ts >= ?"]
    params: list = [cutoff]
    if project:
        clauses.append("dh.project = ?")
        params.append(project)

    where_dh = "WHERE " + " AND ".join(clauses)

    cur = conn.execute(
        f"""
        SELECT
            dh.project,
            dh.detector,
            COUNT(DISTINCT dh.run_id) AS hit_runs
        FROM detector_hits dh
        {where_dh}
        GROUP BY dh.project, dh.detector
        """,
        params,
    )
    hit_rows = {(r["project"], r["detector"]): r["hit_runs"] for r in cur.fetchall()}

    if not hit_rows:
        return []

    # Get total runs per project in the window (gating: ended >= cutoff)
    total_clauses = ["ended >= ?"]
    total_params: list = [cutoff]
    if project:
        total_clauses.append("project = ?")
        total_params.append(project)

    total_where = "WHERE " + " AND ".join(total_clauses)

    cur2 = conn.execute(
        f"""
        SELECT project, COUNT(*) AS total
        FROM runs
        {total_where}
        GROUP BY project
        """,
        total_params,
    )
    total_by_project = {r["project"]: r["total"] for r in cur2.fetchall()}

    results = []
    for (proj, detector), hit_runs in sorted(hit_rows.items()):
        total = total_by_project.get(proj, 0)
        rate = hit_runs / total if total > 0 else 0.0
        results.append({
            "project": proj,
            "detector": detector,
            "hit_runs": hit_runs,
            "total_runs": total,
            "rate": round(rate, 4),
            "window_days": days,
        })
    return results


# ── issues ────────────────────────────────────────────────────────────────────

def query_issues(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Return recurring issue signatures, ordered by recurrence_count DESC.

    ``since``/``days`` filters on ``last_seen`` so recently active issues
    surface first within the window.
    """
    cutoff = _cutoff(since, days)

    clauses: list[str] = []
    params: list = []

    if project:
        clauses.append("project = ?")
        params.append(project)

    if cutoff:
        clauses.append("last_seen >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    cur = conn.execute(
        f"""
        SELECT
            signature,
            project,
            detector,
            first_seen,
            last_seen,
            recurrence_count,
            recurrence_count_at_apply,
            evidence_json
        FROM issues
        {where}
        ORDER BY recurrence_count DESC, last_seen DESC
        """,
        params,
    )
    rows = []
    for row in cur.fetchall():
        d = dict(row)
        try:
            d["evidence"] = json.loads(d.pop("evidence_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["evidence"] = []
        rows.append(d)
    return rows


# ── stats ─────────────────────────────────────────────────────────────────────

def query_stats(
    conn: sqlite3.Connection,
    *,
    project: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Per-project aggregated statistics.

    Returns list of dicts (one per project) with:
        project, total_runs, labeled_runs, unlabeled_runs,
        outcome_breakdown (dict: outcome→count; "unlabeled" for NULL),
        avg_event_count, read_to_finding_ratio

    Null-vs-clean: outcome NULL rows are counted under "unlabeled" in the
    breakdown, never merged with "clean".

    read_to_finding_ratio: average ratio of read-type tool calls (Read, Grep,
    Glob, LS) to write-type tool calls (Edit, Write, NotebookEdit) per run,
    derived from tool_histogram.  None if no runs have histograms.
    """
    cutoff = _cutoff(since, days)

    clauses: list[str] = []
    params: list = []

    if project:
        clauses.append("project = ?")
        params.append(project)

    if cutoff:
        clauses.append("started >= ?")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    # ── Query 1: per-project totals + labeled/unlabeled counts ────────────────
    # Use a single pass over the runs table with explicit labeled_at gating.
    cur = conn.execute(
        f"""
        SELECT
            project,
            COUNT(*) AS total_runs,
            COUNT(labeled_at) AS labeled_runs,
            COUNT(*) - COUNT(labeled_at) AS unlabeled_runs,
            AVG(event_count) AS avg_event_count
        FROM runs
        {where}
        GROUP BY project
        """,
        params,
    )
    totals_by_proj: dict[str, dict] = {}
    for row in cur.fetchall():
        proj = row["project"]
        totals_by_proj[proj] = {
            "project": proj,
            "total_runs": row["total_runs"],
            "labeled_runs": row["labeled_runs"],
            "unlabeled_runs": row["unlabeled_runs"],
            "avg_event_count": round(row["avg_event_count"] or 0.0, 1),
            "outcome_breakdown": {},
        }

    if not totals_by_proj:
        return []

    # ── Query 2: outcome bucket breakdown ─────────────────────────────────────
    # Gate: NULL outcome (labeled_at IS NULL) → 'unlabeled'; never fold into clean.
    cur2 = conn.execute(
        f"""
        SELECT
            project,
            CASE
                WHEN labeled_at IS NULL THEN 'unlabeled'
                ELSE COALESCE(outcome, 'unlabeled')
            END AS outcome_bucket,
            COUNT(*) AS bucket_count
        FROM runs
        {where}
        GROUP BY project,
            CASE
                WHEN labeled_at IS NULL THEN 'unlabeled'
                ELSE COALESCE(outcome, 'unlabeled')
            END
        """,
        params,
    )
    for row in cur2.fetchall():
        proj = row["project"]
        if proj in totals_by_proj:
            totals_by_proj[proj]["outcome_breakdown"][row["outcome_bucket"]] = row["bucket_count"]

    # ── Query 3: per-run tool_histogram for read_to_finding_ratio ─────────────
    cur3 = conn.execute(
        f"SELECT project, tool_histogram FROM runs {where}",
        params,
    )
    reads_by_proj: dict[str, list[int]] = {}
    writes_by_proj: dict[str, list[int]] = {}
    for row in cur3.fetchall():
        proj = row["project"]
        try:
            hist = json.loads(row["tool_histogram"] or "{}")
        except (json.JSONDecodeError, TypeError):
            hist = {}
        r = sum(hist.get(t, 0) for t in _READ_TOOL_NAMES)
        w = sum(hist.get(t, 0) for t in _WRITE_TOOL_NAMES)
        reads_by_proj.setdefault(proj, []).append(r)
        writes_by_proj.setdefault(proj, []).append(w)

    result = []
    for proj, entry in sorted(totals_by_proj.items()):
        reads = reads_by_proj.get(proj, [])
        writes = writes_by_proj.get(proj, [])
        # Per-run ratios, then average (skip runs with 0 writes to avoid div/0)
        ratios = [r / w for r, w in zip(reads, writes) if w > 0]
        entry["read_to_finding_ratio"] = round(sum(ratios) / len(ratios), 2) if ratios else None
        result.append(entry)
    return result


# ── Schema catalog ───────────────────────────────────────────────────────────
#
# One-line semantic description for every column agents are likely to query.
# Kept in sync with store.py DDL and detectors/base.py DDL.
# Used by `reflex schema` so an agent can self-compose `reflex sql` queries.

SCHEMA_CATALOG: dict[str, dict[str, str]] = {
    "runs": {
        "run_id": "ULID primary key; unique per closed run segment.",
        "run_key": "Composite segmentation key: conversation_id or conversation_id#worktree_slug.",
        "run_key_kind": "'session' (main-tree work) or 'worktree' (dispatched shard).",
        "host_conversation_id": "Parent host conversation ID (same as run_key for session runs).",
        "project": "Project name, e.g. 'nervous-bus', 'tengine', 'hearth-loom'.",
        "agent_kind": "Agent type string from bus.agent.activity.v1 (e.g. host_claude_code).",
        "session_id": "Session ULID; shared across a multi-shard conversation.",
        "agent_id": "Agent instance ULID reported in the activity event stream.",
        "started": "RFC3339 UTC timestamp of the first activity event in this run.",
        "ended": "RFC3339 UTC timestamp of the last activity event before close.",
        "close_reason": "Why the run closed: 'idle_timeout', 'ended', or 'recorder_shutdown'.",
        "continues_run_id": "run_id of the preceding segment (idle-split stitching).",
        "event_count": "Total bus.agent.activity.v1 events ingested for this run.",
        "tool_histogram": "JSON dict: {tool_name: call_count}. Bash/Edit/Read/etc.",
        "worktree": "Reconstructed absolute path to the git worktree (never the slug).",
        "worktree_slug": "Short slug, e.g. 'wf_7b3dfff0-da0-3'. NULL for session runs.",
        "git_branch": "Branch name at run close (derived from worktree/cwd via git).",
        "bead_id": "Beads issue ID linked to this run (derived from branch name).",
        "outcome": (
            "Labeled outcome: 'clean', 'abandoned', 'reverted', 'thrashed', etc. "
            "CRITICAL: NULL means NOT-YET-LABELED, never 'clean'. "
            "Always gate on labeled_at IS NOT NULL before trusting this column."
        ),
        "labeled_at": (
            "RFC3339 UTC timestamp when the label was applied. "
            "NULL = unlabeled run. Use this as the gate for all outcome queries."
        ),
        "label_version": "Integer schema version of the labeling pass that set outcome.",
        "label_history": "JSON list of prior {outcome, labeled_at, label_version} dicts.",
        "features": "JSON dict of extracted per-run features (bash_fail_rate, etc.).",
        "schema_version": "Version of the bus.agent.run.closed.v1 schema this row uses.",
        "recorded_at": "RFC3339 UTC timestamp when the recorder wrote this row.",
    },
    "run_events": {
        "id": "Auto-increment row id.",
        "run_id": "Foreign key → runs.run_id.",
        "seq": "Per-run event sequence counter starting at 1.",
        "event_ts": "RFC3339 UTC timestamp copied from the activity event.",
        "event_type": "Always 'bus.agent.activity.v1' in current data.",
        "raw_json": (
            "Full CloudEvents envelope as JSON string. "
            "The .data sub-object contains: tool_name, tool_summary, model, "
            "input_tokens, output_tokens, tool_is_error, event ('tool_call'/'tool_response'/'ended'), "
            "project, cwd, worktree, agent_kind, conversation_id."
        ),
    },
    "detector_hits": {
        "id": "Auto-increment row id.",
        "run_id": "run_id that triggered this detector firing.",
        "detector": "DETECTOR_NAME string, e.g. 'worktree_leak'.",
        "signature": "Stable cross-run issue identifier: '{project}:{detector}:{anchor}'.",
        "project": "Project the hit belongs to.",
        "ts": "RFC3339 UTC timestamp of the hit.",
    },
    "issues": {
        "signature": "Primary key; stable cross-run identifier for a recurring pattern.",
        "project": "Project the issue belongs to.",
        "detector": "DETECTOR_NAME that discovered this issue.",
        "first_seen": "RFC3339 UTC timestamp of first occurrence.",
        "last_seen": "RFC3339 UTC timestamp of most recent occurrence.",
        "recurrence_count": "Total times this signature has been hit across runs.",
        "recurrence_count_at_apply": (
            "Snapshot of recurrence_count when a fix was applied. "
            "NULL if no fix applied yet. Hits after apply = regression evidence."
        ),
        "evidence_json": "JSON list of evidence strings (run_ids, paths, git_branch, etc.).",
    },
}


# ── SQL passthrough ───────────────────────────────────────────────────────────

# Patterns that mark the start of a write/control statement (after stripping
# leading SQL comments and whitespace).
_FORBIDDEN_PREFIXES = (
    "insert", "update", "delete", "drop", "create", "alter",
    "replace", "attach", "detach", "pragma", "vacuum", "reindex",
    "savepoint", "release", "rollback", "begin", "commit",
    "with",   # CTEs are fine, but we block WITH to avoid WITH ... DELETE etc.
              # Agents should write plain SELECTs — no CTE gymnastics needed.
)

# We do allow "with" CTEs that lead to a SELECT — re-enable if needed by
# removing "with" from above.  For now, plain SELECTs only (simpler guard).


def _validate_select(sql: str) -> str:
    """Validate that *sql* is a single, bare SELECT statement.

    Rules:
    1. Strip leading/trailing whitespace and SQL line comments (-- ...).
    2. Reject if the first keyword is not SELECT.
    3. Reject if the statement contains a semicolon (multi-statement guard).

    Returns the stripped SQL string on success.
    Raises ValueError with a human-readable message on rejection.
    """
    # Strip line comments
    import re as _re
    stripped = _re.sub(r"--[^\n]*", " ", sql).strip()
    if not stripped:
        raise ValueError("Empty SQL statement.")
    # Multi-statement guard: reject any semicolon anywhere
    if ";" in stripped:
        raise ValueError(
            "Multi-statement SQL is not allowed. "
            "Provide a single SELECT with no trailing semicolons."
        )
    # Keyword check
    first_word = stripped.split()[0].lower()
    if first_word in _FORBIDDEN_PREFIXES or first_word != "select":
        raise ValueError(
            f"Only SELECT statements are permitted; got leading keyword: {first_word!r}."
        )
    return stripped


def query_sql(
    conn: sqlite3.Connection,
    sql: str,
) -> list[dict]:
    """Execute a validated read-only SELECT and return rows as list of dicts.

    The statement is validated by _validate_select() before execution.
    The connection must already be in read-only / query_only mode (enforced
    by open_db_ro which uses mode=ro).

    Raises ValueError if the SQL fails validation.
    Raises sqlite3.OperationalError on query execution failure.
    """
    clean_sql = _validate_select(sql)
    cur = conn.execute(clean_sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── Schema dump ───────────────────────────────────────────────────────────────

def schema_catalog(conn: Optional[sqlite3.Connection] = None) -> dict:
    """Return the schema catalog as a dict: {table: {column: description}}.

    Optionally cross-references the live DB to confirm which tables exist.
    If conn is None, returns SCHEMA_CATALOG as-is.
    """
    return SCHEMA_CATALOG


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_table(rows: list[dict], columns: Optional[list[str]] = None) -> str:
    """Render a list of dicts as a plain-text table."""
    if not rows:
        return "(no rows)"
    cols = columns or list(rows[0].keys())
    # Stringify values
    str_rows = []
    for row in rows:
        str_rows.append([str(row.get(c, "")) for c in cols])
    # Column widths
    widths = [len(c) for c in cols]
    for srow in str_rows:
        for i, val in enumerate(srow):
            widths[i] = max(widths[i], len(val))
    # Header
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    sep = "  ".join("-" * widths[i] for i in range(len(cols)))
    lines = [header, sep]
    for srow in str_rows:
        lines.append("  ".join(v.ljust(widths[i]) for i, v in enumerate(srow)))
    return "\n".join(lines)


def _outcome_label(outcome: Optional[str], labeled: int) -> str:
    if not labeled or outcome is None:
        return "unlabeled"
    return outcome


# ── CLI subcommand handlers ───────────────────────────────────────────────────

def cmd_runs(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = query_runs(
        conn,
        project=args.project,
        outcome=args.outcome,
        since=args.since,
        days=args.days,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No runs found.")
        return
    display = [
        {
            "run_id": r["run_id"][:20] + "…",
            "project": r["project"],
            "outcome": _outcome_label(r["outcome"], r["labeled"]),
            "close": r["close_reason"] or "",
            "started": (r["started"] or "")[:16],
            "events": r["event_count"],
            "worktree": r["worktree_slug"] or "",
        }
        for r in rows
    ]
    print(_fmt_table(display))
    print(f"\n{len(rows)} run(s)")


def cmd_thrash(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = query_thrash(
        conn,
        project=args.project,
        since=args.since,
        days=args.days,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No thrashed/abandoned/reverted runs found.")
        return
    display = [
        {
            "run_id": r["run_id"][:20] + "…",
            "project": r["project"],
            "outcome": r["outcome"],
            "started": (r["started"] or "")[:16],
            "events": r["event_count"],
            "bead": r["bead_id"] or "",
            "branch": r["git_branch"] or "",
        }
        for r in rows
    ]
    print(_fmt_table(display))
    print(f"\n{len(rows)} thrash run(s)")


def cmd_prevalence(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = query_prevalence(
        conn,
        project=args.project,
        days=args.days or 7,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No detector hits found in window.")
        return
    display = [
        {
            "project": r["project"],
            "detector": r["detector"],
            "hit_runs": r["hit_runs"],
            "total_runs": r["total_runs"],
            "rate": f"{r['rate']:.1%}",
            "window_days": r["window_days"],
        }
        for r in rows
    ]
    print(_fmt_table(display))


def cmd_issues(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = query_issues(
        conn,
        project=args.project,
        since=args.since,
        days=args.days,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No issues found.")
        return
    display = [
        {
            "project": r["project"],
            "detector": r["detector"],
            "recurrence": r["recurrence_count"],
            "fixed": "yes" if r["recurrence_count_at_apply"] else "no",
            "last_seen": (r["last_seen"] or "")[:16],
            "signature": r["signature"][:50] + ("…" if len(r["signature"]) > 50 else ""),
        }
        for r in rows
    ]
    print(_fmt_table(display))
    print(f"\n{len(rows)} issue(s)")


def cmd_sql(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    # Belt-and-suspenders: mode=ro (from open_db_ro) is the primary guard;
    # query_only=ON is a second layer for in-memory/test connections where
    # mode=ro is not enforced by the VFS.  Ignore failures on read-only URIs.
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.OperationalError:
        pass  # Already read-only via URI mode
    try:
        rows = query_sql(conn, args.statement)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("(no rows)")
        return
    print(_fmt_table(rows))
    print(f"\n{len(rows)} row(s)")


def cmd_schema(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    catalog = schema_catalog(conn)
    if args.json:
        print(json.dumps(catalog, indent=2))
        return
    for table, columns in catalog.items():
        print(f"\n## {table}")
        print("-" * (len(table) + 3))
        for col, desc in columns.items():
            print(f"  {col:<32} {desc}")


def cmd_stats(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = query_stats(
        conn,
        project=args.project,
        since=args.since,
        days=args.days,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No stats available.")
        return
    display = [
        {
            "project": r["project"],
            "total": r["total_runs"],
            "labeled": r["labeled_runs"],
            "unlabeled": r["unlabeled_runs"],
            "outcomes": json.dumps(r["outcome_breakdown"]),
            "avg_events": r["avg_event_count"],
            "read/write": r["read_to_finding_ratio"] if r["read_to_finding_ratio"] is not None else "n/a",
        }
        for r in rows
    ]
    print(_fmt_table(display))


# ── skills / dispatch (features folded by skill_usage.py) ─────────────────────

GOOD_OUTCOMES = {"clean", "landed"}

DEFAULT_SKILL_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path.home() / ".agents" / "skills",
    Path.home() / ".codex" / "skills",
    Path.home() / ".claude" / "plugins",
]


def _feature_runs(conn: sqlite3.Connection, project: Optional[str], cutoff: Optional[str]):
    clauses, params = [], []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if cutoff:
        clauses.append("started >= ?")
        params.append(cutoff)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    for r in conn.execute(
        f"SELECT run_id, project, agent_kind, started, outcome, labeled_at, features FROM runs {where}",
        params,
    ):
        try:
            feats = json.loads(r["features"] or "{}")
        except ValueError:
            feats = {}
        yield r, feats


def query_skills(
    conn: sqlite3.Connection,
    *,
    by: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Per-skill usage: invocations, runs, projects, mechanism + harness split.

    ``by`` in {project, month, agent_kind} adds that column as a grouping key.
    """
    cutoff = _cutoff(since, days)
    acc: dict[tuple, dict] = {}
    for r, feats in _feature_runs(conn, project, cutoff):
        for name, mechs in (feats.get("skills") or {}).items():
            group = None
            if by == "project":
                group = r["project"]
            elif by == "month":
                group = (r["started"] or "")[:7]
            elif by == "agent_kind":
                group = r["agent_kind"]
            a = acc.setdefault((name, group), {
                "skill": name, "group": group, "invocations": 0, "runs": 0,
                "projects": set(), "mechanisms": {}, "harness": {},
                "first_seen": r["started"], "last_seen": r["started"],
            })
            n = sum(mechs.values())
            a["invocations"] += n
            a["runs"] += 1
            a["projects"].add(r["project"])
            for m, c in mechs.items():
                a["mechanisms"][m] = a["mechanisms"].get(m, 0) + c
            a["harness"][r["agent_kind"]] = a["harness"].get(r["agent_kind"], 0) + n
            a["first_seen"] = min(a["first_seen"], r["started"])
            a["last_seen"] = max(a["last_seen"], r["started"])
    rows = []
    for a in acc.values():
        a["projects"] = len(a["projects"])
        if by is None:
            a.pop("group")
        rows.append(a)
    rows.sort(key=lambda x: (x.get("group") or "", -x["invocations"]))
    return rows


def discover_skills(roots: Optional[list[Path]] = None) -> dict[str, set[str]]:
    """Map skill name -> set of root labels where a <name>/SKILL.md exists."""
    found: dict[str, set[str]] = {}
    if roots is None:
        roots = DEFAULT_SKILL_ROOTS + sorted((Path.home() / "projects").glob("*/.claude/skills"))
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for skill_md in root.rglob("SKILL.md"):
            found.setdefault(skill_md.parent.name, set()).add(str(root))
    return found


def query_skill_inventory(
    conn: sqlite3.Connection,
    *,
    roots: Optional[list[Path]] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """On-disk skills joined to observed usage; flags never-seen and unknown names."""
    on_disk = discover_skills(roots)
    observed: dict[str, dict] = {}
    for r in query_skills(conn, since=since, days=days):
        name = r["skill"]
        # Plugin skills are invoked as "<plugin>:<skill>" but live on disk as <skill>/SKILL.md.
        if name not in on_disk and ":" in name and name.rsplit(":", 1)[1] in on_disk:
            name = name.rsplit(":", 1)[1]
        o = observed.setdefault(name, {"invocations": 0, "runs": 0})
        o["invocations"] += r["invocations"]
        o["runs"] += r["runs"]
    rows = []
    for name in sorted(set(on_disk) | set(observed)):
        o = observed.get(name)
        rows.append({
            "skill": name,
            "roots": sorted(on_disk.get(name, [])),
            "on_disk": name in on_disk,
            "invocations": o["invocations"] if o else 0,
            "runs": o["runs"] if o else 0,
            "status": ("never-seen" if not o else "used") if name in on_disk else "not-on-disk",
        })
    rows.sort(key=lambda x: (x["status"] != "never-seen", -x["invocations"], x["skill"]))
    return rows


def query_skill_lift(
    conn: sqlite3.Connection,
    *,
    min_n: int = 20,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Outcome rate of runs that used a skill vs same project+agent_kind runs that did not.

    Correlational only. Labeled runs only (labeled_at IS NOT NULL). good = clean|landed.
    The comparison arm is restricted to the (project, agent_kind) strata the skill
    appears in, so a skill used mostly in a healthy project does not borrow its lift.
    """
    cutoff = _cutoff(since, days)
    strata: dict[tuple, list[tuple[set, bool]]] = {}
    for r, feats in _feature_runs(conn, None, cutoff):
        if r["labeled_at"] is None or r["outcome"] is None:
            continue
        strata.setdefault((r["project"], r["agent_kind"]), []).append(
            (set((feats.get("skills") or {}).keys()), r["outcome"] in GOOD_OUTCOMES)
        )
    skills = sorted({s for runs in strata.values() for used, _ in runs for s in used})
    rows = []
    for s in skills:
        w_n = w_good = wo_n = wo_good = 0
        for runs in strata.values():
            if not any(s in used for used, _ in runs):
                continue
            for used, good in runs:
                if s in used:
                    w_n += 1
                    w_good += good
                else:
                    wo_n += 1
                    wo_good += good
        w_rate = w_good / w_n if w_n else None
        wo_rate = wo_good / wo_n if wo_n else None
        rows.append({
            "skill": s,
            "with_n": w_n, "with_good_rate": round(w_rate, 3) if w_rate is not None else None,
            "without_n": wo_n, "without_good_rate": round(wo_rate, 3) if wo_rate is not None else None,
            "diff": round(w_rate - wo_rate, 3) if w_rate is not None and wo_rate is not None else None,
            "status": "ok" if w_n >= min_n and wo_n >= min_n else "insufficient",
        })
    rows.sort(key=lambda x: (x["status"] != "ok", -(x["with_n"])))
    return rows


def query_dispatch(
    conn: sqlite3.Connection,
    *,
    by: Optional[str] = None,
    project: Optional[str] = None,
    since: Optional[str] = None,
    days: Optional[int] = None,
) -> list[dict]:
    """Agent/Task dispatch tiering: model mix, missing-model rate, isolation rate.

    model_missing_rate denominator = dispatches whose model state is known
    (explicit + missing); truncated summaries (model_unknown) are excluded.
    """
    cutoff = _cutoff(since, days)
    acc: dict = {}
    for r, feats in _feature_runs(conn, project, cutoff):
        d = feats.get("dispatch")
        if not d:
            continue
        key = {"project": r["project"], "month": (r["started"] or "")[:7]}.get(by, "all")
        a = acc.setdefault(key, {"group": key, "dispatches": 0, "by_model": {},
                                 "model_missing": 0, "model_unknown": 0, "isolated": 0})
        a["dispatches"] += d.get("total", 0)
        a["model_missing"] += d.get("model_missing", 0)
        a["model_unknown"] += d.get("model_unknown", 0)
        a["isolated"] += d.get("isolated", 0)
        for m, c in (d.get("by_model") or {}).items():
            a["by_model"][m] = a["by_model"].get(m, 0) + c
    rows = []
    for a in acc.values():
        known = a["dispatches"] - a["model_unknown"]
        a["model_missing_rate"] = round(a["model_missing"] / known, 3) if known else None
        a["isolation_rate"] = round(a["isolated"] / a["dispatches"], 3) if a["dispatches"] else None
        rows.append(a)
    rows.sort(key=lambda x: str(x["group"]))
    return rows


def cmd_skills(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if args.inventory:
        roots = [Path(p) for p in args.root] if args.root else None
        rows = query_skill_inventory(conn, roots=roots, since=args.since, days=args.days)
        cols = ["skill", "status", "invocations", "runs", "roots"]
    elif args.lift:
        rows = query_skill_lift(conn, min_n=args.min_n, since=args.since, days=args.days)
        cols = ["skill", "status", "with_n", "with_good_rate", "without_n", "without_good_rate", "diff"]
        if not args.json:
            print("CORRELATIONAL, NOT CAUSAL: good=clean|landed among labeled runs; "
                  "comparison arm = same project+agent_kind runs without the skill.")
    else:
        rows = query_skills(conn, by=args.by, project=args.project, since=args.since, days=args.days)
        cols = (["group"] if args.by else []) + [
            "skill", "invocations", "runs", "projects", "mechanisms", "harness", "first_seen", "last_seen"]
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(_fmt_table([{c: (json.dumps(r[c]) if isinstance(r[c], (dict, list)) else r[c]) for c in cols}
                      for r in rows], cols))


def cmd_dispatch(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = query_dispatch(conn, by=args.by, project=args.project, since=args.since, days=args.days)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(_fmt_table([{**r, "by_model": json.dumps(r["by_model"])} for r in rows]))


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reflex",
        description="Query the Reflexarc run-store.",
    )
    p.add_argument(
        "--db",
        metavar="PATH",
        help="Path to runs.db (default: ~/.cache/nervous-bus/reflex/runs.db)",
    )

    sub = p.add_subparsers(dest="command", required=True)

    # Common flags shared across subcommands
    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--project", "-p", help="Filter by project name")
        sp.add_argument("--since", help="ISO8601 cutoff timestamp (inclusive)")
        sp.add_argument("--days", "-d", type=int, help="Rolling window in days (e.g. 7)")
        sp.add_argument("--json", "-j", action="store_true", help="Machine-readable JSON output")

    # runs
    sp_runs = sub.add_parser("runs", help="List/filter runs")
    _add_common(sp_runs)
    sp_runs.add_argument("--outcome", "-o", help="Filter by outcome (or 'unlabeled')")
    sp_runs.add_argument("--limit", "-n", type=int, default=50, help="Max rows (default 50)")

    # thrash
    sp_thrash = sub.add_parser(
        "thrash",
        help="Runs with outcome in (thrashed, abandoned, reverted)",
    )
    _add_common(sp_thrash)

    # prevalence
    sp_prev = sub.add_parser(
        "prevalence",
        help="Detector hit rate per project (hit_runs / total_runs in window)",
    )
    sp_prev.add_argument("--project", "-p", help="Filter by project name")
    sp_prev.add_argument("--days", "-d", type=int, default=7, help="Rolling window in days (default 7)")
    sp_prev.add_argument("--json", "-j", action="store_true", help="JSON output")

    # issues
    sp_issues = sub.add_parser("issues", help="Recurring issue signatures")
    _add_common(sp_issues)

    # stats
    sp_stats = sub.add_parser(
        "stats",
        help="Per-project aggregates: run count, outcome breakdown, avg events, read/write ratio",
    )
    _add_common(sp_stats)

    # sql — read-only SQL passthrough
    sp_sql = sub.add_parser(
        "sql",
        help="Execute a read-only SELECT against the run-store (agent SQL passthrough)",
    )
    sp_sql.add_argument("statement", help="A single SELECT statement (no semicolons)")
    sp_sql.add_argument("--json", "-j", action="store_true", help="JSON output")

    # schema — dump table+column catalog for agent self-composition
    sp_schema = sub.add_parser(
        "schema",
        help="Dump the run-store table+column catalog with semantic descriptions",
    )
    sp_schema.add_argument("--json", "-j", action="store_true", help="JSON output")

    # skills — skill-load usage, inventory join, outcome lift
    sp_skills = sub.add_parser("skills", help="Skill usage (Skill tool + SKILL.md reads) per skill")
    _add_common(sp_skills)
    sp_skills.add_argument("--by", choices=["project", "month", "agent_kind"], help="Group by")
    sp_skills.add_argument("--inventory", action="store_true",
                           help="Join on-disk skills to usage; flag never-seen")
    sp_skills.add_argument("--root", action="append",
                           help="Skill root to scan for --inventory (repeatable; default: user roots)")
    sp_skills.add_argument("--lift", action="store_true",
                           help="Outcome rate with vs without the skill (same project+agent_kind)")
    sp_skills.add_argument("--min-n", type=int, default=20, help="Min runs per arm for --lift (default 20)")

    # dispatch — Agent/Task model tiering
    sp_disp = sub.add_parser("dispatch", help="Agent/Task dispatch model mix and missing-model rate")
    _add_common(sp_disp)
    sp_disp.add_argument("--by", choices=["project", "month"], help="Group by")

    return p


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db) if getattr(args, "db", None) else None

    try:
        conn = open_db_ro(db_path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        dispatch = {
            "runs": cmd_runs,
            "thrash": cmd_thrash,
            "prevalence": cmd_prevalence,
            "issues": cmd_issues,
            "stats": cmd_stats,
            "sql": cmd_sql,
            "schema": cmd_schema,
            "skills": cmd_skills,
            "dispatch": cmd_dispatch,
        }
        handler = dispatch.get(args.command)
        if handler is None:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 1
        handler(args, conn)
    except sqlite3.OperationalError as exc:
        # Table may not exist yet (e.g. detector tables lazily created)
        print(f"DB error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
