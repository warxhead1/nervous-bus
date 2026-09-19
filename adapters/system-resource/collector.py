"""Bounded Linux cgroup-v2 and PSI collection; no process inspection."""
from __future__ import annotations

from pathlib import Path

COUNTERS = ("cpu_usage_usec", "cpu_throttled_usec", "io_read_bytes", "io_write_bytes")


def _text(path: Path):
    try:
        return path.read_text()
    except OSError:
        return None


def _pairs(text):
    result = {}
    for line in (text or "").splitlines():
        tokens = line.split()
        for token in tokens:
            if "=" in token:
                key, value = token.split("=", 1)
                result[key] = value
        if len(tokens) >= 2 and "=" not in tokens[1]:
            result[tokens[0]] = tokens[1]
    return result


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def psi(text):
    """Parse a PSI small file without claiming missing values are zero."""
    result = {}
    for line in (text or "").splitlines():
        fields = _pairs(line)
        kind = line.split(" ", 1)[0] if line else ""
        if kind in ("some", "full"):
            result[kind] = {key: float(fields[key]) for key in ("avg10", "avg60", "avg300") if key in fields}
            total = _number(fields.get("total"))
            if total is not None:
                result[kind]["total_usec"] = total
    return result


def io_totals(text):
    read_bytes = write_bytes = 0
    found = False
    for line in (text or "").splitlines():
        fields = _pairs(" ".join(line.split()[1:]))
        if not fields:
            continue
        found = True
        read_bytes += _number(fields.get("rbytes")) or 0
        write_bytes += _number(fields.get("wbytes")) or 0
    return (read_bytes, write_bytes) if found else (None, None)


def meminfo(text):
    values = {key.rstrip(":"): value for key, value in _pairs(text).items()}
    return {"host_memory_total_bytes": (_number(values.get("MemTotal")) or 0) * 1024 or None,
            "host_memory_available_bytes": (_number(values.get("MemAvailable")) or 0) * 1024 or None,
            "host_swap_free_bytes": (_number(values.get("SwapFree")) or 0) * 1024 or None}


def diskstats(text):
    read = write = 0; found = False
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 10:
            continue
        sectors_read, sectors_write = _number(parts[5]), _number(parts[9])
        if sectors_read is None or sectors_write is None:
            continue
        read += sectors_read; write += sectors_write; found = True
    return {"host_disk_read_sectors": read if found else None, "host_disk_write_sectors": write if found else None}


def cgroup_sample(root: Path, relpath: str):
    """Read a fixed cgroup path.  The inode fences recreated cgroups."""
    path = root / relpath.lstrip("/")
    try:
        identity = str(path.stat().st_ino)
    except OSError:
        return {"cgroup": relpath, "state": "unavailable", "reason": "cgroup_missing"}
    cpu = _pairs(_text(path / "cpu.stat"))
    mem = _pairs(_text(path / "memory.stat"))
    events = _pairs(_text(path / "memory.events"))
    rb, wb = io_totals(_text(path / "io.stat"))
    quota = (_text(path / "cpu.max") or "").split()
    values = {
        "cgroup": relpath, "cgroup_id": identity, "state": "ok",
        "cpu_usage_usec": _number(cpu.get("usage_usec")),
        "cpu_throttled_usec": _number(cpu.get("throttled_usec")),
        "cpu_quota_usec": None if not quota or quota[0] == "max" else _number(quota[0]),
        "cpu_period_usec": _number(quota[1]) if len(quota) == 2 else None,
        "memory_current_bytes": _number(_text(path / "memory.current")),
        "memory_anon_bytes": _number(mem.get("anon")), "memory_file_bytes": _number(mem.get("file")),
        "memory_swap_bytes": _number(_text(path / "memory.swap.current")), "memory_high_bytes": _number(_text(path / "memory.high")),
        "memory_max_bytes": _number(_text(path / "memory.max")), "io_read_bytes": rb, "io_write_bytes": wb,
        "memory_events": {k: _number(v) for k, v in events.items() if _number(v) is not None},
    }
    required = (values["cpu_usage_usec"], values["cpu_period_usec"], values["memory_current_bytes"], rb, wb)
    if any(value is None for value in required):
        values["state"] = "unavailable"; values["reason"] = "required_counter_missing"
    values["pressure"] = {name: psi(_text(path / (name + ".pressure"))) or {"state": "unavailable"}
                          for name in ("cpu", "memory", "io")}
    return values


def collect(proc_root=Path("/proc"), cgroup_root=Path("/sys/fs/cgroup"), cgroups=("user.slice",)):
    """Return only configured cgroups and fixed kernel files; never enumerate tasks."""
    boot_id = (_text(proc_root / "sys/kernel/random/boot_id") or "").strip() or None
    pressures = {name: psi(_text(proc_root / "pressure" / name)) or {"state": "unavailable"}
                 for name in ("cpu", "memory", "io")}
    return {"boot_id": boot_id, "pressure": pressures, "host": {**meminfo(_text(proc_root / "meminfo")), **diskstats(_text(proc_root / "diskstats"))},
            "cgroups": [cgroup_sample(cgroup_root, item) for item in cgroups]}


def intervalize(current, previous, elapsed_s):
    """Calculate rates only across a matching boot and cgroup inode identity."""
    if not previous or current.get("state") != "ok" or previous.get("state") != "ok":
        return {"interval_state": "unavailable", "reason": "no_previous_or_unavailable"}
    if not current.get("boot_id") or not previous.get("boot_id") or not current.get("cgroup_id") or not previous.get("cgroup_id"):
        return {"interval_state": "unavailable", "reason": "identity_unavailable"}
    if current.get("boot_id") != previous.get("boot_id") or current.get("cgroup_id") != previous.get("cgroup_id"):
        return {"interval_state": "reset", "reason": "boot_or_cgroup_identity_changed"}
    if not elapsed_s or elapsed_s <= 0:
        return {"interval_state": "unavailable", "reason": "invalid_elapsed"}
    result = {"interval_state": "ok", "elapsed_s": elapsed_s}
    for field in COUNTERS:
        cur, old = current.get(field), previous.get(field)
        if cur is None or old is None:
            continue
        delta = cur - old
        if delta < 0:
            return {"interval_state": "reset", "reason": "counter_decreased"}
        result[field + "_delta"] = delta
        result[field + "_per_s"] = delta / elapsed_s
    return result
