"""skill_usage.py — skill-load and dispatch-tiering signals from bus.agent.activity.v1.

Answers "is skill X used, by which harness, and do runs that use it end better?"
from the live stream, without transcript archaeology.

Signals (tool_call events only — tool_return repeats tool_summary):
  skill     Claude ``Skill`` tool (mechanism=skill_tool), or any Read/shell command
            that references ``.../skills/<name>/SKILL.md`` (mechanism=file_read).
            Codex has no Skill tool; reading SKILL.md through the shell IS how it
            loads a skill. file_read also counts agents that open a SKILL.md to
            edit or audit it — the mechanism split keeps that distinguishable.
  dispatch  ``Agent``/``Task`` tool: model, subagent_type, isolation.
  workflow  ``Workflow`` tool.

tool_summary is bounded (1000 chars) and its JSON keys are alphabetical, so a long
``prompt`` routinely truncates away ``subagent_type`` (and sometimes ``model``).
model/subagent_type therefore carry a tri-state: explicit | missing | unknown.
``missing`` is ONLY asserted when the summary parsed as complete JSON — a truncated
summary is never reported as a tiering violation.

Features folded into runs.features:
  skills         {name: {mechanism: count}}
  dispatch       {total, by_model, by_subagent_type, model_missing, model_unknown,
                  isolated}
  workflow_calls int

CLI:
  python3 skill_usage.py backfill [--db PATH] [--since ISO] [--apply]
Dry-run by default (read-only). --apply rewrites these three feature keys on each
run from its stored run_events; per-run, idempotent, resumable.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

FEATURE_KEYS = ("skills", "dispatch", "workflow_calls")

_SKILL_MD_RE = re.compile(r"((?:[^\s'\"`:=]*/)?skills/([A-Za-z0-9_.:-]+)/SKILL\.md)")
_SKILL_FIELD_RE = re.compile(r'"skill"\s*:\s*"([^"]+)"')
_FIELD_RE = {
    k: re.compile(r'"%s"\s*:\s*"([^"]*)"' % k)
    for k in ("model", "subagent_type", "isolation")
}
_DISPATCH_TOOLS = {"Agent", "Task"}


def classify_root(path: str) -> str:
    if "/plugins/" in path:
        return "plugin"
    if "/.claude/skills/" in path:
        # ~/.claude/skills is the user tier; <repo>/.claude/skills is project tier.
        return "claude" if re.search(r"(?:^|/)(?:home/[^/]+|~|root)/\.claude/skills/", path) else "project"
    if "/.agents/skills/" in path:
        return "agents"
    if "/.codex/skills/" in path:
        return "codex"
    return "project"


def _parse(summary: str) -> Optional[dict]:
    try:
        v = json.loads(summary)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def _tri(parsed: Optional[dict], summary: str, key: str) -> tuple[Optional[str], str]:
    if parsed is not None:
        v = parsed.get(key)
        return (v, "explicit") if v else (None, "missing")
    m = _FIELD_RE[key].search(summary)
    return (m.group(1), "explicit") if m else (None, "unknown")


def extract_signals(activity: dict) -> list[dict]:
    """Return skill/dispatch/workflow signals for one activity ``data`` dict."""
    if activity.get("event") != "tool_call":
        return []
    tool = activity.get("tool_name") or ""
    summary = activity.get("tool_summary") or ""
    if not isinstance(summary, str):
        summary = json.dumps(summary)

    if tool == "Skill":
        parsed = _parse(summary)
        name = parsed.get("skill") if parsed else None
        if not name:
            m = _SKILL_FIELD_RE.search(summary)
            name = m.group(1) if m else None
        return [{"kind": "skill", "name": name, "mechanism": "skill_tool"}] if name else []

    if tool in _DISPATCH_TOOLS:
        parsed = _parse(summary)
        model, model_state = _tri(parsed, summary, "model")
        sub, sub_state = _tri(parsed, summary, "subagent_type")
        iso, _ = _tri(parsed, summary, "isolation")
        return [{
            "kind": "dispatch", "tool": tool,
            "model": model, "model_state": model_state,
            "subagent_type": sub, "subagent_type_state": sub_state,
            "isolation": iso,
        }]

    if tool == "Workflow":
        return [{"kind": "workflow"}]

    out, seen = [], set()
    for path, name in _SKILL_MD_RE.findall(summary):
        if name in seen:
            continue
        seen.add(name)
        out.append({"kind": "skill", "name": name, "mechanism": "file_read",
                    "root": classify_root(path)})
    return out


def fold_skill_features(features: dict, activity: dict) -> None:
    """Fold one activity event into ``features`` in place. Never raises."""
    try:
        signals = extract_signals(activity)
    except Exception:  # noqa: BLE001 — extraction must never break run folding
        features["skill_extract_errors"] = features.get("skill_extract_errors", 0) + 1
        return
    for s in signals:
        if s["kind"] == "skill":
            per = features.setdefault("skills", {}).setdefault(s["name"], {})
            per[s["mechanism"]] = per.get(s["mechanism"], 0) + 1
        elif s["kind"] == "dispatch":
            d = features.setdefault("dispatch", {
                "total": 0, "by_model": {}, "by_subagent_type": {},
                "model_missing": 0, "model_unknown": 0, "isolated": 0,
            })
            d["total"] += 1
            if s["model_state"] == "explicit":
                d["by_model"][s["model"]] = d["by_model"].get(s["model"], 0) + 1
            else:
                d["model_" + s["model_state"]] += 1
            st = s["subagent_type"] or ("default" if s["subagent_type_state"] == "missing" else "unknown")
            d["by_subagent_type"][st] = d["by_subagent_type"].get(st, 0) + 1
            if s["isolation"] == "worktree":
                d["isolated"] += 1
        elif s["kind"] == "workflow":
            features["workflow_calls"] = features.get("workflow_calls", 0) + 1


def features_from_events(raw_events: list[str]) -> dict:
    """Recompute the skill feature keys from a run's stored raw_json envelopes."""
    features: dict = {}
    for raw in raw_events:
        try:
            env = json.loads(raw)
        except ValueError:
            continue
        data = env.get("data") if isinstance(env, dict) else None
        if isinstance(data, dict):
            fold_skill_features(features, data)
    return features


# ── backfill ──────────────────────────────────────────────────────────────────

def backfill(db_path: Path, since: Optional[str], apply: bool, out=sys.stdout) -> dict:
    """Recompute skill features for stored runs. Dry-run unless ``apply``."""
    if apply:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    where, params = "", []
    if since:
        where, params = "WHERE started >= ?", [since]
    run_ids = [r[0] for r in conn.execute(f"SELECT run_id FROM runs {where} ORDER BY started", params)]

    totals = {"runs": len(run_ids), "runs_with_skills": 0, "runs_with_dispatch": 0,
              "skill_by_mechanism": {}, "dispatch": 0, "model_missing": 0,
              "model_unknown": 0, "workflow_calls": 0, "top_skills": {}, "updated": 0}
    for run_id in run_ids:
        raws = [r[0] for r in conn.execute(
            "SELECT raw_json FROM run_events WHERE run_id=? ORDER BY seq", (run_id,))]
        f = features_from_events(raws)
        for name, mech in f.get("skills", {}).items():
            for m, c in mech.items():
                totals["skill_by_mechanism"][m] = totals["skill_by_mechanism"].get(m, 0) + c
                totals["top_skills"][name] = totals["top_skills"].get(name, 0) + c
        if f.get("skills"):
            totals["runs_with_skills"] += 1
        if d := f.get("dispatch"):
            totals["runs_with_dispatch"] += 1
            totals["dispatch"] += d["total"]
            totals["model_missing"] += d["model_missing"]
            totals["model_unknown"] += d["model_unknown"]
        totals["workflow_calls"] += f.get("workflow_calls", 0)

        if apply:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT features FROM runs WHERE run_id=?", (run_id,)).fetchone()
                cur = json.loads(row[0] or "{}") if row else {}
                for k in FEATURE_KEYS:
                    cur.pop(k, None)
                cur.update(f)
                conn.execute("UPDATE runs SET features=? WHERE run_id=?", (json.dumps(cur), run_id))
                conn.execute("COMMIT")
                totals["updated"] += 1
            except Exception:
                conn.execute("ROLLBACK")
                raise
    conn.close()
    totals["top_skills"] = dict(sorted(totals["top_skills"].items(), key=lambda kv: -kv[1])[:25])
    print(json.dumps(totals, indent=2), file=out)
    return totals


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="skill_usage")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backfill", help="Recompute skill/dispatch features from run_events")
    b.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    b.add_argument("--since", help="Only runs with started >= this ISO timestamp")
    b.add_argument("--apply", action="store_true", help="Write features (default: dry-run, read-only)")
    args = p.parse_args(argv)
    if not args.db.exists():
        print(f"error: run store not found: {args.db}", file=sys.stderr)
        return 1
    backfill(args.db, args.since, args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
