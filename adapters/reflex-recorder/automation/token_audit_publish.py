#!/usr/bin/env python3
"""token_audit_publish.py — host-side egress for the daily mmx token-audit.

The MiniMax auditor runs read-only and can only write a report. This script
is the one deterministic gate between that report and the work queue:

  1. parse the LAST fenced ```json block in the report ({"proposal": {...}});
  2. validate it against the digest it was given (latest.json): candidate_id
     must exist, rung must be eliminate|automate|inform, the claimed saving must
     not exceed the candidate's measured IET, and every quoted evidence row must
     appear verbatim in the digest markdown;
  3. refuse duplicates: same title ever, or same candidate_id within
     CANDIDATE_COOLDOWN_DAYS (its effect is not readable yet);
  4. file a bead (nervous-bus .beads, label token-efficiency) and, with
     --gh-repo, a GitHub issue (default: the PRIVATE claude-hook-fast repo,
     never the public nervous-bus repo);
  5. append the proposal + the candidate's IET at filing to proposals.jsonl,
     which token_report.py reads back to show whether the fix moved the number.

Rejections are written next to the report as <report>.rejected.txt and exit 2.

    token_audit_publish.py --report R.md [--digest-dir D] [--gh-repo OWNER/NAME | --no-gh] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DIGEST_DIR = Path.home() / ".cache" / "nervous-bus" / "reflex" / "token-digest"
BEADS_REPO = Path.home() / "projects" / "nervous-bus"
DEFAULT_GH_REPO = "warxhead1/claude-hook-fast"
RUNGS = {"eliminate", "automate", "inform"}
# A filed candidate needs a week of data before its effect is readable, and
# MiniMax rephrases titles freely: refuse the same candidate within this window.
CANDIDATE_COOLDOWN_DAYS = 7
_FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.S)


class Rejected(Exception):
    pass


def extract(report: str) -> dict:
    blocks = _FENCE.findall(report)
    if not blocks:
        raise Rejected("no ```json block in report")
    try:
        prop = json.loads(blocks[-1])["proposal"]
    except (ValueError, KeyError, TypeError) as e:
        raise Rejected(f"last json block is not {{\"proposal\": ...}}: {e}")
    if not isinstance(prop, dict):
        raise Rejected("proposal is not an object")
    return prop


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def validate(prop: dict, digest: dict, digest_md: str, prior: list[dict],
             today: date | None = None) -> dict:
    cands = {c["id"]: c for c in digest.get("candidates", [])}
    cid = prop.get("candidate_id")
    if cid not in cands:
        raise Rejected(f"candidate_id {cid!r} not in digest ({sorted(cands)})")
    cutoff = (today or date.today()) - timedelta(days=CANDIDATE_COOLDOWN_DAYS)
    for pp in prior:
        if pp.get("candidate_id") == cid and str(pp.get("filed", "")) > cutoff.isoformat():
            raise Rejected(f"candidate {cid!r} already filed on {pp['filed']} (cooldown "
                           f"{CANDIDATE_COOLDOWN_DAYS}d): {pp.get('title', '')!r}")
    prior_titles = {str(pp.get("title", "")).strip().lower() for pp in prior}
    title = str(prop.get("title") or "").strip()
    if not title or len(title) > 90:
        raise Rejected("title missing or longer than 90 chars")
    if title.lower() in prior_titles:
        raise Rejected(f"duplicate of an earlier proposal: {title!r}")
    if prop.get("rung") not in RUNGS:
        raise Rejected(f"rung must be one of {sorted(RUNGS)}")
    saved = prop.get("expected_iet_saved_per_week")
    if not isinstance(saved, int) or saved <= 0:
        raise Rejected("expected_iet_saved_per_week must be a positive integer")
    if saved > cands[cid]["iet"]:
        raise Rejected(f"claimed saving {saved:,} exceeds the candidate's measured {cands[cid]['iet']:,} IET")
    evidence = prop.get("evidence") or []
    if not evidence:
        raise Rejected("no evidence rows")
    md = _norm(digest_md)
    for row in evidence:
        if _norm(str(row).strip("` ")) not in md:
            raise Rejected(f"evidence row not found verbatim in the digest: {str(row)[:160]}")
    for key in ("mechanism", "verify"):
        if not str(prop.get(key) or "").strip():
            raise Rejected(f"{key} missing")
    return cands[cid]


def _body(prop: dict, cand: dict, report: Path) -> str:
    ev = "\n".join(f"- `{e}`" for e in prop["evidence"])
    return (f"Filed by the daily mmx token-audit (report: {report}).\n\n"
            f"**Candidate:** {prop['candidate_id']} — {cand['iet']:,} IET over the digest window "
            f"({cand['share_of_total']:.1%} of total).\n\n"
            f"**Rung:** {prop['rung']}\n\n**Mechanism:** {prop['mechanism']}\n\n"
            f"**Expected saving:** {prop['expected_iet_saved_per_week']:,} IET/week\n\n"
            f"**Evidence (digest rows):**\n{ev}\n\n**Verify after landing:** {prop['verify']}\n")


def publish(report_path: Path, digest_dir: Path, gh_repo: str | None, dry_run: bool) -> dict:
    report = report_path.read_text()
    digest = json.loads((digest_dir / "latest.json").read_text())
    digest_md = (digest_dir / "latest.md").read_text()
    ledger = digest_dir / "proposals.jsonl"
    prior: list[dict] = []
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                prior.append(json.loads(line))
            except ValueError:
                continue
    prop = extract(report)
    cand = validate(prop, digest, digest_md, prior)
    title = f"[token-audit] {prop['title']}"
    body = _body(prop, cand, report_path)
    rec = {"filed": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "title": prop["title"],
           "candidate_id": prop["candidate_id"], "candidate_iet": cand["iet"],
           "rung": prop["rung"], "expected_iet_saved_per_week": prop["expected_iet_saved_per_week"],
           "report": str(report_path)}
    if dry_run:
        rec["dry_run"] = True
        return rec
    if gh_repo:
        out = subprocess.run(["gh", "issue", "create", "-R", gh_repo, "--title", title, "--body", body],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"gh issue create failed: {out.stderr.strip()[:300]}")
        rec["issue"] = out.stdout.strip()
    bead_desc = (f"Mirror of {rec['issue']}\n\n" if rec.get("issue") else "") + body
    out = subprocess.run(["bd", "create", title, "-p", "2", "-l", "nervous-bus,token-efficiency",
                          "-d", bead_desc, "--json"],
                         cwd=BEADS_REPO, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"bd create failed: {out.stderr.strip()[:300]}")
    try:
        rec["bead"] = json.loads(out.stdout).get("id")
    except ValueError:
        rec["bead"] = out.stdout.strip()[:40]
    with open(ledger, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def latest_report(root: Path = Path("/home/eric/data2/reports/mmx-automations")) -> Path:
    found = sorted(root.glob("*/nervous-bus-token-audit.md"))
    if not found:
        raise FileNotFoundError(f"no nervous-bus-token-audit.md under {root}")
    return found[-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", type=Path, help="default: newest mmx-automations token-audit report")
    ap.add_argument("--digest-dir", type=Path, default=DIGEST_DIR)
    ap.add_argument("--gh-repo", default=DEFAULT_GH_REPO)
    ap.add_argument("--no-gh", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    report = a.report or latest_report()
    try:
        rec = publish(report, a.digest_dir, None if a.no_gh else a.gh_repo, a.dry_run)
    except Rejected as e:
        Path(f"{report}.rejected.txt").write_text(f"REJECTED: {e}\n")
        print(f"token-audit-publish: REJECTED {report}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
