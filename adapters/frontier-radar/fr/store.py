"""sqlite store: sources state, events (one per primary artifact), sightings, snapshots, alarms."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .keys import canonical_url, event_key
from .parsers import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state(
  source_id TEXT PRIMARY KEY, url TEXT, etag TEXT DEFAULT '', last_modified TEXT DEFAULT '',
  first_poll_at TEXT, last_poll_at TEXT, last_status TEXT DEFAULT '', consecutive_fail INTEGER DEFAULT 0,
  newest_published_at TEXT, last_new_item_at TEXT, final_url TEXT DEFAULT '',
  page_hash TEXT DEFAULT '', page_lines TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS sitemap_state(source_id TEXT, url TEXT, lastmod TEXT, PRIMARY KEY(source_id, url));
CREATE TABLE IF NOT EXISTS events(
  event_key TEXT PRIMARY KEY, url TEXT, title TEXT, summary TEXT DEFAULT '', published_at TEXT,
  first_seen_at TEXT, domains TEXT DEFAULT '[]', primary_source TEXT, primary_role TEXT, signal INTEGER DEFAULT 1,
  score REAL DEFAULT 0, score_stage TEXT DEFAULT 'first', status TEXT DEFAULT 'new', lane TEXT,
  triage TEXT, triaged_at TEXT, digested_at TEXT);
CREATE TABLE IF NOT EXISTS sightings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, source_id TEXT, kind TEXT, role TEXT, url TEXT,
  ext_id TEXT DEFAULT '', first_seen_at TEXT, last_seen_at TEXT, engagement TEXT DEFAULT '{}',
  UNIQUE(event_key, source_id, url));
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, label TEXT, taken_at TEXT, engagement TEXT,
  UNIQUE(event_key, label));
CREATE TABLE IF NOT EXISTS alarms(
  source_id TEXT, kind TEXT, detail TEXT, raised_at TEXT, updated_at TEXT, resolved_at TEXT);
CREATE INDEX IF NOT EXISTS idx_sight_event ON sightings(event_key);
CREATE INDEX IF NOT EXISTS idx_events_lane ON events(lane, digested_at);
"""


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def default_state_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("FRONTIER_RADAR_STATE")
    if env:
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "frontier-radar" / "radar.db"


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path) if path != ":memory:" else path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.commit()
        self.db.close()

    # source state -------------------------------------------------------
    def source_state(self, sid: str) -> dict:
        r = self.db.execute("SELECT * FROM source_state WHERE source_id=?", (sid,)).fetchone()
        return dict(r) if r else {}

    def save_source_state(self, sid: str, **fields) -> None:
        if not self.source_state(sid):
            self.db.execute("INSERT INTO source_state(source_id) VALUES(?)", (sid,))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE source_state SET {cols} WHERE source_id=?", (*fields.values(), sid))
        self.db.commit()

    # alarms -------------------------------------------------------------
    def raise_alarm(self, sid: str, kind: str, detail: str, now: datetime) -> bool:
        """Open or refresh an alarm. Returns True when it was newly opened."""
        r = self.db.execute("SELECT rowid FROM alarms WHERE source_id=? AND kind=? AND resolved_at IS NULL",
                            (sid, kind)).fetchone()
        if r:
            self.db.execute("UPDATE alarms SET detail=?, updated_at=? WHERE rowid=?", (detail, iso(now), r[0]))
        else:
            self.db.execute("INSERT INTO alarms VALUES(?,?,?,?,?,NULL)", (sid, kind, detail, iso(now), iso(now)))
        self.db.commit()
        return r is None

    def clear_alarm(self, sid: str, kind: str, now: datetime) -> None:
        self.db.execute("UPDATE alarms SET resolved_at=? WHERE source_id=? AND kind=? AND resolved_at IS NULL",
                        (iso(now), sid, kind))
        self.db.commit()

    def open_alarms(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM alarms WHERE resolved_at IS NULL ORDER BY source_id, kind")]

    # sitemap ------------------------------------------------------------
    def sitemap_rows(self, sid: str) -> dict:
        return {r["url"]: r["lastmod"] for r in self.db.execute(
            "SELECT url, lastmod FROM sitemap_state WHERE source_id=?", (sid,))}

    def save_sitemap(self, sid: str, rows: dict) -> None:
        self.db.executemany("INSERT OR REPLACE INTO sitemap_state VALUES(?,?,?)",
                            [(sid, u, m) for u, m in rows.items()])
        self.db.commit()

    # events -------------------------------------------------------------
    def record_sighting(self, src, item: Item, now: datetime, key: str | None = None) -> tuple[str, bool]:
        """Store one sighting. Returns (event_key, is_new_event)."""
        key = key or event_key(item.url)
        role = "primary" if src.role == "primary" else ("bot-mirror" if item.bot else "mirror")
        if item.bot:
            role = "bot-mirror"
        seen_url = item.discussion_url or item.url
        row = self.db.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
        domains = list(src.domains)
        is_new = row is None
        if is_new:
            self.db.execute(
                "INSERT INTO events(event_key,url,title,summary,published_at,first_seen_at,domains,primary_source,"
                "primary_role,signal) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, canonical_url(item.url), item.title, item.summary, iso(item.published_at), iso(now),
                 json.dumps(domains), src.id, role, src.signal))
        else:
            merged = sorted(set(json.loads(row["domains"])) | set(domains))
            upgrade = row["primary_role"] != "primary" and role == "primary"
            self.db.execute("UPDATE events SET domains=?, signal=MAX(signal, ?) WHERE event_key=?",
                            (json.dumps(merged), src.signal, key))
            if upgrade:
                self.db.execute(
                    "UPDATE events SET url=?, title=?, summary=?, published_at=COALESCE(?, published_at), "
                    "primary_source=?, primary_role=? WHERE event_key=?",
                    (canonical_url(item.url), item.title, item.summary, iso(item.published_at), src.id, role, key))
        eng = json.dumps(item.engagement)
        existing = self.db.execute("SELECT id FROM sightings WHERE event_key=? AND source_id=? AND url=?",
                                   (key, src.id, seen_url)).fetchone()
        if existing:
            self.db.execute("UPDATE sightings SET last_seen_at=?, engagement=? WHERE id=?",
                            (iso(now), eng, existing[0]))
        else:
            self.db.execute(
                "INSERT INTO sightings(event_key,source_id,kind,role,url,ext_id,first_seen_at,last_seen_at,engagement)"
                " VALUES(?,?,?,?,?,?,?,?,?)", (key, src.id, src.kind, role, seen_url, item.ext_id, iso(now), iso(now), eng))
        if item.engagement:
            self.db.execute("INSERT OR IGNORE INTO snapshots(event_key,label,taken_at,engagement) VALUES(?,?,?,?)",
                            (key, "first", iso(now), eng))
        self.db.commit()
        return key, is_new

    def event(self, key: str) -> dict | None:
        r = self.db.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()
        return self._event_dict(r) if r else None

    @staticmethod
    def _event_dict(r) -> dict:
        d = dict(r)
        d["domains"] = json.loads(d["domains"] or "[]")
        d["triage"] = json.loads(d["triage"]) if d["triage"] else None
        return d

    def events(self, where: str = "1=1", args: tuple = (), order: str = "score DESC") -> list[dict]:
        return [self._event_dict(r) for r in self.db.execute(f"SELECT * FROM events WHERE {where} ORDER BY {order}", args)]

    def sightings(self, key: str) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM sightings WHERE event_key=? ORDER BY first_seen_at, id", (key,)):
            d = dict(r)
            d["engagement"] = json.loads(d["engagement"] or "{}")
            out.append(d)
        return out

    def snapshots(self, key: str) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT * FROM snapshots WHERE event_key=? ORDER BY taken_at, id", (key,)):
            d = dict(r)
            d["engagement"] = json.loads(d["engagement"] or "{}")
            out.append(d)
        return out

    def add_snapshot(self, key: str, label: str, now: datetime, engagement: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO snapshots(event_key,label,taken_at,engagement) VALUES(?,?,?,?)",
                        (key, label, iso(now), json.dumps(engagement)))
        self.db.commit()

    def update_event(self, key: str, **fields) -> None:
        for k, v in list(fields.items()):
            if isinstance(v, (dict, list)):
                fields[k] = json.dumps(v)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE events SET {cols} WHERE event_key=?", (*fields.values(), key))
        self.db.commit()
