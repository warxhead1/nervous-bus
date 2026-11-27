"""SQLite index: a cache rebuilt from the JSONL, never a source of truth."""
import hashlib
import json
import os
import sqlite3

from .fold import fold


def fingerprint(ledger):
    h = hashlib.sha256()
    for p in ledger.files():
        st = os.stat(p)
        h.update(f"{os.path.basename(p)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def rebuild(ledger, db_path, records=None):
    if records is None:
        records, _ = ledger.read()
    state = fold(records)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.unlink(tmp)
    con = sqlite3.connect(tmp)
    con.executescript(
        "CREATE TABLE records(id TEXT PRIMARY KEY, ts TEXT, type TEXT, by TEXT, via TEXT, json TEXT);"
        "CREATE TABLE entities(id TEXT PRIMARY KEY, kind TEXT, state TEXT, json TEXT);"
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);"
    )
    con.executemany(
        "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?)",
        [(r.get("id"), r.get("ts"), r.get("type"), r.get("by"), r.get("via"), json.dumps(r)) for r in records],
    )
    con.executemany(
        "INSERT INTO entities VALUES(?,?,?,?)",
        [(e["id"], e["kind"], e["state"], json.dumps(e)) for e in state.values()],
    )
    con.execute("INSERT INTO meta VALUES('fingerprint',?)", (fingerprint(ledger),))
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return len(records), len(state)


def ensure(ledger, db_path):
    """Open the index, rebuilding when the ledger changed. Returns a connection."""
    fp = fingerprint(ledger)
    cur = None
    if os.path.exists(db_path):
        try:
            c = sqlite3.connect(db_path)
            row = c.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
            cur = row[0] if row else None
            c.close()
        except sqlite3.Error:
            cur = None
    if cur != fp:
        rebuild(ledger, db_path)
    return sqlite3.connect(db_path)
