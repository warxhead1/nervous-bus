"""Read-only per-process sampling and project attribution from /proc.

Every function takes ``proc_root`` so tests drive a fake tree. Nothing here
signals, renices, or writes anything.
"""
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

HOME = "/home/eric"
# First match wins; group 1 is the project.
PATH_RULES = [
    re.compile(r"^" + HOME + r"/projects/([^/\s]+)"),
    re.compile(r"^" + HOME + r"/data2/worktrees/([^/\s]+)"),
    re.compile(r"^" + HOME + r"/data2/orca/([^/\s]+)"),
]
DOCKER_ID = re.compile(r"docker-([0-9a-f]{64})\.scope")
SECRET = re.compile(r"(?i)((?:token|secret|password|passwd|key|auth|bearer)[\w-]*[=:\s]+)\S+")
LONGTOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    state: str
    uid: int
    start_ticks: int
    cpu_ticks: int
    rss_bytes: int
    swap_bytes: int
    threads: int
    cwd: str          # "" when unreadable
    cmdline: list
    cgroup: str       # deepest .service/.scope unit, "" when unreadable
    io_bytes: int     # rchar+wchar, -1 when unreadable
    project: str = "unknown"
    basis: str = "none"
    majflt: int = 0           # cumulative major faults (stat field 12)
    read_bytes: int = -1      # /proc/pid/io read_bytes (block-layer reads), -1 when unreadable
    anon_bytes: int = -1      # RssAnon from status, -1 when absent
    cgroup_path: str = ""     # full cgroup v2 path
    child_ticks: int = 0      # cutime+cstime: CPU of already-reaped children, folded in on wait()

    @property
    def key(self):
        return (self.pid, self.start_ticks)


def _read(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return None


def scrub(text, limit=120):
    text = SECRET.sub(r"\1<redacted>", text)
    text = LONGTOKEN.sub("<redacted>", text)
    return text[:limit]


def parse_stat(raw):
    """comm may contain spaces and parens; split on the LAST ')'."""
    left, right = raw.find("("), raw.rfind(")")
    if left < 0 or right < left:
        return None
    f = raw[right + 2:].split()
    if len(f) < 22:
        return None
    return {"comm": raw[left + 1:right], "state": f[0], "ppid": int(f[1]),
            "majflt": int(f[9]), "utime": int(f[11]), "stime": int(f[12]),
            "child": int(f[13]) + int(f[14]), "threads": int(f[17]),
            "start": int(f[19]), "rss_pages": int(f[21])}


def parse_status(raw):
    out = {"uid": -1, "swap": 0, "anon": -1}
    for line in (raw or "").splitlines():
        if line.startswith("Uid:"):
            out["uid"] = int(line.split()[1])
        elif line.startswith("VmSwap:"):
            out["swap"] = int(line.split()[1]) * 1024
        elif line.startswith("RssAnon:"):
            out["anon"] = int(line.split()[1]) * 1024
    return out


def cgroup_leaf(raw):
    """Deepest .service/.scope unit on the cgroup path, else last path element, else ''."""
    for line in (raw or "").splitlines():
        path = line.split(":", 2)[-1].strip()
        units = [p for p in path.split("/") if p.endswith((".service", ".scope"))]
        if units:
            return units[-1]
        return path.rsplit("/", 1)[-1] if path not in ("", "/") else ""
    return ""


def cgroup_path(raw):
    for line in (raw or "").splitlines():
        return line.split(":", 2)[-1].strip()
    return ""


def is_kernel_thread(p):
    return p.pid == 2 or p.ppid == 2


def read_proc(pid, proc_root="/proc"):
    base = Path(proc_root) / str(pid)
    raw = _read(base / "stat")
    try:
        st = parse_stat(raw) if raw else None
    except ValueError:
        st = None
    if not st:
        return None
    status = parse_status(_read(base / "status"))
    cmd = _read(base / "cmdline")
    argv = [a for a in (cmd or "").split("\0") if a]
    try:
        cwd = os.readlink(base / "cwd")
    except OSError:
        cwd = ""
    io_bytes, read_bytes = -1, -1
    io = _read(base / "io")
    if io:
        vals = dict(l.split(": ", 1) for l in io.splitlines() if ": " in l)
        try:
            io_bytes = int(vals["rchar"]) + int(vals["wchar"])
        except (KeyError, ValueError):
            pass
        try:
            read_bytes = int(vals["read_bytes"])
        except (KeyError, ValueError):
            pass
    cg_raw = _read(base / "cgroup")
    return Proc(pid=pid, ppid=st["ppid"], comm=st["comm"], state=st["state"], uid=status["uid"],
                start_ticks=st["start"], cpu_ticks=st["utime"] + st["stime"],
                rss_bytes=st["rss_pages"] * PAGE, swap_bytes=status["swap"], threads=st["threads"],
                cwd=cwd, cmdline=argv, cgroup=cgroup_leaf(cg_raw), io_bytes=io_bytes,
                majflt=st["majflt"], read_bytes=read_bytes, anon_bytes=status["anon"],
                cgroup_path=cgroup_path(cg_raw), child_ticks=st["child"])


def scan(proc_root="/proc", limit=30000):
    procs = {}
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        p = read_proc(int(entry), proc_root)
        if p:
            procs[p.pid] = p
        if len(procs) >= limit:
            break
    return procs


def _docker_ps():
    return subprocess.run(
        ["docker", "ps", "--no-trunc", "--format",
         "{{.ID}}\t{{.Names}}\t{{.Label \"com.docker.compose.project\"}}"],
        capture_output=True, text=True, timeout=5, check=True).stdout


def docker_projects(runner=None):
    """container id -> compose project label verbatim, else container:<name>. {} on any failure."""
    try:
        out = (runner or _docker_ps)()
    except Exception:
        return {}
    mapping = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            mapping[parts[0]] = parts[2] or "container:" + parts[1]
    return mapping


def project_from_path(path):
    for rule in PATH_RULES:
        m = rule.match(path or "")
        if m:
            return m.group(1)
    return None


def unit_bucket(unit):
    """Strip pids/instance ids so app-orca-123.scope and app-orca-456.scope share a bucket."""
    if not unit or unit.endswith(".slice") or unit.startswith("session-"):
        return None
    name = unit.rsplit(".", 1)[0].replace("\\x2d", "-").split("@", 1)[0]
    return "unit:" + re.sub(r"[-_.]?\d{3,}", "", name)


def attribute(procs, docker=None):
    """docker label > cwd > any cmdline path > cgroup unit > unknown. Never guessed."""
    docker = docker or {}
    for p in procs.values():
        if is_kernel_thread(p):
            p.project, p.basis = "kernel", "kernel"
            continue
        m = DOCKER_ID.search(p.cgroup)
        if m and m.group(1) in docker:
            p.project, p.basis = docker[m.group(1)], "docker"
            continue
        proj = project_from_path(p.cwd.replace(" (deleted)", ""))
        if proj:
            p.project, p.basis = proj, "cwd"
            continue
        for arg in p.cmdline:
            proj = project_from_path(arg) or project_from_path(arg.split("=", 1)[-1])
            if proj:
                p.project, p.basis = proj, "cmdline"
                break
        else:
            bucket = unit_bucket(p.cgroup)
            if bucket:
                p.project, p.basis = bucket, "cgroup"
            else:
                p.project, p.basis = "unknown", "none"
    return procs


def cpu_deltas(before, after, wall_s):
    """pid -> cores used over the window. Only processes alive at both ends with the same
    start time count, so pid reuse cannot inflate a project."""
    out = {}
    for pid, b in after.items():
        a = before.get(pid)
        if a and a.key == b.key and wall_s > 0:
            out[pid] = max(0.0, (b.cpu_ticks - a.cpu_ticks) / CLK_TCK / wall_s)
    return out


def read_psi(root="/proc/pressure"):
    out = {}
    for kind in ("cpu", "memory", "io"):
        for line in (_read(Path(root) / kind) or "").splitlines():
            f = line.split()
            if not f or f[0] not in ("some", "full"):
                continue
            kv = dict(i.split("=", 1) for i in f[1:] if "=" in i)
            for k in ("avg10", "avg60", "avg300", "total"):
                if k in kv:
                    out[f"{kind}_{f[0]}_{k}"] = float(kv[k])
    return out


def read_mem(proc_root="/proc"):
    mi = {}
    for line in (_read(Path(proc_root) / "meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if v.split():
            mi[k] = int(v.split()[0]) * 1024
    vm = {}
    for line in (_read(Path(proc_root) / "vmstat") or "").splitlines():
        f = line.split()
        if len(f) == 2 and f[0] in ("pswpin", "pswpout"):
            vm[f[0]] = int(f[1])
    return {"swap_total": mi.get("SwapTotal", 0), "swap_free": mi.get("SwapFree", 0),
            "mem_total": mi.get("MemTotal", 0), "mem_available": mi.get("MemAvailable", 0), **vm}
