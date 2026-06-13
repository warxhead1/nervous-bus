"""store.py — SQLite persistence backend for reflex-recorder.

Abstraction layer so a dolt backend can swap in later (the Reflexarc eventual
target) without touching recorder.py.

Tables:
  runs        — one row per closed bus.agent.run.closed.v1 payload
  run_events  — ordered raw activity events per run_id (for b3 feature/label backfill)

Store path: ~/.cache/nervous-bus/reflex/runs.db (configurable via REFLEX_DB_PATH).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    run_key             TEXT NOT NULL,
    run_key_kind        TEXT NOT NULL,
    host_conversation_id TEXT,
    project             TEXT NOT NULL,
    agent_kind          TEXT NOT NULL,
    session_id          TEXT,
    agent_id            TEXT,
    started             TEXT NOT NULL,
    ended               TEXT NOT NULL,
    close_reason        TEXT,
    continues_run_id    TEXT,
    event_count         INTEGER NOT NULL DEFAULT 0,
    tool_histogram      TEXT NOT NULL DEFAULT '{}',
    worktree            TEXT,
    worktree_slug       TEXT,
    git_branch          TEXT,
    bead_id             TEXT,
    outcome             TEXT,
    labeled_at          TEXT,
    label_version       INTEGER,
    label_history       TEXT NOT NULL DEFAULT '[]',
    features            TEXT NOT NULL DEFAULT '{}',
    schema_version      TEXT NOT NULL DEFAULT '1',
    recorded_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_run_key ON runs(run_key);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project);
CREATE INDEX IF NOT EXISTS idx_runs_close_reason ON runs(close_reason);

CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_run_events_seq ON run_events(run_id, seq);
"""


class SQLiteStore:
    """SQLite-backed run store.

    Thread-safety: single-threaded (recorder is single-threaded). SQLite
    check_same_thread=False is set in case the caller wraps this in a thread
    later, but the primary design assumes single-threaded access.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        # Per-run event sequence counters (in-memory; reset on restart is fine)
        self._event_seq: dict[str, int] = {}

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)

    def save_run(self, payload: dict) -> None:
        """Persist a closed run payload to the runs table."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._conn.execute(
            """
            INSERT OR REPLACE INTO runs (
                run_id, run_key, run_key_kind, host_conversation_id,
                project, agent_kind, session_id, agent_id,
                started, ended, close_reason, continues_run_id,
                event_count, tool_histogram,
                worktree, worktree_slug, git_branch, bead_id,
                outcome, labeled_at, label_version, label_history,
                features, schema_version, recorded_at
            ) VALUES (
                :run_id, :run_key, :run_key_kind, :host_conversation_id,
                :project, :agent_kind, :session_id, :agent_id,
                :started, :ended, :close_reason, :continues_run_id,
                :event_count, :tool_histogram,
                :worktree, :worktree_slug, :git_branch, :bead_id,
                :outcome, :labeled_at, :label_version, :label_history,
                :features, :schema_version, :recorded_at
            )
            """,
            {
                "run_id": payload["run_id"],
                "run_key": payload["run_key"],
                "run_key_kind": payload["run_key_kind"],
                "host_conversation_id": payload.get("host_conversation_id"),
                "project": payload["project"],
                "agent_kind": payload["agent_kind"],
                "session_id": payload.get("session_id"),
                "agent_id": payload.get("agent_id"),
                "started": payload["started"],
                "ended": payload["ended"],
                "close_reason": payload.get("close_reason"),
                "continues_run_id": payload.get("continues_run_id"),
                "event_count": payload["event_count"],
                "tool_histogram": json.dumps(payload.get("tool_histogram", {})),
                "worktree": payload.get("worktree"),
                "worktree_slug": payload.get("worktree_slug"),
                "git_branch": payload.get("git_branch"),
                "bead_id": payload.get("bead_id"),
                "outcome": payload.get("outcome"),
                "labeled_at": payload.get("labeled_at"),
                "label_version": payload.get("label_version"),
                "label_history": json.dumps(payload.get("label_history", [])),
                "features": json.dumps(payload.get("features", {})),
                "schema_version": payload.get("schema_version", "1"),
                "recorded_at": now,
            },
        )

    def append_event(self, run_id: str, event_ts: str, event_type: str, raw: str) -> None:
        """Append a raw activity event to run_events for later backfill."""
        seq = self._event_seq.get(run_id, 0) + 1
        self._event_seq[run_id] = seq
        self._conn.execute(
            """
            INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, seq, event_ts, event_type, raw),
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Diagnostic helpers ────────────────────────────────────────────────────

    def recent_runs(self, limit: int = 10) -> list[dict]:
        """Return recently closed runs as dicts (for smoke tests / inspection)."""
        cur = self._conn.execute(
            """
            SELECT run_id, run_key, run_key_kind, project, agent_kind,
                   started, ended, close_reason, event_count, tool_histogram,
                   worktree, worktree_slug, continues_run_id
            FROM runs
            ORDER BY ended DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["tool_histogram"] = json.loads(d["tool_histogram"])
            rows.append(d)
        return rows

    def run_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def event_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM run_events").fetchone()[0]
