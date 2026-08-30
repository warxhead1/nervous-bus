#!/usr/bin/env python3
"""staleness — watch the watchers: detect nervous-bus channels that have gone quiet.

Motivating incidents (measured, not hypothetical): deer-flow bead-enrichment
publishing dead since 2026-05-14 and tengine CI emitters with no systemd unit
(may be dead) both went unnoticed for weeks/months because nothing alerts when
a channel that used to publish regularly simply stops. This adapter closes that
gap.

Core loop, once per run:
  1. Enumerate live `nbus:*` streams in Redis (SCAN, excluding nbus:all and
     nbus:dedup:*  — those are fan-in/internal, not per-channel).
  2. For each stream, sample its last N entries (XREVRANGE ... COUNT N). Redis
     Stream IDs encode a millisecond timestamp (`<ms>-<seq>`), so no extra
     timestamp field is needed to reconstruct history.
  3. Channels with fewer than SPARSE_MIN_EVENTS lifetime entries are "sparse":
     reported separately, never alerted on (not enough data to have a cadence).
  4. For the rest, compute the historical inter-event gap distribution from the
     sample and take its P95. The staleness threshold for THIS channel is
     max(p95_gap * multiplier, floor_seconds) — never a single hardcoded
     number for every channel, since a channel that fires every few seconds
     and one that fires twice a day have wildly different "quiet" thresholds.
  5. Compare against observed silence (now - last_event_time). If it exceeds
     the threshold, publish bus.channel.stale.v1 (via `nervous publish`, rate
     limited by an alert cooldown persisted in the baseline file) and mark the
     channel stale in the human-readable report.
  6. Persist each channel's computed baseline (p95, sample size, last alert
     time) to ~/.cache/nervous-bus/staleness/baselines.json so subsequent runs
     don't need to re-derive everything from scratch and so the alert cooldown
     is honoured across runs.

Usage:
    python3 monitor.py                  # one run against real Redis, writes report + baselines
    python3 monitor.py --dry-run        # compute + report, never publish
    python3 monitor.py --pattern 'nbus:test.*'   # scope to a stream subset (tests)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import redis

DEFAULT_REDIS_URL = os.environ.get("NERVOUS_REDIS_URL", "redis://localhost:6379")
CACHE_DIR = Path(os.environ.get("NERVOUS_STALENESS_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "staleness")))
BASELINE_FILE = CACHE_DIR / "baselines.json"
REPORT_FILE = CACHE_DIR / "report.md"

# Streams that are fan-in / internal bookkeeping, never per-channel cadence.
EXCLUDED_STREAM_SUFFIXES = ("nbus:all",)
EXCLUDED_STREAM_PREFIXES = ("nbus:dedup:",)

# A channel needs at least this many lifetime events before it has a
# meaningful cadence at all. Below this, "no schedule" and "dead" are
# indistinguishable from the data alone — report as sparse, never alert.
SPARSE_MIN_EVENTS = 5

# How many of the most recent entries to sample for the gap distribution.
# 50 is enough to get a stable P95 (5th-worst-of-49 gaps) without an
# expensive full-stream scan on high-volume channels; XREVRANGE COUNT is O(N)
# in the sample size, not the stream size.
DEFAULT_SAMPLE_SIZE = 50

# Multiplier applied to the historical P95 gap to get the alert threshold.
# 3x is deliberately generous: P95 already means 1 in 20 historical gaps was
# *larger* than this baseline, so alerting the instant we cross 1x P95 would
# false-positive on close to 5% of otherwise-healthy runs by definition.
# 3x P95 pushes the false-positive tail out past ~99.7% under a roughly
# geometric/exponential inter-arrival model, which is the shape observed for
# bus channels (bursty producers, not fixed-period cron).
DEFAULT_MULTIPLIER = 3.0

# Absolute floor under the multiplier term, so a channel with an anomalously
# tight historical P95 (e.g. a burst of heartbeats seconds apart, then normal
# operation) doesn't get an unreasonably twitchy threshold. 30 minutes is
# conservative relative to the systemd timer's own 15-minute cadence: it
# guarantees at least two monitor runs must observe continued silence-beyond-
# threshold territory before the *shortest* possible threshold could ever
# fire on a channel whose true cadence is sub-minute.
DEFAULT_FLOOR_SECONDS = 1800.0

# Once a channel has been alerted stale, don't re-alert on every 15-minute
# timer tick while it stays stale — that would spam bus.channel.stale.v1
# indefinitely for a channel that is (correctly) still dead. Re-alert at most
# once per cooldown window; the report.md always shows current state
# regardless of cooldown.
DEFAULT_ALERT_COOLDOWN_S = 6 * 3600.0

NERVOUS_BIN = os.environ.get(
    "NERVOUS_BIN",
    str(Path(__file__).resolve().parent.parent.parent / "sdk" / "shell" / "nervous"),
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_id_to_epoch_s(entry_id: str) -> float:
    """Redis Stream IDs are '<ms>-<seq>'; return the ms component as epoch seconds."""
    ms_part = entry_id.split("-", 1)[0]
    return int(ms_part) / 1000.0


def percentile(values: List[float], pct: float) -> float:
    """Nearest-rank percentile, no external deps (numpy not assumed present)."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return s[int(k)]
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def list_channel_streams(client: redis.Redis, pattern: str = "nbus:*") -> List[str]:
    """Enumerate candidate per-channel streams, excluding fan-in/internal keys."""
    out: List[str] = []
    for key in client.scan_iter(match=pattern, count=500):
        if key in EXCLUDED_STREAM_SUFFIXES:
            continue
        if any(key.startswith(p) for p in EXCLUDED_STREAM_PREFIXES):
            continue
        try:
            if client.type(key) != "stream":
                continue
        except redis.RedisError:
            continue
        out.append(key)
    return sorted(out)


def load_baselines(path: Path = BASELINE_FILE) -> Dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


def save_baselines(data: Dict[str, dict], path: Path = BASELINE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.rename(path)


def evaluate_stream(
    client: redis.Redis,
    stream: str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    multiplier: float = DEFAULT_MULTIPLIER,
    floor_seconds: float = DEFAULT_FLOOR_SECONDS,
    sparse_min_events: int = SPARSE_MIN_EVENTS,
    now: Optional[float] = None,
) -> dict:
    """Compute the cadence verdict for one stream.

    Returns a dict with keys: channel, verdict ('sparse'|'ok'|'stale'),
    lifetime_count, last_event_at (epoch s or None), p95_gap_seconds,
    expected_gap_seconds, observed_gap_seconds, baseline_sample_size.
    """
    now = now if now is not None else time.time()
    channel = stream[len("nbus:"):] if stream.startswith("nbus:") else stream

    lifetime_count = client.xlen(stream)
    if lifetime_count == 0:
        return {
            "channel": channel,
            "stream": stream,
            "verdict": "empty",
            "lifetime_count": 0,
            "last_event_at": None,
        }

    entries: List[Tuple[str, dict]] = client.xrevrange(stream, count=sample_size)
    if not entries:
        return {
            "channel": channel,
            "stream": stream,
            "verdict": "empty",
            "lifetime_count": lifetime_count,
            "last_event_at": None,
        }

    last_event_at = entry_id_to_epoch_s(entries[0][0])
    observed_gap = max(0.0, now - last_event_at)

    if lifetime_count < sparse_min_events:
        return {
            "channel": channel,
            "stream": stream,
            "verdict": "sparse",
            "lifetime_count": lifetime_count,
            "last_event_at": last_event_at,
            "observed_gap_seconds": observed_gap,
        }

    # entries is newest-first; reverse to ascending for consecutive-gap calc.
    timestamps = sorted(entry_id_to_epoch_s(eid) for eid, _fields in entries)
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    baseline_sample_size = len(gaps)

    if baseline_sample_size < sparse_min_events - 1:
        # Enough lifetime events per XLEN, but the sampled window (e.g. after
        # a MAXLEN trim) didn't yield enough distinct gaps to trust a P95.
        return {
            "channel": channel,
            "stream": stream,
            "verdict": "sparse",
            "lifetime_count": lifetime_count,
            "last_event_at": last_event_at,
            "observed_gap_seconds": observed_gap,
            "baseline_sample_size": baseline_sample_size,
        }

    p95_gap = percentile(gaps, 95)
    expected_gap = max(p95_gap * multiplier, floor_seconds)
    verdict = "stale" if observed_gap > expected_gap else "ok"

    return {
        "channel": channel,
        "stream": stream,
        "verdict": verdict,
        "lifetime_count": lifetime_count,
        "last_event_at": last_event_at,
        "observed_gap_seconds": observed_gap,
        "p95_gap_seconds": p95_gap,
        "expected_gap_seconds": expected_gap,
        "baseline_sample_size": baseline_sample_size,
        "multiplier": multiplier,
        "floor_seconds": floor_seconds,
    }


def publish_stale_event(result: dict, *, dry_run: bool = False) -> bool:
    """Publish bus.channel.stale.v1 via the shell SDK. Returns True on attempt."""
    payload = {
        "channel": result["channel"],
        "last_event_at": iso(result["last_event_at"]),
        "expected_gap_seconds": round(result["expected_gap_seconds"], 3),
        "observed_gap_seconds": round(result["observed_gap_seconds"], 3),
        "baseline_sample_size": result["baseline_sample_size"],
        "p95_gap_seconds": round(result["p95_gap_seconds"], 3),
        "multiplier": result["multiplier"],
        "floor_seconds": result["floor_seconds"],
    }
    if dry_run:
        sys.stderr.write(f"[staleness] (dry-run) would publish bus.channel.stale.v1: {payload}\n")
        return True
    try:
        subprocess.run(
            [NERVOUS_BIN, "publish", "bus.channel.stale.v1", json.dumps(payload)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except Exception as e:
        sys.stderr.write(f"[staleness] publish failed for {result['channel']}: {e}\n")
        return False


def render_report(results: List[dict], *, generated_at: Optional[float] = None) -> str:
    generated_at = generated_at if generated_at is not None else time.time()
    stale = [r for r in results if r["verdict"] == "stale"]
    ok = [r for r in results if r["verdict"] == "ok"]
    sparse = [r for r in results if r["verdict"] in ("sparse", "empty")]

    lines = [
        "# nervous-bus channel staleness report",
        "",
        f"Generated: {iso(generated_at)}",
        f"Channels observed: {len(results)}  (stale={len(stale)} ok={len(ok)} sparse/empty={len(sparse)})",
        "",
    ]

    def fmt_age(epoch: Optional[float]) -> str:
        if epoch is None:
            return "never"
        age_s = generated_at - epoch
        if age_s < 3600:
            return f"{age_s/60:.0f}m ago"
        if age_s < 86400:
            return f"{age_s/3600:.1f}h ago"
        return f"{age_s/86400:.1f}d ago"

    if stale:
        lines.append("## STALE — investigate")
        lines.append("")
        lines.append("| channel | last event | observed gap | expected gap | baseline n |")
        lines.append("|---|---|---|---|---|")
        for r in sorted(stale, key=lambda r: -r["observed_gap_seconds"]):
            lines.append(
                f"| {r['channel']} | {fmt_age(r['last_event_at'])} | "
                f"{r['observed_gap_seconds']/3600:.1f}h | {r['expected_gap_seconds']/3600:.1f}h | "
                f"{r['baseline_sample_size']} |"
            )
        lines.append("")

    lines.append("## OK")
    lines.append("")
    lines.append("| channel | last event | p95 historical gap | threshold |")
    lines.append("|---|---|---|---|")
    for r in sorted(ok, key=lambda r: r["channel"]):
        lines.append(
            f"| {r['channel']} | {fmt_age(r['last_event_at'])} | "
            f"{r['p95_gap_seconds']:.0f}s | {r['expected_gap_seconds']/3600:.1f}h |"
        )
    lines.append("")

    lines.append("## Sparse / empty (never alerted — insufficient history)")
    lines.append("")
    lines.append("| channel | lifetime events | last event |")
    lines.append("|---|---|---|")
    for r in sorted(sparse, key=lambda r: r["channel"]):
        lines.append(f"| {r['channel']} | {r.get('lifetime_count', 0)} | {fmt_age(r.get('last_event_at'))} |")
    lines.append("")

    return "\n".join(lines)


def run(
    *,
    redis_url: str = DEFAULT_REDIS_URL,
    pattern: str = "nbus:*",
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    multiplier: float = DEFAULT_MULTIPLIER,
    floor_seconds: float = DEFAULT_FLOOR_SECONDS,
    alert_cooldown_s: float = DEFAULT_ALERT_COOLDOWN_S,
    dry_run: bool = False,
    baseline_path: Path = BASELINE_FILE,
    report_path: Path = REPORT_FILE,
) -> List[dict]:
    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    streams = list_channel_streams(client, pattern=pattern)
    baselines = load_baselines(baseline_path)
    now = time.time()

    results = []
    for stream in streams:
        r = evaluate_stream(
            client,
            stream,
            sample_size=sample_size,
            multiplier=multiplier,
            floor_seconds=floor_seconds,
            now=now,
        )
        results.append(r)

        if r["verdict"] != "stale":
            continue

        prior = baselines.get(r["channel"], {})
        last_alert = prior.get("last_alert_at", 0.0)
        if now - last_alert < alert_cooldown_s:
            continue  # cooldown active, skip re-publish but keep in report

        published = publish_stale_event(r, dry_run=dry_run)
        # Never persist cooldown state for a dry-run — nothing was actually
        # published, so a later real run must still be free to alert.
        if published and not dry_run:
            baselines.setdefault(r["channel"], {})["last_alert_at"] = now

    # Persist the freshest baseline snapshot for every evaluated channel
    # (not just stale ones) so future runs can trend p95 drift over time.
    for r in results:
        entry = baselines.setdefault(r["channel"], {})
        entry["verdict"] = r["verdict"]
        entry["lifetime_count"] = r.get("lifetime_count")
        entry["last_event_at"] = r.get("last_event_at")
        entry["p95_gap_seconds"] = r.get("p95_gap_seconds")
        entry["expected_gap_seconds"] = r.get("expected_gap_seconds")
        entry["baseline_sample_size"] = r.get("baseline_sample_size")
        entry["updated_at"] = now

    save_baselines(baselines, baseline_path)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(results, generated_at=now))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="staleness — detect silent nervous-bus channel death")
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    parser.add_argument("--pattern", default="nbus:*", help="stream key glob (default: nbus:*)")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--multiplier", type=float, default=DEFAULT_MULTIPLIER)
    parser.add_argument("--floor-seconds", type=float, default=DEFAULT_FLOOR_SECONDS)
    parser.add_argument("--alert-cooldown-s", type=float, default=DEFAULT_ALERT_COOLDOWN_S)
    parser.add_argument("--dry-run", action="store_true", help="compute + report, never publish")
    parser.add_argument("--baseline-file", type=Path, default=BASELINE_FILE)
    parser.add_argument("--report-file", type=Path, default=REPORT_FILE)
    args = parser.parse_args()

    results = run(
        redis_url=args.redis_url,
        pattern=args.pattern,
        sample_size=args.sample_size,
        multiplier=args.multiplier,
        floor_seconds=args.floor_seconds,
        alert_cooldown_s=args.alert_cooldown_s,
        dry_run=args.dry_run,
        baseline_path=args.baseline_file,
        report_path=args.report_file,
    )

    stale = [r for r in results if r["verdict"] == "stale"]
    sys.stderr.write(
        f"[staleness] evaluated {len(results)} channels: "
        f"stale={len(stale)} report={args.report_file}\n"
    )
    for r in stale:
        sys.stderr.write(
            f"[staleness] STALE {r['channel']}: observed={r['observed_gap_seconds']/3600:.1f}h "
            f"expected={r['expected_gap_seconds']/3600:.1f}h (n={r['baseline_sample_size']})\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
