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
  codex   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl and the Orca account
          homes ~/.config/orca/codex-accounts/*/home/sessions (one row per
          session uuid across roots; jsonl, byte-offset incremental)
  gemini  ~/.gemini/tmp/<hash>/chats/*.json (whole-file JSON, re-read when
          (mtime, size) changes; messages are keyed so a re-read adds only new)

Each file is read from its last committed byte offset, complete lines only.
A file shorter than its recorded offset (mirror lagging live) is skipped until
it catches up. Lines are pre-filtered on raw bytes before json parsing.

Tool calls also carry their result timestamp, duration and a TIME CLASS
(wait | loop | gate | test | build | commit | git | read | other for shell
commands, the tool name otherwise), so token_report can say where a lane's
wall time went. Rows ingested before those columns existed are filled by
`--backfill-tool-times` (re-reads only the transcripts that have such rows).

    token_ledger.py [--db PATH] [--max-files N] [--json]
    token_ledger.py --backfill-tool-times [--project P]
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

CODEX_ROOTS = (
    HOME / ".codex" / "sessions",
    *sorted((HOME / ".config" / "orca" / "codex-accounts").glob("*/home/sessions")),
)
GEMINI_ROOTS = (HOME / ".gemini" / "tmp",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_state (
    key         TEXT PRIMARY KEY,      -- files.key; per-file parser carry-over
    model       TEXT,
    last_total  TEXT                   -- codex: last total_token_usage seen
);
CREATE TABLE IF NOT EXISTS file_sig (
    key         TEXT PRIMARY KEY,      -- whole-file formats (gemini)
    mtime_ns    INTEGER NOT NULL,
    size        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    key         TEXT PRIMARY KEY,      -- source:relpath
    offset      INTEGER NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    key           TEXT PRIMARY KEY,    -- same as files.key
    source        TEXT NOT NULL,       -- claude | mmx | codex | gemini
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


# Columns added after the first release. Added by ALTER on connect(), never in
# SCHEMA's CREATE, so an older reader of the same tokens.db keeps working.
TOOL_CALL_COLUMNS = (("result_ts", "TEXT"), ("duration_s", "REAL"), ("time_class", "TEXT"))

_GATE_RE = re.compile(
    r"(?:^|[\s/;&|])(?:check|gate|ci|preflight)(?:[_-][\w-]+)?\.sh\b|\bmake\s+(?:check|gate|ci)\b")
_READ_PROGS = {"cat", "head", "tail", "grep", "rg", "ls", "find", "wc", "sed", "awk", "jq",
               "stat", "diff", "tree", "file", "du", "df", "less", "nl", "sort", "uniq", "cut"}


def time_class(tool: str, raw_cmd: Optional[str] = None) -> str:
    """Where a tool call's time goes. Shell commands: a sleep-polling `wait`, a
    `loop` that runs a program serially, a `gate` script, then classify_command's
    test / build / commit, then `git`, `read` (inspection commands), `other`.
    Any other tool: its name."""
    if raw_cmd is None:
        return tool
    from transcript_steps import is_poll_wait, strip_heredocs
    from detectors.param_sweep_thrash import _LOOP_RE, _program_words, harness_runs, shell_segments
    if is_poll_wait(raw_cmd):
        return "wait"
    if _LOOP_RE.search(strip_heredocs(raw_cmd)) and harness_runs(raw_cmd):
        return "loop"
    if _GATE_RE.search(raw_cmd):
        return "gate"
    c = classify_command(raw_cmd)
    if c != "other":
        return c
    for seg in shell_segments(strip_heredocs(raw_cmd)):
        words = _program_words(seg)
        if not words or words[0] in ("cd", "export", "set", "echo", "printf", "true", "mkdir"):
            continue
        prog = os.path.basename(words[0])
        if prog == "git":
            return "git"
        if prog in _READ_PROGS:
            return "read"
        return "other"
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


# result size, result timestamp, and the call's duration (NULL if a stamp is bad)
_RESULT_SQL = ("UPDATE tool_calls SET result_chars=?, result_ts=?,"
               " duration_s=(julianday(?) - julianday(ts)) * 86400.0 WHERE id=?")
_BACKFILL_RESULT_SQL = ("UPDATE tool_calls SET result_chars=COALESCE(result_chars, ?), result_ts=?,"
                        " duration_s=(julianday(?) - julianday(ts)) * 86400.0"
                        " WHERE id=? AND result_ts IS NULL")


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
                cmd = cmd_class = cmd_hash = raw = None
                if name == "Bash":
                    raw = str(inp.get("command") or "")
                    cmd, cmd_class = raw[:300], classify_command(raw)
                    cmd_hash = hashlib.sha1(raw.encode()).hexdigest()[:16]
                fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
                conn.execute(
                    "INSERT OR IGNORE INTO tool_calls (id, session_key, ts, tool, cmd_class, cmd,"
                    " cmd_hash, file_path, time_class) VALUES (?,?,?,?,?,?,?,?,?)",
                    (tu.get("id") or f"{key}:{ts}:{name}", key, ts, name, cmd_class, cmd,
                     cmd_hash, fp if isinstance(fp, str) else None, time_class(name, raw)))
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
        elif is_result and typ == "user" and isinstance(content, list):
            # `api-request-blob` records replay tool results with no timestamp;
            # only the real `user` record dates (and sizes) the result.
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    conn.execute(_RESULT_SQL, (_content_len(c.get("content")), ts or None,
                                               ts or None, c.get("tool_use_id")))

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


_CODEX_SHELL = {"exec_command", "shell", "local_shell", "container.exec", "exec"}


def _upsert_session(conn, key, source, sess: dict, is_sub: bool = False) -> None:
    conn.execute(
        "INSERT INTO sessions (key, source, project, session_id, agent_id, agent_type, agent_model,"
        " description, is_subagent, cwd, started, ended) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET"
        "  ended=COALESCE(excluded.ended, ended),"
        "  started=COALESCE(started, excluded.started),"
        "  session_id=COALESCE(session_id, excluded.session_id),"
        "  cwd=COALESCE(cwd, excluded.cwd),"
        "  project=COALESCE(project, excluded.project)",
        (key, source, project_of(sess.get("cwd")), sess.get("session_id"), None, None, None,
         None, int(is_sub), sess.get("cwd"), sess.get("first"), sess.get("last")))


def _tool_row(conn, key, ts, call_id, name, raw_cmd, file_path) -> None:
    cmd = cmd_class = cmd_hash = None
    if raw_cmd is not None:
        cmd, cmd_class = raw_cmd[:300], classify_command(raw_cmd)
        cmd_hash = hashlib.sha1(raw_cmd.encode()).hexdigest()[:16]
    conn.execute(
        "INSERT OR IGNORE INTO tool_calls (id, session_key, ts, tool, cmd_class, cmd, cmd_hash,"
        " file_path, time_class) VALUES (?,?,?,?,?,?,?,?,?)",
        (call_id or f"{key}:{ts}:{name}", key, ts, name, cmd_class, cmd, cmd_hash,
         file_path if isinstance(file_path, str) else None, time_class(name, raw_cmd)))


def ingest_codex(conn: sqlite3.Connection, key: str, path: Path) -> int:
    """Stream new complete lines of one Codex rollout. Returns requests added.

    One request per token_count event whose total_token_usage differs from the
    previous one (Codex re-emits identical totals); info:null events are skipped.
    input = input_tokens - cached_input_tokens (OpenAI counts cached inside input);
    output_tokens already includes reasoning, so thinking is a subset of output.
    """
    row = conn.execute("SELECT offset FROM files WHERE key=?", (key,)).fetchone()
    offset = row[0] if row else 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= offset:
        return 0
    st = conn.execute("SELECT model, last_total FROM ledger_state WHERE key=?", (key,)).fetchone()
    model, last_total = (st[0], st[1]) if st else (None, None)
    seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM requests WHERE session_key=?",
                       (key,)).fetchone()[0]
    sess = {"session_id": None, "cwd": None, "first": None, "last": None}
    is_sub = False
    pending_tools = 0
    added = 0
    pos = offset
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break
            line_pos, pos = pos, pos + len(line)
            is_tc = b'"token_count"' in line
            is_call = b'"function_call' in line or b'"custom_tool_call' in line
            if not (is_tc or is_call or b'"turn_context"' in line or b'"session_meta"' in line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ts = d.get("timestamp") or ""
            if ts:
                sess["first"] = sess["first"] or ts
                sess["last"] = ts
            typ, p = d.get("type"), d.get("payload")
            if not isinstance(p, dict):
                continue
            if typ == "session_meta":
                sess["session_id"] = sess["session_id"] or p.get("id") or p.get("session_id")
                sess["cwd"] = sess["cwd"] or p.get("cwd")
                is_sub = p.get("thread_source") == "subagent" or isinstance(p.get("source"), dict)
            elif typ == "turn_context":
                model = p.get("model") or model
            elif typ == "response_item":
                pt = p.get("type")
                if pt in ("function_call", "custom_tool_call"):
                    name = p.get("name") or "?"
                    raw = p.get("arguments") if pt == "function_call" else p.get("input")
                    cmd = fp = None
                    if pt == "function_call":
                        try:
                            a = json.loads(raw) if isinstance(raw, str) else {}
                        except ValueError:
                            a = {}
                        a = a if isinstance(a, dict) else {}
                        if name in _CODEX_SHELL:
                            c = a.get("cmd") or a.get("command")
                            cmd = c if isinstance(c, str) else (
                                " ".join(map(str, c)) if isinstance(c, list) else None)
                        fp = a.get("path") or a.get("file_path")
                    elif name in _CODEX_SHELL and isinstance(raw, str):
                        cmd = raw
                    _tool_row(conn, key, ts, p.get("call_id"), name, cmd, fp)
                    pending_tools += 1
                elif pt in ("function_call_output", "custom_tool_call_output"):
                    conn.execute("UPDATE tool_calls SET result_chars=? WHERE id=?",
                                 (_content_len(p.get("output")), p.get("call_id")))
            elif typ == "event_msg" and p.get("type") == "token_count":
                info = p.get("info")
                if not isinstance(info, dict):
                    continue
                total = json.dumps(info.get("total_token_usage"), sort_keys=True)
                if total == last_total:
                    continue
                last_total = total
                u = info.get("last_token_usage") or {}
                cached = int(u.get("cached_input_tokens") or 0)
                inp = int(u.get("input_tokens") or 0)
                seq += 1
                cur = conn.execute(
                    "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{key}#{line_pos}", key, seq, ts, model, max(0, inp - cached), cached,
                     int(u.get("cache_write_input_tokens") or 0), 0, 0,
                     int(u.get("output_tokens") or 0),
                     int(u.get("reasoning_output_tokens") or 0), pending_tools))
                pending_tools = 0
                if cur.rowcount:
                    added += 1
                else:
                    seq -= 1
    if model and added:
        # the first turn_context can land after the first token_count events
        conn.execute("UPDATE requests SET model=? WHERE session_key=? AND model IS NULL", (model, key))
    _upsert_session(conn, key, "codex", sess, is_sub)
    conn.execute("INSERT INTO ledger_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET"
                 " model=excluded.model, last_total=excluded.last_total", (key, model, last_total))
    conn.execute(
        "INSERT INTO files VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET"
        " offset=excluded.offset, updated_at=excluded.updated_at", (key, pos, time.time()))
    return added


def discover_codex(roots=CODEX_ROOTS, stats: Optional[dict] = None) -> dict[str, tuple[str, Path]]:
    """`codex:<session uuid>` -> (codex, path). The same session under several
    roots (~/.codex plus Orca account homes) is one key; the largest copy wins.
    stats gets files_seen / duplicate_files (files whose uuid was already listed)."""
    best: dict[str, tuple[int, Path]] = {}
    seen = dup = 0
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if not (n.startswith("rollout-") and n.endswith(".jsonl")):
                    continue
                p = Path(dirpath) / n
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                seen += 1
                k = f"codex:{n[:-len('.jsonl')][-36:]}"
                if k in best:
                    dup += 1
                    if sz <= best[k][0]:
                        continue
                best[k] = (sz, p)
    if stats is not None:
        stats.update(files_seen=seen, duplicate_files=dup)
    return {k: ("codex", v[1]) for k, v in best.items()}


def discover_gemini(roots=GEMINI_ROOTS) -> dict[str, tuple[str, Path]]:
    found: dict[str, tuple[str, Path]] = {}
    for root in roots:
        try:
            hashes = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            continue
        for h in hashes:
            chats = os.path.join(h.path, "chats")
            try:
                names = sorted(os.listdir(chats))
            except OSError:
                continue
            for n in names:
                if n.endswith(".json"):
                    found.setdefault(f"gemini:{h.name}/{n}", ("gemini", Path(chats) / n))
    return found


def ingest_gemini(conn: sqlite3.Connection, key: str, path: Path) -> int:
    """Re-read one whole-file chat JSON when (mtime, size) changed. Requests are
    keyed by session id + message id so a rewrite (or the same session split over
    two files) only ever adds messages not yet in the ledger."""
    try:
        stt = path.stat()
    except OSError:
        return 0
    sig = conn.execute("SELECT mtime_ns, size FROM file_sig WHERE key=?", (key,)).fetchone()
    if sig and tuple(sig) == (stt.st_mtime_ns, stt.st_size):
        return 0
    try:
        d = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return 0  # mid-rewrite; signature left unrecorded so the next pass retries
    if not isinstance(d, dict):
        return 0
    sid = d.get("sessionId") or key
    cwd = None
    try:
        cwd = (path.parent.parent / ".project_root").read_text().strip() or None
    except OSError:
        pass
    seq = 0
    added = 0
    first = last = None
    for m in d.get("messages") or []:
        if not isinstance(m, dict):
            continue
        ts = m.get("timestamp") or ""
        if ts:
            first, last = first or ts, ts
        tcs = [t for t in m.get("toolCalls") or [] if isinstance(t, dict)]
        for t in tcs:
            args = t.get("args") if isinstance(t.get("args"), dict) else {}
            name = t.get("name") or "?"
            c = args.get("command") if name == "run_shell_command" else None
            fp = args.get("file_path") or args.get("absolute_path") or args.get("path")
            tid = t.get("id") or f"{key}:{ts}:{name}"
            _tool_row(conn, key, t.get("timestamp") or ts, tid, name,
                      c if isinstance(c, str) else None, fp)
            if t.get("result") is not None:
                conn.execute("UPDATE tool_calls SET result_chars=? WHERE id=?",
                             (len(json.dumps(t["result"])), tid))
        tok = m.get("tokens")
        if m.get("type") != "gemini" or not isinstance(tok, dict):
            continue
        seq += 1
        cached = int(tok.get("cached") or 0)
        thoughts = int(tok.get("thoughts") or 0)
        mid = m.get("id") or f"{seq}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"gemini:{sid}#{mid}", key, seq, ts, m.get("model"),
             max(0, int(tok.get("input") or 0) - cached) + int(tok.get("tool") or 0), cached,
             0, 0, 0, int(tok.get("output") or 0) + thoughts, thoughts, len(tcs)))
        added += cur.rowcount
    if seq:  # chats with no model turn get no session row
        _upsert_session(conn, key, "gemini",
                        {"session_id": sid, "cwd": cwd, "first": d.get("startTime") or first,
                         "last": d.get("lastUpdated") or last}, d.get("kind") == "subagent")
    conn.execute("INSERT INTO file_sig VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET"
                 " mtime_ns=excluded.mtime_ns, size=excluded.size",
                 (key, stt.st_mtime_ns, stt.st_size))
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
    have = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")}
    for col, typ in TOOL_CALL_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE tool_calls ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def backfill_tool_times(conn: sqlite3.Connection, files: dict[str, tuple[str, Path]],
                        project: Optional[str] = None) -> dict:
    """Fill result_ts / duration_s / time_class on rows ingested before those
    columns existed. Re-reads only claude/mmx transcripts that hold such rows;
    never inserts, never moves a file offset."""
    sql = ("SELECT DISTINCT t.session_key FROM tool_calls t JOIN sessions s ON s.key = t.session_key"
           " WHERE s.source IN ('claude', 'mmx') AND (t.time_class IS NULL OR t.result_ts IS NULL)")
    params: tuple = ()
    if project:
        sql += " AND s.project = ?"
        params = (project,)
    keys = [r[0] for r in conn.execute(sql, params)]
    stats = {"sessions": len(keys), "files_read": 0, "missing": 0}
    for key in keys:
        hit = files.get(key)
        if hit is None:
            stats["missing"] += 1
            continue
        try:
            fh = open(hit[1], "rb")
        except OSError:
            stats["missing"] += 1
            continue
        with fh:
            for line in fh:
                if not (b'"tool_use"' in line or b'"tool_result"' in line):
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ts = d.get("timestamp") or None
                content = (d.get("message") or {}).get("content")
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        raw = (str((c.get("input") or {}).get("command") or "")
                               if c.get("name") == "Bash" else None)
                        conn.execute(
                            "UPDATE tool_calls SET time_class=? WHERE id=? AND time_class IS NULL",
                            (time_class(c.get("name") or "?", raw), c.get("id")))
                    elif c.get("type") == "tool_result" and ts:
                        conn.execute(_BACKFILL_RESULT_SQL, (_content_len(c.get("content")), ts, ts,
                                                            c.get("tool_use_id")))
        conn.commit()
        stats["files_read"] += 1
    return stats


def run(db: Path = DEFAULT_DB, roots=None, max_files: Optional[int] = None,
        codex_roots=None, gemini_roots=None) -> dict:
    """Default (roots=None) ingests every agent. Passing `roots` explicitly
    restricts the pass to those claude/mmx roots unless codex/gemini roots are
    also passed."""
    default = roots is None
    roots = ROOTS if default else roots
    codex_roots = CODEX_ROOTS if (default and codex_roots is None) else (codex_roots or ())
    gemini_roots = GEMINI_ROOTS if (default and gemini_roots is None) else (gemini_roots or ())
    conn = connect(db)
    files = discover(roots)
    codex_stats: dict = {}
    files.update(discover_codex(codex_roots, codex_stats))
    files.update(discover_gemini(gemini_roots))
    stats = {"files_seen": len(files), "files_read": 0, "requests_added": 0, "errors": 0,
             "codex_files_seen": codex_stats.get("files_seen", 0),
             "codex_duplicate_files": codex_stats.get("duplicate_files", 0),
             "requests_added_by_source": {}}
    t0 = time.time()
    for i, (key, (source, path)) in enumerate(sorted(files.items())):
        if max_files is not None and stats["files_read"] >= max_files:
            break
        try:
            if source == "codex":
                n = ingest_codex(conn, key, path)
            elif source == "gemini":
                n = ingest_gemini(conn, key, path)
            else:
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
            by = stats["requests_added_by_source"]
            by[source] = by.get(source, 0) + n
    inherit_parent_project(conn)
    stats["elapsed_s"] = round(time.time() - t0, 1)
    conn.close()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--max-files", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backfill-tool-times", action="store_true",
                    help="fill result_ts/duration_s/time_class on rows ingested before them")
    ap.add_argument("--project", help="limit --backfill-tool-times to one project")
    a = ap.parse_args()
    if a.backfill_tool_times:
        conn = connect(a.db)
        stats = backfill_tool_times(conn, discover(ROOTS), a.project)
        conn.close()
        print(json.dumps(stats) if a.json else stats)
        return 0
    stats = run(a.db, max_files=a.max_files)
    print(json.dumps(stats) if a.json else stats)
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
