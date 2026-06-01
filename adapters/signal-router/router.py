#!/usr/bin/env python3
"""signal-router — consume bus.pattern.signal.v1, route by confidence tier."""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
import redis as redis_lib

sys.path.insert(0, str(Path(__file__).parent))
from calibration import load_calibration, save_calibration, record_verdict, should_auto_file

# Per-signal-type confidence threshold for Tier 2 routing (draft/auto-bead)
_TIER2_CONFIDENCE: dict[str, float] = {
    "anomaly":     0.75,
    "pattern":     0.75,
    "correlation": 0.70,
    "recovery":    0.65,
    "silence":     1.1,   # effectively never — silence must always be human-reviewed
}

CONSUMER_GROUP  = "signal-router"
CONSUMER_NAME   = "router-1"
STREAM          = "nbus:bus.pattern.signal.v1"
DRAFT_STREAM    = "nbus:draft-beads"
ANNOTATIONS_KEY = "pattern:annotations"

def _ensure_group(r: redis_lib.Redis) -> None:
    try:
        r.xgroup_create(STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    except redis_lib.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

def _update_thresholds(r: redis_lib.Redis, signal: dict) -> None:
    confidence = signal.get("confidence", 0.0)
    for ch in signal.get("channels", []):
        key = f"pattern:thresholds:{ch}"
        dev = (signal.get("evidence") or {}).get("deviation_sigma")
        if dev is not None and confidence < 0.5:
            stype = signal.get("signal_type", "")
            if stype == "anomaly":
                raw = r.hgetall(key)
                cur = float(raw.get("alert_sigma", 3.0))
                new = round(max(1.5, cur * 0.95 + abs(dev) * 0.05), 2)
                r.hset(key, "alert_sigma", str(new))

def _write_annotation(r: redis_lib.Redis, signal: dict) -> None:
    key = "pattern:annotations"
    sid = signal.get("signal_id", "")
    tr = (signal.get("evidence") or {}).get("time_range", {})
    r.hset(key, sid, json.dumps({
        "signal_id":    sid,
        "signal_type":  signal.get("signal_type"),
        "severity":     signal.get("severity"),
        "confidence":   signal.get("confidence"),
        "channels":     signal.get("channels", []),
        "description":  (signal.get("description") or "")[:200],
        "ts":           time.time(),
        "window_start": tr.get("start", ""),
        "window_end":   tr.get("end", ""),
    }))
    all_entries = r.hgetall(key)
    if len(all_entries) > 50:
        sorted_keys = sorted(all_entries, key=lambda k: json.loads(all_entries[k]).get("ts", 0))
        for old_key in sorted_keys[:-50]:
            r.hdel(key, old_key)

def _create_draft(r: redis_lib.Redis, signal: dict) -> None:
    fields = {
        "signal_id":    signal.get("signal_id", ""),
        "signal_type":  signal.get("signal_type", ""),
        "confidence":   str(signal.get("confidence", 0.0)),
        "project_hint": signal.get("project_hint") or "",
        "description":  (signal.get("description") or "")[:500],
        "_raw":         json.dumps(signal)[:2000],
    }
    r.xadd(DRAFT_STREAM, fields, maxlen=500, approximate=True)

def _auto_create_bead(signal: dict) -> None:
    project = signal.get("project_hint") or "nervous-bus"
    title = f"[pattern] {signal.get('signal_type','anomaly')}: {signal.get('channels',['?'])[0]}"
    desc = (signal.get("description") or "")[:500]
    action = signal.get("recommended_action") or ""
    try:
        subprocess.run([
            "bd", "create",
            f"--title={title}",
            f"--description={desc}\n\nRecommended action: {action}",
            "--type=bug",
            "--priority=2",
        ], capture_output=True, timeout=10,
        cwd=os.path.expanduser(f"~/{os.environ.get('NERVOUS_PROJECTS_SUBDIR', 'projects')}/{project}"))
    except Exception as e:
        sys.stderr.write(f"signal-router bd create: {e}\n")

def _mark_dismissed_fps(r: redis_lib.Redis, channels: list, now: float | None = None) -> None:
    """Increment dismiss count on recently-seen fingerprints for these channels."""
    if now is None:
        now = time.time()
    for channel in channels:
        key = f"pattern:err-fp:{channel}"
        try:
            all_fps = r.hgetall(key)
            for fph, raw in all_fps.items():
                entry = json.loads(raw)
                # Only mark as dismissed if seen in the last 24 hours
                if now - entry.get("last_seen", 0) < 86400:
                    entry["dismissed"] = entry.get("dismissed", 0) + 1
                    r.hset(key, fph, json.dumps(entry))
        except Exception:
            pass

def _process_feedback(r: redis_lib.Redis, signal: dict) -> None:
    stype = signal.get("signal_type", "anomaly")
    verdict = signal.get("verdict")
    if not verdict:
        return
    # Load per-channel calibration if channels are available; fall back to global.
    signal_channels = []
    try:
        ann_raw = r.hget("pattern:annotations", signal.get("signal_id", ""))
        if ann_raw:
            ann = json.loads(ann_raw)
            signal_channels = ann.get("channels", [])
    except Exception:
        pass
    for ch in signal_channels:
        state = load_calibration(r, stype, ch)
        record_verdict(state, verdict)
        save_calibration(r, state)
    if verdict in ("reject", "modify"):
        _mark_dismissed_fps(r, signal_channels)

def route(r: redis_lib.Redis, signal: dict) -> None:
    confidence = float(signal.get("confidence", 0.0))
    stype = signal.get("signal_type", "anomaly")

    _update_thresholds(r, signal)

    if confidence >= 0.5 and stype != "silence":
        _write_annotation(r, signal)

    tier2_threshold = _TIER2_CONFIDENCE.get(stype, 0.80)
    if confidence >= tier2_threshold:
        # Use per-channel calibration if channels are present; fall back to global.
        ch = (signal.get("channels") or [None])[0] if signal.get("channels") else None
        state = load_calibration(r, stype, ch or "")
        if state.auto_enabled:
            _auto_create_bead(signal)
        else:
            _create_draft(r, signal)
        save_calibration(r, state)

    for ch in signal.get("channels", []):
        key = f"pattern:signals:recent:{ch}"
        r.lpush(key, json.dumps({
            "ts": time.time(),
            "signal_type": stype,
            "description": (signal.get("description") or "")[:200],
        }))
        r.ltrim(key, 0, 4)
        r.expire(key, 86400 * 7)

def main() -> int:
    r = redis_lib.Redis.from_url("redis://localhost:6379", decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)
    _ensure_group(r)

    feedback_stream = "nbus:bus.pattern.feedback.v1"
    try:
        r.xgroup_create(feedback_stream, CONSUMER_GROUP, id="0", mkstream=True)
    except redis_lib.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    sys.stderr.write("signal-router: ready\n")

    while True:
        try:
            res = r.xreadgroup(CONSUMER_GROUP, CONSUMER_NAME,
                                {STREAM: ">"}, count=10, block=2000)
            if res:
                for _, entries in res:
                    for eid, data in entries:
                        raw = data.get("_raw", "{}")
                        try:
                            parsed = json.loads(raw)
                            # Unwrap CloudEvents envelope if present
                            signal = parsed.get("data", parsed) if "specversion" in parsed else parsed
                            if not isinstance(signal, dict):
                                signal = {}
                            route(r, signal)
                        except Exception as e:
                            sys.stderr.write(f"signal-router route error: {e}\n")
                        r.xack(STREAM, CONSUMER_GROUP, eid)

            fb = r.xreadgroup(CONSUMER_GROUP, CONSUMER_NAME,
                               {feedback_stream: ">"}, count=10, block=100)
            if fb:
                for _, entries in fb:
                    for eid, data in entries:
                        try:
                            parsed = json.loads(data.get("_raw", "{}"))
                            signal = parsed.get("data", parsed) if "specversion" in parsed else parsed
                            if not isinstance(signal, dict):
                                signal = {}
                            _process_feedback(r, signal)
                        except Exception:
                            pass
                        r.xack(feedback_stream, CONSUMER_GROUP, eid)

        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stderr.write(f"signal-router: {e}\n")
            time.sleep(1)
    return 0

if __name__ == "__main__":
    sys.exit(main())
