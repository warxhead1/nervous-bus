#!/usr/bin/env python3
"""live_check.py — run the transcript detectors on one session's runs now.

The nightly pass finds a lane's thrash hours after it could have been stopped.
This runs param_sweep_thrash, foreground_wait and gate_bound_loosened on the
runs of one session (or the given run ids) and prints each hit it has not
printed before. A SubagentStop hook can run it and hand the lines to the lead.

Read-only on runs.db: it calls detect(), never run(), so nothing is persisted
there. The only write is the seen-hits state file, which dedupes across calls.

    live_check.py (--session ID | --run-id R ...) [--since-hours 6] [--db D]
                  [--state F] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from detectors.foreground_wait import ForegroundWaitDetector
from detectors.gate_bound_loosened import GateBoundLoosenedDetector
from detectors.param_sweep_thrash import ParamSweepThrashDetector

LIVE_DETECTORS = (ParamSweepThrashDetector, ForegroundWaitDetector, GateBoundLoosenedDetector)
DEFAULT_DB = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"
DEFAULT_STATE = Path.home() / ".cache" / "nervous-bus" / "reflex" / "live-seen.json"
SEEN_CAP = 5000


def session_runs(conn: sqlite3.Connection, session_id: str, since_hours: float) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r[0] for r in conn.execute(
        "SELECT run_id FROM runs WHERE session_id = ? AND started >= ? ORDER BY started",
        (session_id, since))]


def live_hits(conn: sqlite3.Connection, run_ids: list[str], seen: set[str],
              detectors=LIVE_DETECTORS, roots: Optional[list[Path]] = None) -> list[dict]:
    """Hits on `run_ids` whose (run, detector, signature) key is not in `seen`.
    Adds the new keys to `seen`."""
    if not run_ids:
        return []
    out = []
    for cls in detectors:
        det = cls(conn)
        det.run_ids = list(run_ids)
        if roots is not None:
            det.transcript_roots = roots
        for cand in det.detect(conn):
            for rid in sorted(set(cand.run_ids) & set(run_ids)):
                key = f"{rid}|{cand.detector}|{cand.signature}"
                if key in seen:
                    continue
                seen.add(key)
                out.append({"run_id": rid, "project": cand.project, "detector": cand.detector,
                            "signature": cand.signature, "evidence": cand.evidence[:3],
                            "remediation": cand.proposed_remediation})
    return out


def _load_seen(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_seen(path: Path, keys: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys[-SEEN_CAP:]))
    tmp.replace(path)


def format_hits(hits: list[dict]) -> list[str]:
    """One line per detector signature, naming its newest run and run count."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for h in hits:
        groups.setdefault((h["detector"], h["signature"]), []).append(h)
    lines = []
    for (det, sig), hs in groups.items():
        more = f" and {len(hs) - 1} more run(s)" if len(hs) > 1 else ""
        line = f"[reflex] {det} on run {hs[-1]['run_id']}{more} ({hs[-1]['project']}): {sig}"
        if hs[-1].get("remediation"):
            line += f". Fix: {hs[-1]['remediation']}"
        lines.append(line)
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--session")
    who.add_argument("--run-id", action="append")
    ap.add_argument("--since-hours", type=float, default=6.0)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.db.exists():
        print(f"live_check: no DB at {a.db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    run_ids = a.run_id or session_runs(conn, a.session, a.since_hours)
    keys = _load_seen(a.state)
    seen = set(keys)
    hits = live_hits(conn, run_ids, seen)
    if hits:
        _save_seen(a.state, keys + [f"{h['run_id']}|{h['detector']}|{h['signature']}" for h in hits])
    for line in ([json.dumps(h) for h in hits] if a.json else format_hits(hits)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
