#!/usr/bin/env python3
"""champion.py — MASS Phase 0b/0c: dispatch-brief extraction, same-task pair
manifests, bounded workspace bundles, and Experiment B (brief-feature lift).

Deterministic and read-only: zero LLM calls. All databases open ``mode=ro``.
Brief text is private material; ``briefs.jsonl`` lives only under
``~/.cache/nervous-bus/reflex/phase0/`` and subcommand stdout carries counts.

Subcommands (JSON on stdout):
    extract  [--since-days N] [--project P] [--out PATH]
    pairs    --population retry|cohort|recipe|similar|all
    bundle   <run_id|brief_sha>
    lift     [--md PATH]

Design contract: MASS-integration-design.md sections 3.1-3.3, 4.1, 4.4.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from label import _source_tier  # noqa: E402
from detectors.dispatch_lineage import DEFAULT_COHORT_WINDOW_S, _epoch  # noqa: E402
from workflow_dispatch import _prompt_text  # noqa: E402
from orca_outcome import _OUTCOME_MAP as _ORCA_MAP  # noqa: E402

HOME = Path.home()
REFLEX = HOME / ".cache" / "nervous-bus" / "reflex"
DEFAULTS = {
    "transcripts_root": REFLEX / "transcripts",
    "projects_root": HOME / ".claude" / "projects",
    "runs_db": REFLEX / "runs.db",
    "orca_db": HOME / ".config" / "orca" / "orchestration.db",
    "mmx_root": HOME / ".mmx" / "runs",
    "phase0_dir": REFLEX / "phase0",
}

POSITIVE = {"landed", "clean"}
EXPLICIT_TIER = 2
POSITIVE_ONLY_SOURCES = {"orca_worker_done"}  # failed/blocked map to None (orca_outcome.py)

# ── feature definitions ─────────────────────────────────────────────────────
ENFORCED = ["has_acceptance_cmd", "has_file_scope", "isolation_worktree",
            "model_explicit", "has_relay_hop", "has_base_branch"]
PROSE = ["caps_count", "length_chars", "has_receipts_clause",
         "has_exhaustion_clause", "names_report_path", "names_scratch_dir"]
NUMERIC = {"caps_count", "length_chars"}  # binarized at the corpus median

_CMD = (r"(?:pytest|cargo|go\s+test|go\s+vet|go\s+build|npm|pnpm|yarn|make|python3?|bash|sh|node|"
        r"vitest|tsc|ruff|uv|just|gradle|bd|git|gh|curl|sqlite3|test\s+-[a-z])")
RE_ACCEPT = re.compile(
    r"(?is)(?:acceptanceGates|acceptance[ _-]?(?:gate|criteri|cmd|command)|\bgates?\b|"
    r"\bverif(?:y|ication)\b|done means|\bbaseline\b|command_succeeds|test_passes)"
    r"[^\n]{0,240}?`[^`\n]*\b" + _CMD + r"\b[^`\n]*`"
    r"|(?m:^\s*[$>]\s*" + _CMD + r"\b)"
    r"|(?:acceptance|gates?|verif\w*|done means)[^\n]*\n(?:[ \t]*[-*0-9.)]+[ \t]*`[^`\n]*\b" + _CMD + r"\b)")
RE_SCOPE = re.compile(
    r"(?i)\bfile[ _-]?scope\b|\bfileScope\b|\bscope\s*\((?:write|writes)\)|\bFILE SCOPE\b|"
    r"\bonly (?:modify|touch|edit|write)\b|\bdo not (?:modify|edit|touch)\b|\bout-?of-?scope\b")
RE_RECEIPTS = re.compile(r"(?i)\breceipts?\b")
RE_EXHAUST = re.compile(
    r"(?i)exhaustion clause|before (?:reporting|calling|claiming)[^\n]{0,60}"
    r"(?:missing|absent|empty|unresolvable)|enumerate every surface|surfaces? (?:you )?searched")
RE_RELAY = re.compile(
    r"(?i)handoff (?:file|doc|path)|write a handoff|hand[- ]?off to\b|\brelay\b|next lane|"
    r"continuation file|\bdependsOn\b|sequential relay")
RE_MODEL = re.compile(r"(?i)\bmodel\s*[:=`]\s*`?(?:sonnet|haiku|opus|fable|gpt[\w.-]*|minimax[\w.-]*|fast)\b|--model\b")
RE_ISOLATION = re.compile(
    r"(?i)isolat(?:ion|ed)[^\n]{0,40}worktree|worktree[^\n]{0,40}isolat|"
    r"git rev-parse --show-toplevel|\bin your (?:own )?worktree\b|\bassert isolation\b")
RE_BASE = re.compile(
    r"(?i)\bbase (?:branch|sha|commit)\b|ff-only origin|merge --ff-only|"
    r"\bbranch(?:es)? from origin/|\bfrom origin/(?:main|master)\b")
RE_REPORT = re.compile(
    r"(?i)\b(?:report|write[- ]?up|deliverable|write)\b[^\n]{0,140}?[\w./~$<>{}-]+\.(?:md|json)\b")
RE_SCRATCH = re.compile(r"(?i)\bscratch\b[^\n]{0,100}[/~]|\bscratch(?:[_ ]dir)?\b[^\n]{0,20}`")
RE_CAPS = re.compile(r"\b[A-Z]{4,}\b")
CAPS_STOP = {"JSON", "HTTP", "HTTPS", "TODO", "NULL", "TRUE", "FALSE", "UUID", "ULID", "HTML",
             "YAML", "TOML", "HEAD", "POST", "DELETE", "SELECT", "FROM", "WHERE", "NOTE"}


def extract_features(text: str, meta: Optional[dict] = None, cwd: str = "") -> dict:
    """Deterministic §3.1 feature vector. ``meta`` may carry structured signals
    (model / gates / scope / isolation); the prose regex and the structured
    field are OR-ed and ``_fired`` records which arm fired."""
    meta = meta or {}
    fired: dict[str, list[str]] = {}

    def _or(name: str, prose: bool, structured: bool) -> bool:
        src = []
        if prose:
            src.append("prose")
        if structured:
            src.append("structured")
        if src:
            fired[name] = src
        return bool(src)

    caps = [w for w in RE_CAPS.findall(text) if w not in CAPS_STOP]
    iso_cwd = any(m in (cwd or "") for m in ("/worktrees/", "/.mmx-worktrees/", ".claude/worktrees"))
    feats = {
        "has_acceptance_cmd": _or("has_acceptance_cmd", bool(RE_ACCEPT.search(text)), bool(meta.get("gates"))),
        "has_file_scope": _or("has_file_scope", bool(RE_SCOPE.search(text)), bool(meta.get("scope"))),
        "isolation_worktree": _or("isolation_worktree", bool(RE_ISOLATION.search(text)),
                                  iso_cwd or meta.get("isolation") == "worktree"),
        "model_explicit": _or("model_explicit", bool(RE_MODEL.search(text)), bool(meta.get("model"))),
        "has_relay_hop": _or("has_relay_hop", bool(RE_RELAY.search(text)), False),
        "has_base_branch": _or("has_base_branch", bool(RE_BASE.search(text)), bool(meta.get("base_sha"))),
        "caps_count": len(caps),
        "length_chars": len(text),
        "has_receipts_clause": bool(RE_RECEIPTS.search(text)),
        "has_exhaustion_clause": bool(RE_EXHAUST.search(text)),
        "names_report_path": bool(RE_REPORT.search(text)),
        "names_scratch_dir": bool(RE_SCRATCH.search(text)),
    }
    feats["_fired"] = fired
    return feats


# ── generic helpers ──────────────────────────────────────────────────────────

def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def project_from_cwd(cwd: str) -> str:
    """Project slug from a cwd: /data2/worktrees/<proj>/..., /projects/<proj>,
    /data2/orca/<proj>[-lane]. Falls back to the last path segment."""
    parts = Path(cwd).parts if cwd else ()
    for marker in ("worktrees", "projects", "orca"):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                return parts[i + 1]
    return (Path(cwd).name if cwd else "") or "unknown"


def _read_lines(path: Path, limit: int) -> Iterator[dict]:
    try:
        with path.open("r", errors="replace") as fh:
            for i, ln in enumerate(fh):
                if i >= limit:
                    return
                if not ln.strip():
                    continue
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue
    except OSError:
        return


def _cutoff(since_days: Optional[int]) -> Optional[str]:
    if since_days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%S")


# ── run / label join ─────────────────────────────────────────────────────────

def load_runs(runs_db: Path) -> dict:
    """Index runs by run_key / worktree_slug / git_branch / worktree. When several
    runs share a key prefer one with an outcome, then the latest start."""
    idx: dict[str, dict] = {"key": {}, "slug": {}, "branch": {}, "wt": {}}
    hits: dict[str, list[dict]] = defaultdict(list)
    if not runs_db.exists():
        return {"idx": idx, "hits": hits}
    conn = _ro(runs_db)
    try:
        rows = conn.execute(
            "SELECT run_id, run_key, project, agent_kind, worktree, worktree_slug, git_branch, "
            "outcome, label_history, started FROM runs").fetchall()
        for r in rows:
            d = dict(r)
            try:
                hist = json.loads(d.pop("label_history") or "[]")
            except ValueError:
                hist = []
            d["label_source"] = (hist[-1].get("source") if hist and isinstance(hist[-1], dict) else None)
            for kind, k in (("key", d["run_key"]), ("slug", d["worktree_slug"]),
                            ("branch", d["git_branch"]), ("wt", d["worktree"])):
                if not k:
                    continue
                cur = idx[kind].get(k)
                if cur is None or (bool(d["outcome"]), d["started"]) > (bool(cur["outcome"]), cur["started"]):
                    idx[kind][k] = d
        for h in conn.execute("SELECT run_id, detector, signature FROM detector_hits"):
            hits[h["run_id"]].append((h["detector"], h["signature"]))
    finally:
        conn.close()
    return {"idx": idx, "hits": hits}


def label_fields(run: Optional[dict], hits: dict) -> dict:
    if not run:
        return {"run_id": None, "run_project": None, "agent_kind": None, "outcome": None,
                "label_source": None, "label_tier": 0, "explicit": False, "positive": None,
                "detectors": [], "failure_taxonomy": []}
    outcome = run.get("outcome")
    src = run.get("label_source")
    tier = _source_tier(src) if (outcome and src) else (1 if outcome else 0)
    explicit = bool(outcome) and tier >= EXPLICIT_TIER
    hs = hits.get(run["run_id"], [])
    return {
        "run_id": run["run_id"], "run_project": run["project"], "agent_kind": run["agent_kind"],
        "outcome": outcome, "label_source": src, "label_tier": tier, "explicit": explicit,
        "positive": (outcome in POSITIVE) if outcome else None,
        "detectors": sorted({d for d, _ in hs if d != "failure_taxonomy"}),
        "failure_taxonomy": sorted({s for d, s in hs if d == "failure_taxonomy"}),
    }


# ── source (a): Claude subagent / mmx session transcripts ────────────────────

def _meta_for(path: Path, transcripts_root: Path, projects_root: Path) -> dict:
    sib = path.with_suffix(".meta.json")
    cands = [sib]
    try:
        rel = path.relative_to(transcripts_root)
        parts = rel.parts[1:] if rel.parts and rel.parts[0] == "mmx" else rel.parts
        cands.append(projects_root.joinpath(*parts).with_suffix(".meta.json"))
    except ValueError:
        pass
    for c in cands:
        if c.is_file():
            try:
                return json.loads(c.read_text(errors="replace")) or {}
            except (OSError, ValueError):
                pass
    return {}


def iter_claude_briefs(transcripts_root: Path, projects_root: Path) -> Iterator[dict]:
    """(a) line 0 of subagents/agent-*.jsonl (includes transcripts/mmx/**), plus
    the first real user message of mmx main-session transcripts."""
    seen: set[str] = set()
    for path in sorted(transcripts_root.rglob("subagents/agent-*.jsonl")):
        line0 = next(_read_lines(path, 1), None)
        if not line0:
            continue
        text = _prompt_text(line0.get("message"))
        agent_id = line0.get("agentId") or path.stem.replace("agent-", "")
        if not text or agent_id in seen:
            continue
        seen.add(agent_id)
        meta = _meta_for(path, transcripts_root, projects_root)
        is_mmx = "mmx" in path.relative_to(transcripts_root).parts[:1]
        yield {
            "source": "mmx_subagent" if is_mmx else "claude_subagent",
            "source_id": agent_id, "text": text, "ts": line0.get("timestamp", ""),
            "cwd": line0.get("cwd", ""), "git_branch": line0.get("gitBranch", ""),
            "session_id": line0.get("sessionId", ""), "transcript": str(path),
            "agent_type": meta.get("agentType", ""), "model": meta.get("model", "") or "",
            "meta": {"model": meta.get("model", ""), "isolation": ""},
            "join": [("key", f"{line0.get('sessionId', '')}#agent-{agent_id}"),
                     ("slug", f"agent-{agent_id}")],
        }
    mmx_dir = transcripts_root / "mmx"
    if mmx_dir.is_dir():
        for path in sorted(mmx_dir.glob("*/*.jsonl")):
            sid = path.stem
            for d in _read_lines(path, 200):
                if d.get("type") == "user" and not d.get("isSidechain"):
                    text = _prompt_text(d.get("message"))
                    if text:
                        yield {
                            "source": "mmx_session", "source_id": sid, "text": text,
                            "ts": d.get("timestamp", ""), "cwd": d.get("cwd", ""),
                            "git_branch": d.get("gitBranch", ""), "session_id": sid,
                            "transcript": str(path), "agent_type": "", "model": "",
                            "meta": {}, "join": [("key", sid)],
                        }
                    break


# ── source (b): Orca tasks ───────────────────────────────────────────────────

def orca_attempts(orca_db: Path) -> dict:
    """task_id -> list of {dispatch_id, retry_of, status, raw_outcome} using
    dispatch_contexts and worker_done messages (payload.dispatchId/outcome)."""
    out: dict[str, list[dict]] = defaultdict(list)
    if not orca_db.exists():
        return {}
    conn = _ro(orca_db)
    try:
        done: dict[str, str] = {}
        for m in conn.execute("SELECT payload FROM messages WHERE type='worker_done'"):
            try:
                p = json.loads(m["payload"] or "{}")
            except ValueError:
                continue
            if p.get("dispatchId") and p.get("outcome"):
                done[p["dispatchId"]] = p["outcome"]
        for d in conn.execute(
                "SELECT id, task_id, retry_of_dispatch_id, status, created_at FROM dispatch_contexts "
                "ORDER BY created_at"):
            out[d["task_id"]].append({
                "dispatch_id": d["id"], "retry_of": d["retry_of_dispatch_id"],
                "status": d["status"], "raw_outcome": done.get(d["id"]),
                "created_at": d["created_at"]})
    finally:
        conn.close()
    return dict(out)


def iter_orca_briefs(orca_db: Path) -> Iterator[dict]:
    if not orca_db.exists():
        return
    attempts = orca_attempts(orca_db)
    conn = _ro(orca_db)
    try:
        for t in conn.execute("SELECT id, task_title, spec, status, created_at FROM tasks"):
            if not t["spec"]:
                continue
            ts = (t["created_at"] or "").replace(" ", "T")
            yield {
                "source": "orca_task", "source_id": t["id"], "text": t["spec"], "ts": ts,
                "cwd": "", "git_branch": "", "session_id": "", "transcript": "",
                "agent_type": "orca_worker", "model": "", "meta": {},
                "join": [], "orca": {"task_status": t["status"], "attempts": attempts.get(t["id"], [])},
            }
    finally:
        conn.close()


# ── source (c): mmx recipe runs ──────────────────────────────────────────────

def iter_mmx_recipe_briefs(mmx_root: Path) -> Iterator[dict]:
    if not mmx_root.is_dir():
        return
    for d in sorted(mmx_root.glob("mmxd-*")):
        brief = d / "brief.md"
        task = d / "task.txt"
        f = brief if brief.is_file() else task if task.is_file() else None
        if f is None:
            continue
        try:
            text = f.read_text(errors="replace")
            meta = json.loads((d / "meta.json").read_text(errors="replace")) if (d / "meta.json").is_file() else {}
        except (OSError, ValueError):
            continue
        ts = (meta.get("started_at") or "")
        yield {
            "source": "mmx_recipe", "source_id": d.name, "text": text, "ts": ts,
            "cwd": meta.get("worktree", ""), "git_branch": meta.get("branch", ""),
            "session_id": "", "transcript": "", "agent_type": "mmx", "model": "",
            "meta": {"gates": meta.get("gates"), "scope": meta.get("scope"),
                     "base_sha": meta.get("base_sha"), "model": ""},
            "join": [("branch", meta.get("branch", "")), ("wt", meta.get("worktree", ""))],
            "repo": meta.get("repo", ""), "title": meta.get("title", ""),
        }


# ── extract ──────────────────────────────────────────────────────────────────

def _orca_label(attempts: list[dict]) -> dict:
    """orca_outcome semantics: succeeded -> clean (tier 3, positive-only);
    failed/blocked -> no label."""
    ok = any(a["raw_outcome"] == "succeeded" for a in attempts)
    base = label_fields(None, {})
    if ok:
        base.update(outcome="clean", label_source="orca_worker_done",
                    label_tier=_source_tier("orca_worker_done"), explicit=True, positive=True)
    return base


def extract(args_ns: argparse.Namespace) -> dict:
    roots = {k: Path(getattr(args_ns, k)) for k in ("transcripts_root", "projects_root", "runs_db",
                                                    "orca_db", "mmx_root")}
    since = _cutoff(args_ns.since_days)
    runs = load_runs(roots["runs_db"])
    sources = [iter_claude_briefs(roots["transcripts_root"], roots["projects_root"]),
               iter_orca_briefs(roots["orca_db"]),
               iter_mmx_recipe_briefs(roots["mmx_root"])]
    out_path = Path(args_ns.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict = defaultdict(lambda: defaultdict(int))
    n = 0
    with out_path.open("w") as fh:
        for src in sources:
            for b in src:
                if since and b["ts"] and b["ts"][:19] < since:
                    continue
                project = project_from_cwd(b["cwd"]) if b["cwd"] else "unknown"
                run = None
                for kind, key in b["join"]:
                    if key and (run := runs["idx"][kind].get(key)):
                        break
                if b["source"] == "orca_task":
                    lab = _orca_label(b["orca"]["attempts"])
                    project = "orca-tasks"
                else:
                    lab = label_fields(run, runs["hits"])
                if project == "unknown" and lab["run_project"]:
                    project = lab["run_project"]
                if args_ns.project and project != args_ns.project:
                    continue
                row = {
                    "brief_id": f"{b['source']}:{b['source_id']}",
                    "source": b["source"], "source_id": b["source_id"],
                    "brief_text": b["text"], "brief_sha256": sha256(b["text"]),
                    "agent_type": b["agent_type"], "model": b["model"],
                    "project": project, "cwd": b["cwd"], "git_branch": b["git_branch"],
                    "ts": b["ts"], "session_id": b["session_id"], "transcript": b["transcript"],
                    "features": extract_features(b["text"], b["meta"], b["cwd"]),
                    **lab,
                }
                row["agent_kind"] = lab["agent_kind"] or {
                    "orca_task": "orca_worker", "mmx_recipe": "mmx_recipe"}.get(b["source"], b["source"])
                for extra in ("orca", "repo", "title"):
                    if extra in b:
                        row[extra] = b[extra]
                fh.write(json.dumps(row) + "\n")
                n += 1
                c = counts[b["source"]]
                c["briefs"] += 1
                c["joined_to_run"] += 1 if lab["run_id"] else 0
                c["explicit_label"] += 1 if lab["explicit"] else 0
                c[f"tier_{lab['label_tier']}"] += 1
    return {"out": str(out_path), "briefs": n, "by_source": {k: dict(v) for k, v in counts.items()},
            "roots": {k: str(v) for k, v in roots.items()}}


def load_briefs(path: Path) -> list[dict]:
    rows = []
    with Path(path).open() as fh:
        for ln in fh:
            if ln.strip():
                rows.append(json.loads(ln))
    return rows


# ── pairs ────────────────────────────────────────────────────────────────────

def shingles(text: str, k: int = 5) -> frozenset:
    toks = re.findall(r"\w+", text.lower())
    if len(toks) < k:
        return frozenset([" ".join(toks)]) if toks else frozenset()
    return frozenset(" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _usable(a: dict, b: dict) -> bool:
    """Both sides explicit (tier>=2), and positive vs not-positive."""
    return bool(a["explicit"] and b["explicit"] and a["positive"] is not None
                and b["positive"] is not None and a["positive"] != b["positive"])


def _pair(pop: str, a: dict, b: dict, **extra) -> dict:
    return {"population": pop, "a": a["brief_id"], "b": b["brief_id"],
            "a_run_id": a["run_id"], "b_run_id": b["run_id"],
            "a_outcome": a["outcome"], "b_outcome": b["outcome"],
            "a_label_source": a["label_source"], "b_label_source": b["label_source"],
            "project": a["project"], "agent_kind": a["agent_kind"],
            "a_sha": a["brief_sha256"], "b_sha": b["brief_sha256"], **extra}


def _key(p: dict) -> frozenset:
    return frozenset((p["a"], p["b"]))


def orca_dispatch_label(raw: Optional[str]) -> tuple[bool, Optional[bool]]:
    """(explicit, positive) for a worker_done outcome via orca_outcome's mapping."""
    mapped = _ORCA_MAP.get(raw) if raw else None
    return (mapped is not None, (mapped in POSITIVE) if mapped is not None else None)


def pairs_retry(rows: list[dict]) -> tuple[list[dict], dict]:
    """Orca retry chains (retry_of_dispatch_id -> original dispatch). Outcomes come
    from worker_done by dispatchId through orca_outcome semantics, so a pair is
    usable only when both sides are explicit and differ in polarity."""
    pairs: list[dict] = []
    diag = {"retry_flagged_dispatches": 0, "retry_raw_opposite": 0,
            "retry_both_explicit": 0, "tasks_multi_dispatch_not_flagged": 0}
    for r in rows:
        if r["source"] != "orca_task":
            continue
        att = r["orca"]["attempts"]
        by_id = {a["dispatch_id"]: a for a in att}
        flagged = [a for a in att if a["retry_of"]]
        diag["retry_flagged_dispatches"] += len(flagged)
        if len(att) > 1 and not flagged:
            diag["tasks_multi_dispatch_not_flagged"] += 1
        for a in flagged:
            o = by_id.get(a["retry_of"])
            if not o:
                continue
            if o["raw_outcome"] and a["raw_outcome"] and o["raw_outcome"] != a["raw_outcome"]:
                diag["retry_raw_opposite"] += 1
            (ea, pa), (eb, pb) = orca_dispatch_label(o["raw_outcome"]), orca_dispatch_label(a["raw_outcome"])
            if ea and eb:
                diag["retry_both_explicit"] += 1
                if pa != pb:
                    pairs.append({"population": "retry", "a": r["brief_id"] + "#" + o["dispatch_id"],
                                  "b": r["brief_id"] + "#" + a["dispatch_id"], "project": r["project"],
                                  "agent_kind": r["agent_kind"], "a_sha": r["brief_sha256"],
                                  "b_sha": r["brief_sha256"], "a_run_id": None, "b_run_id": None,
                                  "a_outcome": o["raw_outcome"], "b_outcome": a["raw_outcome"],
                                  "a_label_source": "orca_worker_done", "b_label_source": "orca_worker_done"})
    return pairs, diag


def pairs_cohort(rows: list[dict], window_s: float = DEFAULT_COHORT_WINDOW_S,
                 thr: float = 0.8) -> list[dict]:
    """Claude subagent siblings dispatched within window_s of each other under one
    parent session, same-template (shingle Jaccard >= thr), opposite explicit labels."""
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["source"] in ("claude_subagent", "mmx_subagent") and r["session_id"] and r["explicit"]:
            by_parent[r["session_id"]].append(r)
    out = []
    for sid, rs in by_parent.items():
        rs.sort(key=lambda r: r["ts"])
        cohorts, cur, prev = [], [], None
        for r in rs:
            e = _epoch(r["ts"])
            if cur and e is not None and prev is not None and (e - prev) > window_s:
                cohorts.append(cur)
                cur = []
            cur.append(r)
            prev = e if e is not None else prev
        if cur:
            cohorts.append(cur)
        for ci, cohort in enumerate(cohorts):
            sh = [shingles(r["brief_text"]) for r in cohort]
            for i in range(len(cohort)):
                for j in range(i + 1, len(cohort)):
                    if _usable(cohort[i], cohort[j]):
                        sim = jaccard(sh[i], sh[j])
                        if sim >= thr:
                            out.append(_pair("cohort", cohort[i], cohort[j],
                                             parent_session=sid, cohort_index=ci, similarity=round(sim, 4)))
    return out


def _recipe_family(r: dict) -> str:
    base = r.get("title") or r["project"]
    base = re.sub(r"(?i)\brun-?\d+\b|\d{8}t?\d{0,6}z?|\d+", "#", base)
    return f"{r['source']}|{re.sub(r'#+', '#', base).strip('-# ')}"


def pairs_recipe(rows: list[dict], thr: float = 0.8) -> list[dict]:
    """Recurring mmx runs (same recipe family, near-identical brief) that carry
    different explicit outcomes."""
    fam: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["source"] in ("mmx_recipe", "mmx_session") and r["explicit"]:
            fam[_recipe_family(r)].append(r)
    out = []
    for f, rs in fam.items():
        sh = [shingles(r["brief_text"]) for r in rs]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if _usable(rs[i], rs[j]):
                    same = rs[i]["brief_sha256"] == rs[j]["brief_sha256"]
                    sim = 1.0 if same else jaccard(sh[i], sh[j])
                    if sim >= thr:
                        out.append(_pair("recipe", rs[i], rs[j], family=f, similarity=round(sim, 4)))
    return out


def pairs_similar(rows: list[dict], exclude: set, thr: float = 0.8) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if r["explicit"] and r["source"] != "orca_task":
            groups[(r["project"], r["agent_kind"], r["agent_type"])].append(r)
    out = []
    for g, rs in groups.items():
        sh = [shingles(r["brief_text"]) for r in rs]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                if not _usable(rs[i], rs[j]) or _key({"a": rs[i]["brief_id"], "b": rs[j]["brief_id"]}) in exclude:
                    continue
                la, lb = len(sh[i]), len(sh[j])
                if not la or not lb or min(la, lb) / max(la, lb) < thr:
                    continue
                sim = jaccard(sh[i], sh[j])
                if sim >= thr:
                    out.append(_pair("similar", rs[i], rs[j], similarity=round(sim, 4)))
    return out


def build_pairs(rows: list[dict], population: str, thr: float = 0.8) -> tuple[dict, dict]:
    res: dict[str, list[dict]] = {}
    retry, diag = pairs_retry(rows)
    res["retry"] = retry
    res["cohort"] = pairs_cohort(rows, thr=thr)
    res["recipe"] = pairs_recipe(rows, thr=thr)
    seen = {_key(p) for pop in ("retry", "cohort", "recipe") for p in res[pop]}
    res["similar"] = pairs_similar(rows, seen, thr=thr) if population in ("similar", "all") else []
    # priority de-dup across populations (retry > cohort > recipe > similar)
    covered: set = set()
    for pop in ("retry", "cohort", "recipe", "similar"):
        keep = []
        for p in res[pop]:
            if _key(p) not in covered:
                covered.add(_key(p))
                keep.append(p)
        res[pop] = keep
    return res, diag


def pairs_cmd(ns: argparse.Namespace) -> dict:
    rows = load_briefs(Path(ns.briefs))
    res, diag = build_pairs(rows, ns.population, ns.threshold)
    outdir = Path(ns.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    pops = ["retry", "cohort", "recipe", "similar"] if ns.population == "all" else [ns.population]
    summary = {"retry_diagnostics": diag, "threshold": ns.threshold, "populations": {}}
    for pop in pops:
        p = outdir / f"pairs-{pop}.jsonl"
        with p.open("w") as fh:
            for pair in res[pop]:
                fh.write(json.dumps(pair) + "\n")
        summary["populations"][pop] = {"usable_pairs": len(res[pop]), "path": str(p),
                                       "by_project": _count_by(res[pop], "project")}
    summary["usable_total"] = sum(v["usable_pairs"] for v in summary["populations"].values())
    summary["phase_0d_needs"] = 80
    return summary


def _count_by(items: Iterable[dict], key: str) -> dict:
    c: dict = defaultdict(int)
    for i in items:
        c[i[key]] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


# ── bundle ───────────────────────────────────────────────────────────────────

MAX_TOTAL_TOKENS = 50_000
MAX_CODE_TOKENS = 10_000
CHARS_PER_TOKEN = 4


def _cap(text: str, tokens: int) -> tuple[str, bool]:
    lim = tokens * CHARS_PER_TOKEN
    return (text, False) if len(text) <= lim else (text[:lim], True)


def _git(args: list[str], cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def diff_stat(row: dict, repo_hint: Optional[str] = None) -> Optional[str]:
    cwd = row.get("cwd") or ""
    if cwd and Path(cwd).is_dir() and (Path(cwd) / ".git").exists():
        for base in ("origin/main", "main", "origin/master", "master"):
            if _git(["rev-parse", "--verify", base], cwd):
                out = _git(["diff", "--stat", f"{base}...HEAD"], cwd)
                if out is not None:
                    return out
    branch = row.get("git_branch")
    repo = repo_hint or (str(HOME / "projects" / row["project"]) if row.get("project") else "")
    if branch and repo and Path(repo).is_dir() and _git(["rev-parse", "--verify", branch], repo):
        for base in ("origin/main", "main"):
            if _git(["rev-parse", "--verify", base], repo):
                out = _git(["diff", "--stat", f"{base}...{branch}"], repo)
                if out is not None:
                    return out
    return None


def transcript_evidence(path: str) -> dict:
    """Last assistant text message and gate (build/test) exit codes from a transcript."""
    from detectors.edit_build_fail_revert import BUILD_KEYWORDS
    last_text, cmds, gates = "", {}, []
    p = Path(path) if path else None
    if not p or not p.is_file():
        return {"last_assistant_message": None, "gate_exit_codes": []}
    for d in _read_lines(p, 10**9):
        msg = d.get("message") or {}
        content = msg.get("content")
        if d.get("type") == "assistant" and isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                last_text = "\n".join(texts)
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                    cmds[c.get("id")] = (c.get("input") or {}).get("command", "")
        elif d.get("type") == "user" and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in cmds:
                    cmd = cmds[c["tool_use_id"]]
                    if any(k in cmd for k in BUILD_KEYWORDS):
                        body = c.get("content")
                        body = body if isinstance(body, str) else json.dumps(body)
                        m = re.search(r"[Ee]xit code:?\s*(\d+)", body or "")
                        code = int(m.group(1)) if m else (1 if c.get("is_error") else 0)
                        gates.append({"cmd": cmd[:160], "exit": code})
    return {"last_assistant_message": last_text or None, "gate_exit_codes": gates[-10:]}


def build_bundle(row: dict, repo_hint: Optional[str] = None) -> dict:
    ev = transcript_evidence(row.get("transcript", ""))
    stat = diff_stat(row, repo_hint)
    reports = []
    for m in RE_REPORT.finditer(row.get("brief_text", "")):
        for path in re.findall(r"[\w./~$-]+\.(?:md|json)\b", m.group(0)):
            pp = Path(os.path.expanduser(path))
            if pp.is_absolute():
                reports.append({"path": str(pp), "exists": pp.is_file()})
    reports = list({r["path"]: r for r in reports}.values())[:10]
    truncated: dict[str, bool] = {}
    code, truncated["diff_stat"] = _cap(stat or "", MAX_CODE_TOKENS)
    last, truncated["last_assistant_message"] = _cap(ev["last_assistant_message"] or "", 8_000)
    bundle = {
        "brief_id": row["brief_id"], "brief_sha256": row["brief_sha256"], "run_id": row["run_id"],
        "outcome": row["outcome"], "label_source": row["label_source"],
        "diff_stat": code if stat is not None else None,
        "diff_stat_recoverable": stat is not None,
        "report_paths": reports,
        "last_assistant_message": last or None,
        "gate_exit_codes": ev["gate_exit_codes"],
        "truncated": truncated,
    }
    total = len(json.dumps(bundle)) // CHARS_PER_TOKEN
    if total > MAX_TOTAL_TOKENS:
        keep = max(0, MAX_TOTAL_TOKENS * CHARS_PER_TOKEN - (len(json.dumps(bundle)) - len(last)) - 256)
        bundle["last_assistant_message"] = last[:keep]
        truncated["last_assistant_message"] = True
    bundle["approx_tokens"] = len(json.dumps(bundle)) // CHARS_PER_TOKEN
    return bundle


def bundle_cmd(ns: argparse.Namespace) -> dict:
    rows = load_briefs(Path(ns.briefs))
    hit = [r for r in rows if ns.ident in (r["run_id"], r["brief_sha256"], r["brief_id"])
           or (len(ns.ident) >= 8 and r["brief_sha256"].startswith(ns.ident))]
    if not hit:
        return {"error": "no brief matches", "ident": ns.ident,
                "searched": [str(ns.briefs)], "fields": ["run_id", "brief_sha256", "brief_id", "sha prefix>=8"]}
    outdir = Path(ns.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    for r in hit[:ns.limit]:
        b = build_bundle(r, ns.repo)
        p = outdir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', r['brief_id'])}.json"
        p.write_text(json.dumps(b, indent=2))
        written.append({"path": str(p), "approx_tokens": b["approx_tokens"],
                        "diff_stat_recoverable": b["diff_stat_recoverable"]})
    return {"matches": len(hit), "written": written}


# ── lift (Experiment B) ──────────────────────────────────────────────────────
MIN_ARM = 20
MIN_LIFT = 0.10
MIN_PROJECTS = 2
MIN_TESTABLE_ENFORCED = 3


def _binarize(rows: list[dict]) -> dict[str, float]:
    return {f: statistics.median(r["features"][f] for r in rows) for f in NUMERIC} if rows else {}


def _flag(r: dict, feat: str, cuts: dict) -> bool:
    v = r["features"][feat]
    return v > cuts[feat] if feat in NUMERIC else bool(v)


def lift(rows: list[dict]) -> dict:
    """Experiment B. Population = explicit-label rows from sources that can carry a
    negative (positive-only orca_worker_done rows are excluded: they would
    manufacture lift for contract-shaped briefs)."""
    pop = [r for r in rows if r["explicit"] and r["label_source"] not in POSITIVE_ONLY_SOURCES]
    cuts = _binarize(pop)
    prevalence = {}
    for f in ENFORCED + PROSE:
        prevalence[f] = {
            "all_briefs": round(sum(_flag(r, f, cuts) for r in rows) / len(rows), 4) if rows else None,
            "lift_population": round(sum(_flag(r, f, cuts) for r in pop) / len(pop), 4) if pop else None,
            "median_cut": cuts.get(f),
        }
    cells_by_feat: dict[str, list[dict]] = {}
    verdicts: dict[str, dict] = {}
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in pop:
        strata[(r["project"], r["agent_kind"])].append(r)
    for f in ENFORCED + PROSE:
        cells = []
        for (proj, kind), rs in sorted(strata.items()):
            w = [r for r in rs if _flag(r, f, cuts)]
            wo = [r for r in rs if not _flag(r, f, cuts)]
            cell = {"project": proj, "agent_kind": kind, "n_with": len(w), "n_without": len(wo),
                    "rate_with": None, "rate_without": None, "delta": None, "valid": False}
            if w:
                cell["rate_with"] = round(sum(r["positive"] for r in w) / len(w), 4)
            if wo:
                cell["rate_without"] = round(sum(r["positive"] for r in wo) / len(wo), 4)
            if len(w) >= MIN_ARM and len(wo) >= MIN_ARM:
                cell["valid"] = True
                cell["delta"] = round(cell["rate_with"] - cell["rate_without"], 4)
            cells.append(cell)
        cells_by_feat[f] = cells
        valid = [c for c in cells if c["valid"]]
        by_proj: dict[str, list[dict]] = defaultdict(list)
        for c in valid:
            by_proj[c["project"]].append(c)
        proj_delta = {p: sum(c["delta"] * (c["n_with"] + c["n_without"]) for c in cs)
                      / sum(c["n_with"] + c["n_without"] for c in cs) for p, cs in by_proj.items()}
        testable = len(by_proj) >= MIN_PROJECTS
        holding = sorted(p for p, d in proj_delta.items() if d >= MIN_LIFT)
        if not testable:
            v = "untestable"
        elif len(holding) >= MIN_PROJECTS:
            v = "load-bearing"
        else:
            v = "no-lift"
        verdicts[f] = {"group": "enforced" if f in ENFORCED else "prose", "verdict": v,
                       "testable_projects": sorted(by_proj), "projects_holding_10pt": holding,
                       "project_weighted_delta": {p: round(d, 4) for p, d in sorted(proj_delta.items())}}
    enforced_testable = [f for f in ENFORCED if verdicts[f]["verdict"] != "untestable"]
    enforced_lb = [f for f in ENFORCED if verdicts[f]["verdict"] == "load-bearing"]
    prose_lb = [f for f in PROSE if verdicts[f]["verdict"] == "load-bearing"]
    if enforced_lb:
        overall = "load-bearing"
    elif len(enforced_testable) >= MIN_TESTABLE_ENFORCED:
        overall = "kill"
    else:
        overall = "corpus too homogeneous"
    return {
        "population": {"briefs": len(rows), "lift_population": len(pop),
                       "positive": sum(1 for r in pop if r["positive"]),
                       "excluded_positive_only": sum(1 for r in rows if r["explicit"]
                                                     and r["label_source"] in POSITIVE_ONLY_SOURCES),
                       "by_source": _count_by(pop, "source"), "by_label_source": _count_by(pop, "label_source"),
                       "by_project": _count_by(pop, "project"), "by_agent_kind": _count_by(pop, "agent_kind")},
        "thresholds": {"min_arm_n": MIN_ARM, "min_lift": MIN_LIFT, "min_projects": MIN_PROJECTS,
                       "min_testable_enforced": MIN_TESTABLE_ENFORCED},
        "prevalence": prevalence, "features": verdicts, "cells": cells_by_feat,
        "enforced_testable": enforced_testable, "enforced_load_bearing": enforced_lb,
        "prose_load_bearing": prose_lb,
        "contradicts_spine": bool(prose_lb) and not enforced_lb,
        "verdict": overall,
    }


def lift_markdown(res: dict) -> str:
    L = ["## Population", ""]
    pop = res["population"]
    L.append(f"briefs={pop['briefs']} lift_population={pop['lift_population']} positive={pop['positive']} "
             f"excluded_positive_only={pop['excluded_positive_only']}")
    for k in ("by_source", "by_label_source", "by_project", "by_agent_kind"):
        L.append(f"\n{k}: " + ", ".join(f"{a}={b}" for a, b in pop[k].items()))
    L += ["", "## Feature prevalence and verdict", "",
          "| feature | group | prevalence (all) | prevalence (lift pop) | testable projects | holding >=10pt | verdict |",
          "|---|---|---|---|---|---|---|"]
    for f, v in res["features"].items():
        pv = res["prevalence"][f]
        L.append(f"| {f} | {v['group']} | {pv['all_briefs']} | {pv['lift_population']} | "
                 f"{len(v['testable_projects'])} | {len(v['projects_holding_10pt'])} | {v['verdict']} |")
    L += ["", "## Lift cells (n>=20 each arm marked valid)", "",
          "| feature | project | agent_kind | n_with | n_without | rate_with | rate_without | delta | valid |",
          "|---|---|---|---|---|---|---|---|---|"]
    for f, cells in res["cells"].items():
        for c in cells:
            if c["n_with"] + c["n_without"] == 0:
                continue
            L.append(f"| {f} | {c['project']} | {c['agent_kind']} | {c['n_with']} | {c['n_without']} | "
                     f"{c['rate_with']} | {c['rate_without']} | {c['delta']} | {c['valid']} |")
    L += ["", f"enforced testable: {res['enforced_testable']}",
          f"enforced load-bearing: {res['enforced_load_bearing']}",
          f"prose load-bearing: {res['prose_load_bearing']}  contradicts_spine={res['contradicts_spine']}",
          f"\nVERDICT: {res['verdict']}"]
    return "\n".join(L)


def lift_cmd(ns: argparse.Namespace) -> dict:
    res = lift(load_briefs(Path(ns.briefs)))
    if ns.md:
        Path(ns.md).write_text(lift_markdown(res))
    if ns.compact:
        res = {k: v for k, v in res.items() if k != "cells"}
    return res


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="champion", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    briefs_default = DEFAULTS["phase0_dir"] / "briefs.jsonl"

    e = sub.add_parser("extract", help="write briefs.jsonl (counts only on stdout)")
    e.add_argument("--since-days", type=int, default=None)
    e.add_argument("--project", default=None)
    e.add_argument("--out", default=str(briefs_default))
    for k in ("transcripts_root", "projects_root", "runs_db", "orca_db", "mmx_root"):
        e.add_argument("--" + k.replace("_", "-"), dest=k, default=str(DEFAULTS[k]))
    e.set_defaults(fn=extract)

    p = sub.add_parser("pairs", help="same-task pair manifests (explicit labels only, opposite outcomes)")
    p.add_argument("--population", choices=["retry", "cohort", "recipe", "similar", "all"], default="all")
    p.add_argument("--briefs", default=str(briefs_default))
    p.add_argument("--out-dir", default=str(DEFAULTS["phase0_dir"]))
    p.add_argument("--threshold", type=float, default=0.8)
    p.set_defaults(fn=pairs_cmd)

    b = sub.add_parser("bundle", help="bounded workspace bundle for a run_id or brief sha")
    b.add_argument("ident")
    b.add_argument("--briefs", default=str(briefs_default))
    b.add_argument("--out-dir", default=str(DEFAULTS["phase0_dir"] / "bundles"))
    b.add_argument("--repo", default=None, help="repo path used to recover a branch diff")
    b.add_argument("--limit", type=int, default=5)
    b.set_defaults(fn=bundle_cmd)

    l = sub.add_parser("lift", help="Experiment B: brief-feature lift, prevalence, verdict")
    l.add_argument("--briefs", default=str(briefs_default))
    l.add_argument("--md", default=None, help="also write a markdown rendering here")
    l.add_argument("--compact", action="store_true", help="omit per-cell tables from stdout")
    l.set_defaults(fn=lift_cmd)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ns = build_parser().parse_args(argv)
    print(json.dumps(ns.fn(ns), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
