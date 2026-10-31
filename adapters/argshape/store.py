"""SQLite state for argshape. Counts and learned facts, never raw output.

Every row is keyed so it stays bounded: by (binary build, context, token),
(connection, relation, column), or a short-lived session pending entry. The
whole file is expected to stay in the low hundreds of KB.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

STATE_DIR = os.environ.get("ARGSHAPE_STATE_DIR") or os.path.expanduser("~/.local/state/argshape")
DB_PATH = os.path.join(STATE_DIR, "argshape.db")
CONFIG_PATH = os.environ.get("ARGSHAPE_CONFIG") or os.path.expanduser("~/.config/argshape/config.json")
MODES = ("off", "observe", "advise", "correct")
SCHEMA_VERSION = 1

DEFAULT_CONFIG = {
    "mode": "correct",
    "bins": {},                  # per-binary mode override, e.g. {"git": "advise"}
    "sql": "correct",            # mode ceiling for SQL identifier/value corrections
    "sql_active_learning": True, # on a column error, query the catalog via the same connection
    "min_pair_sessions": 2,      # session-pair fixes need this many independent sightings
    "breaker_window_h": 24,
    "breaker_min_applied": 4,
    "breaker_fail_ratio": 0.5,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bins (
  bin_key TEXT PRIMARY KEY, name TEXT, path TEXT, version TEXT,
  sha256 TEXT, first_seen REAL);
CREATE TABLE IF NOT EXISTS specs (
  bin_key TEXT, ctx TEXT, spec TEXT, learned_at REAL,
  PRIMARY KEY (bin_key, ctx));
CREATE TABLE IF NOT EXISTS rejects (
  bin_key TEXT, name TEXT, ctx TEXT, kind TEXT, token TEXT,
  n INTEGER DEFAULT 0, first REAL, last REAL,
  PRIMARY KEY (bin_key, ctx, kind, token));
CREATE TABLE IF NOT EXISTS fixes (
  scope TEXT, ctx TEXT, kind TEXT, bad TEXT, good TEXT, source TEXT,
  n INTEGER DEFAULT 0, sessions TEXT DEFAULT '[]', n_applied INTEGER DEFAULT 0,
  n_ok INTEGER DEFAULT 0, n_bad INTEGER DEFAULT 0, demoted INTEGER DEFAULT 0,
  first REAL, last REAL, name TEXT,
  PRIMARY KEY (scope, ctx, kind, bad, good));
CREATE TABLE IF NOT EXISTS sigs (
  name TEXT, bin_key TEXT, ctx TEXT, err_class TEXT, sig TEXT, via TEXT,
  n INTEGER DEFAULT 0, first REAL, last REAL,
  PRIMARY KEY (name, ctx, err_class, sig));
CREATE TABLE IF NOT EXISTS pending (
  session TEXT, bin_key TEXT, ctx TEXT, bad TEXT, opts TEXT, ts REAL,
  PRIMARY KEY (session, bin_key, ctx, bad));
CREATE TABLE IF NOT EXISTS columns (
  conn TEXT, relation TEXT, cols TEXT, source TEXT, learned_at REAL,
  PRIMARY KEY (conn, relation));
CREATE TABLE IF NOT EXISTS shapes (
  conn TEXT, col TEXT, family TEXT, variant TEXT, n INTEGER, learned_at REAL,
  PRIMARY KEY (conn, col));
CREATE TABLE IF NOT EXISTS applied (
  tool_use_id TEXT PRIMARY KEY, session TEXT, ts REAL, fixes TEXT,
  before TEXT, after TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(create: bool = True) -> sqlite3.Connection | None:
    if not create and not os.path.exists(DB_PATH):
        return None
    os.makedirs(STATE_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=2.0, isolation_level=None)
    db.execute("PRAGMA synchronous=NORMAL")
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        db.execute("PRAGMA journal_mode=WAL")      # persistent; set once
        db.executescript(SCHEMA)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return db


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy: never mutate defaults
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    env = os.environ.get("ARGSHAPE_MODE")
    if env in MODES:
        cfg["mode"] = env
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    keep = {k: v for k, v in cfg.items() if DEFAULT_CONFIG.get(k) != v or k == "mode"}
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(keep, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def effective_mode(cfg: dict, name: str | None, sql: bool = False) -> str:
    """The lowest of global, per-binary and (for SQL) the sql ceiling."""
    rank = {m: i for i, m in enumerate(MODES)}
    mode = cfg.get("mode", "correct")
    if name and name in cfg.get("bins", {}):
        mode = min(mode, cfg["bins"][name], key=rank.get)
    if sql:
        mode = min(mode, cfg.get("sql", "correct"), key=rank.get)
    return mode if mode in rank else "advise"


def now() -> float:
    return time.time()


def upsert_count(db, table: str, keys: dict, extra: dict | None = None) -> None:
    t = now()
    cols = {**keys, **(extra or {})}
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    keycols = ", ".join(keys)
    upd = ", ".join([f"{c}=excluded.{c}" for c in (extra or {})] + ["n=n+1", "last=excluded.last"])
    db.execute(
        f"INSERT INTO {table} ({names}, n, first, last) VALUES ({marks}, 1, ?, ?) "
        f"ON CONFLICT ({keycols}) DO UPDATE SET {upd}",
        [*cols.values(), t, t])


def get_spec(db, bin_key: str, ctx: str = ""):
    row = db.execute("SELECT spec FROM specs WHERE bin_key=? AND ctx=?", (bin_key, ctx)).fetchone()
    if not row:
        return None
    from helpspec import Spec
    return Spec.from_json(json.loads(row[0]))


def put_spec(db, bin_key: str, ctx: str, spec) -> None:
    db.execute("INSERT OR REPLACE INTO specs VALUES (?,?,?,?)",
               (bin_key, ctx, json.dumps(spec.to_json()), now()))


def add_fix(db, scope: str, ctx: str, kind: str, bad: str, good: str,
            source: str, session: str, name: str) -> None:
    t = now()
    row = db.execute("SELECT sessions FROM fixes WHERE scope=? AND ctx=? AND kind=? AND bad=? AND good=?",
                     (scope, ctx, kind, bad, good)).fetchone()
    sessions = json.loads(row[0]) if row else []
    if session and session not in sessions:
        sessions = (sessions + [session])[-20:]
    if row:
        db.execute("UPDATE fixes SET n=n+1, sessions=?, last=?, source=CASE WHEN ?='tool-hint' THEN ? ELSE source END "
                   "WHERE scope=? AND ctx=? AND kind=? AND bad=? AND good=?",
                   (json.dumps(sessions), t, source, source, scope, ctx, kind, bad, good))
    else:
        db.execute("INSERT INTO fixes (scope, ctx, kind, bad, good, source, n, sessions, first, last, name) "
                   "VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                   (scope, ctx, kind, bad, good, source, json.dumps(sessions), t, t, name))


def meta_get(db, k: str, default: str = "") -> str:
    row = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(db, k: str, v: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
