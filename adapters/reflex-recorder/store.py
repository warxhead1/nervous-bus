"""store.py — SQLite persistence backend for reflex-recorder.

Abstraction layer so a dolt backend can swap in later (the Reflexarc eventual
target) without touching recorder.py.

Tables:
  runs           — one row per closed bus.agent.run.closed.v1 payload
  run_events     — ordered raw activity events per run_id (for b3 feature/label backfill)
  detector_hits  — one row per detector firing per run (Kyoko #2 prevalence)
  issues         — deduped cross-run issue registry (Kyoko #5 recurrence)

The detector_hits and issues tables are created by
detectors/base.py::ensure_detector_schema(), called lazily at first
SQLiteStore._init_schema() — so the tables appear the first time either the
recorder or a detector opens the DB.

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

-- Write-ahead journal: the live XREADGROUP loop journals every accepted
-- activity event here, in one transaction per batch, before XACKing the
-- batch, so an event is durable independent of whether the run it belongs
-- to has closed yet. `id` is the true ordering key (autoincrement) —
-- `stream_id` alone cannot be a primary key because --replay mode reuses the
-- literal string "replay" for every row.
CREATE TABLE IF NOT EXISTS pending_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key     TEXT NOT NULL,
    stream_id   TEXT NOT NULL,
    event_ts    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    raw_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_events_run_key ON pending_events(run_key, id);
"""

_RUN_UPSERT_SQL = """
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
        # Lazily add detector_hits + issues tables (defined in detectors/base.py).
        # We import here to avoid a circular import at module load time.
        try:
            import sys
            from pathlib import Path as _Path
            _det_dir = _Path(__file__).parent / "detectors"
            if str(_det_dir.parent) not in sys.path:
                sys.path.insert(0, str(_det_dir.parent))
            from detectors.base import ensure_detector_schema
            ensure_detector_schema(self._conn)
        except ImportError:
            # detectors/ not yet present (e.g. unit tests for store only)
            pass

    def _run_params(self, payload: dict) -> dict:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
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
        }

    def save_run(self, payload: dict) -> None:
        """Persist a closed run payload to the runs table (autocommit)."""
        self._conn.execute(_RUN_UPSERT_SQL, self._run_params(payload))

    def append_event(self, run_id: str, event_ts: str, event_type: str, raw: str) -> None:
        """Append a raw activity event to run_events for later backfill.

        Single-event autocommit write. The live recorder path uses
        `journal_events` + `close_run` instead, which batch the same writes
        into one transaction each.
        """
        seq = self._event_seq.get(run_id, 0) + 1
        self._event_seq[run_id] = seq
        self._conn.execute(
            """
            INSERT INTO run_events (run_id, seq, event_ts, event_type, raw_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, seq, event_ts, event_type, raw),
        )

    def journal_events(self, entries: list[tuple[str, str, str, str, str]]) -> list[int]:
        """Write-ahead journal a batch of accepted activity events.

        `entries`: (run_key, stream_id, event_ts, event_type, raw_json), one
        row per event. All rows commit in ONE transaction — the caller must
        not XACK any stream id in the batch until this returns.

        Returns the assigned `pending_events.id` for each entry, in the same
        order as `entries` — the caller needs these to bound `close_run`'s
        drain to "everything folded so far for this run_key", since a single
        batch can contain more than one run under the same run_key (an
        `ended` event followed by a reopen).
        """
        if not entries:
            return []
        ids: list[int] = []
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.cursor()
            for run_key, stream_id, event_ts, event_type, raw_json in entries:
                cur.execute(
                    "INSERT INTO pending_events "
                    "(run_key, stream_id, event_ts, event_type, raw_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_key, stream_id, event_ts, event_type, raw_json),
                )
                ids.append(cur.lastrowid)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return ids

    def recover_pending_events(self) -> list[tuple[int, str, str, str, str]]:
        """All journaled events not yet closed into run_events, oldest first.

        Returns (id, run_key, event_ts, event_type, raw_json). Read once at
        startup to re-fold pre-crash events back into the live Segmenter; the
        rows themselves are left in place here and are only ever cleared by
        `close_run()`, whichever process eventually closes that run_key.
        """
        cur = self._conn.execute(
            "SELECT id, run_key, event_ts, event_type, raw_json "
            "FROM pending_events ORDER BY id"
        )
        return cur.fetchall()

    def pending_event_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM pending_events").fetchone()[0]

    def journaled_stream_ids(self, stream_ids: list[str]) -> set[str]:
        """Which of `stream_ids` already have a `pending_events` row.

        Used by PEL reclaim (recorder.py's `_reclaim_stranded_pel`) to avoid
        re-journaling a reclaimed entry that some earlier delivery already
        wrote to the write-ahead journal before crashing strictly between
        that commit and its XACK -- the row is already durable, so reclaim
        only needs to XACK it, not re-insert it. `run_events` (the table an
        acked, closed event ends up in) carries no `stream_id` column at
        all, so this table is the only place that distinction can be made.
        """
        if not stream_ids:
            return set()
        placeholders = ",".join("?" for _ in stream_ids)
        cur = self._conn.execute(
            f"SELECT DISTINCT stream_id FROM pending_events "
            f"WHERE stream_id IN ({placeholders})",
            list(stream_ids),
        )
        return {row[0] for row in cur.fetchall()}

    def close_run(self, payload: dict, upto_id: Optional[int] = None) -> None:
        """Persist a closed run and drain its journaled events, atomically.

        Single explicit transaction: the `runs` upsert, then every
        `pending_events` row for this run_key (bounded by `upto_id` when
        given) moved into `run_events` (seq preserved in journal arrival
        order) and deleted from the journal.

        `upto_id` must be the highest `pending_events.id` actually folded
        into this closed run. Without that bound, a run_key that reopens
        later in the SAME journaled batch (an `ended` event followed by more
        events for a new run under the same key) would have its
        not-yet-folded rows swept up by this close too. `None` means "no
        bound" (drain everything currently journaled for this run_key) —
        only correct when the caller knows no reopen can be pending, e.g. a
        one-shot test seeding a single run's events.
        """
        run_id = payload["run_id"]
        run_key = payload["run_key"]
        params = self._run_params(payload)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(_RUN_UPSERT_SQL, params)
            if upto_id is None:
                rows = self._conn.execute(
                    "SELECT id, event_ts, event_type, raw_json FROM pending_events "
                    "WHERE run_key = ? ORDER BY id",
                    (run_key,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, event_ts, event_type, raw_json FROM pending_events "
                    "WHERE run_key = ? AND id <= ? ORDER BY id",
                    (run_key, upto_id),
                ).fetchall()
            if rows:
                seq = self._event_seq.get(run_id, 0)
                to_insert = []
                ids = []
                for pending_id, event_ts, event_type, raw_json in rows:
                    seq += 1
                    to_insert.append((run_id, seq, event_ts, event_type, raw_json))
                    ids.append((pending_id,))
                self._conn.executemany(
                    "INSERT INTO run_events "
                    "(run_id, seq, event_ts, event_type, raw_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    to_insert,
                )
                self._conn.executemany(
                    "DELETE FROM pending_events WHERE id = ?", ids,
                )
                self._event_seq[run_id] = seq
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

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

    def latest_run_id_per_key(self) -> dict[str, str]:
        """Return {run_key: run_id} for the most recently closed run per run_key.

        B1 fix: used at startup to rebuild the segmenter's _last_closed_id map
        so continues_run_id linkage survives recorder restarts.

        Excludes recorder_shutdown-closed runs from consideration — those are
        operational closes, not semantic pauses; stitching a continuation through
        a shutdown would incorrectly inherit a recorder_shutdown run as predecessor.
        Idle_timeout and ended closes are valid predecessors.
        """
        cur = self._conn.execute(
            """
            SELECT run_key, run_id
            FROM runs
            WHERE close_reason != 'recorder_shutdown'
               OR close_reason IS NULL
            ORDER BY ended DESC
            """,
        )
        # Keep only the first (most recent) run_id per run_key
        result: dict[str, str] = {}
        for run_key, run_id in cur.fetchall():
            if run_key and run_key not in result:
                result[run_key] = run_id
        return result
