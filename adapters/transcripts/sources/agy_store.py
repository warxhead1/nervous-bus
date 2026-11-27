#!/usr/bin/env python3
"""Decode Antigravity (agy) conversation stores into normalized records.

Two stores, very different decodability:

* agy-cli  ``~/.gemini/antigravity-cli/conversations/<id>.db`` -- SQLite whose
  blob columns are plain protobuf (decoded here with a stdlib wire walker).
  ``<id>.pb`` siblings in the same dir are opaque (high entropy, no known
  framing) and surface as ``decodable=False`` conversations.
* agy-ide  ``~/.gemini/antigravity/conversations/<id>.pb`` -- opaque. The only
  readable trace is ``brain/<id>/.system_generated/logs/overview.txt`` (JSONL),
  present for a minority of conversations. ``antigravity-ide`` is a byte
  duplicate of ``antigravity``; ids are deduped across roots.

Pure stdlib. Never raises on a bad blob: the step becomes kind='unknown'.
Command payloads are exposed on Step.command only; ``summary`` never prints
them. Field numbers below are inferred (evidence in the introducing commit).
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

SRC_CLI = "agy-cli"
SRC_IDE = "agy-ide"

# --- inferred protobuf field numbers (steps table) -------------------------
# step_payload (message): 1=step_type 4=status 5=metadata copy, then ONE
# type-specific oneof field (PAYLOAD_FIELD_BY_TYPE).
PAYLOAD_FIELD_BY_TYPE = {
    5: 10, 7: 13, 8: 14, 9: 15, 14: 19, 15: 20, 17: 24, 21: 28, 23: 30,
    25: 34, 31: 40, 33: 42, 98: 111, 101: 114, 127: 143, 132: 140, 138: 154,
}
STEP_USER_INPUT = 14
STEP_PLANNER_RESPONSE = 15
USER_TEXT_FIELD = 2            # inside payload[19]

# metadata message
META_TIMESTAMP = 1             # {1: epoch seconds, 2: nanos}
META_SOURCE = 3                # 4=user 2=model 5=system
META_TOOL_CALL = 4             # {2: name, 3: args JSON, 9: name again}
META_USAGE = 9                 # present on planner-response steps
TOOL_NAME = 2
TOOL_ARGS_JSON = 3
TOOL_NAME_ALT = 9
USAGE_MODEL_ENUM = 1
USAGE_INPUT_TOKENS = 2
USAGE_OUTPUT_TOKENS = 3        # == REASONING + RESPONSE on every sampled row
USAGE_CACHE_READ_CANDIDATE = 5
USAGE_REASONING_TOKENS = 9
USAGE_RESPONSE_TOKENS = 10
PLANNER_PAYLOAD_TOOL_CALL = 7  # inside payload[20]: embedded tool call msgs

# gen_metadata.data: 1 -> {20: repeated {1:key, 2:value}} carries model_enum
GEN_INFO = 1
GEN_KV = 20
GEN_KV_MODEL_ENUM = b"model_enum"

# trajectory_metadata_blob.data: 1 -> {1: workspace file:// URI, 2: URI}
TMB_INFO = 1
TMB_WORKSPACE = 1

STATUS_DONE = 3
SHELL_TOOLS = {"run_command"}
SHELL_ARG = "CommandLine"


class DecodeError(ValueError):
    pass


# --- wire walker -----------------------------------------------------------
def _varint(b: bytes, i: int):
    r = s = 0
    while True:
        if i >= len(b) or s > 63:
            raise DecodeError("varint")
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def parse(b: bytes) -> dict:
    """Top-level wire parse -> {field: [values]} (ints for varint, bytes else)."""
    out: dict = collections.defaultdict(list)
    i, n = 0, len(b)
    while i < n:
        k, i = _varint(b, i)
        f, w = k >> 3, k & 7
        if f == 0:
            raise DecodeError("field0")
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            ln, i = _varint(b, i)
            if i + ln > n:
                raise DecodeError("len")
            v = b[i:i + ln]
            i += ln
        elif w == 1:
            if i + 8 > n:
                raise DecodeError("f64")
            v = b[i:i + 8]
            i += 8
        elif w == 5:
            if i + 4 > n:
                raise DecodeError("f32")
            v = b[i:i + 4]
            i += 4
        else:
            raise DecodeError("wiretype")
        out[f].append(v)
    return out


def _first(d: dict, f: int, default=None):
    v = d.get(f)
    return v[0] if v else default


def _ts(msg: Optional[bytes]) -> Optional[str]:
    if not msg:
        return None
    try:
        d = parse(msg)
        sec = _first(d, 1)
        if not isinstance(sec, int) or not (0 < sec < 4_102_444_800):
            return None
        return _dt.datetime.fromtimestamp(sec, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (DecodeError, OverflowError, OSError, ValueError):
        return None


# --- records ---------------------------------------------------------------
@dataclass
class Conversation:
    id: str
    source: str
    path: str
    workspace: Optional[str] = None
    started: Optional[str] = None
    ended: Optional[str] = None
    model: Optional[str] = None
    decodable: bool = True
    kind_of_store: str = "sqlite"   # 'sqlite' | 'overview' | 'opaque'
    overview_path: Optional[str] = None


@dataclass
class Step:
    idx: int
    kind: str                       # user|model|tool_call|other|unknown
    step_type: Optional[int] = None
    tool: Optional[str] = None
    command: Optional[str] = None
    status: Optional[object] = None  # raw int (sqlite) or str (overview)
    error: bool = False
    timestamp: Optional[str] = None
    tokens: Optional[dict] = None
    n_tool_calls: int = 0           # tool calls embedded in a model step
    payload_bytes: int = 0
    text: Optional[str] = None      # only with with_text=True


# --- roots / conversations -------------------------------------------------
def default_roots() -> list[str]:
    g = os.path.expanduser("~/.gemini")
    return [f"{g}/antigravity-cli", f"{g}/antigravity", f"{g}/antigravity-ide"]


def _source_for_root(root: str) -> str:
    return SRC_CLI if os.path.basename(root.rstrip("/")) == "antigravity-cli" else SRC_IDE


def _ro(path: str) -> sqlite3.Connection:
    # mode=ro, NOT immutable: the -wal sidecars are live.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _history(root: str) -> dict:
    ws = {}
    try:
        with open(os.path.join(root, "history.jsonl"), errors="replace") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                cid = j.get("conversationId")
                if cid and j.get("workspace"):
                    ws.setdefault(cid, j["workspace"])
    except OSError:
        pass
    return ws


def _uri_to_path(u: bytes) -> Optional[str]:
    try:
        s = u.decode()
    except UnicodeDecodeError:
        return None
    return s[len("file://"):] if s.startswith("file://") else None


def _fill_cli_db(conv: Conversation) -> None:
    try:
        con = _ro(conv.path)
        try:
            ts = [t for (m,) in con.execute("SELECT metadata FROM steps") if m
                  for t in [_ts(_first(_safe_parse(m), META_TIMESTAMP))] if t]
            if ts:
                conv.started, conv.ended = min(ts), max(ts)
            row = con.execute("SELECT data FROM trajectory_metadata_blob LIMIT 1").fetchone()
            if row and row[0]:
                info = _first(_safe_parse(row[0]), TMB_INFO)
                if info:
                    uri = _first(_safe_parse(info), TMB_WORKSPACE)
                    if uri:
                        conv.workspace = _uri_to_path(uri)
            g = con.execute("SELECT data FROM gen_metadata ORDER BY idx LIMIT 1").fetchone()
            if g and g[0]:
                conv.model = _model_from_gen(g[0])
        finally:
            con.close()
    except sqlite3.Error:
        conv.decodable = False


def _safe_parse(b: bytes) -> dict:
    try:
        return parse(b)
    except DecodeError:
        return {}


def _model_from_gen(blob: bytes) -> Optional[str]:
    info = _first(_safe_parse(blob), GEN_INFO)
    if not info:
        return None
    for kv in _safe_parse(info).get(GEN_KV, []):
        d = _safe_parse(kv)
        if _first(d, 1) == GEN_KV_MODEL_ENUM:
            v = _first(d, 2)
            return v.decode("utf8", "replace") if isinstance(v, bytes) else None
    return None


def _iso_mtime(path: str) -> Optional[str]:
    try:
        return _dt.datetime.fromtimestamp(os.path.getmtime(path), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return None


def iter_conversations(roots: Optional[Iterable[str]] = None) -> Iterator[Conversation]:
    """Yield one Conversation per distinct id across roots (first root wins)."""
    seen: set = set()
    for root in (list(roots) if roots is not None else default_roots()):
        cdir = os.path.join(root, "conversations")
        if not os.path.isdir(cdir):
            continue
        src = _source_for_root(root)
        hist = _history(root)
        for name in sorted(os.listdir(cdir)):
            stem, ext = os.path.splitext(name)
            if ext not in (".db", ".pb") or stem in seen:
                continue
            path = os.path.join(cdir, name)
            if ext == ".db" and src == SRC_CLI:
                conv = Conversation(stem, src, path, workspace=hist.get(stem))
                _fill_cli_db(conv)
                if hist.get(stem) and not conv.workspace:
                    conv.workspace = hist[stem]
            else:
                ov = os.path.join(root, "brain", stem, ".system_generated", "logs", "overview.txt")
                if src == SRC_IDE and os.path.isfile(ov):
                    conv = Conversation(stem, src, path, workspace=hist.get(stem),
                                        kind_of_store="overview", overview_path=ov)
                    ts = [s.timestamp for s in _overview_steps(ov) if s.timestamp]
                    if ts:
                        conv.started, conv.ended = min(ts), max(ts)
                else:
                    conv = Conversation(stem, src, path, workspace=hist.get(stem),
                                        decodable=False, kind_of_store="opaque",
                                        ended=_iso_mtime(path))
            seen.add(stem)
            yield conv


# --- steps -----------------------------------------------------------------
def _json_args(raw) -> dict:
    try:
        j = json.loads(raw)
        return j if isinstance(j, dict) else {}
    except (ValueError, TypeError):
        return {}


def _decode_usage(msg: bytes) -> Optional[dict]:
    d = _safe_parse(msg)
    if not d:
        return None
    g = lambda f: _first(d, f)  # noqa: E731
    me = g(USAGE_MODEL_ENUM)
    return {
        "model_enum": me if isinstance(me, int) else None,
        "input": g(USAGE_INPUT_TOKENS) or 0,
        "output": g(USAGE_OUTPUT_TOKENS) or 0,
        "reasoning": g(USAGE_REASONING_TOKENS) or 0,
        "response": g(USAGE_RESPONSE_TOKENS) or 0,
        "cache_read": g(USAGE_CACHE_READ_CANDIDATE) or 0,
    }


def _decode_row(idx, step_type, status, meta, err, payload, with_text) -> Step:
    st = Step(idx=idx, kind="unknown", step_type=step_type, status=status,
              error=bool(err))
    try:
        md = parse(meta) if meta else {}
        st.timestamp = _ts(_first(md, META_TIMESTAMP))
        pf = PAYLOAD_FIELD_BY_TYPE.get(step_type)
        if payload and pf:
            pd = parse(payload)
            body = _first(pd, pf)
            st.payload_bytes = len(body) if isinstance(body, bytes) else 0
        else:
            pd, body = {}, None
        tc = _first(md, META_TOOL_CALL)
        if step_type == STEP_USER_INPUT:
            st.kind = "user"
            if with_text and isinstance(body, bytes):
                t = _first(parse(body), USER_TEXT_FIELD)
                if isinstance(t, bytes):
                    st.text = t.decode("utf8", "replace")
        elif step_type == STEP_PLANNER_RESPONSE:
            st.kind = "model"
            u = _first(md, META_USAGE)
            if u:
                st.tokens = _decode_usage(u)
            if isinstance(body, bytes):
                st.n_tool_calls = len(parse(body).get(PLANNER_PAYLOAD_TOOL_CALL, []))
        elif tc:
            td = parse(tc)
            name = _first(td, TOOL_NAME) or _first(td, TOOL_NAME_ALT)
            st.kind = "tool_call"
            st.tool = name.decode("utf8", "replace") if isinstance(name, bytes) else None
            if st.tool in SHELL_TOOLS:
                cmd = _json_args(_first(td, TOOL_ARGS_JSON)).get(SHELL_ARG)
                st.command = cmd if isinstance(cmd, str) else None
        else:
            st.kind = "other"
    except (DecodeError, ValueError, TypeError):
        st.kind = "unknown"
    return st


def _overview_steps(path: str) -> Iterator[Step]:
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                j = json.loads(line)
                idx = int(j["step_index"])
            except (ValueError, KeyError, TypeError):
                yield Step(idx=-1, kind="unknown")
                continue
            typ = str(j.get("type") or "")
            ts = j.get("created_at")
            status = j.get("status")
            calls = [c for c in (j.get("tool_calls") or []) if isinstance(c, dict)]
            if typ == "USER_INPUT":
                kind = "user"
            elif typ == "PLANNER_RESPONSE":
                kind = "model"
            else:
                kind = "other"
            yield Step(idx=idx, kind=kind, tool=None if kind != "other" else typ.lower() or None,
                       status=status, timestamp=ts if isinstance(ts, str) else None,
                       n_tool_calls=len(calls))
            for c in calls:
                args = c.get("args") if isinstance(c.get("args"), dict) else {}
                name = c.get("name") if isinstance(c.get("name"), str) else None
                cmd = args.get(SHELL_ARG) if name in SHELL_TOOLS else None
                yield Step(idx=idx, kind="tool_call", tool=name,
                           command=cmd if isinstance(cmd, str) else None,
                           status=status, timestamp=ts if isinstance(ts, str) else None)


def iter_steps(conv: Conversation, with_text: bool = False) -> Iterator[Step]:
    """Yield Steps. Tool results are inline in the tool row (no result rows)."""
    if conv.kind_of_store == "overview" and conv.overview_path:
        yield from _overview_steps(conv.overview_path)
        return
    if not conv.decodable or conv.kind_of_store != "sqlite":
        return
    try:
        con = _ro(conv.path)
    except sqlite3.Error:
        return
    try:
        cur = con.execute("SELECT idx, step_type, status, metadata, error_details, "
                          "step_payload FROM steps ORDER BY idx")
        for idx, stype, status, meta, err, payload in cur:
            yield _decode_row(idx, stype, status, meta, err, payload, with_text)
    except sqlite3.Error:
        return
    finally:
        con.close()


def tool_histogram(conv: Conversation) -> dict:
    """{tool_name: count} over tool_call steps (what segment.py stamps)."""
    h: collections.Counter = collections.Counter()
    for s in iter_steps(conv):
        if s.kind == "tool_call" and s.tool:
            h[s.tool] += 1
    return dict(h)


# --- summary ---------------------------------------------------------------
def summarize(roots: Optional[Iterable[str]] = None, top: int = 15) -> dict:
    out: dict = {}
    for conv in iter_conversations(roots):
        b = out.setdefault(conv.source, {
            "conversations": 0, "decodable_conversations": 0, "opaque_conversations": 0,
            "steps": 0, "steps_by_kind": collections.Counter(),
            "tools": collections.Counter(), "errors": 0,
            "tokens": collections.Counter(), "models": collections.Counter(),
            "workspace_known": 0,
        })
        b["conversations"] += 1
        b["workspace_known"] += bool(conv.workspace)
        if conv.model:
            b["models"][conv.model] += 1
        if not conv.decodable or conv.kind_of_store == "opaque":
            b["opaque_conversations"] += 1
            continue
        b["decodable_conversations"] += 1
        for s in iter_steps(conv):
            b["steps"] += 1
            b["steps_by_kind"][s.kind] += 1
            b["errors"] += s.error
            if s.kind == "tool_call" and s.tool:
                b["tools"][s.tool] += 1
            if s.tokens:
                for k in ("input", "output", "reasoning", "response", "cache_read"):
                    b["tokens"][k] += s.tokens.get(k) or 0
                b["tokens"]["model_steps_with_usage"] += 1
    for b in out.values():
        n = b["steps"]
        unknown = b["steps_by_kind"].get("unknown", 0)
        b["steps_decoded_pct"] = round(100.0 * (n - unknown) / n, 2) if n else None
        b["steps_by_kind"] = dict(b["steps_by_kind"])
        b["top_tools"] = [{"tool": t, "count": c} for t, c in b["tools"].most_common(top)]
        del b["tools"]
        b["tokens"] = dict(b["tokens"]) or None
        b["models"] = dict(b["models"].most_common(8))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary", help="per-source counts (no content values)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--root", action="append", help="repeatable; default ~/.gemini/{antigravity-cli,antigravity,antigravity-ide}")
    a = ap.parse_args(argv)
    res = summarize(a.root)
    if a.json:
        json.dump(res, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        for src, b in sorted(res.items()):
            print(f"{src}: {b['conversations']} convs ({b['opaque_conversations']} opaque), "
                  f"{b['steps']} steps, decoded {b['steps_decoded_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
