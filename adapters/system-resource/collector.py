"""Fixed kernel reads and bounded cgroup selection, without process inspection."""
import math
import hashlib
import heapq
import os
import re
from pathlib import Path

MAX_GROUPS = 8
PHYSICAL_DISK = re.compile(r"(?:nvme\d+n\d+|[sv]d[a-z]+|xvd[a-z]+|mmcblk\d+|hd[a-z]+)$")


def text(path):
    try:
        return Path(path).read_text()
    except OSError:
        return None


def number(value):
    try:
        result = int(value) if str(value).strip().isdigit() else float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def pairs(raw):
    result = {}
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0].rstrip(":")] = number(parts[1])
    return result


def psi(raw):
    result = {}
    for line in (raw or "").splitlines():
        fields = line.split()
        if not fields or fields[0] not in ("some", "full"):
            continue
        values = dict(item.split("=", 1) for item in fields[1:] if "=" in item)
        result[fields[0]] = {key: number(values.get(key)) for key in ("avg10", "avg60", "avg300")}
        result[fields[0]]["total_usec"] = number(values.get("total"))
    return result or None


def pressures(root, host=False):
    return {kind: psi(text(root / kind if host else root / (kind + ".pressure")))
            for kind in ("cpu", "memory", "io")}


def disks(raw):
    """Keep physical whole-device counters separate; never sum partitions or dm layers."""
    result = {}
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) < 14 or not PHYSICAL_DISK.fullmatch(parts[2]):
            continue
        values = [number(value) for value in parts[3:14]]
        if any(value is None for value in values):
            continue
        result[parts[0] + ":" + parts[1]] = {
            "name": parts[2], "read_bytes": values[2] * 512,
            "write_bytes": values[6] * 512, "read_ios": values[0],
            "write_ios": values[4], "read_ms": values[3], "write_ms": values[7],
            "busy_ms": values[9], "weighted_io_ms": values[10],
        }
    return result


def cgroup_io(raw, physical):
    if raw is None or not physical:
        return None, None, {}
    devices = {}
    for line in raw.splitlines():
        parts = line.split()
        if not parts or parts[0] not in physical:
            continue
        fields = dict(item.split("=", 1) for item in parts[1:] if "=" in item)
        values = {key: number(fields.get(key)) for key in ("rbytes", "wbytes")}
        if any(value is None for value in values.values()):
            return None, None, devices
        devices[parts[0]] = {"name": physical[parts[0]]["name"],
                             "read_bytes": values["rbytes"], "write_bytes": values["wbytes"]}
    if raw.strip() and not devices:
        return None, None, {}  # Only virtual/unrecognised devices were observable.
    return (sum(row["read_bytes"] for row in devices.values()),
            sum(row["write_bytes"] for row in devices.values()), devices)


def limit(raw):
    if raw is not None and raw.strip() == "max":
        return {"state": "unlimited", "value": None}
    value = number(raw)
    return {"state": "limited" if value is not None else "unavailable", "value": value}


def cgroup_sample(root, relpath, physical):
    path = root / relpath.lstrip("/")
    row = {"entity": "/" + relpath.lstrip("/"), "kind": "cgroup", "identity": None,
           "state": "unavailable", "reason": "cgroup_missing", "counters": {},
           "gauges": {}, "limits": {}, "pressure": pressures(path), "devices": {}}
    try:
        identity = str(path.stat().st_ino)
    except OSError:
        return row
    cpu, mem = pairs(text(path / "cpu.stat")), pairs(text(path / "memory.stat"))
    events = pairs(text(path / "memory.events.local"))
    rb, wb, devices = cgroup_io(text(path / "io.stat"), physical)
    quota = (text(path / "cpu.max") or "").split()
    period = number(quota[1]) if len(quota) == 2 else None
    cpu_limit = limit(quota[0] if quota and period else None)
    row.update(identity=identity, devices=devices, limits={"cpu": cpu_limit,
               "memory_high": limit(text(path / "memory.high")),
               "memory_max": limit(text(path / "memory.max"))})
    row["counters"] = {"cpu_usage_usec": cpu.get("usage_usec"),
                       "cpu_throttled_usec": cpu.get("throttled_usec"),
                       "cpu_periods": cpu.get("nr_periods"),
                       "cpu_throttled_periods": cpu.get("nr_throttled"),
                       "memory_high_events": events.get("high"),
                       "io_read_bytes": rb, "io_write_bytes": wb}
    row["gauges"] = {"memory_current_bytes": number(text(path / "memory.current")),
                     "memory_anon_bytes": mem.get("anon"), "memory_file_bytes": mem.get("file"),
                     "memory_swap_bytes": number(text(path / "memory.swap.current")),
                     "cpu_quota_cores": cpu_limit["value"] / period if cpu_limit["value"] is not None and period else None}
    required = [cpu.get("usage_usec"), period, row["gauges"]["memory_current_bytes"], rb, wb]
    row["state"] = "ok" if all(value is not None for value in required) else "partial"
    row["reason"] = None if row["state"] == "ok" else "required_counter_missing"
    try:
        if str(path.stat().st_ino) != identity:
            row.update(state="unavailable", reason="cgroup_recreated_during_read", identity=None)
    except OSError:
        row.update(state="unavailable", reason="cgroup_disappeared", identity=None)
    return row


def collect(proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup"), selectors=("user.slice",)):
    boot_id = (text(proc_root / "sys/kernel/random/boot_id") or "").strip() or None
    physical = disks(text(proc_root / "diskstats"))
    memory = pairs(text(proc_root / "meminfo"))
    cpu_line = next((line.split()[1:9] for line in (text(proc_root / "stat") or "").splitlines()
                     if line.startswith("cpu ")), [])
    cpu = [number(value) for value in cpu_line]
    used = (sum(cpu) - cpu[3] - cpu[4]) * 1_000_000 / os.sysconf("SC_CLK_TCK") if len(cpu) == 8 and None not in cpu else None
    total, available = memory.get("MemTotal"), memory.get("MemAvailable")
    host = {"entity": "@host", "kind": "host", "identity": boot_id, "state": "ok", "reason": None,
            "counters": {"cpu_usage_usec": used,
                         "io_read_bytes": sum(d["read_bytes"] for d in physical.values()) if physical else None,
                         "io_write_bytes": sum(d["write_bytes"] for d in physical.values()) if physical else None},
            "gauges": {"memory_total_bytes": total * 1024 if total is not None else None,
                       "memory_available_bytes": available * 1024 if available is not None else None,
                       "memory_current_bytes": (total - available) * 1024 if total is not None and available is not None else None,
                       "swap_free_bytes": memory["SwapFree"] * 1024 if memory.get("SwapFree") is not None else None},
            "limits": {}, "pressure": pressures(proc_root / "pressure", host=True), "devices": physical}
    if not boot_id or used is None or not physical or total is None or available is None:
        host.update(state="partial", reason="host_counter_missing")
    selected, warnings = [], []
    for selector in selectors[:MAX_GROUPS]:
        relative = selector.lstrip("/")
        if ".." in Path(relative).parts or len(relative) > 512:
            raise ValueError("invalid cgroup selector")
        if any(char in relative for char in "*?["):
            if "**" in relative or any(char in str(Path(relative).parent) for char in "*?["):
                raise ValueError("cgroup patterns may match only the final path component")
            matches = heapq.nsmallest(MAX_GROUPS + 1, cgroup_root.glob(relative))
            names = [str(path.relative_to(cgroup_root)) for path in matches] or [relative]
        else:
            names = [relative]
        for name in names:
            if name not in selected:
                selected.append(name)
    if len(selected) > MAX_GROUPS or len(selectors) > MAX_GROUPS:
        warnings.append("cgroup_selection_truncated")
    rows = [cgroup_sample(cgroup_root, name, physical) for name in selected[:MAX_GROUPS]]
    device_identity = hashlib.sha256(repr(sorted((key, d['name']) for key, d in physical.items())).encode()).hexdigest()
    for row in [host, *rows]:
        row["device_identity"] = device_identity
    return {"boot_id": boot_id, "entities": [host, *rows], "warnings": warnings}
