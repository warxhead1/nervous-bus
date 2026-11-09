#!/usr/bin/env python3
"""token_report.py — rank the biggest token waste over a window of tokens.db.

Reads the ledger built by token_ledger.py and emits a digest of WASTE
CANDIDATES, each with an estimated cost in input-equivalent tokens (IET:
input 1, cache read 0.1, cache write 5m 1.25, cache write 1h 2, output 5 —
API price ratios, a proxy for subscription-limit burn, not a bill).

The digest is sanitized BY CONSTRUCTION so it may leave the box (the mmx
daily auditor reads it): it carries counts, token sums, model ids, project
names, agent types, tool names, the first word pair of test commands
("cargo test", "pytest"), and home-relative paths of instruction files
(CLAUDE.md / memory). Never prompt text, tool output, full commands, or the
paths of files agents read.

Two time sections sit beside the token candidates. `lanes` gives one row per
subagent: turns, wall, model vs tool time, turns before the first edit,
context at the first and last request, and tool time by class. `tool_time`
gives tool time by class (wait / loop / gate / test / build / ... from
token_ledger.time_class). A lane is named by its agent id, type and model. Its
dispatch DESCRIPTION is prompt text, so it is NOT in the digest; `--lanes`
prints a local table that carries it.

The plan bills by REQUESTS per 5h window, not tokens, so a `requests` section
beside the token candidates counts the unit that is billed. It carries the
request shape (no tool / one tool / parallel), waste ranked in requests
(`unbatched_reads`, `denied_calls`, `redundant_reruns`), and requests per agent
type x requested tier, per project, per lane and per commit. Two of those are
estimates and say so: `unbatched_reads` is an UPPER bound (a later read can
depend on the earlier result) and `denied_calls` only sees transcripts that
still exist (read through adapters/transcripts, ToolCall.harness). Request
views cover claude and mmx, whose requests carry tool calls in the ledger.

    token_report.py [--db PATH] [--days 7] [--json | --md] [--out FILE]
    token_report.py --lanes [--project P] [--days 7]      (local view, not sanitized)
    token_report.py --requests [--project P] [--days 7] [--md]   (request view only)
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from token_ledger import DEFAULT_DB, ROOTS, time_class  # noqa: E402
from transcript_steps import parse_ts  # noqa: E402

W_IN, W_READ, W_5M, W_1H, W_OUT = 1.0, 0.1, 1.25, 2.0, 5.0
IET = (f"(input*{W_IN} + cache_read*{W_READ} + cache_5m*{W_5M} + cache_1h*{W_1H}"
       f" + (cache_create-cache_5m-cache_1h)*{W_5M} + output*{W_OUT})")
PARITY_SOURCES = ("codex", "gemini")
EDIT_TOOLS_SQL = ("'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch', 'replace', 'write_file'")
CHARS_PER_TOKEN = 4
BIG_RESULT_CHARS = 20_000
LONG_CTX = 100_000
LONG_CALL_S = 60.0
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "replace", "write_file"}
PROPOSALS = DEFAULT_DB.parent / "token-digest" / "proposals.jsonl"

# Sources billed per request whose requests carry tool calls in the ledger.
REQUEST_SOURCES = ("claude", "mmx")
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "NotebookRead"})
CHAIN_BUCKETS = ((2, 2, "2"), (3, 3, "3"), (4, 5, "4-5"), (6, 10, "6-10"), (11, None, "11+"))
TRANSCRIPTS_DIR = Path(__file__).resolve().parent.parent / "transcripts"
# Denial text -> a fixed family. The digest carries the family, never the text.
DENIAL_CLASSES = (
    ("worktree_guard", re.compile(r"isolated in the worktree")),
    ("blocked_sleep", re.compile(r"Blocked: sleep")),
    ("hook_blocked", re.compile(r"PreToolUse:\w+ hook error|^Hook PreToolUse")),
    ("permission_denied", re.compile(r"^(?:Permission (?:for|to use)|The user doesn't want to proceed)")),
    ("tool_unavailable", re.compile(r"No such tool available")),
    ("output_lost", re.compile(r"\[Tool call interrupted|Command output was lost")),
    ("tool_use_error", re.compile(r"<tool_use_error>")),
)


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _rows(conn, sql, params=()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _cmd_head(cmd: str) -> str:
    """'cd x && cargo test -p y' -> 'cargo test' (sanitized command family)."""
    seg = re.split(r"&&|\|\||[;|()]", cmd)
    for s in seg:
        words = [w for w in s.split() if re.fullmatch(r"[A-Za-z][\w.:+-]*", w)]
        while words and words[0] in ("timeout", "nice", "env", "time", "sudo", "exec"):
            words = words[1:]
        if words and words[0] not in ("cd", "export", "set", "echo"):
            return " ".join(words[:2])[:40]
    return "?"


def _has_tool_times(conn) -> bool:
    return "duration_s" in {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")}


def _union_s(intervals: list[tuple[float, float]]) -> float:
    total, lo, hi = 0.0, None, None
    for a, b in sorted(intervals):
        if hi is None or a > hi:
            if hi is not None:
                total += hi - lo
            lo, hi = a, b
        else:
            hi = max(hi, b)
    return total + (hi - lo if hi is not None else 0.0)


def classify_denial(output: Optional[str]) -> str:
    for name, rx in DENIAL_CLASSES:
        if rx.search(output or ""):
            return name
    return "other"


def _claude_source():
    """(Claude source, SessionRef) from adapters/transcripts, or None when it
    cannot be imported. Appended, not prepended: its generic module names
    (`model`, `sources`) must never shadow anything already importable."""
    d = str(TRANSCRIPTS_DIR)
    if d not in sys.path:
        sys.path.append(d)
    try:
        from model import SessionRef
        from sources.claude import Claude
    except Exception:
        return None
    return Claude(), SessionRef


def scan_denied(conn: sqlite3.Connection, since: str, until: Optional[str] = None,
                project: Optional[str] = None, roots=None) -> dict:
    """Harness-denied or blocked tool calls in the window, read from the
    transcripts through adapters/transcripts (ToolCall.harness). The ledger does
    not record a call's outcome, so this re-reads the transcripts of the claude
    and mmx sessions active in the window. The scan is one section of the digest
    and must never take the digest down: a transcript that is gone from every
    root is counted in `missing`; one that is there but cannot be read (EACCES,
    EIO, deleted between the exists() check and the open) in `unreadable`, and
    contributes nothing, not even the calls read before the error.

    `calls` counts distinct, dated calls: the ledger keeps one row per call id and
    dates it from the assistant record that issued it, while Claude transcripts
    also replay tool_use/tool_result in timestamp-less `api-request-blob` records.
    A replay is the same call again and an undated record cannot be placed in the
    window, so neither is counted."""
    roots = ROOTS if roots is None else roots
    loaded = _claude_source()
    out = {"available": loaded is not None, "sessions": 0, "scanned": 0, "missing": 0,
           "unreadable": 0, "calls": 0, "denied": {}, "meta": {}}
    if loaded is None:
        return out
    src, session_ref = loaded
    ph = ",".join("?" * len(REQUEST_SOURCES))
    sql = (f"SELECT key, source, project, is_subagent, agent_type FROM sessions"
           f" WHERE source IN ({ph}) AND COALESCE(ended, started) >= ?")
    params: list = [*REQUEST_SOURCES, since]
    if until:
        sql += " AND started < ?"
        params.append(until)
    if project:
        sql += " AND project = ?"
        params.append(project)
    seen: set = set()
    for key, source, proj, is_sub, atype in conn.execute(sql, params).fetchall():
        out["sessions"] += 1
        rel = key.partition(":")[2]
        path, found = None, {}
        try:
            path = (next((root / rel for s, root in roots if s == source and (root / rel).exists()), None)
                    if rel else None)
            if path is not None:
                for tc in src.tool_calls(session_ref("claude", path.stem, str(path), 0.0)):
                    if not tc.ts or tc.ts < since or (until and tc.ts >= until):
                        continue
                    found.setdefault(tc.call_id or f"{key}:{tc.ts}:{tc.tool}",
                                     (tc.tool, classify_denial(tc.output) if tc.harness else None))
        except OSError:
            # dropped whole: a half-read session would skew the denied share
            out["unreadable"] += 1
            continue
        if path is None:
            out["missing"] += 1
            continue
        out["scanned"] += 1
        out["meta"][key] = (proj, (atype or "subagent?") if is_sub else f"main:{source}")
        for cid, (tool, cls) in found.items():
            if cid in seen:
                continue
            seen.add(cid)
            out["calls"] += 1
            if cls:
                out["denied"][cid] = (key, tool, cls)
    return out


def _load_requests(conn: sqlite3.Connection, since: str, until: Optional[str],
                   project: Optional[str]) -> tuple[dict, dict, dict]:
    """Requests of the window per session (seq order), each with the tool calls
    it issued. A call belongs to the last request at or before its timestamp;
    that agrees with the recorded n_tool_uses on every request of a 197-session
    sample, and a request where it does not is never called read-only."""
    ph = ",".join("?" * len(REQUEST_SOURCES))
    scope = f" AND s.source IN ({ph})" + (" AND s.project = ?" if project else "")
    tail = [*REQUEST_SOURCES] + ([project] if project else [])
    rsql = ("SELECT r.session_key, r.seq, r.ts, r.n_tool_uses, r.input, r.cache_read, r.output,"
            " s.source, s.project, s.is_subagent, s.agent_type, s.agent_model"
            " FROM requests r JOIN sessions s ON s.key = r.session_key WHERE r.ts >= ?"
            + (" AND r.ts < ?" if until else "") + scope + " ORDER BY r.session_key, r.seq")
    by_sess: dict[str, list[dict]] = {}
    for (sk, seq, ts, nuse, inp, cr, out, source, proj, is_sub, atype, amodel) in conn.execute(
            rsql, [since] + ([until] if until else []) + tail):
        by_sess.setdefault(sk, []).append({
            "sk": sk, "seq": seq, "ts": ts, "n_uses": nuse, "source": source, "project": proj,
            "agent": (atype or "subagent?") if is_sub else f"main:{source}",
            "tier": amodel if is_sub else None, "calls": [],
            # the turn itself: prefix read + generation. Cache writes are left out:
            # the result they carry would be written by the batched request anyway.
            "turn_iet": inp * W_IN + cr * W_READ + out * W_OUT})
    stamps = {sk: [r["ts"] for r in lst] for sk, lst in by_sess.items()}
    csql = ("SELECT t.session_key, t.ts, t.id, t.tool, t.time_class, t.cmd_class,"
            " CASE WHEN t.time_class IS NULL THEN t.cmd END"
            " FROM tool_calls t JOIN sessions s ON s.key = t.session_key WHERE t.ts >= ?"
            + (" AND t.ts < ?" if until else "") + scope + " ORDER BY t.session_key, t.ts, t.rowid")
    call_req: dict[str, dict] = {}
    seq_calls: dict[str, list] = {}
    for sk, ts, cid, tool, tclass, cmd_class, cmd in conn.execute(
            csql, [since] + ([until] if until else []) + tail):
        reqs = by_sess.get(sk)
        j = bisect.bisect_right(stamps[sk], ts) - 1 if reqs else -1
        if j < 0:
            continue
        if tclass is None and tool == "Bash":
            tclass = time_class("Bash", cmd or "")
        is_read = tool in READ_TOOLS or (tool == "Bash" and tclass == "read")
        reqs[j]["calls"].append((cid, tool, is_read, cmd_class))
        call_req[cid] = reqs[j]
        seq_calls.setdefault(sk, []).append((is_read, j))
    return by_sess, call_req, seq_calls


def _read_only(rec: dict) -> bool:
    c = rec["calls"]
    return bool(c) and len(c) == rec["n_uses"] and all(x[2] for x in c)


def _share(n: int, d: int) -> Optional[float]:
    return round(n / d, 3) if d else None


def request_view(conn: sqlite3.Connection, since: str, until: Optional[str] = None,
                 project: Optional[str] = None, roots=None,
                 rerun_ids=frozenset(), scan: bool = True) -> tuple[dict, dict]:
    """The window in the unit the plan bills: requests.

    Returns (view, by_session). The view is digest-safe (counts, project names,
    agent types, tiers, fixed denial families). by_session maps a ledger session
    key to its unbatched-read and denied counts for the lanes table, and is NOT
    digest-safe (the key embeds a transcript path); callers must not emit it.

      unbatched_reads  read-only requests whose previous request in the session
                       was also read-only. UPPER bound on requests one parallel
                       call could have absorbed: a later read may need the
                       earlier result, and a user message between two requests
                       is invisible here.
      denied_calls     requests whose every call the harness denied or blocked.
      redundant_reruns requests whose every call re-ran a test/build unchanged.
    """
    by_sess, call_req, seq_calls = _load_requests(conn, since, until, project)
    recs = [r for lst in by_sess.values() for r in lst]

    # read-only chains: one request per extra link is the upper-bound saving
    runs: dict[int, int] = {}
    for lst in by_sess.values():
        run, prev = 0, None
        for r in lst:
            if _read_only(r):
                if run and prev["seq"] + 1 == r["seq"]:
                    run += 1
                    r["absorbable"] = True
                else:
                    if run > 1:
                        runs[run] = runs.get(run, 0) + 1
                    run = 1
            else:
                if run > 1:
                    runs[run] = runs.get(run, 0) + 1
                run = 0
            prev = r
        if run > 1:
            runs[run] = runs.get(run, 0) + 1
    # the same chain counted per CALL, as the issue measured it
    calls_after_read = cross_request = read_calls = n_calls = 0
    for lst in seq_calls.values():
        for i, (is_read, j) in enumerate(lst):
            n_calls += 1
            read_calls += is_read
            if i and is_read and lst[i - 1][0]:
                calls_after_read += 1
                cross_request += j != lst[i - 1][1]

    sc = scan_denied(conn, since, until, project, roots) if scan else None
    denied = sc["denied"] if sc else {}
    for cid in denied:
        rec = call_req.get(cid)
        if rec is not None:
            rec["denied_n"] = rec.get("denied_n", 0) + 1
    for r in recs:
        calls = r["calls"]
        full = bool(calls) and len(calls) == r["n_uses"]
        r["all_denied"] = full and all(c[0] in denied for c in calls)
        r["all_rerun"] = full and all(c[0] in rerun_ids for c in calls)

    def agg(keyfn) -> list[dict]:
        acc: dict = {}
        for r in recs:
            a = acc.setdefault(keyfn(r), {"sessions": set(), "requests": 0, "one": 0, "commits": 0,
                                           "unbatched": 0, "denied": 0})
            a["sessions"].add(r["sk"])
            a["requests"] += 1
            a["one"] += r["n_uses"] == 1
            a["commits"] += sum(1 for c in r["calls"] if c[3] == "commit")
            a["unbatched"] += bool(r.get("absorbable"))
            a["denied"] += r["all_denied"]
        rows = []
        for k, a in acc.items():
            rows.append({"key": k, "sessions": len(a["sessions"]), "requests": a["requests"],
                         "requests_per_session": round(a["requests"] / len(a["sessions"]), 1),
                         "single_call_share": _share(a["one"], a["requests"]),
                         "commits": a["commits"],
                         "requests_per_commit": round(a["requests"] / a["commits"], 1) if a["commits"] else None,
                         "unbatched_read_requests": a["unbatched"], "denied_requests": a["denied"]})
        rows.sort(key=lambda x: -x["requests"])
        return rows

    def named(rows, **names) -> list[dict]:
        return [{**{n: (row["key"][i] if isinstance(row["key"], tuple) else row["key"])
                    for i, n in enumerate(names)},
                 **{k: v for k, v in row.items() if k != "key"}} for row in rows]

    all_agent_tier = named(agg(lambda r: (r["agent"], r["tier"])), agent=0, requested_tier=1)
    all_agent = named(agg(lambda r: r["agent"]), agent=0)
    all_project = named(agg(lambda r: r["project"]), project=0)
    by_agent, by_project = all_agent_tier[:15], all_project[:15]
    denied_per_project: dict = {}
    for _, (key, _, _) in denied.items():
        p = sc["meta"].get(key, (None, None))[0]
        denied_per_project[p] = denied_per_project.get(p, 0) + 1
    for row in by_project:
        row["denied_calls"] = denied_per_project.get(row["project"], 0) if sc else None

    total = len(recs)
    shape = {}
    for r in recs:
        s = shape.setdefault(r["source"], {"source": r["source"], "requests": 0, "no_tool": 0,
                                            "one_tool": 0, "multi_tool": 0,
                                            "unbatched_read_requests": 0, "denied_requests": 0})
        s["requests"] += 1
        s["no_tool" if r["n_uses"] == 0 else "one_tool" if r["n_uses"] == 1 else "multi_tool"] += 1
        s["unbatched_read_requests"] += bool(r.get("absorbable"))
        s["denied_requests"] += r["all_denied"]
    by_source = sorted(shape.values(), key=lambda s: -s["requests"])
    for s in by_source:
        s["single_call_share"] = _share(s["one_tool"], s["requests"])

    absorbable = [r for r in recs if r.get("absorbable")]
    unb_req = len(absorbable)
    den_recs = [r for r in recs if r["all_denied"]]
    rerun_recs = [r for r in recs if r["all_rerun"]]
    chain_rows = []
    for lo, hi, label in CHAIN_BUCKETS:
        inside = {n: c for n, c in runs.items() if n >= lo and (hi is None or n <= hi)}
        chain_rows.append({"requests_in_run": label, "runs": sum(inside.values()),
                           "absorbable_requests": sum((n - 1) * c for n, c in inside.items())})

    def top(rows, key) -> list[dict]:
        rows = sorted((r for r in rows if r["unbatched_read_requests"]),
                      key=lambda r: -r["unbatched_read_requests"])[:10]
        return [{key: r[key], "requests": r["requests"],
                 "unbatched_read_requests": r["unbatched_read_requests"]} for r in rows]

    den_by_class: dict[str, dict] = {}
    den_by_tool: dict[str, int] = {}
    den_by_agent: dict[str, int] = {}
    for cid, (key, tool, cls) in denied.items():
        e = den_by_class.setdefault(cls, {"class": cls, "calls": 0, "requests_all_denied": 0})
        e["calls"] += 1
        den_by_tool[tool] = den_by_tool.get(tool, 0) + 1
        a = sc["meta"].get(key, (None, "?"))[1]
        den_by_agent[a] = den_by_agent.get(a, 0) + 1
    for r in den_recs:
        # a request is filed under the family of its first denied call
        den_by_class[denied[r["calls"][0][0]][2]]["requests_all_denied"] += 1
    unb = {"requests": unb_req, "iet": int(sum(r["turn_iet"] for r in absorbable)),
           "read_calls": read_calls, "calls": n_calls,
           "read_calls_after_read": calls_after_read,
           "of_which_in_another_request": cross_request,
           "chain_lengths": chain_rows,
           "by_project": top(all_project, "project"),
           "by_agent": top(all_agent, "agent")}
    den = {"available": bool(sc and sc["available"]),
           "requests_all_denied": len(den_recs), "iet": int(sum(r["turn_iet"] for r in den_recs)),
           "requests_some_denied": sum(1 for r in recs if r.get("denied_n")),
           "calls": len(denied), "calls_matched_to_requests": sum(1 for c in denied if c in call_req),
           "scan": ({k: sc[k] for k in ("sessions", "scanned", "missing", "unreadable", "calls")} if sc else None),
           "by_class": sorted(den_by_class.values(), key=lambda e: -e["calls"]),
           "by_tool": [{"tool": t, "calls": n} for t, n in sorted(den_by_tool.items(), key=lambda kv: -kv[1])[:8]],
           "by_agent": [{"agent": a, "calls": n} for a, n in sorted(den_by_agent.items(), key=lambda kv: -kv[1])[:10]],
           "by_project": [{"project": p, "calls": n} for p, n in
                          sorted(denied_per_project.items(), key=lambda kv: -kv[1])[:10]]}
    rerun_iet = int(sum(r["turn_iet"] for r in rerun_recs))
    waste = [
        {"id": "unbatched_reads", "requests": unb_req, "turn_iet": unb["iet"],
         "bound": "upper: a later read may need the earlier result"},
        {"id": "denied_calls", "requests": len(den_recs), "turn_iet": den["iet"],
         "bound": ("transcripts still on disk" if den["available"] else "not measured: transcripts unreadable")},
        {"id": "redundant_reruns", "requests": len(rerun_recs), "turn_iet": rerun_iet,
         "bound": "requests that held only reruns; a rerun beside other calls is not counted"},
    ]
    for w in waste:
        w["share_of_requests"] = _share(w["requests"], total)
    waste.sort(key=lambda w: -w["requests"])
    commits = sum(1 for r in recs for c in r["calls"] if c[3] == "commit")
    view = {"sources": list(REQUEST_SOURCES), "since": since, "until": until,
            "totals": {"requests": total, "sessions": len(by_sess),
                       "single_call_share": _share(sum(s["one_tool"] for s in by_source), total),
                       "commits": commits,
                       "requests_per_commit": round(total / commits, 1) if commits else None},
            "by_source": by_source, "waste": waste,
            "unbatched_reads": unb, "denied_calls": den,
            "by_agent_tier": by_agent, "by_project": by_project}
    by_session: dict[str, dict] = {}
    for r in recs:
        e = by_session.setdefault(r["sk"], {"unbatched_read_requests": 0, "denied_calls": 0})
        e["unbatched_read_requests"] += bool(r.get("absorbable"))
    for cid, (key, _, _) in denied.items():
        by_session.setdefault(key, {"unbatched_read_requests": 0, "denied_calls": 0})["denied_calls"] += 1
    return view, by_session


def lanes(conn: sqlite3.Connection, since: str, project: Optional[str] = None,
          with_description: bool = False, per_session: Optional[dict] = None) -> list[dict]:
    """One row per subagent session started in the window: where its wall time
    went. model_s = wall - union(tool intervals): generation, thinking and API
    latency between calls. turns_to_first_edit counts requests before the first
    edit-tool call (None when the lane never edited). `turns` is the lane's
    request count; agent_model is the tier the dispatch asked for. per_session
    (from request_view) adds unbatched-read and denied-call counts."""
    timed = _has_tool_times(conn)
    sql = ("SELECT key, project, agent_id, agent_type, agent_model, description, started, ended"
           " FROM sessions WHERE is_subagent = 1 AND started >= ?")
    params: list = [since]
    if project:
        sql += " AND project = ?"
        params.append(project)
    sessions = _rows(conn, sql, params)
    # two batched reads for the whole window (one query per session is ~40 s host-wide)
    scope = sql.replace("SELECT key, project, agent_id, agent_type, agent_model, description,"
                        " started, ended", "SELECT key")
    reqs_by: dict[str, list] = {}
    for key, *rest in conn.execute(
            "SELECT session_key, ts, model, input + cache_read + cache_create, n_tool_uses FROM requests"
            f" WHERE session_key IN ({scope}) ORDER BY session_key, seq", params):
        reqs_by.setdefault(key, []).append(tuple(rest))
    tcols = "ts, tool, time_class, duration_s" if timed else "ts, tool, NULL, NULL"
    calls_by: dict[str, list] = {}
    for key, *rest in conn.execute(
            f"SELECT session_key, {tcols} FROM tool_calls"
            f" WHERE session_key IN ({scope}) ORDER BY session_key, ts", params):
        calls_by.setdefault(key, []).append(tuple(rest))
    out = []
    for s in sessions:
        reqs = reqs_by.get(s["key"], [])
        calls = calls_by.get(s["key"], [])
        t0, t1 = parse_ts(s["started"]), parse_ts(s["ended"])
        wall = max(0.0, t1 - t0) if t0 is not None and t1 is not None else 0.0
        ivals, by_class, long_n, long_s, untimed, commits = [], {}, 0, 0.0, 0, 0
        for ts, tool, cls, dur in calls:
            commits += cls == "commit"
            start = parse_ts(ts)
            if dur is None or start is None:
                untimed += 1
                continue
            ivals.append((start, start + dur))
            e = by_class.setdefault(cls or tool, [0, 0.0])
            e[0] += 1
            e[1] += dur
            if dur > LONG_CALL_S:
                long_n, long_s = long_n + 1, long_s + dur
        tool_s = min(wall, _union_s(ivals)) if wall else _union_s(ivals)
        first_edit = next((ts for ts, tool, _, _ in calls if tool in EDIT_TOOLS), None)
        # no call has a duration yet (rows from before the columns, not backfilled):
        # report the split as unknown, never as "all model time"
        unknown = bool(calls) and untimed == len(calls)
        models: dict[str, int] = {}
        for _, m, _, _ in reqs:
            models[m or "?"] = models.get(m or "?", 0) + 1
        row = {
            "lane": (s["agent_id"] or s["key"].rsplit("/", 1)[-1])[:17],
            "project": s["project"], "agent_type": s["agent_type"],
            "agent_model": s["agent_model"],
            "model": max(models, key=models.get) if models else s["agent_model"],
            "turns": len(reqs),
            "single_call_requests": sum(1 for r in reqs if r[3] == 1),
            "commits": commits if timed else None,
            "requests_per_commit": round(len(reqs) / commits, 1) if timed and commits else None,
            "wall_min": round(wall / 60, 1),
            "model_min": None if unknown else round(max(0.0, wall - tool_s) / 60, 1),
            "tool_min": None if unknown else round(tool_s / 60, 1),
            "turns_to_first_edit": (sum(1 for ts, _, _, _ in reqs if ts < first_edit)
                                    if first_edit else None),
            "ctx_first": reqs[0][2] if reqs else None,
            "ctx_last": reqs[-1][2] if reqs else None,
            "calls_over_60s": long_n, "min_in_calls_over_60s": round(long_s / 60, 1),
            "tool_min_by_class": {k: round(v[1] / 60, 1) for k, v in
                                  sorted(by_class.items(), key=lambda kv: -kv[1][1])[:5]},
            "untimed_calls": untimed,
        }
        if per_session is not None:
            extra = per_session.get(s["key"], {})
            row["unbatched_read_requests"] = extra.get("unbatched_read_requests", 0)
            row["denied_calls"] = extra.get("denied_calls", 0)
        if with_description:
            row["description"] = s["description"]
        out.append(row)
    out.sort(key=lambda r: -r["wall_min"])
    return out


def tool_time(conn: sqlite3.Connection, since: str, project: Optional[str] = None) -> list[dict]:
    """Tool time by time class (shell commands) or tool name, over the window."""
    if not _has_tool_times(conn):
        return []
    sql = f"""
        SELECT COALESCE(t.time_class, t.tool) class, COUNT(*) calls,
               ROUND(SUM(t.duration_s) / 60.0, 1) minutes,
               SUM(CASE WHEN t.duration_s > {LONG_CALL_S} THEN 1 ELSE 0 END) calls_over_60s,
               ROUND(SUM(CASE WHEN t.duration_s > {LONG_CALL_S} THEN t.duration_s ELSE 0 END)
                     / 60.0, 1) min_in_calls_over_60s,
               SUM(CASE WHEN t.duration_s IS NULL THEN 1 ELSE 0 END) untimed
        FROM tool_calls t JOIN sessions s ON s.key = t.session_key
        WHERE t.ts >= ?{" AND s.project = ?" if project else ""}
        GROUP BY 1 ORDER BY minutes DESC"""
    return _rows(conn, sql, (since, project) if project else (since,))


def rerun_calls(conn: sqlite3.Connection, since: str) -> tuple[list[dict], dict, set]:
    """Test/build commands re-run with no edit in between: (top rows by command
    family, runs per class, the ids of every re-run call)."""
    p = (since,)
    calls = _rows(conn, """
        SELECT t.id, t.session_key, t.ts, t.tool, t.cmd_class, t.cmd, t.cmd_hash, t.result_chars, s.project
        FROM tool_calls t JOIN sessions s ON s.key = t.session_key
        WHERE t.ts >= ? AND (t.cmd_class IN ('test', 'build')
              OR t.tool IN (""" + EDIT_TOOLS_SQL + """))
        ORDER BY t.session_key, t.ts""", p)
    reruns: dict[tuple, dict] = {}
    rerun_ids: set = set()
    runs_by_class = {"test": 0, "build": 0}
    seen: set = set()
    cur_session = None
    for c in calls:
        if c["session_key"] != cur_session:
            cur_session, seen = c["session_key"], set()
        if c["cmd_class"] is None:
            seen = set()  # an edit invalidates every prior result
            continue
        runs_by_class[c["cmd_class"]] += 1
        if c["cmd_hash"] in seen:
            rerun_ids.add(c["id"])
            k = (c["project"], c["cmd_class"], _cmd_head(c["cmd"] or ""))
            e = reruns.setdefault(k, {"project": k[0], "class": k[1], "command": k[2],
                                      "reruns": 0, "result_tokens": 0})
            e["reruns"] += 1
            e["result_tokens"] += (c["result_chars"] or 0) // CHARS_PER_TOKEN
        seen.add(c["cmd_hash"])
    rerun_rows = sorted(reruns.values(), key=lambda e: -e["reruns"])[:15]
    return rerun_rows, runs_by_class, rerun_ids


def build(conn: sqlite3.Connection, days: int = 7, roots=None) -> dict:
    """The digest. `roots` are the transcript roots the denied-call scan reads
    (default: token_ledger.ROOTS, the same ones the ledger ingested)."""
    since = _since(days)
    p = (since,)
    out: dict = {"window_days": days, "since": since,
                 "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "weights": "IET = " + IET}

    out["totals"] = _rows(conn, f"""
        SELECT s.source, r.model, COUNT(*) requests, SUM(r.input) input,
               SUM(r.cache_read) cache_read, SUM(r.cache_create) cache_create,
               SUM(r.output) output, CAST(SUM({IET}) AS INTEGER) iet,
               ROUND(1.0*SUM(r.cache_read)/MAX(1, SUM(r.input+r.cache_read+r.cache_create)), 3) hit_ratio
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? GROUP BY 1, 2 ORDER BY iet DESC""", p)
    total_iet = sum(t["iet"] or 0 for t in out["totals"]) or 1

    # 1. Cache rewrites after the first request: context re-written instead of read.
    rewrites = _rows(conn, f"""
        SELECT s.project, s.source, COUNT(*) requests,
               SUM(r.cache_create) tokens,
               CAST(SUM((r.cache_5m*{W_5M} + r.cache_1h*{W_1H}
                   + (r.cache_create-r.cache_5m-r.cache_1h)*{W_5M}) - r.cache_create*{W_READ}) AS INTEGER) excess_iet
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? AND r.seq > 1 AND r.cache_create > 20000
        GROUP BY 1, 2 ORDER BY excess_iet DESC""", p)

    rewrite_gaps = _rows(conn, f"""
        WITH g AS (
            SELECT r.*, (julianday(r.ts) - julianday(LAG(r.ts) OVER w)) * 1440 gap_min,
                   LAG(r.input + r.cache_read + r.cache_create) OVER w prev_ctx
            FROM requests r WHERE r.ts >= ?
            WINDOW w AS (PARTITION BY r.session_key ORDER BY r.seq))
        SELECT CASE WHEN s.is_subagent THEN 'subagent' ELSE 'main' END kind,
               CASE WHEN gap_min < 5 THEN 'a <5m' WHEN gap_min < 60 THEN 'b 5-60m'
                    ELSE 'c >=60m' END idle_gap,
               CASE WHEN g.input + g.cache_read + g.cache_create < 0.7 * g.prev_ctx
                    THEN 'compacted' ELSE 'same-or-grew' END ctx_change,
               COUNT(*) requests, SUM(g.cache_5m) ttl5m_tokens, SUM(g.cache_1h) ttl1h_tokens,
               CAST(AVG(gap_min) AS INTEGER) avg_gap_min
        FROM g JOIN sessions s ON s.key = g.session_key
        WHERE g.seq > 1 AND g.cache_create > 20000 GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""", p)

    # The write base a TTL change applies to: every cache write per agent kind,
    # not only the rewrites. Without it a 5m->1h proposal cannot net its offset.
    write_base = _rows(conn, """
        SELECT CASE WHEN s.is_subagent THEN 'subagent' ELSE 'main' END kind, s.source,
               SUM(r.cache_5m) ttl5m_tokens, SUM(r.cache_1h) ttl1h_tokens,
               SUM(r.cache_create) all_writes, COUNT(*) requests
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? GROUP BY 1, 2 ORDER BY all_writes DESC""", p)

    # A per-agent `experimental.cacheTtl` frontmatter key exists, so the TTL
    # trade-off is decided per agent type: net_if_1h > 0 only where idle
    # rewrites outweigh the dearer 1h write on everything else that type writes.
    ttl_by_agent = _rows(conn, f"""
        WITH g AS (
            SELECT r.*, (julianday(r.ts) - julianday(LAG(r.ts) OVER w)) * 1440 gap_min,
                   LAG(r.input + r.cache_read + r.cache_create) OVER w prev_ctx
            FROM requests r WHERE r.ts >= ?
            WINDOW w AS (PARTITION BY r.session_key ORDER BY r.seq))
        SELECT COALESCE(s.agent_type, 'subagent?') agent_type, s.source,
               SUM(g.cache_5m) ttl5m_tokens, SUM(g.cache_1h) ttl1h_tokens,
               SUM(CASE WHEN g.seq > 1 AND g.cache_create > 20000 AND g.gap_min >= 5
                         AND g.gap_min < 60 AND g.input + g.cache_read + g.cache_create >= 0.7 * g.prev_ctx
                        THEN g.cache_5m ELSE 0 END) idle_rewrite_5m_tokens
        FROM g JOIN sessions s ON s.key = g.session_key
        WHERE s.is_subagent GROUP BY 1, 2 HAVING SUM(g.cache_create) > 0""", p)
    for row in ttl_by_agent:
        rw, base = row["idle_rewrite_5m_tokens"] or 0, row["ttl5m_tokens"] or 0
        row["net_if_1h_iet"] = int(rw * (W_5M - W_READ) - (base - rw) * (W_1H - W_5M))
    ttl_by_agent.sort(key=lambda r: -r["net_if_1h_iet"])

    # 2. Uncached input: providers/paths where the prompt is not cached at all.
    uncached = _rows(conn, """
        SELECT s.source, r.model, COUNT(*) requests, SUM(r.input) input,
               SUM(r.cache_read) cache_read
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? GROUP BY 1, 2
        HAVING SUM(r.input) > 4 * SUM(r.cache_read) AND SUM(r.input) > 1000000
        ORDER BY input DESC""", p)

    # 3. Startup context: what every agent pays before its first action.
    startup = _rows(conn, f"""
        SELECT CASE WHEN s.is_subagent THEN COALESCE(s.agent_type, 'subagent?') ELSE 'main:' || s.source END agent,
               COUNT(*) starts,
               CAST(AVG(r.input + r.cache_read + r.cache_create) AS INTEGER) avg_ctx_tokens,
               ROUND(AVG(CASE WHEN r.cache_read = 0 THEN 1.0 ELSE 0 END), 2) cold_share,
               CAST(SUM({IET}) AS INTEGER) iet
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? AND r.seq = 1
        GROUP BY 1 ORDER BY iet DESC""", p)
    instr = _rows(conn, """
        SELECT REPLACE(i.path, ?, '~') path, COUNT(*) loads,
               CAST(AVG(i.chars) / ? AS INTEGER) avg_tokens,
               CAST(SUM(i.chars) / ? AS INTEGER) total_tokens
        FROM instructions i JOIN sessions s ON s.key = i.session_key
        WHERE s.started >= ? GROUP BY 1 ORDER BY total_tokens DESC LIMIT 25""",
                  (str(Path.home()), CHARS_PER_TOKEN, CHARS_PER_TOKEN, since))

    # 4. Big tool results: each is written once and re-read by every later request.
    big = _rows(conn, f"""
        SELECT t.tool, s.project, COUNT(*) n,
               CAST(SUM(t.result_chars) / {CHARS_PER_TOKEN} AS INTEGER) tokens,
               CAST(SUM(t.result_chars / {CHARS_PER_TOKEN} *
                   ({W_5M} + {W_READ} * (SELECT COUNT(*) FROM requests r2
                        WHERE r2.session_key = t.session_key AND r2.ts > t.ts))) AS INTEGER) iet
        FROM tool_calls t JOIN sessions s ON s.key = t.session_key
        WHERE t.ts >= ? AND t.result_chars > {BIG_RESULT_CHARS}
        GROUP BY 1, 2 ORDER BY iet DESC LIMIT 15""", p)

    # 5. Re-running the same test/build command with no edit in between.
    rerun_rows, runs_by_class, rerun_ids = rerun_calls(conn, since)

    # 6. Output per commit, per project.
    per_commit = _rows(conn, f"""
        SELECT s.project, CAST(SUM({IET}) AS INTEGER) iet,
               (SELECT COUNT(*) FROM tool_calls t JOIN sessions s2 ON s2.key = t.session_key
                 WHERE s2.project = s.project AND t.cmd_class = 'commit' AND t.ts >= ?) commits
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? GROUP BY 1 ORDER BY iet DESC LIMIT 15""", (since, since))
    for row in per_commit:
        row["iet_per_commit"] = row["iet"] // row["commits"] if row["commits"] else None

    # 7. Context size: every request re-reads the whole prefix, so long sessions
    #    pay O(n^2). Excess = cache reads above LONG_CTX per request.
    ctx = "(r.input + r.cache_read + r.cache_create)"
    buckets = _rows(conn, f"""
        SELECT CASE WHEN {ctx} < 50000 THEN 'a <50k' WHEN {ctx} < 100000 THEN 'b 50-100k'
                    WHEN {ctx} < 200000 THEN 'c 100-200k' WHEN {ctx} < 400000 THEN 'd 200-400k'
                    ELSE 'e >=400k' END bucket,
               COUNT(*) requests, CAST(SUM({IET}) AS INTEGER) iet
        FROM requests r WHERE r.ts >= ? GROUP BY 1 ORDER BY 1""", p)
    long_ctx = _rows(conn, f"""
        SELECT CASE WHEN s.is_subagent THEN 'subagent' ELSE 'main' END kind, s.source, r.model,
               COUNT(*) requests, CAST(AVG({ctx}) AS INTEGER) avg_ctx,
               CAST(SUM(({ctx} - {LONG_CTX}) * {W_READ}) AS INTEGER) excess_iet
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? AND {ctx} > {LONG_CTX}
        GROUP BY 1, 2, 3 ORDER BY excess_iet DESC""", p)
    long_sessions = _rows(conn, f"""
        SELECT s.project, CASE WHEN s.is_subagent THEN COALESCE(s.agent_type, 'subagent') ELSE 'main' END agent,
               COUNT(*) requests, MAX({ctx}) peak_ctx,
               CAST(SUM(MAX(0, {ctx} - {LONG_CTX}) * {W_READ}) AS INTEGER) excess_iet
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? GROUP BY r.session_key HAVING excess_iet > 0
        ORDER BY excess_iet DESC LIMIT 10""", p)

    view, by_session = request_view(conn, since, roots=roots, rerun_ids=rerun_ids)
    unb, den = view["unbatched_reads"], view["denied_calls"]
    n_req = view["totals"]["requests"]
    waste = {w["id"]: w for w in view["waste"]}

    candidates = [
        {"id": "unbatched_reads", "iet": unb["iet"], "requests": unb["requests"],
         "what": ("UPPER BOUND on requests one parallel call could have absorbed: read-only requests "
                  "(Read/Grep/Glob or a Bash cat/sed/grep/head) whose previous request in the same session "
                  "was also read-only. A later read may need the earlier result, and a user message between "
                  "two requests is invisible to the ledger. IET is the absorbed turn's prefix read and "
                  "output; its cache write is left out because the result is still written once"),
         "detail": {k: unb[k] for k in ("read_calls", "calls", "read_calls_after_read",
                                        "of_which_in_another_request", "chain_lengths",
                                        "by_project", "by_agent")}},
        {"id": "denied_calls", "iet": den["iet"], "requests": den["requests_all_denied"],
         "what": ("Tool calls the harness denied or blocked (worktree guard, hook block, Blocked: sleep, "
                  "permission, missing tool). Each spends a full-context request that does nothing. "
                  "`requests` counts requests whose every call was denied; `calls` counts all denied calls. "
                  "Read from transcripts through adapters/transcripts, so a transcript that is gone from "
                  "disk is not scanned (see scan.missing, scan.unreadable)"),
         "detail": {k: den[k] for k in ("calls", "requests_some_denied", "calls_matched_to_requests", "scan",
                                        "by_class", "by_tool", "by_agent", "by_project")}},
        {"id": "long_context", "iet": sum(r["excess_iet"] or 0 for r in long_ctx),
         "what": f"Cache reads above {LONG_CTX:,} tokens/request: long sessions re-read the whole prefix every turn",
         "detail": {"ctx_buckets": buckets, "by_kind_model": long_ctx, "top_sessions": long_sessions}},
        {"id": "cache_rewrites", "iet": sum(r["excess_iet"] or 0 for r in rewrites),
         "what": ("Mid-session cache writes >20k tokens instead of reads. Gap > TTL with same-or-grew"
                  " context = cache expired while idle; compacted = auto-compaction rewrite"),
         "detail": {"by_idle_gap": rewrite_gaps, "all_cache_writes": write_base,
                    "ttl_by_agent_type": ttl_by_agent[:12], "by_project": rewrites[:10]}},
        {"id": "startup_context", "iet": sum(r["iet"] or 0 for r in startup),
         "what": "First-request context every main session and subagent pays before acting",
         "detail": {"by_agent": startup[:12], "instruction_files": instr}},
        {"id": "big_tool_results", "iet": sum(r["iet"] or 0 for r in big),
         "what": f"Tool results >{BIG_RESULT_CHARS} chars, written once then re-read by every later request",
         "detail": big},
        {"id": "redundant_reruns", "iet": sum(r["result_tokens"] for r in rerun_rows),
         "what": "Same test/build command re-run with no edit in between (IET counts result tokens only)",
         "detail": {"runs": runs_by_class, "top": rerun_rows}},
        {"id": "uncached_input", "iet": int(sum((r["input"] or 0) * (1 - W_READ) for r in uncached)),
         "what": "Providers/models whose prompts are mostly sent uncached (excess vs cache-read price)",
         "detail": uncached},
    ]
    for c in candidates:
        c["share_of_total"] = round(c["iet"] / total_iet, 3)
        if c["id"] == "redundant_reruns":
            c["requests"] = waste["redundant_reruns"]["requests"]
        if "requests" in c:
            c["share_of_requests"] = _share(c["requests"], n_req)
    candidates.sort(key=lambda c: -c["iet"])
    out["total_iet"] = total_iet
    out["candidates"] = candidates
    out["iet_per_commit"] = per_commit
    out["tool_time"] = tool_time(conn, since)
    all_lanes = lanes(conn, since, per_session=by_session)
    out["lanes"] = all_lanes[:15]
    # Like by_source, the section appears only when the ledger has something to
    # say: a window with no recorded tool calls has no request shape to rank.
    if view["unbatched_reads"]["calls"]:
        view["top_lanes"] = sorted(all_lanes, key=lambda r: -r["turns"])[:15]
        out["requests"] = view

    # Agent parity: only when a non-Claude-format source is in the window, so a
    # claude/mmx-only ledger renders exactly as before.
    by_source = _rows(conn, f"""
        SELECT s.source, COUNT(DISTINCT r.session_key) sessions, COUNT(*) requests,
               SUM(r.input) input, SUM(r.cache_read) cache_read, SUM(r.cache_create) cache_create,
               SUM(r.output) output, SUM(r.thinking) thinking, SUM(r.n_tool_uses) tool_uses,
               CAST(SUM({IET}) AS INTEGER) iet,
               ROUND(1.0*SUM(r.cache_read)/MAX(1, SUM(r.input+r.cache_read+r.cache_create)), 3) hit_ratio
        FROM requests r JOIN sessions s ON s.key = r.session_key
        WHERE r.ts >= ? GROUP BY 1 ORDER BY iet DESC""", p)
    if any(r["source"] in PARITY_SOURCES for r in by_source):
        net_by_source: dict[str, int] = {}
        for row in ttl_by_agent:
            net_by_source[row["source"]] = net_by_source.get(row["source"], 0) + row["net_if_1h_iet"]
        for r in by_source:
            r["top_tools"] = _rows(conn, """
                SELECT t.tool, COUNT(*) n FROM tool_calls t JOIN sessions s ON s.key = t.session_key
                WHERE t.ts >= ? AND s.source = ? GROUP BY 1 ORDER BY n DESC LIMIT 8""",
                                   (since, r["source"]))
            # A source that reports no cache writes has no 5m->1h trade-off to net:
            # null, never zero (zero would read as "measured, no saving").
            if (r["cache_create"] or 0) == 0:
                r["ttl_net_if_1h_iet"] = None
                r["ttl_note"] = "n/a: source reports no cache-write tokens"
            else:
                r["ttl_net_if_1h_iet"] = net_by_source.get(r["source"], 0)
                r["ttl_note"] = "sum of ttl_by_agent_type rows (subagents)"
        out["by_source"] = by_source
    return out


def _detail_lines(detail, label: str = "") -> list[str]:
    """One compact JSON object per row so the digest stays short in lines."""
    if isinstance(detail, dict):
        out = []
        for k, v in detail.items():
            out += _detail_lines(v, k)
        return out
    head = [f"{label}:"] if label else []
    if isinstance(detail, list):
        return head + ["    " + json.dumps(r, separators=(",", ":")) for r in detail]
    return head + ["    " + json.dumps(detail, separators=(",", ":"))]


def attach_prior_proposals(rep: dict, proposals: Path) -> None:
    """Close the loop: for each filed proposal, its candidate's IET then vs now."""
    if not proposals.exists():
        return
    now = {c["id"]: c["iet"] for c in rep["candidates"]}
    rows = []
    for line in proposals.read_text().splitlines():
        try:
            pp = json.loads(line)
        except ValueError:
            continue
        rows.append({"filed": pp.get("filed", "?"), "candidate_id": pp.get("candidate_id"),
                     "title": pp.get("title", "")[:80], "iet_then": int(pp.get("candidate_iet") or 0),
                     "iet_now": int(now.get(pp.get("candidate_id"), 0))})
    rep["prior_proposals"] = rows[-20:]


def to_markdown(rep: dict) -> str:
    lines = [f"# Token waste digest — last {rep['window_days']}d (since {rep['since']}Z)", "",
             f"Total {rep['total_iet']:,} input-equivalent tokens. {rep['weights']}", "",
             "## Totals by source/model", "",
             "| source | model | requests | input | cache_read | cache_create | output | hit | IET |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for t in rep["totals"]:
        lines.append(f"| {t['source']} | {t['model']} | {t['requests']:,} | {t['input']:,} | "
                     f"{t['cache_read']:,} | {t['cache_create']:,} | {t['output']:,} | "
                     f"{t['hit_ratio']} | {t['iet']:,} |")
    if rep.get("requests"):
        lines += [""] + requests_md(rep["requests"])
    lines += ["", "## Waste candidates (ranked)", ""]
    for i, c in enumerate(rep["candidates"], 1):
        req = f", {c['requests']:,} requests" if "requests" in c else ""
        lines += [f"### {i}. {c['id']} — {c['iet']:,} IET ({c['share_of_total']:.1%}){req}", "", c["what"], ""]
        lines += _detail_lines(c["detail"]) + [""]
    if rep.get("prior_proposals"):
        lines += ["## Prior proposals: candidate IET at filing vs now", "",
                  "| filed | candidate | title | IET then | IET now |", "|---|---|---|---:|---:|"]
        for pp in rep["prior_proposals"]:
            lines.append(f"| {pp['filed']} | {pp['candidate_id']} | {pp['title']} | "
                         f"{pp['iet_then']:,} | {pp['iet_now']:,} |")
        lines.append("")
    lines += ["## IET per commit", "", "| project | IET | commits | IET/commit |", "|---|---:|---:|---:|"]
    for r in rep["iet_per_commit"]:
        lines.append(f"| {r['project']} | {r['iet']:,} | {r['commits']} | "
                     f"{'' if r['iet_per_commit'] is None else format(r['iet_per_commit'], ',')} |")
    if rep.get("tool_time"):
        lines += ["", "## Tool time by class", "",
                  "| class | calls | min | calls >60s | min in them | untimed |",
                  "|---|---:|---:|---:|---:|---:|"]
        for r in rep["tool_time"]:
            lines.append(f"| {r['class']} | {r['calls']:,} | {r['minutes'] or 0} | "
                         f"{r['calls_over_60s'] or 0} | {r['min_in_calls_over_60s'] or 0} | "
                         f"{r['untimed'] or 0} |")
    if rep.get("lanes"):
        lines += ["", "## Lanes (subagents) by wall time", ""] + lanes_table(rep["lanes"])
    if rep.get("by_source"):
        lines += ["", "## Agent parity: digest by source", "",
                  "| source | sessions | requests | input | cache_read | cache_create | output | "
                  "thinking | hit | IET | TTL 1h net |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for r in rep["by_source"]:
            net = "n/a (no cache writes)" if r["ttl_net_if_1h_iet"] is None else f"{r['ttl_net_if_1h_iet']:,}"
            lines.append(f"| {r['source']} | {r['sessions']:,} | {r['requests']:,} | {r['input']:,} | "
                         f"{r['cache_read']:,} | {r['cache_create']:,} | {r['output']:,} | "
                         f"{r['thinking']:,} | {r['hit_ratio']} | {r['iet']:,} | {net} |")
        for r in rep["by_source"]:
            lines += ["", f"{r['source']} top tools:"] + [
                "    " + json.dumps(t, separators=(",", ":")) for t in r["top_tools"]]
    return "\n".join(lines) + "\n"


def requests_md(v: dict) -> list[str]:
    """The request-denominated section: what the plan's 5h window is billed in."""
    pct = lambda x: "" if x is None else f"{x:.0%}"
    blank = lambda x: "" if x is None else x
    t = v["totals"]
    per_commit = f" ({t['requests_per_commit']} requests per commit)." if t["requests_per_commit"] else "."
    lines = [f"## Requests (the plan bills per request) — {', '.join(v['sources'])}", "",
             f"{t['requests']:,} requests in {t['sessions']:,} sessions; {pct(t['single_call_share'])} "
             f"issued exactly one tool call; {t['commits']:,} commit commands" + per_commit,
             "", "| source | requests | no tool | 1 tool | 2+ tools (parallel) | 1-tool share | "
             "unbatched reads | denied |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in v["by_source"]:
        lines.append(f"| {r['source']} | {r['requests']:,} | {r['no_tool']:,} | {r['one_tool']:,} | "
                     f"{r['multi_tool']:,} | {pct(r['single_call_share'])} | "
                     f"{r['unbatched_read_requests']:,} | {r['denied_requests']:,} |")
    lines += ["", "### Request waste (ranked by requests)", "",
              "| candidate | requests | share of requests | turn IET | bound |", "|---|---:|---:|---:|---|"]
    for w in v["waste"]:
        lines.append(f"| {w['id']} | {w['requests']:,} | {pct(w['share_of_requests'])} | "
                     f"{w['turn_iet']:,} | {w['bound']} |")
    lines += ["", "### Requests by agent type and requested tier", "",
              "| agent | tier | sessions | requests | per session | 1-tool share | commits | per commit | "
              "unbatched reads | denied |", "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in v["by_agent_tier"]:
        lines.append(f"| {r['agent']} | {r['requested_tier'] or ''} | {r['sessions']:,} | {r['requests']:,} | "
                     f"{r['requests_per_session']} | {pct(r['single_call_share'])} | {r['commits']} | "
                     f"{blank(r['requests_per_commit'])} | "
                     f"{r['unbatched_read_requests']:,} | {r['denied_requests']:,} |")
    lines += ["", "### Requests by project", "",
              "| project | sessions | requests | 1-tool share | commits | per commit | unbatched reads | "
              "denied requests | denied calls |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in v["by_project"]:
        lines.append(f"| {r['project']} | {r['sessions']:,} | {r['requests']:,} | {pct(r['single_call_share'])} | "
                     f"{r['commits']} | {blank(r['requests_per_commit'])} | "
                     f"{r['unbatched_read_requests']:,} | {r['denied_requests']:,} | "
                     f"{blank(r.get('denied_calls'))} |")
    if v.get("top_lanes"):
        lines += ["", "### Top lanes by requests", ""] + lanes_table(v["top_lanes"])
    return lines


def lanes_table(rows: list[dict]) -> list[str]:
    desc = any("description" in r for r in rows)
    reqs = any("denied_calls" in r for r in rows)
    head = ("| lane | " + ("description | " if desc else "") + "project | agent | tier | model | turns | "
            "1-tool | commits | " + ("unbatched reads | denied | " if reqs else "")
            + "wall min | model min | tool min | turns to 1st edit | ctx first k | ctx last k | "
            "top tool min by class |")
    out = [head, "|" + "---|" * (head.count("|") - 1)]
    for r in rows:
        k = lambda v: "" if v is None else f"{v / 1000:.0f}"
        cls = ", ".join(f"{c} {m}" for c, m in list(r["tool_min_by_class"].items())[:4])
        out.append(f"| {r['lane']} | " + (f"{(r.get('description') or '')[:40]} | " if desc else "")
                   + f"{r['project']} | {r['agent_type']} | {r.get('agent_model') or ''} | {r['model']} | "
                   f"{r['turns']} | {r.get('single_call_requests', '')} | "
                   f"{'' if r.get('commits') is None else r['commits']} | "
                   + (f"{r.get('unbatched_read_requests', '')} | {r.get('denied_calls', '')} | " if reqs else "")
                   + f"{r['wall_min']} | {'?' if r['model_min'] is None else r['model_min']} | "
                   f"{'?' if r['tool_min'] is None else r['tool_min']} | "
                   f"{'' if r['turns_to_first_edit'] is None else r['turns_to_first_edit']} | "
                   f"{k(r['ctx_first'])} | {k(r['ctx_last'])} | {cls} |")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--md", action="store_true", help="markdown (default JSON)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--proposals", type=Path, default=PROPOSALS)
    ap.add_argument("--lanes", action="store_true",
                    help="local per-lane time table WITH dispatch descriptions (not sanitized)")
    ap.add_argument("--requests", action="store_true",
                    help="request-denominated view only (sanitized), optionally for one --project")
    ap.add_argument("--project")
    a = ap.parse_args()
    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    if a.requests:
        since = _since(a.days)
        view, by_session = request_view(conn, since, project=a.project,
                                        rerun_ids=rerun_calls(conn, since)[2])
        view["top_lanes"] = sorted(lanes(conn, since, a.project, per_session=by_session),
                                   key=lambda r: -r["turns"])[:15]
        text = "\n".join(requests_md(view)) if a.md else json.dumps(view, indent=1)
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(text)
        else:
            print(text)
        return 0
    if a.lanes:
        since = _since(a.days)
        _, by_session = request_view(conn, since, project=a.project)
        rows = lanes(conn, since, a.project, with_description=True, per_session=by_session)
        tt = tool_time(conn, since, a.project)
        if not a.md:
            print(json.dumps({"lanes": rows, "tool_time": tt}, indent=1))
            return 0
        print("LOCAL VIEW: carries lane descriptions; not for the digest.\n")
        print("\n".join(lanes_table(rows)))
        print("\n".join(["", "| class | calls | min | calls >60s | min in them |", "|---|---:|---:|---:|---:|"]
                        + [f"| {r['class']} | {r['calls']} | {r['minutes'] or 0} | "
                           f"{r['calls_over_60s'] or 0} | {r['min_in_calls_over_60s'] or 0} |" for r in tt]))
        return 0
    rep = build(conn, a.days)
    attach_prior_proposals(rep, a.proposals)
    text = to_markdown(rep) if a.md else json.dumps(rep, indent=1)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
