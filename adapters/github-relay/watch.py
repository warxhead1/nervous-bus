#!/usr/bin/env python3
"""github-relay -- inbound GitHub Issues ingestion + gated outbound comment relay.

Motivating gap (2026-08-30 estate audit): workflows are covered end to end
(ci-watch polls `gh run list` -> files "CI red:" beads -> tools/maintenance-train
drains them into PRs) but GitHub Issues are ingested NOWHERE, and nothing
posts analysis back to a GitHub Issue. This adapter closes both halves,
following the repo's established adapter shape (roster/config -> persisted
diff state -> transition-only emission -> dedup'd bead filing -> report.md),
mirrored from adapters/ci-watch/watch.py and adapters/system-pressure/watch.py.

Two passes, one poll, one report:

1. **Inbound** -- per repo listed in relay-config.json (`ingest: true`),
   `gh issue list --state all --json number,title,state,labels,author,
   updatedAt,url,body,comments` (PRs are never returned by `gh issue list`
   itself -- verified 2026-08-30 against every roster repo with issues
   enabled; `is_pull_request` stays in the schema as a false-only guard
   against a future API change, and normalize_issue() still filters any
   item whose url contains "/pull/" defensively). Diffed against persisted
   state (~/.cache/nervous-bus/github-relay/state.json); emits
   `bus.github.issue.v1` ONLY on a transition (first-seen-open, closed,
   reopened, a new comment, a label-set change, or a catch-all `updated`
   when gh's updatedAt bumped but none of those matched). A closed issue
   seen for the FIRST time (state file empty / issue predates this
   adapter's ever having polled it) emits nothing -- there is no action
   verb that means anything for a closed backlog item, and firing "opened"
   for 50 already-closed issues on the first-ever poll would be pure noise.
2. **Bead pairing** -- for each currently-OPEN issue on a repo with
   `file_beads: true`, file exactly ONE `gh-issue: <owner>/<repo>#<number>:
   <title>` bead the first time that issue is observed open, tracked via
   `bead_id` in the persisted per-issue state. Never refiled once filed --
   unlike ci-watch's red/green retrigger, a GitHub issue isn't a transient
   failure state; if a human/bot closes the bead as "declined" the issue
   stays open by design and refiling would loop forever.
3. **Outbound** -- `post_issue_comment(repo, number, body, mode)` honors the
   per-repo `outbound` mode (off/dry-run/live). Wired to exactly one
   trigger: a bead this adapter filed gets closed with `external_ref` set
   (mirrors how tools/maintenance-train/finalize.sh stamps
   `bd update <id> --external-ref <pr-url>` after a PR opens) -> post
   "linked PR: <url>" on the originating issue, once, tracked via
   `pr_comment_posted` in state so it never double-posts.

Usage:
    python3 watch.py                 # real run: poll, publish, file beads, comment, write report
    python3 watch.py --dry-run       # compute + report, publish/comment as dry-run only, still files real beads unless --no-beads
    python3 watch.py --no-beads      # never call bd, even for real runs
    python3 watch.py --relay-config fixture.json --state-file /tmp/s.json --no-network  # tests
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CACHE_DIR = Path(os.environ.get("NERVOUS_GITHUB_RELAY_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "github-relay")))
STATE_FILE = CACHE_DIR / "state.json"
REPORT_FILE = CACHE_DIR / "report.md"
SNAPSHOT_FILE = CACHE_DIR / "snapshot.json"
RELAY_CONFIG_FILE = Path(__file__).resolve().parent / "relay-config.json"

NERVOUS_BIN = os.environ.get(
    "NERVOUS_BIN",
    str(Path(__file__).resolve().parent.parent.parent / "sdk" / "shell" / "nervous"),
)

ISSUE_LIMIT = 50
BODY_EXCERPT_MAX = 500
VALID_ACTIONS = {"opened", "closed", "reopened", "commented", "labeled", "assigned", "updated"}
VALID_OUTBOUND_MODES = {"off", "dry-run", "live"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Config (relay-config.json) -- allowlist, fail-closed like
# tools/maintenance-train/repo_config.py: a repo not listed is untouched.
# --------------------------------------------------------------------------

def load_relay_config(path: Path = RELAY_CONFIG_FILE) -> dict:
    with path.open() as f:
        return json.load(f)


def get_repo_config(config: dict, repo: str) -> Optional[dict]:
    """Merged {ingest, file_beads, outbound} for `repo`, or None if the repo
    is not present under `repos` -- out of scope by construction."""
    repos = config.get("repos", {})
    if repo not in repos:
        return None
    merged = dict(config.get("default", {}))
    merged.update(repos[repo] or {})
    merged.setdefault("ingest", True)
    merged.setdefault("file_beads", True)
    merged.setdefault("outbound", "dry-run")
    if merged["outbound"] not in VALID_OUTBOUND_MODES:
        merged["outbound"] = "off"  # unknown mode -- fail closed, never guess "live"
    return merged


# --------------------------------------------------------------------------
# gh wrappers -- isolated so tests can stub them without hitting the network.
# --------------------------------------------------------------------------

def gh_json(args: List[str], timeout: int = 30) -> Optional[object]:
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        sys.stderr.write(f"[github-relay] gh {' '.join(args)} failed: {e}\n")
        return None
    if out.returncode != 0:
        sys.stderr.write(f"[github-relay] gh {' '.join(args)} exit {out.returncode}: {out.stderr.strip()[:300]}\n")
        return None
    try:
        return json.loads(out.stdout) if out.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def fetch_issues(repo: str, limit: int = ISSUE_LIMIT) -> List[dict]:
    data = gh_json([
        "issue", "list", "--repo", repo, "--state", "all", "--limit", str(limit),
        "--json", "number,title,state,labels,author,updatedAt,url,body,comments",
    ])
    return data if isinstance(data, list) else []


def post_issue_comment(repo: str, number: int, body: str, mode: str, *, report_lines: Optional[List[str]] = None) -> bool:
    """Honors the per-repo outbound mode. Returns True if the comment was
    posted (mode=live, gh exit 0) or would-have-been handled without error
    (mode=off/dry-run). Never called for mode outside VALID_OUTBOUND_MODES
    (get_repo_config already normalizes unknown modes to "off")."""
    if mode == "off":
        sys.stderr.write(f"[github-relay] outbound off, skipping comment on {repo}#{number}\n")
        return True
    if mode == "dry-run":
        msg = f"(dry-run) would comment on {repo}#{number}: {body}"
        sys.stderr.write(f"[github-relay] {msg}\n")
        if report_lines is not None:
            report_lines.append(f"- DRY-RUN comment {repo}#{number}: {body}")
        return True
    if mode == "live":
        try:
            out = subprocess.run(
                ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            sys.stderr.write(f"[github-relay] gh issue comment failed for {repo}#{number}: {e}\n")
            return False
        if out.returncode != 0:
            sys.stderr.write(f"[github-relay] gh issue comment exit {out.returncode} for {repo}#{number}: {out.stderr.strip()[:300]}\n")
            return False
        if report_lines is not None:
            report_lines.append(f"- LIVE comment posted {repo}#{number}: {body}")
        return True
    return False  # unreachable given VALID_OUTBOUND_MODES gating upstream


def publish_event(payload: dict, *, dry_run: bool) -> bool:
    if dry_run:
        sys.stderr.write(f"[github-relay] (dry-run) would publish bus.github.issue.v1: {payload}\n")
        return True
    try:
        subprocess.run(
            [NERVOUS_BIN, "publish", "bus.github.issue.v1", json.dumps(payload)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return True
    except Exception as e:
        sys.stderr.write(f"[github-relay] publish failed for {payload.get('repo')}#{payload.get('number')}: {e}\n")
        return False


def bd_json(args: List[str], timeout: int = 30) -> Optional[object]:
    try:
        out = subprocess.run(["bd"] + args + ["--json"], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        sys.stderr.write(f"[github-relay] bd {' '.join(args)} failed: {e}\n")
        return None
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def bd_show_one(bead_id: str) -> Optional[dict]:
    data = bd_json(["show", bead_id])
    if not data:
        return None
    obj = data[0] if isinstance(data, list) and data else data
    return obj if isinstance(obj, dict) else None


def bead_is_closed_with_external_ref(bead_id: str) -> Optional[str]:
    """Returns the external_ref URL if the bead is closed AND has one set
    (mirrors finalize.sh's `bd update --external-ref` stamp after a PR
    opens), else None (bead still open, or closed with no external ref --
    e.g. declined)."""
    obj = bd_show_one(bead_id)
    if not obj:
        return None
    status = str(obj.get("status", "")).lower()
    if status not in ("closed", "done", "resolved"):
        return None
    ref = obj.get("external_ref")
    return ref if isinstance(ref, str) and ref.strip() else None


def bead_title_for_issue(repo: str, number: int, title: str) -> str:
    excerpt = (title or "").strip()
    if len(excerpt) > 80:
        excerpt = excerpt[:77] + "..."
    return f"gh-issue: {repo}#{number}: {excerpt}"


def bead_description_for_issue(repo: str, number: int, url: str, body_excerpt: str) -> str:
    return (
        f"Auto-filed by adapters/github-relay (nervous-bus) for an OPEN GitHub issue.\n\n"
        f"Issue: {url}\n\n"
        f"Body excerpt (truncated to {BODY_EXCERPT_MAX} chars):\n\n"
        f"```\n{body_excerpt}\n```\n\n"
        f"## Acceptance criteria\n\n"
        f"Analyze the issue, then either:\n"
        f"1. Fix it via a PR referencing #{number} (title/body should mention "
        f"\"Fixes {repo}#{number}\" or \"Closes {repo}#{number}\" so GitHub auto-links "
        f"and closes the issue on merge), or\n"
        f"2. Post a triage comment on the issue explaining why it isn't actionable "
        f"as scoped, what's missing, or a decision only a human can make.\n\n"
        f"Close this bead with a disposition (fixed / triaged / declined) once done. "
        f"If a PR opens and this bead is later closed with an external-ref, "
        f"github-relay posts a \"linked PR\" comment back on the issue automatically."
    )


def file_issue_bead(repo: str, number: int, title: str, url: str, body_excerpt: str, *, dry_run: bool) -> Optional[str]:
    bead_title = bead_title_for_issue(repo, number, title)
    description = bead_description_for_issue(repo, number, url, body_excerpt)
    if dry_run:
        sys.stderr.write(f"[github-relay] (dry-run) would file bead: {bead_title}\n")
        return None
    data = bd_json(["create", bead_title, "-t", "task", "-p", "2", "-d", description])
    if not data:
        sys.stderr.write(f"[github-relay] bd create returned no data for {bead_title}\n")
        return None
    obj = data[0] if isinstance(data, list) and data else data
    return obj.get("id") if isinstance(obj, dict) else None


# --------------------------------------------------------------------------
# Pure normalization / transition logic (unit-tested, no network)
# --------------------------------------------------------------------------

def is_pull_request_item(raw: dict) -> bool:
    """Defensive PR filter. `gh issue list` never returns PR-shaped items
    today (verified 2026-08-30: PRs live under `gh pr list`, a disjoint
    endpoint) -- this guards a future API change rather than a currently
    observed case."""
    url = raw.get("url") or ""
    return "/pull/" in url


def truncate_body(body: Optional[str]) -> str:
    body = body or ""
    if len(body) <= BODY_EXCERPT_MAX:
        return body
    return body[:BODY_EXCERPT_MAX]


def normalize_issue(raw: dict, repo: str) -> Optional[dict]:
    """Raw `gh issue list --json ...` item -> normalized dict, or None if
    this item should be filtered out entirely (a PR)."""
    if is_pull_request_item(raw):
        return None
    author = raw.get("author") or {}
    comments = raw.get("comments") or []
    labels = raw.get("labels") or []
    return {
        "repo": repo,
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "state": (raw.get("state") or "").strip().lower(),
        "labels": sorted(l.get("name", "") if isinstance(l, dict) else str(l) for l in labels),
        "author": author.get("login", "") if isinstance(author, dict) else str(author),
        "url": raw.get("url") or "",
        "body_excerpt": truncate_body(raw.get("body")),
        "comment_count": len(comments) if isinstance(comments, list) else 0,
        "updated_at": raw.get("updatedAt") or "",
    }


def classify_issue_transition(prior: Optional[dict], cur: dict) -> Optional[str]:
    """Returns the action verb for this poll, or None if nothing worth
    emitting changed. `prior` is the persisted per-issue state dict (or
    None on first-ever observation)."""
    if prior is None:
        # First-ever observation: only "opened" issues get an event. A
        # closed issue discovered for the first time (pre-dates this
        # adapter's polling, or just fell outside a prior ISSUE_LIMIT
        # window) has no meaningful action verb and would be pure noise
        # backfilled across up to 50 issues per repo per first poll.
        return "opened" if cur["state"] == "open" else None

    prev_state = prior.get("state")
    if prev_state == "open" and cur["state"] == "closed":
        return "closed"
    if prev_state == "closed" and cur["state"] == "open":
        return "reopened"

    if cur["comment_count"] > prior.get("comment_count", 0):
        return "commented"

    if sorted(cur["labels"]) != sorted(prior.get("labels", [])):
        return "labeled"

    if cur["updated_at"] and cur["updated_at"] != prior.get("updated_at"):
        return "updated"

    return None


def build_event_data(cur: dict, action: str) -> dict:
    return {
        "repo": cur["repo"],
        "number": cur["number"],
        "action": action,
        "title": cur["title"],
        "state": cur["state"],
        "labels": cur["labels"],
        "author": cur["author"],
        "url": cur["url"],
        "body_excerpt": cur["body_excerpt"],
        "ts": cur["updated_at"] or iso(),
        "comment_count": cur["comment_count"],
        "is_pull_request": False,
    }


def issue_state_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


# --------------------------------------------------------------------------
# State I/O
# --------------------------------------------------------------------------

def load_state(path: Path = STATE_FILE) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data: Dict[str, dict], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.rename(path)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_relay_pass(relay_config: dict, state: Dict[str, dict], *, fetch_fn, dry_run: bool,
                    no_beads: bool = False, report_lines: Optional[List[str]] = None,
                    now: Optional[float] = None) -> List[dict]:
    """Mutates `state` in place. Returns a list of per-issue result dicts for
    the report. Runs both the inbound pass (poll, diff, emit, file beads)
    and the outbound pass (post "linked PR" comments for beads this adapter
    filed that have since closed with an external ref)."""
    now = now if now is not None else time.time()
    results: List[dict] = []

    for repo in relay_config.get("repos", {}):
        cfg = get_repo_config(relay_config, repo)
        if cfg is None or not cfg.get("ingest", True):
            continue

        raw_issues = fetch_fn(repo, ISSUE_LIMIT)
        for raw in raw_issues:
            cur = normalize_issue(raw, repo)
            if cur is None:
                continue  # PR, filtered

            key = issue_state_key(repo, cur["number"])
            prior = state.get(key)
            action = classify_issue_transition(prior, cur)

            bead_id = prior.get("bead_id") if prior else None
            pr_comment_posted = bool(prior.get("pr_comment_posted")) if prior else False

            if action is not None:
                payload = build_event_data(cur, action)
                publish_event(payload, dry_run=dry_run)

            if cur["state"] == "open" and cfg.get("file_beads", True) and bead_id is None:
                bead_id = file_issue_bead(
                    repo, cur["number"], cur["title"], cur["url"], cur["body_excerpt"],
                    dry_run=dry_run or no_beads,
                )

            if bead_id and not pr_comment_posted:
                ref = None if no_beads else bead_is_closed_with_external_ref(bead_id)
                if ref:
                    posted = post_issue_comment(
                        repo, cur["number"], f"linked PR: {ref}", cfg.get("outbound", "dry-run"),
                        report_lines=report_lines,
                    )
                    pr_comment_posted = posted

            state[key] = {
                "state": cur["state"],
                "labels": cur["labels"],
                "comment_count": cur["comment_count"],
                "updated_at": cur["updated_at"],
                "bead_id": bead_id,
                "pr_comment_posted": pr_comment_posted,
                "polled_at": iso(now),
            }

            results.append({
                "repo": repo, "number": cur["number"], "title": cur["title"],
                "state": cur["state"], "action": action, "bead_id": bead_id,
                "pr_comment_posted": pr_comment_posted, "url": cur["url"],
            })

    return results


def render_report(results: List[dict], relay_config: dict, *,
                   outbound_lines: Optional[List[str]] = None,
                   generated_at: Optional[float] = None) -> str:
    generated_at = generated_at if generated_at is not None else time.time()
    lines = [
        "# github-relay report",
        "",
        f"Generated: {iso(generated_at)}",
        "",
        "## Issues observed this poll",
        "",
        "| repo | # | title | state | action | bead | comment |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda r: (r["repo"], r["number"])):
        action = r.get("action") or ""
        lines.append(
            f"| {r['repo']} | {r['number']} | {r['title'][:60]} | {r['state']} | "
            f"{action} | {r.get('bead_id') or ''} | "
            f"{'posted' if r.get('pr_comment_posted') else ''} |"
        )
    lines.append("")

    lines.append("## Repo config")
    lines.append("")
    lines.append("| repo | ingest | file_beads | outbound |")
    lines.append("|---|---|---|---|")
    for repo in sorted(relay_config.get("repos", {})):
        cfg = get_repo_config(relay_config, repo) or {}
        lines.append(f"| {repo} | {cfg.get('ingest')} | {cfg.get('file_beads')} | {cfg.get('outbound')} |")
    lines.append("")

    if outbound_lines:
        lines.append("## Outbound activity this poll")
        lines.append("")
        lines.extend(outbound_lines)
        lines.append("")

    return "\n".join(lines)


def build_snapshot(results: List[dict], *, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    return {
        "ts": iso(now),
        "issues": [
            {"repo": r["repo"], "number": r["number"], "title": r["title"],
             "state": r["state"], "bead_id": r.get("bead_id")}
            for r in results
        ],
    }


def run(*, relay_config_path: Path = RELAY_CONFIG_FILE, state_path: Path = STATE_FILE,
        report_path: Path = REPORT_FILE, snapshot_path: Path = SNAPSHOT_FILE,
        dry_run: bool = False, no_beads: bool = False, no_network: bool = False,
        fetch_fn=None) -> List[dict]:
    relay_config = load_relay_config(relay_config_path)
    state = load_state(state_path)
    fetch_fn = fetch_fn or fetch_issues

    if no_network:
        raise RuntimeError("no_network=True requires fetch_fn stub")

    outbound_lines: List[str] = []
    results = run_relay_pass(
        relay_config, state, fetch_fn=fetch_fn, dry_run=dry_run, no_beads=no_beads,
        report_lines=outbound_lines,
    )

    # A dry run must not consume transitions: persisting state here would make
    # the next REAL run see "no change" for issues the dry run merely printed
    # (bit 2026-08-31: three live hearthsite issues were baselined away).
    if not dry_run:
        save_state(state, state_path)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results, relay_config, outbound_lines=outbound_lines))

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = snapshot_path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(build_snapshot(results), f, indent=2, sort_keys=True)
    tmp.rename(snapshot_path)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="github-relay -- GitHub Issues inbound relay + gated outbound comments")
    parser.add_argument("--relay-config", type=Path, default=RELAY_CONFIG_FILE)
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--report-file", type=Path, default=REPORT_FILE)
    parser.add_argument("--snapshot-file", type=Path, default=SNAPSHOT_FILE)
    parser.add_argument("--dry-run", action="store_true", help="compute + report, never publish, comment, or file beads")
    parser.add_argument("--no-beads", action="store_true", help="never call bd, even on a real run")
    args = parser.parse_args()

    results = run(
        relay_config_path=args.relay_config, state_path=args.state_file,
        report_path=args.report_file, snapshot_path=args.snapshot_file,
        dry_run=args.dry_run, no_beads=args.no_beads,
    )

    transitions = [r for r in results if r.get("action")]
    sys.stderr.write(
        f"[github-relay] evaluated {len(results)} issues: {len(transitions)} transition(s), "
        f"report={args.report_file}\n"
    )
    for r in transitions:
        sys.stderr.write(f"[github-relay] {r['action']} {r['repo']}#{r['number']} bead={r.get('bead_id')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
