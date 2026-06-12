#!/usr/bin/env python3
"""silo-watcher — fs-tail tengine session dirs, emit silo lifecycle to nervous-bus.

tengine writes a per-run dir under ~/.tengine/sessions/silo_<NAME>_<DATE>_<TIME>[_<HEX>]/.
The dir appears at silo start; verification_report.json appears only on clean
completion. Crashed runs leave the dir without a report.

Channels emitted:
    tengine.silo.started.v1   on   new silo_*/ dir creation
    tengine.silo.verify.v1    on   verification_report.json appearing in a known dir

Behavior:
- Polling-based fs scan (no extra deps; 2s detection lag is acceptable for
  silo timescales which are minutes per run).
- Persists seen-dirs + verified-dirs across restarts via offset file so we
  don't replay history on restart.
- On startup, marks all currently existing dirs as 'already seen' (no
  retroactive started events). Verify events for newly appearing reports
  in already-seen dirs DO fire on first run (one-shot backfill).
- Fail-soft: any single publish failure is logged + continues. Missing
  ~/.tengine/sessions/ dir is not an error — we just keep waiting for it.

Usage:
    python watcher.py                          # uses config.toml in same dir
    python watcher.py --config /path/to.toml   # explicit config
    python watcher.py --once                   # scan once, emit pending, exit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

DEFAULT_CONFIG = Path(__file__).parent / "config.toml"
SILO_DIRNAME_RE = re.compile(
    r"^silo_(?P<name>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_[0-9a-f]+)?$"
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] silo-watcher: {msg}", file=sys.stderr, flush=True)


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def parse_dirname(name: str) -> tuple[str, str] | None:
    """Return (silo_name, started_at_rfc3339_utc) or None if dirname doesn't match."""
    m = SILO_DIRNAME_RE.match(name)
    if not m:
        return None
    silo = m.group("name")
    date = m.group("date")
    time_s = m.group("time")
    try:
        # tengine writes local time in dirnames. Best we can do without a tz hint
        # is parse as naive then mark as UTC. Drift up to ~hours possible if user
        # is not on UTC; consumers should treat started_at as approximate.
        dt = datetime.strptime(f"{date}{time_s}", "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return silo, dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def publish(channel: str, data: dict, source: str) -> bool:
    """Shell out to nervous publish. Returns True on success."""
    payload = json.dumps(data, separators=(",", ":"))
    env = os.environ.copy()
    env["NERVOUS_SOURCE"] = source
    try:
        result = subprocess.run(
            ["nervous", "publish", channel, payload],
            env=env,
            timeout=2.0,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log(f"nervous publish failed (rc={result.returncode}): {result.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        log("nervous CLI not on PATH — skipping publish")
        return False
    except subprocess.TimeoutExpired:
        log(f"nervous publish timeout on channel={channel}")
        return False


def load_offset(offset_path: Path) -> dict:
    if not offset_path.exists():
        return {"seen_dirs": [], "verified_dirs": []}
    try:
        with open(offset_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        log(f"offset file unreadable, starting fresh: {offset_path}")
        return {"seen_dirs": [], "verified_dirs": []}


def save_offset(offset_path: Path, state: dict) -> None:
    offset_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = offset_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, offset_path)


def build_verify_event(silo: str, session_id: str, session_dir: Path) -> dict | None:
    """Parse verification_report.json into the v1 event shape. Returns None on parse failure."""
    report_path = session_dir / "verification_report.json"
    try:
        with open(report_path) as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"failed to read {report_path}: {e}")
        return None

    verification = report.get("verification") or {}
    analysis = report.get("analysis") or {}
    fps = report.get("fps") or {}
    anomalies = analysis.get("anomalies") or []

    event = {
        "silo": report.get("silo") or silo,
        "session_id": session_id,
        "session_dir": str(session_dir),
        "success": bool(verification.get("success", False)),
        "message": str(verification.get("message", "")),
    }
    for k_event, k_report in [
        ("frames_rendered", "frames_rendered"),
        ("frames_requested", "frames_requested"),
    ]:
        v = report.get(k_report)
        if isinstance(v, int):
            event[k_event] = v
    for k_event, k_fps in [
        ("avg_fps", "average_fps"),
        ("instant_fps", "instant_fps"),
        ("min_fps", "min_fps"),
        ("max_fps", "max_fps"),
    ]:
        v = fps.get(k_fps)
        if isinstance(v, (int, float)):
            event[k_event] = round(float(v), 3)
    if "is_critical" in fps:
        event["fps_critical"] = bool(fps["is_critical"])
    if "is_warning" in fps:
        event["fps_warning"] = bool(fps["is_warning"])

    event["anomaly_count"] = len(anomalies)
    if anomalies:
        codes = [a.get("code", "?") for a in anomalies[:5] if isinstance(a, dict)]
        event["top_anomaly_codes"] = codes
    if analysis.get("status"):
        event["analysis_status"] = str(analysis["status"])
    return event


def build_frame_metrics_event(silo: str, session_id: str, session_dir: Path) -> list[dict] | None:
    """Parse per-frame batch metrics from verification_report.json.

    Returns a list of frame-batch event dicts (one per batch), or None on parse
    failure. Each event contains frame_index, frame_time_ms, gpu_utilization_pct,
    memory_bandwidth_gbps, top_shader, and anomaly_codes.
    """
    report_path = session_dir / "verification_report.json"
    try:
        with open(report_path) as f:
            report = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"failed to read {report_path}: {e}")
        return None

    frames = report.get("frames") or []
    if not frames:
        return None

    events = []
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        event = {
            "silo": report.get("silo") or silo,
            "session_id": session_id,
            "frame_index": i,
            "frame_time_ms": round(float(frame.get("time_ms", 0)), 3),
            "gpu_utilization_pct": round(float(frame.get("gpu_util_pct", 0)), 2),
            "memory_bandwidth_gbps": round(float(frame.get("mem_bandwidth_gbps", 0)), 3),
            "top_shader": str(frame.get("top_shader", "")),
            "anomaly_codes": frame.get("anomaly_codes", []) if isinstance(frame.get("anomaly_codes"), list) else [],
        }
        events.append(event)
    return events if events else None


def scan(sessions_root: Path, state: dict, source: str, emit_started_for_existing: bool) -> int:
    """One pass over sessions_root. Returns count of events emitted.

    Uses a horizon-mtime strategy instead of an ever-growing seen_dirs list.
    state["seen_horizon"] = mtime of the newest session dir we've fully processed.
    We only stat-scan dirs with mtime > (seen_horizon - epsilon) so this stays
    O(recently_added) rather than O(all_1800_sessions) on every 2-second poll.
    verified_dirs is kept as a set for sessions started but not yet verified.
    """
    if not sessions_root.exists():
        return 0

    seen_horizon: float = state.get("seen_horizon", 0.0)
    verified = set(state.get("verified_dirs", []))
    # Legacy migration: if old format has seen_dirs but no seen_horizon, compute
    # horizon from the list so we don't re-emit started events on first upgrade.
    if "seen_dirs" in state and not state.get("_horizon_migrated"):
        legacy_seen = set(state.pop("seen_dirs", []))
        if legacy_seen and sessions_root.exists():
            max_mtime = 0.0
            try:
                with os.scandir(sessions_root) as it:
                    for e in it:
                        if e.name in legacy_seen:
                            try:
                                max_mtime = max(max_mtime, e.stat().st_mtime)
                            except OSError:
                                pass
            except OSError:
                pass
            if max_mtime:
                seen_horizon = max_mtime
        state["_horizon_migrated"] = True
        log(f"migrated seen_dirs → seen_horizon={seen_horizon:.0f} ({len(legacy_seen)} entries)")

    emitted = 0
    new_horizon = seen_horizon

    # scandir with stat gives us mtime without a second syscall per entry.
    # We only need entries newer than seen_horizon (with a 5s slop for clock skew).
    horizon_cutoff = seen_horizon - 5.0
    try:
        with os.scandir(sessions_root) as it:
            entries = [
                (e.stat().st_mtime, e.name, Path(e.path))
                for e in it
                if e.is_dir(follow_symlinks=False) and e.name.startswith("silo_")
                and e.stat().st_mtime >= horizon_cutoff
            ]
    except OSError:
        return 0

    entries.sort()  # oldest-first so new_horizon advances monotonically

    for mtime, name, entry in entries:
        parsed = parse_dirname(name)
        if parsed is None:
            continue
        silo, started_at = parsed

        is_new = mtime > seen_horizon
        if is_new:
            if emit_started_for_existing:
                event = {
                    "silo": silo,
                    "session_id": name,
                    "started_at": started_at,
                    "session_dir": str(entry),
                }
                if publish("tengine.silo.started.v1", event, source):
                    emitted += 1
                    log(f"started: {name} (silo={silo})")
            new_horizon = max(new_horizon, mtime)

        if name not in verified and (entry / "verification_report.json").exists():
            event = build_verify_event(silo, name, entry)
            if event is not None:
                if publish("tengine.silo.verify.v1", event, source):
                    emitted += 1
                    sym = "✓" if event["success"] else "✗"
                    log(f"verify {sym}: {name} — {event['message']}")

            frame_events = build_frame_metrics_event(silo, name, entry)
            if frame_events is not None:
                for fevent in frame_events:
                    if publish("tengine.frame.metrics.v1", fevent, source):
                        emitted += 1
                        log(f"frame_metrics: {name}[{fevent['frame_index']}]")

            verified.add(name)

    state["seen_horizon"] = new_horizon
    # Keep verified_dirs pruned: once a session dir no longer exists it can't
    # gain a late verification_report.json, so drop it to bound list growth.
    if verified:
        try:
            existing = {e.name for e in os.scandir(sessions_root) if e.is_dir(follow_symlinks=False)}
        except OSError:
            existing = set()
        verified &= existing
    state["verified_dirs"] = sorted(verified)
    return emitted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true",
                    help="scan once, emit pending, exit (for testing/backfill)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    sessions_root = Path(
        os.path.expanduser(
            cfg.get("sessions", {}).get("root", "~/.tengine/sessions")
        )
    )
    poll_interval = float(cfg.get("sessions", {}).get("poll_interval_s", 2.0))
    offset_path = Path(
        os.path.expanduser(
            cfg.get("offset_file", {}).get(
                "path", "~/.cache/nervous-bus/silo-watcher-offset.json"
            )
        )
    )
    source = cfg.get("publish", {}).get("source", "/silo-watcher")

    state = load_offset(offset_path)
    bootstrap = not state.get("seen_dirs") and not state.get("seen_horizon")

    if bootstrap:
        # First run ever: set the horizon to the newest existing session mtime
        # WITHOUT emitting started events (no retroactive flood). Verify events
        # for already-completed runs DO still fire (one-shot backfill).
        log(f"bootstrap: snapshotting {sessions_root} without emitting started events")
        max_mtime = 0.0
        if sessions_root.exists():
            try:
                with os.scandir(sessions_root) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False) and e.name.startswith("silo_"):
                            try:
                                max_mtime = max(max_mtime, e.stat().st_mtime)
                            except OSError:
                                pass
            except OSError:
                pass
        state["seen_horizon"] = max_mtime
        state["verified_dirs"] = []
        save_offset(offset_path, state)

    log(f"watching {sessions_root} (poll={poll_interval}s, source={source})")
    log(f"offset file: {offset_path}")

    if args.once:
        n = scan(sessions_root, state, source, emit_started_for_existing=True)
        save_offset(offset_path, state)
        log(f"once-pass: emitted {n} events; exiting")
        return 0

    try:
        while True:
            n = scan(sessions_root, state, source, emit_started_for_existing=True)
            if n:
                save_offset(offset_path, state)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        save_offset(offset_path, state)
        log("interrupted; offset saved")
        return 0


if __name__ == "__main__":
    sys.exit(main())
