"""CPU and major-fault rates integrated over the whole gap between two --record runs.

The 3s window in ``hl_store.sample`` is a spot reading. A snapshot of cumulative
per-process counters, keyed by (pid, start_ticks), turns two consecutive runs into a
true interval mean, so a build that burned 8 cores for 40 of 60 seconds is not missed.
"""
from pathlib import Path

from hl_procs import CLK_TCK

MAX_GAP_S = 15 * 60          # an older snapshot is no longer "the previous interval"


def boot_id(proc_root="/proc"):
    try:
        return (Path(proc_root) / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def busy_ticks(proc_root="/proc"):
    """System-wide non-idle jiffies from /proc/stat (user nice system irq softirq steal). None if absent."""
    try:
        f = (Path(proc_root) / "stat").read_text().splitlines()[0].split()
        v = [int(x) for x in f[1:9]] + [0] * 8
    except (OSError, IndexError, ValueError):
        return None
    return v[0] + v[1] + v[2] + v[5] + v[6] + v[7]


def snapshot(procs, ts, uptime_s, boot, busy):
    return {"ts": ts, "uptime": uptime_s, "boot": boot, "busy": busy,
            "rows": {p.key: (p.cpu_ticks, p.majflt, p.read_bytes, p.child_ticks, p.ppid) for p in procs.values()}}


def integrate(prev, procs, now_ts, uptime_s, boot, busy):
    """-> (per_pid, info). per_pid is None (with info["reason"]) when no usable previous snapshot.

    per_pid[pid] = {"cores", "majflt_s", "read_bytes_s"} over the interval since ``prev``.
    CPU a process spent in children it has since reaped (cutime+cstime) is credited to that
    process, which recovers short-lived workers (compiler invocations, test runners) that exited
    between runs. A process absent from ``prev`` but born after it counts its whole life (it lived entirely
    inside the interval). Processes that exited before this run are unobservable; ``coverage``
    says how much of the system's busy time the observed deltas explain.
    """
    if not prev:
        return None, {"reason": "no previous snapshot"}
    gap = now_ts - prev["ts"]
    if gap <= 0 or gap > MAX_GAP_S:
        return None, {"reason": f"previous snapshot {gap:.0f}s old"}
    if prev.get("boot") and boot and prev["boot"] != boot:
        return None, {"reason": "rebooted"}
    up_gap = uptime_s - prev["uptime"] if prev.get("uptime") and uptime_s else gap
    if up_gap <= 0:
        return None, {"reason": "uptime went backwards"}
    # A child that exited in the gap hands its WHOLE lifetime to its parent's cutime. Its time up to the
    # previous snapshot was not this interval's, so take that part back from the parent.
    now_keys = {p.key for p in procs.values()}
    by_pid_prev = {k[0]: k for k in prev["rows"]}
    gone_ticks = {}
    for key, row in prev["rows"].items():
        if key in now_keys or len(row) < 5:
            continue
        if by_pid_prev.get(row[4]) in now_keys:
            gone_ticks[row[4]] = gone_ticks.get(row[4], 0) + row[0] + row[3]
    out, observed = {}, 0
    born_after = prev["uptime"] * CLK_TCK if prev.get("uptime") else float("inf")
    for pid, p in procs.items():
        before = prev["rows"].get(p.key)
        if before is not None:
            dt = (p.cpu_ticks + p.child_ticks) - (before[0] + before[3])
            dm = p.majflt - before[1]
            dr = p.read_bytes - before[2] if p.read_bytes >= 0 and before[2] >= 0 else 0
        elif p.start_ticks > born_after:
            dt, dm, dr = p.cpu_ticks + p.child_ticks, p.majflt, max(p.read_bytes, 0)
        else:
            continue
        dt = max(dt - gone_ticks.get(pid, 0), 0)
        observed += dt
        out[pid] = {"cores": dt / CLK_TCK / up_gap, "majflt_s": max(dm, 0) / up_gap,
                    "read_bytes_s": max(dr, 0) / up_gap}
    info = {"interval_s": round(up_gap, 1), "observed_cores": round(observed / CLK_TCK / up_gap, 3)}
    if busy is not None and prev.get("busy") is not None:
        total = (busy - prev["busy"]) / CLK_TCK / up_gap
        info["system_busy_cores"] = round(total, 3)
        info["coverage"] = round(min(1.0, info["observed_cores"] / total), 3) if total > 0.01 else 1.0
        info["unattributed_cores"] = round(max(0.0, total - info["observed_cores"]), 3)
    return out, info
