#!/usr/bin/env python3
"""nightly_analysis.py — Reflexarc nightly analysis batch (ecosystem-connections
packet R1: revive the dormant analysis half of reflex-recorder).

Capture (recorder.py) has run continuously as a systemd service since the
engine's earliest bead. Outcome labeling (label.py), the 12 detectors
(synthesis.py), and the struggle ledger (struggle_ledger.py) are code-complete
but were only ever invoked BY HAND — this is the scheduled batch that closes
that gap. See README.md "## Nightly Analysis Loop" for the charter framing.

Steps, run in order. Each is subprocess-wrapped with a hard timeout and a
logged-but-non-fatal failure: a slow/broken step never blocks the ones after
it, and a bad night never wedges the timer.

  1. label.py         --since-days LABEL_SINCE_DAYS --unlabeled-only
     Incremental outcome labeling. --unlabeled-only means settled outcomes
     (landed/reverted/abandoned/clean/thrashed) are never re-verified — only
     runs still sitting at outcome=NULL get another attempt (a PR may have
     merged, a bead may have closed). --since-days bounds cost against a
     growing run history. First run against a mostly-unlabeled DB may not
     finish inside the timeout; that's fine — progress commits per-row
     (autocommit), so the next night picks up where this one left off.
  2. synthesis.py     (default dry-run)
     Runs every built-in + private-overlay detector and persists hits/issues
     to detector_hits/issues. Dry-run means "no nervous publish calls" — the
     local persistence (what prevalence/issues queries read) happens either
     way, so this is the right invocation for an unattended nightly pass.
  3. struggle_ledger.py --days STRUGGLE_WINDOW_DAYS --json
     Longitudinal friction ledger over the durable transcript archive.
     Captured here (not persisted — it has no store of its own) purely to
     feed the digest below.

Then: writes/updates a rolling digest at ~/knowledge/indexed/shared/reflex-digest.md
(kb vault, id fixed so this rewrites in place rather than growing new files).
The digest is NOT git-committed by this script — the vault is a git repo but
this file is a rolling log; leave it untracked-or-dirty and commit manually
if you want history of a particular snapshot.

Usage:
    python3 nightly_analysis.py
    python3 nightly_analysis.py --db /path/to/runs.db --step-timeout 300
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DEFAULT_DB_PATH = Path.home() / ".cache" / "nervous-bus" / "reflex" / "runs.db"
DIGEST_PATH = Path.home() / "knowledge" / "indexed" / "shared" / "reflex-digest.md"

# Fixed once — do NOT regenerate. Keeping this constant is what makes each
# nightly run update the SAME kb-vault entry in place instead of forking a
# new file every night.
DIGEST_ID = "019f347a-a087-7f3b-abe3-7319d626d8ce"

DEFAULT_STEP_TIMEOUT = 300          # seconds; "suggest 300s/step"
DEFAULT_LABEL_SINCE_DAYS = 30       # bound label.py's pass against a growing history
DEFAULT_DIGEST_WINDOW_DAYS = 7      # per-project stats + detector prevalence window
DEFAULT_STRUGGLE_WINDOW_DAYS = 14   # struggle-ledger scan window
TOP_DETECTORS = 10
TOP_STRUGGLES = 8


class StepResult:
    def __init__(self, name: str, ok: bool, detail: str, duration: float,
                 stdout: str = "", stderr: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.duration = duration
        self.stdout = stdout
        self.stderr = stderr


def run_step(name: str, cmd: list[str], timeout: int) -> StepResult:
    """Run one batch step as a subprocess. Never raises — timeouts and
    non-zero exits are captured and reported, not propagated, so the caller
    can always move on to the next step."""
    t0 = time.monotonic()
    print(f"[nightly] >>> {name}: {' '.join(cmd)}", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd, cwd=str(HERE), capture_output=True, text=True, timeout=timeout,
        )
        dur = time.monotonic() - t0
        if proc.returncode != 0:
            print(f"[nightly] <<< {name} FAILED (exit {proc.returncode}, {dur:.1f}s)\n"
                  f"{proc.stderr[-2000:]}", file=sys.stderr)
            return StepResult(name, False, f"exit {proc.returncode}", dur,
                               proc.stdout, proc.stderr)
        print(f"[nightly] <<< {name} ok ({dur:.1f}s)", file=sys.stderr)
        return StepResult(name, True, "ok", dur, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        dur = time.monotonic() - t0
        print(f"[nightly] <<< {name} TIMED OUT after {dur:.1f}s (limit {timeout}s) — "
              f"non-fatal, moving on", file=sys.stderr)
        partial_out = (exc.stdout or b"")
        if isinstance(partial_out, bytes):
            partial_out = partial_out.decode(errors="replace")
        return StepResult(name, False, f"timeout after {timeout}s", dur, partial_out, "")
    except Exception as exc:
        dur = time.monotonic() - t0
        print(f"[nightly] <<< {name} EXCEPTION: {exc}", file=sys.stderr)
        return StepResult(name, False, str(exc), dur)


def _run_json(name: str, cmd: list[str], timeout: int) -> Optional[object]:
    """Run a --json-emitting query helper and parse its stdout. Returns None
    (never raises) on any failure — the digest section for that source is
    then rendered as 'unavailable' rather than blowing up the whole digest."""
    result = run_step(name, cmd, timeout)
    if not result.ok or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


# ── Digest ────────────────────────────────────────────────────────────────────

def build_digest(
    db_path: Path,
    label_result: StepResult,
    detector_result: StepResult,
    window_days: int,
    struggle_window_days: int,
    step_timeout: int,
) -> str:
    now = datetime.now(timezone.utc)
    stats = _run_json(
        "query.py stats (digest source)",
        [sys.executable, "query.py", "--db", str(db_path), "stats",
         "--days", str(window_days), "--json"],
        step_timeout,
    ) or []
    prevalence = _run_json(
        "query.py prevalence (digest source)",
        [sys.executable, "query.py", "--db", str(db_path), "prevalence",
         "--days", str(window_days), "--json"],
        step_timeout,
    ) or []
    struggles = _run_json(
        "struggle_ledger.py (digest source)",
        [sys.executable, "struggle_ledger.py", "--days", str(struggle_window_days), "--json"],
        step_timeout,
    ) or []

    lines: list[str] = []
    lines.append("---")
    lines.append(f"id: {DIGEST_ID}")
    lines.append("title: Reflex Weekly Digest")
    lines.append("project: shared")
    lines.append("category: log")
    lines.append("tags:")
    lines.append("- reflex")
    lines.append("- session-analysis")
    lines.append("source_type: agent-generated")
    lines.append(f"created: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append(f"updated: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("---")
    lines.append("")
    lines.append("# Reflex Weekly Digest")
    lines.append("")
    lines.append(
        f"<!-- Generated by nervous-bus/adapters/reflex-recorder/nightly_analysis.py. "
        f"Rolling snapshot, rewritten in place each run — NOT git-committed by the "
        f"batch job itself (manual-commit-if-wanted, per repo discipline). -->"
    )
    lines.append("")
    lines.append(f"Window: last {window_days}d (run/label/prevalence) · "
                 f"{struggle_window_days}d (struggle ledger). Generated {now.isoformat()}.")
    lines.append("")

    lines.append(f"## Batch health (this run)")
    lines.append("")
    lines.append(f"- outcome labeling: {'ok' if label_result.ok else label_result.detail} "
                 f"({label_result.duration:.0f}s)")
    lines.append(f"- detectors (synthesis.py): {'ok' if detector_result.ok else detector_result.detail} "
                 f"({detector_result.duration:.0f}s)")
    lines.append("")

    lines.append(f"## Run counts ({window_days}d)")
    lines.append("")
    if stats:
        lines.append("| project | runs | labeled | unlabeled |")
        lines.append("|---|---|---|---|")
        for row in sorted(stats, key=lambda r: -r.get("total_runs", 0)):
            lines.append(
                f"| {row['project']} | {row['total_runs']} | "
                f"{row['labeled_runs']} | {row['unlabeled_runs']} |"
            )
        lines.append("")
        lines.append("### Labeled-outcome distribution")
        lines.append("")
        totals: dict[str, int] = {}
        for row in stats:
            for outcome, count in (row.get("outcome_breakdown") or {}).items():
                totals[outcome] = totals.get(outcome, 0) + count
        if totals:
            for outcome, count in sorted(totals.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {outcome}: {count}")
        else:
            lines.append("- (no runs in window)")
    else:
        lines.append("- unavailable (query.py stats failed — see service log)")
    lines.append("")

    lines.append(f"## Top detector prevalence ({window_days}d)")
    lines.append("")
    if prevalence:
        top = sorted(prevalence, key=lambda r: -r.get("hit_runs", 0))[:TOP_DETECTORS]
        lines.append("| project | detector | hits | of runs | rate |")
        lines.append("|---|---|---|---|---|")
        for row in top:
            lines.append(
                f"| {row['project']} | {row['detector']} | {row['hit_runs']} | "
                f"{row['total_runs']} | {row['rate']:.0%} |"
            )
    else:
        lines.append("- no detector hits in window (or query unavailable)")
    lines.append("")

    lines.append(f"## Top open struggle-ledger items ({struggle_window_days}d)")
    lines.append("")
    if struggles:
        open_items = [s for s in struggles if s.get("status") == "open"]
        open_items.sort(key=lambda s: (-s.get("sessions", 0), -s.get("events", 0)))
        top = open_items[:TOP_STRUGGLES]
        if top:
            lines.append("| project | struggle | events | sessions | fix verdict |")
            lines.append("|---|---|---|---|---|")
            for s in top:
                lines.append(
                    f"| {s['project']} | {s['struggle']} | {s['events']} | "
                    f"{s['sessions']} | {s.get('fix_verdict', '?')} |"
                )
        else:
            lines.append("- no OPEN struggles in window (may still have dormant/resolved ones)")
    else:
        lines.append("- no struggle-ledger data (or query unavailable)")
    lines.append("")

    text = "\n".join(lines) + "\n"
    return text


def write_digest(text: str) -> None:
    DIGEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Preserve the original `created:` timestamp across runs if the file
    # already exists, so "created" reflects first-write, not last-write.
    if DIGEST_PATH.exists():
        try:
            old_created = next(
                (l for l in DIGEST_PATH.read_text().splitlines() if l.startswith("created:")),
                None,
            )
            if old_created:
                text = "\n".join(
                    old_created if l.startswith("created:") else l
                    for l in text.splitlines()
                ) + "\n"
        except Exception:
            pass
    DIGEST_PATH.write_text(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--step-timeout", type=int, default=DEFAULT_STEP_TIMEOUT)
    ap.add_argument("--label-since-days", type=int, default=DEFAULT_LABEL_SINCE_DAYS)
    ap.add_argument("--window-days", type=int, default=DEFAULT_DIGEST_WINDOW_DAYS)
    ap.add_argument("--struggle-window-days", type=int, default=DEFAULT_STRUGGLE_WINDOW_DAYS)
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[nightly] runs.db not found at {args.db} — nothing to do", file=sys.stderr)
        return 1

    print(f"[nightly] === reflex nightly analysis start {datetime.now(timezone.utc).isoformat()} ===",
          file=sys.stderr)

    label_result = run_step(
        "label.py",
        [sys.executable, "label.py", "--db-path", str(args.db),
         "--since-days", str(args.label_since_days), "--unlabeled-only"],
        args.step_timeout,
    )

    detector_result = run_step(
        "synthesis.py",
        [sys.executable, "synthesis.py", "--db", str(args.db)],
        args.step_timeout,
    )

    digest = build_digest(
        args.db, label_result, detector_result,
        args.window_days, args.struggle_window_days, args.step_timeout,
    )
    write_digest(digest)
    print(f"[nightly] digest written to {DIGEST_PATH}", file=sys.stderr)

    print(f"[nightly] === reflex nightly analysis done ===", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
