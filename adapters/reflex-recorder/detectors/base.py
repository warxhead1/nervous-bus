"""detectors/base.py — BaseDetector interface + Kyoko prevalence/recurrence layer.

Every Tier-1 detector subclasses BaseDetector and implements detect().

Interface contract
==================
    class MyDetector(BaseDetector):
        DETECTOR_NAME = "my_detector"   # stable; used as detector column in DB

        def detect(self, store: SQLiteStore) -> list[PatternCandidate]:
            # Query runs, return zero or more candidates.
            ...

The base provides:
  - record_hit(run_id, signature, project)  — write to detector_hits
  - prevalence(project, window_days)        — hits/total over rolling window
  - find_or_create_issue(signature, project, evidence) — dedup into issues table
  - emit_candidate(candidate)               — build the pattern.discovered.v1 dict

Kyoko layer (KYOKO-BORROW #2 + #5)
====================================
#2 Prevalence: detector_hits table tracks each firing; prevalence() divides
   hits-in-window by total-runs-in-window, turning boolean alarms into RATES.

#5 Recurrence: issues table deduplicates by stable signature = (project, detector,
   anchor).  recurrence_count increments on each new hit.
   recurrence_count_at_apply is snapshotted when a fix is applied, so post-fix
   hits become regression evidence rather than noise.

New SQLite tables (added to store.py by this module's ensure_schema())
=======================================================================
  detector_hits  (run_id, detector, signature, project, ts)
  issues         (signature PK, project, detector, first_seen, last_seen,
                  recurrence_count, recurrence_count_at_apply, evidence_json)

Store tables are added via ensure_detector_schema(conn) called from
SQLiteStore.__init__ when this module is present (or called explicitly in tests).
"""
from __future__ import annotations

import json
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# We import SQLiteStore lazily to avoid a hard circular dep; the type hint below
# uses a string forward reference.


# ── Schema additions ──────────────────────────────────────────────────────────

_DETECTOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS detector_hits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    detector    TEXT NOT NULL,
    signature   TEXT NOT NULL,
    project     TEXT NOT NULL,
    ts          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dh_signature  ON detector_hits(signature);
CREATE INDEX IF NOT EXISTS idx_dh_project_det ON detector_hits(project, detector, ts);
CREATE INDEX IF NOT EXISTS idx_dh_run_id     ON detector_hits(run_id);

CREATE TABLE IF NOT EXISTS issues (
    signature                  TEXT PRIMARY KEY,
    project                    TEXT NOT NULL,
    detector                   TEXT NOT NULL,
    first_seen                 TEXT NOT NULL,
    last_seen                  TEXT NOT NULL,
    recurrence_count           INTEGER NOT NULL DEFAULT 1,
    recurrence_count_at_apply  INTEGER,
    evidence_json              TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_issues_project_det ON issues(project, detector);
"""


def ensure_detector_schema(conn: sqlite3.Connection) -> None:
    """Add detector_hits and issues tables to an existing SQLite connection.

    Idempotent — uses CREATE TABLE IF NOT EXISTS throughout.
    Called from SQLiteStore._init_schema (patched in store.py) and from
    test fixtures that open a fresh in-memory DB.
    """
    conn.executescript(_DETECTOR_SCHEMA_SQL)


# ── PatternCandidate ──────────────────────────────────────────────────────────

@dataclass
class PatternCandidate:
    """Structured output from a detector.

    Maps directly onto <project>.pattern.discovered.v1 envelope fields plus
    the internal Kyoko bookkeeping fields.

    Fields
    ------
    project       : project the pattern was observed in (e.g. "nervous-bus")
    pattern_name  : human-readable name (e.g. "worktree_leak")
    signature     : stable cross-run identifier — (project, detector, anchor)
                    used as the issues.signature PK.  Must NOT include run_id.
    detector      : DETECTOR_NAME of the emitting class
    occurrences   : number of matching runs/items found in this scan
    evidence      : list of strings (run IDs, paths, git refs) for the report
    run_ids       : run_ids that fired this candidate (for hit recording)
    proposed_remediation : optional free-text Automate-rung proposal
    extra         : arbitrary extra fields included in the emitted event data
    """
    project: str
    pattern_name: str
    signature: str
    detector: str
    occurrences: int
    evidence: list[str]
    run_ids: list[str] = field(default_factory=list)
    proposed_remediation: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── BaseDetector ──────────────────────────────────────────────────────────────

class BaseDetector(ABC):
    """Base class for all Reflexarc Tier-1 detectors.

    Subclasses must set DETECTOR_NAME (str) and implement detect().

    Usage
    -----
        detector = MyDetector(conn)
        candidates = detector.run(store)
        for c in candidates:
            payload = detector.emit_candidate(c)
            # publish payload via nervous publish ...

    The run() method calls detect(), then for each candidate:
      - calls record_hit() for every run_id in candidate.run_ids
      - calls find_or_create_issue() to dedup + increment recurrence
    """

    DETECTOR_NAME: str = ""

    def __init__(self, conn: sqlite3.Connection):
        if not self.DETECTOR_NAME:
            raise ValueError(f"{self.__class__.__name__} must set DETECTOR_NAME")
        self._conn = conn
        ensure_detector_schema(conn)

    @abstractmethod
    def detect(self, conn: sqlite3.Connection) -> list[PatternCandidate]:
        """Query the run-store and return zero or more pattern candidates.

        Parameters
        ----------
        conn : sqlite3.Connection
            Live connection to runs.db (same connection the detector was
            constructed with).  Do NOT close it.

        Returns
        -------
        list[PatternCandidate]
            Empty list = no patterns found this scan.
        """

    # ── Public orchestration ──────────────────────────────────────────────────

    def run(self, conn: Optional[sqlite3.Connection] = None) -> list[PatternCandidate]:
        """Run detection + Kyoko bookkeeping.

        Returns the candidates list (same as detect() returns).
        """
        c = conn or self._conn
        candidates = self.detect(c)
        now = _now_utc()
        for candidate in candidates:
            for run_id in candidate.run_ids:
                self.record_hit(run_id, candidate.signature, candidate.project, ts=now)
            self.find_or_create_issue(
                signature=candidate.signature,
                project=candidate.project,
                evidence=candidate.evidence,
                ts=now,
            )
        return candidates

    # ── Kyoko layer: hit recording ────────────────────────────────────────────

    def record_hit(
        self,
        run_id: str,
        signature: str,
        project: str,
        ts: Optional[str] = None,
    ) -> None:
        """Write one entry to detector_hits.

        Idempotent per (run_id, detector, signature) — duplicate entries are
        allowed (XACK replay safety) but prevalence() counts distinct run days,
        not raw rows, so duplicates do not inflate the rate.
        """
        self._conn.execute(
            """
            INSERT INTO detector_hits (run_id, detector, signature, project, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, self.DETECTOR_NAME, signature, project, ts or _now_utc()),
        )

    # ── Kyoko layer: prevalence ───────────────────────────────────────────────

    def prevalence(
        self,
        project: str,
        window_days: int = 7,
    ) -> float:
        """Return the hit rate for this detector over the last *window_days*.

        hit_rate = distinct_run_ids_with_a_hit / total_runs_in_window
        Returns 0.0 if there are no runs in the window.

        This converts boolean alarms ("worktree leaked") into rates
        ("worktree leaked 40% of runs this week vs 8% last").
        """
        cutoff = _days_ago_utc(window_days)

        hit_count = self._conn.execute(
            """
            SELECT COUNT(DISTINCT run_id)
            FROM detector_hits
            WHERE detector = ?
              AND project  = ?
              AND ts       >= ?
            """,
            (self.DETECTOR_NAME, project, cutoff),
        ).fetchone()[0]

        total_count = self._conn.execute(
            """
            SELECT COUNT(*)
            FROM runs
            WHERE project = ?
              AND ended   >= ?
            """,
            (project, cutoff),
        ).fetchone()[0]

        if total_count == 0:
            return 0.0
        return hit_count / total_count

    # ── Kyoko layer: issue dedup + recurrence ─────────────────────────────────

    def find_or_create_issue(
        self,
        signature: str,
        project: str,
        evidence: list[str],
        ts: Optional[str] = None,
    ) -> dict:
        """Upsert an issue row by stable signature.

        On first occurrence  → INSERT with recurrence_count=1.
        On repeat occurrence → UPDATE last_seen + recurrence_count++.
        recurrence_count_at_apply is never modified here; it is set by the
        remediation layer (b7+) when a fix is applied, snapshotting the count
        so subsequent hits become regression evidence.

        Returns the current issue row as a dict.
        """
        now = ts or _now_utc()
        evidence_json = json.dumps(evidence)

        existing = self._conn.execute(
            "SELECT recurrence_count FROM issues WHERE signature = ?",
            (signature,),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """
                INSERT INTO issues
                    (signature, project, detector, first_seen, last_seen,
                     recurrence_count, recurrence_count_at_apply, evidence_json)
                VALUES (?, ?, ?, ?, ?, 1, NULL, ?)
                """,
                (signature, project, self.DETECTOR_NAME, now, now, evidence_json),
            )
        else:
            new_count = existing[0] + 1
            self._conn.execute(
                """
                UPDATE issues
                SET last_seen         = ?,
                    recurrence_count  = ?,
                    evidence_json     = ?
                WHERE signature = ?
                """,
                (now, new_count, evidence_json, signature),
            )

        row = self._conn.execute(
            "SELECT * FROM issues WHERE signature = ?",
            (signature,),
        ).fetchone()
        cols = [d[0] for d in self._conn.execute("SELECT * FROM issues WHERE 0").description]
        return dict(zip(cols, row))

    def get_issue(self, signature: str) -> Optional[dict]:
        """Fetch an issue row by signature, or None if not found."""
        cur = self._conn.execute(
            "SELECT * FROM issues WHERE signature = ?",
            (signature,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    # ── Emission helper ───────────────────────────────────────────────────────

    def emit_candidate(self, candidate: PatternCandidate) -> dict:
        """Build a <project>.pattern.discovered.v1 event data dict.

        The caller is responsible for publishing via `nervous publish`.
        Channel = f"{candidate.project}.pattern.discovered.v1"
        """
        data: dict[str, Any] = {
            "project": candidate.project,
            "pattern_name": candidate.pattern_name,
            "occurrences": candidate.occurrences,
            "evidence": candidate.evidence,
            "detector": candidate.detector,
            "signature": candidate.signature,
        }
        if candidate.proposed_remediation:
            data["proposed_remediation"] = candidate.proposed_remediation
        data.update(candidate.extra)
        return data


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_utc(days: int) -> str:
    """Return an RFC3339 UTC string for *days* ago from now."""
    import datetime as dt
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
