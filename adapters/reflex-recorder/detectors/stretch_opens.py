"""detectors/stretch_opens.py — which stretchtext stretches the reader opens.

The cc-stretch plugin adds one reminder to the next prompt after each reply.
Claude Code stores it in the transcript as a line with ``type == "attachment"``
and ``attachment.type == "hook_additional_context"`` whose ``content`` is a list
of strings. Two reminder shapes exist (measured 2026-11-26):

    Stretchtext: no click on a stretch of your last reply was recorded. ...
    Stretchtext: since your last reply the reader opened "A", "B". They did not
        open "C", "D".

The quoted strings are the short forms of the stretches in the reply. The
"They did not open" sentence is absent when every stretch was opened.

This detector counts one turn per reminder, deduplicated on the hook's
``toolUseID`` (a session resume or a subagent transcript repeats the line).
It writes AGGREGATES to runs.db and returns no PatternCandidates, because a
trend, not a per-run pathology, is the output:

  stretch_open_daily  (day, project): turns with and without an open, counts of
                      forms opened and not opened.
  stretch_open_forms  (day, project, kind, form_hash, length): how often each
                      short form was opened or passed over. The form is stored
                      as an 8-hex hash and a length, never as text, so a repeat
                      is countable and the reply text stays out of the DB.

Rows for every day found in the scan are replaced, so a rescan is idempotent.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from detectors.base import BaseDetector, PatternCandidate

DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

MARKER = "Stretchtext: "
NO_CLICK_PREFIX = "Stretchtext: no click on a stretch of your last reply was recorded"
OPENED_PREFIX = "Stretchtext: since your last reply the reader opened "
NOT_OPENED_SPLIT = ". They did not open "

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stretch_open_daily (
    day              TEXT NOT NULL,
    project          TEXT NOT NULL,
    turns_no_click   INTEGER NOT NULL,
    turns_open       INTEGER NOT NULL,
    forms_opened     INTEGER NOT NULL,
    forms_not_opened INTEGER NOT NULL,
    PRIMARY KEY (day, project)
);
CREATE TABLE IF NOT EXISTS stretch_open_forms (
    day       TEXT NOT NULL,
    project   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    form_hash TEXT NOT NULL,
    length    INTEGER NOT NULL,
    n         INTEGER NOT NULL,
    PRIMARY KEY (day, project, kind, form_hash)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def project_of(cwd: str) -> str:
    """Project name from a working directory: worktree and checkout layouts both
    resolve to the repo name; anything else falls back to its basename."""
    parts = [p for p in (cwd or "").split("/") if p]
    for i, p in enumerate(parts):
        if p == "worktrees" and i + 1 < len(parts):
            return parts[i + 1]
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else "unknown"


def _split_forms(segment: str) -> list[str]:
    """`"A", "B".` -> ['A', 'B']. A form that itself contains `", "` over-splits."""
    seg = segment.strip()
    if seg.endswith("."):
        seg = seg[:-1]
    if len(seg) < 2 or seg[0] != '"' or seg[-1] != '"':
        return []
    return seg[1:-1].split('", "')


def parse_reminder(text: str) -> Optional[tuple[list[str], list[str]]]:
    """(opened_forms, not_opened_forms) for a stretchtext reminder, else None.
    A no-click reminder returns ([], [])."""
    if text.startswith(NO_CLICK_PREFIX):
        return [], []
    if not text.startswith(OPENED_PREFIX):
        return None
    body = text[len(OPENED_PREFIX):]
    # The sentence ends at the first line break, if the hook adds more text.
    body = body.split("\n", 1)[0]
    opened_seg, _, rest = body.partition(NOT_OPENED_SPLIT)
    return _split_forms(opened_seg), _split_forms(rest) if rest else []


def iter_reminders(path: Path) -> Iterator[tuple[str, str, str, str]]:
    """Yield (dedup_key, iso_ts, cwd, reminder_text) for each reminder in a transcript.

    Transcripts run to hundreds of MB, nearly all tool output, so the file is
    memory-mapped and only lines holding the marker are decoded."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size == 0:
                return
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    except (OSError, ValueError):
        return
    marker = MARKER.encode()
    with mm:
        pos = mm.find(marker)
        while pos != -1:
            start = mm.rfind(b"\n", 0, pos) + 1
            end = mm.find(b"\n", pos)
            if end == -1:
                end = len(mm)
            raw = mm[start:end]
            pos = mm.find(marker, end)
            if b'"attachment"' not in raw:
                continue
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            att = d.get("attachment")
            if not isinstance(att, dict) or att.get("type") != "hook_additional_context":
                continue
            content = att.get("content")
            for i, s in enumerate(content if isinstance(content, list) else [content]):
                if isinstance(s, str) and s.startswith(MARKER):
                    key = att.get("toolUseID") or d.get("uuid") or f"{path}:{d.get('timestamp')}"
                    yield f"{key}#{i}", d.get("timestamp") or "", d.get("cwd") or "", s


def scan(root: Path, since_day: Optional[str] = None) -> dict:
    """Aggregate every reminder under `root` (recursive *.jsonl).

    With `since_day` (YYYY-MM-DD), files last modified before that day are
    skipped (they cannot hold a later line) and earlier reminders are dropped,
    so the result covers whole days only and `store` can replace them."""
    since_epoch = None
    if since_day:
        since_epoch = datetime.strptime(since_day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    daily: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    forms: dict[tuple[str, str, str, str], list[int]] = {}
    seen: set[str] = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = Path(dirpath) / name
            if since_epoch is not None:
                try:
                    if path.stat().st_mtime < since_epoch:
                        continue
                except OSError:
                    continue
            for key, ts, cwd, text in iter_reminders(path):
                if key in seen:
                    continue
                seen.add(key)
                parsed = parse_reminder(text)
                if parsed is None or len(ts) < 10 or (since_day and ts[:10] < since_day):
                    continue
                day, project = ts[:10], project_of(cwd)
                opened, not_opened = parsed
                row = daily[(day, project)]
                row[1 if opened else 0] += 1
                row[2] += len(opened)
                row[3] += len(not_opened)
                for kind, items in (("opened", opened), ("not_opened", not_opened)):
                    for form in items:
                        h = hashlib.sha1(form.encode("utf-8")).hexdigest()[:8]
                        entry = forms.setdefault((day, project, kind, h), [len(form), 0])
                        entry[1] += 1
    return {"daily": daily, "forms": forms, "reminders": len(seen)}


def store(conn: sqlite3.Connection, agg: dict) -> None:
    ensure_schema(conn)
    days = sorted({d for d, _ in agg["daily"]})
    conn.execute("SAVEPOINT agg")
    try:
        for day in days:
            conn.execute("DELETE FROM stretch_open_daily WHERE day = ?", (day,))
            conn.execute("DELETE FROM stretch_open_forms WHERE day = ?", (day,))
        conn.executemany(
            "INSERT INTO stretch_open_daily VALUES (?,?,?,?,?,?)",
            [(d, p, *v) for (d, p), v in agg["daily"].items()],
        )
        conn.executemany(
            "INSERT INTO stretch_open_forms VALUES (?,?,?,?,?,?)",
            [(d, p, k, h, v[0], v[1]) for (d, p, k, h), v in agg["forms"].items()],
        )
        conn.execute("RELEASE agg")
    except Exception:
        conn.execute("ROLLBACK TO agg")
        conn.execute("RELEASE agg")
        raise


class StretchOpensDetector(BaseDetector):
    """Aggregates stretchtext open/no-click reminders into runs.db."""

    DETECTOR_NAME = "stretch_opens"

    transcript_root: Path = DEFAULT_TRANSCRIPT_ROOT

    def detect(self, conn: sqlite3.Connection, since_ts: Optional[str] = None) -> list[PatternCandidate]:
        since_day = since_ts[:10] if since_ts and re.match(r"\d{4}-\d\d-\d\d", since_ts) else None
        if self.transcript_root.is_dir():
            store(conn, scan(self.transcript_root, since_day))
        return []
