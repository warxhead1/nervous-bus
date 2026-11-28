#!/usr/bin/env python3
"""spans: where agent time goes, from Claude Code transcripts.

  spans scan [--root DIR] [--cursor PATH] [--since 24h] [--dry-run] [--nervous BIN]
      Pair tool_use with tool_result in every session file, classify each call and
      publish agent.tool.span.v1 for spans not published before. A rerun publishes
      nothing twice: the cursor keeps a byte offset per file and the ids published
      after it.
  spans top [--root DIR] [--project P] [--since 24h] [--limit N] [--text]
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
BATCH_SIZE = 500
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
        raise ValueError(f"bad --since {s!r}; use forms like 90m, 24h, 7d")
    return time.time() - int(m.group(1)) * _UNITS[m.group(2)]


def default_root() -> str:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return os.path.join(cfg, "projects")


def default_cursor() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "nervous-bus" / "spans" / "cursor.json"


_SLUG = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")


def safe_label(meta: dict) -> str:
    """The lane's explicit label, or its description when that is already a slug."""
    for key in ("label", "description"):
        v = meta.get(key)
        if isinstance(v, str) and _SLUG.match(v):
            return v
    return ""


def lane_meta(path: str) -> tuple[str, str]:
    """(workflow_id, label) for a session file; blanks when it is not a workflow lane."""
    wf = next((p for p in reversed(path.split(os.sep)) if p.startswith("wf_")), "")
    try:
        meta = json.loads(Path(path[:-len(".jsonl")] + ".meta.json").read_text())
        return wf[:200], safe_label(meta)
    except (OSError, ValueError, AttributeError):
        return wf[:200], ""


def span_id(rel: str, call_id: str) -> str:
    return hashlib.sha256(f"{rel}\0{call_id}".encode()).hexdigest()[:16]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def result_text(block: dict) -> str:
    txt = block.get("content")
    if isinstance(txt, list):
        txt = "\n".join(x.get("text", "") for x in txt if isinstance(x, dict))
    return txt if isinstance(txt, str) else ""


def open_call(line_start: int, ts: float, e: dict, b: dict) -> dict:
    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
    cmd = inp.get("command")
    cls, target = classify(b.get("name", ""), cmd if isinstance(cmd, str) else None)
    return {"offset": line_start, "ts": ts, "tool": (b.get("name") or "unknown")[:100],
            "class": cls, "wait_target": target, "cwd": e.get("cwd") or "",
            "session": e.get("sessionId") or "", "agent_id": e.get("agentId") or ""}


def close_status(p: dict, block: dict) -> tuple[str, Optional[int]]:
    if block.get("is_error"):
        m = _EXIT.match(result_text(block))
        return "error", int(m.group(1)) if m else None
    return "ok", (0 if p["tool"] == "Bash" else None)


def line_blocks(raw: bytes) -> tuple[Optional[float], list]:
    """(timestamp, [(entry, block)]) for one transcript line; empty when it holds no tool blocks."""
    try:
        e = json.loads(raw) if b'"tool_' in raw else None
    except ValueError:
        e = None
    if not isinstance(e, dict):
        return None, []
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    blocks = [(e, b) for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    return parse_ts(e.get("timestamp", "")), blocks


def tool_lines(f) -> Iterator[tuple[int, int, Optional[float], list]]:
    """(line_start, line_end, timestamp, blocks) per complete line from the file's position."""
    pos = f.tell()
    for raw in iter(f.readline, b""):
        if not raw.endswith(b"\n"):
            return
        start, pos = pos, pos + len(raw)
        ts, blocks = line_blocks(raw)
        yield start, pos, ts, blocks


def pair_block(pend: dict, spans: list, ctx: tuple, start: int, ts: float, e: dict, b: dict) -> None:
    """Open a call for a tool_use block, or close one and append its span for a tool_result."""
    if b.get("type") == "tool_use":
        pend[b.get("id", "")] = open_call(start, ts, e, b)
    elif b.get("type") == "tool_result" and b.get("tool_use_id") in pend:
        p = pend.pop(b["tool_use_id"])
        status, code = close_status(p, b)
        spans.append(build(ctx[0], b["tool_use_id"], p, ts, status, code, *ctx[1:]))


def expire(pend: dict, spans: list, ctx: tuple, last_ts: float, mtime: float) -> None:
    """Turn calls open past STALE_PENDING_S (transcript time) or in an idle file into lost spans."""
    idle = time.time() - mtime > STALE_PENDING_S
    for cid in [c for c, p in pend.items() if idle or p["ts"] < last_ts - STALE_PENDING_S]:
        spans.append(build(ctx[0], cid, pend.pop(cid), None, "lost", None, *ctx[1:]))


def read_spans(path: str, rel: str, offset: int = 0, done: frozenset = frozenset()):
    """Pair calls in one session file from `offset`. Returns (fresh spans, next_offset, kept ids).

    next_offset is the start of the earliest call still without a result, or the end of
    the last complete line. `done` holds ids already published at or after `offset`.
    """
    wf, label = lane_meta(path)
    ctx = (rel, wf, label, path)
    pend: dict[str, dict] = {}
    spans: list[dict] = []
    last_ts, pos = 0.0, offset
    with open(path, "rb") as f:
        mtime = os.fstat(f.fileno()).st_mtime
        f.seek(offset)
        for start, pos, ts, blocks in tool_lines(f):
            last_ts = max(last_ts, ts or 0.0)
            for e, b in blocks if ts else ():
                pair_block(pend, spans, ctx, start, ts, e, b)
    expire(pend, spans, ctx, last_ts, mtime)
    next_offset = min([p["offset"] for p in pend.values()] + [pos])
    fresh = [s for s in spans if s["span_id"] not in done]
    return fresh, next_offset, {s["span_id"] for s in spans if s["_off"] >= next_offset}


def build(rel, cid, p, end_ts, status, code, wf, label, path) -> dict:
    proj = projmod.project_of(p["cwd"] or None, transcript=path) or "unknown"
    s = {
        "v": 1, "span_id": span_id(rel, cid), "session": (p["session"] or Path(path).stem)[:200],
        "project": proj[:200], "tool": p["tool"], "class": p["class"], "start": iso(p["ts"]),
        "status": status, "_off": p["offset"],
    }
    optional = {"agent_id": p["agent_id"][:200], "workflow_id": wf, "label": label,
                "wait_target": p["wait_target"], "exit_code": code,
                "duration_ms": None if end_ts is None else max(0, int(round((end_ts - p["ts"]) * 1000)))}
    s.update({k: v for k, v in optional.items() if v not in (None, "")})
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


def publish_batch(nervous: str, records: list) -> Optional[dict]:
    """Send records through one `nervous publish-batch`. The summary dict when every line was
    accounted for (published, or rejected and dead-lettered), None when the batch must be retried."""
    env = dict(os.environ, NERVOUS_SOURCE="/spans")
    body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    try:
        r = subprocess.run([nervous, "publish-batch", CHANNEL], input=body, capture_output=True,
                           text=True, timeout=PUBLISH_TIMEOUT_S + len(records), env=env)
        summary = json.loads(r.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        print(f"spans: publish-batch failed: {exc!r}", file=sys.stderr)
        return None
    accounted = r.returncode in (0, 2) and summary.get("published", 0) + len(summary.get("rejected", [])) == len(records)
    if not accounted:
        print(f"spans: publish-batch exit {r.returncode}: {r.stderr.strip()[:200]}", file=sys.stderr)
    return summary if accounted else None


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


def file_state(c: dict, st: os.stat_result, head: str) -> tuple[int, frozenset]:
    """(offset, done ids) to resume from; (0, empty) when the file is not the one the cursor saw."""
    same = c.get("inode") == st.st_ino and c.get("head") == head and c.get("offset", 0) <= st.st_size
    return (c.get("offset", 0), frozenset(c.get("done", []))) if same else (0, frozenset())


class FileScan:
    """One file's unpublished spans and the cursor entry that matches how many have gone out."""

    def __init__(self, rel: str, st: os.stat_result, head: str, offset: int, done: frozenset, result: tuple):
        self.rel, self.inode, self.head, self.offset, self.done = rel, st.st_ino, head, offset, done
        self.spans, self.next_offset, self.kept = result
        self.sent: set = set()

    def cursor(self) -> dict:
        complete = len(self.sent) >= len(self.spans)
        entry = {"inode": self.inode, "head": self.head}
        if complete:
            return {**entry, "offset": self.next_offset, "done": sorted(self.kept)}
        return {**entry, "offset": self.offset, "done": sorted(self.done | self.sent)}


def open_file(cur: dict, rel: str, path: str) -> FileScan:
    st = os.stat(path)
    head = head_hash(path)
    offset, done = file_state(cur.get(rel) or {}, st, head)
    return FileScan(rel, st, head, offset, done, read_spans(path, rel, offset, done))


def flush(a, nervous: str, batch: list, cur: dict, cursor_path: Path) -> Optional[dict]:
    """Publish one batch, then save the cursor for it. None when the publish must be retried."""
    if a.dry_run:
        for _, s in batch:
            print(json.dumps(public(s)))
        summary: Optional[dict] = {"rejected": []}
    else:
        summary = publish_batch(nervous, [public(s) for _, s in batch])
    if summary is None:
        return None
    for fs, s in batch:
        fs.sent.add(s["span_id"])
        cur[fs.rel] = fs.cursor()
    if not a.dry_run:
        save_cursor(cursor_path, cur)
    return summary


def drain(a, nervous: str, buf: list, cur: dict, cursor_path: Path, full_only: bool) -> Optional[tuple[int, int]]:
    """Flush batches from `buf`; only full batches unless `full_only` is False.
    (published, rejected) counts, or None on failure."""
    published = rejected = 0
    while len(buf) >= (a.batch_size if full_only else 1):
        batch = buf[:a.batch_size]
        summary = flush(a, nervous, batch, cur, cursor_path)
        if summary is None:
            return None
        del buf[:len(batch)]
        rejected += len(summary["rejected"])
        published += len(batch) - len(summary["rejected"])
    return published, rejected


def scan_setup(a) -> tuple:
    """(root, cursor_path, since, nervous) from the scan arguments; clamps the batch size."""
    a.batch_size = max(1, a.batch_size)
    return (a.root or default_root(), Path(a.cursor) if a.cursor else default_cursor(),
            parse_since(a.since) if a.since else 0.0, find_nervous(a.nervous))


def cmd_scan(a) -> int:
    root, cursor_path, since, nervous = scan_setup(a)
    cur = load_cursor(cursor_path)
    published = rejected = 0
    buf = []
    for rel, path in recent_files(root, since):
        try:
            fs = open_file(cur, rel, path)
        except OSError as exc:
            print(f"spans: skipped {rel}: {exc}", file=sys.stderr)
            continue
        cur[rel] = fs.cursor()
        buf.extend((fs, s) for s in fs.spans)
        counts = drain(a, nervous, buf, cur, cursor_path, True)
        if counts is None:
            print(f"spans: stopped after {published} published", file=sys.stderr)
            return 1
        published, rejected = published + counts[0], rejected + counts[1]
    counts = drain(a, nervous, buf, cur, cursor_path, False)
    if counts is None:
        print(f"spans: stopped after {published} published", file=sys.stderr)
        return 1
    published, rejected = published + counts[0], rejected + counts[1]
    if not a.dry_run:
        save_cursor(cursor_path, cur)
    if rejected:
        print(f"spans: WARNING {rejected} spans rejected by the bus and skipped", file=sys.stderr)
    print(json.dumps({"published": published, "rejected": rejected, "dry_run": bool(a.dry_run)}), file=sys.stderr)
    return 0


# ------------------------------------------------------------------ top

def rank(rows: dict, limit: int) -> list[dict]:
    ordered = sorted(rows.items(), key=lambda kv: -kv[1][0])[:limit]
    return [{"key": k, "hours": round(v[0] / 3600, 3), "calls": v[1],
             "avg_s": round(v[0] / max(v[1], 1), 1)} for k, v in ordered]


def tally(rows: dict, key: str, dur: float) -> None:
    rows[key][0] += dur
    rows[key][1] += 1


def top_spans(root: str, since: float, project: Optional[str]) -> Iterator[dict]:
    for rel, path in recent_files(root, since):
        try:
            spans, _, _ = read_spans(path, rel)
        except OSError:
            continue
        for s in spans:
            if s.get("duration_ms") is not None and parse_ts(s["start"]) >= since \
                    and (not project or s["project"] == project):
                yield s


def agent_key(s: dict) -> str:
    who = s.get("label") or s.get("agent_id") or s["session"]
    return f"{s.get('workflow_id', '')} {who}".strip()


def cmd_top(a) -> int:
    since = parse_since(a.since)
    classes, targets, agents = (defaultdict(lambda: [0.0, 0]) for _ in range(3))
    total, n = 0.0, 0
    for s in top_spans(a.root or default_root(), since, a.project):
        dur = s["duration_ms"] / 1000
        n, total = n + 1, total + dur
        tally(classes, s["class"], dur)
        if s["class"] == "wait":
            tally(targets, s["wait_target"], dur)
        tally(agents, agent_key(s), dur)
    res = {"since": a.since, "project": a.project or "*", "calls": n, "tool_hours": round(total / 3600, 3),
           "classes": rank(classes, a.limit), "wait_targets": rank(targets, a.limit),
           "agents": rank(agents, a.limit)}
    print(text_view(res) if a.text else json.dumps(res, indent=2))
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
    sc.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="spans per publish-batch call")
    tp = sub.add_parser("top", help="rank the slowest producers")
    tp.add_argument("--root")
    tp.add_argument("--project")
    tp.add_argument("--since", default="24h")
    tp.add_argument("--limit", type=int, default=10)
    tp.add_argument("--text", action="store_true", help="print a table instead of JSON")
    a = ap.parse_args(argv)
    try:
        return {"scan": cmd_scan, "top": cmd_top}[a.cmd](a)
    except ValueError as exc:
        print(f"spans: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
