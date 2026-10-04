"""Evidence that a busy process reparented to systemd is a deliberate long-running service, not a leak.

Four independent signals, each read-only and batched (one command per signal per sample, never per pid):
  listening   an open listening tcp/udp/unix socket (``ss``)
  unit_main   the MainPID of the .service unit the process lives in (``systemctl --user show``)
  scope_leader the lowest pid of a small dedicated scope (a ``systemd-run --scope`` launch)
  pidfile     a pidfile that names it and was written after it started
A shell spin loop is never excused by listening/pidfile/scope evidence (that is the leak this exists to
catch); only being the MainPID of a unit excuses it, because that is an explicit decision to run it.
"""
import glob
import os
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from hl_procs import CLK_TCK

SS_PID = re.compile(r"pid=(\d+)")
SHARED_SCOPE = re.compile(r"^(app-orca|orca-daemon|session-|app-.*-\d+\.scope$)")
SMALL_SCOPE_MAX = 8
PIDFILE_GLOBS = ["/run/user/{uid}/*.pid", "/run/user/{uid}/*/*.pid",
                 "~/.local/state/*/*.pid", "~/.cache/nervous-bus/*.pid", "~/.cache/nervous-bus/*/*.pid"]


def _run(cmd, timeout=6):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def listening_pids(runner=None):
    runner = runner or _run
    pids = set()
    for cmd in (["ss", "-H", "-lntup"], ["ss", "-H", "-lxp"]):
        try:
            pids.update(int(m) for m in SS_PID.findall(runner(cmd)))
        except Exception:
            continue
    return pids


def unit_main_pids(units, runner=None):
    """{unit: MainPID} for the given .service units via a single `systemctl --user show`."""
    units = sorted({u for u in units if u.endswith(".service")})
    if not units:
        return {}
    try:
        out = (runner or _run)(["systemctl", "--user", "show", "-p", "Id", "-p", "MainPID", *units])
    except Exception:
        return {}
    mains, cur = {}, None
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "Id":
            cur = v
        elif k == "MainPID" and cur and v.isdigit() and int(v) > 0:
            mains[cur] = int(v)
    return mains


def pidfile_pids(globs=None, uid=None, procs=None, now=None, uptime_s=0.0):
    """pids named by a pidfile newer than the process's start (stale files with reused pids don't count)."""
    uid = os.getuid() if uid is None else uid
    found = set()
    for pat in globs or PIDFILE_GLOBS:
        for path in glob.glob(os.path.expanduser(pat.format(uid=uid))):
            try:
                pid = int(Path(path).read_text().split()[0])
                mtime = os.path.getmtime(path)
            except (OSError, ValueError, IndexError):
                continue
            p = (procs or {}).get(pid)
            if procs is not None and p is None:
                continue
            if p is not None and now and uptime_s:
                started = now - uptime_s + p.start_ticks / CLK_TCK
                if mtime + 2 < started:
                    continue
            found.add(pid)
    return found


def scope_leaders(procs, candidates):
    """{pid} among candidates that are the lowest pid of a small dedicated scope."""
    members = defaultdict(list)
    wanted = {p.cgroup_path for p in candidates if p.cgroup.endswith(".scope") and not SHARED_SCOPE.match(p.cgroup)}
    for p in procs.values():
        if p.cgroup_path in wanted:
            members[p.cgroup_path].append(p.pid)
    return {p.pid for p in candidates
            if p.cgroup_path in members and len(members[p.cgroup_path]) <= SMALL_SCOPE_MAX
            and p.pid == min(members[p.cgroup_path])}


def probe(procs, candidates, now=None, uptime_s=0.0, ss_runner=None, systemctl_runner=None, pidfile_globs=None):
    """{pid: [reasons]} for each candidate that looks like a deliberate service."""
    ev = defaultdict(list)
    if not candidates:
        return ev
    listening = listening_pids(ss_runner)
    mains = unit_main_pids({p.cgroup for p in candidates}, systemctl_runner)
    pidfiles = pidfile_pids(pidfile_globs, procs=procs, now=now or time.time(), uptime_s=uptime_s)
    leaders = scope_leaders(procs, candidates)
    for p in candidates:
        if mains.get(p.cgroup) == p.pid:
            ev[p.pid].append("unit_main:" + p.cgroup)
        if p.pid in leaders:
            ev[p.pid].append("scope_leader:" + p.cgroup)
        if p.pid in listening:
            ev[p.pid].append("listening_socket")
        if p.pid in pidfiles:
            ev[p.pid].append("pidfile")
    return ev


STRONG = ("unit_main:",)


def excused(reasons, is_spin_loop):
    """Spin loops are excused only by an explicit unit; anything else needs one signal."""
    if not reasons:
        return False
    if is_spin_loop:
        return any(r.startswith(STRONG) for r in reasons)
    return True
