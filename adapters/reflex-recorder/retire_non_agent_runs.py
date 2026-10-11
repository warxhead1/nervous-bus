#!/usr/bin/env python3
"""Move non-LLM runs (agent_kind in NON_AGENT_KINDS) out of runs.db.

Rows are copied into a sidecar archive DB (default runs_non_agent.db next to
runs.db) and deleted from runs.db in small transactions, so the live recorder
is never locked out for long. Idempotent; reversible by copying back.

    retire_non_agent_runs.py [--db PATH] [--archive PATH] [--batch N] [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent_kinds import NON_AGENT_KINDS  # noqa: E402
from store import DEFAULT_DB_PATH  # noqa: E402

_MOVED = ("runs", "run_events", "detector_hits")


def _ensure_archive(conn: sqlite3.Connection) -> None:
    for table in _MOVED + ("pending_events",):
        conn.execute(f"CREATE TABLE IF NOT EXISTS arch.{table} AS SELECT * FROM main.{table} WHERE 0")


def retire(db: Path, archive: Path, batch: int = 2000, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(str(db), isolation_level=None, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    kinds = sorted(NON_AGENT_KINDS)
    marks = ",".join("?" * len(kinds))
    if dry_run:
        n = conn.execute(f"SELECT COUNT(*) FROM runs WHERE agent_kind IN ({marks})", kinds).fetchone()[0]
        return {"runs": n, "dry_run": True}
    conn.execute("ATTACH DATABASE ? AS arch", (str(archive),))
    _ensure_archive(conn)
    moved = {"runs": 0, "run_events": 0, "detector_hits": 0, "pending_events": 0}
    while True:
        ids = [r[0] for r in conn.execute(
            f"SELECT run_id FROM runs WHERE agent_kind IN ({marks}) LIMIT ?", (*kinds, batch))]
        if not ids:
            break
        idm = ",".join("?" * len(ids))
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in _MOVED:
                conn.execute(f"INSERT INTO arch.{table} SELECT * FROM main.{table} WHERE run_id IN ({idm})", ids)
                moved[table] += conn.execute(f"DELETE FROM main.{table} WHERE run_id IN ({idm})", ids).rowcount
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        time.sleep(0.05)  # let the recorder's writer in between batches
    pred = f"json_extract(raw_json, '$.data.agent_kind') IN ({marks})"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"INSERT INTO arch.pending_events SELECT * FROM main.pending_events WHERE {pred}", kinds)
    moved["pending_events"] = conn.execute(f"DELETE FROM main.pending_events WHERE {pred}", kinds).rowcount
    conn.execute("COMMIT")
    conn.close()
    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--archive", type=Path)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    archive = a.archive or a.db.with_name("runs_non_agent.db")
    print(retire(a.db, archive, a.batch, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
