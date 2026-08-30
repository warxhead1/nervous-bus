#!/usr/bin/env python3
"""board — the total board: one JSON contract + one human kanban across every
project's beads, cross-referenced with live orca dispatch state and hearth-loom
PR lifecycle.

Motivating gap: 11 project beads DBs (app_to_market, beads_global, biz_worthy,
deer_flow, hearth, `hearth-loom`, nervous_bus, sweepers_adventures,
temple_stuart_accounting, tengine, unreal_battlebots_gamedev) live on one dolt
SQL server, but nothing federates them into a single "what's actually
happening right now" view. `bd ready` only sees one project's DB at a time,
and neither bd nor orca knows the other exists -- a bead can be "ready" in
beads while an orca worker is already grinding on it, or "in_progress" while
its worker died hours ago with no lifecycle event to say so. This adapter
answers "what's on every project's board, and is anything actually happening
on it" in one place.

Three inputs, one run:
  1. `beads_global.all_issues` / `beads_global.all_dependencies` -- federated
     SQL views (adapters/board/create_view.sql) UNIONing the `issues` /
     `dependencies` tables of the 10 project DBs (beads_global itself
     excluded -- it is the federation/routing DB, not a project with its own
     work queue). Queried over the dolt server's MySQL-protocol port
     (127.0.0.1:39502) via pymysql; `dolt sql` subprocess is the CLI fallback
     documented in create_view.sql but board.py always uses the wire
     protocol -- one round trip beats N `dolt sql -q` subprocess spawns.
  2. Orca's orchestration.db (sqlite, opened `mode=ro` -- board.py must never
     write orca state): tasks.spec is scanned for a bead reference using the
     SAME convention as orca's own src/main/runtime/orchestration/nbus-emit.ts
     `extractBeadId` (`[bead:<id>]` tag, else loose `bead <id>` prose), then
     joined to that task's most recent dispatch_contexts row for state +
     heartbeat.
  3. Redis nbus:* streams (XREVRANGE, last ~200 entries each): orca's own
     lifecycle stream (nbus:orca.worker.lifecycle.v1 -- schema exists, live
     stream was empty at authoring time, code below still queries it since
     that's a live-system gap in orca's emission, not a reason to skip the
     source) plus hearth-loom's PR + generic lifecycle streams, whose naming
     is NOT consistent between schema and live emission (schema says
     `hearth-loom.pr.opened.v1`; the actually-live stream key observed 2026-08
     was `nbus:bus.hearth-loom.pr.opened.v1`, plus an older `loom.lifecycle.pr`
     / `loom.lifecycle.pr.v1` pair and a legacy `bus.bead.pr_opened`). All
     four are read; this file does NOT assert one is canonical.

Output, written atomically (tmp + rename) every run:
  - ~/.cache/nervous-bus/board/board.json -- FROZEN machine contract, see
    CONTRACT_TOP_LEVEL_KEYS / the module docstring in test_board.py for the
    exact shape. Do not change existing field names/types; additive optional
    fields only.
  - ~/.cache/nervous-bus/board/report.md -- human kanban, one section per lane.

Usage:
    python3 board.py                          # real run against live dolt/orca/redis
    python3 board.py --board-file /tmp/b.json --report-file /tmp/r.md
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CACHE_DIR = Path(os.environ.get("NERVOUS_BOARD_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "board")))
BOARD_FILE = CACHE_DIR / "board.json"
REPORT_FILE = CACHE_DIR / "report.md"

DOLT_HOST = os.environ.get("NERVOUS_BOARD_DOLT_HOST", "127.0.0.1")
DOLT_PORT = int(os.environ.get("NERVOUS_BOARD_DOLT_PORT", "39502"))
DOLT_USER = os.environ.get("NERVOUS_BOARD_DOLT_USER", "root")
DOLT_DB = os.environ.get("NERVOUS_BOARD_DOLT_DB", "beads_global")

ORCA_DB = Path(os.environ.get("NERVOUS_BOARD_ORCA_DB", str(Path.home() / ".config" / "orca" / "orchestration.db")))

REDIS_URL = os.environ.get("NERVOUS_REDIS_URL", "redis://localhost:6379")
REDIS_SAMPLE = int(os.environ.get("NERVOUS_BOARD_REDIS_SAMPLE", "200"))

# orca's own worker-dispatch lifecycle (schema: orca.worker.lifecycle.v1).
LIFECYCLE_STREAMS = [
    "nbus:orca.worker.lifecycle.v1",
    "nbus:loom.lifecycle.v1",
]

# hearth-loom PR lifecycle. Naming is NOT consistent between the checked-in
# schema files (hearth-loom.pr.opened.v1 / .merged.v1) and what has actually
# been observed live on the bus (bus.hearth-loom.pr.opened.v1, the older
# loom.lifecycle.pr / loom.lifecycle.pr.v1 pair, and legacy bus.bead.pr_opened
# which carries no pr_url/state at all, opened-only). Read all of them; a
# nonexistent stream key XREVRANGEs to an empty list, not an error.
PR_STREAMS = [
    "nbus:bus.hearth-loom.pr.opened.v1",
    "nbus:bus.hearth-loom.pr.merged.v1",
    "nbus:hearth-loom.pr.opened.v1",
    "nbus:hearth-loom.pr.merged.v1",
    "nbus:loom.lifecycle.pr.v1",
    "nbus:loom.lifecycle.pr",
    "nbus:bus.bead.pr_opened",
]

# Mirrors orca's src/main/runtime/orchestration/nbus-emit.ts extractBeadId()
# EXACTLY (tag form first, then loose prose form) so board.py's bead
# extraction never drifts from what orca itself considers a bead reference.
BEAD_TAG_RE = re.compile(r"\[bead:([a-z0-9][a-z0-9._-]*)\]", re.IGNORECASE)
BEAD_PROSE_RE = re.compile(r"\bbead\s+([a-z][a-z0-9]*-[a-z0-9-]+)\b", re.IGNORECASE)

LANES = ["ready", "in_progress", "in_flight", "in_review", "blocked", "done_7d"]

# Lane precedence, highest first -- matches the brief exactly.
LANE_PRECEDENCE = ["in_flight", "in_review", "blocked", "in_progress", "ready"]

DONE_WINDOW_S = 7 * 86400.0
IN_FLIGHT_HEARTBEAT_WINDOW_S = 30 * 60.0

PRIORITY_WEIGHT = {0: 8, 1: 5, 2: 3, 3: 2, 4: 1}
DEFAULT_PRIORITY_WEIGHT = 1

CONTRACT_TOP_LEVEL_KEYS = {"generated_at", "lanes", "issues", "summary"}
CONTRACT_ISSUE_KEYS = {
    "id", "project", "title", "status", "lane", "priority", "issue_type",
    "assignee", "created_at", "updated_at", "age_days", "score",
    "blocked_by", "orca", "pr",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value) -> Optional[float]:
    """Parse a datetime that may arrive as a naive `datetime` (pymysql/sqlite
    DATETIME columns), an ISO-8601 string with or without a trailing 'Z', or
    None. Returns epoch seconds (UTC) or None.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def extract_bead_id(text: Optional[str]) -> Optional[str]:
    """Bead-id extraction mirroring orca's extractBeadId() 1:1: tag form
    `[bead:<id>]` wins, else the loose `bead <id>` prose form, else None.
    """
    if not text:
        return None
    m = BEAD_TAG_RE.search(text)
    if m:
        return m.group(1)
    m = BEAD_PROSE_RE.search(text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Dolt (federated issues + dependencies)
# --------------------------------------------------------------------------

def fetch_issues(conn) -> List[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT project, id, title, description, status, priority, issue_type, "
        "assignee, created_at, updated_at, closed_at, notes FROM all_issues"
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_open_blocked_by(conn) -> Dict[str, List[str]]:
    """issue_id -> sorted list of ids currently blocking it: a 'blocks' edge
    whose blocker issue is not closed. Mirrors the logic in each project DB's
    own `blocked_issues` view (dependencies.type = 'blocks' AND blocker.status
    NOT IN ('closed', 'pinned')), simplified to NOT IN ('closed') since
    per-project custom_statuses 'done'/'frozen' categories are not uniformly
    populated across all 10 DBs and board.py has no cross-DB way to resolve
    them generically without per-DB special-casing.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT d.issue_id, d.depends_on_id FROM all_dependencies d "
        "JOIN all_issues blocker ON blocker.id = d.depends_on_id "
        "WHERE d.type = 'blocks' AND blocker.status != 'closed'"
    )
    out: Dict[str, List[str]] = {}
    for issue_id, depends_on_id in cur.fetchall():
        out.setdefault(issue_id, []).append(depends_on_id)
    for k in out:
        out[k].sort()
    return out


# --------------------------------------------------------------------------
# Orca (sqlite, read-only)
# --------------------------------------------------------------------------

def fetch_orca_state(db_path: Path) -> Dict[str, dict]:
    """bead_id -> {"task_id","run_id","state","last_heartbeat_at"} for every
    task whose spec/title carries a bead reference, using that task's most
    recent dispatch_contexts row. If a bead is referenced by more than one
    task, the most recently created dispatch context wins.
    """
    if not db_path.exists():
        return {}
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, run_id, task_title, spec FROM tasks")
        tasks = cur.fetchall()

        cur.execute(
            "SELECT task_id, status, last_heartbeat_at, created_at "
            "FROM dispatch_contexts ORDER BY created_at ASC"
        )
        latest_dispatch: Dict[str, dict] = {}
        for task_id, status, last_heartbeat_at, created_at in cur.fetchall():
            # ORDER BY created_at ASC + overwrite -> last write wins -> latest.
            latest_dispatch[task_id] = {
                "status": status,
                "last_heartbeat_at": last_heartbeat_at,
                "created_at": created_at,
            }
    finally:
        conn.close()

    out: Dict[str, dict] = {}
    for task_id, run_id, task_title, spec in tasks:
        bead_id = extract_bead_id(task_title) or extract_bead_id(spec)
        if not bead_id:
            continue
        dispatch = latest_dispatch.get(task_id, {})
        candidate = {
            "task_id": task_id,
            "run_id": run_id,
            "state": dispatch.get("status"),
            "last_heartbeat_at": dispatch.get("last_heartbeat_at"),
        }
        existing = out.get(bead_id)
        if existing is None:
            out[bead_id] = candidate
            continue
        # Prefer whichever candidate's dispatch context was created later.
        existing_created = latest_dispatch.get(existing["task_id"], {}).get("created_at") or ""
        new_created = dispatch.get("created_at") or ""
        if new_created > existing_created:
            out[bead_id] = candidate
    return out


# --------------------------------------------------------------------------
# Redis (lifecycle + PR events)
# --------------------------------------------------------------------------

def _xrevrange(client, stream: str, count: int) -> List[Tuple[str, dict]]:
    try:
        return client.xrevrange(stream, count=count)
    except Exception:
        return []


def fetch_lifecycle_events(client, streams: List[str] = None, sample: int = REDIS_SAMPLE) -> List[dict]:
    """Recent lifecycle entries (orca worker dispatch + hearth-loom generic
    lifecycle), newest first per stream, parsed into flat dicts with at least
    bead_id/state/ts when present. Non-JSON / unparseable entries are skipped.
    """
    streams = streams if streams is not None else LIFECYCLE_STREAMS
    out: List[dict] = []
    for stream in streams:
        for entry_id, fields in _xrevrange(client, stream, sample):
            raw = fields.get("_raw") if isinstance(fields, dict) else None
            if not raw:
                continue
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            data = envelope.get("data") or {}
            out.append({
                "stream": stream,
                "entry_id": entry_id,
                "type": envelope.get("type"),
                "bead_id": data.get("bead_id"),
                "state": data.get("state") or data.get("event") or data.get("phase"),
                "ts": envelope.get("time") or data.get("ts"),
            })
    return out


def fetch_pr_events(client, streams: List[str] = None, sample: int = REDIS_SAMPLE) -> Dict[str, dict]:
    """bead_id -> {"url","state"} from the most recent PR-lifecycle event
    across all PR_STREAMS variants (see module docstring for why there is
    more than one). "most recent" is by envelope time string comparison
    (RFC3339 UTC sorts lexicographically), not stream order, since streams
    are read independently.
    """
    streams = streams if streams is not None else PR_STREAMS
    best: Dict[str, Tuple[str, dict]] = {}  # bead_id -> (time_str, event_dict)
    for stream in streams:
        for _entry_id, fields in _xrevrange(client, stream, sample):
            raw = fields.get("_raw") if isinstance(fields, dict) else None
            if not raw:
                continue
            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            data = envelope.get("data") or {}
            bead_id = data.get("bead_id")
            if not bead_id:
                continue
            event = (data.get("event") or "").lower()
            ts = envelope.get("time") or data.get("ts") or ""
            if "merge" in event:
                state = "merged"
            elif "close" in event and "merge" not in event:
                state = "closed"
            else:
                # opened, or legacy bus.bead.pr_opened (no explicit "event" field)
                state = "open"
            pr_url = data.get("pr_url") or data.get("url")
            prior = best.get(bead_id)
            if prior is None or ts > prior[0]:
                best[bead_id] = (ts, {"url": pr_url, "state": state})
    return {bead_id: ev for bead_id, (_ts, ev) in best.items()}


# --------------------------------------------------------------------------
# Lane derivation + scoring (pure, unit-tested)
# --------------------------------------------------------------------------

def is_open_pr(pr: Optional[dict]) -> bool:
    return bool(pr) and pr.get("state") == "open"


def is_in_flight_orca(orca: Optional[dict], lifecycle_recent: bool) -> bool:
    if lifecycle_recent:
        return True
    if not orca:
        return False
    return orca.get("state") in ("dispatched", "pending")


def derive_lane(
    *,
    status: str,
    closed_at_epoch: Optional[float],
    has_open_blocker: bool,
    orca: Optional[dict],
    lifecycle_recent: bool,
    pr: Optional[dict],
    now: float,
) -> str:
    """Precedence (highest first): in_flight > in_review > blocked >
    in_progress > ready. done_7d is evaluated first since a closed issue
    can't occupy any of the other lanes regardless of stale blocker/dispatch
    bookkeeping.
    """
    status = (status or "").lower()

    if status == "closed":
        # closed_at_epoch is None for a closed issue only if the DB row is
        # missing its closed_at timestamp -- can't prove "within 7d" without
        # it, so treat as stale/excluded rather than assume recency.
        if closed_at_epoch is not None and (now - closed_at_epoch) <= DONE_WINDOW_S:
            return "done_7d"
        return "_closed_stale"  # excluded by caller (older than 7d, or undated)

    if is_in_flight_orca(orca, lifecycle_recent):
        return "in_flight"
    if is_open_pr(pr):
        return "in_review"
    if status == "blocked" or has_open_blocker:
        return "blocked"
    if status == "in_progress":
        return "in_progress"
    return "ready"


def compute_score(priority: int, age_days: float, blocked: bool) -> float:
    weight = PRIORITY_WEIGHT.get(priority, DEFAULT_PRIORITY_WEIGHT)
    score = weight * math.log2(2 + max(age_days, 0.0))
    if blocked:
        score *= 1.5
    return score


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build_board(
    issues: List[dict],
    blocked_by_map: Dict[str, List[str]],
    orca_state: Dict[str, dict],
    lifecycle_events: List[dict],
    pr_map: Dict[str, dict],
    *,
    now: Optional[float] = None,
) -> dict:
    now = now if now is not None else time.time()

    lifecycle_recent_beads = set()
    for ev in lifecycle_events:
        bead_id = ev.get("bead_id")
        state = (ev.get("state") or "").lower()
        if not bead_id or state not in ("dispatched", "heartbeat"):
            continue
        ts = parse_dt(ev.get("ts"))
        if ts is not None and (now - ts) <= IN_FLIGHT_HEARTBEAT_WINDOW_S:
            lifecycle_recent_beads.add(bead_id)

    out_issues = []
    per_project: Dict[str, Dict[str, int]] = {}
    per_lane: Dict[str, int] = {lane: 0 for lane in LANES}

    for row in issues:
        issue_id = row["id"]
        status = row["status"]
        closed_at_epoch = parse_dt(row.get("closed_at"))
        blocked_by = blocked_by_map.get(issue_id, [])
        orca = orca_state.get(issue_id)
        lifecycle_recent = issue_id in lifecycle_recent_beads
        pr = pr_map.get(issue_id)

        lane = derive_lane(
            status=status,
            closed_at_epoch=closed_at_epoch,
            has_open_blocker=bool(blocked_by),
            orca=orca,
            lifecycle_recent=lifecycle_recent,
            pr=pr,
            now=now,
        )
        if lane == "_closed_stale":
            # Closed older than 7d (or missing closed_at) -- excluded from
            # the board entirely per the spec, not just uncounted.
            continue

        created_epoch = parse_dt(row.get("created_at")) or now
        age_days = max(0.0, (now - created_epoch) / 86400.0)
        priority = int(row.get("priority") if row.get("priority") is not None else 2)
        score = compute_score(priority, age_days, lane == "blocked")

        out_issues.append({
            "id": issue_id,
            "project": row["project"],
            "title": row.get("title"),
            "status": status,
            "lane": lane,
            "priority": priority,
            "issue_type": row.get("issue_type"),
            "assignee": row.get("assignee") or None,
            "created_at": iso(created_epoch),
            "updated_at": iso(parse_dt(row.get("updated_at")) or created_epoch),
            "age_days": round(age_days, 3),
            "score": round(score, 4),
            "blocked_by": blocked_by,
            "orca": orca,
            "pr": pr,
        })

        per_project.setdefault(row["project"], {lane_: 0 for lane_ in LANES})
        per_project[row["project"]][lane] = per_project[row["project"]].get(lane, 0) + 1
        per_lane[lane] = per_lane.get(lane, 0) + 1

    out_issues.sort(key=lambda i: -i["score"])

    return {
        "generated_at": iso(now),
        "lanes": LANES,
        "issues": out_issues,
        "summary": {
            "per_project": per_project,
            "per_lane": per_lane,
        },
    }


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------

def render_report(board: dict, *, generated_at: Optional[str] = None) -> str:
    generated_at = generated_at or board.get("generated_at", iso())
    by_lane: Dict[str, List[dict]] = {lane: [] for lane in LANES}
    for issue in board["issues"]:
        by_lane.setdefault(issue["lane"], []).append(issue)

    lines = [
        "# nervous-bus total board",
        "",
        f"Generated: {generated_at}",
        "",
        "## Per-project totals",
        "",
        "| project | " + " | ".join(LANES) + " | total |",
        "|---|" + "---|" * (len(LANES) + 1),
    ]
    for project in sorted(board["summary"]["per_project"]):
        counts = board["summary"]["per_project"][project]
        total = sum(counts.get(lane, 0) for lane in LANES)
        lines.append(
            f"| {project} | " + " | ".join(str(counts.get(lane, 0)) for lane in LANES)
            + f" | {total} |"
        )
    lines.append("")

    for lane in LANES:
        rows = sorted(by_lane.get(lane, []), key=lambda i: -i["score"])
        lines.append(f"## {lane} ({len(rows)})")
        lines.append("")
        for issue in rows:
            assignee = issue.get("assignee") or "unassigned"
            lines.append(
                f"P{issue['priority']} [{issue['project']}] {issue['id']} — {issue['title']} "
                f"({issue['age_days']:.1f}d, {assignee})"
            )
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Atomic write
# --------------------------------------------------------------------------

def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.rename(path)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(
    *,
    board_file: Path = BOARD_FILE,
    report_file: Path = REPORT_FILE,
    dolt_host: str = DOLT_HOST,
    dolt_port: int = DOLT_PORT,
    dolt_user: str = DOLT_USER,
    dolt_db: str = DOLT_DB,
    orca_db: Path = ORCA_DB,
    redis_url: str = REDIS_URL,
) -> dict:
    import pymysql
    import redis

    conn = pymysql.connect(host=dolt_host, port=dolt_port, user=dolt_user, database=dolt_db, connect_timeout=10)
    try:
        issues = fetch_issues(conn)
        blocked_by_map = fetch_open_blocked_by(conn)
    finally:
        conn.close()

    orca_state = fetch_orca_state(orca_db)

    r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    lifecycle_events = fetch_lifecycle_events(r)
    pr_map = fetch_pr_events(r)

    board = build_board(issues, blocked_by_map, orca_state, lifecycle_events, pr_map)

    write_atomic(board_file, json.dumps(board, indent=2, sort_keys=False))
    write_atomic(report_file, render_report(board))

    return board


def main() -> int:
    parser = argparse.ArgumentParser(description="board — federated total board across all nervous-bus projects")
    parser.add_argument("--board-file", type=Path, default=BOARD_FILE)
    parser.add_argument("--report-file", type=Path, default=REPORT_FILE)
    parser.add_argument("--dolt-host", default=DOLT_HOST)
    parser.add_argument("--dolt-port", type=int, default=DOLT_PORT)
    parser.add_argument("--dolt-user", default=DOLT_USER)
    parser.add_argument("--dolt-db", default=DOLT_DB)
    parser.add_argument("--orca-db", type=Path, default=ORCA_DB)
    parser.add_argument("--redis-url", default=REDIS_URL)
    args = parser.parse_args()

    board = run(
        board_file=args.board_file, report_file=args.report_file,
        dolt_host=args.dolt_host, dolt_port=args.dolt_port, dolt_user=args.dolt_user, dolt_db=args.dolt_db,
        orca_db=args.orca_db, redis_url=args.redis_url,
    )

    per_lane = board["summary"]["per_lane"]
    sys.stderr.write(
        f"[board] {len(board['issues'])} issues across {len(board['summary']['per_project'])} projects: "
        + ", ".join(f"{lane}={per_lane.get(lane, 0)}" for lane in LANES)
        + f" board={args.board_file} report={args.report_file}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
