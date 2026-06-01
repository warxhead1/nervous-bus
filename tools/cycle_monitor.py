#!/usr/bin/env python3
"""Goal-tracking monitor for one autobench cycle session.

Runs as a polling sentinel: every INTERVAL seconds reads the nervous-bus
debug.jsonl, computes the metrics that matter (not just event counts),
checks them against the baseline (recovered_cycles/2026-05-17_larger_plan_killed.json),
and appends one JSON line to the audit log. Goals carry an explicit
``status`` (pass / fail / pending) so a glance at the tail tells you
whether THIS cycle is actually beating the prior one.

Goals (vs killed-cycle baseline):
    1. judges_firing        — autobench.judge.pool.verdict.v1 > 0
                              (was 0 across every prior cycle — Move #2)
    2. classifier_firing    — autobench.failure.category.v1 > 0
                              (new channel from Move #4)
    3. ce_rate_below_baseline — fraction of CE verdicts < 0.50
                              (was 0.70 in killed cycle; CRITICAL OUTPUT
                              RULE should drop it)
    4. cross_domain_active  — non-cf-tier-1 cases scored
                              (shader_tier1 was 0.0 in killed cycle)
    5. cycle_report_at_exit — autobench.cycle.report.v1 fires with our
                              session_id when cycle ends (Move #3)

Usage:
    python -m tools.cycle_monitor --session 01KRT...M3 [--interval 60]
        [--baseline-ce 0.70] [--log path]

The monitor exits when the cycle process exits (detected via no new
events for STALE_MIN minutes AND a final cycle.report.v1 fires, OR
just no new events for STALE_MIN*3 minutes as a hard timeout).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Bus event types we count and their human-readable goal anchors.
CH_CASE_RESULT = "autobench.case.result.v1"
CH_JUDGE_POOL = "autobench.judge.pool.verdict.v1"
CH_JUDGE_DISAGREEMENT = "autobench.judge.disagreement.v1"
CH_FAILURE_CATEGORY = "autobench.failure.category.v1"
CH_CROSS_DOMAIN = "autobench.cross_domain.evaluation.v1"
CH_CYCLE_REPORT = "autobench.cycle.report.v1"
CH_SANDBOX_STDERR = "autobench.sandbox.stderr.v1"
CH_WORKER = "autobench.worker.v1"

# How long with no new events before we consider the cycle done.
STALE_MIN = 5


def _read_session_events(path: Path, session_id: str) -> list[dict[str, Any]]:
    """Return all events in path whose data.session_id matches."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Quick string filter before JSON parse — most lines won't match.
                if session_id not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = e.get("data", {})
                if isinstance(d, dict) and d.get("session_id") == session_id:
                    out.append(e)
    except Exception:  # noqa: BLE001 — never raise from the poller
        pass
    return out


def _ce_rate(events: list[dict[str, Any]]) -> tuple[float, int]:
    """Compute CE fraction over case.result events. Returns (rate, n)."""
    verdicts = [
        e["data"].get("verdict", "?")
        for e in events
        if e.get("type") == CH_CASE_RESULT
    ]
    n = len(verdicts)
    if n == 0:
        return (0.0, 0)
    ce = sum(1 for v in verdicts if v == "CE")
    return (ce / n, n)


def _cross_domain_active(events: list[dict[str, Any]]) -> bool:
    """True if at least one non-cf-tier-1 case has produced a result."""
    for e in events:
        if e.get("type") != CH_CASE_RESULT:
            continue
        cid = e["data"].get("case_id", "")
        if not cid.startswith("cf-") and cid != "":
            return True
    return False


def _compute_snapshot(
    events: list[dict[str, Any]],
    baseline_ce: float,
    session_id: str,
    last_event_age_s: int,
) -> dict[str, Any]:
    """Build one audit line from the current event window."""
    type_counts = Counter(e.get("type", "?") for e in events)
    ce_rate, n_results = _ce_rate(events)
    failure_cats = Counter(
        e["data"].get("category", "?")
        for e in events
        if e.get("type") == CH_FAILURE_CATEGORY
    )

    judges_n = type_counts.get(CH_JUDGE_POOL, 0)
    classifier_n = type_counts.get(CH_FAILURE_CATEGORY, 0)
    cycle_report_n = type_counts.get(CH_CYCLE_REPORT, 0)
    cross_dom_active = _cross_domain_active(events)

    goals = {
        "judges_firing": {
            "target": "> 0 judge.pool.verdict events",
            "value": judges_n,
            "status": "pass" if judges_n > 0 else "pending",
        },
        "classifier_firing": {
            "target": "> 0 failure.category events",
            "value": classifier_n,
            "status": "pass" if classifier_n > 0 else "pending",
        },
        "ce_rate_below_baseline": {
            "target": f"CE fraction < {baseline_ce:.2f}",
            "value": round(ce_rate, 3),
            "n_results": n_results,
            "status": (
                "pass"
                if (n_results >= 10 and ce_rate < baseline_ce)
                else ("fail" if (n_results >= 10 and ce_rate >= baseline_ce) else "pending")
            ),
        },
        "cross_domain_active": {
            "target": "≥1 non-cf-tier-1 case scored",
            "value": cross_dom_active,
            "status": "pass" if cross_dom_active else "pending",
        },
        "cycle_report_at_exit": {
            "target": "cycle.report.v1 with our session_id at end",
            "value": cycle_report_n,
            "status": "pass" if cycle_report_n > 0 else "pending",
        },
    }

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "total_events": len(events),
        "type_counts": dict(type_counts.most_common(12)),
        "case_results": n_results,
        "ce_rate": round(ce_rate, 3),
        "failure_categories": dict(failure_cats.most_common(8)),
        "last_event_age_s": last_event_age_s,
        "goals": goals,
    }


def _print_human(snap: dict[str, Any]) -> None:
    """Compact human-readable summary to stderr (for `journalctl -f`)."""
    g = snap["goals"]
    marks = {"pass": "✓", "fail": "✗", "pending": "·"}
    parts = []
    for k, v in g.items():
        parts.append(f"{marks.get(v['status'],'?')} {k}")
    print(
        f"[cycle-monitor] {snap['ts']} events={snap['total_events']} "
        f"results={snap['case_results']} CE={snap['ce_rate']:.2f} "
        f"| {' '.join(parts)}",
        file=sys.stderr,
    )


def _main() -> int:
    p = argparse.ArgumentParser(
        description="Goal-tracking monitor for one autobench cycle session.")
    p.add_argument("--session", required=True,
                   help="Session ID (26-char ULID) emitted by the cycle.")
    p.add_argument("--interval", type=int, default=60,
                   help="Poll interval in seconds (default: 60).")
    p.add_argument("--baseline-ce", type=float, default=0.70,
                   help="Killed-cycle CE-rate baseline (default: 0.70).")
    p.add_argument("--debug-file", default=None,
                   help="Override path to nervous-bus debug.jsonl.")
    p.add_argument("--log", default=None,
                   help="Audit log path (default: "
                        "~/.cache/nervous-bus/cycle-monitor-<session>.log).")
    p.add_argument("--max-stale-min", type=int, default=STALE_MIN * 3,
                   help="Exit if no new events for this many minutes.")
    args = p.parse_args()

    debug_path = Path(args.debug_file) if args.debug_file else (
        Path.home() / ".cache" / "nervous-bus" / "debug.jsonl"
    )
    log_path = Path(args.log) if args.log else (
        Path.home() / ".cache" / "nervous-bus"
        / f"cycle-monitor-{args.session[:8]}.log"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"[cycle-monitor] starting — session={args.session} "
        f"interval={args.interval}s log={log_path}",
        file=sys.stderr,
    )

    prev_total = 0
    stale_polls = 0
    max_stale_polls = max(1, (args.max_stale_min * 60) // args.interval)
    cycle_report_seen = False
    end_after_report = False

    while True:
        events = _read_session_events(debug_path, args.session)
        # last_event_age — pull the newest event's time and compare to now.
        last_age = -1
        if events:
            try:
                t = events[-1].get("time", "")
                if t:
                    # Event timestamps are UTC; calendar.timegm avoids the
                    # local-TZ shift that mktime applies and that produced
                    # negative ages on hosts not on UTC.
                    import calendar
                    last_dt = time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
                    last_age = int(time.time() - calendar.timegm(last_dt))
            except Exception:  # noqa: BLE001
                pass

        snap = _compute_snapshot(events, args.baseline_ce, args.session, last_age)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap) + "\n")
        _print_human(snap)

        # Termination logic. We exit when:
        #   - cycle.report.v1 fired AND one further poll passes (so the final
        #     state is recorded), OR
        #   - no new events for max_stale_min minutes (hard timeout).
        if snap["goals"]["cycle_report_at_exit"]["status"] == "pass":
            if end_after_report:
                print("[cycle-monitor] cycle.report.v1 seen; exiting.",
                      file=sys.stderr)
                return 0
            end_after_report = True
            cycle_report_seen = True

        if snap["total_events"] == prev_total:
            stale_polls += 1
        else:
            stale_polls = 0
        prev_total = snap["total_events"]

        if stale_polls >= max_stale_polls:
            print(
                f"[cycle-monitor] no new events for {args.max_stale_min}m; "
                f"exiting (cycle_report_seen={cycle_report_seen}).",
                file=sys.stderr,
            )
            return 0

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(_main())
