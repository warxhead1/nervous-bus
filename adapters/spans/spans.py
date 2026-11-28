#!/usr/bin/env python3
"""spans: where agent time goes, from Claude Code transcripts.

  spans scan [--root DIR] [--cursor PATH] [--since 24h] [--dry-run] [--nervous BIN]
      Pair tool_use with tool_result in every session file, classify each call and
      publish agent.tool.span.v1 for spans not published before. A rerun publishes
      nothing twice: the cursor keeps a byte offset per file and the ids published
      after it.
  spans top [--root DIR] [--project P] [--since 24h] [--limit N] [--json]
      Rank the slowest producers by wall time: command classes, wait targets,
      agents. JSON on stdout; --text prints the table instead.

Exit codes: 0 ok, 1 a publish failed (cursor kept, rerun retries), 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "transcripts"))
from classify import classify  # noqa: E402
from claude_layout import session_files  # noqa: E402
import project as projmod  # noqa: E402

CHANNEL = "agent.tool.span.v1"
REPO_NERVOUS = HERE.parents[1] / "sdk" / "shell" / "nervous"
PUBLISH_TIMEOUT_S = 30
STALE_PENDING_S = 6 * 3600   # a call with no result this long before the last line is lost
_EXIT = re.compile(r"^Exit code (\d+)")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def parse_since(s: str) -> float:
    m = re.fullmatch(r"(\d+)([smhd])", s or "")
    if not m:
        raise SystemExit(f"spans: bad --since {s!r}; use forms like 90m, 24h, 7d")
    return time.time() - int(m.group(1)) * _UNITS[m.group(2)]


def default_root() -> str:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return os.path.join(cfg, "projects")


def default_cursor() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "nervous-bus" / "spans" / "cursor.json"


def lane_meta(path: str) -> tuple[str, str]:
    """(workflow_id, label) for a session file; blanks when it is not a workflow lane."""
    wf = ""
    for part in path.split(os.sep):
        if part.startswith("wf_"):
            wf = part
    label = ""
    try:
        meta = json.loads(Path(path[:-len(".jsonl")] + ".meta.json").read_text())
        label = str(meta.get("label") or meta.get("description") or "")[:200]
    except (OSError, ValueError, AttributeError):
        pass
    return wf, label


def span_id(rel: str, call_id: str) -> str:
    return hashlib.sha256(f"{rel}\0{call_id}".encode()).hexdigest()[:16]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_spans(path: str, rel: str, offset: int = 0, done: frozenset = frozenset()):
    """Pair calls in one session file from `offset`. Returns (spans, next_offset, next_done).

    next_offset is the start of the earliest call still without a result, or the end of
    the last complete line. `done` holds ids already published at or after `offset`.
    """
    wf, label = lane_meta(path)
    pend: dict[str, dict] = {}
    spans: list[dict] = []
    last_ts = 0.0
    pos = offset
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            raw = f.readline()
            if not raw.endswith(b"\n"):
                break
            line_start, pos = pos, pos + len(raw)
            if b'"tool_' not in raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            ts = parse_ts(e.get("timestamp", ""))
            if ts:
                last_ts = max(last_ts, ts)
            content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and ts:
                    cid = b.get("id", "")
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    cls, target = classify(b.get("name", ""), inp.get("command") if isinstance(inp.get("command"), str) else None)
                    pend[cid] = {
                        "offset": line_start, "ts": ts, "tool": b.get("name") or "unknown",
                        "class": cls, "wait_target": target, "cwd": e.get("cwd", ""),
                        "session": e.get("sessionId", ""), "agent_id": e.get("agentId", ""),
                    }
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pend and ts:
                    p = pend.pop(b["tool_use_id"])
                    txt = b.get("content")
                    if isinstance(txt, list):
                        txt = "\n".join(x.get("text", "") for x in txt if isinstance(x, dict))
                    txt = txt if isinstance(txt, str) else ""
                    status, code = "ok", None
                    if b.get("is_error"):
                        status = "error"
                        m = _EXIT.match(txt)
                        code = int(m.group(1)) if m else None
                    elif p["tool"] == "Bash":
                        code = 0
                    spans.append(build(rel, b["tool_use_id"], p, ts, status, code, wf, label, path))
    stale = last_ts - STALE_PENDING_S
    for cid in [c for c, p in pend.items() if p["ts"] < stale]:
        spans.append(build(rel, cid, pend.pop(cid), None, "lost", None, wf, label, path))
    next_offset = min([p["offset"] for p in pend.values()] + [pos])
    fresh = [s for s in spans if s["span_id"] not in done]
    kept = {s["span_id"] for s in spans if s["_off"] >= next_offset}
    return fresh, next_offset, kept


def build(rel, cid, p, end_ts, status, code, wf, label, path) -> dict:
    cwd = p["cwd"]
    proj = (projmod.project_of(cwd) if cwd else None) or projmod.project_of_transcript(path) or "unknown"
    s = {
        "v": 1, "span_id": span_id(rel, cid), "session": p["session"] or Path(path).stem,
        "project": proj, "tool": p["tool"], "class": p["class"], "start": iso(p["ts"]),
        "status": status, "_off": p["offset"],
    }
    if p["agent_id"]:
        s["agent_id"] = p["agent_id"]
    if wf:
        s["workflow_id"] = wf
    if label:
        s["label"] = label
    if p["wait_target"]:
        s["wait_target"] = p["wait_target"]
    if end_ts is not None:
        s["duration_ms"] = max(0, int(round((end_ts - p["ts"]) * 1000)))
    if code is not None:
        s["exit_code"] = code
    return s


def public(span: dict) -> dict:
    return {k: v for k, v in span.items() if not k.startswith("_")}


def recent_files(root: str, since: float) -> Iterator[tuple[str, str]]:
    for rel, path in session_files(root):
        try:
            if os.path.getmtime(path) >= since:
                yield rel, path
        except OSError:
            continue


# ------------------------------------------------------------------ scan

def find_nervous(explicit: Optional[str]) -> str:
    return explicit or os.environ.get("NERVOUS_BIN") or (str(REPO_NERVOUS) if REPO_NERVOUS.is_file() else None) \
        or shutil.which("nervous") or "nervous"


def publish(nervous: str, record: dict) -> bool:
    env = dict(os.environ, NERVOUS_SOURCE="/spans")
    try:
        r = subprocess.run([nervous, "publish", CHANNEL, json.dumps(record, separators=(",", ":"))],
                           capture_output=True, text=True, timeout=PUBLISH_TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"spans: publish failed: {exc}", file=sys.stderr)
        return False
    if r.returncode != 0:
        print(f"spans: publish exit {r.returncode}: {r.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


def head_hash(path: str) -> str:
    """Identity of a file's first line: a replaced file with a reused inode still differs."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.readline(512)).hexdigest()[:16]


def load_cursor(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cursor(path: Path, cur: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cur))
    os.replace(tmp, path)


def cmd_scan(a) -> int:
    root = a.root or default_root()
    cursor_path = Path(a.cursor) if a.cursor else default_cursor()
    cur = load_cursor(cursor_path)
    since = parse_since(a.since) if a.since else 0.0
    nervous = find_nervous(a.nervous)
    published = 0
    for rel, path in recent_files(root, since):
        st = os.stat(path)
        c = cur.get(rel) or {}
        same = c.get("inode") == st.st_ino and c.get("head") == head_hash(path) and c.get("offset", 0) <= st.st_size
        offset = c.get("offset", 0) if same else 0
        done = frozenset(c.get("done", [])) if offset else frozenset()
        spans, next_offset, kept = read_spans(path, rel, offset, done)
        for s in spans:
            if a.dry_run:
                print(json.dumps(public(s)))
            elif not publish(nervous, public(s)):
                print(f"spans: stopped after {published} published", file=sys.stderr)
                return 1
            published += 1
        if not a.dry_run:
            cur[rel] = {"inode": st.st_ino, "head": head_hash(path), "offset": next_offset, "done": sorted(kept)}
            save_cursor(cursor_path, cur)
    print(json.dumps({"published": published, "dry_run": bool(a.dry_run)}), file=sys.stderr)
    return 0


# ------------------------------------------------------------------ top

def rank(rows: dict, limit: int) -> list[dict]:
    out = [{"key": k, "hours": round(v[0] / 3600, 3), "calls": v[1],
            "avg_s": round(v[0] / max(v[1], 1), 1)} for k, v in rows.items()]
    return sorted(out, key=lambda r: -r["hours"])[:limit]


def cmd_top(a) -> int:
    root = a.root or default_root()
    since = parse_since(a.since)
    classes, targets, agents = (defaultdict(lambda: [0.0, 0]) for _ in range(3))
    total = 0.0
    n = 0
    for rel, path in recent_files(root, since):
        wf, label = lane_meta(path)
        spans, _, _ = read_spans(path, rel)
        for s in spans:
            if s.get("duration_ms") is None or parse_ts(s["start"]) < since:
                continue
            if a.project and s["project"] != a.project:
                continue
            dur = s["duration_ms"] / 1000
            n += 1
            total += dur
            key = s["class"] if s["class"] != "wait" else "wait"
            classes[key][0] += dur
            classes[key][1] += 1
            if s["class"] == "wait":
                targets[s["wait_target"]][0] += dur
                targets[s["wait_target"]][1] += 1
            who = s.get("label") or s.get("agent_id") or s["session"]
            who = f"{s.get('workflow_id', '')} {who}".strip()
            agents[who][0] += dur
            agents[who][1] += 1
    res = {"since": a.since, "project": a.project or "*", "calls": n, "tool_hours": round(total / 3600, 3),
           "classes": rank(classes, a.limit), "wait_targets": rank(targets, a.limit),
           "agents": rank(agents, a.limit)}
    if a.text:
        print(text_view(res))
    else:
        print(json.dumps(res, indent=2))
    return 0


def text_view(res: dict) -> str:
    out = [f"project {res['project']}, since {res['since']}: {res['calls']} calls, {res['tool_hours']:.2f} h in tools"]
    for title, key in (("TOOL CLASS", "classes"), ("WAIT TARGET", "wait_targets"), ("AGENT", "agents")):
        out.append(f"\n{title}")
        for r in res[key]:
            out.append(f"{r['hours']:7.2f}h {r['calls']:6d} calls  avg {r['avg_s']:6.0f}s  {r['key']}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="spans", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="publish new tool spans")
    sc.add_argument("--root")
    sc.add_argument("--cursor")
    sc.add_argument("--since", help="only files modified within this window, e.g. 24h")
    sc.add_argument("--dry-run", action="store_true", help="print spans, publish nothing, keep the cursor")
    sc.add_argument("--nervous")
    tp = sub.add_parser("top", help="rank the slowest producers")
    tp.add_argument("--root")
    tp.add_argument("--project")
    tp.add_argument("--since", default="24h")
    tp.add_argument("--limit", type=int, default=10)
    tp.add_argument("--text", action="store_true", help="print a table instead of JSON")
    a = ap.parse_args(argv)
    return {"scan": cmd_scan, "top": cmd_top}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
