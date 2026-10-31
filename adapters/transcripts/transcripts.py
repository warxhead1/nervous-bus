#!/usr/bin/env python3
"""transcripts — read every coding agent's session files as one stream of tool calls.

  transcripts sources                          which agents, where they store sessions, what exists here
  transcripts sessions [--agent A] [--days N]  sessions per agent
  transcripts calls [--agent A] [--days N] [--shell] [--errors] [--jsonl]
                                               normalized tool calls (JSONL with --jsonl)

Extra roots per agent: NBUS_TRANSCRIPTS_<AGENT>=dir1<pathsep>dir2 (globs allowed).
Library use: sys.path.insert(0, <this dir>); from transcripts import iter_calls
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from sources import SOURCES  # noqa: E402


def pick(agents=None):
    names = agents or list(SOURCES)
    unknown = [a for a in names if a not in SOURCES]
    if unknown:
        raise SystemExit(f"unknown agent(s) {unknown}; known: {', '.join(SOURCES)}")
    return [SOURCES[a] for a in names]


def iter_sessions(agents=None, since: float = 0.0):
    for src in pick(agents):
        yield from src.sessions(since)


def iter_calls(agents=None, since: float = 0.0, shell_only: bool = False):
    """Every tool call in sessions touched since `since`, oldest session first per agent."""
    for src in pick(agents):
        for ref in sorted(src.sessions(since), key=lambda r: r.mtime):
            for tc in src.tool_calls(ref):
                if not shell_only or tc.shell:
                    yield tc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="transcripts", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources")
    for name in ("sessions", "calls"):
        p = sub.add_parser(name)
        p.add_argument("--agent", action="append")
        p.add_argument("--days", type=float, default=1)
        if name == "calls":
            p.add_argument("--shell", action="store_true", help="shell commands only")
            p.add_argument("--errors", action="store_true", help="failed calls only")
            p.add_argument("--jsonl", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "sources":
        for src in SOURCES.values():
            print(f"{src.name:10} {src.label}")
            for r in src.roots():
                print(f"{'':10}   {'found  ' if os.path.isdir(r) else 'missing'} {r}")
            print(f"{'':10}   live hooks: {src.live_hooks or 'none (post-hoc only)'}")
        return 0
    since = time.time() - a.days * 86400
    if a.cmd == "sessions":
        counts: dict[str, int] = {}
        for ref in iter_sessions(a.agent, since):
            counts[ref.agent] = counts.get(ref.agent, 0) + 1
        for k, v in sorted(counts.items()):
            print(f"{k:10} {v}")
        return 0
    n = 0
    for tc in iter_calls(a.agent, since, a.shell):
        if a.errors and not tc.is_error:
            continue
        n += 1
        if a.jsonl:
            print(json.dumps(dataclasses.asdict(tc)))
        else:
            flag = "HARNESS" if tc.harness else ("ERR" if tc.is_error else "ok")
            what = tc.command if tc.shell else tc.tool
            print(f"{tc.agent:7} {flag:7} {str(tc.exit_code if tc.exit_code is not None else ''):>3} "
                  f"{what.splitlines()[0][:110] if what else ''}")
    if not a.jsonl:
        print(f"-- {n} calls", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
