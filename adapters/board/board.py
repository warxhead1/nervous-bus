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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

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

# --------------------------------------------------------------------------
# Signals -- additive top-level "signals" key (LANE Y). Sourced independently
# of the beads/dolt pipeline above: ci-watch's roster.json + its per-run
# state.json / dependabot.json artifacts, plus kb's own systemd/journal
# health. Deliberately kept OUT of build_board()/CONTRACT_TOP_LEVEL_KEYS --
# build_board()'s contract (generated_at/lanes/issues/summary) stays frozen
# and untouched; run() merges signals into the dict just before writing.
# --------------------------------------------------------------------------

CI_WATCH_DIR = Path(os.environ.get("NERVOUS_BOARD_CIWATCH_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "ci-watch")))
CI_WATCH_STATE_FILE = CI_WATCH_DIR / "state.json"
CI_WATCH_DEPENDABOT_FILE = CI_WATCH_DIR / "dependabot.json"
CI_WATCH_ROSTER_FILE = Path(os.environ.get(
    "NERVOUS_BOARD_ROSTER",
    str(Path(__file__).resolve().parent.parent / "ci-watch" / "roster.json"),
))

CONTRACT_SIGNAL_ENTRY_KEYS = {"ci", "dependabot", "kb"}
CONTRACT_CI_SIGNAL_KEYS = {"status", "failing_workflows", "updated_at"}
CONTRACT_DEPENDABOT_SIGNAL_KEYS = {"critical", "high", "moderate", "low", "updated_at"}
CONTRACT_KB_SIGNAL_KEYS = {"autoingest_fresh", "last_run_errors", "vet_backlog", "updated_at"}

KB_AUTOINGEST_UNIT = "kb-autoingest.service"
KB_AUTOINGEST_CADENCE_S = 30 * 60.0


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
# Signals: roster / CI join
# --------------------------------------------------------------------------

def load_roster_project_names(path: Path = CI_WATCH_ROSTER_FILE) -> List[str]:
    """Basenames (e.g. 'hearth-loom' from 'warxhead1/hearth-loom') of every
    repo in ci-watch's roster.json. Returns [] if the roster can't be read --
    signals degrades to whatever the dolt-derived project set already has,
    never raises and takes the whole board down with it.
    """
    try:
        with path.open() as f:
            roster = json.load(f)
    except Exception:
        return []
    return [entry["repo"].split("/", 1)[-1] for entry in roster.get("repos", []) if entry.get("repo")]


def load_ci_watch_state(path: Path = CI_WATCH_STATE_FILE) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


def group_ci_state_by_repo(ci_state: Dict[str, dict]) -> Dict[str, List[dict]]:
    """ci-watch's state.json is keyed 'owner/repo|workflow|branch' -> per
    (workflow,branch) verdict. Regroup by repo BASENAME (board's project
    naming) so each project's CI signal can be derived from all its
    workflow/branch entries at once.
    """
    out: Dict[str, List[dict]] = {}
    for key, entry in ci_state.items():
        parts = key.split("|")
        if len(parts) != 3 or not isinstance(entry, dict):
            continue
        repo, workflow, _branch = parts
        basename = repo.split("/", 1)[-1]
        out.setdefault(basename, []).append({**entry, "workflow": workflow})
    return out


def derive_ci_signal(repo_entries: Optional[List[dict]]) -> dict:
    """One project's ci-watch state entries -> {"status","failing_workflows",
    "updated_at"}. 'unknown' when there's no data, or when every sampled
    entry is pending/skipped/no-runs (never seen a real green or red).
    """
    if not repo_entries:
        return {"status": "unknown", "failing_workflows": [], "updated_at": None}

    inconclusive = {"pending", "skipped", "no-runs", None}
    states = {e.get("state") for e in repo_entries}
    failing = sorted({e["workflow"] for e in repo_entries if e.get("state") == "red"})
    updated_ats = [e.get("updated_at") for e in repo_entries if e.get("updated_at")]
    updated_at = max(updated_ats) if updated_ats else None

    if failing:
        status = "red"
    elif states - inconclusive:
        status = "green"
    else:
        status = "unknown"

    return {"status": status, "failing_workflows": failing, "updated_at": updated_at}


def load_dependabot_signal_file(path: Path = CI_WATCH_DEPENDABOT_FILE) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


def derive_dependabot_signal(raw: Optional[dict]) -> Optional[dict]:
    """ci-watch's dependabot.json entry for one repo -> the board contract's
    signal shape, or None (never fabricated zeros) when ci-watch itself
    recorded an error (403/scope-missing/transient) for that repo this poll,
    or when there's no entry at all (repo not in the roster / never polled).
    """
    if not raw or "error" in raw:
        return None
    try:
        return {
            "critical": int(raw.get("critical", 0)),
            "high": int(raw.get("high", 0)),
            "moderate": int(raw.get("moderate", 0)),
            "low": int(raw.get("low", 0)),
            "updated_at": raw.get("updated_at"),
        }
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Signals: kb health
# --------------------------------------------------------------------------

def _journalctl_json(unit: str, extra_args: Optional[List[str]] = None, timeout: int = 15) -> List[dict]:
    args = ["journalctl", "--user", "-u", unit, "-o", "json", "--no-pager"] + (extra_args or [])
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    entries = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def fetch_kb_autoingest_run_boundary(unit: str = KB_AUTOINGEST_UNIT) -> Tuple[Optional[float], Optional[float]]:
    """(prev_finish_epoch, last_finish_epoch) from the unit's journal, found
    by locating 'Finished kb autoingest' log lines -- the run boundary itself
    isn't otherwise exposed (no distinct start/end unit events land under
    this -u filter; verified 2026-08-30 against the live kb-autoingest.service
    journal, only Started/Finished/Consumed-CPU lines from the service's own
    stdout/systemd-generated summary are present). `prev_finish_epoch` bounds
    the error-counting window for the LAST run; None if there's only one run
    on record (errors then counted from the beginning of the retained log).
    """
    entries = _journalctl_json(unit)
    finishes: List[float] = []
    for e in entries:
        msg = e.get("MESSAGE")
        if isinstance(msg, str) and msg.startswith("Finished kb autoingest"):
            ts = e.get("__REALTIME_TIMESTAMP")
            if ts is not None:
                try:
                    finishes.append(int(ts) / 1_000_000.0)
                except (TypeError, ValueError):
                    continue
    if not finishes:
        return None, None
    finishes.sort()
    last = finishes[-1]
    prev = finishes[-2] if len(finishes) >= 2 else None
    return prev, last


def fetch_kb_autoingest_error_count(unit: str = KB_AUTOINGEST_UNIT, since_epoch: Optional[float] = None) -> Optional[int]:
    """Count of 'ERROR' lines in the unit's journal for its most recent run
    (since the previous run's finish, or from the start of the retained log
    if there's no earlier boundary). None if the journal can't be read at all
    (unit not present / journalctl unavailable) -- distinct from a real 0.
    """
    args = []
    if since_epoch is not None:
        since_str = datetime.fromtimestamp(since_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        args = ["--since", since_str]
    entries = _journalctl_json(unit, args)
    if not entries and since_epoch is None:
        # Genuinely no journal for this unit at all (vs. "since" just
        # returning nothing because the run was quiet) -- distinguish by
        # re-querying without a --since bound.
        probe = _journalctl_json(unit)
        if not probe:
            return None
    count = 0
    for e in entries:
        msg = e.get("MESSAGE")
        if isinstance(msg, str) and "ERROR" in msg:
            count += 1
    return count


def fetch_kb_vet_backlog(timeout: int = 30) -> Optional[int]:
    """Total line count of `kb vet-pending` (entries with
    empirically_testable=true and no evidence chain) -- the cheapest real
    listing kb exposes for this (checked `kb --help`: `vet` itself requires
    --result/--session for a single entry, not a list; `vet-pending` is
    exactly the bounded list this signal needs). None if the `kb` binary
    isn't available or the call fails -- never a fabricated 0.
    """
    try:
        out = subprocess.run(["kb", "vet-pending"], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return len([line for line in out.stdout.splitlines() if line.strip()])


def derive_kb_signal(*, prev_finish_epoch: Optional[float], last_finish_epoch: Optional[float],
                      error_count: Optional[int], vet_backlog: Optional[int],
                      cadence_s: float = KB_AUTOINGEST_CADENCE_S, now: Optional[float] = None) -> Optional[dict]:
    """None (whole signal absent) only when kb-autoingest has never run at
    all in the retained journal -- kb genuinely not installed/configured on
    this box. Otherwise always the full shape with error_count/vet_backlog
    individually nullable per their own fetch failures.
    """
    now = now if now is not None else time.time()
    if last_finish_epoch is None:
        return None
    return {
        "autoingest_fresh": (now - last_finish_epoch) <= (cadence_s * 2),
        "last_run_errors": error_count,
        "vet_backlog": vet_backlog,
        "updated_at": iso(last_finish_epoch),
    }


def fetch_kb_signal(*, unit: str = KB_AUTOINGEST_UNIT, now: Optional[float] = None) -> Optional[dict]:
    prev, last = fetch_kb_autoingest_run_boundary(unit)
    error_count = fetch_kb_autoingest_error_count(unit, since_epoch=prev) if last is not None else None
    vet_backlog = fetch_kb_vet_backlog() if last is not None else None
    return derive_kb_signal(
        prev_finish_epoch=prev, last_finish_epoch=last,
        error_count=error_count, vet_backlog=vet_backlog, now=now,
    )


# --------------------------------------------------------------------------
# Signals: assembly
# --------------------------------------------------------------------------

def build_signals(project_names: Iterable[str], *, ci_by_repo: Dict[str, List[dict]],
                   dependabot_raw: Dict[str, dict], kb_signal: Optional[dict]) -> Dict[str, dict]:
    """Union of every board project name and every ci-watch roster repo
    basename (so e.g. 'orca' gets a real dependabot/ci row even though it
    has no beads DB / isn't one of the 10 federated project DBs) -> the
    per-project signals entry. Every entry always carries all three keys
    (ci/dependabot/kb); a project with no data for one gets that field's
    'no data' value (unknown / null / null), never an omitted key.
    """
    out: Dict[str, dict] = {}
    for project in sorted(set(project_names)):
        out[project] = {
            "ci": derive_ci_signal(ci_by_repo.get(project)),
            "dependabot": derive_dependabot_signal(dependabot_raw.get(project)),
            "kb": kb_signal,
        }
    return out


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
    ]

    signals = board.get("signals")
    if signals:
        lines.append("## Signals")
        lines.append("")
        lines.append("| project | CI | dependabot C/H/M/L | kb |")
        lines.append("|---|---|---|---|")
        for project in sorted(signals):
            sig = signals[project]
            ci = sig.get("ci") or {}
            ci_cell = ci.get("status", "unknown")
            if ci.get("failing_workflows"):
                ci_cell += f" ({', '.join(ci['failing_workflows'])})"
            dep = sig.get("dependabot")
            dep_cell = (
                f"{dep['critical']}/{dep['high']}/{dep['moderate']}/{dep['low']}" if dep else "null"
            )
            kb = sig.get("kb")
            if kb:
                kb_cell = (
                    f"fresh={kb['autoingest_fresh']} errors={kb['last_run_errors']} "
                    f"vet_backlog={kb['vet_backlog']}"
                )
            else:
                kb_cell = "null"
            lines.append(f"| {project} | {ci_cell} | {dep_cell} | {kb_cell} |")
        lines.append("")

    lines += [
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

    project_names = set(board["summary"]["per_project"].keys()) | set(load_roster_project_names())
    ci_by_repo = group_ci_state_by_repo(load_ci_watch_state())
    dependabot_raw = load_dependabot_signal_file()
    kb_signal = fetch_kb_signal()
    board["signals"] = build_signals(
        project_names, ci_by_repo=ci_by_repo, dependabot_raw=dependabot_raw, kb_signal=kb_signal,
    )

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
