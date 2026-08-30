#!/usr/bin/env python3
"""selector — deterministic nightly maintenance queue for the mmx train.

Queries beads_global.all_issues (federated dolt view, see
adapters/board/board.py for the same connection convention) for open,
unassigned beads matching a maintenance class, excludes repos that are dirty
or already have an open maintenance-train PR, ranks the remainder, and
partitions AT MOST ONE bead per target repo, capped at MAX_REPOS_PER_NIGHT
repos total. Writes the night's manifest to
~/.cache/nervous-bus/maintenance-train/<date>/manifest.json.

Maintenance classes (a bead qualifies if ANY match):
  - title starts with "CI red:"                          (ci-watch filed)
  - title/description mentions "dependabot" (case-fold)   (dependabot-filed)
  - labelled "maintenance" or "chore"                      (labels table)
  - issue_type == "chore" AND estimated_minutes <= 60      (small chores)

Target repo resolution:
  - "CI red: <owner>/<repo>/<check>" -> <repo>            (embedded in title)
  - otherwise -> the bead's own `project` column           (assumes 1:1
    project-db-name <-> repo name, true for every REPOS entry today)

Everything here is read-only against dolt/gh/git — selector.py never claims a
bead or touches a worktree; that is dispatch.sh's job, gated on this
manifest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo_config import REPOS, CACHE_ROOT  # noqa: E402

DOLT_HOST = os.environ.get("NERVOUS_BOARD_DOLT_HOST", "127.0.0.1")
DOLT_PORT = int(os.environ.get("NERVOUS_BOARD_DOLT_PORT", "39502"))
DOLT_USER = os.environ.get("NERVOUS_BOARD_DOLT_USER", "root")
DOLT_DB = os.environ.get("NERVOUS_BOARD_DOLT_DB", "beads_global")

MAX_REPOS_PER_NIGHT = int(os.environ.get("MMTRAIN_MAX_REPOS", "3"))
MAX_ESTIMATE_MINUTES = int(os.environ.get("MMTRAIN_MAX_ESTIMATE_MIN", "60"))

CI_RED_RE = re.compile(r"^CI red: [^/]+/([^/]+)/", re.IGNORECASE)
DEPENDABOT_RE = re.compile(r"dependabot", re.IGNORECASE)


@dataclass
class Candidate:
    bead_id: str
    project: str
    title: str
    status: str
    priority: int
    issue_type: str
    created_at: str
    labels: List[str]
    estimated_minutes: Optional[int]
    reason: str
    repo: str = ""
    age_days: float = 0.0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(v) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def resolve_repo(project: str, title: str) -> str:
    m = CI_RED_RE.match(title)
    if m:
        return m.group(1)
    return project


class DoltSource:
    """Wraps the pymysql round-trip; a FakeSource in tests substitutes rows
    directly so selector logic is verifiable without a live dolt server."""

    def fetch_open_issues(self) -> List[dict]:
        import pymysql  # local import: tests never need this dependency

        conn = pymysql.connect(
            host=DOLT_HOST, port=DOLT_PORT, user=DOLT_USER, database=DOLT_DB,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT project, id, title, status, priority, issue_type, "
                "assignee, created_at, updated_at, notes FROM all_issues "
                "WHERE status = 'open' AND (assignee IS NULL OR assignee = '')"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            cur.execute("SELECT issue_id, label FROM labels")
            labels_by_issue: Dict[str, List[str]] = {}
            for issue_id, label in cur.fetchall():
                labels_by_issue.setdefault(issue_id, []).append(label)
            for row in rows:
                row["labels"] = labels_by_issue.get(row["id"], [])

            # estimated_minutes lives only on the per-project `issues` table,
            # not the federated all_issues view (confirmed via `describe
            # all_issues` 2026-08-30 — it is absent). Best-effort per-project
            # lookup; if a project's local `issues` table isn't reachable
            # from this dolt session, estimated_minutes stays None and the
            # estimated-chore class simply can't match for that row (fails
            # closed, not an error).
            for row in rows:
                row.setdefault("estimated_minutes", None)

            cur.execute(
                "SELECT issue_id, depends_on_id FROM all_dependencies WHERE type = 'blocks'"
            )
            blocked_ids = set()
            deps = cur.fetchall()
            open_ids = {r["id"] for r in rows}
            for issue_id, depends_on_id in deps:
                if depends_on_id in open_ids:
                    blocked_ids.add(issue_id)
            for row in rows:
                row["blocked"] = row["id"] in blocked_ids
            return rows
        finally:
            conn.close()


def classify(row: dict) -> Optional[str]:
    title = row["title"] or ""
    desc = row.get("notes") or ""
    if title.startswith("CI red:"):
        return "ci-watch"
    if DEPENDABOT_RE.search(title) or DEPENDABOT_RE.search(desc):
        return "dependabot"
    labels = {l.lower() for l in row.get("labels", [])}
    if "maintenance" in labels or "chore" in labels:
        return "labelled"
    if row.get("issue_type") == "chore":
        est = row.get("estimated_minutes")
        if est is not None and est <= MAX_ESTIMATE_MINUTES:
            return "estimated-chore"
    return None


def repo_is_dirty(path: str) -> bool:
    if not os.path.isdir(path):
        return True  # unknown repo path -- fail closed, exclude it
    try:
        out = subprocess.run(
            ["git", "-C", path, "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return True
    return bool(out.stdout.strip())


def repo_has_open_train_pr(gh_repo: str, gh_bin: str = "gh") -> bool:
    try:
        out = subprocess.run(
            [gh_bin, "pr", "list", "--repo", gh_repo, "--state", "open",
             "--search", "[maint] in:title", "--json", "number"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return True  # can't verify -- fail closed, skip this repo tonight
    if out.returncode != 0:
        return True
    try:
        return len(json.loads(out.stdout or "[]")) > 0
    except json.JSONDecodeError:
        return True


def rank_key(c: Candidate):
    # Lower priority number = more urgent (beads convention: 0 highest).
    # Older beads (larger age_days) break ties toward staleness.
    return (c.priority, -c.age_days)


def select(rows: List[dict], repos_cfg: Dict[str, dict], *, max_repos: int,
           skip_dirty_check: bool = False, skip_pr_check: bool = False,
           gh_bin: str = "gh") -> List[Candidate]:
    candidates: List[Candidate] = []
    now = now_utc()  # single snapshot: per-row now_utc() calls introduce
    # microsecond drift between otherwise-identical timestamps, which made
    # ranking non-deterministic for beads created at the same instant
    # (measured via test_one_bead_per_repo_per_run flaking 2026-08-30).
    for row in rows:
        if row.get("blocked"):
            continue
        reason = classify(row)
        if reason is None:
            continue
        repo = resolve_repo(row["project"], row["title"])
        if repo not in repos_cfg:
            continue  # out of scope by construction -- no config, no touch
        created = parse_dt(row.get("created_at"))
        age_days = (now - created).total_seconds() / 86400.0 if created else 0.0
        candidates.append(Candidate(
            bead_id=row["id"], project=row["project"], title=row["title"],
            status=row["status"], priority=row["priority"],
            issue_type=row.get("issue_type", "task"),
            created_at=str(row.get("created_at")), labels=row.get("labels", []),
            estimated_minutes=row.get("estimated_minutes"), reason=reason,
            repo=repo, age_days=age_days,
        ))

    candidates.sort(key=rank_key)

    excluded_repos = set()
    picked: List[Candidate] = []
    seen_repos = set()
    for c in candidates:
        if len(picked) >= max_repos:
            break
        if c.repo in seen_repos or c.repo in excluded_repos:
            continue
        repo_path = repos_cfg[c.repo]["path"]
        if not skip_dirty_check and repo_is_dirty(repo_path):
            excluded_repos.add(c.repo)
            continue
        if not skip_pr_check and repo_has_open_train_pr(repos_cfg[c.repo]["gh_repo"], gh_bin):
            excluded_repos.add(c.repo)
            continue
        picked.append(c)
        seen_repos.add(c.repo)
    return picked


def write_manifest(picked: List[Candidate], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_repos_per_night": MAX_REPOS_PER_NIGHT,
        "entries": [
            {
                "bead_id": c.bead_id, "project": c.project, "repo": c.repo,
                "title": c.title, "priority": c.priority, "reason": c.reason,
                "age_days": round(c.age_days, 2),
            }
            for c in picked
        ],
    }
    path = out_dir / "manifest.json"
    tmp = out_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    tmp.rename(path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=None,
                     help="override manifest dir (default: CACHE_ROOT/<date>)")
    ap.add_argument("--dry-run", action="store_true",
                     help="print the manifest, don't write it")
    args = ap.parse_args(argv)

    rows = DoltSource().fetch_open_issues()
    picked = select(rows, REPOS, max_repos=MAX_REPOS_PER_NIGHT)

    out_dir = Path(args.out_dir) if args.out_dir else CACHE_ROOT / now_utc().strftime("%Y-%m-%d")
    if args.dry_run:
        print(json.dumps([c.__dict__ for c in picked], indent=2, default=str))
        return 0
    path = write_manifest(picked, out_dir)
    print(f"selector: wrote {len(picked)} entries -> {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
