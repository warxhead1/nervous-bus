#!/usr/bin/env python3
"""pattern pipeline health check — runs every 5 minutes via systemd timer.

In addition to the original PEL/bundle checks this version:
  1. Checks nbus:all consumer-group lag for ALL registered groups, not just
     the pattern group.
  2. Emits ``bus.intrinsic.marker.v1`` events via ``nervous publish`` when:
       - lag exceeds LAG_ERROR_THRESHOLD (>1000 entries), OR
       - lag has not decreased in LAG_STALL_WINDOW_S (5 min) while non-zero
     Recovery (lag returns to 0) emits a second marker with outcome "accepted".
  3. Persists last-seen lag per group in Valkey so the stall detector survives
     across process restarts.
  4. Alerts on nbus:bus.dead_letter growth rate (>50 new entries in one 5-min
     check window) using the stream's entries-added counter — cheapest path
     to dead-letter alerting per the audit: piggyback this existing timer
     instead of standing up Prometheus alertmanager.
  5. Tracks the age of the newest REAL entry on nbus:bus.pattern.signal.v1 —
     the pipeline's OUTPUT. Every check above looks at plumbing (service up,
     group present, bundler writing); none notices a pipeline that is wired up
     and produces nothing. consumer_degraded entries are the consumer reporting
     itself broken and never count as output. Older than
     PATTERN_SIGNAL_MAX_AGE_S (or an empty / degraded-only stream) makes the
     run exit 1 with a "no bus.pattern.signal.v1 for ..." line on every run;
     the bus warn + marker publish once per outage.

Alert markers use:
  marker_type = "error.spike"   (problem)  / "quality.signal" (recovery)
  phase       = "recycle"       (observation phase — consumer is falling behind)
  outcome     = "failed"        (problem)  / "accepted" (recovery)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

# redis is imported in main(): the checks only call methods on the client they
# are handed, so the module stays importable (and testable) without the package.

STREAM = "nbus:bundles"
GROUP = "deer-flow-patterns"
SERVICE = "pattern-consumer"
MAX_PEL = 100           # unacked entries above this = consumer stuck
MAX_BUNDLE_AGE = 1800   # seconds — bundler should emit within 30 min

# nbus:all lag alerting thresholds
LAG_ERROR_THRESHOLD = 1000          # absolute lag above this = alert
LAG_STALL_WINDOW_S = 300            # 5 min with same non-zero lag = alert

# Valkey keys for stall detection state
HEALTH_KEY_PREFIX = "nbus:watchdog:lag:"  # e.g. nbus:watchdog:lag:hearth-consumer
HEALTH_KEY_TTL = 7200                     # 2 h

# nbus:bus.dead_letter growth-rate alerting (audit P0 #2 — cheapest path:
# piggyback on this existing 5-min timer instead of standing up Prometheus
# alertmanager). Measured baseline on 2026-07-10 was ~0.5 dead letters/min
# steady-state (~2-3 per 5-min window); real incidents burst dozens within
# seconds. 50 new dead letters in one 5-min window is well above baseline
# noise and catches bursts without false-positiving on the trickle.
DLQ_STREAM = "nbus:bus.dead_letter"
DLQ_GROWTH_THRESHOLD = 50           # new dead letters within one check window = alert
DLQ_HEALTH_KEY = "nbus:watchdog:dlq:growth"
DLQ_HEALTH_KEY_TTL = 7200           # 2 h

# Pipeline-output freshness: the newest REAL bus.pattern.signal.v1 entry (not
# consumer_degraded) must be younger than this. Override with
# PATTERN_SIGNAL_MAX_AGE_S (seconds, > 0).
#
# The 3-day default is a judgment bound, not a derived one. The live stream's
# real signals are a single 15 h burst (2026-05-10/11, 3,602 signals, longest
# gap 595 s), which says nothing about a steady cadence, and the consumer only
# calls the LLM for bundles over its deviation gate, so a quiet host can
# legitimately emit nothing for a while. 3 days rides out a quiet weekend.
SIGNAL_STREAM = "nbus:bus.pattern.signal.v1"
SIGNAL_MAX_AGE_ENV = "PATTERN_SIGNAL_MAX_AGE_S"
DEFAULT_SIGNAL_MAX_AGE_S = 3 * 86400
SIGNAL_DEGRADED_TYPE = "consumer_degraded"
SIGNAL_SCAN_FIRST_PAGE = 50     # a healthy stream ends the scan on the first entry
SIGNAL_SCAN_PAGE = 500
SIGNAL_SCAN_MAX = 20_000        # entries; bounds the cost of a long degraded run
SIGNAL_HEALTH_KEY = "nbus:watchdog:signal"
SIGNAL_HEALTH_KEY_TTL = 7200        # 2 h

NBUS_ROOT = os.environ.get("NBUS_ROOT", str(Path(__file__).resolve().parents[2]))


# ── bus.intrinsic.marker.v1 publisher ────────────────────────────────────────

def _publish_marker(
    linked_id: str,
    marker_type: str,
    outcome: str,
    quality_score: float,
    phase: str = "recycle",
) -> None:
    """Emit a bus.intrinsic.marker.v1 event via nervous publish."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = str(uuid.uuid4())
    envelope = {
        "specversion": "1.0",
        "id": event_id,
        "source": "/nervous-bus/watchdog",
        "type": "bus.intrinsic.marker.v1",
        "time": now,
        "datacontenttype": "application/json",
        "data": {
            "project": "nervous-bus",
            "marker_type": marker_type,
            "phase": phase,
            "linked_id": linked_id,
            "ts": now,
            "outcome": outcome,
            "quality_score": quality_score,
        },
    }
    try:
        subprocess.run(
            ["nervous", "publish", "bus.intrinsic.marker.v1", json.dumps(envelope["data"])],
            capture_output=True,
            timeout=5,
            cwd=NBUS_ROOT,
        )
    except Exception as e:
        sys.stderr.write(f"[pattern-watchdog] marker publish failed: {e}\n")


# ── sys.log.entry.v1 publisher (original) ────────────────────────────────────

def _publish_warn(issue: str) -> None:
    try:
        subprocess.run(
            [
                "nervous", "publish", "sys.log.entry.v1",
                json.dumps({
                    "log_source": "app",
                    "service": "pattern-watchdog",
                    "level": "warn",
                    "message": issue,
                    "raw": issue,
                    "parsed_fields": {"component": "pattern-pipeline"},
                }),
            ],
            capture_output=True, timeout=5,
            cwd=NBUS_ROOT,
        )
    except Exception:
        pass


# ── Consumer group lag alerting for nbus:all ──────────────────────────────────

def _check_nbus_all_lag(r: redis.Redis) -> list[str]:
    """Check all consumer groups on nbus:all for lag spikes and stalls.

    Uses Valkey hashes under HEALTH_KEY_PREFIX to track:
      - last_lag: lag value at last check
      - last_decrease_ts: epoch when lag last went down (or reached 0)
      - alerted: "1" if an alert was already fired (prevents storm)

    Returns list of issue strings (empty = OK).
    """
    issues: list[str] = []
    now = time.time()

    try:
        groups = r.xinfo_groups("nbus:all")
    except Exception as e:
        issues.append(f"xinfo_groups nbus:all failed: {e}")
        return issues

    for g in groups:
        group_name = g.get("name", "unknown")
        lag = int(g.get("lag", 0))
        state_key = f"{HEALTH_KEY_PREFIX}{group_name}"

        # Load persisted state
        try:
            state = r.hgetall(state_key)
        except Exception:
            state = {}

        last_lag = int(state.get("last_lag", 0))
        last_decrease_ts = float(state.get("last_decrease_ts", now))
        alerted = state.get("alerted", "0") == "1"

        # Determine if lag is decreasing
        lag_decreased = lag < last_lag
        lag_at_zero = lag == 0

        if lag_decreased or lag_at_zero:
            new_decrease_ts = now
        else:
            new_decrease_ts = last_decrease_ts

        stall_duration = now - new_decrease_ts
        is_stalled = (lag > 0) and (stall_duration >= LAG_STALL_WINDOW_S)
        is_spike = lag > LAG_ERROR_THRESHOLD

        # Alert conditions
        if (is_spike or is_stalled) and not alerted:
            reason = (
                f"lag spike ({lag} > {LAG_ERROR_THRESHOLD})"
                if is_spike
                else f"lag stalled at {lag} for {stall_duration / 60:.1f} min"
            )
            msg = f"nbus:all consumer group {group_name!r}: {reason}"
            issues.append(msg)
            _publish_marker(
                linked_id=group_name,
                marker_type="error.spike",
                outcome="failed",
                quality_score=0.0,
            )
            sys.stderr.write(f"[pattern-watchdog] ALERT: {msg}\n")
            new_alerted = "1"
        elif lag_at_zero and alerted:
            # Recovery
            sys.stderr.write(
                f"[pattern-watchdog] RECOVERY: nbus:all group {group_name!r} lag=0\n"
            )
            _publish_marker(
                linked_id=group_name,
                marker_type="quality.signal",
                outcome="accepted",
                quality_score=1.0,
            )
            new_alerted = "0"
        else:
            new_alerted = "1" if alerted else "0"

        # Persist updated state
        try:
            r.hset(state_key, mapping={
                "last_lag": str(lag),
                "last_decrease_ts": str(new_decrease_ts),
                "alerted": new_alerted,
            })
            r.expire(state_key, HEALTH_KEY_TTL)
        except Exception:
            pass

    return issues


# ── Dead-letter growth-rate alerting (audit P0 #2) ───────────────────────────

def _check_dead_letter_growth(r: redis.Redis) -> list[str]:
    """Alert when nbus:bus.dead_letter grows faster than baseline.

    Uses the stream's ``entries-added`` counter (monotonic, unaffected by
    the MAXLEN~10000 trim that redis-mirror applies to this stream) rather
    than XLEN, so a check window spanning a trim doesn't read as a drop.
    Same persisted-state/storm-prevention pattern as _check_nbus_all_lag.
    """
    issues: list[str] = []
    now = time.time()

    try:
        info = r.xinfo_stream(DLQ_STREAM)
        entries_added = int(info.get("entries-added", 0))
    except Exception as e:
        issues.append(f"xinfo_stream {DLQ_STREAM} failed: {e}")
        return issues

    try:
        state = r.hgetall(DLQ_HEALTH_KEY)
    except Exception:
        state = {}

    last_added = state.get("last_entries_added")
    alerted = state.get("alerted", "0") == "1"

    if last_added is not None:
        growth = entries_added - int(last_added)
        if growth > DLQ_GROWTH_THRESHOLD and not alerted:
            msg = (
                f"{DLQ_STREAM}: {growth} new dead letters since last check "
                f"(> {DLQ_GROWTH_THRESHOLD}/5min threshold)"
            )
            issues.append(msg)
            _publish_marker(
                linked_id=DLQ_STREAM,
                marker_type="error.spike",
                outcome="failed",
                quality_score=0.0,
            )
            sys.stderr.write(f"[pattern-watchdog] ALERT: {msg}\n")
            new_alerted = "1"
        elif growth <= 0 and alerted:
            sys.stderr.write(
                f"[pattern-watchdog] RECOVERY: {DLQ_STREAM} growth back to baseline\n"
            )
            _publish_marker(
                linked_id=DLQ_STREAM,
                marker_type="quality.signal",
                outcome="accepted",
                quality_score=1.0,
            )
            new_alerted = "0"
        else:
            new_alerted = "1" if alerted else "0"
    else:
        new_alerted = "0"  # first run — no baseline yet, just record it

    try:
        r.hset(DLQ_HEALTH_KEY, mapping={
            "last_entries_added": str(entries_added),
            "alerted": new_alerted,
            "last_check_ts": str(now),
        })
        r.expire(DLQ_HEALTH_KEY, DLQ_HEALTH_KEY_TTL)
    except Exception:
        pass

    return issues


# ── Pipeline output freshness ────────────────────────────────────────────────

def _signal_max_age_s() -> float:
    raw = os.environ.get(SIGNAL_MAX_AGE_ENV, "").strip()
    if not raw:
        return float(DEFAULT_SIGNAL_MAX_AGE_S)
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value > 0:
        return value
    sys.stderr.write(
        f"[pattern-watchdog] ignoring invalid {SIGNAL_MAX_AGE_ENV}={raw!r}; "
        f"using {DEFAULT_SIGNAL_MAX_AGE_S}s\n"
    )
    return float(DEFAULT_SIGNAL_MAX_AGE_S)


def _fmt_age(seconds: float) -> str:
    if seconds >= 2 * 86400:
        return f"{seconds / 86400:.1f} days"
    if seconds >= 2 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.0f} min"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _SignalScan(NamedTuple):
    last_real_ts: float | None        # newest entry that is real output
    newest_degraded_ts: float | None  # newest consumer_degraded NEWER than that
    scanned: int
    unclassified: int                 # entries whose signal_type was unreadable
    exhausted: bool                   # reached the oldest entry (vs the scan bound)


def _signal_type(fields: dict) -> str | None:
    """signal_type of one stream entry, or None when it cannot be read.

    redis-mirror writes two shapes: the flattened ``data.signal_type`` field,
    and ``_raw`` (the whole CloudEvent as JSON). Older entries carry both, with
    ``_raw`` cut at 2000 chars and no longer valid JSON; the newest
    consumer_degraded entries carry ``_raw`` alone.
    """
    flat = fields.get("data.signal_type")
    if isinstance(flat, str) and flat:
        return flat
    try:
        kind = json.loads(fields["_raw"])["data"]["signal_type"]
    except (KeyError, TypeError, ValueError):
        return None
    return kind if isinstance(kind, str) and kind else None


def _scan_signals(r: redis.Redis) -> _SignalScan:
    """Page backwards from the newest entry to the newest REAL signal.

    consumer_degraded is the pattern consumer reporting that it is broken
    (budget exhausted, rate limited, slow); it is not pipeline output, so it
    must not count as freshness. Measured on the live stream (5,444 entries):
    every one of the 3,602 real signals landed on 2026-05-10/11 and the 1,842
    entries after that are all consumer_degraded.
    """
    newest_degraded: float | None = None
    scanned = unclassified = 0
    exhausted = False
    upper = "+"
    want = SIGNAL_SCAN_FIRST_PAGE
    while scanned < SIGNAL_SCAN_MAX:
        want = min(want, SIGNAL_SCAN_MAX - scanned)
        page = r.xrevrange(SIGNAL_STREAM, max=upper, count=want)
        for eid, fields in page:
            try:
                ts = int(eid.split("-")[0]) / 1000
            except (ValueError, IndexError, AttributeError) as e:
                raise ValueError(f"bad entry id {eid!r}: {e}") from e
            scanned += 1
            kind = _signal_type(fields)
            if kind == SIGNAL_DEGRADED_TYPE:
                if newest_degraded is None:
                    newest_degraded = ts
            elif kind is None:
                unclassified += 1
            else:
                return _SignalScan(ts, newest_degraded, scanned, unclassified, False)
        if len(page) < want:
            exhausted = True
            break
        upper = "(" + page[-1][0]
        want = SIGNAL_SCAN_PAGE
    return _SignalScan(None, newest_degraded, scanned, unclassified, exhausted)


def _check_signal_age(r: redis.Redis, now: float | None = None) -> list[str]:
    """Fail when the pattern pipeline has stopped producing signals.

    Freshness is the age of the newest NON-consumer_degraded entry: degraded
    entries are the consumer reporting itself broken, so a stream that is
    recent but all degraded is the failure this check exists to catch.

    Returns an issue string on EVERY run while the stream is stale or empty, so
    the unit keeps exiting 1 and the journal keeps saying why. The bus warn and
    the error.spike marker are latched in Valkey and publish once per outage;
    a fresh signal publishes the recovery marker.
    """
    now = time.time() if now is None else now
    max_age = _signal_max_age_s()

    try:
        scan = _scan_signals(r)
    except Exception as e:
        return [f"signal freshness check failed: {e}"]

    last_ts = scan.last_real_ts
    notes = ""
    if scan.newest_degraded_ts is not None:
        notes += (
            f"; newest entry is consumer_degraded {_fmt_age(max(0.0, now - scan.newest_degraded_ts))} "
            f"ago ({_iso(scan.newest_degraded_ts)}), the consumer reporting itself broken"
        )
    if scan.unclassified:
        notes += f"; {scan.unclassified} entries had no readable signal_type and were not counted"

    if last_ts is None:
        age = None
        if scan.scanned == 0:
            issue = (
                f"no bus.pattern.signal.v1 ever: {SIGNAL_STREAM} is empty or missing "
                f"(pattern pipeline has produced nothing)"
            )
        else:
            scope = (
                f"all {scan.scanned} entries" if scan.exhausted
                else f"the newest {scan.scanned} entries (scan bound)"
            )
            issue = (
                f"no non-degraded bus.pattern.signal.v1 in {scope} of {SIGNAL_STREAM} "
                f"(pattern pipeline has produced no real output){notes}"
            )
    else:
        age = max(0.0, now - last_ts)
        issue = (
            f"no bus.pattern.signal.v1 for {_fmt_age(age)} "
            f"(bound {_fmt_age(max_age)}, set {SIGNAL_MAX_AGE_ENV} to change); "
            f"last signal {_iso(last_ts)}{notes}"
        )
    stale = age is None or age > max_age

    try:
        state = r.hgetall(SIGNAL_HEALTH_KEY)
    except Exception:
        state = {}
    alerted = state.get("alerted", "0") == "1"

    if stale and not alerted:
        sys.stderr.write(f"[pattern-watchdog] ALERT: {issue}\n")
        _publish_warn(issue)
        _publish_marker(
            linked_id=SIGNAL_STREAM,
            marker_type="error.spike",
            outcome="failed",
            quality_score=0.0,
        )
    elif not stale and alerted:
        sys.stderr.write(
            f"[pattern-watchdog] RECOVERY: {SIGNAL_STREAM} has a signal "
            f"{_fmt_age(age)} old\n"
        )
        _publish_marker(
            linked_id=SIGNAL_STREAM,
            marker_type="quality.signal",
            outcome="accepted",
            quality_score=1.0,
        )
    elif not stale:
        sys.stderr.write(
            f"[pattern-watchdog] last signal {_fmt_age(age)} ago "
            f"(bound {_fmt_age(max_age)})\n"
        )

    try:
        r.hset(SIGNAL_HEALTH_KEY, mapping={
            "last_signal_ts": "" if last_ts is None else str(last_ts),
            "newest_degraded_ts": (
                "" if scan.newest_degraded_ts is None else str(scan.newest_degraded_ts)
            ),
            "last_signal_age_s": "" if age is None else str(age),
            "alerted": "1" if stale else "0",
            "last_check_ts": str(now),
        })
        r.expire(SIGNAL_HEALTH_KEY, SIGNAL_HEALTH_KEY_TTL)
    except Exception:
        pass

    return [issue] if stale else []


# ── Pattern bundle / PEL checks (original) ───────────────────────────────────

def _check(r: redis.Redis) -> list[str]:
    issues: list[str] = []

    # 1. Service active?
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE],
        capture_output=True, text=True,
    )
    status = proc.stdout.strip()
    if status != "active":
        issues.append(f"{SERVICE} service is {status!r}")

    # 2. Consumer group health on nbus:bundles (pattern pipeline)
    try:
        groups = r.xinfo_groups(STREAM)
        group_found = False
        for g in groups:
            if g.get("name") == GROUP:
                group_found = True
                pel = int(g.get("pel-count", 0))
                lag = int(g.get("lag", 0))
                r.hset("pattern:health", mapping={
                    "ts": str(time.time()),
                    "pel": str(pel),
                    "lag": str(lag),
                    "service_status": status,
                })
                r.expire("pattern:health", 3600)
                if pel > MAX_PEL:
                    issues.append(
                        f"{GROUP} PEL backlog: {pel} unacked entries (consumer may be stuck)"
                    )
        if not group_found:
            issues.append(f"consumer group {GROUP!r} missing from {STREAM}")
    except Exception as e:
        issues.append(f"xinfo_groups check failed: {e}")

    # 3. Bundler liveness — age of last bundle
    try:
        entries = r.xrevrange(STREAM, count=1)
        if entries:
            eid, _ = entries[0]
            ts_ms = int(eid.split("-")[0])
            age_s = time.time() - ts_ms / 1000
            if age_s > MAX_BUNDLE_AGE:
                issues.append(
                    f"nbus:bundles stale: last entry {age_s / 60:.0f}min ago "
                    f"(pattern-bundler may be stuck)"
                )
        else:
            issues.append(f"{STREAM} is empty — pattern-bundler not producing")
    except Exception as e:
        issues.append(f"bundle liveness check failed: {e}")

    # 4. nbus:all consumer group lag checks (new)
    lag_issues = _check_nbus_all_lag(r)
    issues.extend(lag_issues)

    # 5. nbus:bus.dead_letter growth-rate check (audit P0 #2)
    dlq_issues = _check_dead_letter_growth(r)
    issues.extend(dlq_issues)

    return issues


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    import redis

    try:
        r = redis.Redis.from_url(
            "redis://localhost:6379", decode_responses=True,
            socket_timeout=3, socket_connect_timeout=3,
        )
        r.ping()
    except Exception as e:
        msg = f"pattern-watchdog: Redis unreachable — {e}"
        sys.stderr.write(msg + "\n")
        _publish_warn(msg)
        return 1

    issues = _check(r)
    # Output freshness publishes its own latched warn; it must not go through
    # the per-run _publish_warn loop below (a multi-week silence would emit a
    # warn every 5 minutes) and it never triggers the service restart.
    signal_issues = _check_signal_age(r)

    for issue in issues:
        sys.stderr.write(f"[pattern-watchdog] WARN: {issue}\n")
        _publish_warn(issue)
    for issue in signal_issues:
        sys.stderr.write(f"[pattern-watchdog] WARN: {issue}\n")

    if issues or signal_issues:
        # Auto-restart if service is down
        if any("service is" in i for i in issues):
            sys.stderr.write(f"[pattern-watchdog] restarting {SERVICE}...\n")
            subprocess.run(
                ["systemctl", "--user", "restart", SERVICE],
                capture_output=True,
            )
        return 1

    sys.stderr.write("[pattern-watchdog] pipeline ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
