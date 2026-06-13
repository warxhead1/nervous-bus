#!/usr/bin/env python3
"""tier2/analyst.py — Reflexarc Tier-2: off-peak deer-flow analyst over labeled runs.

Scheduled OFF-PEAK by a systemd timer (fires between 01:00–05:00 UTC) to keep
LLM analysis cost low.  Reads outcome-labeled runs from the run-store READ-ONLY
(open SQLite with uri=true&mode=ro), batches per project, asks MiniMax/Anthropic
the contrast question ("what distinguishes failed/thrashed/abandoned from clean
runs in this project?"), and emits ``<project>.pattern.discovered.v1`` for each
semantic insight found.

Cost unit: REQUESTS (MiniMax coding-plan = flat-rate; track requests, not
dollars).  One LLM call per project per analyst run.  Off-peak scheduling
plus a minimum-labeled-runs gate keep the request count low.

Usage:
    python3 analyst.py [--db-path PATH] [--dry-run] [--verbose]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"

# Minimum labeled runs a project must have on EACH side of the contrast before
# we fire an LLM call.  Below this, emit nothing — the contrast set is too thin
# to be meaningful.
MIN_RUNS_PER_SIDE = 2

# Outcome groups for the contrast
_FAILED_OUTCOMES = frozenset({"thrashed", "abandoned", "reverted"})
_CLEAN_OUTCOMES = frozenset({"clean", "landed", "corrected"})

# ── MiniMax / Anthropic call shape (mirrors pattern_consumer.py) ───────────────
MINIMAX_ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"
MINIMAX_MODEL = os.environ.get("TIER2_ANALYST_MODEL", "MiniMax-M2.7-highspeed")
MINIMAX_CONNECT_TIMEOUT_S = float(os.environ.get("TIER2_CONNECT_TIMEOUT_S", "10"))
MINIMAX_READ_TIMEOUT_S = float(os.environ.get("TIER2_READ_TIMEOUT_S", "240"))

FALLBACK_ENDPOINT = "https://api.anthropic.com/v1/messages"
FALLBACK_MODEL = os.environ.get("TIER2_FALLBACK_MODEL", "claude-haiku-4-5-20251001")
FALLBACK_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

NBUS_ROOT = os.environ.get("NBUS_ROOT", str(Path(__file__).resolve().parents[3]))


# ── ULID ──────────────────────────────────────────────────────────────────────

def _ulid() -> str:
    ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    ts = int(time.time() * 1000)
    ts_part = ""
    t = ts
    for _ in range(10):
        ts_part = ENCODING[t % 32] + ts_part
        t //= 32
    rnd = int.from_bytes(os.urandom(10), "big")
    rnd_part = ""
    for _ in range(16):
        rnd_part = ENCODING[rnd % 32] + rnd_part
        rnd //= 32
    return ts_part + rnd_part


# ── Off-peak guard ────────────────────────────────────────────────────────────

def _is_off_peak() -> bool:
    """Return True if current UTC hour is in the off-peak window (01:00–05:59).

    The systemd timer OnCalendar already gates this; this guard is a second line
    of defence for manual / test invocations.  Pass TIER2_SKIP_OFFPEAK_CHECK=1
    to bypass in tests.
    """
    if os.environ.get("TIER2_SKIP_OFFPEAK_CHECK", "0") == "1":
        return True
    hour = datetime.now(timezone.utc).hour
    return 1 <= hour <= 5


# ── Run-store (read-only) ─────────────────────────────────────────────────────

def _open_db_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the SQLite DB in read-only mode using the URI interface."""
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def _load_labeled_runs(db_path: Path) -> list[dict]:
    """Return all outcome-labeled runs with their features."""
    if not db_path.exists():
        return []
    conn = _open_db_readonly(db_path)
    try:
        cur = conn.execute(
            """
            SELECT run_id, project, agent_kind, started, ended,
                   close_reason, event_count, tool_histogram,
                   git_branch, bead_id,
                   outcome, labeled_at, label_history, features
            FROM runs
            WHERE outcome IS NOT NULL AND labeled_at IS NOT NULL
            ORDER BY started ASC
            """
        )
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            for field in ("tool_histogram", "label_history", "features"):
                if d.get(field):
                    try:
                        d[field] = json.loads(d[field])
                    except Exception:
                        d[field] = {}
            rows.append(d)
        return rows
    finally:
        conn.close()


# ── Contrast-set builder ───────────────────────────────────────────────────────

def build_contrast_sets(
    runs: list[dict],
) -> dict[str, dict[str, list[dict]]]:
    """Group labeled runs by project, then by failed vs clean.

    Returns:
        {
            project_name: {
                "failed": [run_dict, ...],
                "clean":  [run_dict, ...],
            },
            ...
        }

    Only includes projects with MIN_RUNS_PER_SIDE on EACH side.
    """
    # Partition by project
    by_project: dict[str, list[dict]] = {}
    for run in runs:
        proj = run.get("project") or "unknown"
        by_project.setdefault(proj, []).append(run)

    result: dict[str, dict[str, list[dict]]] = {}
    for proj, proj_runs in by_project.items():
        failed = [r for r in proj_runs if r.get("outcome") in _FAILED_OUTCOMES]
        clean = [r for r in proj_runs if r.get("outcome") in _CLEAN_OUTCOMES]
        if len(failed) >= MIN_RUNS_PER_SIDE and len(clean) >= MIN_RUNS_PER_SIDE:
            result[proj] = {"failed": failed, "clean": clean}

    return result


# ── Prompt builder ────────────────────────────────────────────────────────────

def _summarise_run(run: dict) -> str:
    """Compact one-line summary of a single run for prompt injection."""
    outcome = run.get("outcome", "?")
    agent_kind = run.get("agent_kind", "?")
    event_count = run.get("event_count", 0)
    close_reason = run.get("close_reason", "?")
    features: dict = run.get("features") or {}

    parts = [
        f"outcome={outcome}",
        f"agent_kind={agent_kind}",
        f"events={event_count}",
        f"close_reason={close_reason}",
    ]
    if features.get("thrash_edit_fail_loops"):
        parts.append(f"thrash_loops={features['thrash_edit_fail_loops']}")
    if features.get("bash_fail_rate") is not None:
        parts.append(f"bash_fail_rate={features['bash_fail_rate']:.2f}")
    if features.get("reread_rate") is not None:
        parts.append(f"reread_rate={features['reread_rate']:.2f}")
    if features.get("has_resolving_commit"):
        parts.append("has_commit=yes")

    tool_hist: dict = run.get("tool_histogram") or {}
    if tool_hist:
        top = sorted(tool_hist.items(), key=lambda x: -x[1])[:4]
        parts.append("top_tools=" + ",".join(f"{k}:{v}" for k, v in top))

    return "  " + "  ".join(parts)


def build_prompt(
    project: str,
    failed_runs: list[dict],
    clean_runs: list[dict],
) -> str:
    """Build the LLM contrast-analysis prompt for a project."""
    lines = [
        f"You are doing cross-run analysis for project '{project}'.",
        "",
        f"I have {len(failed_runs)} FAILED/THRASHED/ABANDONED run(s) and "
        f"{len(clean_runs)} CLEAN/LANDED run(s) recorded by the Reflexarc system.",
        "Each run is a Claude Code agent session. Features come from tool-call telemetry.",
        "",
        "## FAILED runs (outcome in {thrashed, abandoned, reverted})",
    ]
    for run in failed_runs[:10]:
        lines.append(_summarise_run(run))

    lines += [
        "",
        "## CLEAN runs (outcome in {clean, landed, corrected})",
    ]
    for run in clean_runs[:10]:
        lines.append(_summarise_run(run))

    lines += [
        "",
        "## Your task",
        "Identify SEMANTIC cross-run patterns that distinguish the failed group",
        "from the clean group in this project. Focus on:",
        "1. Patterns in tool-use ratios (high reread_rate, high bash_fail_rate, "
        "thrash loops) that are systematically higher in failed runs.",
        "2. Agent-kind or close-reason combinations that correlate with failure.",
        "3. Any pattern that a deterministic threshold detector WOULD MISS —",
        "   e.g. a qualitative combination rather than a single metric spike.",
        "4. Concrete fix candidates: if you see a pattern that implies a systematic",
        "   root cause (not just 'agent was unlucky'), say what kind of rule or",
        "   skill would prevent it.",
        "",
        "Produce ONLY a JSON array (no markdown fences, no commentary outside JSON).",
        "Each element is a pattern object:",
        "[",
        "  {",
        '    "pattern_name": "short slug, e.g. high-reread-before-thrash",',
        '    "description": "one paragraph — what you see and why it matters",',
        '    "occurrences": <integer — how many failed runs show this>,',
        '    "evidence": ["run feature summary or observation …"],',
        '    "proposed_fix": {',
        '      "type": "skill" | "rule" | "code" | null,',
        '      "description": "concrete fix candidate or null if unclear"',
        "    }",
        "  }",
        "]",
        "",
        "Return [] if no meaningful pattern separates the two groups.",
        "Return at most 5 patterns.",
    ]

    return "\n".join(lines)


# ── LLM call (mirrors pattern_consumer.py) ────────────────────────────────────

try:
    import httpx as _httpx_module
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


async def _call_minimax(api_key: str, prompt: str) -> tuple[list[dict] | None, str | None]:
    """Call MiniMax.  Returns (patterns, error_kind)."""
    if not _HTTPX_AVAILABLE:
        return None, "no_httpx"
    import httpx
    payload = {
        "model": MINIMAX_MODEL,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }
    timeout = httpx.Timeout(
        connect=MINIMAX_CONNECT_TIMEOUT_S,
        read=MINIMAX_READ_TIMEOUT_S,
        write=10.0,
        pool=5.0,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                MINIMAX_ENDPOINT,
                json=payload,
                headers={
                    "anthropic-version": "2023-06-01",
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code in (401, 403, 429):
                return None, "rate_limit"
            if resp.status_code != 200:
                sys.stderr.write(
                    f"tier2-analyst: MiniMax {resp.status_code}: {resp.text[:200]}\n"
                )
                return None, "transient"
            return _parse_response(resp.json()), None
    except Exception as e:
        sys.stderr.write(f"tier2-analyst: MiniMax error: {e}\n")
        return None, "transient"


async def _call_anthropic(prompt: str) -> tuple[list[dict] | None, str | None]:
    """Anthropic fallback.  Returns (patterns, error_kind)."""
    if not FALLBACK_API_KEY:
        return None, "no_fallback_key"
    if not _HTTPX_AVAILABLE:
        return None, "no_httpx"
    import httpx
    payload = {
        "model": FALLBACK_MODEL,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": prompt}],
    }
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                FALLBACK_ENDPOINT,
                json=payload,
                headers={
                    "x-api-key": FALLBACK_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            if resp.status_code in (401, 403, 429):
                return None, "rate_limit"
            if resp.status_code != 200:
                sys.stderr.write(
                    f"tier2-analyst: Anthropic {resp.status_code}: {resp.text[:200]}\n"
                )
                return None, "transient"
            return _parse_response(resp.json()), None
    except Exception as e:
        sys.stderr.write(f"tier2-analyst: Anthropic error: {e}\n")
        return None, "transient"


def _parse_response(data: dict) -> list[dict] | None:
    """Extract the JSON pattern array from an Anthropic-format response."""
    blocks = data.get("content") or []
    text_parts = [
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    text = "".join(text_parts).strip()
    text = re.sub(r"^```json?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except Exception as e:
        sys.stderr.write(f"tier2-analyst: JSON parse error: {e}  raw={text[:300]}\n")
    return None


# ── Emit pattern.discovered ───────────────────────────────────────────────────

def _emit_pattern(project: str, pattern: dict, dry_run: bool, verbose: bool) -> bool:
    """Emit <project>.pattern.discovered.v1 via `nervous publish`."""
    channel = f"{project}.pattern.discovered.v1"
    payload = {
        "project": project,
        "pattern_name": pattern.get("pattern_name", "unknown"),
        "occurrences": int(pattern.get("occurrences", 1)),
        "evidence": pattern.get("evidence") or [],
    }
    fix = pattern.get("proposed_fix")
    if fix and fix.get("type"):
        payload["proposed_patch"] = {
            "type": fix["type"],
            "content": fix.get("description") or "",
        }

    if verbose:
        print(
            f"  [emit] channel={channel} "
            f"pattern={payload['pattern_name']} "
            f"occurrences={payload['occurrences']}"
        )

    if dry_run:
        return True

    try:
        result = subprocess.run(
            ["nervous", "publish", channel, json.dumps(payload)],
            capture_output=True,
            timeout=5,
            cwd=NBUS_ROOT,
        )
        return result.returncode == 0
    except Exception as e:
        sys.stderr.write(f"tier2-analyst: emit error: {e}\n")
        return False


# ── Main analysis loop ────────────────────────────────────────────────────────

def analyse_project(
    project: str,
    failed_runs: list[dict],
    clean_runs: list[dict],
    api_key: str,
    dry_run: bool,
    verbose: bool,
    request_counter: list[int],
) -> list[dict]:
    """Run contrast analysis for one project.  Returns emitted patterns."""
    prompt = build_prompt(project, failed_runs, clean_runs)

    if verbose:
        print(
            f"\n[tier2] project={project}  "
            f"failed={len(failed_runs)}  clean={len(clean_runs)}"
        )

    if dry_run:
        # In dry-run mode, return a synthetic placeholder pattern showing
        # what would be sent without spending an LLM call.
        placeholder: dict = {
            "pattern_name": "dry-run-placeholder",
            "description": (
                f"[DRY RUN] Would contrast {len(failed_runs)} failed runs against "
                f"{len(clean_runs)} clean runs for project '{project}'."
            ),
            "occurrences": len(failed_runs),
            "evidence": [_summarise_run(r).strip() for r in (failed_runs + clean_runs)[:4]],
            "proposed_fix": {"type": None, "description": None},
        }
        return [placeholder]

    if not api_key:
        sys.stderr.write(
            "tier2-analyst: MINIMAX_API_KEY not set — skipping LLM call\n"
        )
        return []

    patterns, err = asyncio.run(_call_minimax(api_key, prompt))
    request_counter[0] += 1

    if err == "rate_limit":
        sys.stderr.write("tier2-analyst: MiniMax rate-limited; trying Anthropic fallback\n")
        patterns, err = asyncio.run(_call_anthropic(prompt))
        request_counter[0] += 1

    if patterns is None:
        sys.stderr.write(f"tier2-analyst: LLM call failed for project={project}: {err}\n")
        return []

    emitted = []
    for pat in (patterns or []):
        if not isinstance(pat, dict):
            continue
        ok = _emit_pattern(project, pat, dry_run=dry_run, verbose=verbose)
        if ok:
            emitted.append(pat)

    return emitted


def main() -> int:
    parser = argparse.ArgumentParser(description="Reflexarc Tier-2 off-peak analyst")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print contrast sets and placeholder output; no LLM calls, no emit",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass off-peak time guard (for manual invocations / CI)",
    )
    args = parser.parse_args()

    if not args.force and not _is_off_peak() and not args.dry_run:
        sys.stderr.write(
            "tier2-analyst: not in off-peak window (01:00–05:59 UTC); exiting. "
            "Pass --force to override.\n"
        )
        return 0

    runs = _load_labeled_runs(args.db_path)
    if not runs:
        sys.stderr.write(
            f"tier2-analyst: no labeled runs found in {args.db_path}; exiting.\n"
        )
        return 0

    contrast_sets = build_contrast_sets(runs)

    if not contrast_sets:
        total_labeled = len(runs)
        failed_total = sum(1 for r in runs if r.get("outcome") in _FAILED_OUTCOMES)
        clean_total = sum(1 for r in runs if r.get("outcome") in _CLEAN_OUTCOMES)
        sys.stderr.write(
            f"tier2-analyst: no project has >= {MIN_RUNS_PER_SIDE} runs on each side "
            f"of the contrast.  labeled={total_labeled}, failed={failed_total}, "
            f"clean={clean_total}.  More labeled data needed.\n"
        )
        return 0

    api_key = os.environ.get("MINIMAX_API_KEY", "")
    if not api_key and not args.dry_run:
        sys.stderr.write(
            "tier2-analyst: MINIMAX_API_KEY not set; "
            "will try Anthropic fallback or exit.\n"
        )

    request_counter = [0]
    total_emitted = 0

    for project, sides in contrast_sets.items():
        emitted = analyse_project(
            project=project,
            failed_runs=sides["failed"],
            clean_runs=sides["clean"],
            api_key=api_key,
            dry_run=args.dry_run,
            verbose=args.verbose,
            request_counter=request_counter,
        )
        total_emitted += len(emitted)
        if args.verbose or args.dry_run:
            for pat in emitted:
                print(
                    f"  pattern: {pat.get('pattern_name')}  "
                    f"occurrences={pat.get('occurrences')}"
                )
            if not emitted:
                print(f"  [no patterns emitted for {project}]")

    mode = "[DRY-RUN] " if args.dry_run else ""
    sys.stderr.write(
        f"tier2-analyst: {mode}done.  projects_analysed={len(contrast_sets)}  "
        f"patterns_emitted={total_emitted}  llm_requests={request_counter[0]}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
