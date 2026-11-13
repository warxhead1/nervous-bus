#!/usr/bin/env python3
"""steering_verdicts.py — the deterministic half of the steering loop. No model.

For every rule in the rules registry (rules.toml), derive one verdict from
measured detector/theme hit rates around the rule's change points (the
`remediation` query), plus a budget finding for the always-loaded chain and a
judge-due decision. Output goes to the steering digest dir as
`verdicts-latest.{json,md}` plus a dated copy, written atomically (temp file +
rename) so a failed run never leaves a truncated or empty file behind.

Verdicts (first match wins):

  unmeasurable       the rule declares no detector/theme target, or none of its
                     targets has EVER produced a hit in runs.db: nothing here can
                     tell whether the rule works.
  promote_candidate  inform-rung rule whose target still recurs in the recent
                     window: a remembered rule that is not holding, so the next
                     rung (automate/eliminate) is the proposal.
  cut_candidate      the target is demonstrably measurable and a completed
                     before/after window shows no improvement: zero hits before
                     and after (`zero_before_after`), or hits that did not fall
                     (`no_effect`).
  keep               a measured improvement, or no completed window to judge yet
                     (`window_open` / `no_change_point`).

Correlational, like `remediation`: other changes in the same window confound it.
A candidate is a prompt for the judge (automation/workflows/steering-audit.js),
never an edit.

    steering_verdicts.py [--db D] [--tree T] [--out-dir D] [--window-days 14]
                         [--recent-days 14] [--json] [--no-write]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path


import query
import steering_audit

# Issue #99 cadence: judge weekly, or sooner when the chain grew >= 5% since the
# last audit.
JUDGE_MAX_AGE_DAYS = 7
JUDGE_CHAIN_GROWTH = 0.05
DIGEST_HEALTH_DAYS = 14


# ------------------------------------------------------------------ io

def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so readers see the old file or the whole new one,
    never a truncated one. Refuses to replace a file with empty content."""
    if not text:
        raise ValueError(f"refusing to write empty content to {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------- measurement

def _targets(rule: dict) -> list[tuple[str, str, str | None]]:
    """(label, detector, signature) as query_remediation_effect labels them."""
    return ([(d, d, None) for d in rule.get("detectors", [])]
            + [(f"theme:{t}", "user_correction", t) for t in rule.get("themes", [])])


def _ever_hits(conn: sqlite3.Connection, detector: str, signature: str | None) -> int:
    q, p = "SELECT COUNT(*) FROM detector_hits WHERE detector = ?", [detector]
    if signature is not None:
        q += " AND signature = ?"
        p.append(signature)
    return conn.execute(q, p).fetchone()[0]


def _latest_completed(rows: list[dict]) -> dict[str, dict]:
    done: dict[str, dict] = {}
    for r in sorted((r for r in rows if r["window_complete"]), key=lambda r: r["at"]):
        done[r["target"]] = r
    return done


def rule_verdict(rule: dict, rows: list[dict], ever: dict[str, int],
                 recent: dict[str, float | None]) -> dict:
    """One verdict for one rule. `rows` are this rule's remediation rows, `ever`
    all-time hit counts and `recent` recent per-100 rates, both keyed by target label."""
    targets = [t[0] for t in _targets(rule)]
    base = {"rung": rule.get("rung"), "targets": targets}
    if not targets:
        return {**base, "verdict": "unmeasurable", "reason": "no_target"}
    if not any(ever.get(t) for t in targets):
        return {**base, "verdict": "unmeasurable", "reason": "no_hits_ever"}
    still = {t: recent[t] for t in targets if recent.get(t)}
    if rule.get("rung") == "inform" and still:
        return {**base, "verdict": "promote_candidate", "reason": "target_recurs",
                "recent_per_100": still}
    done = _latest_completed([r for r in rows if r["target"] in targets])
    if not done:
        return {**base, "verdict": "keep",
                "reason": "window_open" if rows else "no_change_point"}
    usable = [r for r in done.values() if r["delta"] is not None]
    if not usable:
        return {**base, "verdict": "keep", "reason": "insufficient_runs"}
    ev = [{k: r[k] for k in ("target", "commit", "at", "before_per_100", "after_per_100", "delta")}
          for r in usable]
    if any(r["delta"] < 0 for r in usable):
        return {**base, "verdict": "keep", "reason": "effect_measured", "evidence": ev}
    zero = all(not r["before_per_100"] and not r["after_per_100"] for r in usable)
    return {**base, "verdict": "cut_candidate",
            "reason": "zero_before_after" if zero else "no_effect", "evidence": ev}


# ------------------------------------------------------------------ budget

def budget_finding(claude_home: Path, budget_toml: Path) -> dict:
    root = claude_home / "CLAUDE.md"
    if not root.is_file():
        return {"status": "unmeasurable", "reason": "no_claude_md"}
    chain = steering_audit.import_chain(root, None)
    out = {"est_tokens": chain["est_tokens"], "lines": chain["lines"], "files": len(chain["files"])}
    if not budget_toml.is_file():
        return {**out, "status": "unmeasurable", "reason": "no_budget_toml"}
    b = tomllib.loads(budget_toml.read_text())
    ceiling = b.get("max_est_tokens")
    if not isinstance(ceiling, int):
        return {**out, "status": "unmeasurable", "reason": "no_ceiling"}
    return {**out, "max_est_tokens": ceiling, "headroom": ceiling - chain["est_tokens"],
            "pct_of_ceiling": round(100 * chain["est_tokens"] / ceiling, 1),
            "status": "over" if chain["est_tokens"] > ceiling else "within",
            "raised_because": b.get("raised_because")}


# -------------------------------------------------------------- judge gate

def judge_due(digest_dir: Path, chain_tokens: int | None, now: datetime) -> dict:
    """The condition under which the model judge should run. Reads `last_audit`
    and the dated digest nearest that day for the chain size then."""
    last = steering_audit._last_audit(digest_dir)
    out = {"last_audit": last, "max_age_days": JUDGE_MAX_AGE_DAYS,
           "chain_growth_threshold": JUDGE_CHAIN_GROWTH}
    if not last:
        return {**out, "due": True, "reasons": ["never_audited"], "age_days": None,
                "chain_growth": None}
    age = (now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
    reasons = []
    if age > JUDGE_MAX_AGE_DAYS:
        reasons.append("steering_audit_stale")
    growth = None
    then = None
    # the first dated digest on or after the audit day is the closest baseline
    for p in sorted(digest_dir.glob("20??-??-??.json")):
        if p.stem >= last[:10] and p.stat().st_size:
            try:
                then = json.loads(p.read_text())["global_chain"]["est_tokens"]
            except (ValueError, KeyError, TypeError):
                continue
            break
    if then and chain_tokens is not None:
        growth = round((chain_tokens - then) / then, 4)
        if growth >= JUDGE_CHAIN_GROWTH:
            reasons.append("chain_growth")
    return {**out, "due": bool(reasons), "reasons": reasons, "age_days": age,
            "chain_growth": growth}


def digest_health(digest_dir: Path, now: datetime, days: int = DIGEST_HEALTH_DAYS) -> dict:
    """Daily digests that are empty, plus recent days with no digest at all."""
    empty = sorted(p.name for p in digest_dir.glob("20??-??-??.*") if p.stat().st_size == 0)
    have = {p.stem for p in digest_dir.glob("20??-??-??.json")}
    missing = [d for d in ((now.date() - timedelta(days=i)).isoformat() for i in range(1, days + 1))
               if d not in have]
    return {"zero_byte": empty, "missing_recent_days": missing}


# ------------------------------------------------------------------- build

def build(conn: sqlite3.Connection, tree: Path, digest_dir: Path, claude_home: Path,
          budget_toml: Path | None = None, window_days: int = 14, recent_days: int = 14,
          now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rules_file = tree / "rules.toml"
    rules = tomllib.loads(rules_file.read_text()).get("rule", {}) if rules_file.is_file() else {}
    rows = query.query_remediation_effect(conn, tree=tree, window_days=window_days)
    lo = (now - timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    verdicts = {}
    for rid, rule in rules.items():
        ever, recent = {}, {}
        for label, det, sig in _targets(rule):
            ever[label] = _ever_hits(conn, det, sig)
            recent[label] = query._rate_per_100(conn, lo, hi, det, sig, None)[2]
        verdicts[rid] = rule_verdict(rule, [r for r in rows if r["rule"] == rid], ever, recent)
    budget = budget_finding(claude_home, budget_toml or tree / "budget.toml")
    counts: dict[str, int] = {}
    for v in verdicts.values():
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": window_days, "recent_days": recent_days,
        "correlational": True,
        "counts": counts, "verdicts": verdicts, "budget": budget,
        "judge": judge_due(digest_dir, budget.get("est_tokens"), now),
        "digest_health": digest_health(digest_dir, now),
    }


def to_markdown(rep: dict) -> str:
    j, b, h = rep["judge"], rep["budget"], rep["digest_health"]
    lines = [f"# Steering verdicts {rep['generated_at']}", "",
             "Correlational: hit rate per 100 runs, window before vs after rule changes.", "",
             "| rule | rung | verdict | reason | targets |", "|---|---|---|---|---|"]
    order = {"cut_candidate": 0, "promote_candidate": 1, "unmeasurable": 2, "keep": 3}
    for rid, v in sorted(rep["verdicts"].items(), key=lambda kv: (order[kv[1]["verdict"]], kv[0])):
        lines.append(f"| {rid} | {v['rung']} | {v['verdict']} | {v['reason']} | "
                     f"{', '.join(v['targets']) or '-'} |")
    lines += ["", "## Budget", ""]
    if b["status"] == "unmeasurable":
        lines.append(f"unmeasurable: {b['reason']}")
    else:
        lines.append(f"{b['status']}: {b['est_tokens']} of {b['max_est_tokens']} est. tokens "
                     f"({b['pct_of_ceiling']}%), headroom {b['headroom']}, {b['lines']} lines "
                     f"in {b['files']} files")
    lines += ["", "## Judge", "",
              f"due: {j['due']} ({', '.join(j['reasons']) or 'no trigger'}); last audit "
              f"{j['last_audit']}, age {j['age_days']} d, chain growth {j['chain_growth']}",
              "", "## Digest health", "",
              f"zero-byte digests: {', '.join(h['zero_byte']) or 'none'}; "
              f"days without a digest (last {DIGEST_HEALTH_DAYS}): "
              f"{', '.join(h['missing_recent_days']) or 'none'}", ""]
    return "\n".join(lines)


def write(rep: dict, out_dir: Path) -> list[Path]:
    md = to_markdown(rep)
    body = json.dumps(rep, indent=2) + "\n"
    paths = []
    for name in ("verdicts-latest", f"verdicts-{rep['generated_at'][:10]}"):
        for ext, text in (("json", body), ("md", md)):
            p = out_dir / f"{name}.{ext}"
            atomic_write(p, text)
            paths.append(p)
    return paths


def run(db: Path | None = None, tree: Path | None = None, out_dir: Path | None = None,
        window_days: int = 14, recent_days: int = 14, write_files: bool = True) -> dict:
    out_dir = out_dir or steering_audit.STEERING_DIGEST_DIR
    tree = tree or query.DEFAULT_RULES_TREE
    conn = query.open_db_ro(db)
    try:
        rep = build(conn, tree, out_dir, steering_audit.CLAUDE_HOME,
                    window_days=window_days, recent_days=recent_days)
    finally:
        conn.close()
    if write_files:
        write(rep, out_dir)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path)
    ap.add_argument("--tree", type=Path)
    ap.add_argument("--out-dir", type=Path, default=steering_audit.STEERING_DIGEST_DIR)
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--recent-days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    try:
        rep = run(a.db, a.tree, a.out_dir, a.window_days, a.recent_days, not a.no_write)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"steering_verdicts: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rep, indent=2) if a.json else to_markdown(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
