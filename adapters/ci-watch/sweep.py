#!/usr/bin/env python3
"""Cancel CI runs that nobody is waiting for: runs of a closed pull request, and
runs of a commit the pull request has moved past.

A merged PR leaves its queued runs in front of main on a single self-hosted
runner. A workflow cannot fix that on a private repo without hosted minutes: on
the same runner it queues behind the runs it should cancel. This runs on the
host, on a timer, and needs no runner.

Per roster repo it lists the queued and in-progress runs, keeps the ones a
pull_request event started, and looks up the open PRs of each head branch. A run
is cancelled when its branch has no open PR, or when no open PR of the branch
still points at the run's commit. Push runs, schedules and manual runs are never
touched.

Usage:
    sweep.py              cancel and print one line per cancelled run
    sweep.py --dry-run    print what would be cancelled
A roster entry with "sweep": false is skipped.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROSTER_FILE = Path(__file__).with_name("roster.json")
ACTIVE = ("queued", "in_progress")


def gh_api(path, method="GET"):
    """Parsed JSON from `gh api`, or None on any failure."""
    try:
        out = subprocess.run(["gh", "api", "-X", method, path], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout) if out.stdout.strip() else {}
    except ValueError:
        return None


def active_pr_runs(repo, api=gh_api):
    runs = {}
    for status in ACTIVE:
        page = api(f"repos/{repo}/actions/runs?status={status}&event=pull_request&per_page=100")
        for r in (page or {}).get("workflow_runs", []):
            runs[r["id"]] = r
    return list(runs.values())


def stale(run, open_heads):
    """Why this run has no reader, or None. open_heads: head SHAs of the branch's open PRs,
    or None when the lookup failed (then nothing is cancelled)."""
    if open_heads is None:
        return None
    if not open_heads:
        return "pull request closed"
    if run.get("head_sha") not in open_heads:
        return "pull request moved to a newer commit"
    return None


def sweep_repo(repo, dry_run=False, api=gh_api):
    """[(run id, workflow, branch, reason)] for the runs cancelled, or that would be."""
    out, heads = [], {}
    for run in active_pr_runs(repo, api):
        branch = run.get("head_branch")
        owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login")
        if not branch or not owner:
            continue
        key = f"{owner}:{branch}"
        if key not in heads:
            prs = api(f"repos/{repo}/pulls?state=open&head={key}&per_page=100")
            heads[key] = None if prs is None else {p["head"]["sha"] for p in prs}
        why = stale(run, heads[key])
        if not why:
            continue
        if not dry_run and api(f"repos/{repo}/actions/runs/{run['id']}/cancel", "POST") is None:
            continue
        out.append((run["id"], run.get("name") or "", branch, why))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--roster", type=Path, default=ROSTER_FILE)
    ap.add_argument("--repo", action="append", help="sweep only this repo (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    repos = a.repo or [r["repo"] for r in json.loads(a.roster.read_text(encoding="utf-8"))["repos"]
                       if r.get("sweep", True)]
    total = 0
    for repo in repos:
        for run_id, workflow, branch, why in sweep_repo(repo, a.dry_run):
            total += 1
            print(f"{'would cancel' if a.dry_run else 'cancelled'} {repo} run {run_id} ({workflow}, {branch}): {why}")
    print(f"ci-sweep: {total} run(s) {'to cancel' if a.dry_run else 'cancelled'} across {len(repos)} repo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
