#!/usr/bin/env python3
"""lessons: record and query agent.lesson.v1 lessons.

  lessons record --project P --area A --lesson "one sentence" --evidence "..." [--rule "..."] [--supersedes les_xxx]
      Append the lesson to the local store, then publish agent.lesson.v1.
  lessons query [--project P] [--area A] [--all] [--text]
      Newest first. Lessons that a newer lesson supersedes are hidden unless --all.

The store is one JSON line per lesson at $NERVOUS_LESSONS_FILE, default
$XDG_STATE_HOME/nervous-bus/lessons/lessons.jsonl. Exit codes: 0 ok, 1 publish failed
(the lesson is stored), 2 bad input.
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
from pathlib import Path

CHANNEL = "agent.lesson.v1"
REPO_NERVOUS = Path(__file__).resolve().parents[2] / "sdk" / "shell" / "nervous"
AREA_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def store_path() -> Path:
    env = os.environ.get("NERVOUS_LESSONS_FILE")
    if env:
        return Path(env)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "nervous-bus" / "lessons" / "lessons.jsonl"


def load(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
    except OSError:
        pass
    return out


def fail(msg: str) -> int:
    print(f"lessons: {msg}", file=sys.stderr)
    return 2


def find_nervous(explicit):
    return explicit or os.environ.get("NERVOUS_BIN") or (str(REPO_NERVOUS) if REPO_NERVOUS.is_file() else None) \
        or shutil.which("nervous") or "nervous"


def cmd_record(a) -> int:
    for name in ("project", "lesson", "evidence"):
        if not getattr(a, name).strip():
            return fail(f"--{name} is empty")
    if not AREA_RE.match(a.area) or len(a.area) > 60:
        return fail("--area must be lowercase letters, digits, '.', '_' or '-'")
    if len(a.lesson) > 300 or len(a.evidence) > 300 or len(a.rule or "") > 300:
        return fail("a lesson is short: --lesson, --evidence and --rule are 300 characters at most")
    if "\n" in a.lesson:
        return fail("--lesson is one sentence on one line")
    path = store_path()
    existing = load(path)
    if a.supersedes and not any(d.get("lesson_id") == a.supersedes for d in existing):
        return fail(f"--supersedes {a.supersedes} is not in the store")
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = {"v": 1, "ts": ts,
           "lesson_id": "les_" + hashlib.sha256(f"{ts}\0{a.project}\0{a.lesson}".encode()).hexdigest()[:12],
           "project": a.project, "area": a.area, "lesson": a.lesson.strip(), "evidence": a.evidence.strip()}
    if a.rule:
        rec["rule"] = a.rule.strip()
    if a.supersedes:
        rec["supersedes"] = a.supersedes
    sid = os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        rec["session_id"] = sid
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    try:
        r = subprocess.run([find_nervous(a.nervous), "publish", CHANNEL, json.dumps(rec, separators=(",", ":"))],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NERVOUS_SOURCE="/lessons"))
        ok, err = r.returncode == 0, r.stderr.strip()[:200]
    except (OSError, subprocess.TimeoutExpired) as exc:
        ok, err = False, str(exc)
    print(json.dumps(rec))
    if not ok:
        print(f"lessons: stored, publish failed: {err}", file=sys.stderr)
        return 1
    return 0


def cmd_query(a) -> int:
    rows = load(store_path())
    replaced = {d["supersedes"] for d in rows if d.get("supersedes")}
    rows = [d for d in rows if (a.all or d.get("lesson_id") not in replaced)
            and (not a.project or d.get("project") == a.project)
            and (not a.area or d.get("area") == a.area)]
    rows.sort(key=lambda d: d.get("ts", ""), reverse=True)
    if a.text:
        for d in rows:
            print(f"{d['lesson_id']} {d['project']}/{d['area']}: {d['lesson']}")
            print(f"    evidence: {d['evidence']}")
            if d.get("rule"):
                print(f"    rule: {d['rule']}")
    else:
        print(json.dumps(rows, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lessons", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="store and publish one lesson")
    r.add_argument("--project", required=True)
    r.add_argument("--area", required=True)
    r.add_argument("--lesson", required=True)
    r.add_argument("--evidence", required=True)
    r.add_argument("--rule", default="")
    r.add_argument("--supersedes", default="")
    r.add_argument("--nervous")
    q = sub.add_parser("query", help="list lessons")
    q.add_argument("--project")
    q.add_argument("--area")
    q.add_argument("--all", action="store_true", help="include superseded lessons")
    q.add_argument("--text", action="store_true")
    a = ap.parse_args(argv)
    return {"record": cmd_record, "query": cmd_query}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
