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
AREA_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
LIMITS = {"project": 100, "area": 60, "lesson": 300, "evidence": 300, "rule": 300, "supersedes": 17}


def store_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return Path(os.environ.get("NERVOUS_LESSONS_FILE") or state / "nervous-bus" / "lessons" / "lessons.jsonl")


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


def validate(a) -> str:
    """The first problem with the record fields, or ''. Strips the fields in place."""
    for name in LIMITS:
        setattr(a, name, (getattr(a, name, "") or "").strip())
    for name in ("project", "area", "lesson", "evidence"):
        if not getattr(a, name):
            return f"--{name} is empty"
    if not AREA_RE.match(a.area):
        return "--area must be lowercase letters, digits, '.', '_' or '-'"
    if any(len(getattr(a, k)) > n for k, n in LIMITS.items() if k != "supersedes"):
        return "a lesson is short: --project 100, --area 60, --lesson, --evidence and --rule 300 characters at most"
    if "\n" in a.lesson:
        return "--lesson is one sentence on one line"
    return ""


def publish(nervous: str, rec: dict) -> tuple[bool, str]:
    try:
        r = subprocess.run([nervous, "publish", CHANNEL, json.dumps(rec, separators=(",", ":"))],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NERVOUS_SOURCE="/lessons"))
        return r.returncode == 0, r.stderr.strip()[:200]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def make_record(a) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = {"v": 1, "ts": ts,
           "lesson_id": "les_" + hashlib.sha256(f"{ts}\0{a.project}\0{a.lesson}".encode()).hexdigest()[:12],
           "project": a.project, "area": a.area, "lesson": a.lesson, "evidence": a.evidence}
    extra = {"rule": a.rule, "supersedes": a.supersedes, "session_id": (os.environ.get("CLAUDE_SESSION_ID") or "")[:200]}
    rec.update({k: v for k, v in extra.items() if v})
    return rec


def cmd_record(a) -> int:
    problem = validate(a)
    if problem:
        return fail(problem)
    path = store_path()
    if a.supersedes and not any(d.get("lesson_id") == a.supersedes for d in load(path)):
        return fail(f"--supersedes {a.supersedes} is not in the store")
    rec = make_record(a)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    ok, err = publish(find_nervous(a.nervous), rec)
    print(json.dumps(rec))
    if not ok:
        print(f"lessons: stored, publish failed: {err}", file=sys.stderr)
    return 0 if ok else 1


def matches(d: dict, a) -> bool:
    return (not a.project or d.get("project") == a.project) and (not a.area or d.get("area") == a.area)


def cmd_query(a) -> int:
    rows = load(store_path())
    replaced = {d["supersedes"] for d in rows if d.get("supersedes")}
    rows = [d for d in rows if (a.all or d.get("lesson_id") not in replaced) and matches(d, a)]
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
