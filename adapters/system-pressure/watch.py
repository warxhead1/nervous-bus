#!/usr/bin/env python3
"""system-pressure — host resource-pressure watchdog (disk, dir growth, restart storms).

Motivating incident (2026-08-30): a hearth-loom poller comment loop drove 4-8
hearth-db commits/min, each triggering a ~219M dolt backup sync into
hearth/.beads/backup — ~280G written in a 2h window, /home hit 100%, hooks and
sessions started failing on ENOSPC. Separately, beads-dolt.service crash-looped
through 800 restarts ("port already in use") over ~3.5h. NOTHING alerted on
either; both were discovered by a session hitting a full disk. This adapter
closes that gap.

Three checks per poll, one state file, one report:

1. **Disk** — statvfs on each watched mountpoint. warn >=90%, critical >=95%.
2. **Dir growth** — size of watched bloat-prone dirs (default: every
   ~/projects/*/.beads/backup). warn: >5 GB/h growth or >50G absolute;
   critical: >20 GB/h. Rate is measured between polls via persisted state.
3. **Restart storms** — NRestarts per systemd user service, diffed against
   the previous poll. warn: unit in failed state; critical: >=5 restarts
   added within one poll interval.

Events (`bus.system.pressure.v1`) are emitted ONLY on level transitions
(ok→warn→critical and recovery), with a re-reminder at most every 6h for
sustained non-ok — never per-poll spam. Critical transitions additionally
fan out a `bus.notify.v1` so wired transports (phone/discord/session) see it.

Usage:
    python3 watch.py             # real run: measure, publish transitions, write report
    python3 watch.py --dry-run   # measure + report, never publish
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

CACHE_DIR = Path(os.environ.get("NERVOUS_SYSTEM_PRESSURE_CACHE", str(Path.home() / ".cache" / "nervous-bus" / "system-pressure")))
STATE_FILE = CACHE_DIR / "state.json"
REPORT_FILE = CACHE_DIR / "report.md"
NERVOUS_BIN = os.environ.get(
    "NERVOUS_BIN",
    str(Path(__file__).resolve().parent.parent.parent / "sdk" / "shell" / "nervous"),
)

WATCHED_MOUNTS = ["/home", str(Path.home() / "data2"), "/tmp"]
WATCHED_DIR_GLOBS = [str(Path.home() / "projects" / "*" / ".beads" / "backup")]

DISK_WARN_PCT = 90.0
DISK_CRIT_PCT = 95.0
DIR_ABS_WARN_BYTES = 50 * 1024**3
DIR_GROWTH_WARN_GBH = 5.0
DIR_GROWTH_CRIT_GBH = 20.0
RESTART_STORM_DELTA = 5
REMIND_INTERVAL_S = 6 * 3600

LEVELS = {"ok": 0, "warn": 1, "critical": 2}


# ---------- pure classification (unit-tested) ----------

def disk_level(used_pct: float) -> str:
    if used_pct >= DISK_CRIT_PCT:
        return "critical"
    if used_pct >= DISK_WARN_PCT:
        return "warn"
    return "ok"


def growth_gb_per_hour(prev_bytes: Optional[int], cur_bytes: int, elapsed_s: float) -> Optional[float]:
    if prev_bytes is None or elapsed_s <= 0:
        return None
    return (cur_bytes - prev_bytes) / 1024**3 / (elapsed_s / 3600.0)


def dir_level(size_bytes: int, gbh: Optional[float]) -> str:
    if gbh is not None and gbh >= DIR_GROWTH_CRIT_GBH:
        return "critical"
    if size_bytes >= DIR_ABS_WARN_BYTES or (gbh is not None and gbh >= DIR_GROWTH_WARN_GBH):
        return "warn"
    return "ok"


def restarts_level(delta: Optional[int], active_state: str) -> str:
    if delta is not None and delta >= RESTART_STORM_DELTA:
        return "critical"
    if active_state == "failed":
        return "warn"
    return "ok"


def should_emit(prev_level: str, level: str, last_emit_ts: float, now: float) -> bool:
    """Transition-only emission with a bounded re-reminder for sustained pressure."""
    if level != prev_level:
        return True
    if level != "ok" and now - last_emit_ts >= REMIND_INTERVAL_S:
        return True
    return False


# ---------- measurement ----------

def measure_disks() -> List[dict]:
    out = []
    for mount in WATCHED_MOUNTS:
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        if total == 0:
            continue
        used_pct = round(100.0 * (total - free) / total, 1)
        out.append({"key": mount, "used_pct": used_pct, "free_bytes": free})
    return out


def measure_dirs() -> List[dict]:
    import glob
    out = []
    for pattern in WATCHED_DIR_GLOBS:
        for d in sorted(glob.glob(pattern)):
            try:
                r = subprocess.run(["du", "-sb", d], capture_output=True, text=True, timeout=120)
                size = int(r.stdout.split()[0])
            except Exception:
                continue
            out.append({"key": d, "size_bytes": size})
    return out


def measure_units() -> List[dict]:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service", "--all",
             "--no-legend", "--plain", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        )
        names = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        return []
    out = []
    for i in range(0, len(names), 40):
        batch = names[i:i + 40]
        try:
            r = subprocess.run(
                ["systemctl", "--user", "show", *batch, "-p", "Id,NRestarts,ActiveState"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue
        for block in r.stdout.split("\n\n"):
            props = dict(ln.split("=", 1) for ln in block.splitlines() if "=" in ln)
            if props.get("Id"):
                out.append({
                    "key": props["Id"],
                    "restarts": int(props.get("NRestarts", "0") or 0),
                    "active_state": props.get("ActiveState", "unknown"),
                })
    return out


# ---------- state / emission ----------

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def publish(channel: str, payload: dict, *, dry_run: bool) -> None:
    if dry_run:
        sys.stderr.write(f"[system-pressure] (dry-run) would publish {channel}: {json.dumps(payload)}\n")
        return
    try:
        subprocess.run(
            [NERVOUS_BIN, "publish", channel, json.dumps(payload)],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        sys.stderr.write(f"[system-pressure] publish {channel} failed for {payload.get('key')}: {e}\n")


def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}T"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = time.time()
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    host = socket.gethostname()
    state = load_state()
    prev = state.get("items", {})
    prev_ts = state.get("ts", None)
    elapsed = now - prev_ts if prev_ts else 0.0

    findings = []  # (kind, key, level, summary, extra)

    for d in measure_disks():
        lv = disk_level(d["used_pct"])
        findings.append(("disk", d["key"], lv,
                         f"{d['key']} at {d['used_pct']}% ({human_bytes(d['free_bytes'])} free)",
                         {"used_pct": d["used_pct"], "free_bytes": d["free_bytes"]}))

    for d in measure_dirs():
        pv = prev.get(f"dir_growth:{d['key']}", {})
        gbh = growth_gb_per_hour(pv.get("size_bytes"), d["size_bytes"], elapsed)
        lv = dir_level(d["size_bytes"], gbh)
        rate = f", growing {gbh:.1f} GB/h" if gbh is not None and abs(gbh) >= 0.1 else ""
        findings.append(("dir_growth", d["key"], lv,
                         f"{d['key']} is {human_bytes(d['size_bytes'])}{rate}",
                         {"size_bytes": d["size_bytes"],
                          **({"growth_gb_per_hour": round(gbh, 2)} if gbh is not None else {})}))

    for u in measure_units():
        pv = prev.get(f"unit_restarts:{u['key']}", {})
        delta = u["restarts"] - pv["restarts"] if "restarts" in pv else None
        lv = restarts_level(delta, u["active_state"])
        if lv == "ok" and u["restarts"] == 0 and u["active_state"] != "failed":
            # keep state small: only track units that have ever restarted or failed
            if f"unit_restarts:{u['key']}" not in prev:
                continue
        d_txt = f" (+{delta} this poll)" if delta else ""
        findings.append(("unit_restarts", u["key"], lv,
                         f"{u['key']} {u['active_state']}, {u['restarts']} restarts{d_txt}",
                         {"restarts": u["restarts"], "active_state": u["active_state"],
                          **({"restart_delta": delta} if delta is not None else {})}))

    new_items: Dict[str, dict] = {}
    emitted = []
    for kind, key, level, summary, extra in findings:
        sk = f"{kind}:{key}"
        pv = prev.get(sk, {})
        prev_level = pv.get("level", "unknown")
        last_emit = pv.get("last_emit_ts", 0.0)
        emit = should_emit(prev_level if prev_level != "unknown" else "ok", level, last_emit, now) \
            if prev_level != "unknown" else level != "ok"
        if emit:
            payload = {"host": host, "kind": kind, "key": key, "level": level,
                       "prev_level": prev_level, "summary": summary[:200], "ts": ts, **extra}
            publish("bus.system.pressure.v1", payload, dry_run=args.dry_run)
            if level == "critical":
                publish("bus.notify.v1", {
                    "priority": "critical", "channels": ["session"],
                    "summary": f"system pressure on {host}: {summary}"[:140],
                    "source_project": "nervous-bus", "ts": ts,
                    "source_event_type": "bus.system.pressure.v1",
                    "dedup_key": f"system-pressure:{sk}",
                }, dry_run=args.dry_run)
            emitted.append((sk, level, summary))
        new_items[sk] = {"level": level, "last_emit_ts": now if emit else last_emit, **extra}

    save_state({"ts": now, "items": new_items})

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"# system-pressure — {host}", f"Updated: {ts}", ""]
    for want in ("critical", "warn"):
        rows = [(k, key, s) for k, key, lvl, s, _ in findings if lvl == want]
        if rows:
            lines.append(f"## {want.upper()}")
            lines += [f"- [{kind}] {summary}" for kind, _, summary in rows]
            lines.append("")
    if len(lines) == 3:
        lines.append("All watched resources ok.")
    lines.append("")
    lines.append(f"Watched: {len(findings)} items "
                 f"({sum(1 for f in findings if f[0] == 'disk')} disks, "
                 f"{sum(1 for f in findings if f[0] == 'dir_growth')} dirs, "
                 f"{sum(1 for f in findings if f[0] == 'unit_restarts')} units). "
                 f"Emitted {len(emitted)} transition(s) this poll.")
    REPORT_FILE.write_text("\n".join(lines) + "\n")

    for sk, level, summary in emitted:
        print(f"[system-pressure] {level}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
