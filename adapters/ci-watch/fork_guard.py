#!/usr/bin/python3
"""Runner job-started hook: refuse a job whose code comes from another repository.

The runner calls this before any step of a job, with the webhook payload at
GITHUB_EVENT_PATH. A non-zero exit fails the job before it runs. The script
lives on the host, so a pull request cannot edit it.

Refused: a pull request event whose head repository is not the base
repository, and a workflow_run event started from another repository. A head
repository that is gone counts as a fork. A payload that cannot be read is
refused too.

Stdlib only, absolute interpreter: the runner's job PATH is not the login PATH.
"""
import json
import os
import sys


def same(a, b):
    """True when two repository objects from a payload name the same repository."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("id") is not None and b.get("id") is not None:
        return a["id"] == b["id"]
    name = a.get("full_name")
    return bool(name) and name == b.get("full_name")


def verdict(event):
    """(allowed, reason) for one webhook payload."""
    if not isinstance(event, dict):
        return False, "the event payload is not an object"
    pr = event.get("pull_request")
    if isinstance(pr, dict):
        head = (pr.get("head") or {}).get("repo")
        base = (pr.get("base") or {}).get("repo") or event.get("repository")
        if not same(head, base):
            return False, "pull request from another repository"
        return True, "pull request from this repository"
    run = event.get("workflow_run")
    if isinstance(run, dict):
        if not same(run.get("head_repository"), event.get("repository")):
            return False, "workflow_run started from another repository"
        return True, "workflow_run from this repository"
    return True, "not a pull request event"


def main():
    path = os.environ.get("GITHUB_EVENT_PATH")
    try:
        with open(path, encoding="utf-8") as f:
            allowed, reason = verdict(json.load(f))
    except Exception as e:
        allowed, reason = False, f"cannot read the event payload ({type(e).__name__})"
    print(f"fork-guard: {'allowed' if allowed else 'REFUSED'} ({reason})")
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
