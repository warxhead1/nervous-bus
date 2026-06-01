#!/usr/bin/env python3
"""
telemetry_collect.py — Aggregates bus events from the nervous-bus debug log
into per-channel statistics and emits a structured telemetry snapshot used
by claims_audit.py for EDD confidence assessment.

Usage:
    python3 tools/telemetry_collect.py [--since YYYY-MM-DD] [--hours N] [--output FILE]

Output: JSON snapshot with per-channel stats, key relationship counts,
        and composite indicators used by claims_audit.py --telemetry mode.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allowlisted channels — internal/test-only, never cross the bus to real consumers
ALLOWLIST = frozenset([
    "test.channel",           # smoke-test artifact
    "bus.test",                # test channel, not production
    "agent.session",         # unversioned legacy — see below
    "soul.modified",          # internal soul-revision signal, no schema
    "deer-flow.audit.recommendation",  # allowlisted via schema_coverage_allowlist.txt
    "deer-flow.bead.enrichment.complete",  # orphan, no consumer
    "deer-flow.bead.pushback",  # orphan, no consumer
    "nervous-bus.claims.result.v1",  # claims_audit tool output, not production
])

# Versioned aliases: producers emit bare name, schema uses .v1 suffix
# These channels are VERSIONED in the schema dir but emitted unversioned
VERSIONED_ALIASES = {
    "agent.session":              "agent.session.v1",
    "bus.bead.created":          "bus.bead.lifecycle.v1",  # event_type=created
    "bus.bead.updated":          "bus.bead.lifecycle.v1",
    "bus.bead.closed":           "bus.bead.lifecycle.v1",
    "tengine.session.frame":     "tengine.session.frame.v1",
    "tengine.session.start":     "tengine.session.start.v1",
    "tengine.session.stop":       "tengine.session.stop.v1",
    "tengine.session.fps_drop":  "tengine.session.fps_drop.v1",
    "tengine.shadergen.cmd":     "tengine.shadergen.cmd.v1",
    "tengine.shadergen.screenshot": "tengine.shadergen.screenshot.v1",
    "tengine.stream.dump_ready": "tengine.stream.dump_ready.v1",
    "deer-flow.council.completed":   "deer-flow.council.completed.v1",
    "deer-flow.council.started":    "deer-flow.council.started.v1",
    "deer-flow.cumulative.hard":    "deer-flow.cumulative.hard.v1",
    "deer-flow.cumulative.exit":   "deer-flow.cumulative.exit.v1",
    "deer-flow.cumulative.soft":   "deer-flow.cumulative.soft.v1",
    "deer-flow.cumulative.soft": "deer-flow.cumulative.soft.v1",
    "deer-flow.cycle.wait.exit": "deer-flow.cycle.wait.exit.v1",
    "deer-flow.telemetry.migration.promoted": "deer-flow.telemetry.migration.promoted.v1",
    "deer-flow.telemetry.migration.rollback": "deer-flow.telemetry.migration.rollback.v1",
    "deer-flow.metaprobe.cycle": "deer-flow.metaprobe.cycle.v1",
    "deer-flow.claim.verified":  "deer-flow.claim.verified.v1",
    "deer-flow.tool.call":        "deer-flow.tool.call.v1",
    "tengine.session.client_connected":    "tengine.session.client_connected.v1",
    "tengine.session.client_disconnected": "tengine.session.client_disconnected.v1",
    "deer-flow.openrouter.credit_exhausted": "deer-flow.openrouter.credit_exhausted.v1",
    "bus.triage.findings":              "bus.triage.findings.v1",
}

# Channel → claim mapping
CHANNEL_CLAIM_MAP = {
    "deer-flow.subagent.lifecycle.v1": "deer-flow.subagent.terminal-guarantee",
    "deer-flow.agent.thread.v1":        "deer-flow.orchestrator.efficiency",
    "deer-flow.council.session.v1":     "deer-flow.council.completeness",
    "deer-flow.council.completed.v1":   "deer-flow.council.completeness",
    "deer-flow.stack-tuner.cycle.start.v1": "deer-flow.stack-tuner.cycle-completeness",
    "deer-flow.stack-tuner.cycle.done.v1":  "deer-flow.stack-tuner.cycle-completeness",
    "bus.bead.lifecycle.v1":            "bus.bead.lifecycle-completeness",
    "autobench.curriculum.cycle.v1":    "autobench.curriculum.cycle.validated-output",
    "autobench.improver.prediction.v1": "autobench.improver.prediction-confidence",
    "autobench.improver.prediction.verified.v1": "autobench.improver.prediction-confidence",
    "autobench.improver.prediction.refuted_live.v1": "autobench.improver.prediction-confidence",
    "autobench.iteration.v1":           "autobench.improver.prediction-confidence",
}


def resolve_channel(raw: str) -> str:
    """Resolve an event's raw channel name to its canonical schema channel."""
    if raw in VERSIONED_ALIASES:
        return VERSIONED_ALIASES[raw]
    return raw


def parse_time(ts: str) -> datetime | None:
    """Parse RFC3339 or ISO8601 with/without timezone."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts[:26], fmt[:26])
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_events(debug_log: Path, since: datetime | None = None) -> list[dict]:
    """Load events from debug log, resolve channels, apply time filter."""
    events = []
    for line in open(debug_log):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        raw_type = d.get("type", "")
        if raw_type in ALLOWLIST:
            continue

        # Resolve unversioned → canonical schema channel
        canonical = resolve_channel(raw_type)
        d["_canonical_channel"] = canonical

        if since:
            ts = parse_time(d.get("time", ""))
            if ts and ts < since:
                continue
        events.append(d)
    return events


def aggregate(events: list[dict]) -> dict:
    """Compute per-channel and cross-channel statistics."""
    by_channel_raw: dict[str, list[dict]] = defaultdict(list)
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_channel_raw[e.get("type", "unknown")].append(e)
        by_channel[e.get("_canonical_channel", "unknown")].append(e)

    stats = {
        "total_events": len(events),
        "unique_raw_channels": len(by_channel_raw),
        "unique_canonical_channels": len(by_channel),
        "by_channel": {},
        "relationships": {},
        "claims": {},
    }

    # ── Per-channel stats ────────────────────────────────────────────────────
    for channel, evts in sorted(by_channel.items(), key=lambda x: -len(x[1])):
        by_status = defaultdict(int)
        by_event_type = defaultdict(int)
        for e in evts:
            data = e.get("data", {})
            by_status[data.get("status", "unknown")] += 1
            by_event_type[data.get("event_type", data.get("disposition", "unknown"))] += 1

        times = [parse_time(e.get("time", "")) for e in evts if e.get("time")]
        times = [t for t in times if t]
        times.sort()

        stats["by_channel"][channel] = {
            "count": len(evts),
            "raw_names": sorted({e.get("type", "") for e in evts}),
            "status_breakdown": dict(by_status),
            "event_type_breakdown": dict(by_event_type),
            "first_seen": times[0].isoformat() if times else None,
            "last_seen": times[-1].isoformat() if times else None,
        }

    # ── Relationship: subagent terminal guarantee ────────────────────────
    sub_lifecycle = by_channel.get("deer-flow.subagent.lifecycle.v1", [])
    terminal = [e for e in sub_lifecycle
                if e.get("data", {}).get("status") in ("completed", "failed", "timed_out")]
    all_thread_ids = {e.get("data", {}).get("thread_id")
                      for e in by_channel.get("deer-flow.agent.thread.v1", [])}
    true_orphans = [e for e in terminal
                    if e.get("data", {}).get("thread_id") and
                    e.get("data", {}).get("thread_id") not in all_thread_ids]

    stats["relationships"]["subagent_terminal_guarantee"] = {
        "claim": "deer-flow.subagent.terminal-guarantee",
        "terminal_subagent_count": len(terminal),
        "orphan_terminal_count": len(true_orphans),
        "parent_thread_count": len(all_thread_ids),
        "confidence": "HIGH" if len(true_orphans) == 0 else "LOW",
        "rationale": f"{len(true_orphans)} orphan terminal subagents of {len(terminal)} total",
    }

    # ── Relationship: bead lifecycle completeness ─────────────────────────
    bead_lifecycle = by_channel.get("bus.bead.lifecycle.v1", [])
    terminal_beads = [e for e in bead_lifecycle
                      if e.get("data", {}).get("event_type") in
                      ("opened", "pr_merged", "pr_closed", "dead", "rejected")]
    created_beads = {e.get("data", {}).get("bead_id") for e in bead_lifecycle
                     if e.get("data", {}).get("event_type") == "created"}
    orphan_beads = [e for e in terminal_beads
                    if e.get("data", {}).get("bead_id") not in created_beads]

    sla_breaches = []
    for e in terminal_beads:
        if e.get("data", {}).get("event_type") != "opened":
            continue
        bid = e.get("data", {}).get("bead_id", "")
        created = next((c for c in bead_lifecycle
                        if c.get("data", {}).get("bead_id") == bid and
                        c.get("data", {}).get("event_type") == "created"), None)
        if created:
            c_ts = parse_time(created.get("time", ""))
            t_ts = parse_time(e.get("time", ""))
            if c_ts and t_ts and abs((t_ts - c_ts).total_seconds()) > 30 * 60:
                sla_breaches.append(f"{bid}: {abs((t_ts - c_ts).total_seconds())/60:.1f}min")

    stats["relationships"]["bead_lifecycle_completeness"] = {
        "claim": "bus.bead.lifecycle-completeness",
        "terminal_bead_count": len(terminal_beads),
        "orphan_terminal_count": len(orphan_beads),
        "created_bead_count": len(created_beads),
        "sla_breach_count": len(sla_breaches),
        "sla_breach_examples": sla_breaches[:5],
        "confidence": "HIGH" if len(orphan_beads) == 0 and len(sla_breaches) == 0 else
                      "MEDIUM" if len(orphan_beads) == 0 else "LOW",
        "rationale": f"{len(orphan_beads)} orphan terminals, {len(sla_breaches)} SLA breaches",
    }

    # ── Relationship: council completeness ────────────────────────────────
    # deer-flow uses deer-flow.council.started.v1 (not council.session.v1) as
    # the session-start channel; correlation is via council_id (not deliberation_id);
    # statuses are "running"/"completed" (not "agreed"/"disagreed").
    #
    # Note: deer-flow.council.completed fires TWO kinds of events:
    #  (a) deliberation completions with council_id + persona_deliberations (2 seen)
    #  (b) lightweight "verdict only" completions with no council_id (81 seen, different signal)
    # We only count type (a) as relevant to this claim.
    # council.started events have no status field — all such events open a session.
    council_sessions = by_channel.get("deer-flow.council.started.v1", [])
    council_completeds = by_channel.get("deer-flow.council.completed.v1", [])
    # Only completed events WITH council_id are deliberation completions
    session_ids = {e.get("data", {}).get("council_id") for e in council_sessions
                   if e.get("data", {}).get("council_id")}
    completed_ids = {e.get("data", {}).get("council_id") for e in council_completeds
                      if e.get("data", {}).get("council_id")}
    sessions_without_completion = [s for s in session_ids if s not in completed_ids]
    orphaned_completions = [c for c in completed_ids if c not in session_ids]

    stats["relationships"]["council_completeness"] = {
        "claim": "deer-flow.council.completeness",
        "active_session_count": len(session_ids),
        "completed_count": len(completed_ids),
        "sessions_without_completion": len(sessions_without_completion),
        "orphaned_completions": len(orphaned_completions),
        "confidence": "HIGH" if len(sessions_without_completion) == 0 and len(orphaned_completions) == 0 else
                      "MEDIUM" if len(orphaned_completions) == 0 else "LOW",
        "rationale": f"{len(orphaned_completions)} orphaned completions, "
                     f"{len(sessions_without_completion)} sessions without completion",
    }

    # ── Relationship: stack-tuner cycle completeness ────────────────────
    cycle_starts = by_channel.get("deer-flow.stack-tuner.cycle.start.v1", [])
    cycle_dones = by_channel.get("deer-flow.stack-tuner.cycle.done.v1", [])
    start_ids = {e.get("data", {}).get("cycle_id") for e in cycle_starts
                  if e.get("data", {}).get("cycle_id")}
    done_ids = {e.get("data", {}).get("cycle_id") for e in cycle_dones
                 if e.get("data", {}).get("cycle_id")}
    dangling_starts = [s for s in start_ids if s not in done_ids]
    orphan_dones = [d for d in done_ids if d not in start_ids]
    durations = [e.get("data", {}).get("duration_s") for e in cycle_dones
                 if e.get("data", {}).get("duration_s") is not None]
    avg_dur = sum(durations) / len(durations) if durations else 0

    stats["relationships"]["stack_tuner_cycle_completeness"] = {
        "claim": "deer-flow.stack-tuner.cycle-completeness",
        "cycle_start_count": len(start_ids),
        "cycle_done_count": len(done_ids),
        "dangling_starts": len(dangling_starts),
        "orphan_dones": len(orphan_dones),
        "avg_cycle_duration_s": round(avg_dur, 1),
        "confidence": "HIGH" if len(dangling_starts) == 0 and len(orphan_dones) == 0 else
                      "MEDIUM" if len(orphan_dones) == 0 else "LOW",
        "rationale": f"{len(dangling_starts)} dangling starts, {len(orphan_dones)} orphan dones, "
                     f"avg {avg_dur:.0f}s/cycle",
    }

    # ── Relationship: improver prediction confidence ──────────────────────
    # refuted_live and verified are NOT mutually exclusive — refuted_live fires
    # mid-iteration as a mathematical alarm; verified fires at end-of-iteration
    # as a final verdict. They measure at different timepoints.
    # refuted_live: "math just became unachievable"
    # verified:     "iteration closed, here's the final score vs prediction"
    # A (session_id, iteration) key can appear in BOTH if refuted_live fired
    # mid-flight and the iteration recovered (confirmed after alarm).
    #
    # Legitimate "contradiction" (bug): verified.outcome_label == "refuted" AND
    # refuted_live fired for same key — the alarm was correct AND confirmed.
    predictions = by_channel.get("autobench.improver.prediction.v1", [])
    verifieds = by_channel.get("autobench.improver.prediction.verified.v1", [])
    refuted_lives = by_channel.get("autobench.improver.prediction.refuted_live.v1", [])
    pred_keys = {(e.get("data", {}).get("session_id", ""),
                  e.get("data", {}).get("iteration", 0)) for e in predictions}
    verified_keys = {(
        e.get("data", {}).get("session_id", ""),
        e.get("data", {}).get("iteration", 0)
    ) for e in verifieds}
    verified_by_key: dict[tuple, dict] = {
        (e.get("data", {}).get("session_id", ""),
         e.get("data", {}).get("iteration", 0)): e.get("data", {})
        for e in verifieds
    }
    refuted_keys = {(
        e.get("data", {}).get("session_id", ""),
        e.get("data", {}).get("iteration", 0)
    ) for e in refuted_lives}

    stale = [k for k in pred_keys if k not in verified_keys and k not in refuted_keys]

    # True contradiction: both refuted_live fired AND verified closed as "refuted"
    true_contradictions = [
        k for k in refuted_keys
        if k in verified_by_key and verified_by_key[k].get("outcome_label") == "refuted"
    ]

    # Mid-flight alarm then confirmed at end (false alarm that resolved)
    refuted_then_confirmed = [
        k for k in refuted_keys
        if k in verified_by_key and verified_by_key[k].get("outcome_label") == "confirmed"
    ]

    # Alarm stood: refuted_live fired but no verified event followed (alarm held)
    refuted_no_resolution = [k for k in refuted_keys if k not in verified_by_key]

    # Clean confirmations (no prior refutation)
    clean_confirmations = [
        k for k in verified_keys
        if k not in refuted_keys
    ]

    errors = [abs(e.get("data", {}).get("score_delta_error", 0)) for e in verifieds]
    median_error = sorted(errors)[len(errors)//2] if errors else 0

    stats["relationships"]["improver_prediction_confidence"] = {
        "claim": "autobench.improver.prediction-confidence",
        "prediction_count": len(predictions),
        "verified_count": len(verifieds),
        "refuted_live_count": len(refuted_lives),
        "stale_predictions": len(stale),
        "true_contradictions": len(true_contradictions),
        "refuted_then_confirmed": len(refuted_then_confirmed),
        "refuted_no_resolution": len(refuted_no_resolution),
        "clean_confirmations": len(clean_confirmations),
        "median_abs_error": round(median_error, 4),
        "confidence": "HIGH" if len(true_contradictions) == 0 and median_error < 0.05 and len(verifieds) > 10 else
                      "MEDIUM" if len(true_contradictions) == 0 and len(verifieds) > 0 else
                      "NONE" if len(predictions) == 0 else "LOW",
        "rationale": (f"{len(stale)} stale, {len(true_contradictions)} true contradictions, "
                      f"{len(refuted_then_confirmed)} mid-flight alarms resolved, "
                      f"{len(refuted_no_resolution)} alarms stood, "
                      f"median error {median_error:.4f}"),
    }

    # ── Schema governance behavioral ──────────────────────────────────────
    event_channels = set(by_channel.keys())
    schema_dir = Path("schemas")
    schema_channels = {p.stem for p in schema_dir.glob("*.json") if p.stem not in (
        "_README", "_deprecated_schemas", "_per-project.capabilities.advertised",
        "_per-project-channels.README",
    )}
    # Also strip the .v1 from versioned names for comparison
    schema_unversioned = {sc.replace(".v1", "") if sc.endswith(".v1") else sc
                           for sc in schema_channels}
    # Build full set of known channels (versioned + unversioned aliases)
    known_channels = schema_channels | set(VERSIONED_ALIASES.values()) | set(VERSIONED_ALIASES)
    unknown_channels = sorted(event_channels - known_channels)

    coverage_stats = {
        "channels_in_events": len(event_channels),
        "channels_in_schemas": len(schema_channels),
        "coverage_ratio": round(len(event_channels - set(unknown_channels)) / len(event_channels), 3)
                          if event_channels else 1.0,
        "unknown_channels": unknown_channels,
        "schema_coverage_complete": len(unknown_channels) == 0,
    }

    stats["relationships"]["schema_governance_behavioral"] = {
        "claim": "nervous-bus.schema-governance.behavioral",
        **coverage_stats,
        "confidence": "HIGH" if len(unknown_channels) == 0 else
                      "MEDIUM" if len(unknown_channels) < 5 else "LOW",
        "rationale": f"{len(unknown_channels)} channels in events without schemas",
    }

    # ── Orchestrator efficiency ─────────────────────────────────────────
    thread_stats = []
    for e in by_channel.get("deer-flow.agent.thread.v1", []):
        data = e.get("data", {})
        if data.get("status") == "completed":
            thread_stats.append({
                "thread_id": data.get("thread_id", ""),
                "subagent_count": data.get("subagent_count", 0),
                "total_duration_s": data.get("total_duration_s", 0),
            })
    waterfall = [t for t in thread_stats if t["subagent_count"] > 10 and t["total_duration_s"] < 30]
    fine_grain = [t for t in thread_stats if t["subagent_count"] == 1 and t["total_duration_s"] < 5]

    stats["relationships"]["orchestrator_efficiency"] = {
        "claim": "deer-flow.orchestrator.efficiency",
        "thread_count": len(thread_stats),
        "waterfall_threads": len(waterfall),
        "fine_grain_threads": len(fine_grain),
        "avg_subagent_per_thread": round(
            sum(t["subagent_count"] for t in thread_stats) / len(thread_stats), 2
        ) if thread_stats else 0,
        "confidence": "HIGH" if len(waterfall) == 0 and len(fine_grain) == 0 else
                      "MEDIUM" if len(waterfall) == 0 else "LOW",
        "rationale": f"{len(waterfall)} waterfall patterns, {len(fine_grain)} fine-grain dispatches",
    }

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect bus event telemetry for claims assessment")
    parser.add_argument("--debug-log", default=f"{Path.home()}/.cache/nervous-bus/debug.jsonl",
                        help="Path to nervous-bus debug log")
    parser.add_argument("--since", default=None, help="ISO datetime lower bound")
    parser.add_argument("--hours", type=float, default=None, help="Hours to look back")
    parser.add_argument("--output", default=None, help="Output JSON file (default: stdout)")
    parser.add_argument("--claim", default=None, help="Focus on one claim ID")
    args = parser.parse_args()

    debug_path = Path(args.debug_log)
    if not debug_path.exists():
        print(f"Debug log not found: {debug_path}", file=sys.stderr)
        return 2

    since = None
    if args.since:
        since = parse_time(args.since)
    elif args.hours:
        since = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    print(f"Loading events from {debug_path}…", file=sys.stderr)
    events = load_events(debug_path, since=since)
    print(f"  → {len(events)} events loaded", file=sys.stderr)

    print("Aggregating…", file=sys.stderr)
    telemetry = aggregate(events)
    telemetry["generated_at"] = datetime.now(timezone.utc).isoformat()
    telemetry["source"] = str(debug_path)
    telemetry["since"] = since.isoformat() if since else None

    output = json.dumps(telemetry, indent=2)
    if args.output:
        Path(args.output).write_text(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)

    if args.claim:
        rel = telemetry.get("relationships", {}).get(args.claim)
        if rel:
            print(json.dumps(rel, indent=2))
        else:
            print(f"Claim {args.claim} not found in telemetry", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())