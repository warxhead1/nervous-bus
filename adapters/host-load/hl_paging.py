"""Who CAUSES paging, as opposed to who merely holds swap.

Three vantage points, each over the window and (when a previous --record exists) the whole interval:
  process  per-pid major-fault deltas. A major fault is paid by the faulting process, so this finds the
           victims whose working set no longer fits (file-backed and swap-backed faults are not separable here).
  cgroup   memory.stat deltas per leaf unit. ``workingset_refault_anon`` is a page that was evicted to swap and
           faulted straight back: genuine swap thrash. ``workingset_refault_file`` is page-cache thrash.
           ``memory.events high`` increments mean the unit hit its MemoryHigh ceiling and was throttled/reclaimed.
  growth   memory.current delta per leaf unit: who is demanding the memory that forces other pages out.
System-wide pswpin/pswpout come from /proc/vmstat. /proc/<pid>/io read_bytes is recorded per process but is
block-layer reads, not specifically swap-ins, so it is reported as disk_read and never labelled as swap.
"""
import os
from collections import defaultdict
from pathlib import Path

CG_ROOT = "/sys/fs/cgroup"
STAT_KEYS = ("pgmajfault", "workingset_refault_anon", "workingset_refault_file")


def _kv(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, _, v = line.partition(" ")
            if v.strip().isdigit():
                out[k] = int(v)
    except OSError:
        pass
    return out


def _int(path):
    try:
        return int(Path(path).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0


def unit_of(relpath):
    parts = [p for p in relpath.split("/") if p.endswith((".service", ".scope"))]
    return parts[-1] if parts else (relpath.rsplit("/", 1)[-1] or "/")


def read_cgroups(root=CG_ROOT, limit=4000):
    """{relpath: counters} for every LEAF cgroup that has a memory controller (parents would double count)."""
    found = {}
    for dirpath, dirs, files in os.walk(root):
        if "memory.stat" not in files:
            continue
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            continue
        st = _kv(os.path.join(dirpath, "memory.stat"))
        ev = _kv(os.path.join(dirpath, "memory.events"))
        found[rel] = {"pgmajfault": st.get("pgmajfault", 0),
                      "refault_anon": st.get("workingset_refault_anon", 0),
                      "refault_file": st.get("workingset_refault_file", 0),
                      "high": ev.get("high", 0),
                      "mem_current": _int(os.path.join(dirpath, "memory.current")),
                      "swap_current": _int(os.path.join(dirpath, "memory.swap.current"))}
        if len(found) >= limit:
            break
    parents = set()
    for path in found:
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            parents.add(path)
    return {k: v for k, v in found.items() if k not in parents}


def cgroup_deltas(prev, cur, secs):
    """Rank leaf units by refault/major-fault rate and by memory growth. prev/cur are read_cgroups outputs."""
    if not prev or secs <= 0:
        return {"by_refault": [], "by_growth": [], "throttled": []}
    rows = []
    for path, c in cur.items():
        p = prev.get(path)
        if p is None:
            continue
        rows.append({"unit": unit_of(path), "path": path,
                     "pgmajfault_s": max(0, c["pgmajfault"] - p["pgmajfault"]) / secs,
                     "refault_anon_s": max(0, c["refault_anon"] - p["refault_anon"]) / secs,
                     "refault_file_s": max(0, c["refault_file"] - p["refault_file"]) / secs,
                     "high_events": max(0, c["high"] - p["high"]),
                     "mem_growth_bytes_s": (c["mem_current"] - p["mem_current"]) / secs,
                     "swap_current": c["swap_current"], "mem_current": c["mem_current"]})
    top = lambda key, n=5, floor=0.0: [  # noqa: E731
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()}
        for r in sorted(rows, key=lambda r: -r[key])[:n] if r[key] > floor]
    return {"by_refault": top("refault_anon_s", floor=0.5) or top("pgmajfault_s", floor=0.5),
            "by_growth": top("mem_growth_bytes_s", floor=1024 * 1024),
            "throttled": top("high_events", floor=0)}


def vm_rates(vm_prev, vm_now, secs):
    if not vm_prev or secs <= 0:
        return {}
    return {k: max(0, vm_now.get(k, 0) - vm_prev.get(k, 0)) / secs
            for k in ("pswpin", "pswpout", "pgmajfault") if k in vm_now and k in vm_prev}


def process_faults(procs, rate, n=5, floor=1.0):
    """Top faulting processes by major faults/s; rate is {pid: faults/s}."""
    rows = sorted(((r, pid) for pid, r in rate.items() if r >= floor and pid in procs), reverse=True)[:n]
    return [{"pid": pid, "comm": procs[pid].comm, "project": procs[pid].project,
             "agent": getattr(procs[pid], "agent", None), "majflt_s": round(r, 1)} for r, pid in rows]


def project_faults(procs, rate, n=5, floor=1.0):
    agg = defaultdict(float)
    for pid, r in rate.items():
        if pid in procs:
            agg[procs[pid].project] += r
    return [{"project": k, "majflt_s": round(v, 1)}
            for k, v in sorted(agg.items(), key=lambda kv: -kv[1])[:n] if v >= floor]


def report(procs, window_rate, interval_rate, vm_window, vm_interval, cg_window, cg_interval):
    """The `paging` block of a sample: who is paying for and who is driving memory pressure."""
    return {
        "swap_in_pages_s": round((vm_interval or vm_window).get("pswpin", 0.0), 1),
        "swap_out_pages_s": round((vm_interval or vm_window).get("pswpout", 0.0), 1),
        "window": {"vm": {k: round(v, 1) for k, v in vm_window.items()},
                   "processes": process_faults(procs, window_rate),
                   "projects": project_faults(procs, window_rate)},
        "interval": ({"vm": {k: round(v, 1) for k, v in vm_interval.items()},
                      "processes": process_faults(procs, interval_rate),
                      "projects": project_faults(procs, interval_rate)} if interval_rate is not None else None),
        "cgroups": {"window": cg_window, "interval": cg_interval},
    }
