#!/usr/bin/env python3
"""nervous-bus persistent dead-letter queue (DLQ).

Subscribes to ``nbus:bus.dead_letter`` Redis stream via an XREADGROUP
consumer group (``dlq-drain`` by default) and persists each event into a
SQLite database at ``~/.cache/nervous-bus/dlq.db``, PLUS a durable
size-bounded JSONL sink and a rolling per-channel violation summary — see
"Durable drain (consumer group)" below. This is the first real
XREADGROUP/XACK consumer on the bus: before this, ``nbus:bus.dead_letter``
sat pinned at its MAXLEN cap (~10000) with zero consumer groups anywhere in
the ecosystem, so entries were silently trimmed (dropped) once the stream
filled, and no channel anywhere had at-least-once delivery.

Durable drain (consumer group):
  - ``XGROUP CREATE nbus:bus.dead_letter dlq-drain 0 MKSTREAM`` (idempotent;
    BUSYGROUP is swallowed). Starting the group at id ``0`` means a brand
    new group also drains whatever backlog is currently sitting in the
    stream, not just entries that arrive after the group is created.
  - Each read is ``XREADGROUP GROUP dlq-drain <consumer> COUNT 100 BLOCK 2000
    STREAMS nbus:bus.dead_letter >``. An entry is XACK'd only AFTER it has
    been durably written to BOTH the SQLite table (via insert_dead_letter,
    idempotent on id) and the JSONL sink (append_jsonl). If the process dies
    between XREADGROUP and XACK, the entry stays in the group's pending
    entries list (PEL) and is reclaimed by ``reclaim_stale_pending()`` (an
    XAUTOCLAIM sweep for entries idle longer than ``STALE_IDLE_MS``) rather
    than lost — that's what makes this at-least-once instead of best-effort.
  - Sink: JSONL under ``~/.cache/nervous-bus/dlq/dead_letter-YYYYMMDD.jsonl``,
    rotated within a day to ``-N`` suffix files once a file exceeds
    ``JSONL_MAX_BYTES`` (~50MB), with day-files older than
    ``JSONL_RETENTION_DAYS`` pruned. This directory is already under
    ``~/.cache`` (private) so full dead-letter records — including
    ``original_payload_excerpt`` — are safe to retain there for forensics,
    matching the existing quarantine-archive precedent below.
  - Rolling per-channel/reason counts are written atomically (tmp+rename) to
    ``~/.cache/nervous-bus/dlq/summary.json`` after every batch, so a session
    can read standing at a glance without querying the DB.
  - REDACTION IS LOAD-BEARING: ``original_payload_excerpt`` (and the rest of
    the raw dead-letter body) is NEVER republished to the bus. The only
    thing this adapter optionally publishes back (``bus.dlq.summary.v1``,
    every ``--publish-interval`` seconds when non-zero) carries channel name
    + failure_reason + count ONLY — see publish_summary_event().

Retry policy:
  - ``failure_reason`` in NON_RETRYABLE_REASONS (``schema_violation``,
    ``malformed_json``, ``missing_type``, ``missing_required_field``) →
    quarantine, no retry. These all mean the emitter/payload is wrong, not
    the bus — redelivering the same bytes just re-fails the same way. (The
    latter three were added after a 2026-07 incident where retrying
    ``event_type=UNKNOWN`` / non-JSON excerpts caused a self-amplifying
    republish loop — see NON_RETRYABLE_REASONS docstring.) Quarantined
    entries are archived verbatim (one JSON line per entry) to
    ``~/.cache/nervous-bus/dlq-quarantine.jsonl`` for forensics/replay-after-fix,
    then marked terminal in the DB (``quarantined_at`` + ``resolved_at`` both
    set — they no longer show up in the "needs attention" unresolved view,
    but the archive + DB row are never deleted).
  - retry_worker() also refuses to retry (and abandons via the same terminal
    mechanism) any row whose event_type is "UNKNOWN" (case-insensitive) or
    whose original_payload fails json.loads, regardless of failure_reason —
    defense in depth for rows scheduled before the checks above existed.
  - All other reasons → exponential backoff: 30 s, 5 min, 30 min (max 3 retries)
  - On retry: re-emits original payload via ``nervous publish``

HTTP endpoints:
  GET /dlq?limit=50   — returns JSON array of unresolved dead-letter entries.
                        Intended for consumption by the Sysmap Stream tab.
  GET /dlq/stats       — terminal-state counters: quarantined_total,
                        retried_ok_total, pending_total.

Run::

    python adapters/dlq/dlq.py [--port 9419] [--valkey-url redis://localhost:6379]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import redis

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "dlq.db"
DEFAULT_QUARANTINE_PATH = Path.home() / ".cache" / "nervous-bus" / "dlq-quarantine.jsonl"
DEFAULT_JSONL_DIR = Path.home() / ".cache" / "nervous-bus" / "dlq"
DEFAULT_PORT = 9419
VALKEY_URL = "redis://localhost:6379"
DLQ_STREAM = "nbus:bus.dead_letter"
CONSUMER_GROUP = "dlq-drain"

# JSONL sink rotation/retention.
JSONL_MAX_BYTES = 50 * 1024 * 1024  # ~50MB per file before rotating to -N suffix
JSONL_RETENTION_DAYS = 14

# XAUTOCLAIM: an entry idle (no XACK) longer than this is assumed to belong
# to a dead/stuck consumer and is reclaimed for reprocessing by whichever
# consumer runs the sweep next.
STALE_IDLE_MS = 60_000

# Retry backoff schedule (seconds) — index = retry_count (0-based)
RETRY_BACKOFF = [30, 300, 1800]  # 30s, 5min, 30min

# failure_reason values that are never retryable — the emitter produced a
# payload the schema/envelope validator rejects, so redelivering the exact
# same bytes fails the exact same way. These get quarantined, not retried.
#
# malformed_json / missing_type / missing_required_field were added after a
# 2026-07 incident: rows classified with these reasons by hearth-api's tail
# consumer carry event_type "UNKNOWN" and a truncated, non-JSON
# original_payload excerpt. Retrying via `nervous publish` republished that
# garbage, which hearth-api dead-lettered again under a *new* uuid (so
# retry_count never exhausted) — a self-amplifying republish loop. See also
# the belt-and-suspenders checks in retry_worker() below, which catch rows
# that reach the retry path despite this set (e.g. legacy rows inserted
# before this set grew).
NON_RETRYABLE_REASONS = {
    "schema_violation",
    "malformed_json",
    "missing_type",
    "missing_required_field",
}

NBUS_ROOT = Path(__file__).parent.parent.parent


# ── Database ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    source TEXT,
    failure_reason TEXT NOT NULL,
    schema_violation_detail TEXT,
    original_payload TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    next_retry_at REAL,
    resolved_at REAL,
    quarantined_at REAL
);
CREATE INDEX IF NOT EXISTS idx_unresolved ON dead_letters(resolved_at, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_created_at ON dead_letters(created_at);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Migration for pre-existing DBs created before quarantined_at existed.
    try:
        conn.execute("ALTER TABLE dead_letters ADD COLUMN quarantined_at REAL")
    except sqlite3.OperationalError:
        pass  # column already present
    conn.commit()
    return conn


def archive_quarantined(archive_path: Path, entry: dict, event_id: str,
                         failure_reason: str, created_at: float) -> None:
    """Append one quarantined dead-letter to the durable JSONL archive.

    Called BEFORE the DB row is marked terminal, so a crash between the two
    writes leaves the archive complete (at worst a duplicate re-append on
    restart, never a silent drop) — archive-then-mark, not mark-then-archive.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "id": event_id,
        "failure_reason": failure_reason,
        "created_at": created_at,
        "quarantined_at": time.time(),
        **entry,
    }, default=str)
    try:
        with open(archive_path, "a") as fh:
            fh.write(line + "\n")
    except Exception as e:
        sys.stderr.write(f"[dlq] quarantine archive write failed: {e}\n")


def insert_dead_letter(conn: sqlite3.Connection, entry: dict,
                        archive_path: Path = DEFAULT_QUARANTINE_PATH) -> None:
    """Insert a new dead-letter entry. Idempotent on id collision.

    schema_violation entries are archived + marked terminal (quarantined)
    rather than left to sit unresolved forever — see module docstring.
    """
    now = time.time()
    event_id = entry.get("id") or entry.get("event_id") or ""
    event_type = entry.get("original_type") or entry.get("event_type") or "unknown"
    source = entry.get("source") or ""
    failure_reason = entry.get("failure_reason") or "unknown"
    schema_detail = entry.get("schema_violation_detail") or ""
    original_payload = entry.get("original_payload_excerpt") or json.dumps(entry)

    non_retryable = failure_reason in NON_RETRYABLE_REASONS
    quarantined_at: Optional[float] = None
    resolved_at: Optional[float] = None

    if non_retryable and event_id:
        archive_quarantined(archive_path, entry, event_id, failure_reason, now)
        quarantined_at = time.time()
        resolved_at = quarantined_at  # terminal: excluded from "needs attention"
        next_retry_at = None
    elif non_retryable or event_id == "":
        # No event_id means we can't safely retry (nothing to re-publish) —
        # leave it unresolved/inert rather than quarantine, since it isn't
        # actually a confirmed schema violation.
        next_retry_at = None
    else:
        next_retry_at = now + RETRY_BACKOFF[0]

    try:
        conn.execute(
            """INSERT OR IGNORE INTO dead_letters
               (id, event_type, source, failure_reason, schema_violation_detail,
                original_payload, retry_count, created_at, next_retry_at,
                resolved_at, quarantined_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
            (event_id, event_type, source, failure_reason, schema_detail,
             original_payload, now, next_retry_at, resolved_at, quarantined_at),
        )
        conn.commit()
    except Exception as e:
        sys.stderr.write(f"[dlq] insert failed: {e}\n")


def get_unresolved(conn: sqlite3.Connection, limit: int = 50) -> list:
    rows = conn.execute(
        """SELECT id, event_type, source, failure_reason, schema_violation_detail,
                  original_payload, retry_count, last_error, created_at,
                  next_retry_at, resolved_at, quarantined_at
           FROM dead_letters
           WHERE resolved_at IS NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_counts(conn: sqlite3.Connection) -> dict:
    """Terminal-state counters for the /dlq/stats endpoint and reporting."""
    row = conn.execute(
        """SELECT
             COUNT(*) AS total,
             SUM(CASE WHEN quarantined_at IS NOT NULL THEN 1 ELSE 0 END) AS quarantined_total,
             SUM(CASE WHEN resolved_at IS NOT NULL AND quarantined_at IS NULL
                      THEN 1 ELSE 0 END) AS retried_ok_total,
             SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS pending_total
           FROM dead_letters"""
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "quarantined_total": row["quarantined_total"] or 0,
        "retried_ok_total": row["retried_ok_total"] or 0,
        "pending_total": row["pending_total"] or 0,
    }


def get_pending_retries(conn: sqlite3.Connection) -> list:
    now = time.time()
    # Exclude all NON_RETRYABLE_REASONS, not just schema_violation — belt and
    # suspenders alongside insert_dead_letter's next_retry_at=None for these
    # reasons, in case a row was scheduled before this set grew.
    placeholders = ",".join("?" for _ in NON_RETRYABLE_REASONS)
    rows = conn.execute(
        f"""SELECT id, event_type, original_payload, retry_count, failure_reason
           FROM dead_letters
           WHERE resolved_at IS NULL
             AND failure_reason NOT IN ({placeholders})
             AND next_retry_at IS NOT NULL
             AND next_retry_at <= ?
             AND retry_count < 3""",
        (*NON_RETRYABLE_REASONS, now),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_retry_success(conn: sqlite3.Connection, entry_id: str) -> None:
    conn.execute(
        "UPDATE dead_letters SET resolved_at=?, last_error=NULL WHERE id=?",
        (time.time(), entry_id),
    )
    conn.commit()


def mark_abandoned(conn: sqlite3.Connection, entry_id: str, archive_path: Path,
                    entry: dict, reason: str) -> None:
    """Terminal, non-retryable — same semantics as insert-time quarantine
    (archive_quarantined + quarantined_at + resolved_at both set), reused
    here for rows that reach the retry path despite NON_RETRYABLE_REASONS
    (e.g. rows scheduled before this set grew, or a payload that only turns
    out to be un-parseable at retry time). Never calls `nervous publish`.
    """
    now = time.time()
    archive_quarantined(archive_path, entry, entry_id, reason, now)
    conn.execute(
        """UPDATE dead_letters
           SET quarantined_at=?, resolved_at=?, last_error=?, next_retry_at=NULL
           WHERE id=?""",
        (now, now, reason[:500], entry_id),
    )
    conn.commit()


def mark_retry_failed(conn: sqlite3.Connection, entry_id: str, error: str, retry_count: int) -> None:
    next_retry = retry_count + 1
    if next_retry < len(RETRY_BACKOFF):
        next_retry_at = time.time() + RETRY_BACKOFF[next_retry]
    else:
        next_retry_at = None  # exhausted
    conn.execute(
        """UPDATE dead_letters
           SET retry_count=?, last_error=?, next_retry_at=?
           WHERE id=?""",
        (next_retry, error[:500], next_retry_at, entry_id),
    )
    conn.commit()


# ── Retry worker ──────────────────────────────────────────────────────────────

def _retry_event(entry: dict) -> tuple[bool, str]:
    """Attempt to re-emit via ``nervous publish``. Returns (success, error_msg)."""
    payload = entry.get("original_payload") or ""
    event_type = entry.get("event_type") or "bus.dead_letter"

    try:
        result = subprocess.run(
            ["nervous", "publish", event_type, payload],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(NBUS_ROOT),
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout or "non-zero exit")[:500]
    except subprocess.TimeoutExpired:
        return False, "timeout after 10s"
    except FileNotFoundError:
        return False, "'nervous' CLI not found in PATH"
    except Exception as e:
        return False, str(e)[:500]


def retry_worker(conn: sqlite3.Connection, stop_event: threading.Event,
                  archive_path: Path = DEFAULT_QUARANTINE_PATH) -> None:
    """Background thread: check for due retries every 10 s.

    Two belt-and-suspenders guards run before any `nervous publish` shell-out
    (see NON_RETRYABLE_REASONS docstring for the incident that motivated
    them): a row with event_type "UNKNOWN" (case-insensitive) or an
    original_payload that doesn't parse as JSON is abandoned instead of
    retried, even if it reached this point with a scheduled next_retry_at.
    """
    while not stop_event.is_set():
        try:
            pending = get_pending_retries(conn)
            for entry in pending:
                entry_id = entry["id"]
                retry_count = entry["retry_count"]
                event_type = (entry.get("event_type") or "").strip()

                if event_type.upper() == "UNKNOWN":
                    sys.stderr.write(
                        f"[dlq] abandoning {entry_id}: event_type is UNKNOWN, "
                        f"refusing to retry (would republish garbage)\n"
                    )
                    mark_abandoned(conn, entry_id, archive_path, entry, "unknown_event_type")
                    continue

                payload = entry.get("original_payload") or ""
                try:
                    json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    sys.stderr.write(
                        f"[dlq] abandoning {entry_id}: original_payload is not "
                        f"valid JSON, refusing to retry\n"
                    )
                    mark_abandoned(conn, entry_id, archive_path, entry, "invalid_payload_json")
                    continue

                sys.stderr.write(
                    f"[dlq] retrying {entry_id} (attempt {retry_count + 1}/3) "
                    f"type={entry['event_type']}\n"
                )
                success, error = _retry_event(entry)
                if success:
                    mark_retry_success(conn, entry_id)
                    sys.stderr.write(f"[dlq] retry succeeded: {entry_id}\n")
                else:
                    mark_retry_failed(conn, entry_id, error, retry_count)
                    sys.stderr.write(f"[dlq] retry failed ({error}): {entry_id}\n")
        except Exception as e:
            sys.stderr.write(f"[dlq] retry_worker error: {e}\n")
        stop_event.wait(10.0)


# ── Durable JSONL sink (size-bounded rotation) ──────────────────────────────────

def _day_stamp(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y%m%d")


def _jsonl_path_for_day(jsonl_dir: Path, day: str, max_bytes: int = JSONL_MAX_BYTES) -> Path:
    """Return the current (possibly rotated) file to append to for ``day``.

    Base name is ``dead_letter-YYYYMMDD.jsonl``; once that file would exceed
    max_bytes, subsequent writes roll to ``dead_letter-YYYYMMDD.1.jsonl``,
    ``.2.jsonl``, etc. Returns whichever numbered file is currently under cap
    (or the base file if none exist yet / all are exhausted only the base is
    checked first, so we always append to the highest-numbered non-full file).
    """
    base = jsonl_dir / f"dead_letter-{day}.jsonl"
    candidates = [base]
    n = 1
    while True:
        p = jsonl_dir / f"dead_letter-{day}.{n}.jsonl"
        if not p.exists():
            candidates.append(p)
            break
        candidates.append(p)
        n += 1
    for p in candidates:
        if not p.exists() or p.stat().st_size < max_bytes:
            return p
    return candidates[-1]


def append_jsonl(jsonl_dir: Path, entry: dict, max_bytes: int = JSONL_MAX_BYTES) -> Path:
    """Append one dead-letter record to the size-bounded, day-stamped JSONL sink.

    Full record (including original_payload_excerpt) is written here — this
    directory lives under ~/.cache (private), same trust boundary as the
    existing dlq-quarantine.jsonl archive. Never republished to the bus.
    """
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    day = _day_stamp(entry.get("created_at"))
    path = _jsonl_path_for_day(jsonl_dir, day, max_bytes=max_bytes)
    line = json.dumps(entry, default=str)
    with open(path, "a") as fh:
        fh.write(line + "\n")
        if fh.tell() >= max_bytes:
            pass  # next append recomputes path and rotates
    return path


def prune_old_jsonl(jsonl_dir: Path, retention_days: int = JSONL_RETENTION_DAYS) -> int:
    """Delete dead_letter-*.jsonl files older than retention_days. Returns count removed."""
    if not jsonl_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for p in jsonl_dir.glob("dead_letter-*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# ── Rolling per-channel violation summary ───────────────────────────────────────

class SummaryTracker:
    """In-memory rolling counts, flushed atomically to summary.json.

    Keyed by (channel, failure_reason). Never stores payload content — only
    channel name, failure reason, and counts, matching the redaction contract
    for anything that might later be published to the bus.
    """

    def __init__(self, summary_path: Path):
        self.summary_path = summary_path
        self._lock = threading.Lock()
        self.counts: dict[tuple[str, str], int] = {}
        self.since_start_total = 0
        self.window_start = time.time()
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.summary_path.exists():
            return
        try:
            data = json.loads(self.summary_path.read_text())
            self.since_start_total = int(data.get("since_start_total", 0))
        except Exception:
            pass

    def record(self, channel: str, failure_reason: str) -> None:
        with self._lock:
            key = (channel or "unknown", failure_reason or "unknown")
            self.counts[key] = self.counts.get(key, 0) + 1
            self.since_start_total += 1

    def snapshot_and_reset_window(self) -> dict:
        """Return the current window's rollup dict and reset the window counts
        (since_start_total is cumulative and NOT reset)."""
        with self._lock:
            by_channel = [
                {"channel": c, "failure_reason": r, "count": n}
                for (c, r), n in sorted(self.counts.items(), key=lambda kv: -kv[1])
            ]
            total = sum(self.counts.values())
            out = {
                "window_start": datetime.fromtimestamp(self.window_start, tz=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "window_end": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "total_count": total,
                "by_channel": by_channel,
                "since_start_total": self.since_start_total,
            }
            self.counts = {}
            self.window_start = time.time()
            return out

    def flush(self) -> None:
        """Write the live (non-window-reset) standing summary to disk atomically."""
        with self._lock:
            by_channel = [
                {"channel": c, "failure_reason": r, "count": n}
                for (c, r), n in sorted(self.counts.items(), key=lambda kv: -kv[1])
            ]
            data = {
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "window_start": datetime.fromtimestamp(self.window_start, tz=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "current_window_total": sum(self.counts.values()),
                "since_start_total": self.since_start_total,
                "by_channel": by_channel,
            }
        try:
            self.summary_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.summary_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(self.summary_path)
        except Exception as e:
            sys.stderr.write(f"[dlq] summary flush failed: {e}\n")


def publish_summary_event(tracker: "SummaryTracker") -> None:
    """Publish bus.dlq.summary.v1 with channel+reason+count ONLY — never payload
    bytes. Best-effort: shells out to `nervous publish`; failures are logged,
    never fatal, and never retried through the DLQ itself (that would be
    self-referential)."""
    rollup = tracker.snapshot_and_reset_window()
    if rollup["total_count"] == 0:
        return  # nothing new this window — skip the publish, don't spam the bus
    payload = json.dumps(rollup)
    try:
        result = subprocess.run(
            ["nervous", "publish", "bus.dlq.summary.v1", payload],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(NBUS_ROOT),
        )
        if result.returncode != 0:
            sys.stderr.write(
                f"[dlq] summary publish failed: {(result.stderr or result.stdout)[:300]}\n"
            )
    except Exception as e:
        sys.stderr.write(f"[dlq] summary publish error: {e}\n")


def summary_publish_worker(tracker: "SummaryTracker", stop_event: threading.Event,
                            interval_s: float) -> None:
    if interval_s <= 0:
        return
    while not stop_event.wait(interval_s):
        publish_summary_event(tracker)


# ── Valkey subscriber ─────────────────────────────────────────────────────────

def _merge_fields(entry_id: str, fields: dict) -> dict:
    raw = fields.get("_raw", "")
    try:
        envelope = json.loads(raw)
        data = envelope.get("data") or {}
    except Exception:
        envelope = {}
        data = {k: v for k, v in fields.items()}

    merged = {
        "id": (fields.get("event_id") or envelope.get("id")) if raw else entry_id,
        "source": fields.get("source") or "",
        **data,
    }
    if "failure_reason" not in merged and "reason" in merged:
        merged["failure_reason"] = merged["reason"]
    if "original_type" not in merged and "channel" in merged:
        merged["original_type"] = merged["channel"]
    return merged


def ensure_consumer_group(r: redis.Redis, stream: str, group: str) -> None:
    """XGROUP CREATE ... MKSTREAM, idempotent (BUSYGROUP == already exists)."""
    try:
        r.xgroup_create(stream, group, id="0", mkstream=True)
        sys.stderr.write(f"[dlq] created consumer group {group!r} on {stream}\n")
    except redis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def reclaim_stale_pending(r: redis.Redis, stream: str, group: str, consumer: str,
                           min_idle_ms: int = STALE_IDLE_MS, count: int = 100) -> list:
    """XAUTOCLAIM entries idle longer than min_idle_ms onto `consumer`.

    This is what makes delivery at-least-once rather than best-effort: if a
    consumer dies between XREADGROUP and XACK, the entry sits in the PEL and
    gets reclaimed here (by this consumer or another one running the same
    sweep) instead of being lost.
    """
    try:
        _cursor, entries, _deleted = r.xautoclaim(
            stream, group, consumer, min_idle_time=min_idle_ms, start_id="0-0", count=count
        )
        return entries
    except redis.ResponseError as e:
        # NOGROUP means the group vanished (stream flushed) — caller will
        # recreate it on the next ensure_consumer_group call.
        sys.stderr.write(f"[dlq] reclaim_stale_pending error: {e}\n")
        return []


def process_entry(conn: sqlite3.Connection, entry_id: str, fields: dict,
                   archive_path: Path, jsonl_dir: Path,
                   tracker: "SummaryTracker") -> dict:
    """Durably persist one dead-letter entry (SQLite row + JSONL sink +
    summary counter). Does NOT ack — caller acks after this returns
    successfully, so a crash mid-write leaves the entry pending for
    reclaim_stale_pending() rather than silently dropped."""
    merged = _merge_fields(entry_id, fields)
    merged.setdefault("created_at", time.time())
    insert_dead_letter(conn, merged, archive_path=archive_path)
    append_jsonl(jsonl_dir, merged)
    tracker.record(merged.get("original_type", "unknown"), merged.get("failure_reason", "unknown"))
    return merged


def consumer_group_worker(
    conn: sqlite3.Connection,
    valkey_url: str,
    stop_event: threading.Event,
    archive_path: Path,
    jsonl_dir: Path,
    tracker: "SummaryTracker",
    group: str = CONSUMER_GROUP,
    consumer_name: Optional[str] = None,
) -> None:
    """Drain nbus:bus.dead_letter via XREADGROUP/XACK (consumer group `group`).

    This is the first real at-least-once consumer on the bus: previously
    nbus:bus.dead_letter (and every other nbus:* stream) had zero consumer
    groups, so it was only ever XREAD-tailed (fire-and-forget) or left to
    trim silently at MAXLEN.
    """
    consumer_name = consumer_name or f"{socket.gethostname()}-{os.getpid()}"
    r: Optional[redis.Redis] = None
    last_reclaim = 0.0
    last_prune = 0.0

    while not stop_event.is_set():
        if r is None:
            try:
                r = redis.Redis.from_url(
                    valkey_url,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=3,
                )
                r.ping()
                ensure_consumer_group(r, DLQ_STREAM, group)
                sys.stderr.write(
                    f"[dlq] connected to Valkey, draining {DLQ_STREAM} "
                    f"via group={group!r} consumer={consumer_name!r}\n"
                )
            except Exception as e:
                sys.stderr.write(f"[dlq] Valkey connect failed: {e}\n")
                r = None
                stop_event.wait(5.0)
                continue

        try:
            now = time.time()
            if now - last_reclaim > 30.0:
                stale = reclaim_stale_pending(r, DLQ_STREAM, group, consumer_name)
                for entry_id, fields in stale:
                    process_entry(conn, entry_id, fields, archive_path, jsonl_dir, tracker)
                    r.xack(DLQ_STREAM, group, entry_id)
                    sys.stderr.write(f"[dlq] reclaimed+acked stale pending: {entry_id}\n")
                last_reclaim = now

            if now - last_prune > 3600.0:
                removed = prune_old_jsonl(jsonl_dir)
                if removed:
                    sys.stderr.write(f"[dlq] pruned {removed} JSONL file(s) past retention\n")
                last_prune = now

            results = r.xreadgroup(
                group, consumer_name, {DLQ_STREAM: ">"}, count=100, block=2000
            )
            if not results:
                tracker.flush()
                continue
            for _stream, entries in results:
                for entry_id, fields in entries:
                    try:
                        merged = process_entry(conn, entry_id, fields, archive_path, jsonl_dir, tracker)
                        r.xack(DLQ_STREAM, group, entry_id)
                        sys.stderr.write(
                            f"[dlq] persisted+acked dead_letter: {merged.get('id', entry_id)} "
                            f"reason={merged.get('failure_reason', '?')}\n"
                        )
                    except Exception as e:
                        # Do NOT ack — leave pending for reclaim_stale_pending().
                        sys.stderr.write(f"[dlq] processing failed for {entry_id} (left pending): {e}\n")
            tracker.flush()
        except redis.RedisError as e:
            sys.stderr.write(f"[dlq] Valkey read error: {e}\n")
            r = None
            stop_event.wait(3.0)
        except Exception as e:
            sys.stderr.write(f"[dlq] consumer_group_worker error: {e}\n")
            stop_event.wait(1.0)


# ── HTTP server ───────────────────────────────────────────────────────────────

def _make_handler(conn: sqlite3.Connection, summary_path: Path = None):
    _conn_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)

            if parsed.path in ("/dlq/stats", "/dlq/stats/"):
                with _conn_lock:
                    stats = get_counts(conn)
                body = json.dumps(stats, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path in ("/dlq/summary", "/dlq/summary/"):
                if summary_path and summary_path.exists():
                    body = summary_path.read_bytes()
                else:
                    body = json.dumps({"error": "no summary written yet"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path not in ("/dlq", "/dlq/"):
                self.send_response(404)
                self.end_headers()
                return

            params = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(params.get("limit", ["50"])[0])
                limit = max(1, min(limit, 500))
            except ValueError:
                limit = 50

            with _conn_lock:
                rows = get_unresolved(conn, limit=limit)

            body = json.dumps({"count": len(rows), "entries": rows}, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # suppress request logs

    return Handler


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port for GET /dlq (default: {DEFAULT_PORT})")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help=f"SQLite DB path (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--valkey-url", default=VALKEY_URL,
                        help=f"Valkey/Redis URL (default: {VALKEY_URL})")
    parser.add_argument("--quarantine-file", type=Path, default=DEFAULT_QUARANTINE_PATH,
                        help=f"JSONL archive for quarantined (non-retryable) "
                             f"dead letters (default: {DEFAULT_QUARANTINE_PATH})")
    parser.add_argument("--jsonl-dir", type=Path, default=DEFAULT_JSONL_DIR,
                        help=f"Durable size-bounded JSONL sink dir (default: {DEFAULT_JSONL_DIR})")
    parser.add_argument("--consumer-group", default=CONSUMER_GROUP,
                        help=f"XREADGROUP consumer group name (default: {CONSUMER_GROUP})")
    parser.add_argument("--consumer-name", default=None,
                        help="XREADGROUP consumer name (default: <hostname>-<pid>)")
    parser.add_argument("--publish-interval", type=float, default=300.0,
                        help="Seconds between bus.dlq.summary.v1 publishes; "
                             "0 disables publishing (default: 300)")
    parser.add_argument("--list", action="store_true",
                        help="Print unresolved entries as JSON and exit")
    parser.add_argument("--stats", action="store_true",
                        help="Print terminal-state counters as JSON and exit")
    args = parser.parse_args(argv)

    conn = open_db(args.db)

    if args.stats:
        print(json.dumps(get_counts(conn), indent=2, default=str))
        return 0

    if args.list:
        rows = get_unresolved(conn, limit=100)
        print(json.dumps({"count": len(rows), "entries": rows}, indent=2, default=str))
        return 0

    stop_event = threading.Event()
    summary_path = args.jsonl_dir / "summary.json"
    tracker = SummaryTracker(summary_path)

    sub_thread = threading.Thread(
        target=consumer_group_worker,
        args=(conn, args.valkey_url, stop_event, args.quarantine_file, args.jsonl_dir, tracker,
              args.consumer_group, args.consumer_name),
        daemon=True,
        name="dlq_subscriber",
    )
    sub_thread.start()

    retry_thread = threading.Thread(
        target=retry_worker,
        args=(conn, stop_event, args.quarantine_file),
        daemon=True,
        name="dlq_retry",
    )
    retry_thread.start()

    publish_thread = threading.Thread(
        target=summary_publish_worker,
        args=(tracker, stop_event, args.publish_interval),
        daemon=True,
        name="dlq_summary_publish",
    )
    publish_thread.start()

    handler = _make_handler(conn, summary_path)
    httpd = HTTPServer(("0.0.0.0", args.port), handler)
    http_thread = threading.Thread(
        target=httpd.serve_forever,
        daemon=True,
        name="dlq_http",
    )
    http_thread.start()

    print(
        f"nbus DLQ daemon: db={args.db}  HTTP=:{args.port}/dlq (+/dlq/stats, /dlq/summary)  "
        f"stream={DLQ_STREAM}  group={args.consumer_group}  valkey={args.valkey_url}  "
        f"quarantine={args.quarantine_file}  jsonl-dir={args.jsonl_dir}  "
        f"publish-interval={args.publish_interval}s"
    )

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_event.set()
        tracker.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
