"""opencode: SQLite store (WAL) at <xdg_data>/opencode/opencode*.db, or the legacy JSON tree.

SQLite generation: `session` (id, directory, parent_id), `part` (session_id,
message_id, time_created, JSON `data`). A tool part is
{type: "tool", callID, tool, state: {status, input, output, error, metadata, time}}.
The shell tool is `bash`; its exit code is state.metadata.exit and a non-zero
exit still has status "completed". status "error" with no exit code means the
call never ran to a result (denied, aborted, failed to start): harness.
Legacy generation: storage/session/<project>/<id>.json and
storage/part/<message>/<part>.json, same part shape.

The db is only ever opened mode=ro; a locked db is skipped with a note.
"""
from __future__ import annotations

import datetime
import glob
import pathlib
import json
import os
import sqlite3
import sys
from typing import Optional

from model import SessionRef, Source, ToolCall, extra_roots, unique, xdg_data

SHELL_TOOLS = {"bash", "shell"}


class OpenCode(Source):
    name = "opencode"
    label = "opencode"
    live_hooks = "plugin hooks tool.execute.before / tool.execute.after"

    def roots(self) -> list[str]:
        # opencode resolves paths with the xdg-basedir package: ~/.local/share on every OS
        # unless XDG_DATA_HOME is set. The platform data dir is a fallback.
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        return unique([os.path.join(xdg, "opencode"), os.path.join(xdg_data(), "opencode")] + extra_roots(self.name))

    def sessions(self, since: float = 0.0):
        seen = set()
        for root in self.roots():
            for db in sorted(glob.glob(os.path.join(root, "opencode*.db"))):
                yield from self._db_sessions(db, since, seen)
            yield from self._legacy_sessions(root, since, seen)

    def _db_sessions(self, db, since, seen):
        try:
            con = _ro(db)
            try:
                rows = con.execute("SELECT id, directory, parent_id, time_updated FROM session").fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            print(f"opencode: skipping {db}: {e}", file=sys.stderr)
            return
        for sid, directory, parent, tu in rows:
            mt = (tu or 0) / 1000.0
            if mt < since or sid in seen:
                continue
            seen.add(sid)
            yield SessionRef(self.name, sid, db, mt, cwd=directory or "",
                             extra={"session_row": sid, "parent_id": parent, "subagent": bool(parent)})

    def _legacy_sessions(self, root, since, seen):
        for path in glob.glob(os.path.join(root, "storage", "session", "*", "*.json")):
            try:
                mt = os.path.getmtime(path)
                with open(path, errors="ignore") as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            sid = s.get("id") or os.path.splitext(os.path.basename(path))[0]
            if mt < since or sid in seen:
                continue
            seen.add(sid)
            parent = s.get("parentID")
            yield SessionRef(self.name, sid, path, mt, cwd=s.get("directory", ""),
                             extra={"session_row": sid, "parent_id": parent, "subagent": bool(parent),
                                    "legacy_root": os.path.join(root, "storage")})

    def tool_calls(self, ref: SessionRef):
        if ref.extra.get("legacy_root"):
            parts = _legacy_parts(ref.extra["legacy_root"], ref.id)
        else:
            try:
                con = _ro(ref.path)
                try:
                    rows = con.execute("SELECT data, time_created FROM part WHERE session_id = ? "
                                       "ORDER BY time_created, id", (ref.id,)).fetchall()
                finally:
                    con.close()
            except sqlite3.Error as e:
                print(f"opencode: skipping session {ref.id}: {e}", file=sys.stderr)
                return
            parts = []
            for data, tc in rows:
                try:
                    parts.append((json.loads(data), tc))
                except ValueError:
                    continue
        for p, tc in parts:
            if isinstance(p, dict) and p.get("type") == "tool":
                yield _to_call(ref, p, tc)


def _ro(path: str) -> sqlite3.Connection:
    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=0.5)


def _legacy_parts(storage: str, sid: str):
    out = []
    for mf in glob.glob(os.path.join(storage, "message", sid, "*.json")):
        mid = os.path.splitext(os.path.basename(mf))[0]
        for pf in glob.glob(os.path.join(storage, "part", mid, "*.json")):
            try:
                with open(pf, errors="ignore") as f:
                    p = json.load(f)
            except (OSError, ValueError):
                continue
            out.append((p, ((p.get("state") or {}).get("time") or {}).get("start") or 0))
    out.sort(key=lambda x: x[1])
    return out


def _iso(ms) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    t = datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _to_call(ref: SessionRef, p: dict, time_created) -> ToolCall:
    state = p.get("state") or {}
    status = state.get("status")
    tool = p.get("tool", "")
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    shell = tool in SHELL_TOOLS
    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    ex: Optional[int] = meta.get("exit") if isinstance(meta.get("exit"), int) else None
    ts = _iso((state.get("time") or {}).get("start") or time_created)
    tc = ToolCall(ref.agent, ref.id, p.get("callID") or p.get("id", ""), tool, inp, ts=ts, cwd=ref.cwd,
                  shell=shell, command=str(inp.get("command", "")) if shell else "")
    if status not in ("completed", "error"):
        return tc          # pending/running: never produced a result to judge
    if status == "error":
        tc.output = str(state.get("error", ""))
        tc.is_error = True
        tc.exit_code = ex
        tc.harness = shell and ex is None
        return tc
    out = state.get("output")
    if out is None:
        out = meta.get("output")
    tc.output = out if isinstance(out, str) or out is None else json.dumps(out)
    if shell and ex is not None:
        tc.exit_code = ex
        tc.is_error = ex != 0
    elif not shell:
        tc.is_error = False
    return tc
