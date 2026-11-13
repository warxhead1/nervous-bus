#!/usr/bin/env python3
"""backfill_dispatch_lineage.py -- coverage report and optional back-fill of the
child agent id on Agent/Task events captured since the metadata-only change
(issue #119).

Read-time resolution (detectors.dispatch_lineage.parse_dispatches) already
recovers the child id and prompt for these events, so this script is optional.
Default is a DRY RUN that opens the store read-only and prints, per month, how
many dispatches resolve by summary, by child envelope, by transcript, or not at
all. `--apply` writes `agentId` (an id, not content) into each unresolved-in-
summary event's tool_response_summary. Prompt text is never written back: it was
dropped from capture on purpose and stays in the transcripts.

    backfill_dispatch_lineage.py [--db PATH] [--since 2026-10-03] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from detectors import transcript_dispatch

DEFAULT_DB = Path("~/.cache/nervous-bus/reflex/runs.db").expanduser()


def _first_id_since(conn: sqlite3.Connection, since: str) -> int:
    """Smallest run_events.id whose event_ts >= since (ids grow with time, so a
    binary search avoids a full scan of the unindexed event_ts column)."""
    lo, hi = 0, conn.execute("SELECT COALESCE(MAX(id), 0) FROM run_events").fetchone()[0]
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute("SELECT event_ts FROM run_events WHERE id >= ? ORDER BY id LIMIT 1",
                           (mid,)).fetchone()
        if row is None or row[0] >= since:
            hi = mid
        else:
            lo = mid + 1
    return lo


def collect(conn: sqlite3.Connection, since: str):
    """(agent_rows, child_index): agent_rows = [(id, month, data)], child_index =
    parent tool_use_id -> subagent_id, over events at/after `since`."""
    start = _first_id_since(conn, since)
    agents, kids = [], {}
    cur = conn.execute("SELECT id, event_ts, raw_json FROM run_events WHERE id >= ?", (start,))
    for rid, ts, raw in cur:
        if '"tool_use_id"' not in raw:
            continue
        try:
            data = (json.loads(raw) or {}).get("data", {})
        except ValueError:
            continue
        if data.get("parent_tool_use_id") and data.get("subagent_id"):
            kids.setdefault(data["parent_tool_use_id"], data["subagent_id"])
        elif data.get("tool_name") in ("Agent", "Task"):
            agents.append((rid, ts[:7], data))
    return agents, kids


def classify(data: dict, kids: dict, use_transcripts: bool = True):
    """(child_id, source) with source in summary|envelope|transcript|unresolved."""
    summary = data.get("tool_response_summary", "") or ""
    if '"agentId"' in summary:
        return "", "summary"
    tuid = data.get("tool_use_id", "")
    if tuid in kids:
        return kids[tuid], "envelope"
    if use_transcripts:
        rec = transcript_dispatch.lookup(data.get("transcript_path", ""),
                                         data.get("session_id", ""), tuid)
        if rec.get("agentId"):
            return rec["agentId"], "transcript"
    return "", "unresolved"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--since", default="2026-10-03")
    ap.add_argument("--apply", action="store_true",
                    help="write agentId into tool_response_summary (default: dry run)")
    args = ap.parse_args(argv)

    uri = f"file:{args.db}?mode={'rw' if args.apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    agents, kids = collect(conn, args.since)
    per_month: dict[str, Counter] = defaultdict(Counter)
    updates = []
    for rid, month, data in agents:
        child, source = classify(data, kids)
        per_month[month][source] += 1
        if child:
            updates.append((rid, child))
    for month in sorted(per_month):
        c = per_month[month]
        print(f"{month}: total={sum(c.values())} summary={c['summary']} "
              f"envelope={c['envelope']} transcript={c['transcript']} "
              f"unresolved={c['unresolved']}")
    print(f"{len(updates)} events would gain agentId" + ("" if args.apply else " (dry run)"))
    if args.apply:
        for rid, child in updates:
            raw = conn.execute("SELECT raw_json FROM run_events WHERE id = ?", (rid,)).fetchone()[0]
            obj = json.loads(raw)
            data = obj.setdefault("data", {})
            try:
                resp = json.loads(data.get("tool_response_summary") or "{}")
            except ValueError:
                continue
            if not isinstance(resp, dict) or "agentId" in resp:
                continue
            resp["agentId"] = child
            data["tool_response_summary"] = json.dumps(resp, sort_keys=True, separators=(",", ":"))
            conn.execute("UPDATE run_events SET raw_json = ? WHERE id = ?",
                         (json.dumps(obj, separators=(",", ":")), rid))
        conn.commit()
        print("applied")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
