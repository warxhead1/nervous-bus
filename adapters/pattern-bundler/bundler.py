#!/usr/bin/env python3
"""pattern-bundler — windowed stats on nbus:all + nbus:logs → nbus:bundles."""
from __future__ import annotations
import datetime, json, os, subprocess, sys, time
from pathlib import Path
import redis as redis_lib
sys.path.insert(0, str(Path(__file__).parent))
from fingerprint import fingerprint as _fingerprint, fp_hash as _fp_hash

def _record_error_fps(r: redis_lib.Redis, channel: str, error_events: list) -> None:
    """Write error event fingerprints to Redis for this channel."""
    if not error_events:
        return
    now = time.time()
    key = f"pattern:err-fp:{channel}"
    for raw in error_events[:20]:
        try:
            ev = json.loads(raw) if isinstance(raw, str) else raw
            msg = ev.get("message", str(raw))[:500]
        except Exception:
            msg = str(raw)[:500]
        fp = _fingerprint(msg)
        fph = _fp_hash(fp)
        existing = r.hget(key, fph)
        if existing:
            entry = json.loads(existing)
            entry["count"] += 1
            entry["last_seen"] = now
        else:
            entry = {
                "fp": fp, "count": 1,
                "first_seen": now, "last_seen": now,
                "dismissed": 0, "sample": msg[:200],
            }
        r.hset(key, fph, json.dumps(entry))
    r.expire(key, 86400 * 30)

def _get_transient_context(r: redis_lib.Redis, channel: str) -> list:
    """Return recurring fingerprints (count >= 2) sorted by count desc."""
    key = f"pattern:err-fp:{channel}"
    try:
        all_fps = r.hgetall(key)
    except Exception:
        return []
    entries = []
    for raw in all_fps.values():
        try:
            entry = json.loads(raw)
            if entry.get("count", 0) >= 2:
                entries.append({
                    "fp":        entry["fp"],
                    "count":     entry["count"],
                    "dismissed": entry.get("dismissed", 0),
                    "sample":    entry.get("sample", "")[:100],
                    "last_seen": entry.get("last_seen", 0),
                })
        except Exception:
            pass
    entries.sort(key=lambda x: x["count"], reverse=True)
    return entries[:8]

def _has_dense_fingerprints(
    r,
    channel: str,
    min_count: int = 3,
    min_distinct: int = 2,
    window_s: float = 86400.0,
) -> bool:
    """True if channel has min_distinct fingerprints each seen >= min_count times recently.

    Overrides low_interest during cold-start when real errors are accumulating
    even before Welford baselines have warmed up.
    """
    now = time.time()
    key = f"pattern:err-fp:{channel}"
    try:
        all_fps = r.hgetall(key)
    except Exception:
        return False

    qualifying = 0
    for raw in all_fps.values():
        try:
            entry = json.loads(raw)
            if (entry.get("count", 0) >= min_count and
                    now - entry.get("last_seen", 0) <= window_s):
                qualifying += 1
                if qualifying >= min_distinct:
                    return True
        except Exception:
            pass
    return False

def _peer_snapshot(windows: dict, baselines: dict, exclude_channel: str) -> list:
    """Compact view of all other open windows' current state at emit time."""
    from baseline import welford_deviation
    peers = []
    for win in windows.values():
        if win.channel == exclude_channel or win.count < 5:
            continue
        bl = baselines.get(win.channel)
        if not bl:
            continue
        rate = win.count / max(1, time.time() - win.opened_at) * 60
        dev = welford_deviation(bl, rate)
        peers.append({
            "channel":         win.channel,
            "rate_per_min":    round(rate, 2),
            "deviation_sigma": round(dev, 2) if dev is not None else None,
            "error_count":     win.error_count,
        })
    peers.sort(key=lambda x: abs(x.get("deviation_sigma") or 0), reverse=True)
    return peers[:10]


def _any_peer_elevated(windows: dict, baselines: dict, exclude_channel: str, threshold: float = 2.5) -> bool:
    """True if any other open window is currently above threshold sigma."""
    from baseline import welford_deviation
    for win in windows.values():
        if win.channel == exclude_channel or win.count < 5:
            continue
        bl = baselines.get(win.channel)
        if not bl:
            continue
        rate = win.count / max(1, time.time() - win.opened_at) * 60
        dev = welford_deviation(bl, rate)
        if dev is not None and abs(dev) >= threshold:
            return True
    return False


DEFAULT_CONFIG = Path(__file__).parent / "bundler.toml"

def _cfg(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore
    with open(path, "rb") as f:
        return tomllib.load(f)

def _ulid() -> str:
    ts = int(time.time() * 1000)
    rnd = os.urandom(10).hex().upper()[:16]
    return f"{ts:013d}{rnd}"

def _load_offset(path: Path) -> str:
    try:
        return path.expanduser().read_text().strip() or "0"
    except Exception:
        return "0"

def _save_offset(path: Path, sid: str) -> None:
    p = path.expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sid)

def _publish(bundle: dict) -> None:
    try:
        subprocess.run(
            ["nervous", "publish", "bus.pattern.bundle.v1", json.dumps(bundle)],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        sys.stderr.write(f"bundler publish: {e}\n")

def _emit(r: redis_lib.Redis, window, deviation, baseline_n: int, maxlen: int, windows: dict | None = None, baselines: dict | None = None) -> None:
    from window import Window  # noqa: F401 — local import guards against circular dep
    stats = window.compute_stats(deviation)
    stats["baseline_n"] = baseline_n
    _record_error_fps(r, window.channel, window.error_events)
    transient_context = _get_transient_context(r, window.channel)
    low_interest = window.is_low_interest(deviation)
    # Fingerprint density gate: real errors accumulating → force evaluation
    # even during cold-start before Welford baselines have warmed up.
    if low_interest and _has_dense_fingerprints(r, window.channel):
        low_interest = False
    if low_interest and windows and baselines and _any_peer_elevated(windows, baselines, window.channel):
        low_interest = False
    # Pattern frequency distribution — top 20 fingerprints by occurrence
    total_events = max(window.count, 1)
    pattern_dist = sorted(
        [
            {
                "fp":           fp,
                "count":        count,
                "rate_per_100": round(count / total_events * 100, 2),
            }
            for fp, count in window.fp_counts.items()
        ],
        key=lambda x: -x["count"],
    )[:20]
    peer_snapshot = _peer_snapshot(windows or {}, baselines or {}, window.channel)
    bundle = {
        "bundle_id": _ulid(),
        "source_stream": window.source_stream,
        "channel": window.channel,
        "window_start": datetime.datetime.utcfromtimestamp(window.opened_at).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_count": window.count,
        "low_interest": low_interest,
        "sample_events": list(window.events),
        "error_events": window.error_events[:20],
        "transient_context": transient_context,
        "pattern_dist": pattern_dist,
        "stats": stats,
        "peer_snapshot": peer_snapshot,
    }
    fields = {
        "bundle_id": bundle["bundle_id"],
        "channel": bundle["channel"],
        "source_stream": bundle["source_stream"],
        "low_interest": "1" if bundle["low_interest"] else "0",
        "event_count": str(bundle["event_count"]),
        "error_events": json.dumps(bundle["error_events"]),
        "transient_context": json.dumps(transient_context),
        "pattern_dist": json.dumps(pattern_dist),
        "peer_snapshot": json.dumps(peer_snapshot),
        "_raw": json.dumps(bundle)[:8000],
    }
    r.xadd("nbus:bundles", fields, maxlen=maxlen, approximate=True)
    if not bundle["low_interest"]:
        _publish(bundle)

def _channel_triggers(channel: str, cfg: dict) -> tuple[int, float]:
    """Return (count_trigger, time_trigger_s) for a channel, applying overrides."""
    for override in cfg.get("channel_overrides", []):
        if channel.startswith(override["pattern"]):
            return override["count_trigger"], float(override["time_trigger_s"])
    # Fall back to source-type defaults
    win_cfg = cfg.get("windows", {})
    if channel.startswith("log:"):
        return win_cfg.get("log_count_trigger", 100), float(win_cfg.get("log_time_trigger_s", 300))
    return win_cfg.get("bus_count_trigger", 50), float(win_cfg.get("bus_time_trigger_s", 900))


def main() -> int:
    sys.path.insert(0, str(Path(__file__).parent))
    from baseline import (load_baseline_redis, save_baseline_redis,
                          welford_update, welford_deviation, init_thresholds)
    from window import Window

    cfg = _cfg(DEFAULT_CONFIG)
    r = redis_lib.Redis.from_url(cfg["redis"]["url"], decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)

    win_cfg = cfg.get("windows", {})
    bc, bt = win_cfg.get("bus_count_trigger", 50), win_cfg.get("bus_time_trigger_s", 900)
    lc, lt = win_cfg.get("log_count_trigger", 100), win_cfg.get("log_time_trigger_s", 300)
    maxlen = cfg.get("streams", {}).get("bundles_maxlen", 1000)

    off_cfg = cfg.get("offsets", {})
    all_off  = Path(off_cfg.get("all_offset_file",  "~/.cache/nervous-bus/bundler-all-offset"))
    logs_off = Path(off_cfg.get("logs_offset_file", "~/.cache/nervous-bus/bundler-logs-offset"))

    last_all  = _load_offset(all_off)
    last_logs = _load_offset(logs_off)

    windows: dict[str, Window] = {}
    baselines: dict[str, dict] = {}
    bl_dirty: set[str] = set()
    last_bl_flush = time.time()

    while True:
        try:
            # --- nbus:all ---
            res = r.xread({"nbus:all": last_all}, count=100, block=200)
            if res:
                for _, entries in res:
                    for eid, data in entries:
                        last_all = eid
                        raw = data.get("_raw", "{}")
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        ch = ev.get("type", "unknown")
                        key = f"bus:{ch}"
                        if key not in windows:
                            count_t, time_t = _channel_triggers(ch, cfg)
                            windows[key] = Window(ch, "bus", count_t, time_t)
                            if ch not in baselines:
                                baselines[ch] = load_baseline_redis(r, ch)
                            init_thresholds(r, ch)
                        windows[key].ingest(raw, ev.get("data") if isinstance(ev.get("data"), dict) else {})
                        rate = windows[key].count / max(1, time.time() - windows[key].opened_at) * 60
                        baselines[ch] = welford_update(baselines[ch], rate)
                        bl_dirty.add(ch)
                _save_offset(all_off, last_all)

            # --- nbus:logs ---
            res_l = r.xread({"nbus:logs": last_logs}, count=200, block=100)
            if res_l:
                for _, entries in res_l:
                    for eid, data in entries:
                        last_logs = eid
                        src = data.get("log_source", "unknown")
                        svc = data.get("service", "unknown")
                        ch = f"log:{src}:{svc}"
                        key = f"logs:{ch}"
                        if key not in windows:
                            count_t, time_t = _channel_triggers(ch, cfg)
                            windows[key] = Window(ch, "logs", count_t, time_t)
                            if ch not in baselines:
                                baselines[ch] = load_baseline_redis(r, ch)
                        windows[key].ingest_log(dict(data))
                _save_offset(logs_off, last_logs)

            # --- check windows ---
            for key, win in list(windows.items()):
                if win.should_close():
                    ch = win.channel
                    bl = baselines.get(ch, load_baseline_redis(r, ch))
                    rate = win.count / max(1, time.time() - win.opened_at) * 60
                    dev = welford_deviation(bl, rate)
                    _emit(r, win, dev, bl["n"], maxlen, windows=windows, baselines=baselines)
                    src = win.source_stream
                    count_t, time_t = _channel_triggers(win.channel, cfg)
                    windows[key] = Window(win.channel, src, count_t, time_t)

            # --- flush baselines to Redis every 60s ---
            if time.time() - last_bl_flush >= 60:
                for ch in bl_dirty:
                    save_baseline_redis(r, ch, baselines[ch])
                bl_dirty.clear()
                last_bl_flush = time.time()

        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stderr.write(f"bundler: {e}\n")
            time.sleep(1)

    return 0

if __name__ == "__main__":
    sys.exit(main())
