#!/usr/bin/env python3
"""steering_audit_publish.py — the deterministic gate between the steering-audit
workflow and the work queue.

The workflow's judge proposes edits to steering (CLAUDE.md chains, memory-global,
skills, agent definitions). Nothing mechanical can say whether a proposed cut is
WISE, but a lot can say whether it is GROUNDED. This gate refuses a proposal
unless:

  1. rung is eliminate|automate|inform and action is a known verb;
  2. target.path exists under ~/.claude or ~/projects, target.line_start..line_end
     is in range, and target.excerpt appears (whitespace-normalized) in those
     lines — the judge read the file it is proposing to change;
  3. every guidance quote appears verbatim (whitespace-normalized) in the
     fetched snapshot of a known source — no paraphrased "the docs say";
  4. the title was never filed before, and the same target file was not filed
     within COOLDOWN_DAYS (its effect is not readable yet).

Accepted proposals become one GitHub issue in the repo that owns the target
file plus a bead labelled `<project>,steering-audit`, and are appended to
proposals.jsonl with the target file's line count at filing, which
steering_audit.py reads back to show whether the file actually shrank.

    steering_audit_publish.py --proposals P.json [--dry-run] [--no-gh]

Exit 0 when every proposal was accepted, 2 when any was rejected (the rest
still file); per-proposal verdicts print as JSON lines.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
DIGEST_DIR = HOME / ".cache" / "nervous-bus" / "reflex" / "steering-digest"
GUIDANCE_DIR = HOME / ".cache" / "nervous-bus" / "reflex" / "guidance"
ALLOWED_ROOTS = (HOME / ".claude", HOME / "projects")
CLAUDE_HOME_REPO = "warxhead1/claude-memory-global"
RUNGS = {"eliminate", "automate", "inform"}
ACTIONS = {"cut", "move", "convert-to-hook", "convert-to-skill", "rewrite", "add", "split"}
COOLDOWN_DAYS = 7
MAX_EXCERPT_LINES = 400


class Rejected(Exception):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _resolve(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise Rejected(f"target.path must be absolute or ~-relative: {raw!r}")
    return p


def _allowed(p: Path, roots=ALLOWED_ROOTS) -> bool:
    for cand in (p, p.resolve()):
        if any(cand == r or r in cand.parents for r in roots):
            return True
    return False


def validate(prop: dict, gdir: Path, prior: list[dict], roots=ALLOWED_ROOTS,
             today: date | None = None) -> dict:
    title = str(prop.get("title") or "").strip()
    if not title or len(title) > 90:
        raise Rejected("title missing or longer than 90 chars")
    if title.lower() in {str(p.get("title", "")).strip().lower() for p in prior}:
        raise Rejected(f"duplicate of an earlier proposal: {title!r}")
    if prop.get("rung") not in RUNGS:
        raise Rejected(f"rung must be one of {sorted(RUNGS)}")
    if prop.get("action") not in ACTIONS:
        raise Rejected(f"action must be one of {sorted(ACTIONS)}")
    t = prop.get("target") or {}
    path = _resolve(str(t.get("path") or ""))
    if not _allowed(path, roots):
        raise Rejected(f"target outside {[str(r) for r in roots]}: {path}")
    if not path.is_file():
        raise Rejected(f"target does not exist: {path}")
    lines = path.read_text(errors="replace").splitlines()
    a, b = t.get("line_start"), t.get("line_end", t.get("line_start"))
    if not (isinstance(a, int) and isinstance(b, int) and 1 <= a <= b <= len(lines)):
        raise Rejected(f"target lines {a}..{b} out of range 1..{len(lines)}")
    if b - a + 1 > MAX_EXCERPT_LINES:
        raise Rejected(f"target span {b - a + 1} lines exceeds {MAX_EXCERPT_LINES}")
    excerpt = str(t.get("excerpt") or "")
    if len(_norm(excerpt)) < 8 or _norm(excerpt) not in _norm("\n".join(lines[a - 1:b])):
        raise Rejected(f"target.excerpt not found in {path}:{a}-{b}")
    cutoff = ((today or date.today()) - timedelta(days=COOLDOWN_DAYS)).isoformat()
    for pp in prior:
        if pp.get("target_path") == str(path) and str(pp.get("filed", "")) > cutoff:
            raise Rejected(f"{path} already has a proposal filed {pp['filed']} "
                           f"(cooldown {COOLDOWN_DAYS}d): {pp.get('title', '')!r}")
    guidance = prop.get("guidance") or []
    if not guidance:
        raise Rejected("no guidance quotes: a steering proposal must cite the source it follows")
    for g in guidance:
        snap = gdir / "sources" / f"{g.get('source')}.md"
        if not snap.is_file():
            raise Rejected(f"unknown guidance source {g.get('source')!r}")
        q = _norm(str(g.get("quote") or ""))
        if len(q) < 20 or q not in _norm(snap.read_text()):
            raise Rejected(f"quote not found verbatim in {g.get('source')}: {str(g.get('quote'))[:120]!r}")
    for key in ("rationale", "edit", "verify"):
        if not str(prop.get(key) or "").strip():
            raise Rejected(f"{key} missing")
    return {"path": path, "lines": len(lines)}


def owning_repo(path: Path) -> tuple[str | None, str, Path]:
    """(gh 'owner/name', project label, cwd for bd) for a steering file."""
    # Resolve symlinks: ~/.claude/skills/<x> points into ~/projects/agent-skills.
    out = subprocess.run(["git", "-C", str(path.resolve().parent), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=20)
    if out.returncode != 0:  # ~/.claude/CLAUDE.md etc.: not in a repo
        return CLAUDE_HOME_REPO, "claude-memory-global", HOME
    root = Path(out.stdout.strip())
    url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"],
                         capture_output=True, text=True, timeout=20).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    slug = m.group(1) if m else None
    return slug, (slug.split("/")[-1] if slug else root.name), root


def _body(prop: dict, path: Path, run: str) -> str:
    t = prop["target"]
    g = "\n".join(f"- **{x['source']}**: \"{x['quote'].strip()}\"" for x in prop["guidance"])
    measured = "\n".join(f"- {m}" for m in prop.get("measured") or []) or "- (none cited)"
    return (f"Filed by the `steering-audit` workflow ({run}).\n\n"
            f"**Target:** `{path}`:{t['line_start']}-{t.get('line_end', t['line_start'])}\n\n"
            f"**Rung:** {prop['rung']} · **Action:** {prop['action']}\n\n"
            f"**Guidance (verbatim from the fetched source):**\n{g}\n\n"
            f"**Measured here:**\n{measured}\n\n**Why:** {prop['rationale']}\n\n"
            f"**Proposed edit:**\n\n```\n{prop['edit'].strip()}\n```\n\n"
            f"**Verify after landing:** {prop['verify']}\n")


def _ledger(d: Path) -> list[dict]:
    p = d / "proposals.jsonl"
    out = []
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def publish_one(prop: dict, gdir: Path, ddir: Path, prior: list[dict], run: str,
                dry_run: bool, use_gh: bool) -> dict:
    v = validate(prop, gdir, prior)
    path = v["path"]
    repo, project, bd_cwd = owning_repo(path)
    title = f"[steering-audit] {prop['title']}"
    body = _body(prop, path, run)
    rec = {"filed": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "title": prop["title"],
           "target_path": str(path), "target_lines_at_filing": v["lines"], "rung": prop["rung"],
           "action": prop["action"], "repo": repo, "project": project, "run": run}
    if dry_run:
        return {**rec, "dry_run": True}
    if use_gh and repo:
        out = subprocess.run(["gh", "issue", "create", "-R", repo, "--title", title, "--body", body],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"gh issue create failed: {out.stderr.strip()[:300]}")
        rec["issue"] = out.stdout.strip()
    desc = (f"Mirror of {rec['issue']}\n\n" if rec.get("issue") else "") + body
    out = subprocess.run(["bd", "create", title, "-p", "2", "-l", f"{project},steering-audit",
                          "-d", desc, "--json"], cwd=bd_cwd, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"bd create failed: {out.stderr.strip()[:300]}")
    # bd can print tips around the JSON; take the id wherever it appears.
    m = re.search(r'"id":\s*"([^"]+)"', out.stdout)
    rec["bead"] = m.group(1) if m else out.stdout.strip()[:40]
    with open(ddir / "proposals.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    prior.append(rec)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--proposals", type=Path, required=True,
                    help='JSON file: {"run": "...", "proposals": [...]}')
    ap.add_argument("--digest-dir", type=Path, default=DIGEST_DIR)
    ap.add_argument("--guidance-dir", type=Path, default=GUIDANCE_DIR)
    ap.add_argument("--no-gh", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    doc = json.loads(a.proposals.read_text())
    a.digest_dir.mkdir(parents=True, exist_ok=True)
    prior = _ledger(a.digest_dir)
    rejected = 0
    for prop in doc.get("proposals", []):
        try:
            rec = publish_one(prop, a.guidance_dir, a.digest_dir, prior, str(doc.get("run", "manual")),
                              a.dry_run, not a.no_gh)
            print(json.dumps({"verdict": "accepted", **rec}))
        except Rejected as e:
            rejected += 1
            print(json.dumps({"verdict": "rejected", "title": prop.get("title"), "reason": str(e)}))
    return 2 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
