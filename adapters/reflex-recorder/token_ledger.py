#!/usr/bin/env python3
"""token_ledger.py — per-request LLM usage ledger built from session transcripts.

The bus carries tool activity, not model requests: only ~4% of
bus.agent.activity.v1 events have token counts and none have cache fields. The
transcripts are the ground truth: every assistant message carries the full
`usage` block (input, cache read, cache creation 5m/1h, output). This module
ingests them incrementally into a separate SQLite DB (tokens.db, never runs.db)
so token-efficiency questions are plain SQL.

Sources (relpath-keyed, so the same transcript seen in two roots is one row):
  claude  ~/.cache/nervous-bus/reflex/transcripts   (durable mirror, preferred)
          ~/.claude/projects                        (live; read only where the
                                                     mirror has no copy yet)
  mmx     ~/.mmx/projects                           (claude-minimax-sandboxed)

Each file is read from its last committed byte offset, complete lines only.
A file shorter than its recorded offset (mirror lagging live) is skipped until
it catches up. Lines are pre-filtered on raw bytes before json parsing.

    token_ledger.py [--db PATH] [--max-files N] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

HOME = Path.home()
DEFAULT_DB = HOME / ".cache" / "nervous-bus" / "reflex" / "tokens.db"
ROOTS = (
    ("claude", HOME / ".cache" / "nervous-bus" / "reflex" / "transcripts"),
    ("claude", HOME / ".claude" / "projects"),
    ("mmx", HOME / ".mmx" / "projects"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    key         TEXT PRIMARY KEY,      -- source:relpath
    offset      INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    key           TEXT PRIMARY KEY,    -- same as files.key
    source        TEXT NOT NULL,       -- claude | mmx
    project       TEXT,
    session_id    TEXT,
    agent_id      TEXT,                -- set for subagent transcripts
    agent_type    TEXT,                -- from <agent>.meta.json
    agent_model   TEXT,                -- requested tier from meta.json
    description   TEXT,
    is_subagent   INTEGER NOT NULL DEFAULT 0,
    cwd           TEXT,
    started       TEXT,
    ended         TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    key            TEXT PRIMARY KEY,   -- session key + message id
    session_key    TEXT NOT NULL,
    seq            INTEGER NOT NULL,   -- 1 = first request of the transcript
    ts             TEXT NOT NULL,
    model          TEXT,
    input          INTEGER NOT NULL,
    cache_read     INTEGER NOT NULL,
    cache_create   INTEGER NOT NULL,
    cache_1h       INTEGER NOT NULL,
    cache_5m       INTEGER NOT NULL,
    output         INTEGER NOT NULL,
    thinking       INTEGER NOT NULL,
    n_tool_uses    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_req_session ON requests(session_key, seq);
CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS tool_calls (
    id            TEXT PRIMARY KEY,    -- tool_use id
    session_key   TEXT NOT NULL,
    ts            TEXT NOT NULL,
    tool          TEXT NOT NULL,
    cmd_class     TEXT,                -- test | build | commit | other (Bash only)
    cmd           TEXT,                -- first 300 chars of a Bash command
    cmd_hash      TEXT,
    file_path     TEXT,                -- Read/Edit/Write target
    result_chars  INTEGER              -- size of the tool_result fed back
);
CREATE INDEX IF NOT EXISTS idx_tc_session ON tool_calls(session_key, ts);
CREATE INDEX IF NOT EXISTS idx_tc_ts ON tool_calls(ts);
CREATE TABLE IF NOT EXISTS instructions (
    session_key   TEXT NOT NULL,
    path          TEXT NOT NULL,       -- CLAUDE.md / memory file loaded at start
    kind          TEXT,
    chars         INTEGER NOT NULL,
    PRIMARY KEY (session_key, path)
);
"""

_TEST_RE = re.compile(
    r"\b(pytest|cargo (?:nextest|test)|go test|npm (?:run )?test|pnpm (?:run )?test|"
    r"yarn test|vitest|jest|ctest|swift test|gradlew? \S*test|make \S*test\S*|"
    r"unittest)\b")
_BUILD_RE = re.compile(
    r"\b(cargo (?:build|check|clippy)|go build|go vet|npm run build|pnpm (?:run )?build|"
    r"tsc\b|make\b|cmake --build|gradlew? \S*(?:assemble|build)|hearth-build)")
_COMMIT_RE = re.compile(r"\bgit (?:-C \S+ )?commit\b")
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_PROJECT_RES = (
    re.compile(r"^/home/[^/]+/data2/worktrees/([^/]+)"),
    re.compile(r"^/home/[^/]+/data2/orca/([^/]+)"),
    re.compile(r"^/home/[^/]+/projects/([^/]+)"),
    re.compile(r"^/home/[^/]+/data2/([^/]+)"),
)


def classify_command(cmd: str) -> str:
    if _COMMIT_RE.search(cmd):
        return "commit"
    if _TEST_RE.search(cmd):
        return "test"
    if _BUILD_RE.search(cmd):
        return "build"
    return "other"


def project_of(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    for rx in _PROJECT_RES:
        m = rx.match(cwd)
        if m:
            return m.group(1)
    return os.path.basename(cwd.rstrip("/")) or None


def _iter_transcripts(root: Path) -> Iterator[tuple[str, Path]]:
    """(relpath, path) for <dir>/*.jsonl and <dir>/<session>/subagents/*.jsonl."""
    try:
        dirs = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            names = os.listdir(d.path)
        except OSError:
            continue
        for n in sorted(names):
            p = os.path.join(d.path, n)
            if n.endswith(".jsonl"):
                yield os.path.relpath(p, root), Path(p)
            else:
                sub = os.path.join(p, "subagents")
                try:
                    subs = sorted(os.listdir(sub))
                except OSError:
                    continue
                for m in subs:
                    if m.endswith(".jsonl"):
                        q = os.path.join(sub, m)
                        yield os.path.relpath(q, root), Path(q)


def discover(roots=ROOTS) -> dict[str, tuple[str, Path]]:
    """key -> (source, path); the first root listing a relpath wins."""
    found: dict[str, tuple[str, Path]] = {}
    for source, root in roots:
        for rel, path in _iter_transcripts(root):
            found.setdefault(f"{source}:{rel}", (source, path))
    return found


def _content_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) if isinstance(c, dict) else len(str(c)) for c in content)
    return len(str(content or ""))


class _Session:
    """Per-file state accumulated while parsing new lines."""

    def __init__(self, conn: sqlite3.Connection, key: str):
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM requests WHERE session_key=?", (key,)).fetchone()
        self.seq = row[0]
        self.meta: dict = {}


def ingest_file(conn: sqlite3.Connection, key: str, source: str, path: Path) -> int:
    """Ingest new complete lines of one transcript. Returns requests added."""
    row = conn.execute("SELECT offset FROM files WHERE key=?", (key,)).fetchone()
    offset = row[0] if row else 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= offset:
        return 0
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    end = chunk.rfind(b"\n")
    if end < 0:
        return 0
    chunk = chunk[: end + 1]

    st = _Session(conn, key)
    is_sub = "/subagents/" in key
    meta = {}
    if is_sub and not row:
        try:
            meta = json.loads(path.with_suffix("").with_suffix(".meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
    sess = {"session_id": None, "agent_id": None, "cwd": None, "first": None, "last": None}
    seen_msg: set[str] = set()
    added = 0

    for line in chunk.split(b"\n"):
        if not line:
            continue
        is_result = b'"tool_result"' in line
        if not (b'"assistant"' in line or is_result or b'"instructions"' in line):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("timestamp") or ""
        sess["session_id"] = sess["session_id"] or d.get("sessionId")
        sess["agent_id"] = sess["agent_id"] or d.get("agentId")
        sess["cwd"] = sess["cwd"] or d.get("cwd")
        if ts:
            sess["first"] = sess["first"] or ts
            sess["last"] = ts
        typ = d.get("type")
        if typ == "attachment":
            att = d.get("attachment") or {}
            for fobj in att.get("files") or []:
                conn.execute(
                    "INSERT OR REPLACE INTO instructions VALUES (?,?,?,?)",
                    (key, fobj.get("path", "?"), fobj.get("type"), len(fobj.get("content") or "")))
            continue
        msg = d.get("message") or {}
        content = msg.get("content")
        if typ == "assistant":
            usage = msg.get("usage") or {}
            mid = msg.get("id") or d.get("requestId") or d.get("uuid")
            tool_uses = [c for c in content or [] if isinstance(c, dict) and c.get("type") == "tool_use"]
            for tu in tool_uses:
                name = tu.get("name") or "?"
                inp = tu.get("input") or {}
                cmd = cmd_class = cmd_hash = None
                if name == "Bash":
                    raw = str(inp.get("command") or "")
                    cmd, cmd_class = raw[:300], classify_command(raw)
                    cmd_hash = hashlib.sha1(raw.encode()).hexdigest()[:16]
                fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
                conn.execute(
                    "INSERT OR IGNORE INTO tool_calls (id, session_key, ts, tool, cmd_class, cmd,"
                    " cmd_hash, file_path) VALUES (?,?,?,?,?,?,?,?)",
                    (tu.get("id") or f"{key}:{ts}:{name}", key, ts, name, cmd_class, cmd,
                     cmd_hash, fp if isinstance(fp, str) else None))
            if not usage or not mid:
                continue
            rkey = f"{key}#{mid}"
            if mid in seen_msg:
                # streamed content blocks repeat the same usage; count tool uses only
                if tool_uses:
                    conn.execute("UPDATE requests SET n_tool_uses = n_tool_uses + ? WHERE key=?",
                                 (len(tool_uses), rkey))
                continue
            seen_msg.add(mid)
            cc = usage.get("cache_creation") or {}
            st.seq += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rkey, key, st.seq, ts, msg.get("model"),
                 int(usage.get("input_tokens") or 0),
                 int(usage.get("cache_read_input_tokens") or 0),
                 int(usage.get("cache_creation_input_tokens") or 0),
                 int(cc.get("ephemeral_1h_input_tokens") or 0),
                 int(cc.get("ephemeral_5m_input_tokens") or 0),
                 int(usage.get("output_tokens") or 0),
                 int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0),
                 len(tool_uses)))
            if cur.rowcount:
                added += 1
            else:
                st.seq -= 1
        elif is_result and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    conn.execute("UPDATE tool_calls SET result_chars=? WHERE id=?",
                                 (_content_len(c.get("content")), c.get("tool_use_id")))

    conn.execute(
        "INSERT INTO sessions (key, source, project, session_id, agent_id, agent_type, agent_model,"
        " description, is_subagent, cwd, started, ended) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET"
        "  ended=COALESCE(excluded.ended, ended),"
        "  started=COALESCE(started, excluded.started),"
        "  session_id=COALESCE(session_id, excluded.session_id),"
        "  agent_id=COALESCE(agent_id, excluded.agent_id),"
        "  cwd=COALESCE(cwd, excluded.cwd),"
        "  project=COALESCE(project, excluded.project)",
        (key, source, project_of(meta.get("worktreePath") or sess["cwd"]), sess["session_id"],
         sess["agent_id"], meta.get("agentType"), meta.get("model"), meta.get("description"),
         int(is_sub), sess["cwd"], sess["first"], sess["last"]))
    conn.execute(
        "INSERT INTO files VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET"
        " offset=excluded.offset, updated_at=excluded.updated_at",
        (key, offset + len(chunk), time.time()))
    return added


def inherit_parent_project(conn: sqlite3.Connection) -> None:
    """A subagent's worktree is named after the lane (data2/worktrees/<lane>/
    agent-x), not the repo, so subagents take the project of the main session
    that spawned them whenever that transcript is in the ledger."""
    conn.execute("""
        UPDATE sessions SET project = (
            SELECT p.project FROM sessions p
            WHERE p.key = substr(sessions.key, 1, instr(sessions.key, '/subagents/') - 1) || '.jsonl')
        WHERE is_subagent = 1 AND EXISTS (
            SELECT 1 FROM sessions p
            WHERE p.key = substr(sessions.key, 1, instr(sessions.key, '/subagents/') - 1) || '.jsonl'
              AND p.project IS NOT NULL)""")
    conn.commit()


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def run(db: Path = DEFAULT_DB, roots=ROOTS, max_files: Optional[int] = None) -> dict:
    conn = connect(db)
    files = discover(roots)
    stats = {"files_seen": len(files), "files_read": 0, "requests_added": 0, "errors": 0}
    t0 = time.time()
    for i, (key, (source, path)) in enumerate(sorted(files.items())):
        if max_files is not None and stats["files_read"] >= max_files:
            break
        try:
            n = ingest_file(conn, key, source, path)
        except Exception as e:  # one bad file never aborts the pass
            conn.rollback()
            stats["errors"] += 1
            sys.stderr.write(f"[token-ledger] {key}: {type(e).__name__}: {e}\n")
            continue
        conn.commit()
        if n:
            stats["files_read"] += 1
            stats["requests_added"] += n
    inherit_parent_project(conn)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--max-files", type=int)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    stats = run(a.db, max_files=a.max_files)
    print(json.dumps(stats) if a.json else stats)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
