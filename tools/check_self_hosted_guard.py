#!/usr/bin/env python3
"""Fail when a job on a self-hosted runner can be started by a fork's pull request.

A pull request from a fork runs the fork's code. On a self-hosted runner that
code runs on our own machine. Every job with `self-hosted` in `runs-on` must
either carry the same-repo condition in its `if:`, or sit in a workflow with no
pull_request trigger. `pull_request_target` with a self-hosted job always fails.

usage: check_self_hosted_guard.py [workflow dir]
"""
import re
import sys
from pathlib import Path

GUARD = "github.event.pull_request.head.repo.full_name == github.repository"
JOB = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")


def problems(text):
    lines = text.splitlines()
    head = "\n".join(l for l in lines if not l.startswith(" "))
    in_jobs, job, body, jobs = False, None, [], {}
    for l in lines:
        if re.match(r"^jobs:\s*$", l):
            in_jobs = True
            continue
        if in_jobs and (m := JOB.match(l)):
            job, body = m.group(1), []
            jobs[job] = body
        elif in_jobs and job:
            body.append(l)
    triggers = text.split("\njobs:")[0]
    on_pr = bool(re.search(r"^\s*pull_request:|^on:.*pull_request", triggers, re.M))
    on_target = "pull_request_target" in triggers
    out = []
    for name, body in jobs.items():
        b = "\n".join(body)
        if not re.search(r"runs-on:.*self-hosted", b):
            continue
        if on_target:
            out.append(f"{name}: self-hosted job in a pull_request_target workflow")
        elif on_pr and GUARD not in b:
            out.append(f"{name}: self-hosted job has no same-repo condition")
    return out


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else ".github/workflows")
    bad = [f"{p.name}: {m}" for p in sorted(root.glob("*.y*ml")) for m in problems(p.read_text(encoding="utf-8"))]
    for b in bad:
        print(b)
    if bad:
        print(f"Add to the job: if: github.event_name != 'pull_request' || {GUARD}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
