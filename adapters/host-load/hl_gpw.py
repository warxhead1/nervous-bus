"""Read-only view of what ``game-priority-watch`` is doing to each project's units.

That watcher is THE owner of CPUWeight/CPUQuota policy on this host. Nothing here writes a property, runs
``set-property``, or imports the watcher: policy is read from the live systemd user manager (``systemctl
--user show``, the ground truth the watcher itself reasserts) and the watcher's own last ``mode=`` journal
line. Protected workloads (ac-worldserver and other ac-*, GeForce NOW, Steam, session.slice) are labelled
so a report never suggests touching them.
"""
import re
import subprocess

WATCHER_UNIT = "game-priority-watch.service"
PROTECTED_UNIT = re.compile(r"^(session\.slice|session-\d+\.scope|app-steam|sunshine|ac-)|steam|geforce", re.I)
PROTECTED_PROJECT = re.compile(r"^(azerothcore|ac-.*)$")
GEFORCE_CMD = re.compile(r"play\.geforce\.com|GeForceNOW", re.I)
MODE_LINE = re.compile(r"^(\d+(?:\.\d+)?)\s.*?mode=(\w+)\s*(.*)$")
PROPS = ("CPUWeight", "CPUQuotaPerSecUSec", "MemoryHigh", "MemorySwapMax")


def _run(cmd, timeout=6):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def watcher_mode(runner=None):
    """{"mode", "detail", "ts"} from the newest mode= line the watcher logged; mode "unknown" if unreadable."""
    try:
        out = (runner or _run)(["journalctl", "--user", "-u", WATCHER_UNIT, "-n", "40", "-o", "short-unix",
                                "--no-pager"])
    except Exception:
        return {"mode": "unknown", "detail": "journal unreadable", "ts": None}
    for line in reversed(out.splitlines()):
        m = MODE_LINE.match(line)
        if m:
            return {"mode": m.group(2), "detail": m.group(3)[:160], "ts": float(m.group(1))}
    return {"mode": "unknown", "detail": "no mode= line in recent journal", "ts": None}


def unit_policy(units, runner=None):
    """{unit: {CPUWeight, CPUQuota, MemoryHigh, MemorySwapMax}} from ONE `systemctl --user show` call."""
    units = sorted({u for u in units if u.endswith((".service", ".scope", ".slice"))})
    if not units:
        return {}
    cmd = ["systemctl", "--user", "show"] + [a for p in ("Id",) + PROPS for a in ("-p", p)] + units
    try:
        out = (runner or _run)(cmd)
    except Exception:
        return {}
    pol, cur = {}, None
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "Id":
            cur = pol.setdefault(v, {})
        elif cur is not None and k in PROPS:
            cur[k] = v
    return pol


def _weight(v):
    return int(v) if v and v.isdigit() else None


def unit_state(unit, props, project=""):
    if PROTECTED_UNIT.search(unit) or PROTECTED_PROJECT.match(project or ""):
        return "protected"
    w = _weight(props.get("CPUWeight"))
    quota = props.get("CPUQuotaPerSecUSec", "infinity")
    if w is not None and w <= 20:
        return "batched"
    if quota not in ("infinity", "", None):
        return "capped"
    if w is not None and w >= 300:
        return "boosted"
    return "default"


ORDER = ["protected", "boosted", "batched", "capped", "default"]


def annotate(procs, projects, mode_runner=None, policy_runner=None, max_units=60):
    """-> {"mode": {...}, "projects": {name: {"state", "units": [...]}}} for projects present in `projects`."""
    by_project = {}
    geforce_units = set()
    for p in procs.values():
        if not p.cgroup or p.project not in projects:
            continue
        by_project.setdefault(p.project, {}).setdefault(p.cgroup, 0)
        by_project[p.project][p.cgroup] += 1
        if p.cmdline and GEFORCE_CMD.search(" ".join(p.cmdline[:6])):
            geforce_units.add(p.cgroup)
    wanted = {u for us in by_project.values() for u in us}
    # most populated units first, bounded: one systemctl call regardless of host size
    ranked = sorted(wanted, key=lambda u: -sum(us.get(u, 0) for us in by_project.values()))[:max_units]
    policy = unit_policy(ranked, policy_runner)
    out = {}
    for proj, units in by_project.items():
        rows = []
        for unit, n in sorted(units.items(), key=lambda kv: -kv[1])[:6]:
            props = policy.get(unit, {})
            state = "protected" if unit in geforce_units else unit_state(unit, props, proj)
            rows.append({"unit": unit, "state": state if (props or state == "protected") else "unknown",
                         "cpu_weight": props.get("CPUWeight"), "cpu_quota": props.get("CPUQuotaPerSecUSec"),
                         "memory_high": props.get("MemoryHigh"), "procs": n})
        known = [r["state"] for r in rows if r["state"] != "unknown"]
        out[proj] = {"state": min(known, key=ORDER.index) if known else "unknown", "units": rows}
    return {"mode": watcher_mode(mode_runner), "projects": out}


def label(entry):
    """Short table cell: `protected`, `batched w20`, `capped 400%`, or ''."""
    if not entry or entry["state"] in ("default", "unknown"):
        return ""
    row = next((r for r in entry["units"] if r["state"] == entry["state"]), None) or {}
    if entry["state"] == "batched":
        return f"batched w{row.get('cpu_weight')}"
    if entry["state"] == "boosted":
        return f"boosted w{row.get('cpu_weight')}"
    if entry["state"] == "capped":
        q = row.get("cpu_quota", "")
        m = re.match(r"^(\d+(?:\.\d+)?)s$", q or "")
        return f"capped {float(m.group(1)) * 100:.0f}%" if m else "capped"
    return entry["state"]
