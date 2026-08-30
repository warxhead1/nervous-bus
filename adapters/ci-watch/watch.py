#!/usr/bin/env python3
"""ci-watch — deterministic CI/CD pipeline observability + project-contract lint.

Motivating incidents (measured, 2026-08-30, `gh run list` sample against real
warxhead1 repos): red MAINS nobody was watching. shader-garden "Test battery"
failing 3 days straight, deer-flow "Frontend Unit Tests"/"Unit Tests"/"E2E
Tests" red on main, tengine "cpu-extended" red, career-ops 3 red workflows
(Release Please, CodeQL Analysis, gfi-claims), orca "Node next compatibility"
red, hearth CI showing "skipped" on main (root cause: `if: false` hardcoded
in .github/workflows/ci.yml, not a path filter -- see contract lint output).
Nothing in the bus or beads previously surfaced this; it was discoverable only
by manually running `gh run list` per repo. This adapter closes that gap.

Two independent passes share one run and one report:

1. **Pipeline status** — per roster repo, `gh run list` on the watched
   branch(es), diffed against persisted prior state
   (~/.cache/nervous-bus/ci-watch/state.json). Emits `ci.pipeline.status.v1`
   ONLY on a state transition (green->red, red->green, first-seen-red) or a
   persistent-red reminder at most once per 24h -- never per-poll spam for a
   workflow that has been red for a week. A main-branch workflow red for
   >=2 consecutive runs or >6h auto-files ONE dedup'd triage bead in this
   repo (`bd create`), the queue a triage agent sweeps later.

2. **Contract lint** — per roster repo: does .github/workflows/ exist? do
   tests actually run on push/PR to the default branch (parses `on:` triggers
   + job `if:` conditions, not just presence of a workflow file -- a workflow
   that structurally exists but is gated `if: false` or filtered to paths
   that never touch that branch is NOT running tests)? is CLAUDE.md or
   AGENTS.md present at the repo root? Reads the LOCAL checkout under
   /home/eric/projects/<name> (read-only) for the workflow YAML and root
   files -- this adapter's write scope is its own worktree + ~/.cache + bd
   beads; it never mutates another project's checkout.

Both passes write into one human-readable ~/.cache/nervous-bus/ci-watch/report.md.

Usage:
    python3 watch.py                    # real run: poll, publish transitions, file beads, write report
    python3 watch.py --dry-run          # compute + report, never publish or file beads
    python3 watch.py --roster fixture.json --state-file /tmp/s.json --no-network  # tests
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CACHE_DIR = Path(os.environ.get("NERVOUS_CI_WATCH_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "ci-watch")))
STATE_FILE = CACHE_DIR / "state.json"
REPORT_FILE = CACHE_DIR / "report.md"
ROSTER_FILE = Path(__file__).resolve().parent / "roster.json"
PROJECTS_ROOT = Path(os.environ.get("NERVOUS_CI_WATCH_PROJECTS_ROOT", str(Path.home() / "projects")))

NERVOUS_BIN = os.environ.get(
    "NERVOUS_BIN",
    str(Path(__file__).resolve().parent.parent.parent / "sdk" / "shell" / "nervous"),
)

# How many recent runs to sample per repo. gh run list is not filterable by
# workflow server-side in one cheap call across all workflows, so one
# `gh run list --limit N` per repo (not per workflow) keeps this at one API
# round trip per repo per poll, as instructed.
RUN_LIMIT = 20

# Re-alert on a workflow that has been red every poll since the last alert,
# at most this often. Matches the staleness adapter's cooldown convention.
PERSISTENT_RED_REMINDER_S = 24 * 3600.0

# Auto-file a triage bead once a default-branch workflow has been red for at
# least this many consecutive (most-recent, in-window) runs...
BEAD_MIN_CONSECUTIVE_FAILURES = 2
# ...OR has been continuously red for at least this long, whichever comes first.
BEAD_MIN_RED_DURATION_S = 6 * 3600.0

# Never let a run's failing-log excerpt (or anything else derived from a run)
# leak a credential onto the bus/into a bead body.
SECRET_PATTERN = re.compile(r"(token|key|password|authorization|secret)", re.IGNORECASE)
LOG_TAIL_LINES = 30

GREEN = "green"
RED = "red"
SKIPPED = "skipped"
NO_RUNS = "no-runs"
PENDING = "pending"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: Optional[float] = None) -> str:
    ts = ts if ts is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def redact(text: str) -> str:
    """Drop any line that looks like it might carry a credential.

    Conservative: whole-line drop rather than partial-mask, since a partial
    mask has bitten before (base64 body spilling past a narrow cut). Applied
    to failing-job-log excerpts before they ever reach a bead body.
    """
    out = []
    for line in text.splitlines():
        if SECRET_PATTERN.search(line):
            out.append("[ci-watch: line redacted -- matched secret-ish pattern]")
        else:
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# gh wrappers -- isolated so tests can monkeypatch/stub without hitting the
# network (fixtures feed classify_run()/compute_workflow_state() directly).
# --------------------------------------------------------------------------

def gh_json(args: List[str], timeout: int = 30) -> Optional[object]:
    try:
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        sys.stderr.write(f"[ci-watch] gh {' '.join(args)} failed: {e}\n")
        return None
    if out.returncode != 0:
        sys.stderr.write(f"[ci-watch] gh {' '.join(args)} exit {out.returncode}: {out.stderr.strip()[:300]}\n")
        return None
    try:
        return json.loads(out.stdout) if out.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def fetch_default_branch(repo: str) -> Optional[str]:
    data = gh_json(["repo", "view", repo, "--json", "defaultBranchRef"])
    if not data:
        return None
    ref = data.get("defaultBranchRef") or {}
    return ref.get("name")


def fetch_runs(repo: str, limit: int = RUN_LIMIT) -> List[dict]:
    data = gh_json([
        "run", "list", "-R", repo,
        "--json", "workflowName,conclusion,headBranch,headSha,updatedAt,url,databaseId",
        "--limit", str(limit),
    ])
    return data if isinstance(data, list) else []


def fetch_failing_log(repo: str, database_id: int) -> str:
    try:
        out = subprocess.run(
            ["gh", "run", "view", str(database_id), "-R", repo, "--log-failed"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return f"(failed to fetch log: {e})"
    text = out.stdout if out.stdout.strip() else out.stderr
    lines = text.splitlines()[-LOG_TAIL_LINES:]
    return redact("\n".join(lines))


def publish_event(payload: dict, *, dry_run: bool) -> bool:
    if dry_run:
        sys.stderr.write(f"[ci-watch] (dry-run) would publish ci.pipeline.status.v1: {payload}\n")
        return True
    try:
        subprocess.run(
            [NERVOUS_BIN, "publish", "ci.pipeline.status.v1", json.dumps(payload)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return True
    except Exception as e:
        sys.stderr.write(f"[ci-watch] publish failed for {payload.get('repo')}/{payload.get('workflow')}: {e}\n")
        return False


def bd_json(args: List[str], timeout: int = 30) -> Optional[object]:
    try:
        out = subprocess.run(["bd"] + args + ["--json"], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        sys.stderr.write(f"[ci-watch] bd {' '.join(args)} failed: {e}\n")
        return None
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def bead_is_open(bead_id: str) -> bool:
    data = bd_json(["show", bead_id])
    if not data:
        return False
    # bd show --json returns either a single object or a list of one.
    obj = data[0] if isinstance(data, list) and data else data
    if not isinstance(obj, dict):
        return False
    status = str(obj.get("status", "")).lower()
    return status not in ("closed", "done", "resolved", "cancelled")


def file_triage_bead(repo: str, workflow: str, run_url: str, head_sha: str,
                      log_excerpt: str, *, dry_run: bool) -> Optional[str]:
    title = f"CI red: {repo}/{workflow}"
    description = (
        f"Auto-filed by adapters/ci-watch (nervous-bus). Workflow '{workflow}' on "
        f"{repo} has been red on its default branch past the trigger threshold "
        f"(>= {BEAD_MIN_CONSECUTIVE_FAILURES} consecutive failing runs or "
        f">{BEAD_MIN_RED_DURATION_S/3600:.0f}h continuously red).\n\n"
        f"Run: {run_url}\nCommit: {head_sha}\n\n"
        f"Last {LOG_TAIL_LINES} lines of the failing job log "
        f"(gh run view --log-failed; secret-pattern lines redacted):\n\n"
        f"```\n{log_excerpt}\n```\n"
    )
    if dry_run:
        sys.stderr.write(f"[ci-watch] (dry-run) would file bead: {title}\n")
        return None
    data = bd_json(["create", title, "-t", "bug", "-p", "2", "-d", description])
    if not data:
        sys.stderr.write(f"[ci-watch] bd create returned no data for {title}\n")
        return None
    obj = data[0] if isinstance(data, list) and data else data
    return obj.get("id") if isinstance(obj, dict) else None


# --------------------------------------------------------------------------
# Roster
# --------------------------------------------------------------------------

def load_roster(path: Path = ROSTER_FILE) -> dict:
    with path.open() as f:
        return json.load(f)


def is_ignored(workflow_name: str, global_ignore: List[str], repo_ignore: List[str]) -> bool:
    name = (workflow_name or "").lower()
    for pat in list(global_ignore) + list(repo_ignore):
        if pat.lower() in name:
            return True
    return False


# --------------------------------------------------------------------------
# Pipeline status computation
# --------------------------------------------------------------------------

def classify_run(conclusion: Optional[str]) -> str:
    c = (conclusion or "").strip().lower()
    if c == "":
        return PENDING
    if c == "success":
        return GREEN
    if c == "skipped":
        return SKIPPED
    # failure, cancelled, timed_out, action_required, neutral, stale, startup_failure...
    return RED


def group_runs(runs: List[dict], branches: List[str]) -> Dict[Tuple[str, str], List[dict]]:
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for r in runs:
        branch = r.get("headBranch")
        if branch not in branches:
            continue
        key = (r.get("workflowName") or "unknown", branch)
        groups.setdefault(key, []).append(r)
    for key in groups:
        groups[key].sort(key=lambda r: r.get("updatedAt") or "", reverse=True)
    return groups


def compute_workflow_state(run_group: List[dict], prior: dict) -> dict:
    """Compute this poll's verdict for one (workflow, branch) from its sampled
    runs (newest-first) plus the persisted prior state.

    consecutive_failures = length of the leading run of RED entries in the
    sampled window (stops at the first green/skipped/pending). If the whole
    sampled window is red, the persisted red_since (if any, and if earlier)
    is preserved so a streak longer than RUN_LIMIT doesn't appear to "restart"
    every poll once it scrolls out of the window.
    """
    if not run_group:
        return {
            "state": NO_RUNS,
            "run_url": prior.get("run_url"),
            "head_sha": prior.get("head_sha"),
            "database_id": None,
            "consecutive_failures": 0,
            "red_since": None,
        }

    top = run_group[0]
    state = classify_run(top.get("conclusion"))

    consecutive_failures = 0
    oldest_red_updated_at = None
    for r in run_group:
        if classify_run(r.get("conclusion")) != RED:
            break
        consecutive_failures += 1
        oldest_red_updated_at = r.get("updatedAt")

    red_since = None
    if state == RED:
        red_since = oldest_red_updated_at
        window_fully_red = consecutive_failures == len(run_group)
        prior_red_since = prior.get("red_since")
        if window_fully_red and prior_red_since:
            prior_ts, cur_ts = parse_iso(prior_red_since), parse_iso(red_since)
            if prior_ts is not None and cur_ts is not None and prior_ts < cur_ts:
                red_since = prior_red_since

    return {
        "state": state,
        "run_url": top.get("url"),
        "head_sha": top.get("headSha"),
        "database_id": top.get("databaseId"),
        "consecutive_failures": consecutive_failures,
        "red_since": red_since,
    }


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


def state_key(repo: str, workflow: str, branch: str) -> str:
    return f"{repo}|{workflow}|{branch}"


def run_pipeline_pass(roster: dict, state: Dict[str, dict], *, default_branch_fn, run_fetch_fn,
                       log_fetch_fn, dry_run: bool, now: Optional[float] = None) -> List[dict]:
    """Returns a list of per-workflow result dicts (for the report), and
    mutates `state` in place with the freshest verdict for every observed
    (repo, workflow, branch), publishing/filing beads as a side effect
    unless dry_run.
    """
    now = now if now is not None else time.time()
    global_ignore = roster.get("default_ignore_workflows", [])
    results: List[dict] = []

    for entry in roster.get("repos", []):
        repo = entry["repo"]
        repo_ignore = entry.get("ignore_workflows", [])
        branches = entry.get("branches") or []
        if not branches:
            db = default_branch_fn(repo)
            branches = [db] if db else []
        if not branches:
            sys.stderr.write(f"[ci-watch] {repo}: could not resolve default branch, skipping\n")
            continue

        runs = run_fetch_fn(repo, RUN_LIMIT)
        groups = group_runs(runs, branches)

        for (workflow, branch), group in groups.items():
            if is_ignored(workflow, global_ignore, repo_ignore):
                continue

            key = state_key(repo, workflow, branch)
            prior = state.get(key, {})
            computed = compute_workflow_state(group, prior)

            if computed["state"] == PENDING:
                # Never overwrite last-known-concluded state with an
                # in-progress run, and never emit for it.
                results.append({
                    "repo": repo, "workflow": workflow, "branch": branch,
                    "state": prior.get("state", PENDING), "displayed_as_pending_run": True,
                    "run_url": computed["run_url"], "consecutive_failures": prior.get("consecutive_failures", 0),
                    "red_since": prior.get("red_since"), "bead_id": prior.get("bead_id"),
                })
                continue

            prev_state = prior.get("state", "unknown")
            cur_state = computed["state"]
            is_transition = cur_state != prev_state

            reason = None
            if is_transition:
                reason = "transition"
            elif cur_state == RED:
                last_notified = parse_iso(prior.get("last_notified_at"))
                if last_notified is None or (now - last_notified) >= PERSISTENT_RED_REMINDER_S:
                    reason = "persistent-red-reminder"

            bead_id = prior.get("bead_id")
            if bead_id and not bead_is_open(bead_id):
                bead_id = None  # prior bead closed -- free to refile if it recurs

            if cur_state == RED and not bead_id:
                should_file = (
                    computed["consecutive_failures"] >= BEAD_MIN_CONSECUTIVE_FAILURES
                    or (parse_iso(computed["red_since"]) is not None
                        and (now - parse_iso(computed["red_since"])) >= BEAD_MIN_RED_DURATION_S)
                )
                if should_file:
                    log_excerpt = log_fetch_fn(repo, computed["database_id"]) if computed["database_id"] else "(no run id)"
                    bead_id = file_triage_bead(
                        repo, workflow, computed["run_url"] or "", computed["head_sha"] or "",
                        log_excerpt, dry_run=dry_run,
                    )
            elif cur_state != RED:
                bead_id = None  # recovered -- clear so a future new red streak can refile

            if reason:
                payload = {
                    "repo": repo, "workflow": workflow, "branch": branch,
                    "state": cur_state, "prev_state": prev_state,
                    "run_url": computed["run_url"], "head_sha": computed["head_sha"],
                    "red_since": computed["red_since"],
                    "consecutive_failures": computed["consecutive_failures"],
                    "reason": reason,
                    "bead_id": bead_id,
                }
                publish_event(payload, dry_run=dry_run)
                if not dry_run:
                    state.setdefault(key, {})["last_notified_at"] = iso(now)
                    prior = state[key]

            entry_state = state.setdefault(key, {})
            entry_state.update({
                "state": cur_state,
                "run_url": computed["run_url"],
                "head_sha": computed["head_sha"],
                "consecutive_failures": computed["consecutive_failures"],
                "red_since": computed["red_since"],
                "bead_id": bead_id,
                "updated_at": iso(now),
            })
            if "last_notified_at" not in entry_state:
                entry_state["last_notified_at"] = None

            results.append({
                "repo": repo, "workflow": workflow, "branch": branch,
                "state": cur_state, "consecutive_failures": computed["consecutive_failures"],
                "red_since": computed["red_since"], "run_url": computed["run_url"],
                "bead_id": bead_id, "transitioned": is_transition,
            })

    return results


# --------------------------------------------------------------------------
# Contract lint
# --------------------------------------------------------------------------

def repo_local_dir(repo: str) -> Path:
    name = repo.split("/", 1)[-1]
    return PROJECTS_ROOT / name


def lint_repo(repo: str, projects_root: Path = PROJECTS_ROOT) -> dict:
    local = projects_root / repo.split("/", 1)[-1]
    result = {
        "repo": repo,
        "local_checkout": str(local),
        "has_workflows_dir": False,
        "workflow_files": [],
        "tests_run_on_main": None,  # True/False/None (couldn't determine)
        "tests_run_notes": "",
        "has_claude_or_agents_md": False,
        "contract_files": [],
    }

    if not local.is_dir():
        result["tests_run_notes"] = "local checkout not found under PROJECTS_ROOT"
        return result

    for name in ("CLAUDE.md", "AGENTS.md"):
        if (local / name).is_file():
            result["contract_files"].append(name)
    result["has_claude_or_agents_md"] = bool(result["contract_files"])

    wf_dir = local / ".github" / "workflows"
    if not wf_dir.is_dir():
        result["tests_run_notes"] = "no .github/workflows directory"
        return result
    result["has_workflows_dir"] = True

    try:
        import yaml  # local import: only needed for lint, keep pipeline pass dependency-free
    except ImportError:
        result["tests_run_notes"] = "PyYAML not available, cannot parse workflow triggers"
        return result

    notes = []
    any_test_trigger = False
    for wf_path in sorted(wf_dir.glob("*.y*ml")):
        try:
            doc = yaml.safe_load(wf_path.read_text())
        except Exception as e:
            notes.append(f"{wf_path.name}: YAML parse error ({e})")
            continue
        if not isinstance(doc, dict):
            continue
        result["workflow_files"].append(wf_path.name)

        on = doc.get("on") or doc.get(True)  # PyYAML parses bare `on:` key as boolean True in some configs
        triggers = set()
        if isinstance(on, dict):
            triggers = set(on.keys())
        elif isinstance(on, list):
            triggers = set(on)
        elif isinstance(on, str):
            triggers = {on}

        has_push_or_pr = bool({"push", "pull_request"} & triggers)
        if not has_push_or_pr:
            continue

        jobs = doc.get("jobs") or {}
        looks_like_test = any(
            re.search(r"test|ci|build|lint|check", str(j.get("name", jid)), re.IGNORECASE)
            for jid, j in jobs.items() if isinstance(j, dict)
        ) or bool(jobs)

        disabled_jobs = []
        for jid, j in jobs.items():
            if not isinstance(j, dict):
                continue
            if str(j.get("if", "")).strip().lower() == "false":
                disabled_jobs.append(jid)

        if disabled_jobs:
            notes.append(f"{wf_path.name}: job(s) {disabled_jobs} hardcoded `if: false` -- structurally disabled, not path-filtered")
        elif has_push_or_pr and looks_like_test:
            any_test_trigger = True

        # Path filters that could starve a job on branches that never touch
        # those paths -- surfaced as a note, not a hard fail (a path filter is
        # a legitimate choice; it's only a problem if it silently explains an
        # "all runs skipped" pattern this adapter observed).
        push_cfg = on.get("push") if isinstance(on, dict) else None
        if isinstance(push_cfg, dict) and push_cfg.get("paths"):
            notes.append(f"{wf_path.name}: push trigger has path filters {push_cfg['paths']}")

    result["tests_run_on_main"] = any_test_trigger
    result["tests_run_notes"] = "; ".join(notes) if notes else "ok"
    return result


def run_lint_pass(roster: dict, projects_root: Path = PROJECTS_ROOT) -> List[dict]:
    return [lint_repo(entry["repo"], projects_root) for entry in roster.get("repos", [])]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def render_report(pipeline_results: List[dict], lint_results: List[dict], *, generated_at: Optional[float] = None) -> str:
    generated_at = generated_at if generated_at is not None else time.time()
    lines = [
        "# ci-watch report",
        "",
        f"Generated: {iso(generated_at)}",
        "",
        "## Pipeline status",
        "",
        "| repo | workflow | branch | state | consecutive_failures | red_since | bead | run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(pipeline_results, key=lambda r: (0 if r["state"] == RED else 1, r["repo"], r["workflow"])):
        run_link = f"[link]({r['run_url']})" if r.get("run_url") else ""
        lines.append(
            f"| {r['repo']} | {r['workflow']} | {r['branch']} | {r['state']} | "
            f"{r.get('consecutive_failures', 0)} | {r.get('red_since') or ''} | "
            f"{r.get('bead_id') or ''} | {run_link} |"
        )
    lines.append("")

    lines.append("## Contract lint scorecard")
    lines.append("")
    lines.append("| repo | .github/workflows | tests run on main | CLAUDE.md/AGENTS.md | notes |")
    lines.append("|---|---|---|---|---|")
    for r in sorted(lint_results, key=lambda r: r["repo"]):
        lines.append(
            f"| {r['repo']} | {'yes' if r['has_workflows_dir'] else 'NO'} | "
            f"{'yes' if r['tests_run_on_main'] else ('NO' if r['tests_run_on_main'] is False else 'unknown')} | "
            f"{'yes (' + ','.join(r['contract_files']) + ')' if r['has_claude_or_agents_md'] else 'NO'} | "
            f"{r['tests_run_notes']} |"
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(*, roster_path: Path = ROSTER_FILE, state_path: Path = STATE_FILE, report_path: Path = REPORT_FILE,
        projects_root: Path = PROJECTS_ROOT, dry_run: bool = False, no_network: bool = False,
        run_fetch_fn=None, default_branch_fn=None, log_fetch_fn=None) -> Tuple[List[dict], List[dict]]:
    roster = load_roster(roster_path)
    state = load_state(state_path)

    run_fetch_fn = run_fetch_fn or fetch_runs
    default_branch_fn = default_branch_fn or fetch_default_branch
    log_fetch_fn = log_fetch_fn or fetch_failing_log

    if no_network:
        raise RuntimeError("no_network=True requires run_fetch_fn/default_branch_fn/log_fetch_fn stubs")

    pipeline_results = run_pipeline_pass(
        roster, state, default_branch_fn=default_branch_fn, run_fetch_fn=run_fetch_fn,
        log_fetch_fn=log_fetch_fn, dry_run=dry_run,
    )
    lint_results = run_lint_pass(roster, projects_root=projects_root)

    save_state(state, state_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(pipeline_results, lint_results))

    return pipeline_results, lint_results


def main() -> int:
    parser = argparse.ArgumentParser(description="ci-watch — CI/CD pipeline observability + contract lint")
    parser.add_argument("--roster", type=Path, default=ROSTER_FILE)
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--report-file", type=Path, default=REPORT_FILE)
    parser.add_argument("--projects-root", type=Path, default=PROJECTS_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="compute + report, never publish or file beads")
    args = parser.parse_args()

    pipeline_results, lint_results = run(
        roster_path=args.roster, state_path=args.state_file, report_path=args.report_file,
        projects_root=args.projects_root, dry_run=args.dry_run,
    )

    red = [r for r in pipeline_results if r["state"] == RED]
    sys.stderr.write(
        f"[ci-watch] evaluated {len(pipeline_results)} workflow/branch pairs across "
        f"{len(load_roster(args.roster).get('repos', []))} repos: red={len(red)} report={args.report_file}\n"
    )
    for r in red:
        sys.stderr.write(f"[ci-watch] RED {r['repo']}/{r['workflow']}@{r['branch']} (n={r.get('consecutive_failures')}) bead={r.get('bead_id')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
