#!/usr/bin/env python3
"""nervous-bus persistent dead-letter queue (DLQ).

Subscribes to ``nbus:bus.dead_letter`` Redis stream and persists each event
into a SQLite database at ``~/.cache/nervous-bus/dlq.db``.

Retry policy:
  - ``failure_reason == "schema_violation"`` → no retry (fix the emitter)
  - All other reasons → exponential backoff: 30 s, 5 min, 30 min (max 3 retries)
  - On retry: re-emits original payload via ``nervous publish``

HTTP endpoint:
  GET /dlq?limit=50  — returns JSON array of unresolved dead-letter entries.
  Intended for consumption by the Sysmap Stream tab.

Run::

    python adapters/dlq/dlq.py [--port 9419] [--valkey-url redis://localhost:6379]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import redis

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "dlq.db"
DEFAULT_PORT = 9419
VALKEY_URL = "redis://localhost:6379"
DLQ_STREAM = "nbus:bus.dead_letter"

# Retry backoff schedule (seconds) — index = retry_count (0-based)
RETRY_BACKOFF = [30, 300, 1800]  # 30s, 5min, 30min

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
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_unresolved ON dead_letters(resolved_at, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_created_at ON dead_letters(created_at);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def insert_dead_letter(conn: sqlite3.Connection, entry: dict) -> None:
    """Insert a new dead-letter entry. Idempotent on id collision."""
    now = time.time()
    event_id = entry.get("id") or entry.get("event_id") or ""
    event_type = entry.get("original_type") or entry.get("event_type") or "unknown"
    source = entry.get("source") or ""
    failure_reason = entry.get("failure_reason") or "unknown"
    schema_detail = entry.get("schema_violation_detail") or ""
    original_payload = entry.get("original_payload_excerpt") or json.dumps(entry)

    # Schema violations don't retry; everything else gets first retry scheduled
    if failure_reason == "schema_violation" or event_id == "":
        next_retry_at = None
    else:
        next_retry_at = now + RETRY_BACKOFF[0]

    try:
        conn.execute(
            """INSERT OR IGNORE INTO dead_letters
               (id, event_type, source, failure_reason, schema_violation_detail,
                original_payload, retry_count, created_at, next_retry_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (event_id, event_type, source, failure_reason, schema_detail,
             original_payload, now, next_retry_at),
        )
        conn.commit()
    except Exception as e:
        sys.stderr.write(f"[dlq] insert failed: {e}\n")


def get_unresolved(conn: sqlite3.Connection, limit: int = 50) -> list:
    rows = conn.execute(
        """SELECT id, event_type, source, failure_reason, schema_violation_detail,
                  original_payload, retry_count, last_error, created_at,
                  next_retry_at, resolved_at
           FROM dead_letters
           WHERE resolved_at IS NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_pending_retries(conn: sqlite3.Connection) -> list:
    now = time.time()
    rows = conn.execute(
        """SELECT id, event_type, original_payload, retry_count, failure_reason
           FROM dead_letters
           WHERE resolved_at IS NULL
             AND failure_reason != 'schema_violation'
             AND next_retry_at IS NOT NULL
             AND next_retry_at <= ?
             AND retry_count < 3""",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_retry_success(conn: sqlite3.Connection, entry_id: str) -> None:
    conn.execute(
        "UPDATE dead_letters SET resolved_at=?, last_error=NULL WHERE id=?",
        (time.time(), entry_id),
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


def retry_worker(conn: sqlite3.Connection, stop_event: threading.Event) -> None:
    """Background thread: check for due retries every 10 s."""
    while not stop_event.is_set():
        try:
            pending = get_pending_retries(conn)
            for entry in pending:
                entry_id = entry["id"]
                retry_count = entry["retry_count"]
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


# ── Valkey subscriber ─────────────────────────────────────────────────────────

def subscribe_worker(
    conn: sqlite3.Connection,
    valkey_url: str,
    stop_event: threading.Event,
) -> None:
    """Tail nbus:bus.dead_letter with XREAD (no consumer group — just observe)."""
    last_id = "$"
    r: Optional[redis.Redis] = None

    while not stop_event.is_set():
        # Connect / reconnect
        if r is None:
            try:
                r = redis.Redis.from_url(
                    valkey_url,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=3,
                )
                r.ping()
                # On first connect, read all existing entries not yet seen
                last_id = "0"
                sys.stderr.write(f"[dlq] connected to Valkey, tailing {DLQ_STREAM}\n")
            except Exception as e:
                sys.stderr.write(f"[dlq] Valkey connect failed: {e}\n")
                r = None
                stop_event.wait(5.0)
                continue

        try:
            # Block up to 2 s waiting for new entries
            results = r.xread({DLQ_STREAM: last_id}, block=2000, count=100)
            if not results:
                continue
            for _stream, entries in results:
                for entry_id, fields in entries:
                    last_id = entry_id
                    raw = fields.get("_raw", "")
                    try:
                        envelope = json.loads(raw)
                        data = envelope.get("data") or {}
                    except Exception:
                        data = {k: v for k, v in fields.items()}

                    # Merge fields for a complete picture
                    # Support both "failure_reason" (schema) and legacy "reason" field
                    merged = {
                        "id": fields.get("event_id") or envelope.get("id") if raw else entry_id,
                        "source": fields.get("source") or "",
                        **data,
                    }
                    if "failure_reason" not in merged and "reason" in merged:
                        merged["failure_reason"] = merged["reason"]
                    if "original_type" not in merged and "channel" in merged:
                        merged["original_type"] = merged["channel"]
                    insert_dead_letter(conn, merged)
                    sys.stderr.write(
                        f"[dlq] persisted dead_letter: {merged.get('id',entry_id)} "
                        f"reason={merged.get('failure_reason','?')}\n"
                    )
        except redis.RedisError as e:
            sys.stderr.write(f"[dlq] Valkey read error: {e}\n")
            r = None
            stop_event.wait(3.0)
        except Exception as e:
            sys.stderr.write(f"[dlq] subscribe_worker error: {e}\n")
            stop_event.wait(1.0)


# ── HTTP server ───────────────────────────────────────────────────────────────

def _make_handler(conn: sqlite3.Connection):
    _conn_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
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
    parser.add_argument("--list", action="store_true",
                        help="Print unresolved entries as JSON and exit")
    args = parser.parse_args(argv)

    conn = open_db(args.db)

    if args.list:
        rows = get_unresolved(conn, limit=100)
        print(json.dumps({"count": len(rows), "entries": rows}, indent=2, default=str))
        return 0

    stop_event = threading.Event()

    sub_thread = threading.Thread(
        target=subscribe_worker,
        args=(conn, args.valkey_url, stop_event),
        daemon=True,
        name="dlq_subscriber",
    )
    sub_thread.start()

    retry_thread = threading.Thread(
        target=retry_worker,
        args=(conn, stop_event),
        daemon=True,
        name="dlq_retry",
    )
    retry_thread.start()

    handler = _make_handler(conn)
    httpd = HTTPServer(("0.0.0.0", args.port), handler)
    http_thread = threading.Thread(
        target=httpd.serve_forever,
        daemon=True,
        name="dlq_http",
    )
    http_thread.start()

    print(
        f"nbus DLQ daemon: db={args.db}  HTTP=:{args.port}/dlq  "
        f"stream={DLQ_STREAM}  valkey={args.valkey_url}"
    )

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_event.set()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
